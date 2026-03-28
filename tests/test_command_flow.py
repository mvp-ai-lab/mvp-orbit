from __future__ import annotations

import sys
import threading
import time
from datetime import timedelta

from fastapi.testclient import TestClient

from mvp_orbit.agent.runtime import AgentRuntime
from mvp_orbit.cli.package import build_file_package
from mvp_orbit.core.models import AgentEvent, AgentEventsRequest, CommandLease, ShellSessionLease, utc_now
from mvp_orbit.hub.app import create_app
from mvp_orbit.hub.store import HubStore

BOOTSTRAP_TOKEN = "bootstrap-token"


def _build_client(tmp_path):
    store = HubStore(tmp_path / "hub.sqlite3", tmp_path / "objects")
    app = create_app(store=store, bootstrap_token=BOOTSTRAP_TOKEN)
    return TestClient(app), store


def _connect(client: TestClient, user_id: str) -> dict:
    response = client.post(
        "/api/connect",
        json={"user_id": user_id},
        headers={"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"},
    )
    assert response.status_code == 200
    return response.json()


def _auth(user_token: str, *, content_type: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {user_token}"}
    if content_type is not None:
        headers["Content-Type"] = content_type
    return headers


def _post_agent_events(client: TestClient, user_token: str, agent_id: str, events: list[AgentEvent]) -> None:
    response = client.post(
        f"/api/agents/{agent_id}/events",
        json=AgentEventsRequest(events=events).model_dump(mode="json"),
        headers=_auth(user_token),
    )
    assert response.status_code == 200


def _run_command_runtime(client: TestClient, user_token: str, tmp_path, command_id: str, *, workspace=None) -> None:
    runtime = AgentRuntime(agent_id="agent-a", base_workspace=workspace or (tmp_path / "workspace"))
    claimed = client.post(f"/api/commands/{command_id}/claim", headers=_auth(user_token))
    assert claimed.status_code == 200
    lease = CommandLease.model_validate(claimed.json())

    def emit(kind: str, payload: dict) -> None:
        _post_agent_events(client, user_token, "agent-a", [AgentEvent(kind=kind, payload=payload)])

    outcome = runtime.handle_command(
        lease=lease,  # type: ignore[arg-type]
        fetch_package=lambda package_id: client.get(f"/api/packages/{package_id}", headers=_auth(user_token)).content,
        on_started=lambda: emit("command.started", {"command_id": command_id}),
        append_output=lambda stream, data: emit(f"command.{stream}", {"command_id": command_id, "data": data}),
        should_cancel=lambda: False,
    )
    emit(
        "command.exit",
        {
            "command_id": command_id,
            "status": outcome.status.value,
            "exit_code": outcome.exit_code,
            "failure_code": outcome.failure_code,
        },
    )


def test_connect_rejects_invalid_bootstrap_token(tmp_path):
    client, _ = _build_client(tmp_path)
    response = client.post(
        "/api/connect",
        json={"user_id": "alice"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401


def test_root_serves_landing_page(tmp_path):
    client, _ = _build_client(tmp_path)
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Orbit Hub Online" in response.text


def test_command_creation_emits_agent_control_event(tmp_path):
    client, store = _build_client(tmp_path)
    alice = _connect(client, "alice")
    store.register_agent("agent-a", "alice")

    created = client.post(
        "/api/commands",
        json={
            "agent_id": "agent-a",
            "argv": [sys.executable, "-c", "print('hello', flush=True)"],
            "working_dir": ".",
            "timeout_sec": 30,
            "env_patch": {},
        },
        headers=_auth(alice["user_token"]),
    )
    command_id = created.json()["command_id"]
    events = store.get_agent_control_events("agent-a", 0)
    assert events[-1].kind == "command.start"
    assert events[-1].payload["command_id"] == command_id


def test_end_to_end_command_stream_with_package(tmp_path):
    client, store = _build_client(tmp_path)
    alice = _connect(client, "alice")
    store.register_agent("agent-a", "alice")

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "payload.txt").write_text("hello-orbit", encoding="utf-8")
    package_build = build_file_package(source_dir)
    package = client.post(
        "/api/packages",
        content=package_build.archive_path.read_bytes(),
        headers=_auth(alice["user_token"], content_type="application/gzip"),
    )
    package_id = package.json()["package_id"]

    created = client.post(
        "/api/commands",
        json={
            "agent_id": "agent-a",
            "package_id": package_id,
            "argv": [sys.executable, "-c", "from pathlib import Path; print(Path('payload.txt').read_text(), flush=True)"],
            "working_dir": ".",
            "timeout_sec": 30,
            "env_patch": {},
        },
        headers=_auth(alice["user_token"]),
    )
    command_id = created.json()["command_id"]

    _run_command_runtime(client, alice["user_token"], tmp_path, command_id)

    events = store.get_command_events(command_id, 0)
    stdout = "".join(event.payload.get("data", "") for event in events if event.kind == "command.stdout")
    assert "hello-orbit" in stdout
    assert events[-1].kind == "command.exit"

    status = client.get(f"/api/commands/{command_id}", headers=_auth(alice["user_token"])).json()
    assert status["status"] == "succeeded"


def test_cancel_queued_command_emits_terminal_stream_event(tmp_path):
    client, store = _build_client(tmp_path)
    alice = _connect(client, "alice")
    store.register_agent("agent-a", "alice")

    created = client.post(
        "/api/commands",
        json={
            "agent_id": "agent-a",
            "argv": [sys.executable, "-c", "print('ignored')"],
            "working_dir": ".",
            "timeout_sec": 30,
            "env_patch": {},
        },
        headers=_auth(alice["user_token"]),
    )
    command_id = created.json()["command_id"]
    canceled = client.post(f"/api/commands/{command_id}/cancel", headers=_auth(alice["user_token"]))
    assert canceled.status_code == 200

    events = store.get_command_events(command_id, 0)
    assert events[-1].kind == "command.exit"
    assert events[-1].payload["status"] == "canceled"


def test_shell_resize_and_repl_output_flow(tmp_path):
    client, store = _build_client(tmp_path)
    alice = _connect(client, "alice")
    store.register_agent("agent-a", "alice")
    runtime = AgentRuntime(agent_id="agent-a", base_workspace=tmp_path / "workspace")

    created = client.post("/api/shells", json={"agent_id": "agent-a"}, headers=_auth(alice["user_token"]))
    session_id = created.json()["session_id"]
    claimed = client.post(f"/api/shells/{session_id}/claim", headers=_auth(alice["user_token"]))
    assert claimed.status_code == 200
    lease = ShellSessionLease.model_validate(claimed.json())

    input_queue: list[bytes] = []
    resize_queue: list[tuple[int, int]] = []
    close_requested = threading.Event()

    def pump_controls() -> None:
        after = 0
        while not close_requested.is_set():
            for event in store.get_agent_control_events("agent-a", after):
                after = event.event_id
                if event.kind == "shell.stdin" and event.payload["session_id"] == session_id:
                    input_queue.append(str(event.payload["data"]).encode("utf-8"))
                elif event.kind == "shell.resize" and event.payload["session_id"] == session_id:
                    resize_queue.append((int(event.payload["rows"]), int(event.payload["cols"])))
                elif event.kind == "shell.close" and event.payload["session_id"] == session_id:
                    close_requested.set()
            time.sleep(0.05)

    control_thread = threading.Thread(target=pump_controls, daemon=True)
    control_thread.start()

    def emit(kind: str, payload: dict) -> None:
        _post_agent_events(client, alice["user_token"], "agent-a", [AgentEvent(kind=kind, payload=payload)])

    def run_shell() -> None:
        outcome = runtime.handle_shell_session(
            lease=lease,
            fetch_package=lambda package_id: client.get(f"/api/packages/{package_id}", headers=_auth(alice["user_token"])).content,
            on_started=lambda: emit("shell.started", {"session_id": session_id}),
            append_output=lambda data: emit("shell.stdout", {"session_id": session_id, "data": data}),
            pop_input=lambda: _drain(input_queue),
            pop_resize=lambda: _drain(resize_queue),
            should_close=close_requested.is_set,
        )
        emit(
            "shell.closed" if outcome.status.value == "closed" else "shell.exit",
            {
                "session_id": session_id,
                "status": outcome.status.value,
                "exit_code": outcome.exit_code,
                "failure_code": outcome.failure_code,
            },
        )

    shell_thread = threading.Thread(target=run_shell, daemon=True)
    shell_thread.start()

    client.post(f"/api/shells/{session_id}/resize", json={"rows": 40, "cols": 100}, headers=_auth(alice["user_token"]))
    client.post(f"/api/shells/{session_id}/input", json={"data": "stty size\n"}, headers=_auth(alice["user_token"]))
    client.post(f"/api/shells/{session_id}/input", json={"data": "python3\n"}, headers=_auth(alice["user_token"]))
    time.sleep(0.5)
    client.post(f"/api/shells/{session_id}/input", json={"data": "print('repl-ok')\n"}, headers=_auth(alice["user_token"]))
    client.post(f"/api/shells/{session_id}/input", json={"data": "exit()\nexit\n"}, headers=_auth(alice["user_token"]))

    shell_thread.join(timeout=20)
    assert not shell_thread.is_alive()

    events = store.get_shell_events(session_id, 0)
    stdout = "".join(event.payload.get("data", "") for event in events if event.kind == "shell.stdout")
    assert "40 100" in stdout
    assert ">>>" in stdout
    assert "repl-ok" in stdout


def test_expired_token_is_rejected_for_business_api(tmp_path):
    client, store = _build_client(tmp_path)
    alice = _connect(client, "alice")

    with store._lock, store._conn:
        store._conn.execute(
            "UPDATE user_tokens SET expires_at = ?",
            ((utc_now() - timedelta(seconds=1)).isoformat(),),
        )

    response = client.post(
        "/api/commands",
        json={
            "agent_id": "agent-a",
            "argv": [sys.executable, "-c", "print('x')"],
            "working_dir": ".",
            "timeout_sec": 30,
            "env_patch": {},
        },
        headers=_auth(alice["user_token"]),
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "token expired"


def _drain(items: list):
    drained = list(items)
    items.clear()
    return drained
