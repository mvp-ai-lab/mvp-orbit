from __future__ import annotations

import io
import os
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


class AgentRuntime:
    def __init__(
        self,
        *,
        agent_id: str,
        base_workspace: str | Path,
        heartbeat_interval_sec: float = 5.0,
    ) -> None:
        self.agent_id = agent_id
        self.base_workspace = Path(base_workspace).resolve()
        self.base_workspace.mkdir(parents=True, exist_ok=True)
        self.packages_root = self.base_workspace / ".orbit" / "packages"
        self.packages_root.mkdir(parents=True, exist_ok=True)
        self.heartbeat_interval_sec = heartbeat_interval_sec

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

        stdout_thread = threading.Thread(
            target=self._stream_reader,
            args=(proc.stdout, "stdout", append_output),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._stream_reader,
            args=(proc.stderr, "stderr", append_output),
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

        if canceled:
            return CommandExecutionOutcome(status=CommandStatus.CANCELED, exit_code=return_code, failure_code="canceled")
        if timed_out:
            return CommandExecutionOutcome(status=CommandStatus.FAILED, exit_code=return_code, failure_code="timeout")
        status = CommandStatus.SUCCEEDED if return_code == 0 else CommandStatus.FAILED
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
                proc.stdin.write(data)
                proc.stdin.flush()
                next_input_seq = seq

            polled = proc.poll()
            if polled is not None:
                stdout_thread.join(timeout=2)
                stderr_thread.join(timeout=2)
                status = ShellSessionStatus.CLOSED if polled == 0 else ShellSessionStatus.FAILED
                return ShellExecutionOutcome(status=status, exit_code=polled)

            if should_close():
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
            return workspace

        package_bytes = fetch_package(package_id)
        require_matching_object_id(package_id, package_bytes)
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(package_bytes), mode="r:gz") as tar:
            self._extract_safely(tar, workspace)
        marker.write_text("ready\n", encoding="utf-8")
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
    def _extract_safely(tar: tarfile.TarFile, destination: Path) -> None:
        root = destination.resolve()
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"package member escapes workspace: {member.name}")
        tar.extractall(destination)
