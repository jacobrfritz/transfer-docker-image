# transfer-docker-image

## Quick start

Install from this checkout with Python 3.11 or newer:

```text
pipx install .
```

Set up a target and a reusable workflow, then build and transfer:

```text
transfer-docker-image target add ubuntu
transfer-docker-image workflow add my-app
transfer-docker-image run my-app
```

Target setup offers existing SSH aliases and key files, tests remote Docker
access, and saves the destination platform. Workflow setup asks for a local
directory or Git URL, a Dockerfile or Compose configuration, and a saved target.
Use `--help` on any command for scriptable setup options.

When the target name matches an existing SSH alias, setup automatically reuses
that alias without asking you to re-enter its hostname, username, port, or key:

```text
transfer-docker-image target add website_vps
```

Selecting an existing alias during interactive setup also reuses its SSH
configuration and agent authentication. No new key is needed. SSH can still ask
for the server password or a key passphrase if your existing authentication
requires it; unlocked agent keys or configured connection sharing are reused.

For example, configure an existing SSH alias and a local Compose project without
interactive setup:

```text
transfer-docker-image target add ubuntu --host my-server --non-interactive
transfer-docker-image workflow add my-app --source ./my-app --target ubuntu --build-type compose --compose-file compose.yaml --non-interactive
transfer-docker-image run my-app
```

A Git-hosted Dockerfile workflow:

```text
transfer-docker-image workflow add api --source https://github.com/example/api.git --git-ref main --target ubuntu --build-type dockerfile --dockerfile Dockerfile --image api:latest --non-interactive
```

Use `--services api worker` to select Compose services, `--build-arg KEY=VALUE`
for non-secret build arguments, and `--builder NAME` to save a Buildx builder.
`run NAME --builder NAME` overrides the saved builder for that run. Target setup
accepts `--user`, `--port`, and `--identity` when an SSH alias is unavailable.

View saved settings and their location:

```text
transfer-docker-image config-path
transfer-docker-image target show ubuntu
transfer-docker-image workflow show my-app
transfer-docker-image target test ubuntu
```

The default config file is:

- Windows: `%APPDATA%\transfer-docker-image\config.toml`
- macOS: `~/Library/Application Support/transfer-docker-image/config.toml`
- Linux: `$XDG_CONFIG_HOME/transfer-docker-image/config.toml`, defaulting to
  `~/.config/transfer-docker-image/config.toml`.

You can edit TOML directly or use the `edit` commands. CLI writes are atomic and
normalize the file's formatting, including removing comments. `--config PATH`
is a global option placed before the command. CLI `--source` paths are resolved
from your current directory; relative sources written directly in TOML are
resolved from the config file's directory.

## Desktop interface

Open a window for managing targets and workflows and running transfers:

```text
transfer-docker-image ui
```

The app has Targets, Workflows, and Run tabs plus a shared output pane. Every
action runs through the same commands documented above, so validation, probing,
and transfers behave identically. SSH prompts for a server password or host-key
confirmation appear in the terminal that launched the app; keep it open during
setup.

Tkinter ships with python.org Python on Windows and macOS. Linux needs a Tk
package (`sudo apt install python3-tk` or `sudo dnf install python3-tkinter`).
Homebrew Python needs the matching `python-tk@X.Y` formula, for example
`brew install python-tk@3.14`. The `ui` command explains what to install when
Tkinter is missing.

### Passwordless SSH keys

Target setup already reuses existing SSH aliases, agent keys, and key files.
To create a new key and install it on a server:

```text
transfer-docker-image key generate ~/.ssh/id_ed25519
transfer-docker-image key install --host my-server --key ~/.ssh/id_ed25519
transfer-docker-image target add my-server --host my-server --identity ~/.ssh/id_ed25519 --non-interactive
```

