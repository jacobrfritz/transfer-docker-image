"""Buildx exports, resolved Compose builds, and archive validation."""

from contextlib import contextmanager
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import tempfile

from .errors import UserError
from .ssh import normalize_platform


def git_source(source):
    return bool(re.match(r"^(?:https?://|ssh://|git://|file://|git@|[^/\\\s]+@[^:]+:)", source))


def validate_workflow(workflow):
    for field in ("source", "target", "build_type"):
        if not isinstance(workflow.get(field), str) or not workflow[field]:
            raise UserError(f"Workflow requires a nonempty '{field}'.")
    if workflow["build_type"] not in ("dockerfile", "compose"):
        raise UserError("build_type must be dockerfile or compose.")
    for field in ("context", "dockerfile", "compose_file", "builder", "git_ref"):
        if field in workflow and (not isinstance(workflow[field], str) or not workflow[field]):
            raise UserError(f"Workflow '{field}' must be a nonempty string.")
    if workflow["build_type"] == "dockerfile":
        validate_image(workflow.get("image", ""))
    elif not isinstance(workflow.get("compose_file"), str) or not workflow["compose_file"]:
        raise UserError("Compose workflows require compose_file.")
    if "services" in workflow and (not isinstance(workflow["services"], list)
                                   or any(not isinstance(item, str) for item in workflow["services"])):
        raise UserError("services must be a list of service names.")
    args = workflow.get("build_args", {})
    if not isinstance(args, dict) or any(not isinstance(v, str) for v in args.values()):
        raise UserError("build_args must be a table of string values.")
    if workflow.get("git_ref") and not git_source(workflow["source"]):
        raise UserError("git_ref can only be used with a Git repository URL.")


def validate_image(image):
    if not isinstance(image, str) or not image or re.search(r"[\s,]", image) or image.startswith("-") or "@" in image:
        raise UserError("Specify an image name with a tag, for example api:latest (not a digest).")
    last = image.rsplit("/", 1)[-1]
    if ":" not in last or not re.fullmatch(r"[\w][\w.-]{0,127}", last.split(":", 1)[1], flags=re.ASCII):
        raise UserError(f"Image {image!r} requires a valid explicit tag, for example api:latest.")
    return image


@contextmanager
def source_directory(workflow, config, runner):
    source = workflow["source"]
    if not git_source(source):
        path = config.local_path(source)
        if not path.is_dir():
            raise UserError(f"Source directory does not exist: {path}")
        yield path
        return
    runner.require("git")
    with tempfile.TemporaryDirectory(prefix="transfer-source-") as temporary:
        path = Path(temporary) / "repo"
        runner.run(["git", "clone", "--", source, str(path)], capture=False)
        if workflow.get("git_ref"):
            ref = workflow["git_ref"]
            if ref.startswith("-"):
                raise UserError("Git refs cannot begin with '-'.")
            # Fetch explicitly so a branch outside the default checkout also works.
            runner.run(["git", "fetch", "origin", ref], cwd=path, capture=False)
            runner.run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=path, capture=False)
        runner.run(["git", "submodule", "update", "--init", "--recursive"], cwd=path, capture=False)
        commit = runner.run(["git", "rev-parse", "HEAD"], cwd=path).stdout.strip()
        print(f"Source commit: {commit}")
        yield path


def project_path(source, relative, description):
    path = (source / relative).resolve()
    if not path.exists():
        raise UserError(f"{description} does not exist: {path}")
    return path


def builder_args(builder):
    return ["--builder", builder] if builder else []


def check_builder(runner, platform, builder=None):
    runner.run(["docker", "info"], capture=True)
    runner.run(["docker", "buildx", "version"])
    inspection = runner.run(["docker", "buildx", "inspect", *([builder] if builder else []),
                             "--bootstrap"]).stdout
    available = set()
    # Buildx inspect has no --format option; each builder node has a Platforms line.
    for line in inspection.splitlines():
        if line.strip().startswith("Platforms:"):
            available.update(value.strip().rstrip("*") for value in line.split(":", 1)[1].split(","))
    if platform not in available:
        raise UserError(f"Builder cannot build {platform}. Available platforms: {', '.join(sorted(available)) or 'none'}. "
                        "Select a compatible builder with --builder or configure native nodes/emulation.")


