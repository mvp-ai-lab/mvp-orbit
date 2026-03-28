from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import select
import signal
import shutil
import sys
import termios
import textwrap
import threading
import tty
from datetime import datetime
from pathlib import Path

import httpx
import questionary

from mvp_orbit.cli.package import build_file_package
from mvp_orbit.config import OrbitConfig, ensure_bootstrap_token, load_config, save_config
from mvp_orbit.core.models import (
    CommandCreateRequest,
    CommandStatus,
    ConnectRequest,
    ShellResizeRequest,
    ShellSessionCreateRequest,
    ShellSessionStatus,
    utc_now,
)

_SHELL_META_PATTERN = re.compile(r"(?:&&|\|\||[|;<>()$`\n])")
_AGENT_CONFIG_STRING_PREFIX = "orbit-agent-config-string-v1:"


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


def _headers(user_token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if user_token:
        headers["Authorization"] = f"Bearer {user_token}"
    return headers


def _set_if_missing(args: argparse.Namespace, name: str, value) -> None:
    if getattr(args, name, None) is None and value is not None:
        setattr(args, name, value)


def _apply_config_defaults(args: argparse.Namespace, config: OrbitConfig) -> None:
    _set_if_missing(args, "hub_url", config.hub.resolved_url())
    _set_if_missing(args, "user_token", config.auth.user_token)
    _set_if_missing(args, "token_expires_at", config.auth.expires_at.isoformat() if config.auth.expires_at else None)
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
    _set_env_if_missing("ORBIT_BOOTSTRAP_TOKEN", config.auth.bootstrap_token)
    _set_env_if_missing("ORBIT_USER_TOKEN", config.auth.user_token)
    _set_env_if_missing("ORBIT_TOKEN_EXPIRES_AT", config.auth.expires_at.isoformat() if config.auth.expires_at else None)
    _set_env_if_missing("ORBIT_AGENT_ID", config.agent.id)
    _set_env_if_missing("ORBIT_WORKSPACE_ROOT", config.agent.workspace_root)


def _validate_required(parser: argparse.ArgumentParser, args: argparse.Namespace, *names: str) -> None:
    missing = [name for name in names if not getattr(args, name, None)]
    if missing:
        parser.error(f"missing required configuration/arguments: {', '.join('--' + name.replace('_', '-') for name in missing)}")


def _is_terminal_command_status(value: str) -> bool:
    return value in {CommandStatus.SUCCEEDED.value, CommandStatus.FAILED.value, CommandStatus.CANCELED.value}


def _normalize_process_exit_code(value: int | None, *, default: int) -> int:
    if value is None:
        return default
    if value < 0:
        signal_code = min(abs(value), 127)
        return 128 + signal_code
    return max(0, min(value, 255))


def _command_summary_line(command_id: str, payload: dict) -> str:
    status = str(payload.get("status") or "unknown")
    parts = [f"[orbit] command {command_id} {status}"]
    if payload.get("exit_code") is not None:
        parts.append(f"exit={payload['exit_code']}")
    if payload.get("failure_code"):
        parts.append(f"reason={payload['failure_code']}")
    return " ".join(parts)


def _command_result_exit_code(payload: dict) -> int:
    status = str(payload.get("status") or "")
    exit_code = payload.get("exit_code")
    failure_code = payload.get("failure_code")
    if status == CommandStatus.SUCCEEDED.value:
        return _normalize_process_exit_code(exit_code, default=0)
    if status == CommandStatus.CANCELED.value:
        if failure_code == "canceled":
            return 130
        return _normalize_process_exit_code(exit_code, default=1)
    if status == CommandStatus.FAILED.value:
        if failure_code == "timeout":
            return 124
        return _normalize_process_exit_code(exit_code, default=1)
    return 1


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


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _encode_agent_config_string(*, hub_url: str, user_token: str, expires_at: str, workspace_root: str | None) -> str:
    payload = {
        "hub_url": hub_url,
        "user_token": user_token,
        "expires_at": expires_at,
        "workspace_root": workspace_root,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return f"{_AGENT_CONFIG_STRING_PREFIX}{encoded}"


def _decode_agent_config_string(value: str) -> dict:
    if not value.startswith(_AGENT_CONFIG_STRING_PREFIX):
        raise RuntimeError("invalid agent config-string")
    encoded = value.removeprefix(_AGENT_CONFIG_STRING_PREFIX)
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("invalid agent config-string") from exc
    required = {"hub_url", "user_token", "expires_at"}
    if not required.issubset(payload):
        raise RuntimeError("invalid agent config-string")
    return payload


def _apply_agent_config_string_payload(config: OrbitConfig, payload: dict) -> None:
    config.hub.url = str(payload["hub_url"])
    config.auth.user_token = str(payload["user_token"])
    config.auth.expires_at = _parse_datetime(str(payload["expires_at"]))
    config.agent.workspace_root = payload.get("workspace_root") or None


def _require_live_user_token(user_token: str | None, expires_at: str | datetime | None) -> str:
    if not user_token:
        raise RuntimeError("missing user token; run `orbit connect` first")
    expiry = _parse_datetime(expires_at) if isinstance(expires_at, str) else expires_at
    if expiry is None:
        raise RuntimeError("missing token expiry; run `orbit connect` first")
    if expiry <= utc_now():
        raise RuntimeError("user token expired; run `orbit connect` again")
    return user_token


def cmd_init_hub(args: argparse.Namespace) -> int:
    config_path, config = load_config(args.config)
    wizard = SetupWizard(
        "ORBIT HUB SETUP",
        "Configure the Hub API, local object storage, and the bootstrap token used by `orbit connect`.",
    )
    wizard.section("Hub Service", "These values define where the Hub listens and where it stores packages and command output.")
    config.hub.host = wizard.prompt("Hub bind host", config.hub.host)
    config.hub.port = _prompt_int(wizard, "Hub bind port", config.hub.port)
    config.hub.db = wizard.prompt("Hub sqlite path", config.hub.db)
    config.hub.object_root = wizard.prompt("Hub object root", config.hub.object_root)
    config.hub.url = wizard.prompt("Hub public URL", config.hub.resolved_url())

    ensure_bootstrap_token(config)
    wizard.section("Bootstrap Token", "This token is only used by `orbit connect` to mint 7-day user tokens.")
    config.auth.bootstrap_token = wizard.prompt(
        "Bootstrap token",
        config.auth.bootstrap_token,
        required=True,
        secret=True,
    )

    saved_path = save_config(config, config_path)
    wizard.summary(
        "Hub Ready",
        [
            f"Config saved: {saved_path}",
            f"Hub URL: {config.hub.resolved_url()}",
            "Users now authenticate through `orbit connect`.",
        ],
    )
    print(f"ORBIT_BOOTSTRAP_TOKEN={config.auth.bootstrap_token}")
    return 0


def cmd_init_node(args: argparse.Namespace) -> int:
    config_path, config = load_config(args.config)
    if args.config_string:
        payload = _decode_agent_config_string(args.config_string)
        _apply_agent_config_string_payload(config, payload)
        if args.agent_id:
            config.agent.id = args.agent_id
        elif config.agent.id:
            pass
        elif sys.stdin.isatty():
            wizard = SetupWizard(
                "ORBIT NODE SETUP",
                "Configure a node that runs an agent with a 7-day user token from `orbit connect`.",
            )
            wizard.section("Identity", "The config-string sets Hub access. Enter the local agent ID for this machine.")
            config.agent.id = wizard.prompt("Agent ID", "agent-a", required=True)
        else:
            raise RuntimeError("missing agent id; rerun with --agent-id or run interactively to enter it")
        saved_path = save_config(config, config_path)
        print(
            json.dumps(
                {
                    "config_path": str(saved_path),
                    "agent_id": config.agent.id,
                    "hub_url": config.hub.resolved_url(),
                    "expires_at": config.auth.expires_at.isoformat() if config.auth.expires_at else None,
                    "workspace_root": config.agent.workspace_root,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    wizard = SetupWizard(
        "ORBIT NODE SETUP",
        "Configure a node that runs an agent with a 7-day user token from `orbit connect`.",
    )

    config_string = wizard.prompt(
        "Agent config-string",
        required=False,
        hint="Paste the config-string printed by `orbit connect` to prefill Hub access. Leave empty for manual setup.",
        secret=True,
    )
    if config_string:
        payload = _decode_agent_config_string(config_string)
        _apply_agent_config_string_payload(config, payload)
        wizard.section("Identity", "The config-string does not include agent identity. Set the local agent ID on this machine.")
        config.agent.id = wizard.prompt("Agent ID", args.agent_id or config.agent.id or "agent-a", required=True)
        saved_path = save_config(config, config_path)
        wizard.summary(
            "Node Ready",
            [
                f"Config saved: {saved_path}",
                f"Agent ID: {config.agent.id}",
                f"Hub URL: {config.hub.resolved_url()}",
                f"Token expires at: {config.auth.expires_at.isoformat() if config.auth.expires_at else '-'}",
            ],
        )
        return 0

    wizard.section("Identity", "This node ID is how the Hub targets command execution.")
    config.agent.id = wizard.prompt("Agent ID", args.agent_id or config.agent.id, required=True)

    wizard.section("Hub Access", "These values let the node talk to the Hub over HTTP using a user token.")
    config.hub.url = wizard.prompt("Hub URL", config.hub.resolved_url(), required=True)
    config.auth.user_token = wizard.prompt("User token", config.auth.user_token, required=True, secret=True)
    config.auth.expires_at = _parse_datetime(
        wizard.prompt(
            "Token expires at (ISO8601)",
            config.auth.expires_at.isoformat() if config.auth.expires_at else None,
            required=True,
        )
    )

    wizard.section(
        "Runtime",
        "The workspace root is optional. If left empty, the agent startup directory becomes the base workspace.",
    )
    config.agent.workspace_root = wizard.prompt("Workspace root", config.agent.workspace_root)

    saved_path = save_config(config, config_path)
    wizard.summary(
        "Node Ready",
        [
            f"Config saved: {saved_path}",
            f"Agent ID: {config.agent.id}",
            f"Hub URL: {config.hub.resolved_url()}",
            f"Token expires at: {config.auth.expires_at.isoformat() if config.auth.expires_at else '-'}",
        ],
    )
    return 0


def cmd_init_agent(args: argparse.Namespace) -> int:
    return cmd_init_node(args)


def cmd_connect(args: argparse.Namespace) -> int:
    config_path, config = load_config(args.config)
    hub_url = args.hub_url or config.hub.resolved_url()
    user_id = args.user_id or getpass.getuser()
    wizard = SetupWizard(
        "ORBIT CONNECT",
        "Exchange the Hub bootstrap token for a 7-day user token.",
    )
    wizard.section("Hub", "Connect writes the returned user token into your local Orbit config.")
    hub_url = wizard.prompt("Hub URL", hub_url, required=True)
    user_id = wizard.prompt("User ID", user_id, required=True)
    bootstrap_token = wizard.prompt("Bootstrap token", required=True, secret=True)

    request = ConnectRequest(user_id=user_id)
    with httpx.Client(timeout=20) as client:
        response = client.post(
            f"{hub_url}/api/connect",
            headers={**_headers(None), "Authorization": f"Bearer {bootstrap_token}"},
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        payload = response.json()

    config.hub.url = hub_url
    config.auth.user_token = payload["user_token"]
    config.auth.expires_at = _parse_datetime(payload["expires_at"])
    saved_path = save_config(config, config_path)

    wizard.section(
        "Agent Config-String",
        "Generate a config-string so another machine can run `orbit init agent` without manually entering Hub URL or user token.",
    )
    config_string_workspace_root = wizard.prompt("Workspace root for config-string", config.agent.workspace_root)
    agent_config_string = _encode_agent_config_string(
        hub_url=hub_url,
        user_token=payload["user_token"],
        expires_at=payload["expires_at"],
        workspace_root=config_string_workspace_root or None,
    )

    wizard.summary(
        "Connected",
        [
            f"Config saved: {saved_path}",
            f"User ID: {payload['user_id']}",
            f"Token expires at: {payload['expires_at']}",
            "Use the printed ORBIT_AGENT_CONFIG_STRING on the target machine, then enter Agent ID locally.",
        ],
    )
    print(f"ORBIT_AGENT_CONFIG_STRING={agent_config_string}")
    return 0


def cmd_package_upload(args: argparse.Namespace) -> int:
    user_token = _require_live_user_token(args.user_token, args.token_expires_at)
    build = build_file_package(args.source_dir, tmp_dir=args.tmp_dir)
    try:
        with httpx.Client(timeout=60) as client:
            response = client.post(
                f"{args.hub_url}/api/packages",
                headers={**_headers(user_token), "Content-Type": "application/gzip"},
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
    if getattr(args, "shell", False):
        argv = _shell_wrapped_argv(" ".join(argv))
    elif len(argv) == 1 and _looks_like_shell_command(argv[0]):
        argv = _shell_wrapped_argv(argv[0])
    return CommandCreateRequest(
        agent_id=args.agent_id,
        package_id=args.package_id,
        argv=argv,
        env_patch=_load_json(args.env_file) if args.env_file else {},
        timeout_sec=args.timeout_sec,
        working_dir=args.working_dir,
    )


def _looks_like_shell_command(value: str) -> bool:
    return " " in value or bool(_SHELL_META_PATTERN.search(value))


def _shell_wrapped_argv(command: str) -> list[str]:
    return ["/bin/sh", "-lc", command]


def cmd_command_exec(args: argparse.Namespace) -> int:
    user_token = _require_live_user_token(args.user_token, args.token_expires_at)
    request = _command_create_request(args)
    with httpx.Client(timeout=60) as client:
        response = client.post(
            f"{args.hub_url}/api/commands",
            headers=_headers(user_token),
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        payload = response.json()
        command_id = payload["command_id"]
        if args.detach:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
    final_payload = _follow_command_output(args.hub_url, user_token, command_id)
    print(_command_summary_line(command_id, final_payload), file=sys.stderr, flush=True)
    return _command_result_exit_code(final_payload)


def cmd_command_status(args: argparse.Namespace) -> int:
    user_token = _require_live_user_token(args.user_token, args.token_expires_at)
    with httpx.Client(timeout=20) as client:
        response = client.get(f"{args.hub_url}/api/commands/{args.command_id}", headers=_headers(user_token))
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


def cmd_command_output(args: argparse.Namespace) -> int:
    user_token = _require_live_user_token(args.user_token, args.token_expires_at)
    if args.follow:
        final_payload = _follow_command_output(args.hub_url, user_token, args.command_id)
        print(_command_summary_line(args.command_id, final_payload), file=sys.stderr, flush=True)
        return _command_result_exit_code(final_payload)
    with httpx.Client(timeout=20) as client:
        response = client.get(
            f"{args.hub_url}/api/commands/{args.command_id}/output",
            headers=_headers(user_token),
        )
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


def cmd_command_cancel(args: argparse.Namespace) -> int:
    user_token = _require_live_user_token(args.user_token, args.token_expires_at)
    with httpx.Client(timeout=20) as client:
        response = client.post(f"{args.hub_url}/api/commands/{args.command_id}/cancel", headers=_headers(user_token))
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


def _iter_sse_events(response: httpx.Response) -> list[dict]:
    block: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if not block:
                continue
            event_type = "message"
            data_lines: list[str] = []
            event_id = None
            for item in block:
                if not item or item.startswith(":"):
                    continue
                if item.startswith("event:"):
                    event_type = item.partition(":")[2].strip()
                elif item.startswith("id:"):
                    event_id = item.partition(":")[2].strip()
                elif item.startswith("data:"):
                    data_lines.append(item.partition(":")[2].lstrip())
            block = []
            if not data_lines:
                continue
            raw_data = "\n".join(data_lines)
            try:
                payload = json.loads(raw_data)
            except json.JSONDecodeError:
                payload = {"data": raw_data}
            yield {"id": event_id, "event": event_type, "payload": payload}
            continue
        block.append(line)


def _follow_command_output(hub_url: str, user_token: str, command_id: str) -> dict:
    timeout = httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
    with httpx.Client(timeout=timeout) as client:
        with client.stream(
            "GET",
            f"{hub_url}/api/commands/{command_id}/stream",
            headers=_headers(user_token) | {"Accept": "text/event-stream"},
        ) as response:
            response.raise_for_status()
            for event in _iter_sse_events(response):
                payload = event["payload"]
                if event["event"] == "command.stdout":
                    print(payload.get("data", ""), end="", file=sys.stdout, flush=True)
                elif event["event"] == "command.stderr":
                    print(payload.get("data", ""), end="", file=sys.stderr, flush=True)
                elif event["event"] == "command.exit":
                    return payload
    raise RuntimeError(f"command stream ended before terminal event for {command_id}")


def cmd_shell_start(args: argparse.Namespace) -> int:
    user_token = _require_live_user_token(args.user_token, args.token_expires_at)
    request = ShellSessionCreateRequest(agent_id=args.agent_id, package_id=args.package_id)
    with httpx.Client(timeout=20) as client:
        response = client.post(
            f"{args.hub_url}/api/shells",
            headers=_headers(user_token),
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        payload = response.json()
    if args.detach or not sys.stdin.isatty():
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"[orbit] shell session {payload['session_id']}", file=sys.stderr, flush=True)
    _attach_shell(args.hub_url, user_token, payload["session_id"])
    return 0


def cmd_shell_list(args: argparse.Namespace) -> int:
    user_token = _require_live_user_token(args.user_token, args.token_expires_at)
    params: dict[str, str] = {}
    if args.agent_id:
        params["agent_id"] = args.agent_id
    if args.status:
        params["status"] = args.status
    with httpx.Client(timeout=20) as client:
        response = client.get(
            f"{args.hub_url}/api/shells",
            headers=_headers(user_token),
            params=params or None,
        )
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


def cmd_shell_attach(args: argparse.Namespace) -> int:
    user_token = _require_live_user_token(args.user_token, args.token_expires_at)
    _attach_shell(args.hub_url, user_token, args.session_id)
    return 0


def cmd_shell_close(args: argparse.Namespace) -> int:
    user_token = _require_live_user_token(args.user_token, args.token_expires_at)
    with httpx.Client(timeout=20) as client:
        response = client.post(f"{args.hub_url}/api/shells/{args.session_id}/close", headers=_headers(user_token))
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


def _post_shell_resize(hub_url: str, user_token: str, session_id: str) -> None:
    size = shutil.get_terminal_size((80, 24))
    with httpx.Client(timeout=10.0) as client:
        response = client.post(
            f"{hub_url}/api/shells/{session_id}/resize",
            headers=_headers(user_token),
            json=ShellResizeRequest(rows=size.lines, cols=size.columns).model_dump(mode="json"),
        )
        response.raise_for_status()


def _attach_shell(hub_url: str, user_token: str, session_id: str) -> None:
    stop = threading.Event()
    stream_error: list[BaseException] = []

    def consume_events() -> None:
        timeout = httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
        try:
            with httpx.Client(timeout=timeout) as client:
                with client.stream(
                    "GET",
                    f"{hub_url}/api/shells/{session_id}/stream",
                    headers=_headers(user_token) | {"Accept": "text/event-stream"},
                ) as response:
                    response.raise_for_status()
                    for event in _iter_sse_events(response):
                        payload = event["payload"]
                        if event["event"] == "shell.stdout":
                            print(payload.get("data", ""), end="", file=sys.stdout, flush=True)
                        elif event["event"] == "shell.stderr":
                            print(payload.get("data", ""), end="", file=sys.stderr, flush=True)
                        elif event["event"] in {"shell.closed", "shell.exit"}:
                            stop.set()
                            break
        except BaseException as exc:
            stream_error.append(exc)
            stop.set()

    thread = threading.Thread(target=consume_events, daemon=True)
    thread.start()
    if not sys.stdin.isatty():
        thread.join()
        if stream_error:
            raise stream_error[0]
        return

    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    resize_pending = threading.Event()
    previous_handler = signal.getsignal(signal.SIGWINCH)

    def on_winch(signum, frame) -> None:
        resize_pending.set()

    try:
        signal.signal(signal.SIGWINCH, on_winch)
        tty.setraw(fd)
        resize_pending.set()
        while not stop.is_set():
            if resize_pending.is_set():
                resize_pending.clear()
                _post_shell_resize(hub_url, user_token, session_id)
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            data = os.read(fd, 1024)
            if not data:
                break
            with httpx.Client(timeout=10.0) as client:
                response = client.post(
                    f"{hub_url}/api/shells/{session_id}/input",
                    headers=_headers(user_token),
                    json={"data": data.decode("utf-8", errors="replace")},
                )
                response.raise_for_status()
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGWINCH, previous_handler)
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        stop.set()
        thread.join(timeout=2)
    if stream_error:
        raise stream_error[0]


def cmd_agent_run(args: argparse.Namespace) -> int:
    _require_live_user_token(args._orbit_config.auth.user_token, args._orbit_config.auth.expires_at)
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
    init_node.add_argument("--config-string", default=None, help="agent config-string printed by `orbit connect`")
    init_node.set_defaults(func=cmd_init_node)
    init_agent = init_sub.add_parser("agent", help="alias for `orbit init node`")
    init_agent.add_argument("--agent-id", default=None)
    init_agent.add_argument("--config-string", default=None, help="agent config-string printed by `orbit connect`")
    init_agent.set_defaults(func=cmd_init_agent)

    connect = sub.add_parser("connect", help="exchange a bootstrap token for a 7-day user token")
    connect.add_argument("--hub-url", default=None)
    connect.add_argument("--user-id", default=None)
    connect.set_defaults(func=cmd_connect)

    package = sub.add_parser("package", help="package commands")
    package_sub = package.add_subparsers(dest="package_command", required=True)
    package_upload = package_sub.add_parser("upload", help="build and upload a file package to the Hub")
    package_upload.add_argument("--source-dir", required=True)
    package_upload.add_argument("--tmp-dir", default=None)
    package_upload.add_argument("--hub-url", default=None)
    package_upload.add_argument("--user-token", default=os.getenv("ORBIT_USER_TOKEN"))
    package_upload.add_argument("--token-expires-at", default=os.getenv("ORBIT_TOKEN_EXPIRES_AT"))
    package_upload.set_defaults(func=cmd_package_upload)

    command = sub.add_parser("command", aliases=["cmd"], help="command execution commands")
    command_sub = command.add_subparsers(dest="command_command", required=True)
    command_exec = command_sub.add_parser("exec", help="submit a command and stream output until completion")
    command_exec.add_argument("--hub-url", default=None)
    command_exec.add_argument("--user-token", default=os.getenv("ORBIT_USER_TOKEN"))
    command_exec.add_argument("--token-expires-at", default=os.getenv("ORBIT_TOKEN_EXPIRES_AT"))
    command_exec.add_argument("--agent-id", default=None)
    command_exec.add_argument("--package-id", default=None)
    command_exec.add_argument("--working-dir", default=".")
    command_exec.add_argument("--timeout-sec", type=int, default=3600)
    command_exec.add_argument("--env-file", default=None)
    command_exec.add_argument("--detach", action="store_true", help="submit the command and return immediately without following output")
    command_exec.add_argument("--shell", action="store_true", help="run the trailing command through /bin/sh -lc on the agent")
    command_exec.add_argument("command_argv", nargs=argparse.REMAINDER)
    command_exec.set_defaults(func=cmd_command_exec)

    command_status = command_sub.add_parser("status", help="show command status")
    command_status.add_argument("--hub-url", default=None)
    command_status.add_argument("--user-token", default=os.getenv("ORBIT_USER_TOKEN"))
    command_status.add_argument("--token-expires-at", default=os.getenv("ORBIT_TOKEN_EXPIRES_AT"))
    command_status.add_argument("--command-id", required=True)
    command_status.set_defaults(func=cmd_command_status)

    command_output = command_sub.add_parser("output", help="fetch command output")
    command_output.add_argument("--hub-url", default=None)
    command_output.add_argument("--user-token", default=os.getenv("ORBIT_USER_TOKEN"))
    command_output.add_argument("--token-expires-at", default=os.getenv("ORBIT_TOKEN_EXPIRES_AT"))
    command_output.add_argument("--command-id", required=True)
    command_output.add_argument("--follow", action="store_true")
    command_output.set_defaults(func=cmd_command_output)

    command_cancel = command_sub.add_parser("cancel", help="cancel a queued or running command")
    command_cancel.add_argument("--hub-url", default=None)
    command_cancel.add_argument("--user-token", default=os.getenv("ORBIT_USER_TOKEN"))
    command_cancel.add_argument("--token-expires-at", default=os.getenv("ORBIT_TOKEN_EXPIRES_AT"))
    command_cancel.add_argument("--command-id", required=True)
    command_cancel.set_defaults(func=cmd_command_cancel)

    shell = sub.add_parser("shell", help="interactive shell commands")
    shell_sub = shell.add_subparsers(dest="shell_command", required=True)
    shell_start = shell_sub.add_parser("start", help="start a remote shell session")
    shell_start.add_argument("--hub-url", default=None)
    shell_start.add_argument("--user-token", default=os.getenv("ORBIT_USER_TOKEN"))
    shell_start.add_argument("--token-expires-at", default=os.getenv("ORBIT_TOKEN_EXPIRES_AT"))
    shell_start.add_argument("--agent-id", default=None)
    shell_start.add_argument("--package-id", default=None)
    shell_start.add_argument("--detach", action="store_true")
    shell_start.set_defaults(func=cmd_shell_start)

    shell_list = shell_sub.add_parser("list", help="list remote shell sessions")
    shell_list.add_argument("--hub-url", default=None)
    shell_list.add_argument("--user-token", default=os.getenv("ORBIT_USER_TOKEN"))
    shell_list.add_argument("--token-expires-at", default=os.getenv("ORBIT_TOKEN_EXPIRES_AT"))
    shell_list.add_argument("--agent-id", default=None)
    shell_list.add_argument("--status", choices=[item.value for item in ShellSessionStatus])
    shell_list.set_defaults(func=cmd_shell_list)

    shell_attach = shell_sub.add_parser("attach", help="attach to a remote shell session")
    shell_attach.add_argument("--hub-url", default=None)
    shell_attach.add_argument("--user-token", default=os.getenv("ORBIT_USER_TOKEN"))
    shell_attach.add_argument("--token-expires-at", default=os.getenv("ORBIT_TOKEN_EXPIRES_AT"))
    shell_attach.add_argument("--session-id", required=True)
    shell_attach.set_defaults(func=cmd_shell_attach)

    shell_close = shell_sub.add_parser("close", help="close a remote shell session")
    shell_close.add_argument("--hub-url", default=None)
    shell_close.add_argument("--user-token", default=os.getenv("ORBIT_USER_TOKEN"))
    shell_close.add_argument("--token-expires-at", default=os.getenv("ORBIT_TOKEN_EXPIRES_AT"))
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
    if args.command == "cmd":
        args.command = "command"
    _apply_config_defaults(args, config)

    if args.command == "connect":
        _validate_required(parser, args, "hub_url")
    if args.command == "package" and args.package_command == "upload":
        _validate_required(parser, args, "hub_url", "user_token", "token_expires_at")
    if args.command == "command" and args.command_command == "exec":
        _validate_required(parser, args, "hub_url", "user_token", "token_expires_at", "agent_id")
        argv = list(args.command_argv or [])
        if argv and argv[0] == "--":
            argv = argv[1:]
        if not argv:
            parser.error("command exec requires a trailing command, for example: orbit command exec --agent-id agent-a python3 -V")
    if args.command == "command" and args.command_command in {"status", "output", "cancel"}:
        _validate_required(parser, args, "hub_url", "user_token", "token_expires_at")
    if args.command == "shell" and args.shell_command == "start":
        _validate_required(parser, args, "hub_url", "user_token", "token_expires_at", "agent_id")
    if args.command == "shell" and args.shell_command in {"list", "attach", "close"}:
        _validate_required(parser, args, "hub_url", "user_token", "token_expires_at")
    return args


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = prepare_args(parser, parser.parse_args(argv))
    result = args.func(args)
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
