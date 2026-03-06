# mvp-orbit

`mvp-orbit` is a small remote-debug runtime with two planes:

- Content plane: GitHub Release Assets store all sensitive objects.
- Control plane: Hub API stores only run metadata and object IDs.

The runtime model is built around three objects:

- `file_package`: a `.tar.gz` bundle of files selected from a directory.
- `command`: structured execution JSON (`argv`, `env_patch`, `timeout_sec`, `working_dir`).
- `task`: a signed binding of `package_id + command_id + constraints`.

Hub never receives package bytes, command JSON, task content, logs, or results. Agent only gets IDs from Hub, then pulls real objects from GitHub and verifies them locally.

## Security model

- `package_id = sha256(package_bytes)`
- `command_id = sha256(canonical_json(command))`
- `task_id = sha256(canonical_json(task))`
- `task_signature = Ed25519(task canonical bytes)`
- `run_ticket` is HMAC-protected, short-lived, and one-time through nonce replay protection

Agent execution checks, in order:

1. `run_ticket` validity and replay
2. `task_id` hash match
3. `task_signature` verification
4. `task.package_id / task.command_id` match the Hub lease IDs
5. `package_id` and `command_id` match downloaded GitHub objects

GitHub access is performed through `gh` CLI. `mvp-orbit` assumes the machine has already completed `gh auth login`.

## Storage model

v1 is GitHub-only. Objects are stored in a dedicated relay repository through GitHub Release Assets.

- `package/<package_id>.tar.gz`
- `command/<command_id>.json`
- `task/<task_id>.json`
- `log/<log_id>.json`
- `result/<result_id>.json`

The storage layer is abstracted behind `ObjectStoreBackend`, so later backends like S3 or Hugging Face can be added without changing the run flow.

## Quick start

### 1) Prerequisites

- Python 3.11+
- GitHub CLI (`gh`)
- A private GitHub relay repository
- `gh auth login` already completed on both the Hub/developer machine and the Agent machine
- Optional proxy via `HTTPS_PROXY`

The runtime now uses `gh` CLI for GitHub storage operations. You no longer need to pass `ORBIT_GITHUB_TOKEN` into `mvp-orbit`.

### 2) Generate task signing keypair

```bash
orbit keys generate
```

### 3) Prepare Hub secrets

You can provide them manually:

```bash
export ORBIT_TICKET_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
export ORBIT_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

If you do not provide them, `orbit hub serve` will generate both values at startup and print them to stdout.

Rules:

- `ORBIT_TICKET_SECRET` must be identical on Hub and Agent.
- `ORBIT_API_TOKEN` must match on Hub and all Hub clients.

### 4) Export GitHub relay settings

```bash
export ORBIT_GITHUB_OWNER="<owner>"
export ORBIT_GITHUB_REPO="<relay-repo>"
export ORBIT_GITHUB_RELEASE_PREFIX="mvp-orbit"
```

### 5) Start Hub

```bash
export ORBIT_HUB_HOST="127.0.0.1"
export ORBIT_HUB_PORT="8080"
export ORBIT_HUB_DB="./.orbit-hub/runs.sqlite3"

orbit hub serve
```

If `ORBIT_TICKET_SECRET` or `ORBIT_API_TOKEN` is missing, Hub will print generated values like:

```text
ORBIT_TICKET_SECRET=...
ORBIT_API_TOKEN=...
Generated missing Hub secrets for this process. Export the values above if other processes must reuse them.
```

Use those same values in Agent and CLI clients.

### 6) Upload a file package

`orbit package upload` is git-aware. If the source is inside a Git repo, it uses:

- `git ls-files --cached --others --exclude-standard`

That means `.gitignore` is respected. The selected files are packed into a deterministic `.tar.gz`, so the same content produces the same `package_id`.

```bash
orbit package upload \
  --source-dir /path/to/project \
  --github-owner "$ORBIT_GITHUB_OWNER" \
  --github-repo "$ORBIT_GITHUB_REPO" \
  --github-release-prefix "$ORBIT_GITHUB_RELEASE_PREFIX"
```

Output:

- `package_id`
- `file_count`

### 7) Upload a command object

Create `command.json`:

```json
{
  "argv": ["python3", "train.py", "--epochs", "1"],
  "env_patch": {
    "MODE": "debug"
  },
  "timeout_sec": 3600,
  "working_dir": "."
}
```

Upload it:

```bash
orbit command upload \
  --file command.json \
  --github-owner "$ORBIT_GITHUB_OWNER" \
  --github-repo "$ORBIT_GITHUB_REPO" \
  --github-release-prefix "$ORBIT_GITHUB_RELEASE_PREFIX"
```

Output:

- `command_id`

### 8) Upload a signed task object

```bash
orbit task upload \
  --package-id <PACKAGE_ID> \
  --command-id <COMMAND_ID> \
  --private-key <ORBIT_TASK_PRIVATE_KEY_B64> \
  --created-by "$USER" \
  --github-owner "$ORBIT_GITHUB_OWNER" \
  --github-repo "$ORBIT_GITHUB_REPO" \
  --github-release-prefix "$ORBIT_GITHUB_RELEASE_PREFIX"
```

Output:

- `task_id`
- `package_id`
- `command_id`

### 9) Submit a run

```bash
orbit run submit \
  --hub-url http://127.0.0.1:8080 \
  --agent-id agent-a \
  --task-id <TASK_ID> \
  --package-id <PACKAGE_ID> \
  --command-id <COMMAND_ID> \
  --api-token "$ORBIT_API_TOKEN"
```

Output:

- `run_id`
- `run_ticket`

### 10) Start Agent

```bash
export ORBIT_AGENT_ID="agent-a"
export ORBIT_HUB_URL="http://127.0.0.1:8080"
export ORBIT_API_TOKEN="$ORBIT_API_TOKEN"
export ORBIT_TICKET_SECRET="$ORBIT_TICKET_SECRET"
export ORBIT_TASK_PUBLIC_KEY_B64="<ORBIT_TASK_PUBLIC_KEY_B64>"

orbit agent run
```

The agent uses the same GitHub environment variables as the upload side, but it only needs read access.

### 11) Query status, logs, and result

```bash
orbit run status --hub-url http://127.0.0.1:8080 --run-id <RUN_ID> --api-token "$ORBIT_API_TOKEN"

orbit run logs \
  --hub-url http://127.0.0.1:8080 \
  --run-id <RUN_ID> \
  --api-token "$ORBIT_API_TOKEN" \
  --github-owner "$ORBIT_GITHUB_OWNER" \
  --github-repo "$ORBIT_GITHUB_REPO" \
  --github-release-prefix "$ORBIT_GITHUB_RELEASE_PREFIX"

orbit run result \
  --hub-url http://127.0.0.1:8080 \
  --run-id <RUN_ID> \
  --api-token "$ORBIT_API_TOKEN" \
  --github-owner "$ORBIT_GITHUB_OWNER" \
  --github-repo "$ORBIT_GITHUB_REPO" \
  --github-release-prefix "$ORBIT_GITHUB_RELEASE_PREFIX"
```

## Current scope

Implemented:

- `file_package + command + task` object model
- GitHub Release Asset object store backend
- Hub `/api` run submission, lease, heartbeat, completion, and status
- Agent pull-execute-complete loop
- Deterministic git-aware file packaging
- Task signature verification and run ticket replay protection
- Log/result objects stored in GitHub, referenced by Hub only through IDs

Not implemented yet:

- artifact upload/download
- streaming log chunks
- multiple object store backends
- richer task scheduling or placement constraints
