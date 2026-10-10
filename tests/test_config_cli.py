from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from transfer_docker_image.cli import main
from transfer_docker_image.config import Config, dumps
from transfer_docker_image.errors import UserError


class ConfigurationTests(unittest.TestCase):
    def test_toml_roundtrip_and_relative_paths(self):
        data = {"targets": {"my-server": {"ssh_host": "server", "ssh_port": 2222,
                                        "identity_file": r"C:\Users\Jake Smith\.ssh\id_ed25519"}},
                "workflows": {"app": {"source": "./project", "services": ["api", "worker"],
                                      "build_args": {"quote": 'a "quoted" value\nnext'}}}}
        self.assertEqual(tomllib.loads(dumps(data)), data)
        with tempfile.TemporaryDirectory() as temporary:
            config = Config(Path(temporary) / "settings/config.toml")
            config.data = data
            config.save()
            loaded = Config(config.path)
            self.assertEqual(loaded.data, data)
            self.assertEqual(loaded.local_path("project"), config.path.parent / "project")
            self.assertEqual(list(config.path.parent.glob(".config-*")), [])

    def test_invalid_config_is_actionable(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text("targets = []", encoding="utf-8")
            with self.assertRaisesRegex(UserError, "must be a table"):
                Config(path)

    def test_missing_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(UserError, "Unknown target"):
                Config(Path(temporary) / "config.toml").get("targets", "missing")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "config.toml"
        self.config = Config(self.path)
        self.config.data["targets"]["server"] = {"ssh_host": "server", "platform": "linux/amd64"}
        self.config.save()

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(["--config", str(self.path), *args])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_noninteractive_workflow_and_show(self):
        source = Path(self.temporary.name) / "project"
        source.mkdir()
        (source / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        status, _, error = self.invoke("workflow", "add", "api", "--non-interactive", "--source", str(source),
                                       "--target", "server", "--image", "api:v1", "--build-arg", "VERSION=1=2")
        self.assertEqual(status, 0, error)
        saved = Config(self.path).get("workflows", "api")
        self.assertEqual(saved["build_type"], "dockerfile")
        self.assertEqual(saved["build_args"], {"VERSION": "1=2"})
        status, text, _ = self.invoke("workflow", "show", "api")
        self.assertEqual(status, 0)
        self.assertEqual(tomllib.loads(text)["workflows"]["api"], saved)

    def test_referenced_target_cannot_be_removed(self):
        self.config.data["workflows"]["app"] = {"target": "server"}
        self.config.save()
        status, _, error = self.invoke("target", "remove", "server")
        self.assertEqual(status, 1)
        self.assertIn("used by workflows: app", error)
        self.assertIn("server", Config(self.path).data["targets"])

    def test_failed_connection_does_not_save_target(self):
        with patch("transfer_docker_image.cli.Remote.probe", side_effect=UserError("Authentication failed")):
            status, _, error = self.invoke("target", "add", "bad", "--host", "bad-host", "--non-interactive")
        self.assertEqual(status, 1)
        self.assertIn("Authentication failed", error)
        self.assertNotIn("bad", Config(self.path).data["targets"])

    def test_matching_ssh_alias_reuses_settings_without_setup_prompts(self):
        for flags in ([], ["--non-interactive"]):
            with self.subTest(flags=flags), \
                 patch("transfer_docker_image.cli.discover_aliases", return_value=["website_vps"]), \
                 patch("transfer_docker_image.cli.discover_keys", side_effect=AssertionError("must reuse SSH config")), \
                 patch("builtins.input", side_effect=AssertionError("must not prompt")), \
                 patch("transfer_docker_image.cli.Remote") as remote:
                remote.return_value.probe.return_value = {
                    "ssh_host": "website_vps", "host_os": "linux", "remote_shell": "posix", "platform": "linux/amd64"}
                status, output, error = self.invoke("target", "add", "website_vps", *flags)
                self.assertEqual(status, 0, error)
                self.assertIn("Using existing SSH alias website_vps", output)
                self.assertEqual(remote.call_args.args[0], {"ssh_host": "website_vps"})
                saved = Config(self.path).get("targets", "website_vps")
                self.assertNotIn("identity_file", saved)
                self.assertNotIn("ssh_user", saved)
                self.assertNotIn("ssh_port", saved)
            self.invoke("target", "remove", "website_vps")

    def test_selecting_alias_only_asks_for_the_alias(self):
        with patch("transfer_docker_image.cli.discover_aliases", return_value=["website_vps"]), \
             patch("transfer_docker_image.cli.discover_keys", side_effect=AssertionError("must reuse SSH config")), \
             patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", side_effect=["website_vps"]) as input_mock, \
             patch("transfer_docker_image.cli.Remote") as remote:
            remote.return_value.probe.return_value = {
                "ssh_host": "website_vps", "host_os": "linux", "remote_shell": "posix", "platform": "linux/amd64"}
            status, _, error = self.invoke("target", "add", "website")
            self.assertEqual(status, 0, error)
            input_mock.assert_called_once()
            self.assertEqual(remote.call_args.args[0], {"ssh_host": "website_vps"})

    def test_missing_flags_do_not_prompt_in_noninteractive_mode(self):
        with patch("builtins.input", side_effect=AssertionError("must not prompt")):
            status, _, error = self.invoke("workflow", "add", "app", "--non-interactive")
        self.assertEqual(status, 1)
        self.assertIn("Missing --source", error)

    def test_duplicate_add_preserves_existing_workflow(self):
        self.config.data["workflows"]["app"] = {"source": "existing"}
        self.config.save()
        status, _, error = self.invoke("workflow", "add", "app", "--source", "new")
        self.assertEqual(status, 1)
        self.assertIn("already exists", error)
        self.assertEqual(Config(self.path).data["workflows"]["app"], {"source": "existing"})
