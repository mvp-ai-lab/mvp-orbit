from __future__ import annotations

import io
import logging
import os
import shlex
import shutil
import subprocess
import tarfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from mvp_orbit.core.canonical import require_matching_object_id
from mvp_orbit.core.models import (
    CommandLease,
    CommandStatus,
    ShellSessionLease,
    ShellSessionStatus,
)

logger = logging.getLogger(__name__)


@dataclass
class CommandExecutionOutcome:
    status: CommandStatus
    exit_code: int
    failure_code: str | None = None


@dataclass
class ShellExecutionOutcome:
    status: ShellSessionStatus
    exit_code: int
    failure_code: str | None = None


class _ChunkedOutputForwarder:
    def __init__(
        self,
        callback: Callable[[str, str], None],
        *,
        flush_bytes: int,
        flush_interval_sec: float,
    ) -> None:
        self._callback = callback
        self._flush_bytes = max(1, flush_bytes)
        self._flush_interval_sec = max(0.01, flush_interval_sec)
        self._buffers = {"stdout": [], "stderr": []}
        self._sizes = {"stdout": 0, "stderr": 0}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._thread.start()

    def write(self, stream: str, data: str) -> None:
        payload: str | None = None
        with self._lock:
            self._buffers[stream].append(data)
            self._sizes[stream] += len(data)
            if self._sizes[stream] >= self._flush_bytes:
                payload = "".join(self._buffers[stream])
                self._buffers[stream] = []
                self._sizes[stream] = 0
        if payload:
            self._callback(stream, payload)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        self.flush_all()

    def flush_all(self) -> None:
        pending: list[tuple[str, str]] = []
        with self._lock:
            for stream in ("stdout", "stderr"):
                if not self._buffers[stream]:
                    continue
                pending.append((stream, "".join(self._buffers[stream])))
                self._buffers[stream] = []
                self._sizes[stream] = 0
        for stream, payload in pending:
            self._callback(stream, payload)

    def _flush_loop(self) -> None:
        while not self._stop.wait(self._flush_interval_sec):
            self.flush_all()