def docker_output(path):
    stream = io.StringIO()
    csv.writer(stream, lineterminator="").writerow(["type=docker", f"dest={path.as_posix()}"])
    return stream.getvalue()


def bake_permissions(plan, source, workspace):
    """Grant Bake only declared local inputs and the temporary export directory."""
    roots = {source.resolve()}
    targets = json.loads(plan.read_text(encoding="utf-8"))["target"]

    def local_input(value):
        if not isinstance(value, str) or value.startswith(("target:", "docker-image:")) or "://" in value or git_source(value):
            return None
        path = (source / value).resolve()
        roots.add(path if path.is_dir() else path.parent)
        return path

    for target in targets.values():
        context = local_input(target.get("context", "."))
        for value in target.get("contexts", {}).values():
            local_input(value)
        if context and target.get("dockerfile"):
            local_input(str(context / target["dockerfile"]))
        for secret in target.get("secret", []):
            if isinstance(secret, dict):
                local_input(secret.get("src") or secret.get("source"))
            elif isinstance(secret, str):
                for part in next(csv.reader([secret])):
                    key, _, value = part.partition("=")
                    if key in ("src", "source"):
                        local_input(value)
    options = ["--allow", f"fs.write={workspace.resolve()}"]
    for path in sorted(roots):
        options += ["--allow", f"fs.read={path}"]
    return options


def compose_plan(name, workflow, source, workspace, platform, runner):
    compose_file = project_path(source, workflow["compose_file"], "Compose file")
    resolved = runner.json(["docker", "compose", "--profile", "*", "--project-name", name.lower(),
                            "-f", str(compose_file), "config", "--format", "json"], cwd=source)
    services = resolved.get("services", {})
    selected = list(dict.fromkeys(workflow.get("services") or [key for key, value in services.items() if "build" in value]))
    if not selected:
        raise UserError("Compose project has no buildable services.")
    for service in selected:
        if service not in services or "build" not in services[service]:
            raise UserError(f"Service {service!r} does not have a build definition.")
        definition = services[service]
        build = definition["build"]
        settings = build.get("platforms", []) if isinstance(build, dict) else []
        if (definition.get("platform") and definition["platform"] != platform) or (settings and settings != [platform]):
            raise UserError(f"Service {service!r} has platform settings conflicting with {platform}. Update the Compose settings.")
        image = definition.get("image", f"{name.lower()}-{service.lower()}:latest")
        if ":" not in image.rsplit("/", 1)[-1] and "@" not in image:
            image += ":latest"
        definition["image"] = validate_image(image)
    tags = [services[service]["image"] for service in selected]
    if len(tags) != len(set(tags)):
        raise UserError("Selected Compose services share an image tag. Assign unique tags before transferring them.")

    # JSON is valid YAML. A .yaml suffix makes Bake treat this as Compose, not Bake JSON.
    resolved_file = workspace / "resolved-compose.yaml"
    resolved_file.write_text(json.dumps(resolved), encoding="utf-8")
    bake = runner.json(["docker", "buildx", "bake", "-f", str(resolved_file), "--print", *selected], cwd=source)
    all_targets = bake.get("target", {})
    # Bake normalizes Compose service dots to underscores in its target names.
    target_names = {service: service.replace(".", "_") for service in services if "build" in services[service]}
    if len(set(target_names.values())) != len(target_names):
        raise UserError("Compose service names collide after Buildx replaces dots with underscores. Rename the conflicting services.")
    selected_targets = [target_names[service] for service in selected]
    targets = {}
    outputs = []
    for index, service in enumerate(selected):
        target_name = target_names[service]
        if target_name not in all_targets:
            raise UserError(f"Buildx did not resolve service {service!r} as a build target.")
        target = all_targets[target_name].copy()
        for context in target.get("contexts", {}).values():
            if isinstance(context, str) and context.startswith("target:") and context[7:] not in selected_targets:
                raise UserError(f"Build {service!r} requires service {context[7:]!r}; include it in workflow services.")
        target["platforms"] = [platform]
        target["tags"] = [services[service]["image"]]
        target["args"] = {**target.get("args", {}), **workflow.get("build_args", {})}
        archive = workspace / f"image-{index}.tar"
        target["output"] = [docker_output(archive)]
        targets[target_name] = target
        outputs.append((archive, target["tags"][0]))
    plan_file = workspace / "build.json"
    plan_file.write_text(json.dumps({"target": targets}), encoding="utf-8")
    return plan_file, selected_targets, outputs


