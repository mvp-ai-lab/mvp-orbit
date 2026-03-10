from __future__ import annotations

import os
import secrets

import uvicorn
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Response, status

from mvp_orbit.core.canonical import object_id_for_bytes
from mvp_orbit.core.models import (
    CommandCompletionRequest,
    CommandCreateRequest,
    CommandOutputAppendRequest,
    CommandOutputChunk,
    CommandRecord,
    PackageRecord,
    ShellCompletionRequest,
    ShellEventAppendRequest,
    ShellEventsResponse,
    ShellInputRequest,
    ShellSessionCreateRequest,
    ShellSessionRecord,
    default_command_id,
    default_shell_session_id,
)
from mvp_orbit.hub.store import HubStore


def _required_env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"missing env var: {name}")
    return value


def _auth_dependency(expected_token: str | None):
    def _check_auth(authorization: str | None = Header(default=None)) -> None:
        if expected_token is None:
            return
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
        token = authorization.removeprefix("Bearer ").strip()
        if token != expected_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token")

    return _check_auth


def create_app(*, store: HubStore | None = None, api_token: str | None = None) -> FastAPI:
    api_token = api_token if api_token is not None else os.getenv("ORBIT_API_TOKEN")
    store = store or HubStore(
        os.getenv("ORBIT_HUB_DB", "./.orbit-hub/hub.sqlite3"),
        os.getenv("ORBIT_OBJECT_ROOT", "./.orbit-hub/objects"),
    )
    require_auth = _auth_dependency(api_token)

    app = FastAPI(title="mvp-orbit-hub", version="0.3.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/packages", response_model=PackageRecord, dependencies=[Depends(require_auth)])
    def upload_package(payload: bytes = Body(..., media_type="application/gzip")) -> PackageRecord:
        package_id = object_id_for_bytes(payload)
        return store.put_package(package_id, payload)

    @app.get("/api/packages/{package_id}", dependencies=[Depends(require_auth)], response_model=None)
    def download_package(package_id: str) -> Response:
        try:
            payload = store.get_package(package_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="package not found") from exc
        return Response(content=payload, media_type="application/gzip")

    @app.post("/api/commands", response_model=CommandRecord, dependencies=[Depends(require_auth)])
    def create_command(request: CommandCreateRequest) -> CommandRecord:
        return store.create_command(default_command_id(), request)

    @app.get("/api/commands/{command_id}", response_model=CommandRecord, dependencies=[Depends(require_auth)])
    def get_command(command_id: str) -> CommandRecord:
        record = store.get_command(command_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="command not found")
        return record

    @app.get("/api/commands/{command_id}/output", response_model=CommandOutputChunk, dependencies=[Depends(require_auth)])
    def get_command_output(
        command_id: str,
        stdout_offset: int = Query(default=0, ge=0),
        stderr_offset: int = Query(default=0, ge=0),
    ) -> CommandOutputChunk:
        try:
            return store.read_command_output(
                command_id,
                stdout_offset=stdout_offset,
                stderr_offset=stderr_offset,
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="command not found") from exc

    @app.post("/api/commands/{command_id}/output", dependencies=[Depends(require_auth)])
    def append_command_output(command_id: str, request: CommandOutputAppendRequest) -> dict[str, str]:
        try:
            store.append_command_output(command_id, request.stream, request.data)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="command not found") from exc
        return {"status": "accepted"}

    @app.post("/api/commands/{command_id}/cancel", response_model=CommandRecord, dependencies=[Depends(require_auth)])
    def cancel_command(command_id: str) -> CommandRecord:
        try:
            return store.cancel_command(command_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="command not found") from exc

    @app.get("/api/agents/{agent_id}/commands/next", dependencies=[Depends(require_auth)], response_model=None)
    def poll_next_command(agent_id: str) -> Response | dict:
        lease = store.lease_next_command(agent_id)
        if lease is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return lease.model_dump(mode="json")

    @app.post("/api/commands/{command_id}/heartbeat", response_model=CommandRecord, dependencies=[Depends(require_auth)])
    def heartbeat_command(command_id: str) -> CommandRecord:
        try:
            return store.heartbeat_command(command_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="command not found") from exc

    @app.post("/api/commands/{command_id}/complete", response_model=CommandRecord, dependencies=[Depends(require_auth)])
    def complete_command(command_id: str, request: CommandCompletionRequest) -> CommandRecord:
        try:
            return store.complete_command(command_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="command not found") from exc

    @app.post("/api/shells", response_model=ShellSessionRecord, dependencies=[Depends(require_auth)])
    def create_shell_session(request: ShellSessionCreateRequest) -> ShellSessionRecord:
        cwd_root = "." if request.package_id is None else f".orbit/packages/{request.package_id}"
        return store.create_shell_session(default_shell_session_id(), request, cwd_root=cwd_root)

    @app.get("/api/shells/{session_id}", response_model=ShellSessionRecord, dependencies=[Depends(require_auth)])
    def get_shell_session(session_id: str) -> ShellSessionRecord:
        record = store.get_shell_session(session_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shell session not found")
        return record

    @app.post("/api/shells/{session_id}/input", dependencies=[Depends(require_auth)])
    def append_shell_input(session_id: str, request: ShellInputRequest) -> dict[str, int]:
        if store.get_shell_session(session_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shell session not found")
        return {"seq": store.append_shell_input(session_id, request.data)}

    @app.get("/api/shells/{session_id}/events", response_model=ShellEventsResponse, dependencies=[Depends(require_auth)])
    def get_shell_events(session_id: str, after_seq: int = Query(default=0, ge=0)) -> ShellEventsResponse:
        record = store.get_shell_session(session_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shell session not found")
        events = store.get_shell_events(session_id, after_seq)
        next_seq = after_seq + 1 if not events else events[-1].seq + 1
        return ShellEventsResponse(
            session_id=session_id,
            status=record.status,
            events=events,
            next_seq=next_seq,
            exit_code=record.exit_code,
            failure_code=record.failure_code,
        )

    @app.post("/api/shells/{session_id}/close", response_model=ShellSessionRecord, dependencies=[Depends(require_auth)])
    def close_shell_session(session_id: str) -> ShellSessionRecord:
        try:
            return store.close_shell_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shell session not found") from exc

    @app.get("/api/agents/{agent_id}/shells/next", dependencies=[Depends(require_auth)], response_model=None)
    def poll_next_shell(agent_id: str) -> Response | dict:
        lease = store.lease_next_shell_session(agent_id)
        if lease is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return lease.model_dump(mode="json")

    @app.get("/api/shells/{session_id}/inputs", dependencies=[Depends(require_auth)])
    def consume_shell_inputs(session_id: str, after_seq: int = Query(default=0, ge=0)) -> dict:
        if store.get_shell_session(session_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shell session not found")
        items = store.consume_shell_inputs(session_id, after_seq)
        return {"inputs": [{"seq": seq, "data": data} for seq, data in items]}

    @app.post("/api/shells/{session_id}/events", dependencies=[Depends(require_auth)])
    def append_shell_event(session_id: str, request: ShellEventAppendRequest) -> dict:
        if store.get_shell_session(session_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shell session not found")
        event = store.append_shell_event(session_id, request.stream, request.data)
        return event.model_dump(mode="json")

    @app.post("/api/shells/{session_id}/heartbeat", response_model=ShellSessionRecord, dependencies=[Depends(require_auth)])
    def heartbeat_shell_session(session_id: str) -> ShellSessionRecord:
        try:
            return store.heartbeat_shell_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shell session not found") from exc

    @app.post("/api/shells/{session_id}/complete", response_model=ShellSessionRecord, dependencies=[Depends(require_auth)])
    def complete_shell_session(session_id: str, request: ShellCompletionRequest) -> ShellSessionRecord:
        try:
            return store.complete_shell_session(session_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shell session not found") from exc

    return app


def _ensure_runtime_token() -> str:
    current = os.getenv("ORBIT_API_TOKEN")
    if current:
        return current
    value = secrets.token_urlsafe(32)
    os.environ["ORBIT_API_TOKEN"] = value
    print(f"ORBIT_API_TOKEN={value}")
    return value


try:
    app = create_app()
except RuntimeError:
    app = FastAPI(title="mvp-orbit-hub", version="0.3.0")


def main() -> None:
    _ensure_runtime_token()
    host = os.getenv("ORBIT_HUB_HOST", "127.0.0.1")
    port = int(os.getenv("ORBIT_HUB_PORT", "8080"))
    uvicorn.run(create_app(), host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