The install step asks for the server password or host-key confirmation once in
the terminal. Existing keys are appended to `authorized_keys` only when absent,
with POSIX and Windows OpenSSH destinations both supported. Configuration stores
only the key path, never passwords or private-key contents. The `ui` Targets tab
offers generate, install, and agent steps as buttons.

## Specification

A command-line utility that builds Docker images on Windows, macOS, or Linux and
transfers them over SSH into Docker on another Windows, macOS, or Linux machine.
All source/destination host combinations are supported, subject to compatible
Docker engines and builder capabilities. Saved targets and named
workflows make repeated builds and transfers possible without re-entering the
connection or build settings.

The first version implements the workflow below. Its automated tests cover
configuration, CLI management, SSH command construction, platform detection,
Compose selection, image verification, and failure cleanup. Opt-in integration
tests exercise real Dockerfile and Compose builds; live SSH transfers and
Windows-container builds require validation against your destination machines.

## Goals

- Build images from a local project directory or a Git repository URL.
- Support projects using a Dockerfile or Docker Compose.
- Build for the destination Docker engine's container OS and CPU architecture,
  including building `linux/amd64` images on an Apple Silicon Mac. Support any
  platform the selected builder can build and the destination engine can use.
- Transfer only images built by the workflow and load them into remote Docker.
- Make SSH connection setup, testing, and reuse core features.
- Save reusable workflows describing what to build and where to send it.
- Support Windows, macOS, and Linux as both CLI hosts and destinations, including
  Ubuntu and transfers between different host operating systems.

## Scope

The first version ends when the built images have been loaded into Docker on the
destination server.

It does not:

- Start, restart, recreate, or deploy containers.
- Transfer Compose configuration, environment files, secrets, or application
  files to the server.
- Transfer Compose services that only use prebuilt images.
- Generate Dockerfiles or Compose files.
- Provision Docker, SSH, or the destination server.
- Push images to a registry.

Base images and other build dependencies may still be pulled during a build.
Excluding prebuilt Compose services applies to the images being transferred, not
to dependencies needed to build them.

## Requirements

### Distribution

The product is a command-line utility implemented in Python. Package it with a
CLI entry point for straightforward installation and upgrades, for example via
`pipx`. Packaging is a distribution mechanism; a public Python library API is
not a first-version requirement. Standalone executables that bundle Python may
be added later.

### Local machine

- Windows, macOS, or Linux.
- Python 3.11 or newer, with the command-line utility installed.
- Docker with a running engine and a recent Docker Buildx (0.26 or newer).
- Docker Compose v2 for Compose workflows.
- OpenSSH 9 or newer, including `ssh` and `scp` using its default SFTP protocol.
- Git for workflows using a Git repository URL.
- A Buildx builder capable of building for the target platform. Cross-architecture
  builds may require emulation support supplied by the Docker installation.

### Destination machine

- Windows, macOS, or Linux with an SSH server enabled and Docker installed.
- Windows destinations must have Windows PowerShell available. It is invoked
  explicitly for remote operations whether OpenSSH defaults to PowerShell or
  `cmd.exe`. The SSH server must support SFTP for SCP transfers.
- A running Docker engine accessible from the remote SSH session. On Windows
  and macOS this may be Docker Desktop's Linux engine; Windows may also use a
  Windows-container engine when the build environment supports it.
- The configured SSH user can load images into Docker without an interactive
  privilege prompt.
- Enough disk space for both the transferred archive and the loaded images.

## Key concepts

### Target

A target is a named destination containing:

- An SSH config alias, or a hostname and username.
- An optional port and private-key path.
- The destination host OS and remote command shell for path and command handling.
- A container target platform, such as `linux/amd64`, `linux/arm64`, or
  `windows/amd64`, with an optional architecture variant.

The tool detects the host OS and shell over SSH, then queries the remote Docker
engine for its container OS and architecture. Use the engine's platform rather
than inferring it from the host OS or `uname`: a Windows or macOS host can run a
Linux Docker engine. Normalize architecture names such as `x86_64` to `amd64`
and `aarch64` to `arm64`, preserving variants where applicable. Unknown values
require explicit supported settings rather than guesses.

