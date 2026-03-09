from __future__ import annotations

import base64
import json
import os
import secrets
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from mvp_orbit.core.signing import generate_keypair_b64, public_key_from_private_key_b64

DEFAULT_CONFIG_PATH = Path("~/.config/mvp-orbit/config.toml").expanduser()
NODE_SHARED_CONFIG_PREFIX = "orbit-node:"
NODE_SHARED_CONFIG_VERSION = 1


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "github"


class GitHubConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: str | None = None
    repo: str | None = None
    release_prefix: str = "mvp-orbit"
    gh_bin: str = "gh"


class HuggingFaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: str | None = None
    repo_type: str = "dataset"
    path_prefix: str = "mvp-orbit"
    hf_bin: str = "hf"
    private: bool = True
    token: str | None = None


class HubConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 8080
    db: str = "./.orbit-hub/runs.sqlite3"
    url: str | None = None

    def resolved_url(self) -> str:
        return self.url or f"http://{self.host}:{self.port}"


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_token: str | None = None
    ticket_secret: str | None = None


class TaskSigningConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_key_b64: str | None = None
    private_key_b64: str | None = None


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    workspace_root: str = "./.orbit-workspaces"
    poll_interval_sec: float = 5.0
    heartbeat_interval_sec: float = 5.0


class OrbitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage: StorageConfig = Field(default_factory=StorageConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    huggingface: HuggingFaceConfig = Field(default_factory=HuggingFaceConfig)
    hub: HubConfig = Field(default_factory=HubConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    task_signing: TaskSigningConfig = Field(default_factory=TaskSigningConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)


def resolve_config_path(path: str | Path | None = None) -> Path:
    if path is None:
        path = os.getenv("ORBIT_CONFIG")
    if path is None:
        return DEFAULT_CONFIG_PATH
    return Path(path).expanduser()


def load_config(path: str | Path | None = None) -> tuple[Path, OrbitConfig]:
    resolved = resolve_config_path(path)
    if not resolved.exists():
        return resolved, OrbitConfig()
    payload = tomllib.loads(resolved.read_text(encoding="utf-8"))
    return resolved, OrbitConfig.model_validate(payload)


def save_config(config: OrbitConfig, path: str | Path | None = None) -> Path:
    resolved = resolve_config_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(render_config(config), encoding="utf-8")
    try:
        os.chmod(resolved, 0o600)
    except OSError:
        pass
    return resolved


def ensure_hub_secrets(config: OrbitConfig) -> OrbitConfig:
    if not config.auth.ticket_secret:
        config.auth.ticket_secret = secrets.token_urlsafe(48)
    if not config.auth.api_token:
        config.auth.api_token = secrets.token_urlsafe(32)
    if not config.task_signing.private_key_b64 or not config.task_signing.public_key_b64:
        private_key, public_key = generate_keypair_b64()
        config.task_signing.private_key_b64 = private_key
        config.task_signing.public_key_b64 = public_key
    return config


def encode_node_shared_config(config: OrbitConfig) -> str:
    payload = {
        "version": NODE_SHARED_CONFIG_VERSION,
        "storage": config.storage.model_dump(mode="json", exclude_none=True),
        "github": config.github.model_dump(mode="json", exclude_none=True),
        "huggingface": config.huggingface.model_dump(mode="json", exclude_none=True),
        "hub": {"url": config.hub.resolved_url()},
        "auth": {
            "api_token": config.auth.api_token,
            "ticket_secret": config.auth.ticket_secret,
        },
        "task_signing": {
            "private_key_b64": config.task_signing.private_key_b64,
        },
    }
    missing = [
        name
        for name, value in (
            ("hub.url", payload["hub"]["url"]),
            ("auth.api_token", payload["auth"]["api_token"]),
            ("auth.ticket_secret", payload["auth"]["ticket_secret"]),
            ("task_signing.private_key_b64", payload["task_signing"]["private_key_b64"]),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"cannot encode node shared config; missing fields: {', '.join(missing)}")
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii")
    return f"{NODE_SHARED_CONFIG_PREFIX}{encoded.rstrip('=')}"


def apply_node_shared_config(config: OrbitConfig, shared_config: str) -> OrbitConfig:
    if not shared_config.startswith(NODE_SHARED_CONFIG_PREFIX):
        raise ValueError(f"node shared config must start with {NODE_SHARED_CONFIG_PREFIX}")
    token = shared_config[len(NODE_SHARED_CONFIG_PREFIX) :]
    padding = "=" * (-len(token) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode((token + padding).encode("ascii")).decode("utf-8"))
    except Exception as exc:  # pragma: no cover - defensive parsing
        raise ValueError("invalid node shared config encoding") from exc
    if payload.get("version") != NODE_SHARED_CONFIG_VERSION:
        raise ValueError(f"unsupported node shared config version: {payload.get('version')!r}")

    config.storage = StorageConfig.model_validate(payload.get("storage") or {})
    config.github = GitHubConfig.model_validate(payload.get("github") or {})
    config.huggingface = HuggingFaceConfig.model_validate(payload.get("huggingface") or {})
    config.hub.url = HubConfig.model_validate(payload.get("hub") or {}).resolved_url()
    config.auth = AuthConfig.model_validate(payload.get("auth") or {})
    config.task_signing.private_key_b64 = TaskSigningConfig.model_validate(payload.get("task_signing") or {}).private_key_b64
    if not config.auth.api_token or not config.auth.ticket_secret or not config.task_signing.private_key_b64:
        raise ValueError("node shared config is missing required credentials")
    config.task_signing.public_key_b64 = public_key_from_private_key_b64(config.task_signing.private_key_b64)
    return config


def render_config(config: OrbitConfig) -> str:
    lines = [
        "# mvp-orbit configuration",
        "# Generated by `orbit init ...`.",
        "",
    ]
    sections = [
        ("storage", config.storage.model_dump(mode="json", exclude_none=True)),
        ("github", config.github.model_dump(mode="json", exclude_none=True)),
        ("huggingface", config.huggingface.model_dump(mode="json", exclude_none=True)),
        ("hub", config.hub.model_dump(mode="json", exclude_none=True)),
        ("auth", config.auth.model_dump(mode="json", exclude_none=True)),
        ("task_signing", config.task_signing.model_dump(mode="json", exclude_none=True)),
        ("agent", config.agent.model_dump(mode="json", exclude_none=True)),
    ]
    for name, values in sections:
        if not values:
            continue
        lines.append(f"[{name}]")
        for key, value in values.items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return json.dumps(str(value))
