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

### Agent machine

```bash
orbit init agent --agent-id agent-a
orbit agent run
```

## Runtime object flow

### 1) Upload package

```bash
orbit package upload --source-dir /path/to/project
```

Output:

```json
{
  "package_id": "sha256-...",
  "file_count": 42
}
```

### 2) Upload command

Example command file:

```json
{
  "argv": ["python3", "train.py", "--epochs", "1"],
  "env_patch": {},
  "timeout_sec": 3600,
  "working_dir": "."
}
```

Upload:

```bash
orbit command upload --file command.json
```

Output:

```json
{
  "command_id": "sha256-..."
}
```

### 3) Upload task

```bash
orbit task upload \
  --package-id <PACKAGE_ID> \
  --command-id <COMMAND_ID> \
  --created-by "$USER"
```

Output:

```json
{
  "task_id": "sha256-...",
  "package_id": "sha256-...",
  "command_id": "sha256-..."
}
```

### 4) Submit run

```bash
orbit run submit \
  --agent-id agent-a \
  --task-id <TASK_ID>
```

Output:

```json
{
  "run_id": "run-...",
  "agent_id": "agent-a",
  "task_id": "sha256-...",
  "run_ticket": "...",
  "expires_at": "..."
}
```

## Run inspection

### Status

```bash
orbit run status --run-id <RUN_ID>
```

### Logs

```bash
orbit run logs --run-id <RUN_ID>

orbit run logs --run-id <RUN_ID> --follow
```

Notes:

- `--follow` prints incremental stdout/stderr as chunks arrive.
- The Agent uploads a chunk every 10 seconds by default, or earlier when buffered output exceeds 16 KiB.

### Result

```bash
orbit run result --run-id <RUN_ID>
```

### Cancel

```bash
orbit run cancel --run-id <RUN_ID>
```

Result shape:

```json
{
  "run_id": "run-...",
  "result": {
    "status": "succeeded",
    "exit_code": 0,
    "started_at": "...",
    "finished_at": "..."
  }
}
```

## Suggested AI loop

1. modify files
2. upload package
3. write `command.json`
4. upload command
5. upload task
6. submit run
7. poll status
8. fetch logs/result or cancel the run
9. decide next code change