Save the detected settings and allow user overrides. Revalidate them during
target tests and before builds, since the destination engine or its container
mode may have changed. Confirm that the selected local builder supports the
target platform before starting a build.

### Workflow

A workflow is a named build-and-transfer configuration containing:

- A source: a local directory or Git repository URL.
- An optional Git branch, tag, or commit for repository sources.
- A build type: Dockerfile or Compose.
- Build settings, including file paths, image tags, and optional build arguments.
- A destination target.

The target's saved platform is used for the build. Each workflow produces images
for one destination platform per run.

## Configuration

Store configuration in a user-level TOML file using the standard per-user
configuration directory for each operating system. Allow an explicit config
path through `--config` so users can maintain separate configurations.

Configuration stores connection metadata and key paths, never private-key
contents or passwords. SSH agent and key passphrase handling are delegated to
the installed SSH client. Configuration values must not contain secrets;
projects requiring build secrets need a separate secret mechanism rather than
saved plaintext build arguments.

Example configuration:

```toml
[targets.ubuntu]
ssh_host = "my-server" # Alias from the user's SSH config
host_os = "linux"
remote_shell = "posix"
platform = "linux/amd64"

[targets.lab]
ssh_host = "192.0.2.10"
ssh_user = "jake"
ssh_port = 22
identity_file = "~/.ssh/id_ed25519"
host_os = "linux"
remote_shell = "posix"
platform = "linux/arm64"

[targets.mac]
ssh_host = "my-mac"
host_os = "macos"
remote_shell = "posix"
platform = "linux/arm64"

[targets.windows]
ssh_host = "my-windows-pc"
host_os = "windows"
remote_shell = "powershell"
platform = "linux/amd64" # Docker Desktop in Linux-container mode

[workflows.my-app]
source = "~/projects/my-app"
build_type = "compose"
compose_file = "compose.yaml"
target = "ubuntu"

[workflows.api]
source = "https://github.com/example/api.git"
git_ref = "main"
build_type = "dockerfile"
context = "."
dockerfile = "Dockerfile"
image = "api:latest"
target = "ubuntu"

[workflows.api.build_args]
APP_ENV = "production"
```

Paths within a project, such as `context`, `dockerfile`, and `compose_file`, are
relative to the source directory. Local source and identity paths support home
directory expansion. Relative local source paths are resolved relative to the
configuration file. Platform-native absolute paths are supported on Windows,
macOS, and Linux. Local paths use the source host's conventions; remote temporary
paths use the destination host's conventions.

For Git sources, omit `git_ref` to use the repository's default branch. Clone into
a temporary workspace for each run and report the resolved commit. Private
repository authentication uses the user's existing Git credentials or SSH setup.

## SSH connection management

Interactive target setup must:

1. Let the user choose an existing SSH config alias or enter a hostname and
   username, with an optional port. Automatically reuse an alias matching the
   new target's name, including in non-interactive setup.
2. For existing aliases, inherit authentication and connection details without
   redundant prompts. For new hostnames or edits to explicit overrides, offer
   SSH agent/default authentication or selection of an existing private key.
   Discover candidate keys in the user's SSH directory without reading or
   copying private-key contents.
3. Test the connection using the selected settings.
4. Check that remote Docker is available and accessible to the SSH user.
5. Detect the destination host OS and command shell, query the Docker engine's
   container OS and architecture, and confirm the target platform.
6. Save the target after successful validation.

Use the installed SSH client's configuration and host-key verification behavior.
SSH aliases must retain settings such as their configured user, identity,
port, and proxy connection. Explicit target settings override corresponding SSH
config defaults. Key discovery is a convenience; a connection test determines
whether authentication succeeds.

