from __future__ import annotations

import logging
import sys
import threading
import time

from fastapi.testclient import TestClient

from mvp_orbit.agent.runtime import AgentRuntime
from mvp_orbit.agent.service import AgentService
from mvp_orbit.cli.package import build_file_package
from mvp_orbit.hub.app import create_app
from mvp_orbit.hub.store import HubStore


def _build_client(tmp_path):
    store = HubStore(tmp_path / "hub.sqlite3", tmp_path / "objects")
    app = create_app(store=store, api_token="api-token")
    return TestClient(app)


def test_end_to_end_command_with_package(tmp_path):
    client = _build_client(tmp_path)

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "payload.txt").write_text("hello-orbit", encoding="utf-8")
    package_build = build_file_package(source_dir)
    package = client.post(
        "/api/packages",
        content=package_build.archive_path.read_bytes(),
        headers={"Authorization": "Bearer api-token", "Content-Type": "application/gzip"},
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
        headers={"Authorization": "Bearer api-token"},
    )
    command_id = created.json()["command_id"]

    runtime = AgentRuntime(agent_id="agent-a", base_workspace=tmp_path / "workspace", heartbeat_interval_sec=0.01)
    service = AgentService(agent_id="agent-a", hub_url=str(client.base_url), runtime=runtime, api_token="api-token")
    assert service.poll_once(client=client) == "succeeded"

    status = client.get(f"/api/commands/{command_id}", headers={"Authorization": "Bearer api-token"}).json()
    output = client.get(f"/api/commands/{command_id}/output", headers={"Authorization": "Bearer api-token"}).json()
    assert status["status"] == "succeeded"
    assert "hello-orbit" in output["stdout"]


def test_agent_logs_package_download_and_command_execution(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    client = _build_client(tmp_path)

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "payload.txt").write_text("hello-orbit", encoding="utf-8")
    package_build = build_file_package(source_dir)
    package = client.post(
        "/api/packages",
        content=package_build.archive_path.read_bytes(),
        headers={"Authorization": "Bearer api-token", "Content-Type": "application/gzip"},
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
        headers={"Authorization": "Bearer api-token"},
    )
    command_id = created.json()["command_id"]

    runtime = AgentRuntime(agent_id="agent-a", base_workspace=tmp_path / "workspace", heartbeat_interval_sec=0.01)
    service = AgentService(agent_id="agent-a", hub_url=str(client.base_url), runtime=runtime, api_token="api-token")
    assert service.poll_once(client=client) == "succeeded"

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert f"leased command {command_id}" in messages
    assert f"requesting package {package_id}" in messages
    assert f"downloading package {package_id}" in messages
    assert f"prepared package {package_id}" in messages
    assert f"starting command {command_id}" in messages
    assert f"finished command {command_id} status=succeeded exit_code=0" in messages


def test_end_to_end_command_without_package_uses_base_workspace(tmp_path):
    client = _build_client(tmp_path)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("base-workspace", encoding="utf-8")

    created = client.post(
        "/api/commands",
        json={
            "agent_id": "agent-a",
            "argv": [sys.executable, "-c", "from pathlib import Path; print(Path('hello.txt').read_text())"],
            "working_dir": ".",
            "timeout_sec": 30,
            "env_patch": {},
        },
        headers={"Authorization": "Bearer api-token"},
    )
    command_id = created.json()["command_id"]

    runtime = AgentRuntime(agent_id="agent-a", base_workspace=workspace, heartbeat_interval_sec=0.01)
    service = AgentService(agent_id="agent-a", hub_url=str(client.base_url), runtime=runtime, api_token="api-token")
    assert service.poll_once(client=client) == "succeeded"

    output = client.get(f"/api/commands/{command_id}/output", headers={"Authorization": "Bearer api-token"}).json()
    assert "base-workspace" in output["stdout"]


def test_agent_logs_shell_session_inputs(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    client = _build_client(tmp_path)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = AgentRuntime(agent_id="agent-a", base_workspace=workspace, heartbeat_interval_sec=0.01)
    service = AgentService(agent_id="agent-a", hub_url=str(client.base_url), runtime=runtime, api_token="api-token")

    created = client.post(
        "/api/shells",
        json={"agent_id": "agent-a"},
        headers={"Authorization": "Bearer api-token"},
    )
    session_id = created.json()["session_id"]

    def run_shell():
        assert service.poll_once(client=client) in {"closed", "failed"}

    thread = threading.Thread(target=run_shell)
    thread.start()
    time.sleep(0.5)

    client.post(
        f"/api/shells/{session_id}/input",
        json={"data": "printf 'hello-shell\\n'\n"},
        headers={"Authorization": "Bearer api-token"},
    )
    client.post(
        f"/api/shells/{session_id}/input",
        json={"data": "exit\n"},
        headers={"Authorization": "Bearer api-token"},
    )
    thread.join(timeout=10)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert f"leased shell session {session_id}" in messages
    assert f"starting shell session {session_id}" in messages
    assert "hello-shell" in messages
    assert f"finished shell session {session_id}" in messages or f"closed shell session {session_id}" in messages


def test_shell_session_uses_base_workspace_and_emits_output(tmp_path):
    client = _build_client(tmp_path)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = AgentRuntime(agent_id="agent-a", base_workspace=workspace, heartbeat_interval_sec=0.01)
    service = AgentService(agent_id="agent-a", hub_url=str(client.base_url), runtime=runtime, api_token="api-token")

    created = client.post(
        "/api/shells",
        json={"agent_id": "agent-a"},
        headers={"Authorization": "Bearer api-token"},
    )
    session_id = created.json()["session_id"]

    def run_shell():
        assert service.poll_once(client=client) in {"closed", "failed"}

    thread = threading.Thread(target=run_shell)
    thread.start()
    time.sleep(0.5)

    client.post(
        f"/api/shells/{session_id}/input",
        json={"data": "printf 'hello-shell\\n'\n"},
        headers={"Authorization": "Bearer api-token"},
    )
    client.post(
        f"/api/shells/{session_id}/input",
        json={"data": "exit\n"},
        headers={"Authorization": "Bearer api-token"},
    )
    thread.join(timeout=10)

    events = client.get(
        f"/api/shells/{session_id}/events",
        params={"after_seq": 0},
        headers={"Authorization": "Bearer api-token"},
    ).json()
    assert any("hello-shell" in event["data"] for event in events["events"])
