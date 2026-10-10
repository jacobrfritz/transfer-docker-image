"""SSH target probing and host-specific remote operations."""

import base64
import glob
import json
from pathlib import Path
import re
import shlex
import uuid

from .errors import UserError


def normalize_platform(os_name, architecture, variant=None):
    arch = str(architecture).lower()
    arch = {"x86_64": "amd64", "x86-64": "amd64", "aarch64": "arm64",
            "i386": "386", "i686": "386", "armv7l": "arm/v7", "armv6l": "arm/v6"}.get(arch, arch)
    platform = f"{str(os_name).lower()}/{arch}"
    if variant and arch.count("/") == 0:
        platform += f"/{variant}"
    if not re.fullmatch(r"(linux|windows)/[a-z0-9_]+(?:/[a-z0-9_]+)?", platform):
        raise UserError(f"Unrecognized Docker platform {platform!r}; configure a supported platform explicitly.")
    return platform


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def powershell(script):
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return f"powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand {encoded}"


def discover_aliases(path=None):
    """Read only SSH config, following Includes; never read private keys."""
    path = Path(path or Path.home() / ".ssh/config")
    aliases, seen = set(), set()

    def visit(file):
        file = file.expanduser().resolve()
        if file in seen or not file.is_file():
            return
        seen.add(file)
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                parts = shlex.split(line, comments=True)
            except ValueError:
                continue
            if not parts:
                continue
            # OpenSSH allows 'Host=alias' as well as 'Host alias'.
            if "=" in parts[0]:
                key, value = parts.pop(0).split("=", 1)
                parts = [key, value, *parts]
            if parts[0].lower() == "host":
                aliases.update(name for name in parts[1:] if not any(c in name for c in "*?!"))
            elif parts[0].lower() == "include":
                for pattern in parts[1:]:
                    expanded = Path(pattern).expanduser()
                    if not expanded.is_absolute():
                        expanded = path.parent / expanded
                    for included in glob.glob(str(expanded)):
                        visit(Path(included))

    visit(path)
    return sorted(aliases)


def discover_keys(directory=None):
    directory = Path(directory or Path.home() / ".ssh")
    if not directory.is_dir():
        return []
    # Conventional names plus keys with companion public files. No content reads.
    keys = {p for p in directory.glob("id_*") if p.is_file() and p.suffix != ".pub"}
    keys.update(p.with_suffix("") for p in directory.glob("*.pub") if p.with_suffix("").is_file())
    return sorted(keys)


