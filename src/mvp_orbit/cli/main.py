from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import textwrap
import threading
import time
from pathlib import Path

import httpx
import questionary

from mvp_orbit.cli.package import build_file_package
from mvp_orbit.config import (
    OrbitConfig,
    apply_node_shared_config,
    encode_node_shared_config,
    ensure_hub_token,
    load_config,
    save_config,
)
from mvp_orbit.core.models import CommandCreateRequest, CommandStatus, ShellSessionCreateRequest


class SetupWizard:
    def __init__(self, title: str, subtitle: str) -> None:
        self.title = title
        self.subtitle = subtitle
        self.width = min(92, shutil.get_terminal_size((92, 24)).columns)
        self.color = sys.stdout.isatty() and os.getenv("TERM", "dumb") != "dumb" and not os.getenv("NO_COLOR")
        self.interactive = sys.stdin.isatty() and sys.stdout.isatty() and os.getenv("TERM", "dumb") != "dumb"
        self.qstyle = questionary.Style(
            [
                ("qmark", "fg:#23b7d9 bold"),
                ("question", "fg:#e8f1f2 bold"),
                ("answer", "fg:#77e0c6 bold"),
                ("pointer", "fg:#ffcf56 bold"),
                ("highlighted", "fg:#ffcf56 bold"),
                ("selected", "fg:#77e0c6"),
                ("separator", "fg:#6a7d89"),
                ("instruction", "fg:#6a7d89"),
                ("text", "fg:#e8f1f2"),
                ("disabled", "fg:#6a7d89 italic"),
            ]
        )
        self._print_banner()

    def _style(self, text: str, code: str) -> str:
        if not self.color:
            return text
        return f"\033[{code}m{text}\033[0m"

    def _accent(self, text: str) -> str:
        return self._style(text, "38;5;45;1")

    def _muted(self, text: str) -> str:
        return self._style(text, "38;5;246")

    def _success(self, text: str) -> str:
        return self._style(text, "38;5;84;1")

    def _warning(self, text: str) -> str:
        return self._style(text, "38;5;221;1")

    def _line(self, fill: str = "=") -> str:
        return fill * self.width

    def _print_banner(self) -> None:
        print(self._accent(self._line("=")))
        print(self._accent(self.title.center(self.width)))
        print(self._muted(self.subtitle.center(self.width)))
        print(self._accent(self._line("=")))
        print()

    def section(self, title: str, description: str | None = None) -> None:
        print()
        print(self._accent(f"[ {title} ]"))
        if description:
            for line in textwrap.wrap(description, width=max(40, self.width - 2)):
                print(self._muted(line))
        print(self._muted(self._line("-")))
        print()

    def note(self, text: str) -> None:
        for line in textwrap.wrap(text, width=max(40, self.width - 2)):
            print(self._muted(line))

    @staticmethod
    def _questionary_default(default: str | None) -> str:
        return "" if default is None else str(default)

    def prompt(
        self,
        label: str,
        default: str | None = None,
        *,
        required: bool = False,
        hint: str | None = None,
        secret: bool = False,
    ) -> str:
        if hint:
            self.note(hint)
        if not self.interactive:
            suffix = f" [{default}]" if default not in (None, "") else ""
            while True:
                value = input(f"{label}{suffix}: ").strip()
                if value:
                    return value
                if default not in (None, ""):
                    return str(default)
                if not required:
                    return ""
        while True:
            prompt_fn = questionary.password if secret else questionary.text
            question = prompt_fn(
                label,
                default=self._questionary_default(default),
                qmark="◆",
                style=self.qstyle,
                instruction="press Enter to confirm",
            )
            value = question.ask()
            if value is None:
                raise KeyboardInterrupt
            value = value.strip()
            if value:
                return value
            if default not in (None, ""):
                return str(default)
            if not required:
                return ""
            print(self._warning("value required"))

    def boolean(self, label: str, default: bool, *, hint: str | None = None) -> bool:
        if hint:
            self.note(hint)
        if self.interactive:
            result = questionary.confirm(
                label,
                default=default,
                qmark="◆",
                style=self.qstyle,
                instruction="press y/n",
            ).ask()
            if result is None:
                raise KeyboardInterrupt
            return bool(result)
        value = self.prompt(label, "true" if default else "false", required=True)
        return value.lower() in {"1", "true", "yes", "y"}

    def summary(self, title: str, lines: list[str]) -> None:
        print()
        print(self._success(f"[ {title} ]"))
        for line in lines:
            print(f"  {line}")
        print()