class AgentRuntime:
    def __init__(
        self,
        *,
        agent_id: str,
        base_workspace: str | Path,
        heartbeat_interval_sec: float = 5.0,
        command_output_chunk_bytes: int = 16384,
        command_output_flush_interval_sec: float = 10.0,
    ) -> None:
        self.agent_id = agent_id
        self.base_workspace = Path(base_workspace).resolve()
        self.base_workspace.mkdir(parents=True, exist_ok=True)
        self.packages_root = self.base_workspace / ".orbit" / "packages"
        self.packages_root.mkdir(parents=True, exist_ok=True)
        self.heartbeat_interval_sec = heartbeat_interval_sec
        self.command_output_chunk_bytes = command_output_chunk_bytes
        self.command_output_flush_interval_sec = command_output_flush_interval_sec

    def handle_command(
        self,
        lease: CommandLease,
        *,
        fetch_package: Callable[[str], bytes],
        append_output: Callable[[str, str], None],
        heartbeat: Callable[[], bool],
    ) -> CommandExecutionOutcome:
        workspace = self._command_workspace(lease, fetch_package)
        cwd = self._resolve_working_dir(workspace, lease.working_dir)
        env = self._merged_env(lease.env_patch)
        logger.info(
            "agent %s starting command %s in %s package_id=%s argv=%s",
            self.agent_id,
            lease.command_id,
            cwd,
            lease.package_id or "-",
            shlex.join(lease.argv),
        )

        proc = subprocess.Popen(
            lease.argv,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        output_forwarder = _ChunkedOutputForwarder(
            append_output,
            flush_bytes=self.command_output_chunk_bytes,
            flush_interval_sec=self.command_output_flush_interval_sec,
        )

        stdout_thread = threading.Thread(
            target=self._stream_reader,
            args=(proc.stdout, "stdout", output_forwarder.write),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._stream_reader,
            args=(proc.stderr, "stderr", output_forwarder.write),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        deadline = time.monotonic() + lease.timeout_sec
        next_heartbeat = time.monotonic()
        canceled = False
        timed_out = False
        return_code = 0
        while True:
            if time.monotonic() >= next_heartbeat:
                canceled = heartbeat()
                next_heartbeat = time.monotonic() + self.heartbeat_interval_sec
                if canceled:
                    return_code = self._terminate_process(proc)
                    break

            polled = proc.poll()
            if polled is not None:
                return_code = polled
                break
            if time.monotonic() >= deadline:
                timed_out = True
                proc.kill()
                proc.wait()
                return_code = -1
                break
            time.sleep(0.2)

        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        output_forwarder.close()

        if canceled:
            logger.info("agent %s canceled command %s exit_code=%s", self.agent_id, lease.command_id, return_code)
            return CommandExecutionOutcome(status=CommandStatus.CANCELED, exit_code=return_code, failure_code="canceled")
        if timed_out:
            logger.warning("agent %s timed out command %s", self.agent_id, lease.command_id)
            return CommandExecutionOutcome(status=CommandStatus.FAILED, exit_code=return_code, failure_code="timeout")
        status = CommandStatus.SUCCEEDED if return_code == 0 else CommandStatus.FAILED
        logger.info(
            "agent %s finished command %s status=%s exit_code=%s",
            self.agent_id,
            lease.command_id,
            status.value,
            return_code,
        )
        return CommandExecutionOutcome(status=status, exit_code=return_code)

    def handle_shell_session(
        self,
        lease: ShellSessionLease,
        *,
        fetch_package: Callable[[str], bytes],
        get_inputs: Callable[[int], list[tuple[int, str]]],
        append_event: Callable[[str, str], None],
        heartbeat: Callable[[], bool],
        should_close: Callable[[], bool],
    ) -> ShellExecutionOutcome:
        workspace = self._shell_workspace(lease, fetch_package)
        logger.info(
            "agent %s starting shell session %s in %s package_id=%s",
            self.agent_id,
            lease.session_id,
            workspace,
            lease.package_id or "-",
        )
        proc = subprocess.Popen(
            self._shell_argv(),
            cwd=str(workspace),
            env=self._merged_env({}),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None

        stdout_thread = threading.Thread(
            target=self._stream_reader,
            args=(proc.stdout, "stdout", append_event),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._stream_reader,
            args=(proc.stderr, "stderr", append_event),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        next_input_seq = 0
        next_heartbeat = time.monotonic()
        while True:
            if time.monotonic() >= next_heartbeat:
                heartbeat()
                next_heartbeat = time.monotonic() + self.heartbeat_interval_sec

            for seq, data in get_inputs(next_input_seq):
                logger.info(
                    "agent %s shell session %s input seq=%s command=%r",
                    self.agent_id,
                    lease.session_id,
                    seq,
                    self._input_preview(data),
                )
                proc.stdin.write(data)
                proc.stdin.flush()
                next_input_seq = seq

            polled = proc.poll()
            if polled is not None:
                stdout_thread.join(timeout=2)
                stderr_thread.join(timeout=2)
                status = ShellSessionStatus.CLOSED if polled == 0 else ShellSessionStatus.FAILED
                logger.info(
                    "agent %s finished shell session %s status=%s exit_code=%s",
                    self.agent_id,
                    lease.session_id,
                    status.value,
                    polled,
                )
                return ShellExecutionOutcome(status=status, exit_code=polled)

            if should_close():
                logger.info("agent %s closing shell session %s on request", self.agent_id, lease.session_id)
                proc.stdin.write("exit\n")
                proc.stdin.flush()
                try:
                    return_code = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    return_code = self._terminate_process(proc)
                stdout_thread.join(timeout=2)
                stderr_thread.join(timeout=2)
                status = ShellSessionStatus.CLOSED if return_code == 0 else ShellSessionStatus.FAILED
                failure = None if return_code == 0 else "closed"
                logger.info(
                    "agent %s closed shell session %s status=%s exit_code=%s",
                    self.agent_id,
                    lease.session_id,
                    status.value,
                    return_code,
                )
                return ShellExecutionOutcome(status=status, exit_code=return_code, failure_code=failure)
            time.sleep(0.2)

    def _command_workspace(self, lease: CommandLease, fetch_package: Callable[[str], bytes]) -> Path:
        if lease.package_id is None:
            return self.base_workspace
        return self._ensure_package_workspace(lease.package_id, fetch_package)

    def _shell_workspace(self, lease: ShellSessionLease, fetch_package: Callable[[str], bytes]) -> Path:
        if lease.package_id is None:
            return self.base_workspace
        return self._ensure_package_workspace(lease.package_id, fetch_package)

    def _ensure_package_workspace(self, package_id: str, fetch_package: Callable[[str], bytes]) -> Path:
        workspace = self.packages_root / package_id
        marker = workspace / ".orbit-ready"
        if marker.exists():
            logger.info("agent %s reusing package %s in %s", self.agent_id, package_id, workspace)
            return workspace

        logger.info("agent %s downloading package %s into %s", self.agent_id, package_id, workspace)
        package_bytes = fetch_package(package_id)
        require_matching_object_id(package_id, package_bytes)
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(package_bytes), mode="r:gz") as tar:
            self._extract_safely(tar, workspace)
        marker.write_text("ready\n", encoding="utf-8")
        logger.info("agent %s prepared package %s in %s", self.agent_id, package_id, workspace)
        return workspace

    def _resolve_working_dir(self, workspace: Path, working_dir: str) -> Path:
        cwd = (workspace / working_dir).resolve()
        workspace_root = workspace.resolve()
        if cwd != workspace_root and workspace_root not in cwd.parents:
            raise RuntimeError("command working_dir escapes workspace")
        cwd.mkdir(parents=True, exist_ok=True)
        return cwd

    def _merged_env(self, patch: dict[str, str]) -> dict[str, str]:
        env = os.environ.copy()
        env.update(patch)
        return env

    @staticmethod
    def _stream_reader(handle, stream: str, callback: Callable[[str, str], None]) -> None:
        try:
            while True:
                chunk = handle.readline()
                if chunk == "":
                    break
                callback(stream, chunk)
        finally:
            handle.close()

    @staticmethod
    def _terminate_process(proc: subprocess.Popen) -> int:
        proc.terminate()
        try:
            return proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.wait()

    @staticmethod
    def _shell_argv() -> list[str]:
        if Path("/bin/bash").exists():
            return ["/bin/bash", "-i"]
        return ["/bin/sh", "-i"]

    @staticmethod
    def _input_preview(data: str) -> str:
        preview = data.strip()
        if not preview:
            return "<empty>"
        if len(preview) > 120:
            return f"{preview[:117]}..."
        return preview

    @staticmethod
    def _extract_safely(tar: tarfile.TarFile, destination: Path) -> None:
        root = destination.resolve()
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"package member escapes workspace: {member.name}")
        tar.extractall(destination)
