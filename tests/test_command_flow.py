from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import timedelta

import httpx
from fastapi.testclient import TestClient

from mvp_orbit.agent.runtime import AgentRuntime
from mvp_orbit.agent.service import AgentService, TokenExpiredError
from mvp_orbit.cli.package import build_file_package
from mvp_orbit.core.models import CommandLease, CommandStatus, utc_now
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


def _register_agent(client: TestClient, tmp_path, agent_id: str, user_token: str) -> AgentService:
    runtime = AgentRuntime(agent_id=agent_id, base_workspace=tmp_path / agent_id, heartbeat_interval_sec=0.01)
    service = AgentService(agent_id=agent_id, hub_url=str(client.base_url), runtime=runtime, user_token=user_token)
    assert service.poll_once(client=client) == "idle"
    return service


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
    assert "Own Agents." in response.text
    assert "GitHub Repository" in response.text


def test_end_to_end_command_with_package(tmp_path):
    client, _ = _build_client(tmp_path)
    alice = _connect(client, "alice")
    service = _register_agent(client, tmp_path, "agent-a", alice["user_token"])

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
            "argv": [sys.executable, "-c", "from pathlib import Path; print(Path('payload.txt').read_text())"],
            "working_dir": ".",
            "timeout_sec": 30,
            "env_patch": {},
        },
        headers=_auth(alice["user_token"]),
    )
    command_id = created.json()["command_id"]

    assert service.poll_once(client=client) == "succeeded"

    status = client.get(f"/api/commands/{command_id}", headers=_auth(alice["user_token"])).json()
    output = client.get(f"/api/commands/{command_id}/output", headers=_auth(alice["user_token"])).json()
    assert status["status"] == "succeeded"
    assert status["owner_user_id"] == "alice"
    assert "hello-orbit" in output["stdout"]


def test_end_to_end_command_without_package_uses_base_workspace(tmp_path):
    client, _ = _build_client(tmp_path)
    alice = _connect(client, "alice")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("base-workspace", encoding="utf-8")
    runtime = AgentRuntime(agent_id="agent-a", base_workspace=workspace, heartbeat_interval_sec=0.01)
    service = AgentService(agent_id="agent-a", hub_url=str(client.base_url), runtime=runtime, user_token=alice["user_token"])
    assert service.poll_once(client=client) == "idle"

    created = client.post(
        "/api/commands",
        json={
            "agent_id": "agent-a",
            "argv": [sys.executable, "-c", "from pathlib import Path; print(Path('hello.txt').read_text())"],
            "working_dir": ".",
            "timeout_sec": 30,
            "env_patch": {},
        },
        headers=_auth(alice["user_token"]),
    )
    command_id = created.json()["command_id"]

    assert service.poll_once(client=client) == "succeeded"

    output = client.get(f"/api/commands/{command_id}/output", headers=_auth(alice["user_token"])).json()
    assert "base-workspace" in output["stdout"]


def test_owner_can_reconnect_and_keep_control_of_existing_agent(tmp_path):
    client, _ = _build_client(tmp_path)
    first = _connect(client, "alice")
    second = _connect(client, "alice")

    _register_agent(client, tmp_path, "agent-a", first["user_token"])

    created = client.post(
        "/api/commands",
        json={
            "agent_id": "agent-a",
            "argv": [sys.executable, "-c", "print('hello-reconnect')"],
            "working_dir": ".",
            "timeout_sec": 30,
            "env_patch": {},
        },
        headers=_auth(second["user_token"]),
    )
    assert created.status_code == 200


