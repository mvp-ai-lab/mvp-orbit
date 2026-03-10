<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="./assets/logo-light.png">
    <img alt="mvp-orbit" src="./assets/logo-dark.png" width="560">
  </picture>
</p>

<div align="center">by MVP Lab.</div>

## Why Orbit

`mvp-orbit` is designed for one specific workflow:

1. prepare code on one machine
2. send it to a target machine
3. execute commands there
4. stream output back immediately
5. especially you cannot use SSH for some reason, or want something more repeatable than copy-paste

That makes it a good fit for:

- AI coding agents that need remote execution
- GPU/NPU/embedded debug loops
- build-on-one-box, run-on-another workflows

## Product Model

Orbit is built around three user-facing actions:

- `package`
  Build a deterministic `.tar.gz` from a directory and upload it to the Hub.
- `command exec`
  Send a command to a specific Agent. `package_id` is optional.
- `shell`
  Open a persistent remote shell session, with reconnect support.

Key runtime semantics:

- The Agent startup directory is the base workspace.
- Commands without `package_id` run directly in the base workspace.
- Commands with `package_id` run in a package-specific subdirectory under the base workspace.
- Shell sessions start in the base workspace by default, or in the package workspace when `package_id` is provided.
- Hub, CLI, and Agent communicate using HTTP + Bearer token only.

## Quick Start

### 1. Initialize the Hub

```bash
orbit init hub
orbit hub serve
```

`orbit init hub` prints:

- the Hub URL
- the API token
- `ORBIT_NODE_SHARED_CONFIG`

### 2. Initialize a node / Agent

```bash
orbit init node --agent-id agent-a
```

Or bootstrap from the Hub-generated shared string:

```bash
orbit init node --agent-id agent-a --shared-config "$ORBIT_NODE_SHARED_CONFIG"
```

Then start the Agent:

```bash
orbit agent run
```

## Core Flows

### Upload a package

```bash
orbit package upload --source-dir /path/to/project
```

Example response:

```json
{
  "package_id": "sha256-...",
  "size": 12345,
  "created_at": "2026-03-10T00:00:00+00:00"
}
```

### Execute a command against a package

```bash
orbit command exec \
  --agent-id agent-a \
  --package-id <PACKAGE_ID> \
  python3 train.py --epochs 1
```

### Execute a command directly in the Agent base workspace

```bash
orbit command exec \
  --agent-id agent-a \
  bash -lc 'pwd && ls'
```

### Submit without waiting

```bash
orbit command exec \
  --agent-id agent-a \
  --package-id <PACKAGE_ID> \
  --detach \
  python3 train.py
```

Then inspect it later:

```bash
orbit command status --command-id <COMMAND_ID>
orbit command output --command-id <COMMAND_ID>
orbit command output --command-id <COMMAND_ID> --follow
orbit command cancel --command-id <COMMAND_ID>
```

### Open a remote shell

Base workspace:

```bash
orbit shell start --agent-id agent-a
```

Package workspace:

```bash
orbit shell start --agent-id agent-a --package-id <PACKAGE_ID>
```

Reconnect or close:

```bash
orbit shell list
orbit shell list --agent-id agent-a
orbit shell attach --session-id <SESSION_ID>
orbit shell close --session-id <SESSION_ID>
```

While attached locally:

- `/detach` keeps the remote shell alive and disconnects your local session
- `/close` closes the remote shell
- `shell start` prints the new `session_id` before attaching so you can reattach or close it later

## Configuration

Default config path:

```text
~/.config/mvp-orbit/config.toml
```

Current config shape:

```toml
[hub]
host = "127.0.0.1"
port = 8080
db = "./.orbit-hub/hub.sqlite3"
object_root = "./.orbit-hub/objects"
url = "http://127.0.0.1:8080"

[auth]
api_token = "..."

[agent]
id = "agent-a"
workspace_root = "./workspace"
poll_interval_sec = 5.0
heartbeat_interval_sec = 5.0
```