def build_images(name, workflow, source, workspace, platform, runner, builder=None):
    if workflow["build_type"] == "compose":
        plan, selected, outputs = compose_plan(name, workflow, source, workspace, platform, runner)
        print("Building services: " + ", ".join(selected), flush=True)
        runner.run(["docker", "buildx", "bake", *builder_args(builder),
                    *bake_permissions(plan, source, workspace), "-f", str(plan), *selected],
                   cwd=source, capture=False)
        return outputs
    context = project_path(source, workflow.get("context", "."), "Build context")
    dockerfile = project_path(source, workflow.get("dockerfile", "Dockerfile"), "Dockerfile")
    archive = workspace / "image-0.tar"
    args = ["docker", "buildx", "build", *builder_args(builder), "--platform", platform,
            "--file", str(dockerfile), "--tag", workflow["image"], "--output", docker_output(archive)]
    for key, value in workflow.get("build_args", {}).items():
        args += ["--build-arg", f"{key}={value}"]
    runner.run([*args, str(context)], cwd=source, capture=False)
    return [(archive, workflow["image"])]


def inspect_archive(path, image, platform, windows_version=None):
    """Verify tags/platform and return the image ID without extracting files."""
    try:
        with tarfile.open(path) as archive:
            manifest_file = archive.extractfile("manifest.json")
            if manifest_file is None:
                raise UserError(f"Archive {path.name} has no Docker manifest.")
            manifest = json.load(manifest_file)
            if not isinstance(manifest, list) or any(not isinstance(item, dict) for item in manifest):
                raise UserError(f"Archive {path.name} has an invalid Docker manifest.")
            entry = next((item for item in manifest if image in item.get("RepoTags", [])), None)
            if entry is None:
                raise UserError(f"Archive {path.name} does not contain expected tag {image}.")
            config_file = archive.extractfile(entry["Config"])
            if config_file is None:
                raise UserError("Image archive is missing its config.")
            raw = config_file.read()
            image_config = json.loads(raw)
            actual = normalize_platform(image_config["os"], image_config["architecture"], image_config.get("variant"))
            if actual.split("/")[:2] != platform.split("/")[:2] or (platform.count("/") == 2 and actual != platform):
                raise UserError(f"Built image {image} is {actual}, expected {platform}.")
            if platform.startswith("windows/"):
                version = image_config.get("os.version")
                def windows_build(value):
                    match = re.search(r"(\d+\.\d+)[. ](\d+)", value or "")
                    return f"{match[1]}.{match[2]}" if match else None

                expected, built = windows_build(windows_version), windows_build(version)
                if not expected or not built or expected != built:
                    raise UserError(f"Windows image OS version {version!r} does not match destination {windows_version!r}. "
                                    "Use a base image compatible with the destination Windows build.")
            return "sha256:" + hashlib.sha256(raw).hexdigest()
    except (OSError, tarfile.TarError, ValueError, KeyError, TypeError) as exc:
        raise UserError(f"Invalid Docker archive {path}: {exc}") from exc