class Remote:
    def __init__(self, target, runner, config):
        self.target = target.copy()
        self.runner = runner
        self.config = config
        host = self.target.get("ssh_host", "")
        if not isinstance(host, str) or not host or host.startswith("-") or re.search(r"[\s/@]", host):
            raise UserError("ssh_host must be an SSH alias, hostname, or IP address; specify the user separately.")
        if "ssh_port" in self.target:
            port = self.target["ssh_port"]
            if not isinstance(port, int) or not 1 <= port <= 65535:
                raise UserError("ssh_port must be an integer between 1 and 65535.")
        if self.target.get("remote_shell") not in (None, "posix", "powershell", "cmd"):
            raise UserError("remote_shell must be posix, powershell, or cmd.")
        if self.target.get("identity_file"):
            identity = config.local_path(self.target["identity_file"])
            if not identity.is_file():
                raise UserError(f"SSH identity file does not exist: {identity}")

    def options(self):
        # Aliases may launch tmux/a shell via RemoteCommand or force a TTY.
        # SSH/SCP must instead execute the workflow's commands without a terminal.
        options = ["-o", "ConnectTimeout=15", "-o", "RemoteCommand=none", "-o", "RequestTTY=no"]
        for key, option in (("ssh_user", "User"), ("ssh_port", "Port")):
            if self.target.get(key) is not None:
                options += ["-o", f"{option}={self.target[key]}"]
        if self.target.get("identity_file"):
            options += ["-i", str(self.config.local_path(self.target["identity_file"])),
                        "-o", "IdentitiesOnly=yes"]
        return options

    def raw(self, command, *, check=True):
        return self.runner.run(["ssh", *self.options(), self.target["ssh_host"], command], check=check)

    @property
    def windows(self):
        return self.target.get("remote_shell") in ("powershell", "cmd")

    def script(self, posix, windows, *, check=True):
        if self.windows:
            # Explicit PowerShell also works when Windows OpenSSH's default shell is cmd.exe.
            windows = "$ErrorActionPreference='Stop'; " + windows
            return self.raw(powershell(windows), check=check)
        return self.raw(posix, check=check)

    def command(self, args, *, check=True):
        posix = shlex.join([str(arg) for arg in args])
        windows = "& " + " ".join(ps_quote(arg) for arg in args) + "; exit $LASTEXITCODE"
        return self.script(posix, windows, check=check)

    def probe(self):
        self.runner.require("ssh")
        if self.target.get("remote_shell") is None:
            result = self.raw("uname -s", check=False)
            host = result.stdout.strip().lower()
            if result.returncode == 0 and host in ("linux", "darwin"):
                self.target.update(host_os="macos" if host == "darwin" else "linux", remote_shell="posix")
            else:
                result = self.raw(powershell("[Environment]::OSVersion.Platform.ToString()"))
                if "Win32NT" not in result.stdout:
                    raise UserError("Cannot detect remote OS/shell. Set host_os and remote_shell explicitly.")
                self.target.update(host_os="windows", remote_shell="powershell")
        else:
            result = self.script("uname -s", "[Environment]::OSVersion.Platform.ToString()")
            actual = {"Linux": "linux", "Darwin": "macos", "Win32NT": "windows"}.get(result.stdout.strip())
            if not actual:
                raise UserError(f"Unsupported destination host: {result.stdout.strip()}")
            if self.target.get("host_os") and self.target["host_os"] != actual:
                raise UserError("Destination host OS changed. Edit and re-test the target.")
            self.target["host_os"] = actual
        result = self.command(["docker", "info", "--format", "{{json .}}"])
        try:
            info = json.loads(result.stdout)
            detected = normalize_platform(info["OSType"], info["Architecture"])
        except (ValueError, KeyError, TypeError) as exc:
            raise UserError("Remote Docker did not return its OS and architecture. Check remote Docker access.") from exc
        saved = self.target.get("platform")
        if saved and (not isinstance(saved, str) or saved.split("/")[:2] != detected.split("/")[:2]):
            raise UserError(f"Saved platform {saved!r} differs from remote Docker {detected}. Edit the target.")
        self.target["platform"] = saved or detected
        if detected.startswith("windows/"):
            self.target["engine_os_version"] = info.get("OSVersion") or info.get("KernelVersion", "")
        return self.target.copy()

    def mkdir(self):
        name = "transfer-docker-image-" + uuid.uuid4().hex
        result = self.script(
            f"mktemp -d \"${{TMPDIR:-/tmp}}/{name}.XXXXXX\"",
            f"$p=Join-Path ([IO.Path]::GetTempPath()) {ps_quote(name)}; "
            "New-Item -ItemType Directory -Path $p | Out-Null; [Console]::WriteLine($p)",
        )
        path = result.stdout.strip()
        if not path or "\n" in path:
            raise UserError("Could not determine remote temporary directory.")
        return path

    def remote_path(self, directory, filename):
        return directory.rstrip("/\\") + ("\\" if self.windows else "/") + filename

    def copy(self, archive, destination):
        host = self.target["ssh_host"]
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        path = destination.replace("\\", "/") if self.windows else destination
        # scp's default SFTP protocol takes paths literally, including spaces.
        self.runner.run(["scp", *self.options(), str(archive), f"{host}:{path}"], capture=False)

    def remove(self, directory):
        self.script(f"rm -rf -- {shlex.quote(directory)}",
                    f"Remove-Item -LiteralPath {ps_quote(directory)} -Recurse -Force", check=True)
