from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from mvp_orbit.agent.runtime import AgentRuntime
from mvp_orbit.agent.service import AgentService
from mvp_orbit.cli.package import build_file_package
from mvp_orbit.core.canonical import object_id_for_json
from mvp_orbit.core.models import CommandObject, RunCompletionRequest, RunCreateRequest, RunLease, SignedTaskObject, TaskObject, utc_now
from mvp_orbit.core.signing import generate_keypair_b64, sign_payload
from mvp_orbit.core.tickets import ReplayGuard, RunTicketManager
from mvp_orbit.hub.app import create_app
from mvp_orbit.hub.store import RunStore
from mvp_orbit.integrations.object_store import ObjectStore
from tests.helpers import InMemoryObjectStoreBackend


def _build_store() -> ObjectStore:
    return ObjectStore(InMemoryObjectStoreBackend())


def _signed_task(package_id: str, command_id: str, private_key: str) -> SignedTaskObject:
    task = TaskObject(
        package_id=package_id,
        command_id=command_id,
        created_by="tester",
        created_at=utc_now(),
    )
    payload = task.model_dump(mode="json", exclude_none=True)
    task_id = object_id_for_json(payload)
    return SignedTaskObject(
        task_id=task_id,
        task=task,
        task_signature=sign_payload(payload, private_key),
        signer="test",
    )


def test_end_to_end_run_flow_uses_ids_only(tmp_path):
    store = _build_store()
    private_key, public_key = generate_keypair_b64()

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "payload.txt").write_text("hello-orbit", encoding="utf-8")
    package_build = build_file_package(source_dir)
    package_id = store.put_package(package_build.archive_path.read_bytes())

    command = CommandObject(
        argv=[sys.executable, "-c", "from pathlib import Path; print(Path('payload.txt').read_text(encoding='utf-8'))"],
        working_dir=".",
    )
    command_id = store.put_command(command)
    signed_task = _signed_task(package_id, command_id, private_key)
    task_id = store.put_signed_task(signed_task)

    app = create_app(
        run_store=RunStore(tmp_path / "runs.sqlite3"),
        ticket_manager=RunTicketManager("t" * 32),
        api_token="api-token",
    )
    client = TestClient(app)
    submit = client.post(
        "/api/runs",
        json=RunCreateRequest(agent_id="agent-a", task_id=task_id).model_dump(mode="json"),
        headers={"Authorization": "Bearer api-token"},
    )
    assert submit.status_code == 200
    run_payload = submit.json()
    run_id = run_payload["run_id"]

    next_resp = client.get("/api/agents/agent-a/next", headers={"Authorization": "Bearer api-token"})
    lease_payload = next_resp.json()
    assert set(lease_payload) == {"run_id", "agent_id", "task_id", "run_ticket", "expires_at"}

    runtime = AgentRuntime(
        agent_id="agent-a",
        ticket_manager=RunTicketManager("t" * 32),
        replay_guard=ReplayGuard(),
        object_store=store,
        verify_public_key_b64=public_key,
        workspace_root=tmp_path / "workspaces",
        heartbeat_interval_sec=0.01,
    )
    lease = RunLease.model_validate(lease_payload)
    outcome = runtime.handle_run(lease)
    complete = client.post(
        f"/api/runs/{run_id}/complete",
        json=RunCompletionRequest(
            status=outcome.status,
            log_ids=outcome.log_ids,
            result_id=outcome.result_id,
            artifact_ids=outcome.artifact_ids,
            failure_code=outcome.failure_code,
        ).model_dump(mode="json"),
        headers={"Authorization": "Bearer api-token"},
    )
    assert complete.status_code == 200

    record_resp = client.get(f"/api/runs/{run_id}", headers={"Authorization": "Bearer api-token"})
    assert record_resp.status_code == 200
    record = record_resp.json()
    assert record["status"] == "succeeded"
    assert record["result_id"]
    assert record["log_ids"]

    result = store.get_result(record["result_id"])
    assert result.exit_code == 0
    logs = [store.get_log(log_id) for log_id in record["log_ids"]]
    assert any("hello-orbit" in log.data for log in logs)