def test_other_user_cannot_submit_or_read_commands_for_foreign_agent(tmp_path):
    client, _ = _build_client(tmp_path)
    alice = _connect(client, "alice")
    bob = _connect(client, "bob")
    service = _register_agent(client, tmp_path, "agent-a", alice["user_token"])

    forbidden_create = client.post(
        "/api/commands",
        json={
            "agent_id": "agent-a",
            "argv": [sys.executable, "-c", "print('nope')"],
            "working_dir": ".",
            "timeout_sec": 30,
            "env_patch": {},
        },
        headers=_auth(bob["user_token"]),
    )
    assert forbidden_create.status_code == 403

    created = client.post(
        "/api/commands",
        json={
            "agent_id": "agent-a",
            "argv": [sys.executable, "-c", "print('hello-owner')"],
            "working_dir": ".",
            "timeout_sec": 30,
            "env_patch": {},
        },
        headers=_auth(alice["user_token"]),
    )
    command_id = created.json()["command_id"]

    assert client.get(f"/api/commands/{command_id}", headers=_auth(bob["user_token"])).status_code == 403
    assert client.get(f"/api/commands/{command_id}/output", headers=_auth(bob["user_token"])).status_code == 403
    assert client.post(f"/api/commands/{command_id}/cancel", headers=_auth(bob["user_token"])).status_code == 403
    assert service.poll_once(client=client) == "succeeded"


def test_package_access_is_isolated_per_user_even_for_same_package_id(tmp_path):
    client, _ = _build_client(tmp_path)
    alice = _connect(client, "alice")
    bob = _connect(client, "bob")

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "payload.txt").write_text("hello-orbit", encoding="utf-8")
    package_build = build_file_package(source_dir)
    payload = package_build.archive_path.read_bytes()

    alice_upload = client.post(
        "/api/packages",
        content=payload,
        headers=_auth(alice["user_token"], content_type="application/gzip"),
    )
    package_id = alice_upload.json()["package_id"]

    assert client.get(f"/api/packages/{package_id}", headers=_auth(bob["user_token"])).status_code == 403

    bob_upload = client.post(
        "/api/packages",
        content=payload,
        headers=_auth(bob["user_token"], content_type="application/gzip"),
    )
    assert bob_upload.json()["package_id"] == package_id
    assert client.get(f"/api/packages/{package_id}", headers=_auth(bob["user_token"])).status_code == 200


def test_agent_logs_package_download_and_command_execution(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    client, _ = _build_client(tmp_path)
    alice = _connect(client, "alice")
    service = _register_agent(client, tmp_path, "agent-a", alice["user_token"])

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
            "argv": [sys.executable, "-c", "print('hello-log')"],
            "working_dir": ".",
            "timeout_sec": 30,
            "env_patch": {},
        },
        headers=_auth(alice["user_token"]),
    )
    command_id = created.json()["command_id"]

    assert service.poll_once(client=client) == "succeeded"

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert f"leased command {command_id}" in messages
    assert f"requesting package {package_id}" in messages
    assert f"downloading package {package_id}" in messages
    assert f"prepared package {package_id}" in messages
    assert f"starting command {command_id}" in messages
    assert f"finished command {command_id} status=succeeded exit_code=0" in messages


