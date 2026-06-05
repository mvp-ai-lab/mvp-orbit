from __future__ import annotations

import base64
import sys
import threading
import time
from datetime import timedelta

from fastapi.testclient import TestClient

from mvp_orbit.client.runtime import ClientRuntime
from mvp_orbit.client.service import ClientService
from mvp_orbit.core.models import ClientEvent, ClientEventsRequest, CommandLease, ShellSessionLease, utc_now
from mvp_orbit.hub.app import create_app
from mvp_orbit.hub.store import HubStore

def _build_client(tmp_path):
    store = HubStore(tmp_path / "hub.sqlite3", tmp_path / "objects")
    app = create_app(store=store)
    return TestClient(app), store


def _join(client: TestClient, alias: str, channel: str = "test-channel", approver_token: str | None = None) -> dict:
    response = client.post("/api/join", json={"alias": alias, "channel": channel})
    assert response.status_code == 200
    payload = response.json()
    if payload["status"] == "pending" and approver_token is not None:
        approved = client.post(f"/api/join-requests/{payload['request_id']}/approve", headers=_auth(approver_token))
        assert approved.status_code == 200
        completed = client.get(f"/api/join-requests/{payload['request_id']}")
        assert completed.status_code == 200
        payload = completed.json()
    assert payload["status"] == "approved"
    assert payload["member_token"]
    return payload



