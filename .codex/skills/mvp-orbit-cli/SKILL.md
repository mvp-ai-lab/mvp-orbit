---
name: mvp-orbit-cli
description: Operate the mvp-orbit CLI for the current pure HTTP Hub/Agent workflow. Use when Codex needs to initialize Hub or Agent config, upload a file package, execute a command on an agent, inspect command status or output, or open and manage a remote shell session.
---

# mvp-orbit CLI

Use `mvp-orbit` as a small remote execution loop over HTTP:

1. initialize the Hub and Agent
2. upload files as a package when needed
3. execute a command on a target `agent_id`
4. inspect command status or output when running detached
5. open a persistent remote shell when interactive iteration is needed

## Workflow

### 1) Verify configuration first

- Assume `orbit` reads the default config file at `~/.config/mvp-orbit/config.toml` unless `--config` or `ORBIT_CONFIG` is set.
- If the machine has not been initialized yet, use:
  - `orbit init hub`
  - `orbit init node --agent-id <agent_id>`
  - `orbit init agent --agent-id <agent_id>`
- `orbit init hub` prints the Hub URL, API token, and `ORBIT_NODE_SHARED_CONFIG`.
- `orbit init node` can consume that shared string with `--shared-config`.

### 2) Use only the current execution primitives

- Upload a file package with `orbit package upload --source-dir <dir>` when the remote command depends on local source files.
- Execute batch work with `orbit command exec --agent-id <id> [--package-id <pkg>] ...`.
- Use `--detach` when you want a background command and a returned `command_id`.
- Inspect a detached command with:
  - `orbit command status --command-id <command_id>`
  - `orbit command output --command-id <command_id>`
  - `orbit command output --command-id <command_id> --follow`
  - `orbit command cancel --command-id <command_id>`
- Use `orbit shell start --agent-id <id> [--package-id <pkg>]` for interactive work.
- Use `orbit shell list [--agent-id <id>] [--status <status>]` when you need to find active or historical shell sessions.
- Reconnect or stop a shell with:
  - `orbit shell attach --session-id <session_id>`
  - `orbit shell close --session-id <session_id>`

### 3) Respect current workspace semantics

- The Agent startup directory is the base workspace.
- Commands without `package_id` run directly in the base workspace.
- Commands with `package_id` run in a package-specific subdirectory under the base workspace.
- Shell sessions start in the base workspace by default, or in the package workspace when `package_id` is provided.
- Keep `working_dir` relative to the command or shell workspace root.

### 4) Prefer direct command execution over legacy object workflows

- Do not create command JSON files unless the user explicitly needs one for another purpose.
- Do not use any legacy object workflow built around uploaded command objects, task objects, run polling, or external object storage.
- Do not assume any external storage service is part of the system.

## Working rules

- Prefer `orbit package upload --source-dir <dir>` from the project root the user wants to send.
- Remember that package upload is Git-aware and respects `.gitignore` when the source directory is inside a Git repository.
- Prefer `orbit command exec` directly for one-off remote execution.
- Prefer `orbit command exec --detach` followed by `status` or `output` when the command needs to run in the background.
- Prefer `orbit shell start` only when the task is truly interactive or stateful across multiple commands.
- When reporting results back to the user, include `command_id` or `session_id`, the current status, the exit code when available, and the important output lines.

## References

- For exact command templates and output shapes, read [references/cli-flow.md](references/cli-flow.md).
