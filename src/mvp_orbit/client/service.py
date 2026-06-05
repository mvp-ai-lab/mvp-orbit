from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from mvp_orbit.client.runtime import ClientRuntime
from mvp_orbit.core.logging import log_kv
from mvp_orbit.core.models import ClientEvent, ClientEventsRequest, CommandLease, FileTransferResult, ShellSessionLease

logger = logging.getLogger(__name__)


class TokenExpiredError(RuntimeError):
    pass


@dataclass
class _ShellControl:
    inputs: queue.Queue[bytes] = field(default_factory=queue.Queue)
    resizes: queue.Queue[tuple[int, int]] = field(default_factory=queue.Queue)
    close_requested: threading.Event = field(default_factory=threading.Event)

    def pop_inputs(self) -> list[bytes]:
        items: list[bytes] = []
        while True:
            try:
                items.append(self.inputs.get_nowait())
            except queue.Empty:
                return items

    def pop_resizes(self) -> list[tuple[int, int]]:
        items: list[tuple[int, int]] = []
        while True:
            try:
                items.append(self.resizes.get_nowait())
            except queue.Empty:
                return items


@dataclass
class ClientService:
    client_id: str
    hub_url: str
    runtime: ClientRuntime
    member_token: str | None = None
    heartbeat_interval_sec: float = 15.0
    join_request_prompt: Callable[[dict], bool | None] | None = None

    def __post_init__(self) -> None:
        self._command_cancels: dict[str, threading.Event] = {}
        self._shell_controls: dict[str, _ShellControl] = {}
        self._last_event_id = 0

    def _headers(self, *, accept: str = "application/json") -> dict[str, str]:
        headers = {"Accept": accept}
        if self.member_token:
            headers["Authorization"] = f"Bearer {self.member_token}"
        return headers

    def run_forever(self, client: httpx.Client | None = None) -> None:
        timeout = httpx.Timeout(connect=5.0, read=None, write=10.0, pool=10.0)
        own_client = client is None
        stop_heartbeat = threading.Event()
        heartbeat_thread = threading.Thread(target=self._heartbeat_loop, args=(stop_heartbeat,), daemon=True)
        heartbeat_thread.start()
        if client is None:
            client = httpx.Client(timeout=timeout)
        try:
            while True:
                try:
                    self._consume_stream(client)
                except TokenExpiredError as exc:
                    log_kv(logger, logging.ERROR, "client.token_expired", client_id=self.client_id, action="run orbit join again")
                    raise RuntimeError("token expired") from exc
                except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                    log_kv(logger, logging.WARNING, "client.stream_error", client_id=self.client_id, error=exc.__class__.__name__, detail=exc)
                    time.sleep(1.0)
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=2.0)
            if own_client:
                client.close()

    def _heartbeat_loop(self, stop: threading.Event) -> None:
        interval = max(1.0, float(self.heartbeat_interval_sec))
        timeout = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
        with httpx.Client(timeout=timeout) as client:
            while not stop.is_set():
                try:
                    self._post_client_events(client, [ClientEvent(kind="client.heartbeat", payload={})])
                except Exception as exc:
                    log_kv(logger, logging.DEBUG, "client.heartbeat_failed", client_id=self.client_id, error=exc.__class__.__name__, detail=exc)
                stop.wait(interval)

    def _consume_stream(self, client: httpx.Client) -> None:
        headers = self._headers(accept="text/event-stream")
        if self._last_event_id:
            headers["Last-Event-ID"] = str(self._last_event_id)
        with client.stream(
            "GET",
            f"{self.hub_url}/api/clients/{self.client_id}/stream",
            headers=headers,
        ) as response:
            self._raise_for_status(response)
            block: list[str] = []
            for line in response.iter_lines():
                if line == "":
                    event = self._parse_sse_block(block)
                    block = []
                    if event is None:
                        continue
                    self._last_event_id = max(self._last_event_id, event["event_id"])
                    self._dispatch_event(client, event["kind"], event["payload"])
                    continue
                block.append(line)

    def _dispatch_event(self, client: httpx.Client, kind: str, payload: dict) -> None:
        if kind == "keepalive":
            return
        if kind == "command.start":
            command_id = str(payload["command_id"])
            if command_id in self._command_cancels:
                return
            cancel_event = threading.Event()
            self._command_cancels[command_id] = cancel_event
            thread = threading.Thread(
                target=self._run_command,
                args=(client, command_id, cancel_event),
                daemon=True,
            )
            thread.start()
            return
        if kind == "command.cancel":
            command_id = str(payload["command_id"])
            event = self._command_cancels.get(command_id)
            if event is not None:
                event.set()
            return
        if kind == "shell.start":
            session_id = str(payload["session_id"])
            if session_id in self._shell_controls:
                return
            control = _ShellControl()
            self._shell_controls[session_id] = control
            thread = threading.Thread(
                target=self._run_shell,
                args=(client, session_id, control),
                daemon=True,
            )
            thread.start()
            return
        if kind == "shell.stdin":
            session_id = str(payload["session_id"])
            control = self._shell_controls.get(session_id)
            if control is not None:
                control.inputs.put(str(payload.get("data", "")).encode("utf-8"))
            return
        if kind == "shell.resize":
            session_id = str(payload["session_id"])
            control = self._shell_controls.get(session_id)
            if control is not None:
                control.resizes.put((int(payload["rows"]), int(payload["cols"])))
            return
        if kind == "shell.close":
            session_id = str(payload["session_id"])
            control = self._shell_controls.get(session_id)
            if control is not None:
                control.close_requested.set()
            return
        if kind == "file.push":
            thread = threading.Thread(target=self._handle_file_push, args=(client, payload), daemon=True)
            thread.start()
            return
        if kind == "file.pull":
            thread = threading.Thread(target=self._handle_file_pull, args=(client, payload), daemon=True)
            thread.start()
            return
        if kind == "join.request":
            self._handle_join_request(client, payload)
            return
        log_kv(logger, logging.WARNING, "control.unknown", client_id=self.client_id, kind=kind)

    def _handle_join_request(self, client: httpx.Client, payload: dict) -> None:
        request_id = str(payload.get("request_id") or "")
        alias = str(payload.get("alias") or "")
        if not request_id:
            log_kv(logger, logging.WARNING, "join_request.malformed", client_id=self.client_id, payload=payload)
            return

        decision = self._prompt_join_request(payload)
        if decision is None:
            log_kv(logger, logging.INFO, "join_request.pending", client_id=self.client_id, request_id=request_id, alias=alias or "-", action=f"orbit approve {request_id}")
            return

        action = "approve" if decision else "reject"
        response = client.post(
            f"{self.hub_url}/api/join-requests/{request_id}/{action}",
            headers=self._headers(),
        )
        self._raise_for_status(response)
        status_text = "approved" if decision else "rejected"
        print(f"[orbit] join request {request_id} {status_text}", file=sys.stderr, flush=True)

    def _prompt_join_request(self, payload: dict) -> bool | None:
        if self.join_request_prompt is not None:
            return self.join_request_prompt(payload)
        if not (sys.stdin.isatty() and sys.stderr.isatty()):
            return None

        request_id = str(payload.get("request_id") or "")
        alias = str(payload.get("alias") or "unknown")
        channel_id = str(payload.get("channel_id") or "unknown")
        print("", file=sys.stderr)
        print("[orbit] new client join request", file=sys.stderr)
        print(f"  alias: {alias}", file=sys.stderr)
        print(f"  channel: {channel_id}", file=sys.stderr)
        print(f"  request: {request_id}", file=sys.stderr)
        while True:
            answer = input("[orbit] approve this client? [y/N]: ").strip().lower()
            if answer in {"y", "yes"}:
                return True
            if answer in {"", "n", "no"}:
                return False
            print("enter y or n", file=sys.stderr)

    def _run_command(self, client: httpx.Client, command_id: str, cancel_event: threading.Event) -> None:
        try:
            lease = self._claim_command(client, command_id)
        except ValueError:
            self._command_cancels.pop(command_id, None)
            return
        outcome = self.runtime.handle_command(
            lease,
            on_started=lambda: self._post_client_events(
                client,
                [ClientEvent(kind="command.started", payload={"command_id": command_id})],
            ),
            append_output=lambda stream, data: self._post_client_events(
                client,
                [ClientEvent(kind=f"command.{stream}", payload={"command_id": command_id, "data": data})],
            ),
            should_cancel=cancel_event.is_set,
        )
        self._post_client_events(
            client,
            [
                ClientEvent(
                    kind="command.exit",
                    payload={
                        "command_id": command_id,
                        "status": outcome.status.value,
                        "exit_code": outcome.exit_code,
                        "failure_code": outcome.failure_code,
                    },
                )
            ],
        )
        self._command_cancels.pop(command_id, None)

    def _run_shell(self, client: httpx.Client, session_id: str, control: _ShellControl) -> None:
        try:
            lease = self._claim_shell(client, session_id)
        except ValueError:
            self._shell_controls.pop(session_id, None)
            return
        outcome = self.runtime.handle_shell_session(
            lease,
            on_started=lambda: self._post_client_events(
                client,
                [ClientEvent(kind="shell.started", payload={"session_id": session_id})],
            ),
            append_output=lambda data: self._post_client_events(
                client,
                [ClientEvent(kind="shell.stdout", payload={"session_id": session_id, "data": data})],
            ),
            pop_input=control.pop_inputs,
            pop_resize=control.pop_resizes,
            should_close=control.close_requested.is_set,
        )
        final_kind = "shell.closed" if outcome.status.value == "closed" else "shell.exit"
        self._post_client_events(
            client,
            [
                ClientEvent(
                    kind=final_kind,
                    payload={
                        "session_id": session_id,
                        "status": outcome.status.value,
                        "exit_code": outcome.exit_code,
                        "failure_code": outcome.failure_code,
                    },
                )
            ],
        )
        self._shell_controls.pop(session_id, None)

    def _handle_file_push(self, client: httpx.Client, payload: dict) -> None:
        transfer_id = str(payload["transfer_id"])
        self._post_client_events(client, [ClientEvent(kind="file.started", payload={"transfer_id": transfer_id})])
        result = self.runtime.handle_file_push(
            transfer_id=transfer_id,
            remote_path=str(payload["remote_path"]),
            data_b64=str(payload["data_b64"]),
            max_bytes=int(payload.get("max_bytes", 1024 * 1024)),
        )
        self._post_file_result(client, result)

    def _handle_file_pull(self, client: httpx.Client, payload: dict) -> None:
        transfer_id = str(payload["transfer_id"])
        self._post_client_events(client, [ClientEvent(kind="file.started", payload={"transfer_id": transfer_id})])
        result = self.runtime.handle_file_pull(
            transfer_id=transfer_id,
            remote_path=str(payload["remote_path"]),
            max_bytes=int(payload.get("max_bytes", 1024 * 1024)),
        )
        self._post_file_result(client, result)

    def _post_file_result(self, client: httpx.Client, result: FileTransferResult) -> None:
        self._post_client_events(
            client,
            [ClientEvent(kind="file.result", payload=result.model_dump(mode="json"))],
        )

    def _claim_command(self, client: httpx.Client, command_id: str) -> CommandLease:
        response = client.post(f"{self.hub_url}/api/commands/{command_id}/claim", headers=self._headers())
        if response.status_code == 409:
            raise ValueError(command_id)
        self._raise_for_status(response)
        return CommandLease.model_validate(response.json())

    def _claim_shell(self, client: httpx.Client, session_id: str) -> ShellSessionLease:
        response = client.post(f"{self.hub_url}/api/shells/{session_id}/claim", headers=self._headers())
        if response.status_code == 409:
            raise ValueError(session_id)
        self._raise_for_status(response)
        return ShellSessionLease.model_validate(response.json())

    def _post_client_events(self, client: httpx.Client, events: list[ClientEvent]) -> None:
        if not events:
            return
        response = client.post(
            f"{self.hub_url}/api/clients/{self.client_id}/events",
            headers=self._headers(),
            json=ClientEventsRequest(events=events).model_dump(mode="json"),
        )
        self._raise_for_status(response)

    @staticmethod
    def _parse_sse_block(block: list[str]) -> dict | None:
        if not block:
            return None
        event_id: int | None = None
        kind = "message"
        data_lines: list[str] = []
        for line in block:
            if not line or line.startswith(":"):
                continue
            if line.startswith("id:"):
                event_id = int(line.partition(":")[2].strip())
            elif line.startswith("event:"):
                kind = line.partition(":")[2].strip()
            elif line.startswith("data:"):
                data_lines.append(line.partition(":")[2].lstrip())
        if event_id is None:
            return None
        payload = {}
        if data_lines:
            payload = json.loads("\n".join(data_lines))
        return {"event_id": event_id, "kind": kind, "payload": payload}

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code == 401:
            detail = None
            try:
                detail = response.json().get("detail")
            except Exception:
                detail = None
            if detail == "token expired":
                raise TokenExpiredError(detail)
        response.raise_for_status()
