from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from mvp_orbit.core.canonical import require_matching_json_object_id, require_matching_object_id
from mvp_orbit.core.models import LogObject, ResultObject, RunLease, RunStatus
from mvp_orbit.core.signing import SignatureError, verify_payload_signature
from mvp_orbit.core.tickets import ReplayGuard, RunTicketManager, TicketError
from mvp_orbit.integrations.object_store import ObjectStore


@dataclass
class ExecutionOutcome:
    status: RunStatus
    log_ids: list[str]
    result_id: str
    artifact_ids: list[str]
    failure_code: str | None = None


class AgentRuntime:
    def __init__(
        self,
        *,
        agent_id: str,
        ticket_manager: RunTicketManager,
        replay_guard: ReplayGuard,
        object_store: ObjectStore,
        verify_public_key_b64: str,
        workspace_root: str | Path,
        heartbeat_interval_sec: float = 5.0,
    ) -> None:
        self.agent_id = agent_id
        self.ticket_manager = ticket_manager
        self.replay_guard = replay_guard
        self.object_store = object_store
        self.verify_public_key_b64 = verify_public_key_b64
        self.workspace_root = Path(workspace_root)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.heartbeat_interval_sec = heartbeat_interval_sec

    def handle_run(self, lease: RunLease, *, heartbeat: Callable[[str], None] | None = None) -> ExecutionOutcome:
        try:
            package_id, command = self._validate_and_load_objects(lease)
            if heartbeat is not None:
                heartbeat("preparing")
            workspace = self._prepare_workspace(lease.run_id, package_id)
            return self._execute_command(lease, command, workspace, heartbeat)
        except Exception as exc:
            now = datetime.now(timezone.utc)
            stderr_log_id = self.object_store.put_log(
                LogObject(stream="stderr", data=str(exc), captured_at=now)
            )
            result_id = self.object_store.put_result(
                ResultObject(
                    status=RunStatus.REJECTED.value,
                    exit_code=1,
                    started_at=now,
                    finished_at=now,
                )
            )
            return ExecutionOutcome(
                status=RunStatus.REJECTED,
                log_ids=[stderr_log_id],
                result_id=result_id,
                artifact_ids=[],
                failure_code="rejected",
            )

    def _validate_and_load_objects(self, lease: RunLease):
        if lease.agent_id != self.agent_id:
            raise RuntimeError(f"run is assigned to a different agent: {lease.agent_id}")

        try:
            ticket = self.ticket_manager.verify(lease.run_ticket)
            self.replay_guard.consume(ticket.nonce)
        except TicketError as exc:
            raise RuntimeError(f"ticket rejected: {exc}") from exc

        if (
            ticket.run_id != lease.run_id
            or ticket.agent_id != lease.agent_id
            or ticket.task_id != lease.task_id
        ):
            raise RuntimeError("ticket claims do not match run lease")

        signed_task = self.object_store.get_signed_task(lease.task_id)
        task_payload = signed_task.task.model_dump(mode="json", exclude_none=True)
        require_matching_json_object_id(lease.task_id, task_payload)
        try:
            verify_payload_signature(task_payload, signed_task.task_signature, self.verify_public_key_b64)
        except SignatureError as exc:
            raise RuntimeError(f"task signature verification failed: {exc}") from exc

        package_id = signed_task.task.package_id
        command_id = signed_task.task.command_id

        command = self.object_store.get_command(command_id)
        require_matching_json_object_id(
            command_id,
            command.model_dump(mode="json", exclude_none=True),
        )

        package_bytes = self.object_store.get_package(package_id)
        require_matching_object_id(package_id, package_bytes)
        return package_id, command

    def _prepare_workspace(self, run_id: str, package_id: str) -> Path:
        workspace = self.workspace_root / run_id
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)

        package_bytes = self.object_store.get_package(package_id)
        with tarfile.open(fileobj=io.BytesIO(package_bytes), mode="r:gz") as tar:
            self._extract_safely(tar, workspace)
        return workspace

    def _execute_command(
        self,
        lease: RunLease,
        command,
        workspace: Path,
        heartbeat: Callable[[str], None] | None,
    ) -> ExecutionOutcome:
        started_at = datetime.now(timezone.utc)
        stdout_path = Path(tempfile.mkstemp(prefix="orbit-stdout-", dir=workspace)[1])
        stderr_path = Path(tempfile.mkstemp(prefix="orbit-stderr-", dir=workspace)[1])

        try:
            cwd = (workspace / command.working_dir).resolve()
            workspace_root = workspace.resolve()
            if cwd != workspace_root and workspace_root not in cwd.parents:
                raise RuntimeError("command working_dir escapes workspace")

            env = self._merged_env(command.env_patch)
            with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open("w", encoding="utf-8") as stderr_handle:
                proc = subprocess.Popen(
                    command.argv,
                    cwd=str(cwd),
                    env=env,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                )

                next_heartbeat = time.monotonic()
                deadline = time.monotonic() + command.timeout_sec
                timed_out = False
                while True:
                    if heartbeat is not None and time.monotonic() >= next_heartbeat:
                        heartbeat("running")
                        next_heartbeat = time.monotonic() + self.heartbeat_interval_sec

                    return_code = proc.poll()
                    if return_code is not None:
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        proc.kill()
                        proc.wait()
                        return_code = -1
                        break
                    time.sleep(0.2)

            finished_at = datetime.now(timezone.utc)
            stdout = stdout_path.read_text(encoding="utf-8")
            stderr = stderr_path.read_text(encoding="utf-8")

            log_ids: list[str] = []
            if stdout:
                log_ids.append(
                    self.object_store.put_log(
                        LogObject(stream="stdout", data=stdout, captured_at=finished_at)
                    )
                )
            if stderr:
                log_ids.append(
                    self.object_store.put_log(
                        LogObject(stream="stderr", data=stderr, captured_at=finished_at)
                    )
                )

            status = RunStatus.SUCCEEDED if return_code == 0 and not timed_out else RunStatus.FAILED
            failure_code = "timeout" if timed_out else None
            result_id = self.object_store.put_result(
                ResultObject(
                    status=status.value,
                    exit_code=return_code,
                    started_at=started_at,
                    finished_at=finished_at,
                )
            )
            return ExecutionOutcome(
                status=status,
                log_ids=log_ids,
                result_id=result_id,
                artifact_ids=[],
                failure_code=failure_code,
            )
        finally:
            stdout_path.unlink(missing_ok=True)
            stderr_path.unlink(missing_ok=True)

    @staticmethod
    def _extract_safely(tar: tarfile.TarFile, destination: Path) -> None:
        destination_root = destination.resolve()
        for member in tar.getmembers():
            member_path = (destination / member.name).resolve()
            if destination_root != member_path and destination_root not in member_path.parents:
                raise RuntimeError(f"illegal package path: {member.name}")
        try:
            tar.extractall(destination, filter="data")
        except TypeError:
            tar.extractall(destination)

    @staticmethod
    def _merged_env(extra_env: dict[str, str]) -> dict[str, str]:
        import os

        env = os.environ.copy()
        env.update(extra_env)
        return env
