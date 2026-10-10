"""Interactive setup and scriptable management commands."""

import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .build import git_source, source_directory, validate_workflow
from .config import Config, check_name, dumps
from .errors import UserError
from .pipeline import run_workflow
from .process import Runner
from .ssh import Remote, discover_aliases, discover_keys


def prompt(label, default=None, *, required=False):
    if not sys.stdin.isatty():
        raise UserError(f"Missing {label}. Supply command options when running without a terminal; see --help.")
    suffix = f" [{default}]" if default not in (None, "") else ""
    value = input(f"{label}{suffix}: ").strip()
    value = value if value else default
    if value == "-":
        return None
    if required and not value:
        raise UserError(f"{label} is required.")
    return value


def parser():
    root = argparse.ArgumentParser(description="Build Docker images for saved targets and transfer them over SSH.")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--config", type=Path, help="TOML configuration path")
    commands = root.add_subparsers(dest="command", required=True)
    for section in ("target", "workflow"):
        group = commands.add_parser(section, help=f"Manage saved {section}s")
        actions = group.add_subparsers(dest="action", required=True)
        actions.add_parser("list")
        actions.add_parser("show").add_argument("name")
        actions.add_parser("remove").add_argument("name")
        if section == "target":
            actions.add_parser("test").add_argument("name")
        for action in ("add", "edit"):
            item = actions.add_parser(action)
            item.add_argument("name")
            item.add_argument("--non-interactive", action="store_true", help="Use only flags/defaults; never prompt")
            if section == "target":
                item.add_argument("--host", dest="ssh_host", help="SSH config alias, hostname, or IP address")
                item.add_argument("--user", dest="ssh_user")
                item.add_argument("--port", dest="ssh_port", type=int)
                item.add_argument("--identity", dest="identity_file", help="Private-key path; '-' clears it")
                item.add_argument("--host-os", choices=("windows", "macos", "linux"))
                item.add_argument("--shell", dest="remote_shell", choices=("posix", "powershell", "cmd"))
                item.add_argument("--platform", help="Container platform; '-' clears it for detection")
            else:
                item.add_argument("--source", help="Local directory or Git repository URL")
                item.add_argument("--git-ref", help="Branch, tag, or commit; '-' clears it")
                item.add_argument("--target", help="Saved target name")
                item.add_argument("--build-type", choices=("dockerfile", "compose"))
                item.add_argument("--dockerfile")
                item.add_argument("--context")
                item.add_argument("--image", help="Image name with explicit tag")
                item.add_argument("--compose-file")
                item.add_argument("--services", nargs="+", help="Compose services to build; '-' selects all buildable services")
                item.add_argument("--build-arg", action="append", default=[], metavar="KEY=VALUE")
                item.add_argument("--builder", help="Buildx builder name; '-' clears it")
    run = commands.add_parser("run", help="Build, transfer, and load a saved workflow")
    run.add_argument("name")
    run.add_argument("--builder", help="Override the workflow's Buildx builder")
    commands.add_parser("config-path", help="Print the configuration path")
    return root


def merge_flags(value, args, fields):
    for key in fields:
        update = getattr(args, key, None)
        if update is not None:
            if update == "-" or update == ["-"]:
                value.pop(key, None)
            else:
                value[key] = update


def configure_target(args, existing, config, runner):
    value = existing.copy()
    if args.ssh_host is not None and args.ssh_host != existing.get("ssh_host"):
        for field in ("host_os", "remote_shell", "platform", "engine_os_version"):
            value.pop(field, None)
    merge_flags(value, args, ("ssh_host", "ssh_user", "ssh_port", "identity_file", "host_os", "remote_shell", "platform"))
    interactive = not args.non_interactive and not any(getattr(args, field) is not None for field in
                      ("ssh_host", "ssh_user", "ssh_port", "identity_file", "host_os", "remote_shell", "platform"))
    if interactive:
        aliases = discover_aliases()
        if aliases:
            print("SSH aliases: " + ", ".join(aliases))
        value["ssh_host"] = prompt("SSH alias or hostname", value.get("ssh_host"), required=True)
        for field, label in (("ssh_user", "SSH username (blank uses SSH config; '-' clears)"),
                             ("ssh_port", "SSH port (blank uses SSH config; '-' clears)")):
            entered = prompt(label, value.get(field))
            if entered:
                try:
                    value[field] = int(entered) if field == "ssh_port" else entered
                except ValueError:
                    raise UserError("SSH port must be an integer.") from None
            else:
                value.pop(field, None)
        keys = discover_keys()
        for index, key in enumerate(keys, 1):
            print(f"  {index}. {key}")
        entered = prompt("SSH key path or number (blank uses agent/defaults; '-' clears)", value.get("identity_file"))
        if entered:
            if str(entered).isdigit() and 1 <= int(entered) <= len(keys):
                entered = str(keys[int(entered) - 1])
            value["identity_file"] = entered
        else:
            value.pop("identity_file", None)
        # Re-detect on an interactive edit; don't preserve stale host/platform values.
        for field in ("host_os", "remote_shell", "platform", "engine_os_version"):
            value.pop(field, None)
    if not value.get("ssh_host"):
        value["ssh_host"] = prompt("SSH alias or hostname", required=True) if not args.non_interactive else None
    detected = Remote(value, runner, config).probe()
    print(f"Connected: {detected['ssh_host']} ({detected['host_os']}, {detected['platform']})")
    return detected


