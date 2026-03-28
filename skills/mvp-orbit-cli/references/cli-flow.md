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

`orbit init hub` prints the bootstrap token used by `orbit connect`.

### User machine

```bash
orbit connect
```

`orbit connect` writes the local `user_token` and `expires_at`, then prints:

```text
ORBIT_AGENT_CONFIG_STRING=orbit-agent-config-string-v1:...
```

### Agent machine

Fast path with the config-string:

```bash
orbit init agent --config-string "$ORBIT_AGENT_CONFIG_STRING" --agent-id agent-a
orbit agent run
```

Interactive path:

```bash
orbit init agent
orbit agent run
```

Notes:

- `orbit init node` is an alias-equivalent path for the same Agent config flow.
- The config-string carries Hub URL, user token, token expiry, and optional workspace root.
- `agent_id` is local input, not part of the config-string.

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

### Parsing rules

- Prefer argv-style command execution:

```bash
orbit cmd exec --agent-id agent-a -- python3 -V
orbit cmd exec --agent-id agent-a -- ls -lah /tmp
```

- Use explicit `--shell` only when shell syntax is required:

```bash
orbit cmd exec \
  --agent-id agent-a \
  --shell \
  "cd /cache/models && HF_TOKEN=hf_xxx hf download repo --local-dir model-dir"
```

- Avoid sending a single opaque string without `--shell`, because that path relies on CLI heuristics and is easier to break with quoting or tokenization mistakes.
- Prefer `--env-file` over inline shell env assignments when possible.
- For long multi-step logic, prefer executing a script file instead of building a very long shell one-liner.

### Execute against a package

```bash
orbit cmd exec \
  --agent-id agent-a \
  --package-id <PACKAGE_ID> \
  -- python3 train.py --epochs 1
```

### Execute in the Agent base workspace

```bash
orbit cmd exec \
  --agent-id agent-a \
  -- pwd
```

### Execute a shell-style command

```bash
orbit cmd exec \
  --agent-id agent-a \
  --shell \
  "cd /cache/models && echo ready"
```

### Execute without waiting

```bash
orbit cmd exec \
  --agent-id agent-a \
  --package-id <PACKAGE_ID> \
  --detach \
  -- python3 train.py
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
orbit cmd status --command-id <COMMAND_ID>
orbit cmd output --command-id <COMMAND_ID>
orbit cmd output --command-id <COMMAND_ID> --follow
orbit cmd cancel --command-id <COMMAND_ID>
```

Notes:

- `cmd exec` waits and streams output unless `--detach` is used.
- `cmd output --follow` is the right choice when reattaching to a detached command.
- Prefer explicit argv or explicit `--shell`; do not depend on implicit single-string shell detection.
- `cmd exec` and `cmd output --follow` print a terminal summary on `stderr`.
- `cmd exec` and `cmd output --follow` exit with a local code mapped from the remote terminal state.
- `cmd output` returns stdout, stderr, offsets, and the current terminal or non-terminal status.
- Use `cmd cancel` to stop a queued or running command.

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
orbit shell list
orbit shell list --agent-id agent-a
orbit shell attach --session-id <SESSION_ID>
orbit shell close --session-id <SESSION_ID>
```

Notes:

- `shell start` prints the new `session_id` before attaching.
- `shell start --detach` leaves the shell running and returns the session record immediately.
- `shell attach` connects to a PTY-backed remote shell.
- To close a shell, use `shell close --session-id <SESSION_ID>`.

## Transport notes

- The product is HTTP-only.
- Realtime control and output use SSE streams.
- Agent output and control acknowledgements go back to the Hub through normal HTTP POST requests.
- The older polling endpoints and shared-config strings are no longer part of the current flow.

## Removed legacy flow

The following old steps are no longer part of the product:

- upload command JSON
- create a task object
- submit a run and poll by `run_id`
- use `ORBIT_AGENT_INIT`, `ORBIT_NODE_SHARED_CONFIG`, or `--shared-config`
- use any external storage service
