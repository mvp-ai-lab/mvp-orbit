---
name: mvp-orbit-cli
description: Operate the mvp-orbit CLI for remote debug workflows in this repository. Use when Codex needs to initialize Hub or Agent config, upload a file package, upload a command, create a task, submit a run to an agent, or fetch status, logs, and results from mvp-orbit.
---

# mvp-orbit CLI

Use `mvp-orbit` as a small remote execution loop:

1. upload files as a package
2. upload a command JSON
3. create a task from `package_id + command_id`
4. submit the task to a target `agent_id`
5. read run status, logs, and result

## Workflow

### 1) Verify configuration first

- Assume `orbit` reads the default config file at `~/.config/mvp-orbit/config.toml` unless `--config` or `ORBIT_CONFIG` is set.
- If the user has not initialized the machine yet, use:
  - `orbit init hub`
  - `orbit init agent --agent-id <agent_id>`
- If `gh` operations fail, check that `gh auth login` has already been completed on that machine.

### 2) Create runnable objects in order

- Upload the file package first.
- Upload the command JSON second.
- Create the task third.
- Submit the task last.

Always parse the JSON output of each CLI command and feed the returned IDs into the next step. Do not scrape IDs from prose.

### 3) Submit only `task_id`

- `orbit run submit` only needs `--agent-id` and `--task-id`.
- Do not pass `package_id` or `command_id` to the Hub.
- The Agent expands the task object itself and fetches package and command from storage.

### 4) Inspect execution through run APIs

- Use `orbit run status --run-id <run_id>` for current state.
- Use `orbit run logs --run-id <run_id>` to fetch collected stdout/stderr.
- Use `orbit run result --run-id <run_id>` to fetch the final result object.
- Treat `queued` and `running` as non-terminal.
- Treat `succeeded`, `failed`, and `rejected` as terminal.

## Working rules

- Prefer `orbit package upload --source-dir <dir>` from the repository root or task root the user wants to send.
- Remember that package upload is Git-aware and respects `.gitignore` when the source directory is inside a Git repository.
- Build command objects as structured JSON with `argv` rather than shell strings when possible.
- Keep `working_dir` relative to the package root.
- Use a temporary `command.json` file when you need to synthesize a one-off command.
- When reporting results back to the user, include `run_id`, final status, exit code, and the important log lines.

## References

- For exact command templates and output shapes, read [references/cli-flow.md](references/cli-flow.md).