def _headers(api_token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    return headers


def _set_if_missing(args: argparse.Namespace, name: str, value) -> None:
    if getattr(args, name, None) is None and value is not None:
        setattr(args, name, value)


def _apply_config_defaults(args: argparse.Namespace, config: OrbitConfig) -> None:
    _set_if_missing(args, "hub_url", config.hub.resolved_url())
    _set_if_missing(args, "api_token", config.auth.api_token)
    _set_if_missing(args, "agent_id", config.agent.id)


def _set_env_if_missing(name: str, value: str | None) -> None:
    if value is None or os.getenv(name) is not None:
        return
    os.environ[name] = value


def _apply_runtime_env(config: OrbitConfig) -> None:
    _set_env_if_missing("ORBIT_HUB_HOST", config.hub.host)
    _set_env_if_missing("ORBIT_HUB_PORT", str(config.hub.port))
    _set_env_if_missing("ORBIT_HUB_DB", config.hub.db)
    _set_env_if_missing("ORBIT_OBJECT_ROOT", config.hub.object_root)
    _set_env_if_missing("ORBIT_HUB_URL", config.hub.resolved_url())
    _set_env_if_missing("ORBIT_API_TOKEN", config.auth.api_token)
    _set_env_if_missing("ORBIT_AGENT_ID", config.agent.id)
    _set_env_if_missing("ORBIT_WORKSPACE_ROOT", config.agent.workspace_root)
    _set_env_if_missing("ORBIT_AGENT_POLL_SEC", str(config.agent.poll_interval_sec))
    _set_env_if_missing("ORBIT_AGENT_HEARTBEAT_SEC", str(config.agent.heartbeat_interval_sec))


def _validate_required(parser: argparse.ArgumentParser, args: argparse.Namespace, *names: str) -> None:
    missing = [name for name in names if not getattr(args, name, None)]
    if missing:
        parser.error(f"missing required configuration/arguments: {', '.join('--' + name.replace('_', '-') for name in missing)}")


def _is_terminal_command_status(value: str) -> bool:
    return value in {CommandStatus.SUCCEEDED.value, CommandStatus.FAILED.value, CommandStatus.CANCELED.value}


def _prompt_int(wizard: SetupWizard, label: str, default: int, *, hint: str | None = None) -> int:
    while True:
        value = wizard.prompt(label, str(default), required=True, hint=hint)
        try:
            return int(value)
        except ValueError:
            print(wizard._warning("enter an integer"))


def _prompt_float(wizard: SetupWizard, label: str, default: float, *, hint: str | None = None) -> float:
    while True:
        value = wizard.prompt(label, str(default), required=True, hint=hint)
        try:
            return float(value)
        except ValueError:
            print(wizard._warning("enter a number"))


def cmd_init_hub(args: argparse.Namespace) -> int:
    config_path, config = load_config(args.config)
    wizard = SetupWizard(
        "ORBIT HUB SETUP",
        "Configure the Hub API, local object storage, and the shared API token for all nodes.",
    )
    wizard.section("Hub Service", "These values define where the Hub listens and where it stores packages and command output.")
    config.hub.host = wizard.prompt("Hub bind host", config.hub.host)
    config.hub.port = _prompt_int(wizard, "Hub bind port", config.hub.port)
    config.hub.db = wizard.prompt("Hub sqlite path", config.hub.db)
    config.hub.object_root = wizard.prompt("Hub object root", config.hub.object_root)
    config.hub.url = wizard.prompt("Hub public URL", config.hub.resolved_url())

    ensure_hub_token(config)
    saved_path = save_config(config, config_path)
    shared_config = encode_node_shared_config(config)

    wizard.summary(
        "Hub Ready",
        [
            f"Config saved: {saved_path}",
            f"Hub URL: {config.hub.resolved_url()}",
            "Distribute ORBIT_NODE_SHARED_CONFIG to every node.",
        ],
    )
    print(f"ORBIT_API_TOKEN={config.auth.api_token}")
    print(f"ORBIT_NODE_SHARED_CONFIG={shared_config}")
    return 0


def cmd_init_node(args: argparse.Namespace) -> int:
    config_path, config = load_config(args.config)
    wizard = SetupWizard(
        "ORBIT NODE SETUP",
        "Configure a node that can upload packages, submit commands, and run the agent loop.",
    )
    shared_config = args.shared_config
    if wizard.interactive and not shared_config:
        wizard.section(
            "Shared Bootstrap",
            "If you already have the ORBIT_NODE_SHARED_CONFIG string from `orbit init hub`, you can import shared values in one step.",
        )
        if wizard.boolean("Do you have an ORBIT_NODE_SHARED_CONFIG string?", False):
            shared_config = wizard.prompt("ORBIT_NODE_SHARED_CONFIG", required=True)
    if shared_config:
        config = apply_node_shared_config(config, shared_config)

    wizard.section("Identity", "This node ID is how the Hub targets command execution.")
    config.agent.id = wizard.prompt("Agent ID", args.agent_id or config.agent.id, required=True)

    if not shared_config:
        wizard.section("Hub Access", "These values let the node talk to the Hub over HTTP.")
        config.hub.url = wizard.prompt("Hub URL", config.hub.resolved_url(), required=True)
        config.auth.api_token = wizard.prompt("Hub API token", config.auth.api_token, required=True, secret=True)
    else:
        wizard.section("Shared Config", "Imported Hub URL and API token from the shared bootstrap string.")

    wizard.section(
        "Runtime",
        "The workspace root is optional. If left empty, the agent startup directory becomes the base workspace.",
    )
    config.agent.workspace_root = wizard.prompt("Workspace root", config.agent.workspace_root)
    config.agent.poll_interval_sec = _prompt_float(wizard, "Poll interval seconds", config.agent.poll_interval_sec)
    config.agent.heartbeat_interval_sec = _prompt_float(wizard, "Heartbeat interval seconds", config.agent.heartbeat_interval_sec)

    saved_path = save_config(config, config_path)
    wizard.summary(
        "Node Ready",
        [
            f"Config saved: {saved_path}",
            f"Agent ID: {config.agent.id}",
            f"Hub URL: {config.hub.resolved_url()}",
        ],
    )
    return 0


def cmd_init_agent(args: argparse.Namespace) -> int:
    return cmd_init_node(args)


def cmd_package_upload(args: argparse.Namespace) -> int:
    build = build_file_package(args.source_dir, tmp_dir=args.tmp_dir)
    try:
        with httpx.Client(timeout=60) as client:
            response = client.post(
                f"{args.hub_url}/api/packages",
                headers={**_headers(args.api_token), "Content-Type": "application/gzip"},
                content=build.archive_path.read_bytes(),
            )
            response.raise_for_status()
            print(json.dumps(response.json(), ensure_ascii=False))
        return 0
    finally:
        shutil.rmtree(build.archive_path.parent, ignore_errors=True)


def _command_create_request(args: argparse.Namespace) -> CommandCreateRequest:
    argv = list(args.command_argv or [])
    if argv and argv[0] == "--":
        argv = argv[1:]
    return CommandCreateRequest(
        agent_id=args.agent_id,
        package_id=args.package_id,
        argv=argv,
        env_patch=_load_json(args.env_file) if args.env_file else {},
        timeout_sec=args.timeout_sec,
        working_dir=args.working_dir,
    )


def cmd_command_exec(args: argparse.Namespace) -> int:
    request = _command_create_request(args)
    with httpx.Client(timeout=60) as client:
        response = client.post(
            f"{args.hub_url}/api/commands",
            headers=_headers(args.api_token),
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        payload = response.json()
        command_id = payload["command_id"]
        if args.detach:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
    _follow_command_output(args.hub_url, args.api_token, command_id, poll_interval_sec=args.poll_interval_sec)
    return 0


def cmd_command_status(args: argparse.Namespace) -> int:
    with httpx.Client(timeout=20) as client:
        response = client.get(f"{args.hub_url}/api/commands/{args.command_id}", headers=_headers(args.api_token))
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


def cmd_command_output(args: argparse.Namespace) -> int:
    if args.follow:
        _follow_command_output(args.hub_url, args.api_token, args.command_id, poll_interval_sec=args.poll_interval_sec)
        return 0
    with httpx.Client(timeout=20) as client:
        response = client.get(
            f"{args.hub_url}/api/commands/{args.command_id}/output",
            headers=_headers(args.api_token),
        )
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


def cmd_command_cancel(args: argparse.Namespace) -> int:
    with httpx.Client(timeout=20) as client:
        response = client.post(f"{args.hub_url}/api/commands/{args.command_id}/cancel", headers=_headers(args.api_token))
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


def _follow_command_output(hub_url: str, api_token: str | None, command_id: str, *, poll_interval_sec: float) -> None:
    stdout_offset = 0
    stderr_offset = 0
    with httpx.Client(timeout=20) as client:
        while True:
            response = client.get(
                f"{hub_url}/api/commands/{command_id}/output",
                headers=_headers(api_token),
                params={"stdout_offset": stdout_offset, "stderr_offset": stderr_offset},
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("stdout"):
                print(payload["stdout"], end="", file=sys.stdout, flush=True)
            if payload.get("stderr"):
                print(payload["stderr"], end="", file=sys.stderr, flush=True)
            stdout_offset = payload["stdout_offset"]
            stderr_offset = payload["stderr_offset"]
            if _is_terminal_command_status(payload["status"]):
                break
            time.sleep(poll_interval_sec)


def cmd_shell_start(args: argparse.Namespace) -> int:
    request = ShellSessionCreateRequest(agent_id=args.agent_id, package_id=args.package_id)
    with httpx.Client(timeout=20) as client:
        response = client.post(
            f"{args.hub_url}/api/shells",
            headers=_headers(args.api_token),
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        payload = response.json()
    if args.detach or not sys.stdin.isatty():
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    _attach_shell(args.hub_url, args.api_token, payload["session_id"], poll_interval_sec=args.poll_interval_sec)
    return 0


def cmd_shell_attach(args: argparse.Namespace) -> int:
    _attach_shell(args.hub_url, args.api_token, args.session_id, poll_interval_sec=args.poll_interval_sec)
    return 0


def cmd_shell_close(args: argparse.Namespace) -> int:
    with httpx.Client(timeout=20) as client:
        response = client.post(f"{args.hub_url}/api/shells/{args.session_id}/close", headers=_headers(args.api_token))
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


def _attach_shell(hub_url: str, api_token: str | None, session_id: str, *, poll_interval_sec: float) -> None:
    stop = threading.Event()

    def poll_events() -> None:
        after_seq = 0
        with httpx.Client(timeout=20) as client:
            while not stop.is_set():
                response = client.get(
                    f"{hub_url}/api/shells/{session_id}/events",
                    headers=_headers(api_token),
                    params={"after_seq": after_seq},
                )
                response.raise_for_status()
                payload = response.json()
                for event in payload.get("events", []):
                    target = sys.stderr if event["stream"] == "stderr" else sys.stdout
                    print(event["data"], end="", file=target, flush=True)
                    after_seq = max(after_seq, int(event["seq"]))
                if payload["status"] in {"closed", "failed"}:
                    stop.set()
                    break
                time.sleep(poll_interval_sec)

    thread = threading.Thread(target=poll_events, daemon=True)
    thread.start()
    try:
        while not stop.is_set():
            try:
                line = input()
            except EOFError:
                break
            if line == "/detach":
                break
            if line == "/close":
                with httpx.Client(timeout=20) as client:
                    client.post(f"{hub_url}/api/shells/{session_id}/close", headers=_headers(api_token))
                break
            with httpx.Client(timeout=20) as client:
                response = client.post(
                    f"{hub_url}/api/shells/{session_id}/input",
                    headers=_headers(api_token),
                    json={"data": line + "\n"},
                )
                response.raise_for_status()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        thread.join(timeout=2)


def cmd_agent_run(args: argparse.Namespace) -> int:
    from mvp_orbit.agent.main import main as agent_main

    _apply_runtime_env(args._orbit_config)
    agent_main()
    return 0


def cmd_hub_serve(args: argparse.Namespace) -> int:
    from mvp_orbit.hub.app import main as hub_main

    _apply_runtime_env(args._orbit_config)
    hub_main()
    return 0


def _load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orbit", description="mvp-orbit CLI")
    parser.add_argument("--config", default=os.getenv("ORBIT_CONFIG"), help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="interactive configuration setup")
    init_sub = init.add_subparsers(dest="init_command", required=True)
    init_hub = init_sub.add_parser("hub", help="interactively create/update Hub config")
    init_hub.set_defaults(func=cmd_init_hub)
    init_node = init_sub.add_parser("node", help="interactively create/update a node config")
    init_node.add_argument("--agent-id", default=None)
    init_node.add_argument("--shared-config", default=os.getenv("ORBIT_NODE_SHARED_CONFIG"))
    init_node.set_defaults(func=cmd_init_node)
    init_agent = init_sub.add_parser("agent", help="alias for `orbit init node`")
    init_agent.add_argument("--agent-id", default=None)
    init_agent.add_argument("--shared-config", default=os.getenv("ORBIT_NODE_SHARED_CONFIG"))
    init_agent.set_defaults(func=cmd_init_agent)

    package = sub.add_parser("package", help="package commands")
    package_sub = package.add_subparsers(dest="package_command", required=True)
    package_upload = package_sub.add_parser("upload", help="build and upload a file package to the Hub")
    package_upload.add_argument("--source-dir", required=True)
    package_upload.add_argument("--tmp-dir", default=None)
    package_upload.add_argument("--hub-url", default=None)
    package_upload.add_argument("--api-token", default=os.getenv("ORBIT_API_TOKEN"))
    package_upload.set_defaults(func=cmd_package_upload)

    command = sub.add_parser("command", help="command execution commands")
    command_sub = command.add_subparsers(dest="command_command", required=True)
    command_exec = command_sub.add_parser("exec", help="submit a command for remote execution")
    command_exec.add_argument("--hub-url", default=None)
    command_exec.add_argument("--api-token", default=os.getenv("ORBIT_API_TOKEN"))
    command_exec.add_argument("--agent-id", default=None)
    command_exec.add_argument("--package-id", default=None)
    command_exec.add_argument("--working-dir", default=".")
    command_exec.add_argument("--timeout-sec", type=int, default=3600)
    command_exec.add_argument("--env-file", default=None)
    command_exec.add_argument("--poll-interval-sec", type=float, default=0.5)
    command_exec.add_argument("--detach", action="store_true")
    command_exec.add_argument("command_argv", nargs=argparse.REMAINDER)
    command_exec.set_defaults(func=cmd_command_exec)

    command_status = command_sub.add_parser("status", help="show command status")
    command_status.add_argument("--hub-url", default=None)
    command_status.add_argument("--api-token", default=os.getenv("ORBIT_API_TOKEN"))
    command_status.add_argument("--command-id", required=True)
    command_status.set_defaults(func=cmd_command_status)

    command_output = command_sub.add_parser("output", help="fetch command output")
    command_output.add_argument("--hub-url", default=None)
    command_output.add_argument("--api-token", default=os.getenv("ORBIT_API_TOKEN"))
    command_output.add_argument("--command-id", required=True)
    command_output.add_argument("--follow", action="store_true")
    command_output.add_argument("--poll-interval-sec", type=float, default=0.5)
    command_output.set_defaults(func=cmd_command_output)

    command_cancel = command_sub.add_parser("cancel", help="cancel a queued or running command")
    command_cancel.add_argument("--hub-url", default=None)
    command_cancel.add_argument("--api-token", default=os.getenv("ORBIT_API_TOKEN"))
    command_cancel.add_argument("--command-id", required=True)
    command_cancel.set_defaults(func=cmd_command_cancel)

    shell = sub.add_parser("shell", help="interactive shell commands")
    shell_sub = shell.add_subparsers(dest="shell_command", required=True)
    shell_start = shell_sub.add_parser("start", help="start a remote shell session")
    shell_start.add_argument("--hub-url", default=None)
    shell_start.add_argument("--api-token", default=os.getenv("ORBIT_API_TOKEN"))
    shell_start.add_argument("--agent-id", default=None)
    shell_start.add_argument("--package-id", default=None)
    shell_start.add_argument("--poll-interval-sec", type=float, default=0.5)
    shell_start.add_argument("--detach", action="store_true")
    shell_start.set_defaults(func=cmd_shell_start)

    shell_attach = shell_sub.add_parser("attach", help="attach to a remote shell session")
    shell_attach.add_argument("--hub-url", default=None)
    shell_attach.add_argument("--api-token", default=os.getenv("ORBIT_API_TOKEN"))
    shell_attach.add_argument("--session-id", required=True)
    shell_attach.add_argument("--poll-interval-sec", type=float, default=0.5)
    shell_attach.set_defaults(func=cmd_shell_attach)

    shell_close = shell_sub.add_parser("close", help="close a remote shell session")
    shell_close.add_argument("--hub-url", default=None)
    shell_close.add_argument("--api-token", default=os.getenv("ORBIT_API_TOKEN"))
    shell_close.add_argument("--session-id", required=True)
    shell_close.set_defaults(func=cmd_shell_close)

    agent = sub.add_parser("agent", help="agent commands")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_run = agent_sub.add_parser("run", help="run the polling agent")
    agent_run.set_defaults(func=cmd_agent_run)

    hub = sub.add_parser("hub", help="hub commands")
    hub_sub = hub.add_subparsers(dest="hub_command", required=True)
    hub_serve = hub_sub.add_parser("serve", help="serve the Hub API")
    hub_serve.set_defaults(func=cmd_hub_serve)

    return parser


def prepare_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> argparse.Namespace:
    config_path, config = load_config(args.config)
    args.config = str(config_path)
    args._orbit_config = config
    _apply_config_defaults(args, config)

    if args.command == "package" and args.package_command == "upload":
        _validate_required(parser, args, "hub_url")
    if args.command == "command" and args.command_command == "exec":
        _validate_required(parser, args, "hub_url", "agent_id")
        argv = list(args.command_argv or [])
        if argv and argv[0] == "--":
            argv = argv[1:]
        if not argv:
            parser.error("command exec requires a trailing command, for example: orbit command exec --agent-id agent-a python3 -V")
    if args.command == "command" and args.command_command in {"status", "output", "cancel"}:
        _validate_required(parser, args, "hub_url")
    if args.command == "shell" and args.shell_command == "start":
        _validate_required(parser, args, "hub_url", "agent_id")
    if args.command == "shell" and args.shell_command in {"attach", "close"}:
        _validate_required(parser, args, "hub_url")
    return args


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = prepare_args(parser, parser.parse_args(argv))
    result = args.func(args)
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
