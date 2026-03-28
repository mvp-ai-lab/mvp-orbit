from __future__ import annotations

from typing import Any
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CommandStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class ShellSessionStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CLOSED = "closed"
    FAILED = "failed"


class PackageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_id: str = Field(min_length=8)
    size: int = Field(ge=0)
    created_at: datetime


class UserRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)
    created_at: datetime


class AgentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1)
    owner_user_id: str = Field(min_length=1)
    created_at: datetime
    last_seen_at: datetime | None = None


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)


class ConnectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)
    user_token: str = Field(min_length=1)
    expires_at: datetime


class CommandCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1)
    package_id: str | None = None
    argv: list[str] = Field(min_length=1)
    env_patch: dict[str, str] = Field(default_factory=dict)
    timeout_sec: int = Field(default=3600, ge=1, le=86400)
    working_dir: str = "."


class CommandRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str
    agent_id: str
    owner_user_id: str
    package_id: str | None = None
    argv: list[str]
    env_patch: dict[str, str] = Field(default_factory=dict)
    timeout_sec: int
    working_dir: str
    status: CommandStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    exit_code: int | None = None
    failure_code: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None


class CommandLease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str
    agent_id: str
    package_id: str | None = None
    argv: list[str]
    env_patch: dict[str, str] = Field(default_factory=dict)
    timeout_sec: int
    working_dir: str


class CommandCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: CommandStatus
    exit_code: int
    failure_code: str | None = None

    @field_validator("status")
    @classmethod
    def validate_final_status(cls, value: CommandStatus) -> CommandStatus:
        if value not in {CommandStatus.SUCCEEDED, CommandStatus.FAILED, CommandStatus.CANCELED}:
            raise ValueError("completion status must be final")
        return value


class CommandOutputAppendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stream: str = Field(pattern="^(stdout|stderr)$")
    data: str


class CommandOutputChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str
    status: CommandStatus
    stdout: str = ""
    stderr: str = ""
    stdout_offset: int = 0
    stderr_offset: int = 0
    exit_code: int | None = None
    failure_code: str | None = None


class EventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: int = Field(ge=1)
    kind: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AgentControlEvent(EventRecord):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1)


class AgentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentEventsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[AgentEvent] = Field(default_factory=list)


class ShellSessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1)
    package_id: str | None = None


class ShellSessionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    agent_id: str
    owner_user_id: str
    package_id: str | None = None
    cwd_root: str
    status: ShellSessionStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None
    close_requested_at: datetime | None = None
    exit_code: int | None = None
    failure_code: str | None = None


class ShellSessionLease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    agent_id: str
    package_id: str | None = None
    cwd_root: str


class ShellInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: str = Field(min_length=1)


class ShellResizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: int = Field(ge=1)
    cols: int = Field(ge=1)


class ShellEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    stream: str = Field(pattern="^(stdout|stderr|system)$")
    data: str
    created_at: datetime


class ShellEventAppendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stream: str = Field(pattern="^(stdout|stderr|system)$")
    data: str


class ShellEventsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    status: ShellSessionStatus
    events: list[ShellEvent] = Field(default_factory=list)
    next_seq: int = 1
    exit_code: int | None = None
    failure_code: str | None = None


class ShellCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ShellSessionStatus
    exit_code: int
    failure_code: str | None = None

    @field_validator("status")
    @classmethod
    def validate_final_status(cls, value: ShellSessionStatus) -> ShellSessionStatus:
        if value not in {ShellSessionStatus.CLOSED, ShellSessionStatus.FAILED}:
            raise ValueError("completion status must be final")
        return value


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_command_id() -> str:
    return f"cmd-{uuid4().hex}"


def default_shell_session_id() -> str:
    return f"shell-{uuid4().hex}"
