from __future__ import annotations

import os
import secrets

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status

from mvp_orbit.core.models import (
    RunCompletionRequest,
    RunCreateRequest,
    RunCreateResponse,
    RunHeartbeatRequest,
    RunRecord,
    RunStatus,
    default_run_id,
    default_ticket_expiry,
    utc_now,
)
from mvp_orbit.core.tickets import RunTicketManager
from mvp_orbit.hub.store import RunStore


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


def create_app(
    *,
    run_store: RunStore | None = None,
    ticket_manager: RunTicketManager | None = None,
    api_token: str | None = None,
) -> FastAPI:
    api_token = api_token if api_token is not None else os.getenv("ORBIT_API_TOKEN")
    ticket_ttl = int(os.getenv("ORBIT_TICKET_TTL_SEC", "120"))
    run_store = run_store or RunStore(os.getenv("ORBIT_HUB_DB", "./.orbit-hub/runs.sqlite3"))
    if ticket_manager is None:
        ticket_manager = RunTicketManager(_required_env("ORBIT_TICKET_SECRET"))
    require_auth = _auth_dependency(api_token)

    app = FastAPI(title="mvp-orbit-hub", version="0.2.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/runs", response_model=RunCreateResponse, dependencies=[Depends(require_auth)])
    def create_run(request: RunCreateRequest) -> RunCreateResponse:
        run_id = default_run_id()
        expires_at = default_ticket_expiry(ticket_ttl)
        run_ticket, _ = ticket_manager.issue(
            run_id=run_id,
            agent_id=request.agent_id,
            task_id=request.task_id,
            expires_at=expires_at,
        )
        record = RunRecord(
            run_id=run_id,
            agent_id=request.agent_id,
            task_id=request.task_id,
            run_ticket=run_ticket,
            expires_at=expires_at,
            status=RunStatus.QUEUED,
            created_at=utc_now(),
        )
        run_store.create_run(record)
        return RunCreateResponse(
            run_id=run_id,
            agent_id=request.agent_id,
            task_id=request.task_id,
            run_ticket=run_ticket,
            expires_at=expires_at,
        )

    @app.get("/api/agents/{agent_id}/next", dependencies=[Depends(require_auth)], response_model=None)
    def poll_next(agent_id: str) -> Response | dict:
        record = run_store.lease_next(agent_id)
        if record is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return record.model_dump(mode="json", include={"run_id", "agent_id", "task_id", "run_ticket", "expires_at"})

    @app.post("/api/runs/{run_id}/heartbeat", dependencies=[Depends(require_auth)])
    def heartbeat(run_id: str, heartbeat_request: RunHeartbeatRequest) -> dict[str, str]:
        try:
            run_store.heartbeat(run_id, phase=heartbeat_request.phase)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found") from exc
        return {"status": "accepted"}

    @app.post("/api/runs/{run_id}/complete", dependencies=[Depends(require_auth)])
    def complete(run_id: str, completion: RunCompletionRequest) -> dict[str, str]:
        try:
            run_store.complete(run_id, completion)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found") from exc
        return {"status": "accepted"}

    @app.get("/api/runs/{run_id}", dependencies=[Depends(require_auth)])
    def get_run(run_id: str) -> dict:
        record = run_store.get_run(run_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        return record.model_dump(mode="json")

    return app


def _ensure_runtime_secret(name: str, *, generator) -> str:
    current = os.getenv(name)
    if current:
        return current
    value = generator()
    os.environ[name] = value
    print(f"{name}={value}")
    return value


def _bootstrap_runtime_settings() -> None:
    generated: list[str] = []

    if not os.getenv("ORBIT_TICKET_SECRET"):
        _ensure_runtime_secret("ORBIT_TICKET_SECRET", generator=lambda: secrets.token_urlsafe(48))
        generated.append("ORBIT_TICKET_SECRET")
    if not os.getenv("ORBIT_API_TOKEN"):
        _ensure_runtime_secret("ORBIT_API_TOKEN", generator=lambda: secrets.token_urlsafe(32))
        generated.append("ORBIT_API_TOKEN")

    if generated:
        print("Generated missing Hub secrets for this process. Export the values above if other processes must reuse them.")


try:
    app = create_app()
except RuntimeError:
    # Import-time fallback for environments that only need create_app() or main().
    app = FastAPI(title="mvp-orbit-hub", version="0.2.0")


def main() -> None:
    _bootstrap_runtime_settings()
    host = os.getenv("ORBIT_HUB_HOST", "127.0.0.1")
    port = int(os.getenv("ORBIT_HUB_PORT", "8080"))
    uvicorn.run(create_app(), host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
