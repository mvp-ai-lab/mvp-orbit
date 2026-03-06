<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="./assets/logo-light.png">
    <img alt="MVP Engine" src="./assets/logo-dark.png">
  </picture>
</p>

<p align="center">
  <p align="center">
    by MVP Lab.
  </p>
</p>


`mvp-orbit` is a small remote-debug tool for running files and commands on another machine while keeping the workflow simple.

It is designed for workflows like:

- develop on one machine
- send a file package and command to another machine
- execute remotely through a lightweight agent
- fetch logs and exit status back to the local side
- iterate quickly

It is also a good fit for AI coding tools that need:

- automatic remote test execution
- automatic log collection
- automatic retry and debug loops
- a simple way to hand work from a coding agent to a target machine

Typical examples:

- develop on a GPU server, debug on an NPU server
- prepare files on a build machine, run on a test machine
- submit small remote debug tasks without building a full CI platform
- let an AI coding tool submit a package, run a command remotely, and inspect the result automatically

## What it does

`mvp-orbit` currently provides:

- file packaging from a directory
- Git-aware file selection that respects `.gitignore`
- command upload as structured JSON
- task creation from `file_package + command`
- Hub-based run submission to a specific `agent_id`
- Agent pull mode execution
- stdout/stderr collection
- exit code and final status reporting
- GitHub-backed storage for packages, commands, tasks, logs, and results
- interactive initialization for Hub and Agent

In practice, this makes it useful as a lightweight execution loop for AI-assisted development:

1. the coding tool edits files locally
2. uploads a package and command
3. runs the task on the target machine
4. reads logs and exit status
5. decides the next change

## Core objects

The runtime is built around three objects:

- `file_package`: a `.tar.gz` bundle created from a source directory
- `command`: structured execution data such as `argv`, `env_patch`, `timeout_sec`, and `working_dir`
- `task`: a runnable binding of `package_id + command_id`

This keeps the workflow simple:

1. upload files as a package
2. upload the command
3. create a task
4. submit the task to a target agent
5. read logs and result

## How it runs

- Hub stores run metadata and object IDs
- Agent polls the Hub for work
- real task content is stored in GitHub Release Assets
- both the developer side and the agent side use the same `orbit` CLI
- configuration is kept in a default TOML file after initialization

## Current backend

The current version is GitHub-only for object storage.

- GitHub access is performed through `gh` CLI
- the machine is expected to have completed `gh auth login`
- objects are stored in a dedicated relay repository through GitHub Release Assets

Storage is abstracted behind `ObjectStoreBackend`, so later backends like S3 or Hugging Face can be added without changing the run flow.

## Quick start

By default, `orbit` reads configuration from:

```text
~/.config/mvp-orbit/config.toml
```

You can override that path with `--config /path/to/config.toml` or `ORBIT_CONFIG=/path/to/config.toml`, but normal usage should not need it.

### Required setup

- Python 3.11+
- GitHub CLI (`gh`)
- A private GitHub relay repository
- `gh auth login` already completed on both the Hub/developer machine and the Agent machine
- Optional proxy via `HTTPS_PROXY`

#### 1) Initialize Hub config interactively

Run this on the Hub / developer machine:

```bash
orbit init hub
```

The command prompts for:

- GitHub relay repo settings
- Hub bind host / port / sqlite path
- Hub public URL

It also generates and stores:

- `api_token`
- `ticket_secret`
- task signing keypair

Then start the Hub:

```bash
orbit hub serve
```

#### 2) Initialize Agent config interactively

Run this on the Agent machine:

```bash
orbit init agent --agent-id agent-a
```

The command prompts for:

- `agent_id`
- Hub URL
- Hub `api_token`
- shared `ticket_secret`
- task signing public key
- GitHub relay repo settings

Then start the Agent:

```bash
orbit agent run
```

After this step, both Hub and Agent can use the default config file path directly.

### Usage

The intended day-to-day usage is that a coding AI tool such as Codex calls these CLI commands for you, instead of you typing every step manually.

A typical loop looks like this:

1. the coding tool edits files locally
2. it uploads the current working tree as a file package
3. it uploads the command to run remotely
4. it creates a task
5. it submits the task to a target agent
6. it reads logs and results
7. it decides the next code change

#### 1) Upload a file package

`orbit package upload` is git-aware. If the source is inside a Git repo, it uses:

- `git ls-files --cached --others --exclude-standard`

That means `.gitignore` is respected. The selected files are packed into a deterministic `.tar.gz`, so the same content produces the same `package_id`.

```bash
orbit package upload \
  --source-dir /path/to/project
```

Output:

- `package_id`
- `file_count`

#### 2) Upload a command object

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
  --file command.json
```

Output:

- `command_id`

#### 3) Upload a signed task object

```bash
orbit task upload \
  --package-id <PACKAGE_ID> \
  --command-id <COMMAND_ID> \
  --created-by "$USER"
```

`orbit task upload` reads the private signing key from the default config file unless you override it with `--private-key`.
The generated task already contains `package_id` and `command_id`.

Output:

- `task_id`
- `package_id`
- `command_id`

#### 4) Submit a run

```bash
orbit run submit \
  --agent-id agent-a \
  --task-id <TASK_ID>
```

Output:

- `run_id`
- `run_ticket`

#### 5) Query status, logs, result, or cancel

```bash
orbit run status --run-id <RUN_ID>

orbit run logs \
  --run-id <RUN_ID>

orbit run logs \
  --run-id <RUN_ID> \
  --follow

orbit run result \
  --run-id <RUN_ID>

orbit run cancel \
  --run-id <RUN_ID>
```

`orbit run logs --follow` prints streamed stdout/stderr as log chunks arrive.
The Agent uploads log chunks every 10 seconds by default, or earlier when buffered output exceeds 16 KiB.

#### 6) Clean relay repository content

If you want to remove all `mvp-orbit` managed content from the relay repository and start from a clean state:

```bash
orbit relay clean --yes
```

This deletes the managed releases under the current `release_prefix-*` namespace.
