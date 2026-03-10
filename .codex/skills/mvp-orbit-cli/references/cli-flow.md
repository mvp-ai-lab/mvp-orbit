# mvp-orbit CLI flow

## Default config

- Default path: `~/.config/mvp-orbit/config.toml`
- Override with:
  - `--config /path/to/config.toml`
  - `ORBIT_CONFIG=/path/to/config.toml`

## Initialization

### Hub machine

```bash
orbit init hub
orbit hub serve
```

`orbit init hub` prints:

- the Hub URL
- the API token
- `ORBIT_NODE_SHARED_CONFIG`

### Agent machine

```bash
orbit init node --agent-id agent-a
orbit agent run
```

Or bootstrap from the Hub-generated shared string:

```bash
orbit init node --agent-id agent-a --shared-config "$ORBIT_NODE_SHARED_CONFIG"
orbit agent run
```

## Package flow

### Upload package

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

## Command flow

### Execute against a package

```bash
orbit command exec \
  --agent-id agent-a \
  --package-id <PACKAGE_ID> \
  python3 train.py --epochs 1
```

### Execute in the Agent base workspace

```bash
orbit command exec \
  --agent-id agent-a \
  bash -lc 'pwd && ls'
```

### Execute without waiting

```bash
orbit command exec \
  --agent-id agent-a \
  --package-id <PACKAGE_ID> \
  --detach \
  python3 train.py
```

Example detached response:

```json
{
  "command_id": "cmd-...",
  "agent_id": "agent-a",
  "status": "queued",
  "package_id": "sha256-..."
}
```

### Inspect a command

```bash
orbit command status --command-id <COMMAND_ID>
orbit command output --command-id <COMMAND_ID>
orbit command output --command-id <COMMAND_ID> --follow
orbit command cancel --command-id <COMMAND_ID>
```

Notes:

- `command exec` waits and streams output unless `--detach` is used.
- `command output --follow` is the right choice when reattaching to a detached command.
- `command output` returns stdout, stderr, offsets, and the current terminal or non-terminal status.
- Use `command cancel` to stop a queued or running command.

## Shell flow

### Start a shell

Base workspace:

```bash
orbit shell start --agent-id agent-a
```

Package workspace:

```bash
orbit shell start --agent-id agent-a --package-id <PACKAGE_ID>
```

### Reattach or close

```bash
orbit shell attach --session-id <SESSION_ID>
orbit shell close --session-id <SESSION_ID>
```

While attached locally:

- `/detach` keeps the remote shell alive and disconnects the local terminal
- `/close` closes the remote shell

## Removed legacy flow

The following old steps are no longer part of the product:

- upload command JSON
- create a task object
- submit a run and poll by `run_id`
- use any external storage service
