from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import Lock

from mvp_orbit.core.models import RunCompletionRequest, RunRecord, RunStatus, utc_now


class RunStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    run_ticket TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    leased_at TEXT,
                    heartbeat_at TEXT,
                    completed_at TEXT,
                    log_ids TEXT NOT NULL DEFAULT '[]',
                    result_id TEXT,
                    artifact_ids TEXT NOT NULL DEFAULT '[]',
                    failure_code TEXT
                )
                """
            )

    def create_run(self, record: RunRecord) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO runs (
                    run_id, agent_id, task_id,
                    run_ticket, expires_at, status, created_at,
                    leased_at, heartbeat_at, completed_at,
                    log_ids, result_id, artifact_ids, failure_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._row_values(record),
            )

    def lease_next(self, agent_id: str) -> RunRecord | None:
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT * FROM runs
                WHERE agent_id = ? AND status = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (agent_id, RunStatus.QUEUED.value),
            ).fetchone()
            if row is None:
                return None
            leased_at = utc_now()
            self._conn.execute(
                "UPDATE runs SET status = ?, leased_at = ? WHERE run_id = ?",
                (RunStatus.LEASED.value, leased_at.isoformat(), row["run_id"]),
            )
            row_dict = dict(row)
            row_dict["status"] = RunStatus.LEASED.value
            row_dict["leased_at"] = leased_at.isoformat()
            return self._row_to_record(row_dict)

    def heartbeat(self, run_id: str, *, phase: str) -> RunRecord:
        status = RunStatus.RUNNING if phase == "running" else RunStatus.LEASED
        heartbeat_at = utc_now()
        with self._lock, self._conn:
            updated = self._conn.execute(
                "UPDATE runs SET status = ?, heartbeat_at = ? WHERE run_id = ?",
                (status.value, heartbeat_at.isoformat(), run_id),
            )
            if updated.rowcount == 0:
                raise KeyError(run_id)
            row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            assert row is not None
            return self._row_to_record(dict(row))

    def complete(self, run_id: str, completion: RunCompletionRequest) -> RunRecord:
        completed_at = utc_now()
        with self._lock, self._conn:
            updated = self._conn.execute(
                """
                UPDATE runs
                SET status = ?, completed_at = ?, log_ids = ?, result_id = ?, artifact_ids = ?, failure_code = ?
                WHERE run_id = ?
                """,
                (
                    completion.status.value,
                    completed_at.isoformat(),
                    json.dumps(completion.log_ids),
                    completion.result_id,
                    json.dumps(completion.artifact_ids),
                    completion.failure_code,
                    run_id,
                ),
            )
            if updated.rowcount == 0:
                raise KeyError(run_id)
            row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            assert row is not None
            return self._row_to_record(dict(row))

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_record(dict(row))

    @staticmethod
    def _row_values(record: RunRecord) -> tuple:
        return (
            record.run_id,
            record.agent_id,
            record.task_id,
            record.run_ticket,
            record.expires_at.isoformat(),
            record.status.value,
            record.created_at.isoformat(),
            record.leased_at.isoformat() if record.leased_at else None,
            record.heartbeat_at.isoformat() if record.heartbeat_at else None,
            record.completed_at.isoformat() if record.completed_at else None,
            json.dumps(record.log_ids),
            record.result_id,
            json.dumps(record.artifact_ids),
            record.failure_code,
        )

    @staticmethod
    def _row_to_record(row: dict) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            agent_id=row["agent_id"],
            task_id=row["task_id"],
            run_ticket=row["run_ticket"],
            expires_at=_parse_dt(row["expires_at"]),
            status=RunStatus(row["status"]),
            created_at=_parse_dt(row["created_at"]),
            leased_at=_parse_dt(row["leased_at"]),
            heartbeat_at=_parse_dt(row["heartbeat_at"]),
            completed_at=_parse_dt(row["completed_at"]),
            log_ids=json.loads(row["log_ids"] or "[]"),
            result_id=row["result_id"],
            artifact_ids=json.loads(row["artifact_ids"] or "[]"),
            failure_code=row["failure_code"],
        )


def _parse_dt(value: str | None):
    if value is None:
        return None
    return datetime.fromisoformat(value)
