---
name: mvp-orbit-cli
description: Operate the current mvp-orbit CLI for the HTTP-only Hub/Agent workflow. Use when Codex needs to initialize Hub or Agent config, upload a file package, execute a command on an agent, inspect command status or output, or open and manage a remote shell session.
---

# mvp-orbit CLI

Use `mvp-orbit` as a small HTTP-only remote execution loop:

1. initialize the Hub and Agent
2. use `orbit connect` to mint a 7-day user token
3. upload files as a package when needed
4. execute a command on a target `agent_id`
5. inspect command status or output when running detached
6. open a persistent remote shell when interactive iteration is needed

## Workflow

### 1) Verify configuration first

- Assume `orbit` reads the default config file at `~/.config/mvp-orbit/config.toml` unless `--config` or `ORBIT_CONFIG` is set.
- If the machine has not been initialized yet, use:
  - `orbit init hub`
  - `orbit connect`
  - `orbit init agent --config-string <value> --agent-id <agent_id>`
  - `orbit init node --config-string <value> --agent-id <agent_id>`
- `orbit connect` prints `ORBIT_AGENT_CONFIG_STRING`.
- `agent_id` is not encoded into the config-string. It is chosen locally on the Agent machine.

### 2) Use only the current execution primitives

- Upload a file package with `orbit package upload --source-dir <dir>` when the remote command depends on local source files.
- Execute batch work with `orbit cmd exec --agent-id <id> [--package-id <pkg>] ...`.
- `orbit command ...` remains valid, but prefer `orbit cmd ...` in new instructions.
- Use `orbit cmd exec --shell "..."` when the remote command needs shell features such as `cd`, `&&`, pipes, redirects, or inline env assignments.
- Use `--detach` when you want a background command and a returned `command_id`.
- Inspect a detached command with:
  - `orbit cmd status --command-id <command_id>`
  - `orbit cmd output --command-id <command_id>`
  - `orbit cmd output --command-id <command_id> --follow`
  - `orbit cmd cancel --command-id <command_id>`
- Use `orbit shell start --agent-id <id> [--package-id <pkg>]` for interactive work.
- Use `orbit shell list [--agent-id <id>] [--status <status>]` when you need to find active or historical shell sessions.
- Reconnect or stop a shell with:
  - `orbit shell attach --session-id <session_id>`
  - `orbit shell close --session-id <session_id>`

### 3) Respect current workspace semantics

- The Agent startup directory is the base workspace unless `workspace_root` is configured.
- Commands without `package_id` run directly in the base workspace.
- Commands with `package_id` run in a package-specific subdirectory under the base workspace.
- Shell sessions start in the base workspace by default, or in the package workspace when `package_id` is provided.
- Keep `working_dir` relative to the command or shell workspace root.

### 4) Avoid string parsing failures in `cmd exec`

- Prefer argv-style execution. Pass the remote command as distinct trailing arguments after `--`.
- For simple commands, use forms like:
  - `orbit cmd exec --agent-id <id> -- python3 -V`
  - `orbit cmd exec --agent-id <id> -- ls -lah /tmp`
- Use `--shell` only when the remote command actually needs shell semantics such as `cd`, `&&`, pipes, redirects, globbing, or inline env assignments.
- Do not rely on the CLI's single-string auto-detection for shell commands. It exists, but it is more error-prone than explicit argv or explicit `--shell`.
- Prefer `--env-file` over inline `FOO=bar cmd ...` when environment variables can be expressed structurally.
- For long or fragile multi-step logic, prefer uploading or generating a script and executing the script, instead of building a long one-line shell string.
- Always keep the `--` separator between Orbit flags and the remote argv when not using `--shell`.

### 5) Respect current transport semantics

- The product is HTTP-only.
- Realtime delivery is `SSE` down and `POST` up.
- Do not describe the Agent as polling the Hub.
- Shell sessions are PTY-backed; this is why prompts and REPLs behave like a real terminal.

### 6) Prefer direct command execution over legacy object workflows

- Do not create command JSON files unless the user explicitly needs one for another purpose.
- Do not use any legacy object workflow built around uploaded command objects, task objects, run polling, shared bundle strings, or external object storage.
- Do not assume any external storage service is part of the system.

## Working rules

- Prefer `orbit package upload --source-dir <dir>` from the project root the user wants to send.
- Remember that package upload is Git-aware and respects `.gitignore` when the source directory is inside a Git repository.
- Prefer `orbit cmd exec` directly for one-off remote execution.
- Prefer `orbit cmd exec --detach` followed by `status` or `output` when the command needs to run in the background.
- Prefer argv-style `orbit cmd exec -- ...` over string-style commands whenever possible.
- Prefer explicit `--shell` over implicit string parsing whenever shell features are required.
- Prefer `orbit shell start` only when the task is truly interactive or stateful across multiple commands.
- Remember that `orbit cmd exec` and `orbit cmd output --follow` now return a local exit code derived from the remote terminal state and print a summary line on `stderr`.
- When reporting results back to the user, include `command_id` or `session_id`, the current status, the exit code when available, and the important output lines.

## References

- For exact command templates and output shapes, read [references/cli-flow.md](references/cli-flow.md).
