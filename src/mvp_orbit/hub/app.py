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
    ConnectRequest,
    ConnectResponse,
    PackageRecord,
    ShellCompletionRequest,
    ShellEventAppendRequest,
    ShellEventsResponse,
    ShellInputRequest,
    ShellSessionCreateRequest,
    ShellSessionRecord,
    ShellSessionStatus,
    default_command_id,
    default_shell_session_id,
)
from mvp_orbit.hub.store import (
    AuthenticatedUser,
    ExpiredTokenError,
    HubStore,
    InvalidTokenError,
    OwnershipError,
)


def _bearer_token(authorization: str | None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    return token


def _bootstrap_dependency(expected_token: str | None):
    def _check_auth(authorization: str | None = Header(default=None)) -> None:
        token = _bearer_token(authorization)
        if expected_token is None or token != expected_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bootstrap token")

    return _check_auth


def _user_dependency(store: HubStore):
    def _check_auth(authorization: str | None = Header(default=None)) -> AuthenticatedUser:
        token = _bearer_token(authorization)
        try:
            return store.authenticate_user_token(token)
        except ExpiredTokenError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="token expired") from exc
        except InvalidTokenError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid user token") from exc

    return _check_auth


def _require_agent_owner(store: HubStore, user: AuthenticatedUser, agent_id: str) -> None:
    try:
        store.assert_agent_owner(agent_id, user.user_id)
    except OwnershipError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc


def _require_command_owner(store: HubStore, user: AuthenticatedUser, command_id: str) -> CommandRecord:
    record = store.get_command(command_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="command not found")
    if record.owner_user_id != user.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    return record