def test_agent_rejects_tampered_task_id(tmp_path):
    store = _build_store()
    private_key, public_key = generate_keypair_b64()

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "payload.txt").write_text("safe", encoding="utf-8")
    package_build = build_file_package(source_dir)
    package_id = store.put_package(package_build.archive_path.read_bytes())

    command = CommandObject(argv=[sys.executable, "-c", "print('ignored')"], working_dir=".")
    command_id = store.put_command(command)
    signed_task = _signed_task(package_id, command_id, private_key)
    task_id = store.put_signed_task(signed_task)
    other_task_id = "sha256-" + ("0" * 64)

    ticket_mgr = RunTicketManager("s" * 32)
    run_ticket, _ = ticket_mgr.issue(
        run_id="run-1",
        agent_id="agent-a",
        task_id=other_task_id,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=120),
    )
    lease = RunLease(
        run_id="run-1",
        agent_id="agent-a",
        task_id=task_id,
        run_ticket=run_ticket,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=120),
    )

    runtime = AgentRuntime(
        agent_id="agent-a",
        ticket_manager=ticket_mgr,
        replay_guard=ReplayGuard(),
        object_store=store,
        verify_public_key_b64=public_key,
        workspace_root=tmp_path / "workspaces",
    )
    outcome = runtime.handle_run(lease)
    assert outcome.status.value == "rejected"


def test_task_signature_forgery_is_rejected(tmp_path):
    store = _build_store()
    signer_private, _ = generate_keypair_b64()
    _, verifier_public = generate_keypair_b64()

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "payload.txt").write_text("safe", encoding="utf-8")
    package_build = build_file_package(source_dir)
    package_id = store.put_package(package_build.archive_path.read_bytes())
    command_id = store.put_command(CommandObject(argv=[sys.executable, "-c", "print('x')"], working_dir="."))
    signed_task = _signed_task(package_id, command_id, signer_private)
    task_id = store.put_signed_task(signed_task)

    ticket_mgr = RunTicketManager("v" * 32)
    run_ticket, _ = ticket_mgr.issue(
        run_id="run-2",
        agent_id="agent-a",
        task_id=task_id,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=120),
    )
    lease = RunLease(
        run_id="run-2",
        agent_id="agent-a",
        task_id=task_id,
        run_ticket=run_ticket,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=120),
    )

    runtime = AgentRuntime(
        agent_id="agent-a",
        ticket_manager=ticket_mgr,
        replay_guard=ReplayGuard(),
        object_store=store,
        verify_public_key_b64=verifier_public,
        workspace_root=tmp_path / "workspaces",
    )
    outcome = runtime.handle_run(lease)
    assert outcome.status.value == "rejected"


def test_run_ticket_replay_is_rejected(tmp_path):
    store = _build_store()
    private_key, public_key = generate_keypair_b64()

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "payload.txt").write_text("safe", encoding="utf-8")
    package_build = build_file_package(source_dir)
    package_id = store.put_package(package_build.archive_path.read_bytes())
    command_id = store.put_command(CommandObject(argv=[sys.executable, "-c", "print('x')"], working_dir="."))
    signed_task = _signed_task(package_id, command_id, private_key)
    task_id = store.put_signed_task(signed_task)

    ticket_mgr = RunTicketManager("r" * 32)
    run_ticket, _ = ticket_mgr.issue(
        run_id="run-3",
        agent_id="agent-a",
        task_id=task_id,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=120),
    )
    lease = RunLease(
        run_id="run-3",
        agent_id="agent-a",
        task_id=task_id,
        run_ticket=run_ticket,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=120),
    )

    runtime = AgentRuntime(
        agent_id="agent-a",
        ticket_manager=ticket_mgr,
        replay_guard=ReplayGuard(),
        object_store=store,
        verify_public_key_b64=public_key,
        workspace_root=tmp_path / "workspaces",
    )
    first = runtime.handle_run(lease)
    second = runtime.handle_run(lease)
    assert first.status.value in {"succeeded", "failed"}
    assert second.status.value == "rejected"


def test_api_unavailable_does_not_execute(tmp_path):
    store = _build_store()
    runtime = AgentRuntime(
        agent_id="agent-a",
        ticket_manager=RunTicketManager("p" * 32),
        replay_guard=ReplayGuard(),
        object_store=store,
        verify_public_key_b64=generate_keypair_b64()[1],
        workspace_root=tmp_path / "workspaces",
    )
    service = AgentService(agent_id="agent-a", hub_url="http://127.0.0.1:9", runtime=runtime, poll_interval_sec=0.01)
    assert service.poll_once() == "poll_error"


def test_queued_run_can_be_canceled(tmp_path):
    app = create_app(
        run_store=RunStore(tmp_path / "runs.sqlite3"),
        ticket_manager=RunTicketManager("q" * 32),
        api_token="api-token",
    )
    client = TestClient(app)
    submit = client.post(
        "/api/runs",
        json=RunCreateRequest(agent_id="agent-a", task_id="sha256-" + ("1" * 64)).model_dump(mode="json"),
        headers={"Authorization": "Bearer api-token"},
    )
    run_id = submit.json()["run_id"]

    cancel = client.post(f"/api/runs/{run_id}/cancel", headers={"Authorization": "Bearer api-token"})
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "canceled"

    status_resp = client.get(f"/api/runs/{run_id}", headers={"Authorization": "Bearer api-token"})
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "canceled"


