import base64
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

from transfer_docker_image.config import Config
from transfer_docker_image.errors import UserError
from transfer_docker_image.ssh import Remote, discover_aliases, discover_keys, normalize_platform


def result(stdout="", code=0):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr="")


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Config(Path(self.temporary.name) / "config.toml")
        self.runner = Mock()

    def test_apple_host_uses_linux_engine_platform(self):
        self.runner.run.side_effect = [result("Darwin\n"), result(json.dumps({"OSType": "linux", "Architecture": "aarch64"}))]
        remote = Remote({"ssh_host": "mac"}, self.runner, self.config)
        detected = remote.probe()
        self.assertEqual(detected["host_os"], "macos")
        self.assertEqual(detected["platform"], "linux/arm64")

    def test_windows_linux_container_detection(self):
        self.runner.run.side_effect = [result(code=1), result("Win32NT\n"),
                                       result(json.dumps({"OSType": "linux", "Architecture": "x86_64"}))]
        detected = Remote({"ssh_host": "windows"}, self.runner, self.config).probe()
        self.assertEqual(detected["host_os"], "windows")
        self.assertEqual(detected["platform"], "linux/amd64")

    def test_stale_engine_platform_is_rejected(self):
        self.runner.run.side_effect = [result("Linux\n"), result(json.dumps({"OSType": "linux", "Architecture": "aarch64"}))]
        with self.assertRaisesRegex(UserError, "differs from remote Docker"):
            Remote({"ssh_host": "server", "platform": "linux/amd64"}, self.runner, self.config).probe()

    def test_posix_quoting(self):
        self.runner.run.return_value = result()
        remote = Remote({"ssh_host": "server", "remote_shell": "posix"}, self.runner, self.config)
        remote.command(["docker", "load", "--input", "/tmp/Jake's files/image.tar"])
        command = self.runner.run.call_args.args[0][-1]
        import shlex
        self.assertEqual(shlex.split(command), ["docker", "load", "--input", "/tmp/Jake's files/image.tar"])

    def test_windows_cmd_uses_encoded_powershell_and_quotes_arguments(self):
        self.runner.run.return_value = result()
        remote = Remote({"ssh_host": "windows", "remote_shell": "cmd"}, self.runner, self.config)
        remote.command(["docker", "load", "--input", r"C:\Jake's files\image.tar"])
        command = self.runner.run.call_args.args[0][-1]
        script = base64.b64decode(command.split()[-1]).decode("utf-16-le")
        self.assertIn("'C:\\Jake''s files\\image.tar'", script)
        self.assertIn("exit $LASTEXITCODE", script)

    def test_scp_uses_same_identity_port_user_and_ipv6(self):
        identity = Path(self.temporary.name) / "key with spaces"
        identity.write_text("not-read", encoding="utf-8")
        remote = Remote({"ssh_host": "2001:db8::1", "ssh_user": "jake", "ssh_port": 2222,
                         "identity_file": str(identity), "remote_shell": "powershell"}, self.runner, self.config)
        remote.copy(Path("/local/image.tar"), r"C:\Users\Jake Smith\image.tar")
        args = self.runner.run.call_args.args[0]
        self.assertIn("User=jake", args)
        self.assertIn("Port=2222", args)
        self.assertIn(str(identity.resolve()), args)
        self.assertEqual(args[-1], "[2001:db8::1]:C:/Users/Jake Smith/image.tar")

    def test_invalid_host_cannot_be_an_ssh_option(self):
        with self.assertRaises(UserError):
            Remote({"ssh_host": "-oProxyCommand=bad"}, self.runner, self.config)

    def test_key_discovery_and_config_includes(self):
        root = Path(self.temporary.name)
        (root / "config").write_text(f'Include "{root / "extra"}"\nHost prod *.example !excluded\n', encoding="utf-8")
        (root / "extra").write_text("Host=lab\n", encoding="utf-8")
        key = root / "custom"
        key.write_text("never read", encoding="utf-8")
        (root / "custom.pub").write_text("public", encoding="utf-8")
        with unittest.mock.patch.object(Path, "read_text", side_effect=AssertionError("no key reads")):
            self.assertEqual(discover_keys(root), [key])
        self.assertEqual(discover_aliases(root / "config"), ["lab", "prod"])

    def test_other_architectures_and_variants(self):
        self.assertEqual(normalize_platform("linux", "riscv64"), "linux/riscv64")
        self.assertEqual(normalize_platform("linux", "armv7l"), "linux/arm/v7")
        self.assertEqual(normalize_platform("linux", "arm", "v6"), "linux/arm/v6")
