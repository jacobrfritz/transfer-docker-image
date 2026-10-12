from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from transfer_docker_image import ui
from transfer_docker_image.cli import main, parser
from transfer_docker_image.config import Config


class MessageTests(unittest.TestCase):
    def test_tkinter_message_is_platform_specific(self):
        with patch.object(sys, "platform", "darwin"):
            self.assertIn("brew install python-tk@", ui.tkinter_message())
        with patch.object(sys, "platform", "linux"):
            self.assertIn("python3-tk", ui.tkinter_message())
        with patch.object(sys, "platform", "win32"):
            self.assertIn("python.org", ui.tkinter_message())


class ArgumentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Config(Path(self.temporary.name) / "config.toml")

    def test_cli_arguments_include_this_python_and_config(self):
        self.assertEqual(
            ui.cli_arguments(self.config, ["target", "list"]),
            [sys.executable, "-m", "transfer_docker_image", "--config", str(self.config.path), "target", "list"])

    def test_target_arguments_skip_blank_fields(self):
        fields = {"name": "ubuntu", "ssh_host": "server", "ssh_user": "", "ssh_port": 2222,
                  "identity_file": "~/.ssh/id_ed25519", "host_os": "", "remote_shell": "posix", "platform": ""}
        arguments = ui.target_arguments(fields, "add")
        self.assertEqual(arguments[:4], ["target", "add", "ubuntu", "--non-interactive"])
        self.assertEqual(arguments[4:], ["--host", "server", "--port", "2222",
                                         "--identity", "~/.ssh/id_ed25519", "--shell", "posix"])

    def test_workflow_arguments_include_services_and_build_args(self):
        fields = {"name": "app", "source": "/tmp/app", "target": "ubuntu", "build_type": "compose",
                  "compose_file": "compose.yaml", "services": "api worker", "build_args": "A=1\nB=2\n", "git_ref": ""}
        arguments = ui.workflow_arguments(fields, "add")
        index = arguments.index("--services")
        self.assertEqual(arguments[index + 1:index + 3], ["api", "worker"])
        self.assertEqual(arguments.count("--build-arg"), 2)
        self.assertNotIn("--git-ref", arguments)

    def test_key_install_arguments(self):
        fields = {"ssh_host": "server", "ssh_user": "jake", "ssh_port": "2222",
                  "identity_file": "~/.ssh/id_ed25519", "remote_shell": ""}
        self.assertEqual(ui.key_install_arguments(fields),
                         ["key", "install", "--host", "server", "--key", "~/.ssh/id_ed25519",
                          "--user", "jake", "--port", "2222"])


class DispatchTests(unittest.TestCase):
    def test_parser_accepts_ui_and_key_commands(self):
        self.assertEqual(parser().parse_args(["ui"]).command, "ui")
        install = parser().parse_args(["key", "install", "--host", "server", "--key", "~/.ssh/id_ed25519"])
        self.assertEqual((install.command, install.action, install.ssh_host, install.key_path),
                         ("key", "install", "server", "~/.ssh/id_ed25519"))
        generate = parser().parse_args(["key", "generate", "~/.ssh/id_ed25519"])
        self.assertEqual((generate.command, generate.action, generate.path),
                         ("key", "generate", "~/.ssh/id_ed25519"))

    def test_ui_command_starts_the_app(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("transfer_docker_image.ui.start") as start, \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                status = main(["--config", str(path), "ui"])
            self.assertEqual(status, 0, stderr.getvalue())
            start.assert_called_once()