def test_running_run_can_be_canceled(tmp_path):
    store = _build_store()
    private_key, public_key = generate_keypair_b64()

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "payload.txt").write_text("safe", encoding="utf-8")
    package_build = build_file_package(source_dir)
    package_id = store.put_package(package_build.archive_path.read_bytes())
    command_id = store.put_command(
        CommandObject(argv=[sys.executable, "-c", "import time; time.sleep(30)"], working_dir=".")
    )
    signed_task = _signed_task(package_id, command_id, private_key)
    task_id = store.put_signed_task(signed_task)

    app = create_app(
        run_store=RunStore(tmp_path / "runs.sqlite3"),
        ticket_manager=RunTicketManager("z" * 32),
        api_token="api-token",
    )
    client = TestClient(app)
    submit = client.post(
        "/api/runs",
        json=RunCreateRequest(agent_id="agent-a", task_id=task_id).model_dump(mode="json"),
        headers={"Authorization": "Bearer api-token"},
    )
    run_id = submit.json()["run_id"]
    lease_payload = client.get("/api/agents/agent-a/next", headers={"Authorization": "Bearer api-token"}).json()

    runtime = AgentRuntime(
        agent_id="agent-a",
        ticket_manager=RunTicketManager("z" * 32),
        replay_guard=ReplayGuard(),
        object_store=store,
        verify_public_key_b64=public_key,
        workspace_root=tmp_path / "workspaces",
        heartbeat_interval_sec=0.01,
    )
    lease = RunLease.model_validate(lease_payload)

    phases: list[str] = []

    def heartbeat(phase: str) -> bool:
        phases.append(phase)
        if phase == "running":
            client.post(f"/api/runs/{run_id}/cancel", headers={"Authorization": "Bearer api-token"})
        response = client.post(
            f"/api/runs/{run_id}/heartbeat",
            json={"phase": phase},
            headers={"Authorization": "Bearer api-token"},
        )
        return response.json()["cancel_requested"]

    outcome = runtime.handle_run(lease, heartbeat=heartbeat)
    complete = client.post(
        f"/api/runs/{run_id}/complete",
        json=RunCompletionRequest(
            status=outcome.status,
            log_ids=outcome.log_ids,
            result_id=outcome.result_id,
            artifact_ids=outcome.artifact_ids,
            failure_code=outcome.failure_code,
        ).model_dump(mode="json"),
        headers={"Authorization": "Bearer api-token"},
    )
    assert complete.status_code == 200
    assert outcome.status.value == "canceled"
    assert "running" in phases

    record = client.get(f"/api/runs/{run_id}", headers={"Authorization": "Bearer api-token"}).json()
    assert record["status"] == "canceled"
    assert record["failure_code"] == "canceled"


def test_large_stdout_is_published_in_chunks(tmp_path):
    store = _build_store()
    private_key, public_key = generate_keypair_b64()

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "placeholder.txt").write_text("placeholder", encoding="utf-8")
    package_build = build_file_package(source_dir)
    package_id = store.put_package(package_build.archive_path.read_bytes())
    command_id = store.put_command(
        CommandObject(
            argv=[
                sys.executable,
                "-c",
                "import sys,time; sys.stdout.write('x'*20000); sys.stdout.flush(); time.sleep(1.0)",
            ],
            working_dir=".",
            timeout_sec=30,
        )
    )
    signed_task = _signed_task(package_id, command_id, private_key)
    task_id = store.put_signed_task(signed_task)

    ticket_mgr = RunTicketManager("m" * 32)
    run_ticket, _ = ticket_mgr.issue(
        run_id="run-stream",
        agent_id="agent-a",
        task_id=task_id,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=120),
    )
    lease = RunLease(
        run_id="run-stream",
        agent_id="agent-a",
        task_id=task_id,
        run_ticket=run_ticket,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=120),
    )

    runtime = AgentRuntime(
        agent_id="agent-a",
        ticket_manager=ticket_mgr,
        replay_guard=ReplayGuard(),
        object_store=store,
        verify_public_key_b64=public_key,
        workspace_root=tmp_path / "workspaces",
        heartbeat_interval_sec=0.01,
    )

    published: list[list[str]] = []
    outcome = runtime.handle_run(lease, publish_logs=lambda log_ids: published.append(log_ids[:]))
    assert outcome.status.value == "succeeded"
    assert published
    assert outcome.log_ids
    logs = [store.get_log(log_id) for log_id in outcome.log_ids]
    assert any(log.stream == "stdout" and "x" * 1000 in log.data for log in logs)
