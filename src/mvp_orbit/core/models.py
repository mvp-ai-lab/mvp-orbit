from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mvp_orbit.core.canonical import object_id_for_json


class ObjectNamespace(str, Enum):
    PACKAGE = "package"
    COMMAND = "command"
    TASK = "task"
    LOG = "log"
    RESULT = "result"
    ARTIFACT = "artifact"


class CommandObject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[str] = Field(min_length=1)
    env_patch: dict[str, str] = Field(default_factory=dict)
    timeout_sec: int = Field(default=3600, ge=1, le=86400)
    working_dir: str = "."


class TaskObject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_version: int = 1
    package_id: str = Field(min_length=8)
    command_id: str = Field(min_length=8)
    constraints: dict[str, Any] = Field(default_factory=dict)
    created_by: str | None = None
    created_at: datetime


class SignedTaskObject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    task: TaskObject
    task_signature: str
    signer: str | None = None

    def computed_task_id(self) -> str:
        return object_id_for_json(self.task.model_dump(mode="json", exclude_none=True))


class LogObject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stream: str = Field(pattern="^(stdout|stderr)$")
    data: str
    captured_at: datetime


class ResultObject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    exit_code: int
    started_at: datetime
    finished_at: datetime


class RunStatus(str, Enum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    CANCELED = "canceled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1)
    task_id: str = Field(min_length=8)


class RunCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    agent_id: str
    task_id: str
    run_ticket: str
    expires_at: datetime


class RunLease(BaseModel):
    model_config = ConfigDict(extra="ignore")

    run_id: str
    agent_id: str
    task_id: str
    run_ticket: str
    expires_at: datetime


class TicketPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    agent_id: str
    task_id: str
    nonce: str
    issued_at: datetime
    expires_at: datetime


class RunHeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: str = Field(default="running", pattern="^(preparing|running)$")


class RunHeartbeatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "accepted"
    cancel_requested: bool = False


class RunCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: RunStatus
    log_ids: list[str] = Field(default_factory=list)
    result_id: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    failure_code: str | None = None

    @field_validator("status")
    @classmethod
    def validate_final_status(cls, value: RunStatus) -> RunStatus:
        if value not in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.REJECTED, RunStatus.CANCELED}:
            raise ValueError("completion status must be a final state")
        return value


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    agent_id: str
    task_id: str
    run_ticket: str
    expires_at: datetime
    status: RunStatus
    created_at: datetime
    leased_at: datetime | None = None
    heartbeat_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    completed_at: datetime | None = None
    log_ids: list[str] = Field(default_factory=list)
    result_id: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    failure_code: str | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_run_id() -> str:
    return f"run-{uuid4().hex}"


def default_ticket_expiry(seconds: int = 120) -> datetime:
    return utc_now() + timedelta(seconds=seconds)
