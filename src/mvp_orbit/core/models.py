from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_FILE_MAX_BYTES = 1024 * 1024


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


class FileTransferStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JoinRequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ChannelRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1)
    created_at: datetime


class ClientRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    created_at: datetime
    last_seen_at: datetime | None = None


class TokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1)
    member_token: str = Field(min_length=1)
    expires_at: datetime
    alias: str | None = None


class JoinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    channel: str = Field(min_length=1)


class JoinResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: JoinRequestStatus
    alias: str
    channel_id: str
    request_id: str | None = None
    member_token: str | None = None
    expires_at: datetime | None = None


class JoinApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    channel_id: str
    alias: str
    status: JoinRequestStatus
    requested_at: datetime
    approved_at: datetime | None = None
    approved_by: str | None = None
    rejected_at: datetime | None = None
    rejected_by: str | None = None


class CommandCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1)
    argv: list[str] = Field(min_length=1)
    env_patch: dict[str, str] = Field(default_factory=dict)
    timeout_sec: int = Field(default=3600, ge=1, le=86400)
    working_dir: str = "."


class CommandRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str
    client_id: str
    channel_id: str
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
    client_id: str
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


class ClientControlEvent(EventRecord):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1)


class ClientEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class ClientEventsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[ClientEvent] = Field(default_factory=list)


class ShellSessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1)


class ShellSessionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    client_id: str
    channel_id: str
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
    client_id: str
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


class FilePushRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1)
    remote_path: str = Field(min_length=1)
    data_b64: str = Field(min_length=1)
    max_bytes: int = Field(default=DEFAULT_FILE_MAX_BYTES, ge=1)


class FilePullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1)
    remote_path: str = Field(min_length=1)
    max_bytes: int = Field(default=DEFAULT_FILE_MAX_BYTES, ge=1)


class FileTransferRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transfer_id: str
    client_id: str
    channel_id: str
    direction: str = Field(pattern="^(push|pull)$")
    remote_path: str
    size: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=DEFAULT_FILE_MAX_BYTES, ge=1)
    status: FileTransferStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failure_code: str | None = None
    data_b64: str | None = None


class FileTransferResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transfer_id: str
    status: FileTransferStatus
    direction: str = Field(pattern="^(push|pull)$")
    remote_path: str
    size: int = Field(default=0, ge=0)
    data_b64: str | None = None
    failure_code: str | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_command_id() -> str:
    return f"cmd-{uuid4().hex}"


def default_shell_session_id() -> str:
    return f"shell-{uuid4().hex}"


def default_file_transfer_id() -> str:
    return f"file-{uuid4().hex}"


def default_join_request_id() -> str:
    return f"join-{uuid4().hex}"
