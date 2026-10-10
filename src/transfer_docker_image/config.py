"""Small, dependency-free TOML configuration store."""

import json
import os
from pathlib import Path
import re
import sys
import tempfile
import tomllib

from .errors import UserError


def default_path():
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "transfer-docker-image/config.toml"


def check_name(name):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", name):
        raise UserError("Names must contain letters, digits, hyphens or underscores and start with a letter or digit.")
    return name


def _value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    raise UserError(f"Unsupported configuration value: {type(value).__name__}")


def dumps(data):
    lines = []

    def table(values, path):
        scalars = {key: value for key, value in values.items() if not isinstance(value, dict)}
        if path:
            lines.append("[" + ".".join(json.dumps(part) for part in path) + "]")
        for key, value in scalars.items():
            lines.append(f"{json.dumps(key)} = {_value(value)}")
        if path or scalars:
            lines.append("")
        for key, value in values.items():
            if isinstance(value, dict):
                table(value, [*path, key])

    table(data, [])
    return "\n".join(lines)


class Config:
    def __init__(self, path=None):
        self.path = Path(path).expanduser().resolve() if path else default_path()
        try:
            self.data = tomllib.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise UserError(f"Cannot read config {self.path}: {exc}") from exc
        for section in ("targets", "workflows"):
            self.data.setdefault(section, {})
            if not isinstance(self.data[section], dict):
                raise UserError(f"Config section {section} must be a table.")
            for name, value in self.data[section].items():
                check_name(name)
                if not isinstance(value, dict):
                    raise UserError(f"{section}.{name} must be a table.")

    def get(self, section, name):
        try:
            value = self.data[section][name]
        except KeyError:
            raise UserError(f"Unknown {section[:-1]} {name!r}. Use '{section[:-1]} list'.") from None
        if not isinstance(value, dict):
            raise UserError(f"{section}.{name} must be a table.")
        return value.copy()

    def local_path(self, value):
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (self.path.parent / path).resolve()

    def save(self):
        text = dumps(self.data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".config-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(text)
            temporary.replace(self.path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
