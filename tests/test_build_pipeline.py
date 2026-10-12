from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from transfer_docker_image.build import bake_permissions, check_builder, compose_plan, inspect_archive
from transfer_docker_image.config import Config
from transfer_docker_image.errors import UserError
from transfer_docker_image.pipeline import run_workflow


def archive_fixture(path, image, os_name="linux", arch="amd64", os_version=None):
    config = {"os": os_name, "architecture": arch}
    if os_version:
        config["os.version"] = os_version
    raw = json.dumps(config).encode()
    image_id = "sha256:" + hashlib.sha256(raw).hexdigest()
    manifest = [{"Config": "config.json", "RepoTags": [image], "Layers": []}]
    with tarfile.open(path, "w") as tar:
        for name, content in (("manifest.json", json.dumps(manifest).encode()), ("config.json", raw)):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return image_id


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "compose.yaml").write_text("services: {}", encoding="utf-8")
        self.workflow = {"compose_file": "compose.yaml", "build_args": {"VERSION": "2"}}
        self.runner = Mock()
        self.resolved = {"services": {"api": {"build": {"context": str(self.root)}, "image": "api:dev"},
                                      "worker": {"build": {"context": str(self.root)}},
                                      "db": {"image": "postgres:17"}}}
        self.bake = {"target": {"api": {"context": str(self.root), "args": {"BASE": "1"}, "labels": {"keep": "yes"}},
                                 "worker": {"context": str(self.root)}}}

    def test_compose_only_exports_built_services_and_preserves_settings(self):
        self.runner.json.side_effect = [self.resolved, self.bake]
        plan, selected, outputs = compose_plan("app", self.workflow, self.root, self.root, "linux/amd64", self.runner)
        saved = json.loads(plan.read_text())
        self.assertEqual(selected, ["api", "worker"])
        self.assertNotIn("db", saved["target"])
        self.assertEqual([tag for _, tag in outputs], ["api:dev", "app-worker:latest"])
        api = saved["target"]["api"]
        self.assertEqual(api["args"], {"BASE": "1", "VERSION": "2"})
        self.assertEqual(api["labels"], {"keep": "yes"})
        self.assertEqual(api["platforms"], ["linux/amd64"])
        self.assertIn("type=docker", api["output"][0])
        self.assertEqual((self.root / "compose.yaml").read_text(), "services: {}")

    def test_explicit_service_selection_excludes_other_builds(self):
        self.runner.json.side_effect = [self.resolved, self.bake]
        self.workflow["services"] = ["worker"]
        plan, selected, _ = compose_plan("app", self.workflow, self.root, self.root, "linux/amd64", self.runner)
        self.assertEqual(selected, ["worker"])
        self.assertEqual(list(json.loads(plan.read_text())["target"]), ["worker"])

    def shared_image(self):
        self.resolved["services"]["worker"].update(image="api:dev", command=["run-worker"],
                                                   environment={"MODE": "worker"})
        self.bake["target"]["worker"] = {**self.bake["target"]["api"], "tags": ["api:dev"]}

    def test_shared_image_with_identical_builds_is_exported_once(self):
        self.shared_image()
        self.runner.json.side_effect = [self.resolved, self.bake]
        with redirect_stdout(io.StringIO()) as stdout:
            plan, selected, outputs = compose_plan("app", self.workflow, self.root, self.root, "linux/amd64", self.runner)
        self.assertEqual(selected, ["api"])
        self.assertEqual(list(json.loads(plan.read_text())["target"]), ["api"])
        self.assertEqual([image for _, image in outputs], ["api:dev"])
        self.assertIn("Shared image api:dev: api, worker", stdout.getvalue())

    def test_shared_tag_with_different_builds_reports_services_and_tag(self):
        for setting, value in (("dockerfile", "Dockerfile.worker"), ("target", "worker"),
                               ("args", {"BASE": "different"}), ("labels", {"keep": "different"})):
            with self.subTest(setting=setting):
                self.shared_image()
                self.bake["target"]["worker"][setting] = value
                self.runner.json.side_effect = [self.resolved, self.bake]
                with self.assertRaisesRegex(UserError, "'api' and 'worker' share image tag 'api:dev'.*build settings differ"):
                    compose_plan("app", self.workflow, self.root, self.root, "linux/amd64", self.runner)

    def test_workflow_overrides_are_applied_before_comparing_shared_builds(self):
        self.shared_image()
        self.bake["target"]["api"]["args"] = {"BASE": "1", "VERSION": "api"}
        self.bake["target"]["worker"]["args"] = {"BASE": "1", "VERSION": "worker"}
        self.runner.json.side_effect = [self.resolved, self.bake]
        with redirect_stdout(io.StringIO()):
            plan, _, outputs = compose_plan("app", self.workflow, self.root, self.root, "linux/amd64", self.runner)
        self.assertEqual(len(outputs), 1)
        self.assertEqual(json.loads(plan.read_text())["target"]["api"]["args"]["VERSION"], "2")

    def test_dependencies_on_deduplicated_services_are_redirected(self):
        self.shared_image()
        self.resolved["services"]["child"] = {"build": {"context": str(self.root)}, "image": "child:v1"}
        self.bake["target"]["child"] = {"context": str(self.root), "contexts": {"base": "target:worker"}}
        self.runner.json.side_effect = [self.resolved, self.bake]
        with redirect_stdout(io.StringIO()):
            plan, selected, outputs = compose_plan("app", self.workflow, self.root, self.root, "linux/amd64", self.runner)
        self.assertEqual(selected, ["api", "child"])
        self.assertEqual([image for _, image in outputs], ["api:dev", "child:v1"])
        self.assertEqual(json.loads(plan.read_text())["target"]["child"]["contexts"], {"base": "target:api"})

    def test_explicitly_selecting_one_shared_service_keeps_that_service(self):
        self.shared_image()
        self.workflow["services"] = ["worker"]
        self.runner.json.side_effect = [self.resolved, self.bake]
        plan, selected, outputs = compose_plan("app", self.workflow, self.root, self.root, "linux/amd64", self.runner)
        self.assertEqual(selected, ["worker"])
        self.assertEqual(list(json.loads(plan.read_text())["target"]), ["worker"])
        self.assertEqual([image for _, image in outputs], ["api:dev"])

    def test_prebuilt_selected_service_is_rejected(self):
        self.runner.json.return_value = self.resolved
        self.workflow["services"] = ["db"]
        with self.assertRaisesRegex(UserError, "does not have a build definition"):
            compose_plan("app", self.workflow, self.root, self.root, "linux/amd64", self.runner)

    def test_build_dependency_must_be_explicitly_selected(self):
        self.bake["target"]["worker"]["contexts"] = {"base": "target:api"}
        self.runner.json.side_effect = [self.resolved, self.bake]
        self.workflow["services"] = ["worker"]
        with self.assertRaisesRegex(UserError, "requires service 'api'"):
            compose_plan("app", self.workflow, self.root, self.root, "linux/amd64", self.runner)

    def test_conflicting_compose_platform_is_rejected(self):
        self.resolved["services"]["api"]["platform"] = "linux/arm64"
        self.runner.json.return_value = self.resolved
        with self.assertRaisesRegex(UserError, "conflicting"):
            compose_plan("app", self.workflow, self.root, self.root, "linux/amd64", self.runner)

    def test_builder_platforms_include_all_nodes(self):
        inspection = "Nodes:\nPlatforms: linux/arm64*\nPlatforms: linux/amd64, linux/riscv64\n"
        self.runner.run.return_value = subprocess.CompletedProcess([], 0, stdout=inspection)
        check_builder(self.runner, "linux/amd64")
        with self.assertRaisesRegex(UserError, "cannot build windows/amd64"):
            check_builder(self.runner, "windows/amd64")

    def test_archive_tag_and_platform_validation(self):
        archive = self.root / "image.tar"
        expected = archive_fixture(archive, "api:v1")
        self.assertEqual(inspect_archive(archive, "api:v1", "linux/amd64"), expected)
        with self.assertRaisesRegex(UserError, "expected tag"):
            inspect_archive(archive, "other:v1", "linux/amd64")
        with self.assertRaisesRegex(UserError, "expected linux/arm64"):
            inspect_archive(archive, "api:v1", "linux/arm64")

    def test_windows_os_version_checked(self):
        archive = self.root / "windows.tar"
        archive_fixture(archive, "app:v1", "windows", "amd64", "10.0.20348.2402")
        inspect_archive(archive, "app:v1", "windows/amd64", "10.0.20348.2600")
        inspect_archive(archive, "app:v1", "windows/amd64", "10.0 20348 (20348.2600.amd64fre)")
        with self.assertRaisesRegex(UserError, "does not match destination"):
            inspect_archive(archive, "app:v1", "windows/amd64", "10.0.17763.1")

    def test_bake_permissions_cover_declared_external_inputs_only(self):
        source = self.root / "project"
        source.mkdir()
        external = self.root / "external"
        external.mkdir()
        output = self.root / "output"
        output.mkdir()
        plan = self.root / "plan.json"
        plan.write_text(json.dumps({"target": {"app": {"context": str(external), "contexts": {
            "base": "target:base", "remote": "https://example.com/context.git"}}}}), encoding="utf-8")
        options = bake_permissions(plan, source, output)
        self.assertIn(f"fs.read={external.resolve()}", options)
        self.assertIn(f"fs.write={output.resolve()}", options)
        self.assertNotIn(f"fs.read={self.root.resolve()}", options)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = Config(self.root / "config.toml")
        self.config.data["targets"]["server"] = {"ssh_host": "server", "platform": "linux/amd64"}
        self.workflow = {"source": str(self.root), "target": "server", "build_type": "dockerfile", "image": "app:v1"}
        self.remote = Mock()
        self.remote.probe.return_value = {"platform": "linux/amd64"}
        self.remote.mkdir.return_value = "/tmp/remote-dir"
        self.remote.remote_path.side_effect = lambda directory, name: f"{directory}/{name}"

    def build(self, name, workflow, source, workspace, platform, runner, builder):
        path = workspace / "image-0.tar"
        image_id = archive_fixture(path, "app:v1")
        self.remote.command.side_effect = [subprocess.CompletedProcess([], 0, stdout="Loaded image: app:v1"),
                                           subprocess.CompletedProcess([], 0, stdout=image_id)]
        return [(path, "app:v1")]

    def execute(self):
        with patch("transfer_docker_image.pipeline.Remote", return_value=self.remote), \
             patch("transfer_docker_image.pipeline.check_builder"), \
             patch("transfer_docker_image.pipeline.build_images", side_effect=self.build), \
             redirect_stdout(io.StringIO()):
            run_workflow("app", self.workflow, self.config, Mock())

    def test_load_verifies_image_id_and_cleans_up(self):
        self.execute()
        self.remote.copy.assert_called_once()
        self.assertEqual(self.remote.command.call_args_list[0].args[0],
                         ["docker", "load", "--input", "/tmp/remote-dir/image-0.tar"])
        self.assertEqual(self.remote.command.call_args_list[1].args[0],
                         ["docker", "image", "inspect", "--format", "{{.Id}}", "app:v1"])
        self.remote.remove.assert_called_once_with("/tmp/remote-dir")

    def test_failed_copy_still_cleans_up_and_identifies_stage(self):
        self.remote.copy.side_effect = UserError("connection lost")
        with self.assertRaisesRegex(UserError, "during transfer .*connection lost"):
            self.execute()
        self.remote.command.assert_not_called()
        self.remote.remove.assert_called_once()

    def test_failed_load_still_cleans_up(self):
        original = self.build

        def build(*args):
            outputs = original(*args)
            self.remote.command.side_effect = UserError("no disk space")
            return outputs

        self.build = build
        with self.assertRaisesRegex(UserError, "during remote load .*no disk space"):
            self.execute()
        self.remote.remove.assert_called_once()

    def test_cleanup_failure_does_not_hide_transfer_failure(self):
        self.remote.copy.side_effect = UserError("transfer failed")
        self.remote.remove.side_effect = UserError("cleanup failed")
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaisesRegex(UserError, "transfer failed"):
            self.execute()
        self.assertIn("Cleanup failed", stderr.getvalue())

    def test_cleanup_failure_returns_error_after_successful_load(self):
        self.remote.remove.side_effect = UserError("permission denied")
        with self.assertRaisesRegex(UserError, "Cleanup failed.*permission denied"):
            self.execute()

    def test_wrong_remote_image_id_reports_loaded_tag(self):
        original = self.build

        def build(*args):
            outputs = original(*args)
            self.remote.command.side_effect = [subprocess.CompletedProcess([], 0, stdout="Loaded"),
                                               subprocess.CompletedProcess([], 0, stdout="sha256:wrong")]
            return outputs

        self.build = build
        with self.assertRaisesRegex(UserError, "Images already loaded: app:v1"):
            self.execute()
        self.remote.remove.assert_called_once()