def detect_project(source):
    compose = [name for name in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
               if (source / name).is_file()]
    dockerfiles = [p.name for p in source.glob("*Dockerfile*") if p.is_file()]
    if len(compose) == 1 and not dockerfiles:
        return "compose", compose[0]
    if len(dockerfiles) == 1 and not compose:
        return "dockerfile", dockerfiles[0]
    return None, None


def configure_workflow(args, existing, config, runner):
    value = existing.copy()
    fields = ("source", "git_ref", "target", "build_type", "dockerfile", "context", "image", "compose_file", "services", "builder")
    merge_flags(value, args, fields)
    interactive = not args.non_interactive and not any(getattr(args, field) is not None for field in fields) and not args.build_arg
    for field, label in (("source", "Project directory or Git URL"), ("target", "Target")):
        if interactive or not value.get(field):
            if args.non_interactive and not value.get(field):
                raise UserError(f"Missing --{field}.")
            if field == "target":
                print("Saved targets: " + ", ".join(config.data["targets"]))
            value[field] = prompt(label, value.get(field), required=True)
    config.get("targets", value["target"])
    if git_source(value["source"]) and interactive:
        ref = prompt("Git branch, tag or commit (blank uses default branch; '-' clears)", value.get("git_ref"))
        if ref:
            value["git_ref"] = ref
        else:
            value.pop("git_ref", None)
    if not git_source(value["source"]):
        # Capture a local interactive path relative to the current working directory.
        if interactive or args.source is not None:
            value["source"] = str(Path(value["source"]).expanduser().resolve())
        value.pop("git_ref", None)
    with source_directory(value, config, runner) as source:
        detected_type, detected_file = detect_project(source)
        if interactive or not value.get("build_type"):
            default = value.get("build_type") or detected_type
            if not args.non_interactive:
                value["build_type"] = prompt("Build type (dockerfile/compose)", default, required=True)
            elif default:
                value["build_type"] = default
            else:
                raise UserError("Ambiguous project: specify --build-type and its build file.")
        if value["build_type"] == "compose":
            default = value.get("compose_file") or (detected_file if detected_type == "compose" else None)
            if interactive or not default:
                if args.non_interactive and not default:
                    raise UserError("Missing --compose-file.")
                default = prompt("Compose file", default, required=True)
            value["compose_file"] = default
            if interactive:
                entered = prompt("Services (space-separated; blank builds all; '-' clears)", " ".join(value.get("services", [])))
                if entered:
                    value["services"] = entered.split()
                else:
                    value.pop("services", None)
            for field in ("dockerfile", "context", "image"):
                value.pop(field, None)
            build_file = source / value["compose_file"]
        elif value["build_type"] == "dockerfile":
            for field, label, default in (("dockerfile", "Dockerfile", detected_file if detected_type == "dockerfile" else "Dockerfile"),
                                           ("context", "Build context", "."), ("image", "Image name:tag", None)):
                default = value.get(field, default)
                if interactive or not default:
                    if args.non_interactive and not default:
                        raise UserError(f"Missing --{field}.")
                    default = prompt(label, default, required=True)
                value[field] = default
            for field in ("compose_file", "services"):
                value.pop(field, None)
            build_file = source / value["dockerfile"]
            if not (source / value["context"]).is_dir():
                raise UserError(f"Build context is not a directory: {value['context']}")
        else:
            raise UserError("Build type must be dockerfile or compose.")
        if not build_file.is_file():
            raise UserError(f"Build file does not exist: {build_file}")
    build_args = value.get("build_args", {}).copy()
    for arg in args.build_arg:
        if "=" not in arg or not arg.split("=", 1)[0]:
            raise UserError("Build arguments must use KEY=VALUE.")
        key, item = arg.split("=", 1)
        build_args[key] = item
    if build_args:
        value["build_args"] = build_args
    validate_workflow(value)
    return value


def dispatch(args, config, runner):
    if args.command == "config-path":
        print(config.path)
        return
    if args.command == "run":
        check_name(args.name)
        run_workflow(args.name, config.get("workflows", args.name), config, runner, args.builder)
        return
    section = args.command + "s"
    if args.action == "list":
        for name, value in sorted(config.data[section].items()):
            details = value.get("ssh_host", "") if section == "targets" else f"{value.get('source', '')} -> {value.get('target', '')}"
            print(f"{name}\t{details}")
        if not config.data[section]:
            print(f"No saved {section}. Use '{args.command} add NAME'.")
        return
    check_name(args.name)
    if args.action == "show":
        print(dumps({section: {args.name: config.get(section, args.name)}}))
    elif args.action == "test":
        detected = Remote(config.get(section, args.name), runner, config).probe()
        print(json.dumps(detected, indent=2))
    elif args.action == "remove":
        config.get(section, args.name)
        if section == "targets":
            references = [name for name, value in config.data["workflows"].items() if value.get("target") == args.name]
            if references:
                raise UserError("Target is used by workflows: " + ", ".join(references) + ". Edit or remove them first.")
        del config.data[section][args.name]
        config.save()
        print(f"Removed {args.command} {args.name}.")
    elif args.action in ("add", "edit"):
        if args.action == "add" and args.name in config.data[section]:
            raise UserError(f"{args.command.title()} {args.name!r} already exists; use edit.")
        existing = config.get(section, args.name) if args.action == "edit" else {}
        configure = configure_target if section == "targets" else configure_workflow
        value = configure(args, existing, config, runner)
        config.data[section][args.name] = value
        config.save()
        print(f"Saved {args.command} {args.name} in {config.path}")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        dispatch(args, Config(args.config), Runner())
        return 0
    except (UserError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Cancelled.", file=sys.stderr)
        return 130