def test_command_output_post_403_does_not_break_command(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    client, _ = _build_client(tmp_path)
    alice = _connect(client, "alice")
    service = _register_agent(client, tmp_path, "agent-a", alice["user_token"])

    created = client.post(
        "/api/commands",
        json={
            "agent_id": "agent-a",
            "argv": [sys.executable, "-c", "print('hello-output')"],
            "working_dir": ".",
            "timeout_sec": 30,
            "env_patch": {},
        },
        headers=_auth(alice["user_token"]),
    )
    command_id = created.json()["command_id"]

    original_post = client.post

    def flaky_post(url, *args, **kwargs):
        if str(url).endswith(f"/api/commands/{command_id}/output"):
            request = httpx.Request("POST", str(url))
            return httpx.Response(status_code=403, request=request)
        return original_post(url, *args, **kwargs)

    client.post = flaky_post
    try:
        assert service.poll_once(client=client) == "succeeded"
    finally:
        client.post = original_post

    status = client.get(f"/api/commands/{command_id}", headers=_auth(alice["user_token"])).json()
    output = client.get(f"/api/commands/{command_id}/output", headers=_auth(alice["user_token"])).json()
    assert status["status"] == "succeeded"
    assert output["stdout"] == ""
    assert f"failed to append command {command_id} stdout output" in "\n".join(record.getMessage() for record in caplog.records)


def test_command_output_is_chunked_before_append(tmp_path):
    runtime = AgentRuntime(
        agent_id="agent-a",
        base_workspace=tmp_path / "workspace",
        heartbeat_interval_sec=0.01,
        command_output_chunk_bytes=1_000_000,
        command_output_flush_interval_sec=60.0,
    )
    lease = CommandLease(
        command_id="cmd-test",
        agent_id="agent-a",
        package_id=None,
        argv=[sys.executable, "-c", "print('a'); print('b'); print('c')"],
        env_patch={},
        timeout_sec=30,
        working_dir=".",
    )
    appended: list[tuple[str, str]] = []

    outcome = runtime.handle_command(
        lease,
        fetch_package=lambda _: b"",
        append_output=lambda stream, data: appended.append((stream, data)),
        heartbeat=lambda: False,
    )

    assert outcome.status == CommandStatus.SUCCEEDED
    assert [(stream, data) for stream, data in appended if stream == "stdout"] == [("stdout", "a\nb\nc\n")]


def test_shell_session_access_isolated_by_owner(tmp_path):
    client, _ = _build_client(tmp_path)
    alice = _connect(client, "alice")
    bob = _connect(client, "bob")
    service = _register_agent(client, tmp_path, "agent-a", alice["user_token"])

    created = client.post(
        "/api/shells",
        json={"agent_id": "agent-a"},
        headers=_auth(alice["user_token"]),
    )
    session_id = created.json()["session_id"]

    def run_shell():
        assert service.poll_once(client=client) in {"closed", "failed"}

    thread = threading.Thread(target=run_shell)
    thread.start()
    time.sleep(0.5)

    assert client.get(f"/api/shells/{session_id}", headers=_auth(bob["user_token"])).status_code == 403
    assert client.post(f"/api/shells/{session_id}/input", json={"data": "echo nope\n"}, headers=_auth(bob["user_token"])).status_code == 403
    assert client.get(f"/api/shells/{session_id}/events", headers=_auth(bob["user_token"])).status_code == 403
    assert client.post(f"/api/shells/{session_id}/close", headers=_auth(bob["user_token"])).status_code == 403

    client.post(
        f"/api/shells/{session_id}/input",
        json={"data": "printf 'hello-shell\\n'\n"},
        headers=_auth(alice["user_token"]),
    )
    client.post(
        f"/api/shells/{session_id}/input",
        json={"data": "exit\n"},
        headers=_auth(alice["user_token"]),
    )
    thread.join(timeout=10)

    events = client.get(
        f"/api/shells/{session_id}/events",
        params={"after_seq": 0},
        headers=_auth(alice["user_token"]),
    ).json()
    assert any("hello-shell" in event["data"] for event in events["events"])


def test_list_shell_sessions_only_returns_owner_sessions(tmp_path):
    client, _ = _build_client(tmp_path)
    alice = _connect(client, "alice")
    bob = _connect(client, "bob")
    _register_agent(client, tmp_path, "agent-a", alice["user_token"])
    _register_agent(client, tmp_path, "agent-b", bob["user_token"])

    first = client.post("/api/shells", json={"agent_id": "agent-a"}, headers=_auth(alice["user_token"])).json()
    second = client.post("/api/shells", json={"agent_id": "agent-b"}, headers=_auth(bob["user_token"])).json()

    listed = client.get("/api/shells", headers=_auth(alice["user_token"])).json()
    assert [item["session_id"] for item in listed] == [first["session_id"]]
    assert second["session_id"] not in {item["session_id"] for item in listed}


def test_expired_token_is_rejected_for_business_api_and_agent_poll(tmp_path):
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

    runtime = AgentRuntime(agent_id="agent-a", base_workspace=tmp_path / "workspace", heartbeat_interval_sec=0.01)
    service = AgentService(agent_id="agent-a", hub_url=str(client.base_url), runtime=runtime, user_token=alice["user_token"])
    try:
        service.poll_once(client=client)
        raise AssertionError("expected token expiry")
    except TokenExpiredError:
        pass
