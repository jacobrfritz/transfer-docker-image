import tempfile
from pathlib import Path
import sys

from .build import build_images, check_builder, inspect_archive, source_directory, validate_workflow
from .errors import UserError
from .ssh import Remote


def run_workflow(name, workflow, config, runner, builder=None):
    stage = "validation"
    remote_dir = None
    remote = None
    loaded = []
    try:
        validate_workflow(workflow)
        runner.require("docker", "ssh", "scp")
        target = config.get("targets", workflow["target"])
        remote = Remote(target, runner, config)
        stage = "target validation"
        detected = remote.probe()
        platform = detected["platform"]
        builder = builder or workflow.get("builder")
        stage = "builder validation"
        check_builder(runner, platform, builder)
        stage = "source preparation"
        with source_directory(workflow, config, runner) as source:
            with tempfile.TemporaryDirectory(prefix="transfer-images-") as temporary:
                workspace = Path(temporary)
                print(f"Workflow {name}: build {platform} -> {workflow['target']} ({target['ssh_host']})", flush=True)
                stage = "build"
                outputs = build_images(name, workflow, source, workspace, platform, runner, builder)
                stage = "archive verification"
                ids = {image: inspect_archive(archive, image, platform, detected.get("engine_os_version"))
                       for archive, image in outputs}
                stage = "remote workspace creation"
                remote_dir = remote.mkdir()
                for archive, image in outputs:
                    destination = remote.remote_path(remote_dir, archive.name)
                    stage = f"transfer ({image})"
                    print(f"Transferring {image} ({archive.stat().st_size:,} bytes)", flush=True)
                    remote.copy(archive, destination)
                    stage = f"remote load ({image})"
                    result = remote.command(["docker", "load", "--input", destination])
                    print(result.stdout.strip())
                    # Record the load before verification, since Docker state already changed.
                    loaded.append(image)
                    stage = f"remote verification ({image})"
                    actual = remote.command(["docker", "image", "inspect", "--format", "{{.Id}}", image]).stdout.strip()
                    if actual != ids[image]:
                        raise UserError(f"Remote tag {image} resolves to {actual}, expected {ids[image]}.")
                print(f"Loaded into {workflow['target']} ({platform}): " + ", ".join(loaded))
    except (UserError, OSError) as exc:
        suffix = f" Images already loaded: {', '.join(loaded)}." if loaded else ""
        raise UserError(f"Workflow {name!r} failed during {stage}: {exc}{suffix}") from exc
    finally:
        if remote_dir and remote:
            failed = sys.exc_info()[0] is not None
            try:
                remote.remove(remote_dir)
            except (UserError, OSError) as exc:
                message = f"Cleanup failed for remote directory {remote_dir}: {exc}"
                if not failed:
                    raise UserError(message) from exc
                print(message, file=sys.stderr)
