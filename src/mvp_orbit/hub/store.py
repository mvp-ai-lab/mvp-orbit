from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

from mvp_orbit.core.models import (
    AgentRecord,
    CommandCompletionRequest,
    CommandCreateRequest,
    CommandLease,
    CommandOutputChunk,
    CommandRecord,
    CommandStatus,
    ConnectResponse,
    PackageRecord,
    ShellCompletionRequest,
    ShellEvent,
    ShellSessionCreateRequest,
    ShellSessionLease,
    ShellSessionRecord,
    ShellSessionStatus,
    UserRecord,
    utc_now,
)

TOKEN_TTL = timedelta(days=7)


class InvalidTokenError(Exception):
    pass


class ExpiredTokenError(Exception):
    pass


class OwnershipError(Exception):
    pass


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    expires_at: datetime


class HubStore:
    def __init__(self, db_path: str | Path, object_root: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.object_root = Path(object_root)
        self.object_root.mkdir(parents=True, exist_ok=True)
        self.packages_root = self.object_root / "packages"
        self.commands_root = self.object_root / "commands"
        self.packages_root.mkdir(parents=True, exist_ok=True)
        self.commands_root.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_tokens (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS packages (
                    package_id TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS package_access (
                    package_id TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (package_id, owner_user_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS commands (
                    command_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    package_id TEXT,
                    argv TEXT NOT NULL,
                    env_patch TEXT NOT NULL,
                    timeout_sec INTEGER NOT NULL,
                    working_dir TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    heartbeat_at TEXT,
                    cancel_requested_at TEXT,
                    exit_code INTEGER,
                    failure_code TEXT,
                    stdout_path TEXT NOT NULL,
                    stderr_path TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shell_sessions (
                    session_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    package_id TEXT,
                    cwd_root TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    heartbeat_at TEXT,
                    close_requested_at TEXT,
                    exit_code INTEGER,
                    failure_code TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shell_inputs (
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    consumed_at TEXT,
                    PRIMARY KEY (session_id, seq)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shell_events (
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    stream TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, seq)
                )
                """
            )

    def issue_user_token(self, user_id: str) -> ConnectResponse:
        user = self.ensure_user(user_id)
        created_at = utc_now()
        expires_at = created_at + TOKEN_TTL
        user_token = secrets.token_urlsafe(32)
        token_hash = self._hash_token(user_token)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO user_tokens (token_hash, user_id, created_at, expires_at, revoked_at)
                VALUES (?, ?, ?, ?, NULL)
                """,
                (token_hash, user.user_id, created_at.isoformat(), expires_at.isoformat()),
            )
        return ConnectResponse(user_id=user.user_id, user_token=user_token, expires_at=expires_at)

    def ensure_user(self, user_id: str) -> UserRecord:
        now = utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO users (user_id, created_at)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (user_id, now.isoformat()),
            )
            row = self._conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            assert row is not None
        return self._row_to_user(dict(row))

    def authenticate_user_token(self, user_token: str) -> AuthenticatedUser:
        token_hash = self._hash_token(user_token)
        with self._lock:
            row = self._conn.execute(
                "SELECT user_id, expires_at, revoked_at FROM user_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        if row is None or row["revoked_at"] is not None:
            raise InvalidTokenError("invalid token")
        expires_at = _parse_dt(row["expires_at"])
        assert expires_at is not None
        if expires_at <= utc_now():
            raise ExpiredTokenError("token expired")
        return AuthenticatedUser(user_id=str(row["user_id"]), expires_at=expires_at)

    def register_agent(self, agent_id: str, owner_user_id: str) -> AgentRecord:
        now = utc_now().isoformat()
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO agents (agent_id, owner_user_id, created_at, last_seen_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (agent_id, owner_user_id, now, now),
                )
            else:
                if row["owner_user_id"] != owner_user_id:
                    raise OwnershipError(agent_id)
                self._conn.execute(
                    "UPDATE agents SET last_seen_at = ? WHERE agent_id = ?",
                    (now, agent_id),
                )
            updated = self._conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            assert updated is not None
        return self._row_to_agent(dict(updated))

    def get_agent(self, agent_id: str) -> AgentRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_agent(dict(row))

    def assert_agent_owner(self, agent_id: str, owner_user_id: str) -> None:
        agent = self.get_agent(agent_id)
        if agent is None or agent.owner_user_id != owner_user_id:
            raise OwnershipError(agent_id)

    def put_package(self, package_id: str, payload: bytes, owner_user_id: str) -> PackageRecord:
        path = self.package_path(package_id)
        if not path.exists():
            path.write_bytes(payload)
        now = utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO packages (package_id, size, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(package_id) DO NOTHING
                """,
                (package_id, len(payload), now.isoformat()),
            )
            self._conn.execute(
                """
                INSERT INTO package_access (package_id, owner_user_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(package_id, owner_user_id) DO NOTHING
                """,
                (package_id, owner_user_id, now.isoformat()),
            )
            row = self._conn.execute("SELECT * FROM packages WHERE package_id = ?", (package_id,)).fetchone()
            assert row is not None
        return self._row_to_package(dict(row))

    def get_package(self, package_id: str, owner_user_id: str) -> bytes:
        with self._lock:
            access = self._conn.execute(
                "SELECT 1 FROM package_access WHERE package_id = ? AND owner_user_id = ?",
                (package_id, owner_user_id),
            ).fetchone()
        if access is None:
            raise OwnershipError(package_id)
        path = self.package_path(package_id)
        if not path.exists():
            raise FileNotFoundError(package_id)
        return path.read_bytes()

    def ensure_package_access(self, package_id: str, owner_user_id: str) -> None:
        with self._lock:
            access = self._conn.execute(
                "SELECT 1 FROM package_access WHERE package_id = ? AND owner_user_id = ?",
                (package_id, owner_user_id),
            ).fetchone()
        if access is None:
            raise OwnershipError(package_id)

    def create_command(self, command_id: str, owner_user_id: str, request: CommandCreateRequest) -> CommandRecord:
        stdout_path = self.command_output_path(command_id, "stdout")
        stderr_path = self.command_output_path(command_id, "stderr")
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        now = utc_now()
        record = CommandRecord(
            command_id=command_id,
            agent_id=request.agent_id,
            owner_user_id=owner_user_id,
            package_id=request.package_id,
            argv=request.argv,
            env_patch=request.env_patch,
            timeout_sec=request.timeout_sec,
            working_dir=request.working_dir,
            status=CommandStatus.QUEUED,
            created_at=now,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO commands (
                    command_id, agent_id, owner_user_id, package_id, argv, env_patch, timeout_sec, working_dir,
                    status, created_at, started_at, finished_at, heartbeat_at, cancel_requested_at,
                    exit_code, failure_code, stdout_path, stderr_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._command_values(record),
            )
        return record

    def lease_next_command(self, agent_id: str, owner_user_id: str) -> CommandLease | None:
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT * FROM commands
                WHERE agent_id = ? AND owner_user_id = ? AND status = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (agent_id, owner_user_id, CommandStatus.QUEUED.value),
            ).fetchone()
            if row is None:
                return None
            now = utc_now().isoformat()
            self._conn.execute(
                """
                UPDATE commands
                SET status = ?, started_at = ?, heartbeat_at = ?
                WHERE command_id = ?
                """,
                (CommandStatus.RUNNING.value, now, now, row["command_id"]),
            )
            updated = self._conn.execute("SELECT * FROM commands WHERE command_id = ?", (row["command_id"],)).fetchone()
            assert updated is not None
        record = self._row_to_command(dict(updated))
        return CommandLease(
            command_id=record.command_id,
            agent_id=record.agent_id,
            package_id=record.package_id,
            argv=record.argv,
            env_patch=record.env_patch,
            timeout_sec=record.timeout_sec,
            working_dir=record.working_dir,
        )

    def heartbeat_command(self, command_id: str) -> CommandRecord:
        now = utc_now().isoformat()
        with self._lock, self._conn:
            updated = self._conn.execute(
                "UPDATE commands SET heartbeat_at = ? WHERE command_id = ?",
                (now, command_id),
            )
            if updated.rowcount == 0:
                raise KeyError(command_id)
            row = self._conn.execute("SELECT * FROM commands WHERE command_id = ?", (command_id,)).fetchone()
            assert row is not None
        return self._row_to_command(dict(row))

    def append_command_output(self, command_id: str, stream: str, data: str) -> None:
        path = self.command_output_path(command_id, stream)
        if not path.exists():
            raise KeyError(command_id)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(data)

    def complete_command(self, command_id: str, request: CommandCompletionRequest) -> CommandRecord:
        finished = utc_now().isoformat()
        with self._lock, self._conn:
            updated = self._conn.execute(
                """
                UPDATE commands
                SET status = ?, finished_at = ?, exit_code = ?, failure_code = ?
                WHERE command_id = ?
                """,
                (request.status.value, finished, request.exit_code, request.failure_code, command_id),
            )
            if updated.rowcount == 0:
                raise KeyError(command_id)
            row = self._conn.execute("SELECT * FROM commands WHERE command_id = ?", (command_id,)).fetchone()
            assert row is not None
        return self._row_to_command(dict(row))

    def cancel_command(self, command_id: str) -> CommandRecord:
        now = utc_now().isoformat()
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM commands WHERE command_id = ?", (command_id,)).fetchone()
            if row is None:
                raise KeyError(command_id)
            record = self._row_to_command(dict(row))
            if record.status in {CommandStatus.SUCCEEDED, CommandStatus.FAILED, CommandStatus.CANCELED}:
                return record
            if record.status == CommandStatus.QUEUED:
                self._conn.execute(
                    """
                    UPDATE commands
                    SET status = ?, cancel_requested_at = ?, finished_at = ?, exit_code = ?, failure_code = ?
                    WHERE command_id = ?
                    """,
                    (CommandStatus.CANCELED.value, now, now, -15, "canceled", command_id),
                )
            else:
                self._conn.execute(
                    "UPDATE commands SET cancel_requested_at = ? WHERE command_id = ?",
                    (now, command_id),
                )
            updated = self._conn.execute("SELECT * FROM commands WHERE command_id = ?", (command_id,)).fetchone()
            assert updated is not None
        return self._row_to_command(dict(updated))

    def get_command(self, command_id: str) -> CommandRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM commands WHERE command_id = ?", (command_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_command(dict(row))

    def read_command_output(
        self,
        command_id: str,
        *,
        stdout_offset: int = 0,
        stderr_offset: int = 0,
    ) -> CommandOutputChunk:
        record = self.get_command(command_id)
        if record is None:
            raise KeyError(command_id)
        stdout = self._read_from_offset(self.command_output_path(command_id, "stdout"), stdout_offset)
        stderr = self._read_from_offset(self.command_output_path(command_id, "stderr"), stderr_offset)
        return CommandOutputChunk(
            command_id=command_id,
            status=record.status,
            stdout=stdout,
            stderr=stderr,
            stdout_offset=stdout_offset + len(stdout),
            stderr_offset=stderr_offset + len(stderr),
            exit_code=record.exit_code,
            failure_code=record.failure_code,
        )

    def create_shell_session(
        self,
        session_id: str,
        owner_user_id: str,
        request: ShellSessionCreateRequest,
        cwd_root: str,
    ) -> ShellSessionRecord:
        record = ShellSessionRecord(
            session_id=session_id,
            agent_id=request.agent_id,
            owner_user_id=owner_user_id,
            package_id=request.package_id,
            cwd_root=cwd_root,
            status=ShellSessionStatus.QUEUED,
            created_at=utc_now(),
        )
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO shell_sessions (
                    session_id, agent_id, owner_user_id, package_id, cwd_root, status, created_at,
                    started_at, finished_at, heartbeat_at, close_requested_at, exit_code, failure_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._shell_values(record),
            )
        return record

    def lease_next_shell_session(self, agent_id: str, owner_user_id: str) -> ShellSessionLease | None:
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT * FROM shell_sessions
                WHERE agent_id = ? AND owner_user_id = ? AND status = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (agent_id, owner_user_id, ShellSessionStatus.QUEUED.value),
            ).fetchone()
            if row is None:
                return None
            now = utc_now().isoformat()
            self._conn.execute(
                """
                UPDATE shell_sessions
                SET status = ?, started_at = ?, heartbeat_at = ?
                WHERE session_id = ?
                """,
                (ShellSessionStatus.RUNNING.value, now, now, row["session_id"]),
            )
            updated = self._conn.execute("SELECT * FROM shell_sessions WHERE session_id = ?", (row["session_id"],)).fetchone()
            assert updated is not None
        record = self._row_to_shell(dict(updated))
        return ShellSessionLease(
            session_id=record.session_id,
            agent_id=record.agent_id,
            package_id=record.package_id,
            cwd_root=record.cwd_root,
        )

    def heartbeat_shell_session(self, session_id: str) -> ShellSessionRecord:
        now = utc_now().isoformat()
        with self._lock, self._conn:
            updated = self._conn.execute(
                "UPDATE shell_sessions SET heartbeat_at = ? WHERE session_id = ?",
                (now, session_id),
            )
            if updated.rowcount == 0:
                raise KeyError(session_id)
            row = self._conn.execute("SELECT * FROM shell_sessions WHERE session_id = ?", (session_id,)).fetchone()
            assert row is not None
        return self._row_to_shell(dict(row))

    def append_shell_input(self, session_id: str, data: str) -> int:
        now = utc_now()
        with self._lock, self._conn:
            seq_row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM shell_inputs WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = int(seq_row["max_seq"]) + 1
            self._conn.execute(
                """
                INSERT INTO shell_inputs (session_id, seq, data, created_at, consumed_at)
                VALUES (?, ?, ?, ?, NULL)
                """,
                (session_id, seq, data, now.isoformat()),
            )
        return seq

    def consume_shell_inputs(self, session_id: str, after_seq: int) -> list[tuple[int, str]]:
        now = utc_now().isoformat()
        with self._lock, self._conn:
            rows = self._conn.execute(
                """
                SELECT seq, data FROM shell_inputs
                WHERE session_id = ? AND seq > ? AND consumed_at IS NULL
                ORDER BY seq ASC
                """,
                (session_id, after_seq),
            ).fetchall()
            if rows:
                self._conn.executemany(
                    "UPDATE shell_inputs SET consumed_at = ? WHERE session_id = ? AND seq = ?",
                    [(now, session_id, int(row["seq"])) for row in rows],
                )
        return [(int(row["seq"]), str(row["data"])) for row in rows]

    def append_shell_event(self, session_id: str, stream: str, data: str) -> ShellEvent:
        now = utc_now()
        with self._lock, self._conn:
            seq_row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM shell_events WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = int(seq_row["max_seq"]) + 1
            self._conn.execute(
                """
                INSERT INTO shell_events (session_id, seq, stream, data, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, seq, stream, data, now.isoformat()),
            )
        return ShellEvent(seq=seq, stream=stream, data=data, created_at=now)

    def get_shell_events(self, session_id: str, after_seq: int) -> list[ShellEvent]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT seq, stream, data, created_at FROM shell_events
                WHERE session_id = ? AND seq > ?
                ORDER BY seq ASC
                """,
                (session_id, after_seq),
            ).fetchall()
        return [
            ShellEvent(
                seq=int(row["seq"]),
                stream=str(row["stream"]),
                data=str(row["data"]),
                created_at=_parse_dt(row["created_at"]),
            )
            for row in rows
        ]

    def close_shell_session(self, session_id: str) -> ShellSessionRecord:
        now = utc_now().isoformat()
        with self._lock, self._conn:
            updated = self._conn.execute(
                "UPDATE shell_sessions SET close_requested_at = ? WHERE session_id = ?",
                (now, session_id),
            )
            if updated.rowcount == 0:
                raise KeyError(session_id)
            row = self._conn.execute("SELECT * FROM shell_sessions WHERE session_id = ?", (session_id,)).fetchone()
            assert row is not None
        return self._row_to_shell(dict(row))

    def complete_shell_session(self, session_id: str, request: ShellCompletionRequest) -> ShellSessionRecord:
        finished = utc_now().isoformat()
        with self._lock, self._conn:
            updated = self._conn.execute(
                """
                UPDATE shell_sessions
                SET status = ?, finished_at = ?, exit_code = ?, failure_code = ?
                WHERE session_id = ?
                """,
                (request.status.value, finished, request.exit_code, request.failure_code, session_id),
            )
            if updated.rowcount == 0:
                raise KeyError(session_id)
            row = self._conn.execute("SELECT * FROM shell_sessions WHERE session_id = ?", (session_id,)).fetchone()
            assert row is not None
        return self._row_to_shell(dict(row))

    def get_shell_session(self, session_id: str) -> ShellSessionRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM shell_sessions WHERE session_id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_shell(dict(row))

    def list_shell_sessions(
        self,
        owner_user_id: str,
        *,
        agent_id: str | None = None,
        session_status: ShellSessionStatus | None = None,
    ) -> list[ShellSessionRecord]:
        query = "SELECT * FROM shell_sessions WHERE owner_user_id = ?"
        params: list[str] = [owner_user_id]
        if agent_id is not None:
            query += " AND agent_id = ?"
            params.append(agent_id)
        if session_status is not None:
            query += " AND status = ?"
            params.append(session_status.value)
        query += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_shell(dict(row)) for row in rows]

    def package_path(self, package_id: str) -> Path:
        return self.packages_root / f"{package_id}.tar.gz"

    def command_output_path(self, command_id: str, stream: str) -> Path:
        return self.commands_root / f"{command_id}.{stream}"

    @staticmethod
    def _read_from_offset(path: Path, offset: int) -> str:
        if not path.exists():
            return ""
        with path.open("r", encoding="utf-8") as handle:
            handle.seek(offset)
            return handle.read()

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _command_values(record: CommandRecord) -> tuple:
        return (
            record.command_id,
            record.agent_id,
            record.owner_user_id,
            record.package_id,
            json.dumps(record.argv),
            json.dumps(record.env_patch),
            record.timeout_sec,
            record.working_dir,
            record.status.value,
            record.created_at.isoformat(),
            record.started_at.isoformat() if record.started_at else None,
            record.finished_at.isoformat() if record.finished_at else None,
            record.heartbeat_at.isoformat() if record.heartbeat_at else None,
            record.cancel_requested_at.isoformat() if record.cancel_requested_at else None,
            record.exit_code,
            record.failure_code,
            record.stdout_path,
            record.stderr_path,
        )

    @staticmethod
    def _shell_values(record: ShellSessionRecord) -> tuple:
        return (
            record.session_id,
            record.agent_id,
            record.owner_user_id,
            record.package_id,
            record.cwd_root,
            record.status.value,
            record.created_at.isoformat(),
            record.started_at.isoformat() if record.started_at else None,
            record.finished_at.isoformat() if record.finished_at else None,
            record.heartbeat_at.isoformat() if record.heartbeat_at else None,
            record.close_requested_at.isoformat() if record.close_requested_at else None,
            record.exit_code,
            record.failure_code,
        )

    @staticmethod
    def _row_to_user(row: dict) -> UserRecord:
        return UserRecord(user_id=row["user_id"], created_at=_parse_dt(row["created_at"]))

    @staticmethod
    def _row_to_agent(row: dict) -> AgentRecord:
        return AgentRecord(
            agent_id=row["agent_id"],
            owner_user_id=row["owner_user_id"],
            created_at=_parse_dt(row["created_at"]),
            last_seen_at=_parse_dt(row["last_seen_at"]),
        )

    @staticmethod
    def _row_to_package(row: dict) -> PackageRecord:
        return PackageRecord(
            package_id=row["package_id"],
            size=int(row["size"]),
            created_at=_parse_dt(row["created_at"]),
        )

    @staticmethod
    def _row_to_command(row: dict) -> CommandRecord:
        return CommandRecord(
            command_id=row["command_id"],
            agent_id=row["agent_id"],
            owner_user_id=row["owner_user_id"],
            package_id=row["package_id"],
            argv=json.loads(row["argv"]),
            env_patch=json.loads(row["env_patch"] or "{}"),
            timeout_sec=int(row["timeout_sec"]),
            working_dir=row["working_dir"],
            status=CommandStatus(row["status"]),
            created_at=_parse_dt(row["created_at"]),
            started_at=_parse_dt(row["started_at"]),
            finished_at=_parse_dt(row["finished_at"]),
            heartbeat_at=_parse_dt(row["heartbeat_at"]),
            cancel_requested_at=_parse_dt(row["cancel_requested_at"]),
            exit_code=row["exit_code"],
            failure_code=row["failure_code"],
            stdout_path=row["stdout_path"],
            stderr_path=row["stderr_path"],
        )

    @staticmethod
    def _row_to_shell(row: dict) -> ShellSessionRecord:
        return ShellSessionRecord(
            session_id=row["session_id"],
            agent_id=row["agent_id"],
            owner_user_id=row["owner_user_id"],
            package_id=row["package_id"],
            cwd_root=row["cwd_root"],
            status=ShellSessionStatus(row["status"]),
            created_at=_parse_dt(row["created_at"]),
            started_at=_parse_dt(row["started_at"]),
            finished_at=_parse_dt(row["finished_at"]),
            heartbeat_at=_parse_dt(row["heartbeat_at"]),
            close_requested_at=_parse_dt(row["close_requested_at"]),
            exit_code=row["exit_code"],
            failure_code=row["failure_code"],
        )


def _parse_dt(value: str | None):
    if value is None:
        return None
    return datetime.fromisoformat(value)