For automation, SSH and SCP override `RemoteCommand` with `none` and
`RequestTTY` with `no`. This lets aliases that normally start tmux or an
interactive shell execute the tool's Docker commands. Connection and
authentication settings, including configured connection sharing, are inherited.

Users must be able to list, test, edit, and remove saved targets. Removing a
target referenced by a workflow must produce an actionable error.

Remote operations must support POSIX shells on macOS and Linux, and PowerShell
or `cmd.exe` on Windows. Use shell-appropriate commands and quoting for Docker
invocations, temporary directory creation, and cleanup. SCP paths must work with
the destination's SSH server and filesystem, including paths containing spaces.

## Build behavior

### Dockerfile projects

- Build the configured context and Dockerfile with Docker Buildx.
- Require an explicit image name and tag during workflow setup.
- Apply the destination platform and configured build arguments.
- Produce a Docker-loadable image archive for the destination platform.

### Compose projects

- Resolve the Compose configuration using Docker Compose, including its normal
  interpolation and build-context rules.
- Select services with a `build:` definition. A service containing both `build:`
  and `image:` is included; a service containing only `image:` is excluded.
- Support an optional saved list of services to build. Reject selected services
  that have no build definition. Fail clearly if no buildable services remain.
- Build the selected services for the destination platform using Buildx.
  Conflicting Compose platform settings must produce an actionable error.
- Preserve explicit image names and tags. For services without `image:`, assign
  a stable name such as `<workflow>-<service>:latest` and report that name.
- When selected services share an image tag and have identical effective build
  settings, build, export, transfer, and load that image once. Runtime settings
  such as commands, environment variables, and volumes do not affect this check.
  Compare the resolved Buildx settings after applying the destination platform
  and workflow build arguments. If the builds differ, report the services and
  shared tag and require unique tags or matching build settings.
- Preserve build dependencies when deduplicating shared images by redirecting
  service build-context references to the retained build target.
- Export only the selected build outputs. Do not include unrelated local images
  or prebuilt service images.
- Do not start services or build non-selected services implicitly. If selected
  builds require additional buildable services, explain the dependency and ask
  the user to include them in the workflow.

The tool must not rewrite the user's Dockerfile or Compose files to apply these
settings.

### Architecture and archive format

Docker on macOS already builds Linux container images. An M4 Mac commonly defaults
to `linux/arm64`, while an Intel or AMD Linux engine commonly needs
`linux/amd64`. Docker Desktop on Windows can also run Linux containers.

The tool addresses this by selecting the destination platform **during the
build**, then exporting a Docker-loadable archive. Changing an archive format
does not convert an image's CPU architecture or container OS. macOS is a host
platform, not a Docker container target OS.

Cross-architecture builds require appropriate builder support, such as emulation
or native builder nodes. Windows-container builds require a compatible Windows
builder, base images, and destination Windows engine; Linux emulation alone does
not supply that capability. The tool uses the selected Buildx builder, which may
include remote nodes, and reports unsupported combinations before building. No
separate architecture or container-OS conversion step is part of this workflow.

Windows image validation currently requires the image and destination Windows
major/minor/build numbers to match; revision differences are allowed. This is a
conservative compatibility check rather than support for every Windows
cross-version or isolation-mode combination.

## Transfer behavior

1. Validate the source, local tools, target connection, remote Docker access,
   destination host/shell settings, and build platform before building.
2. Build all selected images and export them into a local temporary archive or
   archives that can be loaded by Docker.
3. Create a unique temporary directory on the destination over SSH.
4. Transfer the archive or archives with SCP using the target's SSH settings.
5. Run `docker load` over SSH and verify that each expected image tag resolves to
   the image ID from its exported archive.
6. Report the destination, platform, and loaded image tags.
7. Clean up temporary local and remote artifacts on success, and attempt cleanup
   on failure without obscuring the original error.

Repeated runs may replace existing remote image tags. They do not update running
containers. A failed run must return a nonzero exit code and identify the stage
that failed. Multiple remote loads are not atomic; if a later load fails, report
which images were already loaded.

