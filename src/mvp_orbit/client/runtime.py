from __future__ import annotations

import base64
import fcntl
import logging
import os
import pty
import select
import selectors
import shlex
import signal
import struct
import subprocess
import termios
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from mvp_orbit.core.logging import log_kv
from mvp_orbit.core.models import (
    CommandLease,
    CommandStatus,
    FileTransferResult,
    FileTransferStatus,
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


class ClientRuntime:
    def __init__(
        self,
        *,
        client_id: str,
        base_workspace: str | Path,
        command_output_chunk_bytes: int = 4096,
        command_output_flush_interval_sec: float = 0.1,
    ) -> None:
        self.client_id = client_id
        self.base_workspace = Path(base_workspace).resolve()
        self.base_workspace.mkdir(parents=True, exist_ok=True)
        self.command_output_chunk_bytes = max(64, command_output_chunk_bytes)
        self.command_output_flush_interval_sec = max(0.01, command_output_flush_interval_sec)

    def handle_command(
        self,
        lease: CommandLease,
        *,
        on_started: Callable[[], None],
        append_output: Callable[[str, str], None],
        should_cancel: Callable[[], bool],
    ) -> CommandExecutionOutcome:
        cwd = self._resolve_working_dir(lease.working_dir)
        env = self._merged_env(lease.env_patch)
        log_kv(logger, logging.INFO, "command.start", client_id=self.client_id, command_id=lease.command_id, cwd=cwd, argv=shlex.join(lease.argv))

        proc = subprocess.Popen(
            lease.argv,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
            start_new_session=True,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        on_started()

        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")

        deadline = time.monotonic() + lease.timeout_sec
        canceled = False
        timed_out = False
        try:
            while True:
                if should_cancel():
                    canceled = True
                    self._terminate_process(proc)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    proc.kill()
                    proc.wait()
                    break

                for key, _ in selector.select(timeout=self.command_output_flush_interval_sec):
                    chunk = os.read(key.fileobj.fileno(), self.command_output_chunk_bytes)
                    if not chunk:
                        try:
                            selector.unregister(key.fileobj)
                        except Exception:
                            pass
                        continue
                    append_output(key.data, chunk.decode("utf-8", errors="replace"))

                if proc.poll() is not None and not selector.get_map():
                    break
        finally:
            selector.close()
            for handle in (proc.stdout, proc.stderr):
                try:
                    handle.close()
                except Exception:
                    pass

        return_code = proc.wait()
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
        on_started: Callable[[], None],
        append_output: Callable[[str], None],
        pop_input: Callable[[], list[bytes]],
        pop_resize: Callable[[], list[tuple[int, int]]],
        should_close: Callable[[], bool],
    ) -> ShellExecutionOutcome:
        workspace = self.base_workspace
        log_kv(logger, logging.INFO, "shell.start", client_id=self.client_id, session_id=lease.session_id, workspace=workspace)
        master_fd, slave_fd = pty.openpty()
        proc = subprocess.Popen(
            self._shell_argv(),
            cwd=str(workspace),
            env=self._merged_env({}),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=False,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave_fd)
        self._set_nonblocking(master_fd)
        on_started()

        try:
            while True:
                for rows, cols in pop_resize():
                    self._set_winsize(master_fd, rows, cols)

                for data in pop_input():
                    if data:
                        os.write(master_fd, data)

                if should_close():
                    try:
                        os.write(master_fd, b"exit\n")
                    except OSError:
                        pass
                    try:
                        return_code = proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        return_code = self._terminate_process(proc)
                    status = ShellSessionStatus.CLOSED if return_code == 0 else ShellSessionStatus.FAILED
                    failure = None if return_code == 0 else "closed"
                    return ShellExecutionOutcome(status=status, exit_code=return_code, failure_code=failure)

                ready, _, _ = select.select([master_fd], [], [], 0.05)
                if ready:
                    try:
                        chunk = os.read(master_fd, 4096)
                    except BlockingIOError:
                        chunk = b""
                    except OSError:
                        chunk = b""
                    if chunk:
                        append_output(chunk.decode("utf-8", errors="replace"))

                polled = proc.poll()
                if polled is not None:
                    try:
                        while True:
                            chunk = os.read(master_fd, 4096)
                            if not chunk:
                                break
                            append_output(chunk.decode("utf-8", errors="replace"))
                    except OSError:
                        pass
                    status = ShellSessionStatus.CLOSED if polled == 0 else ShellSessionStatus.FAILED
                    return ShellExecutionOutcome(status=status, exit_code=polled)
        finally:
            try:
                os.close(master_fd)
            except OSError:
                pass

    def handle_file_push(self, *, transfer_id: str, remote_path: str, data_b64: str, max_bytes: int) -> FileTransferResult:
        try:
            data = base64.b64decode(data_b64.encode("ascii"), validate=True)
            if len(data) > max_bytes:
                return FileTransferResult(
                    transfer_id=transfer_id,
                    status=FileTransferStatus.FAILED,
                    direction="push",
                    remote_path=remote_path,
                    size=len(data),
                    failure_code="too_large",
                )
            path = self._resolve_remote_file(remote_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return FileTransferResult(
                transfer_id=transfer_id,
                status=FileTransferStatus.SUCCEEDED,
                direction="push",
                remote_path=remote_path,
                size=len(data),
            )
        except Exception as exc:
            log_kv(logger, logging.WARNING, "file.push.failed", client_id=self.client_id, remote_path=remote_path, error=exc.__class__.__name__)
            return FileTransferResult(
                transfer_id=transfer_id,
                status=FileTransferStatus.FAILED,
                direction="push",
                remote_path=remote_path,
                size=0,
                failure_code=exc.__class__.__name__,
            )

    def handle_file_pull(self, *, transfer_id: str, remote_path: str, max_bytes: int) -> FileTransferResult:
        try:
            path = self._resolve_remote_file(remote_path)
            size = path.stat().st_size
            if size > max_bytes:
                return FileTransferResult(
                    transfer_id=transfer_id,
                    status=FileTransferStatus.FAILED,
                    direction="pull",
                    remote_path=remote_path,
                    size=size,
                    failure_code="too_large",
                )
            data = path.read_bytes()
            return FileTransferResult(
                transfer_id=transfer_id,
                status=FileTransferStatus.SUCCEEDED,
                direction="pull",
                remote_path=remote_path,
                size=len(data),
                data_b64=base64.b64encode(data).decode("ascii"),
            )
        except Exception as exc:
            log_kv(logger, logging.WARNING, "file.pull.failed", client_id=self.client_id, remote_path=remote_path, error=exc.__class__.__name__)
            return FileTransferResult(
                transfer_id=transfer_id,
                status=FileTransferStatus.FAILED,
                direction="pull",
                remote_path=remote_path,
                size=0,
                failure_code=exc.__class__.__name__,
            )

    def _resolve_working_dir(self, working_dir: str) -> Path:
        cwd = (self.base_workspace / working_dir).resolve()
        root = self.base_workspace.resolve()
        if cwd != root and root not in cwd.parents:
            raise RuntimeError("command working_dir escapes workspace")
        cwd.mkdir(parents=True, exist_ok=True)
        return cwd

    def _resolve_remote_file(self, remote_path: str) -> Path:
        path = Path(remote_path).expanduser()
        if path.is_absolute():
            return path
        return (self.base_workspace / path).resolve()

    @staticmethod
    def _merged_env(patch: dict[str, str]) -> dict[str, str]:
        env = os.environ.copy()
        env.update(patch)
        return env

    @staticmethod
    def _terminate_process(proc: subprocess.Popen) -> int:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            proc.terminate()
        try:
            return proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            return proc.wait()

    @staticmethod
    def _shell_argv() -> list[str]:
        if Path("/bin/bash").exists():
            return ["/bin/bash", "-i"]
        return ["/bin/sh", "-i"]

    @staticmethod
    def _set_nonblocking(fd: int) -> None:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    @staticmethod
    def _set_winsize(fd: int, rows: int, cols: int) -> None:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
