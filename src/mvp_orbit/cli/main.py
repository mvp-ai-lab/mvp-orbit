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
import time
import tty
from datetime import datetime
from pathlib import Path

import httpx
import questionary

from mvp_orbit.config import OrbitConfig, load_config, save_config
from mvp_orbit.core.models import (
    CommandCreateRequest,
    CommandStatus,
    FilePullRequest,
    FilePushRequest,
    FileTransferStatus,
    JoinRequest,
    JoinRequestStatus,
    ShellResizeRequest,
    ShellSessionCreateRequest,
    ShellSessionStatus,
    utc_now,
)

_SHELL_META_PATTERN = re.compile(r"(?:&&|\|\||[|;<>()$`\n])")
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


def _headers(member_token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if member_token:
        headers["Authorization"] = f"Bearer {member_token}"
    return headers


def _set_if_missing(args: argparse.Namespace, name: str, value) -> None:
    if getattr(args, name, None) is None and value is not None:
        setattr(args, name, value)


def _apply_config_defaults(args: argparse.Namespace, config: OrbitConfig) -> None:
    _set_if_missing(args, "hub_url", config.hub.resolved_url())
    _set_if_missing(args, "member_token", config.auth.member_token)
    _set_if_missing(args, "token_expires_at", config.auth.expires_at.isoformat() if config.auth.expires_at else None)
    _set_if_missing(args, "client_id", config.client.id)


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
    _set_env_if_missing("ORBIT_MEMBER_TOKEN", config.auth.member_token)
    _set_env_if_missing("ORBIT_TOKEN_EXPIRES_AT", config.auth.expires_at.isoformat() if config.auth.expires_at else None)
    _set_env_if_missing("ORBIT_CLIENT_ID", config.client.id)
    _set_env_if_missing("ORBIT_WORKSPACE_ROOT", config.client.workspace_root)


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


def _require_live_member_token(member_token: str | None, expires_at: str | datetime | None) -> str:
    if not member_token:
        raise RuntimeError("missing member token; run `orbit join` first")
    expiry = _parse_datetime(expires_at) if isinstance(expires_at, str) else expires_at
    if expiry is None:
        raise RuntimeError("missing token expiry; run `orbit join` first")
    if expiry <= utc_now():
        raise RuntimeError("member token expired; run `orbit join` again")
    return member_token


def _run_client_loop(config: OrbitConfig) -> int:
    from mvp_orbit.client.main import main as client_main

    _apply_runtime_env(config)
    client_main()
    return 0


def cmd_join(args: argparse.Namespace) -> int:
    config_path, config = load_config(args.config)
    host = args.host or getattr(args, "hub_url", None) or config.hub.resolved_url()
    alias = args.alias or config.client.id or getpass.getuser()
    channel = args.channel
    if not (args.host and args.alias and channel):
        wizard = SetupWizard(
            "ORBIT JOIN",
            "Join a shared command channel. The first client creates it; later clients need approval from an existing member.",
        )
        wizard.section("Channel", "Use the same channel name on machines that should be able to approve and control each other.")
        host = wizard.prompt("Host URL", host, required=True)
        alias = wizard.prompt("Local alias", alias, required=True)
        channel = wizard.prompt("Channel name", channel, required=True)
    request = JoinRequest(alias=alias, channel=channel)
    with httpx.Client(timeout=20) as client:
        response = client.post(f"{host}/api/join", headers=_headers(None), json=request.model_dump(mode="json"))
        response.raise_for_status()
        payload = response.json()

    if payload["status"] == JoinRequestStatus.PENDING.value:
        request_id = payload["request_id"]
        print(json.dumps({"status": "pending", "request_id": request_id, "alias": alias, "channel_id": payload["channel_id"]}, ensure_ascii=False, indent=2))
        if args.no_wait:
            return 0
        deadline = time.monotonic() + args.wait_sec
        while time.monotonic() < deadline:
            time.sleep(2.0)
            with httpx.Client(timeout=20) as client:
                response = client.get(f"{host}/api/join-requests/{request_id}", headers=_headers(None))
                response.raise_for_status()
                payload = response.json()
            if payload["status"] == JoinRequestStatus.APPROVED.value:
                break
            if payload["status"] == JoinRequestStatus.REJECTED.value:
                print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
                return 1
        else:
            print(f"join request still pending: {request_id}", file=sys.stderr)
            return 124

    config.hub.url = host
    config.client.id = alias
    config.auth.member_token = payload["member_token"]
    config.auth.expires_at = _parse_datetime(payload["expires_at"])
    saved_path = save_config(config, config_path)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "config_path": str(saved_path),
                "alias": alias,
                "host": host,
                "channel_id": payload.get("channel_id"),
                "token_expires_at": payload["expires_at"],
                "started": not args.no_start,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.no_start:
        return 0
    return _run_client_loop(config)


def cmd_join_requests(args: argparse.Namespace) -> int:
    member_token = _require_live_member_token(args.member_token, args.token_expires_at)
    params = {"status": args.status} if args.status else None
    with httpx.Client(timeout=20) as client:
        response = client.get(f"{args.hub_url}/api/join-requests", headers=_headers(member_token), params=params)
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


def cmd_approve_join(args: argparse.Namespace) -> int:
    member_token = _require_live_member_token(args.member_token, args.token_expires_at)
    with httpx.Client(timeout=20) as client:
        response = client.post(f"{args.hub_url}/api/join-requests/{args.request_id}/approve", headers=_headers(member_token))
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


def cmd_reject_join(args: argparse.Namespace) -> int:
    member_token = _require_live_member_token(args.member_token, args.token_expires_at)
    with httpx.Client(timeout=20) as client:
        response = client.post(f"{args.hub_url}/api/join-requests/{args.request_id}/reject", headers=_headers(member_token))
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


def cmd_peers(args: argparse.Namespace) -> int:
    member_token = _require_live_member_token(args.member_token, args.token_expires_at)
    with httpx.Client(timeout=20) as client:
        response = client.get(f"{args.hub_url}/api/peers", headers=_headers(member_token))
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


def cmd_exec_peer(args: argparse.Namespace) -> int:
    if getattr(args, "to", None):
        if getattr(args, "target", None):
            args.command_argv = [args.target] + list(args.command_argv or [])
        args.client_id = args.to
    else:
        args.client_id = args.target
    args.working_dir = args.working_dir or "."
    args.env_file = None
    args.detach = False
    return cmd_command_exec(args)


def cmd_shell_peer(args: argparse.Namespace) -> int:
    args.client_id = args.target
    args.detach = False
    return cmd_shell_start(args)


def cmd_put(args: argparse.Namespace) -> int:
    args.to = args.target
    return cmd_file_push(args)


def cmd_get(args: argparse.Namespace) -> int:
    args.source = args.target
    return cmd_file_pull(args)


def cmd_file_push(args: argparse.Namespace) -> int:
    member_token = _require_live_member_token(args.member_token, args.token_expires_at)
    local_path = Path(args.local_path)
    data = local_path.read_bytes()
    if len(data) > args.max_bytes:
        raise RuntimeError(f"local file exceeds max bytes: {len(data)} > {args.max_bytes}")
    request = FilePushRequest(
        client_id=args.to,
        remote_path=args.remote_path,
        data_b64=base64.b64encode(data).decode("ascii"),
        max_bytes=args.max_bytes,
    )
    with httpx.Client(timeout=60) as client:
        response = client.post(
            f"{args.hub_url}/api/files/push",
            headers=_headers(member_token),
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        transfer_id = response.json()["transfer_id"]
    result = _follow_file_transfer(args.hub_url, member_token, transfer_id)
    if result.get("status") != FileTransferStatus.SUCCEEDED.value:
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_file_pull(args: argparse.Namespace) -> int:
    member_token = _require_live_member_token(args.member_token, args.token_expires_at)
    request = FilePullRequest(client_id=args.source, remote_path=args.remote_path, max_bytes=args.max_bytes)
    with httpx.Client(timeout=20) as client:
        response = client.post(
            f"{args.hub_url}/api/files/pull",
            headers=_headers(member_token),
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        transfer_id = response.json()["transfer_id"]
    result = _follow_file_transfer(args.hub_url, member_token, transfer_id)
    if result.get("status") != FileTransferStatus.SUCCEEDED.value:
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 1
    data_b64 = result.get("data_b64")
    if not data_b64:
        raise RuntimeError("file transfer succeeded without payload")
    data = base64.b64decode(data_b64.encode("ascii"), validate=True)
    if len(data) > args.max_bytes:
        raise RuntimeError(f"remote file exceeds max bytes: {len(data)} > {args.max_bytes}")
    local_path = Path(args.local_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(data)
    print(json.dumps({k: v for k, v in result.items() if k != "data_b64"}, ensure_ascii=False))
    return 0

def _command_create_request(args: argparse.Namespace) -> CommandCreateRequest:
    argv = list(args.command_argv or [])
    if argv and argv[0] == "--":
        argv = argv[1:]
    if getattr(args, "shell", False):
        argv = _shell_wrapped_argv(" ".join(argv))
    elif len(argv) == 1 and _looks_like_shell_command(argv[0]):
        argv = _shell_wrapped_argv(argv[0])
    return CommandCreateRequest(
        client_id=args.client_id,
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
    member_token = _require_live_member_token(args.member_token, args.token_expires_at)
    request = _command_create_request(args)
    with httpx.Client(timeout=60) as client:
        response = client.post(
            f"{args.hub_url}/api/commands",
            headers=_headers(member_token),
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        payload = response.json()
        command_id = payload["command_id"]
        if args.detach:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
    final_payload = _follow_command_output(args.hub_url, member_token, command_id)
    print(_command_summary_line(command_id, final_payload), file=sys.stderr, flush=True)
    return _command_result_exit_code(final_payload)


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


def _follow_file_transfer(hub_url: str, member_token: str, transfer_id: str) -> dict:
    timeout = httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
    with httpx.Client(timeout=timeout) as client:
        with client.stream(
            "GET",
            f"{hub_url}/api/files/{transfer_id}/stream",
            headers=_headers(member_token) | {"Accept": "text/event-stream"},
        ) as response:
            response.raise_for_status()
            for event in _iter_sse_events(response):
                if event["event"] == "file.result":
                    return event["payload"]
    raise RuntimeError(f"file transfer stream ended before terminal event for {transfer_id}")


def _follow_command_output(hub_url: str, member_token: str, command_id: str) -> dict:
    timeout = httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
    with httpx.Client(timeout=timeout) as client:
        with client.stream(
            "GET",
            f"{hub_url}/api/commands/{command_id}/stream",
            headers=_headers(member_token) | {"Accept": "text/event-stream"},
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
    member_token = _require_live_member_token(args.member_token, args.token_expires_at)
    request = ShellSessionCreateRequest(client_id=args.client_id)
    with httpx.Client(timeout=20) as client:
        response = client.post(
            f"{args.hub_url}/api/shells",
            headers=_headers(member_token),
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        payload = response.json()
    if args.detach or not sys.stdin.isatty():
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"[orbit] shell session {payload['session_id']}", file=sys.stderr, flush=True)
    _attach_shell(args.hub_url, member_token, payload["session_id"])
    return 0


def _post_shell_resize(hub_url: str, member_token: str, session_id: str) -> None:
    size = shutil.get_terminal_size((80, 24))
    with httpx.Client(timeout=10.0) as client:
        response = client.post(
            f"{hub_url}/api/shells/{session_id}/resize",
            headers=_headers(member_token),
            json=ShellResizeRequest(rows=size.lines, cols=size.columns).model_dump(mode="json"),
        )
        response.raise_for_status()


def _attach_shell(hub_url: str, member_token: str, session_id: str) -> None:
    stop = threading.Event()
    stream_error: list[BaseException] = []

    def consume_events() -> None:
        timeout = httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
        try:
            with httpx.Client(timeout=timeout) as client:
                with client.stream(
                    "GET",
                    f"{hub_url}/api/shells/{session_id}/stream",
                    headers=_headers(member_token) | {"Accept": "text/event-stream"},
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
                _post_shell_resize(hub_url, member_token, session_id)
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            data = os.read(fd, 1024)
            if not data:
                break
            with httpx.Client(timeout=10.0) as client:
                response = client.post(
                    f"{hub_url}/api/shells/{session_id}/input",
                    headers=_headers(member_token),
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
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{host,join,join-requests,approve,reject,peers,exec,sh,put,get}",
    )

    host = sub.add_parser("host", help="start the control host")
    host.set_defaults(func=cmd_hub_serve)

    join = sub.add_parser("join", help="join and start this client")
    join.add_argument("--host", "--hub-url", dest="host", default=None)
    join.add_argument("--alias", default=None)
    join.add_argument("--channel", default=None)
    join.add_argument("--wait-sec", type=int, default=600)
    join.add_argument("--no-wait", action="store_true")
    join.add_argument("--no-start", action="store_true", help="join and save config without starting the client loop")
    join.set_defaults(func=cmd_join)

    join_requests = sub.add_parser("join-requests", help="list pending join requests")
    join_requests.add_argument("--hub-url", default=None)
    join_requests.add_argument("--member-token", default=os.getenv("ORBIT_MEMBER_TOKEN"))
    join_requests.add_argument("--token-expires-at", default=os.getenv("ORBIT_TOKEN_EXPIRES_AT"))
    join_requests.add_argument("--status", choices=[item.value for item in JoinRequestStatus], default=JoinRequestStatus.PENDING.value)
    join_requests.set_defaults(func=cmd_join_requests)

    approve = sub.add_parser("approve", help="approve a join request")
    approve.add_argument("request_id")
    approve.add_argument("--hub-url", default=None)
    approve.add_argument("--member-token", default=os.getenv("ORBIT_MEMBER_TOKEN"))
    approve.add_argument("--token-expires-at", default=os.getenv("ORBIT_TOKEN_EXPIRES_AT"))
    approve.set_defaults(func=cmd_approve_join)

    reject = sub.add_parser("reject", help="reject a join request")
    reject.add_argument("request_id")
    reject.add_argument("--hub-url", default=None)
    reject.add_argument("--member-token", default=os.getenv("ORBIT_MEMBER_TOKEN"))
    reject.add_argument("--token-expires-at", default=os.getenv("ORBIT_TOKEN_EXPIRES_AT"))
    reject.set_defaults(func=cmd_reject_join)

    peers = sub.add_parser("peers", help="list clients in the current channel")
    peers.add_argument("--hub-url", default=None)
    peers.add_argument("--member-token", default=os.getenv("ORBIT_MEMBER_TOKEN"))
    peers.add_argument("--token-expires-at", default=os.getenv("ORBIT_TOKEN_EXPIRES_AT"))
    peers.set_defaults(func=cmd_peers)

    exec_cmd = sub.add_parser("exec", help="send one command: orbit exec <peer> -- <command>")
    exec_cmd.add_argument("--hub-url", default=None)
    exec_cmd.add_argument("--member-token", default=os.getenv("ORBIT_MEMBER_TOKEN"))
    exec_cmd.add_argument("--token-expires-at", default=os.getenv("ORBIT_TOKEN_EXPIRES_AT"))
    exec_cmd.add_argument("--working-dir", default=".")
    exec_cmd.add_argument("--timeout-sec", type=int, default=3600)
    exec_cmd.add_argument("--shell", action="store_true", help="run the trailing command through /bin/sh -lc on the peer")
    exec_cmd.add_argument("target")
    exec_cmd.add_argument("command_argv", nargs=argparse.REMAINDER)
    exec_cmd.set_defaults(func=cmd_exec_peer)

    sh = sub.add_parser("sh", help="open an interactive shell: orbit sh <peer>")
    sh.add_argument("--hub-url", default=None)
    sh.add_argument("--member-token", default=os.getenv("ORBIT_MEMBER_TOKEN"))
    sh.add_argument("--token-expires-at", default=os.getenv("ORBIT_TOKEN_EXPIRES_AT"))
    sh.add_argument("target")
    sh.set_defaults(func=cmd_shell_peer)

    put = sub.add_parser("put", help="send a file: orbit put <peer> <local> <remote>")
    put.add_argument("--hub-url", default=None)
    put.add_argument("--member-token", default=os.getenv("ORBIT_MEMBER_TOKEN"))
    put.add_argument("--token-expires-at", default=os.getenv("ORBIT_TOKEN_EXPIRES_AT"))
    put.add_argument("--max-bytes", type=int, default=1024 * 1024)
    put.add_argument("target")
    put.add_argument("local_path")
    put.add_argument("remote_path")
    put.set_defaults(func=cmd_put)

    get = sub.add_parser("get", help="fetch a file: orbit get <peer> <remote> <local>")
    get.add_argument("--hub-url", default=None)
    get.add_argument("--member-token", default=os.getenv("ORBIT_MEMBER_TOKEN"))
    get.add_argument("--token-expires-at", default=os.getenv("ORBIT_TOKEN_EXPIRES_AT"))
    get.add_argument("--max-bytes", type=int, default=1024 * 1024)
    get.add_argument("target")
    get.add_argument("remote_path")
    get.add_argument("local_path")
    get.set_defaults(func=cmd_get)

    return parser

def prepare_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> argparse.Namespace:
    config_path, config = load_config(args.config)
    args.config = str(config_path)
    args._orbit_config = config
    _apply_config_defaults(args, config)

    if args.command == "join":
        if getattr(args, "host", None) is None:
            args.host = config.hub.resolved_url()
    if args.command in {"join-requests", "approve", "reject", "peers", "exec", "sh", "put", "get"}:
        _validate_required(parser, args, "hub_url", "member_token", "token_expires_at")
    if args.command == "exec":
        argv = list(args.command_argv or [])
        if argv and argv[0] == "--":
            argv = argv[1:]
        if not argv:
            parser.error("exec requires a trailing command, for example: orbit exec client-b -- python3 -V")
    return args

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = prepare_args(parser, parser.parse_args(argv))
    result = args.func(args)
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
