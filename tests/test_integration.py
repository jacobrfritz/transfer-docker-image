"""Opt-in real Docker builds; exports only, no containers or image loads."""

import os
from pathlib import Path
import tempfile
import unittest

from transfer_docker_image.build import build_images, check_builder, inspect_archive
from transfer_docker_image.process import Runner


@unittest.skipUnless(os.environ.get("TRANSFER_DOCKER_INTEGRATION") == "1", "set TRANSFER_DOCKER_INTEGRATION=1 to build with Docker")
class DockerIntegrationTests(unittest.TestCase):
    def test_cross_architecture_dockerfile_and_compose_exports(self):
        runner = Runner()
        runner.require("docker")
        source = Path(__file__).parent / "fixtures"
        platform = os.environ.get("TRANSFER_DOCKER_TEST_PLATFORM", "linux/amd64")
        check_builder(runner, platform)
        workflows = [
            {"build_type": "dockerfile", "dockerfile": "Dockerfile", "context": ".", "image": "transfer-docker-image-test:v1"},
            {"build_type": "compose", "compose_file": "compose.yaml", "build_args": {"VERSION": "override"}},
        ]
        for workflow in workflows:
            with self.subTest(kind=workflow["build_type"]), tempfile.TemporaryDirectory(prefix="transfer-integration-") as temporary:
                outputs = build_images("test-workflow", workflow, source, Path(temporary), platform, runner)
                for archive, image in outputs:
                    self.assertTrue(inspect_archive(archive, image, platform).startswith("sha256:"))
                if workflow["build_type"] == "compose":
                    self.assertEqual([image for _, image in outputs],
                                     ["transfer-docker-image-test-api:v1", "test-workflow-worker:latest"])

    def test_compose_dotted_service_with_external_build_context(self):
        runner = Runner()
        source = Path(__file__).parent / "fixtures/project"
        platform = os.environ.get("TRANSFER_DOCKER_TEST_PLATFORM", "linux/amd64")
        workflow = {"build_type": "compose", "compose_file": "compose.yaml", "services": ["api.jobs"]}
        with tempfile.TemporaryDirectory(prefix="transfer-integration-") as temporary:
            outputs = build_images("test-workflow", workflow, source, Path(temporary), platform, runner)
            self.assertEqual([image for _, image in outputs], ["test-workflow-api.jobs:latest"])
            self.assertTrue(inspect_archive(outputs[0][0], outputs[0][1], platform).startswith("sha256:"))
