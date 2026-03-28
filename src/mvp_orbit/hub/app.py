from __future__ import annotations

import asyncio
import json
import os
import secrets

import uvicorn
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, StreamingResponse

from mvp_orbit.core.canonical import object_id_for_bytes
from mvp_orbit.core.models import (
    CommandStatus,
    CommandCreateRequest,
    CommandOutputChunk,
    CommandLease,
    CommandRecord,
    ConnectRequest,
    ConnectResponse,
    AgentEventsRequest,
    PackageRecord,
    ShellInputRequest,
    ShellResizeRequest,
    ShellSessionLease,
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

LANDING_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Orbit Hub</title>
    <style>
      :root {
        --bg: #f5f7fb;
        --surface: rgba(255, 255, 255, 0.8);
        --surface-strong: rgba(255, 255, 255, 0.96);
        --line: rgba(15, 23, 42, 0.09);
        --line-strong: rgba(15, 23, 42, 0.14);
        --text: #0f172a;
        --muted: #52607a;
        --soft: #6b7280;
        --accent: #2563eb;
        --accent-soft: rgba(37, 99, 235, 0.12);
        --shadow: 0 24px 80px rgba(15, 23, 42, 0.08);
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        min-height: 100vh;
        font-family: "Space Grotesk", "Sora", "Avenir Next", sans-serif;
        color: var(--text);
        background:
          radial-gradient(circle at top left, rgba(37, 99, 235, 0.08), transparent 24%),
          radial-gradient(circle at 85% 15%, rgba(15, 23, 42, 0.06), transparent 22%),
          linear-gradient(180deg, #fcfdff 0%, #f5f7fb 50%, #eef2f8 100%);
      }
      body::before {
        content: "";
        position: fixed;
        inset: 0;
        background:
          linear-gradient(rgba(255, 255, 255, 0.28), rgba(255, 255, 255, 0.28)),
          repeating-linear-gradient(
            90deg,
            transparent 0,
            transparent 79px,
            rgba(15, 23, 42, 0.03) 80px
          );
        pointer-events: none;
      }
      main {
        position: relative;
        width: min(1160px, calc(100vw - 32px));
        margin: 0 auto;
        padding: 28px 0 72px;
      }
      .hero {
        padding: 28px;
        border: 1px solid var(--line);
        border-radius: 32px;
        background: linear-gradient(180deg, rgba(255, 255, 255, 0.95), rgba(255, 255, 255, 0.72));
        box-shadow: var(--shadow);
        backdrop-filter: blur(12px);
      }
      .topbar {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 16px;
        margin-bottom: 32px;
      }
      .kicker {
        display: inline-flex;
        align-items: center;
        gap: 10px;
        padding: 10px 14px;
        border: 1px solid var(--line);
        border-radius: 999px;
        background: rgba(255, 255, 255, 0.78);
        color: var(--soft);
        font-size: 12px;
        letter-spacing: 0.16em;
        text-transform: uppercase;
      }
      .kicker::before {
        content: "";
        width: 8px;
        height: 8px;
        border-radius: 999px;
        background: var(--accent);
        box-shadow: 0 0 0 6px var(--accent-soft);
      }
      .brandplate {
        padding: 12px 16px;
        border: 1px solid var(--line);
        border-radius: 18px;
        background: rgba(255, 255, 255, 0.84);
        text-align: right;
      }
      .brandplate strong {
        display: block;
        font-size: 14px;
        letter-spacing: 0.14em;
        text-transform: uppercase;
      }
      .brandplate span {
        color: var(--muted);
        font-size: 12px;
      }
      .hero-grid {
        display: grid;
        grid-template-columns: minmax(0, 1.2fr) minmax(320px, 0.8fr);
        gap: 24px;
        align-items: start;
      }
      h1 {
        margin: 0;
        max-width: 10ch;
        font-size: clamp(50px, 11vw, 108px);
        line-height: 0.9;
        letter-spacing: -0.065em;
      }
      .accent {
        color: var(--accent);
      }
      .lead {
        margin: 24px 0 0;
        max-width: 58ch;
        color: var(--muted);
        font-size: 17px;
        line-height: 1.75;
      }
      .hero-actions {
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        margin-top: 28px;
      }
      .chip {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 11px 14px;
        border: 1px solid var(--line);
        border-radius: 999px;
        background: rgba(255, 255, 255, 0.88);
        color: var(--text);
        font-size: 13px;
      }
      .chip strong {
        font-size: 12px;
        letter-spacing: 0.14em;
        text-transform: uppercase;
      }
      .spotlight {
        padding: 20px;
        border: 1px solid var(--line);
        border-radius: 24px;
        background:
          radial-gradient(circle at top right, rgba(37, 99, 235, 0.08), transparent 34%),
          rgba(248, 250, 252, 0.96);
      }
      .spotlight h2 {
        margin: 0;
        font-size: 13px;
        letter-spacing: 0.16em;
        text-transform: uppercase;
        color: var(--soft);
      }
      .command-stack {
        display: grid;
        gap: 12px;
        margin-top: 18px;
      }
      .cmd {
        display: block;
        overflow-x: auto;
        padding: 16px;
        border: 1px solid rgba(15, 23, 42, 0.08);
        border-radius: 18px;
        background: #ffffff;
        color: #172033;
        font-family: "IBM Plex Mono", "JetBrains Mono", monospace;
        font-size: 13px;
        line-height: 1.6;
        white-space: pre-wrap;
        word-break: break-word;
      }
      .metrics {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 14px;
        margin-top: 18px;
      }
      .metric {
        padding: 16px;
        border: 1px solid var(--line);
        border-radius: 20px;
        background: rgba(255, 255, 255, 0.88);
      }
      .metric strong {
        display: block;
        margin-bottom: 6px;
        font-size: 26px;
        letter-spacing: -0.04em;
      }
      .metric span {
        color: var(--muted);
        font-size: 13px;
      }
      .sections {
        display: grid;
        grid-template-columns: minmax(0, 1fr) minmax(0, 0.9fr);
        gap: 18px;
        margin-top: 18px;
      }
      .card {
        padding: 22px;
        border: 1px solid var(--line);
        border-radius: 24px;
        background: var(--surface-strong);
        box-shadow: 0 12px 40px rgba(15, 23, 42, 0.05);
      }
      .card h2 {
        margin: 0 0 16px;
        font-size: 13px;
        letter-spacing: 0.16em;
        text-transform: uppercase;
        color: var(--soft);
      }
      .feature-list {
        display: grid;
        gap: 14px;
      }
      .feature {
        padding-top: 14px;
        border-top: 1px solid rgba(15, 23, 42, 0.08);
      }
      .feature:first-child {
        padding-top: 0;
        border-top: 0;
      }
      .feature strong {
        display: block;
        margin-bottom: 6px;
        font-size: 18px;
        letter-spacing: -0.03em;
      }
      .feature span {
        color: var(--muted);
        line-height: 1.7;
      }
      .links {
        display: grid;
        gap: 12px;
      }
      .link-card {
        display: block;
        padding: 18px;
        border: 1px solid var(--line);
        border-radius: 20px;
        background: rgba(255, 255, 255, 0.86);
        color: inherit;
        text-decoration: none;
        transition: transform 150ms ease, border-color 150ms ease, box-shadow 150ms ease;
      }
      .link-card strong {
        display: block;
        margin-bottom: 6px;
        font-size: 17px;
      }
      .link-card span {
        color: var(--muted);
        line-height: 1.68;
      }
      .link-card:hover {
        transform: translateY(-2px);
        border-color: rgba(37, 99, 235, 0.24);
        box-shadow: 0 16px 34px rgba(37, 99, 235, 0.08);
      }
      .footer {
        margin-top: 18px;
        color: var(--muted);
        font-size: 13px;
        text-align: center;
      }
      @media (max-width: 920px) {
        .topbar,
        .hero-grid,
        .sections,
        .metrics {
          grid-template-columns: 1fr;
          flex-direction: column;
          align-items: stretch;
        }
        .brandplate {
          text-align: left;
        }
      }
    </style>
  </head>
  <body>
    <main>
      <section class="hero">
        <div class="topbar">
          <div class="kicker">Orbit Hub Online</div>
          <div class="brandplate">
            <strong>By MVP Lab</strong>
            <span>Minimal control plane for remote execution</span>
          </div>
        </div>
        <div class="hero-grid">
          <div>
            <h1>Route Jobs.<br /><span class="accent">Own Agents.</span></h1>
            <p class="lead">
              Orbit gives one endpoint a clean HTTP control plane for packages, reconnectable shells, and
              deterministic remote command dispatch. Connect once, claim an agent, and run work without SSH hops,
              tunnels, or per-host terminal sprawl.
            </p>
            <div class="hero-actions">
              <div class="chip"><strong>HTTP Only</strong><span>No SSH required</span></div>
              <div class="chip"><strong>7D</strong><span>User token lifetime</span></div>
              <div class="chip"><strong>Live</strong><span>Shell and output stream</span></div>
            </div>
          </div>
          <aside class="spotlight">
            <h2>Quick Start</h2>
            <div class="command-stack">
              <code class="cmd">orbit connect --hub-url https://your-hub.example</code>
              <code class="cmd">orbit init node --agent-id gpu-a</code>
              <code class="cmd">orbit command exec --agent-id gpu-a --shell "python3 -V && nvidia-smi"</code>
            </div>
            <div class="metrics">
              <div class="metric"><strong>1</strong><span>Hub endpoint</span></div>
              <div class="metric"><strong>N</strong><span>User-owned agents</span></div>
              <div class="metric"><strong>0</strong><span>Shared global tokens</span></div>
            </div>
          </aside>
        </div>
      </section>
      <section class="sections">
        <div class="card">
          <h2>Core Features</h2>
          <div class="feature-list">
            <div class="feature">
              <strong>HTTP instead of SSH</strong>
              <span>Connect the CLI and agents to one Hub over HTTP and run remote work without opening SSH sessions, tunnels, or jump hosts.</span>
            </div>
            <div class="feature">
              <strong>Package shipping</strong>
              <span>Upload a workspace once, reuse deduplicated packages, and dispatch remote runs without rebuilding the same payload every time.</span>
            </div>
            <div class="feature">
              <strong>Live execution surface</strong>
              <span>Run one-off commands, follow stdout and stderr, and keep reconnectable shells available when interactive work is needed.</span>
            </div>
          </div>
        </div>
        <div class="card">
          <h2>Links</h2>
          <div class="links">
            <a class="link-card" href="https://github.com/mvp-ai-lab/mvp-orbit" target="_blank" rel="noreferrer">
              <strong>GitHub Repository</strong>
              <span>Source, issues, and commit history for the Hub, CLI, and Agent runtime.</span>
            </a>
            <a class="link-card" href="https://github.com/mvp-ai-lab/mvp-orbit/releases/tag/v0.5.0" target="_blank" rel="noreferrer">
              <strong>Latest Release</strong>
              <span>Grab the current wheel, inspect release notes, and track shipped versions.</span>
            </a>
            <a class="link-card" href="https://github.com/mvp-ai-lab/mvp-orbit/blob/main/README.md" target="_blank" rel="noreferrer">
              <strong>README / Quick Start</strong>
              <span>Connect, claim an agent, ship a package, and dispatch work from one CLI.</span>
            </a>
          </div>
        </div>
      </section>
      <div class="footer">
        Orbit Hub is live. Authenticate, claim your agent, and dispatch work through a minimal control plane.
      </div>
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


def _format_sse(event_id: int, kind: str, payload: dict) -> bytes:
    return f"id: {event_id}\nevent: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def create_app(*, store: HubStore | None = None, bootstrap_token: str | None = None) -> FastAPI:
    bootstrap_token = bootstrap_token if bootstrap_token is not None else os.getenv("ORBIT_BOOTSTRAP_TOKEN")
    store = store or HubStore(
        os.getenv("ORBIT_HUB_DB", "./.orbit-hub/hub.sqlite3"),
        os.getenv("ORBIT_OBJECT_ROOT", "./.orbit-hub/objects"),
    )
    require_bootstrap = _bootstrap_dependency(bootstrap_token)
    require_user = _user_dependency(store)

    app = FastAPI(title="mvp-orbit-hub", version="0.5.0")

    async def _agent_stream(request: Request, agent_id: str):
        try:
            last_event_id = int(request.headers.get("Last-Event-ID", "0") or "0")
        except ValueError:
            last_event_id = 0
        while True:
            events = store.get_agent_control_events(agent_id, last_event_id)
            if events:
                for event in events:
                    yield _format_sse(event.event_id, event.kind, event.payload)
                    last_event_id = event.event_id
                continue
            if await request.is_disconnected():
                break
            updated = await asyncio.to_thread(store.wait_for_updates, 5.0)
            if not updated:
                yield b": keepalive\n\n"

    async def _command_stream(request: Request, command_id: str):
        try:
            last_event_id = int(request.headers.get("Last-Event-ID", "0") or "0")
        except ValueError:
            last_event_id = 0
        while True:
            events = store.get_command_events(command_id, last_event_id)
            if events:
                for event in events:
                    yield _format_sse(event.event_id, event.kind, event.payload)
                    last_event_id = event.event_id
                continue
            record = store.get_command(command_id)
            if record is None:
                break
            if record.status in {CommandStatus.SUCCEEDED, CommandStatus.FAILED, CommandStatus.CANCELED}:
                break
            if await request.is_disconnected():
                break
            updated = await asyncio.to_thread(store.wait_for_updates, 5.0)
            if not updated:
                yield b": keepalive\n\n"

    async def _shell_stream(request: Request, session_id: str):
        try:
            last_event_id = int(request.headers.get("Last-Event-ID", "0") or "0")
        except ValueError:
            last_event_id = 0
        while True:
            events = store.get_shell_events(session_id, last_event_id)
            if events:
                for event in events:
                    yield _format_sse(event.event_id, event.kind, event.payload)
                    last_event_id = event.event_id
                continue
            record = store.get_shell_session(session_id)
            if record is None:
                break
            if record.status in {ShellSessionStatus.CLOSED, ShellSessionStatus.FAILED}:
                break
            if await request.is_disconnected():
                break
            updated = await asyncio.to_thread(store.wait_for_updates, 5.0)
            if not updated:
                yield b": keepalive\n\n"

    @app.get("/", response_class=HTMLResponse)
    def landing_page() -> HTMLResponse:
        return HTMLResponse(LANDING_PAGE_HTML)

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

    @app.post("/api/commands/{command_id}/claim", response_model=CommandLease)
    def claim_command(command_id: str, user: AuthenticatedUser = Depends(require_user)) -> CommandLease:
        record = _require_command_owner(store, user, command_id)
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
        user: AuthenticatedUser = Depends(require_user),
    ) -> CommandOutputChunk:
        _require_command_owner(store, user, command_id)
        try:
            return store.read_command_output(command_id, stdout_offset=stdout_offset, stderr_offset=stderr_offset)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="command not found") from exc

    @app.get("/api/commands/{command_id}/stream")
    async def stream_command_output(command_id: str, request: Request, user: AuthenticatedUser = Depends(require_user)) -> StreamingResponse:
        _require_command_owner(store, user, command_id)
        return StreamingResponse(
            _command_stream(request, command_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/commands/{command_id}/cancel", response_model=CommandRecord)
    def cancel_command(command_id: str, user: AuthenticatedUser = Depends(require_user)) -> CommandRecord:
        _require_command_owner(store, user, command_id)
        try:
            return store.cancel_command(command_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="command not found") from exc

    @app.get("/api/agents/{agent_id}/stream")
    async def agent_stream(agent_id: str, request: Request, user: AuthenticatedUser = Depends(require_user)) -> StreamingResponse:
        try:
            store.register_agent(agent_id, user.user_id)
        except OwnershipError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc
        return StreamingResponse(
            _agent_stream(request, agent_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/agents/{agent_id}/events")
    def append_agent_events(
        agent_id: str,
        request: AgentEventsRequest,
        user: AuthenticatedUser = Depends(require_user),
    ) -> dict[str, int]:
        try:
            store.register_agent(agent_id, user.user_id)
        except OwnershipError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc
        store.apply_agent_events(agent_id, request.events)
        return {"accepted": len(request.events)}

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

    @app.post("/api/shells/{session_id}/claim", response_model=ShellSessionLease)
    def claim_shell_session(session_id: str, user: AuthenticatedUser = Depends(require_user)) -> ShellSessionLease:
        record = _require_shell_owner(store, user, session_id)
        try:
            return store.claim_shell_session(record.session_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shell session not found") from exc

    @app.post("/api/shells/{session_id}/input")
    def append_shell_input(
        session_id: str,
        request: ShellInputRequest,
        user: AuthenticatedUser = Depends(require_user),
    ) -> dict[str, int]:
        _require_shell_owner(store, user, session_id)
        try:
            return {"seq": store.append_shell_input(session_id, request.data)}
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shell session not found") from exc

    @app.post("/api/shells/{session_id}/resize")
    def resize_shell_session(
        session_id: str,
        request: ShellResizeRequest,
        user: AuthenticatedUser = Depends(require_user),
    ) -> dict[str, int]:
        _require_shell_owner(store, user, session_id)
        try:
            return {"seq": store.resize_shell_session(session_id, request.rows, request.cols)}
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shell session not found") from exc

    @app.get("/api/shells/{session_id}/stream")
    async def stream_shell_output(session_id: str, request: Request, user: AuthenticatedUser = Depends(require_user)) -> StreamingResponse:
        _require_shell_owner(store, user, session_id)
        return StreamingResponse(
            _shell_stream(request, session_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/shells/{session_id}/close", response_model=ShellSessionRecord)
    def close_shell_session(session_id: str, user: AuthenticatedUser = Depends(require_user)) -> ShellSessionRecord:
        _require_shell_owner(store, user, session_id)
        try:
            return store.close_shell_session(session_id)
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
    app = FastAPI(title="mvp-orbit-hub", version="0.5.0")


def main() -> None:
    _ensure_runtime_token()
    host = os.getenv("ORBIT_HUB_HOST", "127.0.0.1")
    port = int(os.getenv("ORBIT_HUB_PORT", "8080"))
    uvicorn.run(create_app(), host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