For example, update a running Compose deployment on the destination after a run:

```text
# in the VPS directory that has docker-compose.prod.yml + .env
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f migrate app
```

`up -d` recreates only the services whose images changed; `ps` and `logs` are
optional checks.

## Command-line interface

```text
# Configure and validate a destination interactively
transfer-docker-image target add ubuntu

# Inspect and maintain destinations
transfer-docker-image target list
transfer-docker-image target test ubuntu
transfer-docker-image target edit ubuntu
transfer-docker-image target remove ubuntu

# Create a reusable build-and-transfer workflow interactively
transfer-docker-image workflow add my-app

# Inspect and maintain workflows
transfer-docker-image workflow list
transfer-docker-image workflow show my-app
transfer-docker-image workflow edit my-app
transfer-docker-image workflow remove my-app

# Build, transfer, and load the workflow's images
transfer-docker-image run my-app

# Use a specific configuration file
transfer-docker-image --config ./transfer.toml run my-app
```

Workflow setup asks for the source, Git ref when applicable, build type and
settings, and destination. A single detected Dockerfile or Compose file may be
offered as a default; ambiguous projects require a user selection. Normal runs
reuse saved settings without configuration prompts, although SSH or Git may
require authentication or host-key confirmation.

## Acceptance criteria

- On an M4 Mac, a saved workflow targeting an `x86_64` Ubuntu server builds
  `linux/amd64` images and loads them into the server's Docker engine.
- The CLI can be configured and run on Windows, macOS, and Linux using native
  paths and installed Docker, Git, and OpenSSH tools.
- Build-and-transfer workflows work for all nine combinations of Windows,
  macOS, and Linux source and destination hosts when using compatible Linux
  container engines and builders.
- Platform detection uses the destination Docker engine, correctly selecting
  Linux container platforms on Windows and macOS hosts.
- Supported target CPU architectures and variants are not restricted to
  `amd64` and `arm64`; builder and destination compatibility determine support.
- Windows-container workflows build and load images when a compatible Windows
  builder and destination engine are available. Incompatible container OS,
  architecture, or Windows version combinations produce actionable errors.
- Remote temporary files, SCP transfer, Docker commands, and cleanup work with
  POSIX shells, PowerShell, and `cmd.exe`, including paths containing spaces.
- A local Dockerfile project and a Git-hosted project with a selected branch,
  tag, or commit can each be built and transferred.
- A Compose project containing both buildable and prebuilt services transfers
  only the selected buildable services' images.
- Compose services such as an application and migration runner can share an
  image tag with identical builds; the workflow produces one archive for that
  tag. Conflicting builds sharing a tag fail before building or transferring.
- SSH aliases, SSH agent authentication, and explicit identity-file connections
  can each be saved and tested.
- A saved workflow can be rerun without re-entering its source, target, or build
  settings.
- Missing prerequisites, authentication errors, unsupported platforms, build
  failures, transfer failures, and remote load failures produce actionable
  messages and nonzero exit codes.
- Successful runs report the loaded image tags and clean up temporary archives.
- Runs leave container lifecycle management to the user.

## Development and verification

Install in an activated virtual environment and run the tests:

```text
python -m pip install -e .
python -m unittest discover -s tests -v
```

To include real Docker builds on macOS/Linux:

```sh
TRANSFER_DOCKER_INTEGRATION=1 python -m unittest discover -s tests -v
```

On Windows PowerShell:

```powershell
$env:TRANSFER_DOCKER_INTEGRATION = "1"
python -m unittest discover -s tests -v
```

The integration fixture builds from `scratch` and exports archives without
starting containers or loading images into the local engine. It targets
`linux/amd64` by default; set `TRANSFER_DOCKER_TEST_PLATFORM` to another platform
supported by your builder. Docker may retain build cache. CI runs unit tests on
Windows, macOS, and Linux, plus real Docker builds on Linux.