def _auth(member_token: str, *, content_type: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {member_token}"}
    if content_type is not None:
        headers["Content-Type"] = content_type
    return headers


def _post_client_events(client: TestClient, member_token: str, client_id: str, events: list[ClientEvent]) -> None:
    response = client.post(
        f"/api/clients/{client_id}/events",
        json=ClientEventsRequest(events=events).model_dump(mode="json"),
        headers=_auth(member_token),
    )
    assert response.status_code == 200


def _run_command_runtime(
    client: TestClient,
    member_token: str,
    tmp_path,
    command_id: str,
    *,
    workspace=None,
    client_id: str = "client-a",
) -> None:
    runtime = ClientRuntime(client_id=client_id, base_workspace=workspace or (tmp_path / "workspace"))
    claimed = client.post(f"/api/commands/{command_id}/claim", headers=_auth(member_token))
    assert claimed.status_code == 200
    lease = CommandLease.model_validate(claimed.json())

    def emit(kind: str, payload: dict) -> None:
        _post_client_events(client, member_token, client_id, [ClientEvent(kind=kind, payload=payload)])

    outcome = runtime.handle_command(
        lease=lease,  # type: ignore[arg-type]
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


def test_join_requires_channel_name(tmp_path):
    client, _ = _build_client(tmp_path)
    response = client.post("/api/join", json={"alias": "client-a"})
    assert response.status_code == 422


def test_root_serves_landing_page(tmp_path):
    client, _ = _build_client(tmp_path)
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "mvp-orbit host" in response.text


def test_command_creation_emits_client_control_event(tmp_path):
    client, store = _build_client(tmp_path)
    alice = _join(client, "client-a")
    store.register_client("client-a", alice["channel_id"])

    created = client.post(
        "/api/commands",
        json={
            "client_id": "client-a",
            "argv": [sys.executable, "-c", "print('hello', flush=True)"],
            "working_dir": ".",
            "timeout_sec": 30,
            "env_patch": {},
        },
        headers=_auth(alice["member_token"]),
    )
    command_id = created.json()["command_id"]
    events = store.get_client_control_events("client-a", 0)
    assert events[-1].kind == "command.start"
    assert events[-1].payload["command_id"] == command_id


def test_end_to_end_command_stream_in_base_workspace(tmp_path):
    client, store = _build_client(tmp_path)
    alice = _join(client, "client-a")
    store.register_client("client-a", alice["channel_id"])

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "payload.txt").write_text("hello-orbit", encoding="utf-8")

    created = client.post(
        "/api/commands",
        json={
            "client_id": "client-a",
            "argv": [sys.executable, "-c", "from pathlib import Path; print(Path('payload.txt').read_text(), flush=True)"],
            "working_dir": ".",
            "timeout_sec": 30,
            "env_patch": {},
        },
        headers=_auth(alice["member_token"]),
    )
    command_id = created.json()["command_id"]

    _run_command_runtime(client, alice["member_token"], tmp_path, command_id, workspace=workspace)

    events = store.get_command_events(command_id, 0)
    stdout = "".join(event.payload.get("data", "") for event in events if event.kind == "command.stdout")
    assert "hello-orbit" in stdout
    assert events[-1].kind == "command.exit"

    status = client.get(f"/api/commands/{command_id}", headers=_auth(alice["member_token"])).json()
    assert status["status"] == "succeeded"


def test_cancel_queued_command_emits_terminal_stream_event(tmp_path):
    client, store = _build_client(tmp_path)
    alice = _join(client, "client-a")
    store.register_client("client-a", alice["channel_id"])

    created = client.post(
        "/api/commands",
        json={
            "client_id": "client-a",
            "argv": [sys.executable, "-c", "print('ignored')"],
            "working_dir": ".",
            "timeout_sec": 30,
            "env_patch": {},
        },
        headers=_auth(alice["member_token"]),
    )
    command_id = created.json()["command_id"]
    canceled = client.post(f"/api/commands/{command_id}/cancel", headers=_auth(alice["member_token"]))
    assert canceled.status_code == 200

    events = store.get_command_events(command_id, 0)
    assert events[-1].kind == "command.exit"
    assert events[-1].payload["status"] == "canceled"


def test_shell_resize_and_repl_output_flow(tmp_path):
    client, store = _build_client(tmp_path)
    alice = _join(client, "client-a")
    store.register_client("client-a", alice["channel_id"])
    runtime = ClientRuntime(client_id="client-a", base_workspace=tmp_path / "workspace")

    created = client.post("/api/shells", json={"client_id": "client-a"}, headers=_auth(alice["member_token"]))
    session_id = created.json()["session_id"]
    claimed = client.post(f"/api/shells/{session_id}/claim", headers=_auth(alice["member_token"]))
    assert claimed.status_code == 200
    lease = ShellSessionLease.model_validate(claimed.json())

    input_queue: list[bytes] = []
    resize_queue: list[tuple[int, int]] = []
    close_requested = threading.Event()

    def pump_controls() -> None:
        after = 0
        while not close_requested.is_set():
            for event in store.get_client_control_events("client-a", after):
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
        _post_client_events(client, alice["member_token"], "client-a", [ClientEvent(kind=kind, payload=payload)])

    def run_shell() -> None:
        outcome = runtime.handle_shell_session(
            lease=lease,
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

    client.post(f"/api/shells/{session_id}/resize", json={"rows": 40, "cols": 100}, headers=_auth(alice["member_token"]))
    client.post(f"/api/shells/{session_id}/input", json={"data": "stty size\n"}, headers=_auth(alice["member_token"]))
    client.post(f"/api/shells/{session_id}/input", json={"data": "python3\n"}, headers=_auth(alice["member_token"]))
    time.sleep(0.5)
    client.post(f"/api/shells/{session_id}/input", json={"data": "print('repl-ok')\n"}, headers=_auth(alice["member_token"]))
    client.post(f"/api/shells/{session_id}/input", json={"data": "exit()\nexit\n"}, headers=_auth(alice["member_token"]))

    shell_thread.join(timeout=20)
    assert not shell_thread.is_alive()

    events = store.get_shell_events(session_id, 0)
    stdout = "".join(event.payload.get("data", "") for event in events if event.kind == "shell.stdout")
    assert "40 100" in stdout
    assert ">>>" in stdout
    assert "repl-ok" in stdout


def test_expired_token_is_rejected_for_business_api(tmp_path):
    client, store = _build_client(tmp_path)
    alice = _join(client, "client-a")

    with store._lock, store._conn:
        store._conn.execute(
            "UPDATE member_tokens SET expires_at = ?",
            ((utc_now() - timedelta(seconds=1)).isoformat(),),
        )

    response = client.post(
        "/api/commands",
        json={
            "client_id": "client-a",
            "argv": [sys.executable, "-c", "print('x')"],
            "working_dir": ".",
            "timeout_sec": 30,
            "env_patch": {},
        },
        headers=_auth(alice["member_token"]),
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "token expired"


def _drain(items: list):
    drained = list(items)
    items.clear()
    return drained



def test_client_foreground_prompt_can_approve_join_request(tmp_path):
    client, store = _build_client(tmp_path)
    alice = _join(client, "client-a")
    store.register_client("client-a", alice["channel_id"])

    requested = client.post("/api/join", json={"alias": "client-b", "channel": "test-channel"})
    assert requested.status_code == 200
    pending = requested.json()
    assert pending["status"] == "pending"

    events = store.get_client_control_events("client-a", 0)
    join_event = next(event for event in events if event.kind == "join.request")
    service = ClientService(
        client_id="client-a",
        hub_url="",
        runtime=ClientRuntime(client_id="client-a", base_workspace=tmp_path / "client-a"),
        member_token=alice["member_token"],
        join_request_prompt=lambda payload: True,
    )
    service._dispatch_event(client, join_event.kind, join_event.payload)

    completed = client.get(f"/api/join-requests/{pending['request_id']}")
    assert completed.status_code == 200
    payload = completed.json()
    assert payload["status"] == "approved"
    assert payload["member_token"]

def test_channel_join_requires_existing_member_approval_after_first_member(tmp_path):
    client, _ = _build_client(tmp_path)
    alice = _join(client, "client-a")

    requested = client.post("/api/join", json={"alias": "client-b", "channel": "test-channel"})
    assert requested.status_code == 200
    pending = requested.json()
    assert pending["status"] == "pending"
    assert pending["member_token"] is None

    listed = client.get("/api/join-requests", headers=_auth(alice["member_token"]))
    assert listed.status_code == 200
    assert listed.json()[0]["request_id"] == pending["request_id"]

    approved = client.post(f"/api/join-requests/{pending['request_id']}/approve", headers=_auth(alice["member_token"]))
    assert approved.status_code == 200
    completed = client.get(f"/api/join-requests/{pending['request_id']}")
    assert completed.status_code == 200
    payload = completed.json()
    assert payload["status"] == "approved"
    assert payload["member_token"]


def test_channel_join_allows_peer_to_command_another_client(tmp_path):
    client, store = _build_client(tmp_path)
    alice = _join(client, "client-a")
    bob = _join(client, "client-b", approver_token=alice["member_token"])
    assert alice["channel_id"] == bob["channel_id"]
    assert alice["channel_id"] == bob["channel_id"]
    store.register_client("client-a", alice["channel_id"])
    store.register_client("client-b", bob["channel_id"])

    peers = client.get("/api/peers", headers=_auth(alice["member_token"]))
    assert peers.status_code == 200
    assert {item["client_id"] for item in peers.json()} == {"client-a", "client-b"}

    created = client.post(
        "/api/commands",
        json={
            "client_id": "client-b",
            "argv": [sys.executable, "-c", "print('from-b', flush=True)"],
            "working_dir": ".",
            "timeout_sec": 30,
            "env_patch": {},
        },
        headers=_auth(alice["member_token"]),
    )
    assert created.status_code == 200
    command_id = created.json()["command_id"]
    _run_command_runtime(client, bob["member_token"], tmp_path, command_id, client_id="client-b")

    output = client.get(f"/api/commands/{command_id}/output", headers=_auth(alice["member_token"])).json()
    assert "from-b" in output["stdout"]
    assert output["status"] == "succeeded"


def test_channel_file_push_and_pull(tmp_path):
    client, store = _build_client(tmp_path)
    alice = _join(client, "client-a")
    bob = _join(client, "client-b", approver_token=alice["member_token"])
    store.register_client("client-b", bob["channel_id"])
    runtime = ClientRuntime(client_id="client-b", base_workspace=tmp_path / "client-b")

    payload = base64.b64encode(b"hello-file").decode("ascii")
    pushed = client.post(
        "/api/files/push",
        json={"client_id": "client-b", "remote_path": "inbox/hello.txt", "data_b64": payload, "max_bytes": 1024},
        headers=_auth(alice["member_token"]),
    )
    assert pushed.status_code == 200
    push_id = pushed.json()["transfer_id"]
    push_event = store.get_client_control_events("client-b", 0)[-1]
    assert push_event.kind == "file.push"
    _post_client_events(client, bob["member_token"], "client-b", [ClientEvent(kind="file.started", payload={"transfer_id": push_id})])
    push_result = runtime.handle_file_push(
        transfer_id=push_id,
        remote_path=push_event.payload["remote_path"],
        data_b64=push_event.payload["data_b64"],
        max_bytes=push_event.payload["max_bytes"],
    )
    _post_client_events(client, bob["member_token"], "client-b", [ClientEvent(kind="file.result", payload=push_result.model_dump(mode="json"))])
    assert (tmp_path / "client-b" / "inbox" / "hello.txt").read_text(encoding="utf-8") == "hello-file"
    assert client.get(f"/api/files/{push_id}", headers=_auth(alice["member_token"])).json()["status"] == "succeeded"

    pulled = client.post(
        "/api/files/pull",
        json={"client_id": "client-b", "remote_path": "inbox/hello.txt", "max_bytes": 1024},
        headers=_auth(alice["member_token"]),
    )
    assert pulled.status_code == 200
    pull_id = pulled.json()["transfer_id"]
    pull_event = store.get_client_control_events("client-b", push_event.event_id)[-1]
    assert pull_event.kind == "file.pull"
    _post_client_events(client, bob["member_token"], "client-b", [ClientEvent(kind="file.started", payload={"transfer_id": pull_id})])
    pull_result = runtime.handle_file_pull(
        transfer_id=pull_id,
        remote_path=pull_event.payload["remote_path"],
        max_bytes=pull_event.payload["max_bytes"],
    )
    _post_client_events(client, bob["member_token"], "client-b", [ClientEvent(kind="file.result", payload=pull_result.model_dump(mode="json"))])
    transfer = client.get(f"/api/files/{pull_id}", headers=_auth(alice["member_token"])).json()
    assert transfer["status"] == "succeeded"
    assert base64.b64decode(transfer["data_b64"]) == b"hello-file"


def test_file_push_rejects_payload_over_limit(tmp_path):
    client, store = _build_client(tmp_path)
    alice = _join(client, "client-a")
    store.register_client("client-b", alice["channel_id"])
    payload = base64.b64encode(b"too-large").decode("ascii")
    response = client.post(
        "/api/files/push",
        json={"client_id": "client-b", "remote_path": "x.txt", "data_b64": payload, "max_bytes": 3},
        headers=_auth(alice["member_token"]),
    )
    assert response.status_code == 413


def test_cleanup_empty_channel_keeps_online_clients_and_prunes_empty_channels(tmp_path):
    client, store = _build_client(tmp_path)
    alice = _join(client, "client-a", channel="active")
    store.register_client("client-a", alice["channel_id"])
    kept = store.cleanup_empty_channels(offline_after_sec=3600, empty_ttl_sec=0)
    assert alice["channel_id"] not in kept
    assert client.get("/api/peers", headers=_auth(alice["member_token"])).status_code == 200

    stale = _join(client, "stale-a", channel="stale")
    pending = client.post("/api/join", json={"alias": "stale-b", "channel": "stale"}).json()
    assert pending["status"] == "pending"
    pruned = store.cleanup_empty_channels(offline_after_sec=1, empty_ttl_sec=0)
    assert stale["channel_id"] in pruned

    assert client.get("/api/peers", headers=_auth(stale["member_token"])).status_code == 401
    recreated = client.post("/api/join", json={"alias": "stale-c", "channel": "stale"})
    assert recreated.status_code == 200
    assert recreated.json()["status"] == "approved"
