---
name: mvp-orbit
description: Use this skill when working in the mvp-orbit repository, explaining or changing its host/client peer command channel, CLI workflows, join approval model, command execution, interactive shell, file transfer, logging, tests, or documentation.
---

# MVP Orbit

## Purpose

`mvp-orbit` is an HTTP-only peer command channel. One process runs the control host, and each machine runs a foreground client loop after joining a named channel. Approved members in the same channel can run commands, open shells, and transfer files through the host without direct client-to-client networking.

Treat the current implementation as the source of truth. Keep the public surface compact and focused on the supported host/client workflow.

## Architecture

- `src/mvp_orbit/cli/main.py` defines the public `orbit` CLI.
- `src/mvp_orbit/hub/app.py` exposes the FastAPI host endpoints and SSE streams.
- `src/mvp_orbit/hub/store.py` owns SQLite state for channels, member tokens, join requests, clients, commands, shells, files, and cleanup.
- `src/mvp_orbit/client/` runs the foreground client loop and executes received work.
- `src/mvp_orbit/core/models.py` contains shared Pydantic request/response models.
- `src/mvp_orbit/core/logging.py` contains the structured runtime log formatter.
- `src/mvp_orbit/config.py` loads and saves local CLI/client configuration.
- `tests/` covers command, approval, config, host, and file-transfer behavior.

## User Workflow

Start the control host:

```bash
orbit host
```

Join the first client in a channel. This creates the channel and starts the foreground client loop:

```bash
orbit join --host http://HOST:8080 --alias client-a --channel team-a
```

Join another client with the same host and channel:

```bash
orbit join --host http://HOST:8080 --alias client-b --channel team-a
```

The first already-joined interactive client should receive a prompt and can approve the new client. If no interactive prompt is available, approve manually from any existing member:

```bash
orbit join-requests
orbit approve <REQUEST_ID>
orbit reject <REQUEST_ID>
```

List online peers:

```bash
orbit peers
```

Run one command on a peer and wait for output:

```bash
orbit exec client-b -- python3 -V
orbit exec client-b --shell "cd /tmp && pwd && ls -la"
```

Open an interactive shell on a peer:

```bash
orbit sh client-b
```

Transfer files. The default limit is 1 MiB unless `--max-bytes` is supplied:

```bash
orbit put client-b ./local.txt inbox/local.txt
orbit get client-b inbox/local.txt ./downloaded.txt
orbit put --max-bytes 10485760 client-b ./large.bin inbox/large.bin
```

## Command Surface

The supported CLI commands are:

```text
orbit host
orbit join
orbit join-requests
orbit approve <REQUEST_ID>
orbit reject <REQUEST_ID>
orbit peers
orbit exec <peer> -- <command>
orbit sh <peer>
orbit put <peer> <local> <remote>
orbit get <peer> <remote> <local>
```

Do not expand the public CLI unless the change directly supports one of the three peer operation modes: single command execution, interactive shell, or file transfer.

## Security Model

Channel membership is the trust boundary.

- The first client in a channel is accepted automatically.
- Later clients must be approved by an existing channel member.
- Approved members receive a member token for that channel.
- Any approved member can execute commands on any other approved, connected member in the same channel.

This project is not a sandbox. Avoid implying untrusted code isolation. Changes that affect approval, token validation, workspace paths, shell execution, or file transfer limits need tests.

## Development Workflow

Install and run commands with the project environment already configured for `src` layout:

```bash
uv run orbit --help
uv run python -m pytest -q
```

When changing behavior:

1. Prefer existing modules and models over adding new surfaces.
2. Keep config written by `orbit join` sufficient for later commands.
3. Keep `orbit join` foreground by default; use `--no-start` only as an explicit opt-out.
4. Keep file transfers bounded by `--max-bytes` and default to 1 MiB.
5. Preserve structured log format from `mvp_orbit.core.logging`.
6. Update both `README.md` and `README.zh-CN.md` when user-facing behavior changes.

## Validation Checklist

Use targeted tests for narrow changes and the full suite for shared behavior:

```bash
uv run python -m pytest -q
```

For CLI or workflow changes, also exercise the relevant parser path with `uv run orbit --help` or the specific subcommand help. For docs-only changes, scan for stale command names and ensure examples match `src/mvp_orbit/cli/main.py`.