def _require_shell_owner(store: HubStore, user: AuthenticatedUser, session_id: str) -> ShellSessionRecord:
    record = store.get_shell_session(session_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shell session not found")
    if record.owner_user_id != user.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    return record


def create_app(*, store: HubStore | None = None, bootstrap_token: str | None = None) -> FastAPI:
    bootstrap_token = bootstrap_token if bootstrap_token is not None else os.getenv("ORBIT_BOOTSTRAP_TOKEN")
    store = store or HubStore(
        os.getenv("ORBIT_HUB_DB", "./.orbit-hub/hub.sqlite3"),
        os.getenv("ORBIT_OBJECT_ROOT", "./.orbit-hub/objects"),
    )
    require_bootstrap = _bootstrap_dependency(bootstrap_token)
    require_user = _user_dependency(store)

    app = FastAPI(title="mvp-orbit-hub", version="0.4.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/connect", response_model=ConnectResponse, dependencies=[Depends(require_bootstrap)])
    def connect(request: ConnectRequest) -> ConnectResponse:
        return store.issue_user_token(request.user_id)

    @app.post("/api/packages", response_model=PackageRecord)
    def upload_package(
        payload: bytes = Body(..., media_type="application/gzip"),
        user: AuthenticatedUser = Depends(require_user),
    ) -> PackageRecord:
        package_id = object_id_for_bytes(payload)
        return store.put_package(package_id, payload, owner_user_id=user.user_id)

    @app.get("/api/packages/{package_id}", response_model=None)
    def download_package(package_id: str, user: AuthenticatedUser = Depends(require_user)) -> Response:
        try:
            payload = store.get_package(package_id, owner_user_id=user.user_id)
        except OwnershipError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="package not found") from exc
        return Response(content=payload, media_type="application/gzip")

    @app.post("/api/commands", response_model=CommandRecord)
    def create_command(request: CommandCreateRequest, user: AuthenticatedUser = Depends(require_user)) -> CommandRecord:
        _require_agent_owner(store, user, request.agent_id)
        if request.package_id is not None:
            try:
                store.ensure_package_access(request.package_id, owner_user_id=user.user_id)
            except OwnershipError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc
        return store.create_command(default_command_id(), user.user_id, request)

    @app.get("/api/commands/{command_id}", response_model=CommandRecord)
    def get_command(command_id: str, user: AuthenticatedUser = Depends(require_user)) -> CommandRecord:
        return _require_command_owner(store, user, command_id)

    @app.get("/api/commands/{command_id}/output", response_model=CommandOutputChunk)
    def get_command_output(
        command_id: str,
        stdout_offset: int = Query(default=0, ge=0),
        stderr_offset: int = Query(default=0, ge=0),
        user: AuthenticatedUser = Depends(require_user),
    ) -> CommandOutputChunk:
        _require_command_owner(store, user, command_id)
        try:
            return store.read_command_output(
                command_id,
                stdout_offset=stdout_offset,
                stderr_offset=stderr_offset,
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="command not found") from exc

    @app.post("/api/commands/{command_id}/output")
    def append_command_output(
        command_id: str,
        request: CommandOutputAppendRequest,
        user: AuthenticatedUser = Depends(require_user),
    ) -> dict[str, str]:
        _require_command_owner(store, user, command_id)
        try:
            store.append_command_output(command_id, request.stream, request.data)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="command not found") from exc
        return {"status": "accepted"}

    @app.post("/api/commands/{command_id}/cancel", response_model=CommandRecord)
    def cancel_command(command_id: str, user: AuthenticatedUser = Depends(require_user)) -> CommandRecord:
        _require_command_owner(store, user, command_id)
        try:
            return store.cancel_command(command_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="command not found") from exc

    @app.get("/api/agents/{agent_id}/commands/next", response_model=None)
    def poll_next_command(agent_id: str, user: AuthenticatedUser = Depends(require_user)) -> Response | dict:
        try:
            store.register_agent(agent_id, user.user_id)
        except OwnershipError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc
        lease = store.lease_next_command(agent_id, user.user_id)
        if lease is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return lease.model_dump(mode="json")

    @app.post("/api/commands/{command_id}/heartbeat", response_model=CommandRecord)
    def heartbeat_command(command_id: str, user: AuthenticatedUser = Depends(require_user)) -> CommandRecord:
        _require_command_owner(store, user, command_id)
        try:
            return store.heartbeat_command(command_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="command not found") from exc

    @app.post("/api/commands/{command_id}/complete", response_model=CommandRecord)
    def complete_command(
        command_id: str,
        request: CommandCompletionRequest,
        user: AuthenticatedUser = Depends(require_user),
    ) -> CommandRecord:
        _require_command_owner(store, user, command_id)
        try:
            return store.complete_command(command_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="command not found") from exc

    @app.post("/api/shells", response_model=ShellSessionRecord)
    def create_shell_session(
        request: ShellSessionCreateRequest,
        user: AuthenticatedUser = Depends(require_user),
    ) -> ShellSessionRecord:
        _require_agent_owner(store, user, request.agent_id)
        if request.package_id is not None:
            try:
                store.ensure_package_access(request.package_id, owner_user_id=user.user_id)
            except OwnershipError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc
        cwd_root = "." if request.package_id is None else f".orbit/packages/{request.package_id}"
        return store.create_shell_session(default_shell_session_id(), user.user_id, request, cwd_root=cwd_root)

    @app.get("/api/shells", response_model=list[ShellSessionRecord])
    def list_shell_sessions(
        agent_id: str | None = Query(default=None),
        session_status: ShellSessionStatus | None = Query(default=None, alias="status"),
        user: AuthenticatedUser = Depends(require_user),
    ) -> list[ShellSessionRecord]:
        return store.list_shell_sessions(user.user_id, agent_id=agent_id, session_status=session_status)

    @app.get("/api/shells/{session_id}", response_model=ShellSessionRecord)
    def get_shell_session(session_id: str, user: AuthenticatedUser = Depends(require_user)) -> ShellSessionRecord:
        return _require_shell_owner(store, user, session_id)

    @app.post("/api/shells/{session_id}/input")
    def append_shell_input(
        session_id: str,
        request: ShellInputRequest,
        user: AuthenticatedUser = Depends(require_user),
    ) -> dict[str, int]:
        _require_shell_owner(store, user, session_id)
        return {"seq": store.append_shell_input(session_id, request.data)}

    @app.get("/api/shells/{session_id}/events", response_model=ShellEventsResponse)
    def get_shell_events(
        session_id: str,
        after_seq: int = Query(default=0, ge=0),
        user: AuthenticatedUser = Depends(require_user),
    ) -> ShellEventsResponse:
        record = _require_shell_owner(store, user, session_id)
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

    @app.post("/api/shells/{session_id}/close", response_model=ShellSessionRecord)
    def close_shell_session(session_id: str, user: AuthenticatedUser = Depends(require_user)) -> ShellSessionRecord:
        _require_shell_owner(store, user, session_id)
        try:
            return store.close_shell_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shell session not found") from exc

    @app.get("/api/agents/{agent_id}/shells/next", response_model=None)
    def poll_next_shell(agent_id: str, user: AuthenticatedUser = Depends(require_user)) -> Response | dict:
        try:
            store.register_agent(agent_id, user.user_id)
        except OwnershipError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc
        lease = store.lease_next_shell_session(agent_id, user.user_id)
        if lease is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return lease.model_dump(mode="json")

    @app.get("/api/shells/{session_id}/inputs")
    def consume_shell_inputs(
        session_id: str,
        after_seq: int = Query(default=0, ge=0),
        user: AuthenticatedUser = Depends(require_user),
    ) -> dict:
        _require_shell_owner(store, user, session_id)
        items = store.consume_shell_inputs(session_id, after_seq)
        return {"inputs": [{"seq": seq, "data": data} for seq, data in items]}

    @app.post("/api/shells/{session_id}/events")
    def append_shell_event(
        session_id: str,
        request: ShellEventAppendRequest,
        user: AuthenticatedUser = Depends(require_user),
    ) -> dict:
        _require_shell_owner(store, user, session_id)
        event = store.append_shell_event(session_id, request.stream, request.data)
        return event.model_dump(mode="json")

    @app.post("/api/shells/{session_id}/heartbeat", response_model=ShellSessionRecord)
    def heartbeat_shell_session(session_id: str, user: AuthenticatedUser = Depends(require_user)) -> ShellSessionRecord:
        _require_shell_owner(store, user, session_id)
        try:
            return store.heartbeat_shell_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shell session not found") from exc

    @app.post("/api/shells/{session_id}/complete", response_model=ShellSessionRecord)
    def complete_shell_session(
        session_id: str,
        request: ShellCompletionRequest,
        user: AuthenticatedUser = Depends(require_user),
    ) -> ShellSessionRecord:
        _require_shell_owner(store, user, session_id)
        try:
            return store.complete_shell_session(session_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shell session not found") from exc

    return app


def _ensure_runtime_token() -> str:
    current = os.getenv("ORBIT_BOOTSTRAP_TOKEN")
    if current:
        return current
    value = secrets.token_urlsafe(32)
    os.environ["ORBIT_BOOTSTRAP_TOKEN"] = value
    print(f"ORBIT_BOOTSTRAP_TOKEN={value}")
    return value


try:
    app = create_app()
except RuntimeError:
    app = FastAPI(title="mvp-orbit-hub", version="0.4.0")


def main() -> None:
    _ensure_runtime_token()
    host = os.getenv("ORBIT_HUB_HOST", "127.0.0.1")
    port = int(os.getenv("ORBIT_HUB_PORT", "8080"))
    uvicorn.run(create_app(), host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
