from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, StreamingResponse

from mvp_orbit.core.logging import configure_logging, log_kv
from mvp_orbit.core.models import (
    ClientEventsRequest,
    ClientRecord,
    CommandCreateRequest,
    CommandLease,
    CommandOutputChunk,
    CommandRecord,
    CommandStatus,
    FilePullRequest,
    FilePushRequest,
    FileTransferRecord,
    FileTransferStatus,
    JoinApprovalRecord,
    JoinRequest,
    JoinRequestStatus,
    JoinResponse,
    ShellInputRequest,
    ShellResizeRequest,
    ShellSessionCreateRequest,
    ShellSessionLease,
    ShellSessionRecord,
    ShellSessionStatus,
    default_command_id,
    default_file_transfer_id,
    default_join_request_id,
    default_shell_session_id,
)
from mvp_orbit.hub.store import AuthenticatedMember, ExpiredTokenError, HubStore, InvalidTokenError, MembershipError

LANDING_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>mvp-orbit host</title>
  <style>
    body { margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f8fa; color: #151922; }
    main { max-width: 760px; margin: 0 auto; padding: 48px 20px; }
    h1 { margin: 0 0 12px; font-size: 32px; letter-spacing: 0; }
    p { margin: 0 0 22px; color: #566070; line-height: 1.65; }
    code { display: block; margin: 10px 0; padding: 12px 14px; border: 1px solid #dde2ea; border-radius: 6px; background: #fff; color: #151922; overflow-x: auto; }
  </style>
</head>
<body>
  <main>
    <h1>mvp-orbit host</h1>
    <p>The control host is running. Clients join a channel, receive approval from an existing member, then exchange command, shell, and file requests through this host.</p>
    <code>orbit join --host http://HOST:8080 --alias client-a --channel team-a</code>
    <code>orbit exec client-b -- python3 -V</code>
  </main>
</body>
</html>
"""


def _bearer_token(authorization: str | None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    return token


def _member_dependency(store: HubStore):
    def _check_auth(authorization: str | None = Header(default=None)) -> AuthenticatedMember:
        token = _bearer_token(authorization)
        try:
            return store.authenticate_member_token(token)
        except ExpiredTokenError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="token expired") from exc
        except InvalidTokenError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid member token") from exc

    return _check_auth


def _require_client_member(store: HubStore, member: AuthenticatedMember, client_id: str) -> None:
    try:
        store.assert_client_member(client_id, member.channel_id)
    except MembershipError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc


def _require_command_member(store: HubStore, member: AuthenticatedMember, command_id: str) -> CommandRecord:
    record = store.get_command(command_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="command not found")
    if record.channel_id != member.channel_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    return record


def _require_shell_member(store: HubStore, member: AuthenticatedMember, session_id: str) -> ShellSessionRecord:
    record = store.get_shell_session(session_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shell session not found")
    if record.channel_id != member.channel_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    return record


def _require_file_member(store: HubStore, member: AuthenticatedMember, transfer_id: str) -> FileTransferRecord:
    record = store.get_file_transfer(transfer_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="file transfer not found")
    if record.channel_id != member.channel_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    return record


def _format_sse(event_id: int, kind: str, payload: dict) -> bytes:
    return f"id: {event_id}\nevent: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def create_app(*, store: HubStore | None = None) -> FastAPI:
    store = store or HubStore(
        os.getenv("ORBIT_HUB_DB", "./.orbit-hub/hub.sqlite3"),
        os.getenv("ORBIT_OBJECT_ROOT", "./.orbit-hub/objects"),
    )
    require_member = _member_dependency(store)

    cleanup_enabled = os.getenv("ORBIT_CHANNEL_CLEANUP_ENABLED", "1").lower() not in {"0", "false", "no"}
    cleanup_interval_sec = float(os.getenv("ORBIT_CHANNEL_CLEANUP_INTERVAL_SEC", "60"))
    client_offline_sec = float(os.getenv("ORBIT_CLIENT_OFFLINE_SEC", "90"))
    channel_empty_ttl_sec = float(os.getenv("ORBIT_CHANNEL_EMPTY_TTL_SEC", "3600"))

    async def _channel_cleanup_loop() -> None:
        interval = max(1.0, cleanup_interval_sec)
        while True:
            await asyncio.sleep(interval)
            await asyncio.to_thread(
                store.cleanup_empty_channels,
                offline_after_sec=client_offline_sec,
                empty_ttl_sec=channel_empty_ttl_sec,
            )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(_channel_cleanup_loop()) if cleanup_enabled else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="mvp-orbit-host", version="0.5.0", lifespan=lifespan)

    async def _client_stream(request: Request, client_id: str):
        last_event_id = _last_event_id(request)
        while True:
            events = store.get_client_control_events(client_id, last_event_id)
            if events:
                for event in events:
                    yield _format_sse(event.event_id, event.kind, event.payload)
                    last_event_id = event.event_id
                continue
            if await request.is_disconnected():
                break
            if not await asyncio.to_thread(store.wait_for_updates, 5.0):
                yield b": keepalive\n\n"

    async def _record_stream(request: Request, list_events, get_record, terminal_statuses: set):
        last_event_id = _last_event_id(request)
        while True:
            events = list_events(last_event_id)
            if events:
                for event in events:
                    yield _format_sse(event.event_id, event.kind, event.payload)
                    last_event_id = event.event_id
                continue
            record = get_record()
            if record is None or record.status in terminal_statuses:
                break
            if await request.is_disconnected():
                break
            if not await asyncio.to_thread(store.wait_for_updates, 5.0):
                yield b": keepalive\n\n"

    @app.get("/", response_class=HTMLResponse)
    def landing_page() -> HTMLResponse:
        return HTMLResponse(LANDING_PAGE_HTML)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/join", response_model=JoinResponse)
    def join(request: JoinRequest) -> JoinResponse:
        return store.request_channel_join(request_id=default_join_request_id(), alias=request.alias, channel=request.channel)

    @app.get("/api/join-requests/{request_id}", response_model=JoinResponse)
    def get_join_request(request_id: str) -> JoinResponse:
        response = store.get_join_request_response(request_id)
        if response is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="join request not found")
        return response

    @app.get("/api/join-requests", response_model=list[JoinApprovalRecord])
    def list_join_requests(
        request_status: JoinRequestStatus | None = Query(default=JoinRequestStatus.PENDING, alias="status"),
        member: AuthenticatedMember = Depends(require_member),
    ) -> list[JoinApprovalRecord]:
        return store.list_join_requests(member.channel_id, status_filter=request_status)

    @app.post("/api/join-requests/{request_id}/approve", response_model=JoinApprovalRecord)
    def approve_join_request(request_id: str, member: AuthenticatedMember = Depends(require_member)) -> JoinApprovalRecord:
        try:
            return store.approve_join_request(request_id, member.channel_id)
        except MembershipError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="join request not found") from exc

    @app.post("/api/join-requests/{request_id}/reject", response_model=JoinApprovalRecord)
    def reject_join_request(request_id: str, member: AuthenticatedMember = Depends(require_member)) -> JoinApprovalRecord:
        try:
            return store.reject_join_request(request_id, member.channel_id)
        except MembershipError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="join request not found") from exc

    @app.get("/api/peers", response_model=list[ClientRecord])
    def list_peers(member: AuthenticatedMember = Depends(require_member)) -> list[ClientRecord]:
        return store.list_clients(member.channel_id)

    @app.get("/api/clients/{client_id}/stream")
    async def client_stream(client_id: str, request: Request, member: AuthenticatedMember = Depends(require_member)) -> StreamingResponse:
        try:
            store.register_client(client_id, member.channel_id)
        except MembershipError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc
        return StreamingResponse(_client_stream(request, client_id), media_type="text/event-stream", headers=_sse_headers())

    @app.post("/api/clients/{client_id}/events")
    def append_client_events(client_id: str, request: ClientEventsRequest, member: AuthenticatedMember = Depends(require_member)) -> dict[str, int]:
        try:
            store.register_client(client_id, member.channel_id)
        except MembershipError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc
        store.apply_client_events(client_id, request.events)
        return {"accepted": len(request.events)}

    @app.post("/api/commands", response_model=CommandRecord)
    def create_command(request: CommandCreateRequest, member: AuthenticatedMember = Depends(require_member)) -> CommandRecord:
        _require_client_member(store, member, request.client_id)
        return store.create_command(default_command_id(), member.channel_id, request)

    @app.get("/api/commands/{command_id}", response_model=CommandRecord)
    def get_command(command_id: str, member: AuthenticatedMember = Depends(require_member)) -> CommandRecord:
        return _require_command_member(store, member, command_id)

    @app.post("/api/commands/{command_id}/claim", response_model=CommandLease)
    def claim_command(command_id: str, member: AuthenticatedMember = Depends(require_member)) -> CommandLease:
        record = _require_command_member(store, member, command_id)
        try:
            return store.claim_command(record.command_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="command not found") from exc

    @app.get("/api/commands/{command_id}/output", response_model=CommandOutputChunk)
    def get_command_output(
        command_id: str,
        stdout_offset: int = Query(default=0, ge=0),
        stderr_offset: int = Query(default=0, ge=0),
        member: AuthenticatedMember = Depends(require_member),
    ) -> CommandOutputChunk:
        _require_command_member(store, member, command_id)
        try:
            return store.read_command_output(command_id, stdout_offset=stdout_offset, stderr_offset=stderr_offset)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="command not found") from exc

    @app.get("/api/commands/{command_id}/stream")
    async def stream_command_output(command_id: str, request: Request, member: AuthenticatedMember = Depends(require_member)) -> StreamingResponse:
        _require_command_member(store, member, command_id)
        return StreamingResponse(
            _record_stream(
                request,
                lambda after: store.get_command_events(command_id, after),
                lambda: store.get_command(command_id),
                {CommandStatus.SUCCEEDED, CommandStatus.FAILED, CommandStatus.CANCELED},
            ),
            media_type="text/event-stream",
            headers=_sse_headers(),
        )

    @app.post("/api/commands/{command_id}/cancel", response_model=CommandRecord)
    def cancel_command(command_id: str, member: AuthenticatedMember = Depends(require_member)) -> CommandRecord:
        _require_command_member(store, member, command_id)
        try:
            return store.cancel_command(command_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="command not found") from exc

    @app.post("/api/shells", response_model=ShellSessionRecord)
    def create_shell_session(request: ShellSessionCreateRequest, member: AuthenticatedMember = Depends(require_member)) -> ShellSessionRecord:
        _require_client_member(store, member, request.client_id)
        return store.create_shell_session(default_shell_session_id(), member.channel_id, request, cwd_root=".")

    @app.get("/api/shells", response_model=list[ShellSessionRecord])
    def list_shell_sessions(
        client_id: str | None = Query(default=None),
        session_status: ShellSessionStatus | None = Query(default=None, alias="status"),
        member: AuthenticatedMember = Depends(require_member),
    ) -> list[ShellSessionRecord]:
        return store.list_shell_sessions(member.channel_id, client_id=client_id, session_status=session_status)

    @app.get("/api/shells/{session_id}", response_model=ShellSessionRecord)
    def get_shell_session(session_id: str, member: AuthenticatedMember = Depends(require_member)) -> ShellSessionRecord:
        return _require_shell_member(store, member, session_id)

    @app.post("/api/shells/{session_id}/claim", response_model=ShellSessionLease)
    def claim_shell_session(session_id: str, member: AuthenticatedMember = Depends(require_member)) -> ShellSessionLease:
        record = _require_shell_member(store, member, session_id)
        try:
            return store.claim_shell_session(record.session_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shell session not found") from exc

    @app.post("/api/shells/{session_id}/input")
    def append_shell_input(session_id: str, request: ShellInputRequest, member: AuthenticatedMember = Depends(require_member)) -> dict[str, int]:
        _require_shell_member(store, member, session_id)
        try:
            return {"seq": store.append_shell_input(session_id, request.data)}
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shell session not found") from exc

    @app.post("/api/shells/{session_id}/resize")
    def resize_shell_session(session_id: str, request: ShellResizeRequest, member: AuthenticatedMember = Depends(require_member)) -> dict[str, int]:
        _require_shell_member(store, member, session_id)
        try:
            return {"seq": store.resize_shell_session(session_id, request.rows, request.cols)}
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shell session not found") from exc

    @app.get("/api/shells/{session_id}/stream")
    async def stream_shell_output(session_id: str, request: Request, member: AuthenticatedMember = Depends(require_member)) -> StreamingResponse:
        _require_shell_member(store, member, session_id)
        return StreamingResponse(
            _record_stream(
                request,
                lambda after: store.get_shell_events(session_id, after),
                lambda: store.get_shell_session(session_id),
                {ShellSessionStatus.CLOSED, ShellSessionStatus.FAILED},
            ),
            media_type="text/event-stream",
            headers=_sse_headers(),
        )

    @app.post("/api/shells/{session_id}/close", response_model=ShellSessionRecord)
    def close_shell_session(session_id: str, member: AuthenticatedMember = Depends(require_member)) -> ShellSessionRecord:
        _require_shell_member(store, member, session_id)
        try:
            return store.close_shell_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shell session not found") from exc

    @app.post("/api/files/push", response_model=FileTransferRecord)
    def push_file(request: FilePushRequest, member: AuthenticatedMember = Depends(require_member)) -> FileTransferRecord:
        _require_client_member(store, member, request.client_id)
        try:
            size = len(base64.b64decode(request.data_b64.encode("ascii"), validate=True))
            return store.create_file_push(default_file_transfer_id(), member.channel_id, request, size=size)
        except binascii.Error as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid base64 payload") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)) from exc

    @app.post("/api/files/pull", response_model=FileTransferRecord)
    def pull_file(request: FilePullRequest, member: AuthenticatedMember = Depends(require_member)) -> FileTransferRecord:
        _require_client_member(store, member, request.client_id)
        return store.create_file_pull(default_file_transfer_id(), member.channel_id, request)

    @app.get("/api/files/{transfer_id}", response_model=FileTransferRecord)
    def get_file_transfer(transfer_id: str, member: AuthenticatedMember = Depends(require_member)) -> FileTransferRecord:
        return _require_file_member(store, member, transfer_id)

    @app.get("/api/files/{transfer_id}/stream")
    async def stream_file_transfer(transfer_id: str, request: Request, member: AuthenticatedMember = Depends(require_member)) -> StreamingResponse:
        _require_file_member(store, member, transfer_id)
        return StreamingResponse(
            _record_stream(
                request,
                lambda after: store.get_file_events(transfer_id, after),
                lambda: store.get_file_transfer(transfer_id),
                {FileTransferStatus.SUCCEEDED, FileTransferStatus.FAILED},
            ),
            media_type="text/event-stream",
            headers=_sse_headers(),
        )

    return app


def _last_event_id(request: Request) -> int:
    try:
        return int(request.headers.get("Last-Event-ID", "0") or "0")
    except ValueError:
        return 0


def _sse_headers() -> dict[str, str]:
    return {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}


app = create_app()


def main() -> None:
    configure_logging("host")
    host = os.getenv("ORBIT_HUB_HOST", "127.0.0.1")
    port = int(os.getenv("ORBIT_HUB_PORT", "8080"))
    access_log = os.getenv("ORBIT_ACCESS_LOG", "0").lower() in {"1", "true", "yes"}
    log_kv(logging.getLogger(__name__), logging.INFO, "host.start", bind=f"{host}:{port}", db=os.getenv("ORBIT_HUB_DB", "./.orbit-hub/hub.sqlite3"))
    uvicorn.run(create_app(), host=host, port=port, reload=False, log_config=None, access_log=access_log)


if __name__ == "__main__":
    main()
