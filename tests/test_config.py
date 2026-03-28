from __future__ import annotations

from argparse import Namespace
from datetime import datetime, timedelta, timezone
import json

from fastapi.testclient import TestClient

from mvp_orbit.cli.main import (
    _command_create_request,
    _decode_agent_config_string,
    _encode_agent_config_string,
    build_parser,
    main,
    prepare_args,
)
from mvp_orbit.config import OrbitConfig, ensure_bootstrap_token, load_config
from mvp_orbit.hub.app import create_app
from mvp_orbit.hub.store import HubStore


BOOTSTRAP_TOKEN = "bootstrap-token"


def test_init_hub_writes_default_config(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("ORBIT_CONFIG", str(config_path))
    answers = iter(["0.0.0.0", "10551", "", "", "http://127.0.0.1:10551", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    assert main(["init", "hub"]) == 0

    _, config = load_config(config_path)
    assert config.hub.host == "0.0.0.0"
    assert config.hub.port == 10551
    assert config.hub.url == "http://127.0.0.1:10551"
    assert config.auth.bootstrap_token

    output = capsys.readouterr().out
    assert "ORBIT HUB SETUP" in output
    assert "ORBIT_BOOTSTRAP_TOKEN=" in output


def test_init_node_writes_user_token_and_expiry(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("ORBIT_CONFIG", str(config_path))
    expires_at = "2026-03-18T12:00:00+00:00"
    answers = iter(["", "agent-a", "http://127.0.0.1:10551", "user-token", expires_at, ""])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    assert main(["init", "node"]) == 0

    _, config = load_config(config_path)
    assert config.agent.id == "agent-a"
    assert config.hub.url == "http://127.0.0.1:10551"
    assert config.auth.user_token == "user-token"
    assert config.auth.expires_at == datetime.fromisoformat(expires_at)


def test_connect_writes_user_token_and_expiry(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("ORBIT_CONFIG", str(config_path))
    store = HubStore(tmp_path / "hub.sqlite3", tmp_path / "objects")
    client = TestClient(create_app(store=store, bootstrap_token=BOOTSTRAP_TOKEN))

    class _ClientWrapper:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            return self.inner

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("mvp_orbit.cli.main.httpx.Client", lambda timeout=20: _ClientWrapper(client))
    answers = iter(["http://127.0.0.1:10551", "alice", BOOTSTRAP_TOKEN, ""])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    assert main(["connect", "--hub-url", "http://127.0.0.1:10551"]) == 0

    _, config = load_config(config_path)
    assert config.hub.url == "http://127.0.0.1:10551"
    assert config.auth.user_token
    assert config.auth.expires_at is not None


def test_connect_prints_agent_config_string(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("ORBIT_CONFIG", str(config_path))
    store = HubStore(tmp_path / "hub.sqlite3", tmp_path / "objects")
    client = TestClient(create_app(store=store, bootstrap_token=BOOTSTRAP_TOKEN))

    class _ClientWrapper:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            return self.inner

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("mvp_orbit.cli.main.httpx.Client", lambda timeout=20: _ClientWrapper(client))
    answers = iter(["http://127.0.0.1:10551", "alice", BOOTSTRAP_TOKEN, "/srv/work"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    assert main(["connect", "--hub-url", "http://127.0.0.1:10551"]) == 0

    output = capsys.readouterr().out
    config_string_line = next(line for line in output.splitlines() if line.startswith("ORBIT_AGENT_CONFIG_STRING="))
    payload = _decode_agent_config_string(config_string_line.removeprefix("ORBIT_AGENT_CONFIG_STRING="))
    assert payload["hub_url"] == "http://127.0.0.1:10551"
    assert "agent_id" not in payload
    assert payload["workspace_root"] == "/srv/work"


def test_init_node_config_string_writes_full_agent_config(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("ORBIT_CONFIG", str(config_path))
    config_string = _encode_agent_config_string(
        hub_url="http://127.0.0.1:10551",
        user_token="user-token",
        expires_at="2026-03-18T12:00:00+00:00",
        workspace_root="/srv/work",
    )

    assert main(["init", "node", "--config-string", config_string, "--agent-id", "agent-a"]) == 0

    _, config = load_config(config_path)
    assert config.agent.id == "agent-a"
    assert config.hub.url == "http://127.0.0.1:10551"
    assert config.auth.user_token == "user-token"
    assert config.auth.expires_at == datetime.fromisoformat("2026-03-18T12:00:00+00:00")
    assert config.agent.workspace_root == "/srv/work"


def test_prepare_args_uses_config_defaults(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    expires_at = "2026-03-18T12:00:00+00:00"
    config_path.write_text(
        f"""
[hub]
url = "http://127.0.0.1:10551"

[auth]
user_token = "user-token"
expires_at = "{expires_at}"

[agent]
id = "agent-a"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ORBIT_CONFIG", str(config_path))
    parser = build_parser()

    exec_args = prepare_args(
        parser,
        parser.parse_args(
            [
                "command",
                "exec",
                "python3",
                "-c",
                "print('x')",
            ]
        ),
    )
    assert exec_args.hub_url == "http://127.0.0.1:10551"
    assert exec_args.user_token == "user-token"
    assert exec_args.token_expires_at == expires_at
    assert exec_args.agent_id == "agent-a"

    package_args = prepare_args(
        parser,
        parser.parse_args(
            [
                "package",
                "upload",
                "--source-dir",
                ".",
            ]
        ),
    )
    assert package_args.hub_url == "http://127.0.0.1:10551"
    assert package_args.user_token == "user-token"
    assert package_args.token_expires_at == expires_at


def test_prepare_args_accepts_cmd_alias(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    expires_at = "2026-03-18T12:00:00+00:00"
    config_path.write_text(
        f"""
[hub]
url = "http://127.0.0.1:10551"

[auth]
user_token = "user-token"
expires_at = "{expires_at}"

[agent]
id = "agent-a"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ORBIT_CONFIG", str(config_path))
    parser = build_parser()

    args = prepare_args(
        parser,
        parser.parse_args(
            [
                "cmd",
                "exec",
                "python3",
                "-V",
            ]
        ),
    )

    assert args.command == "command"
    assert args.command_command == "exec"
    assert args.hub_url == "http://127.0.0.1:10551"
    assert args.user_token == "user-token"
    assert args.token_expires_at == expires_at
    assert args.agent_id == "agent-a"


def test_command_exec_follows_output_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CONFIG", str(tmp_path / "config.toml"))

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"command_id": "cmd-123"}

    class _ClientWrapper:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, headers=None, json=None):
            return _Response()

    followed: list[tuple[str, str, str]] = []

    monkeypatch.setattr("mvp_orbit.cli.main.httpx.Client", lambda timeout=60: _ClientWrapper())
    monkeypatch.setattr(
        "mvp_orbit.cli.main._follow_command_output",
        lambda hub_url, user_token, command_id: (
            followed.append((hub_url, user_token, command_id))
            or {"status": "succeeded", "exit_code": 0, "failure_code": None}
        ),
    )

    result = main(
        [
            "cmd",
            "exec",
            "--hub-url",
            "http://127.0.0.1:10551",
            "--user-token",
            "user-token",
            "--token-expires-at",
            "2027-03-18T12:00:00+00:00",
            "--agent-id",
            "agent-a",
            "python3",
            "-V",
        ]
    )

    assert result == 0
    assert followed == [("http://127.0.0.1:10551", "user-token", "cmd-123")]


def test_command_exec_detach_skips_follow_and_prints_payload(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("ORBIT_CONFIG", str(tmp_path / "config.toml"))

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"command_id": "cmd-123", "status": "queued"}

    class _ClientWrapper:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, headers=None, json=None):
            return _Response()

    followed: list[str] = []

    monkeypatch.setattr("mvp_orbit.cli.main.httpx.Client", lambda timeout=60: _ClientWrapper())
    monkeypatch.setattr(
        "mvp_orbit.cli.main._follow_command_output",
        lambda hub_url, user_token, command_id: followed.append(command_id),
    )

    result = main(
        [
            "cmd",
            "exec",
            "--detach",
            "--hub-url",
            "http://127.0.0.1:10551",
            "--user-token",
            "user-token",
            "--token-expires-at",
            "2027-03-18T12:00:00+00:00",
            "--agent-id",
            "agent-a",
            "python3",
            "-V",
        ]
    )

    assert result == 0
    assert followed == []
    assert json.loads(capsys.readouterr().out) == {"command_id": "cmd-123", "status": "queued"}


def test_command_exec_returns_remote_failure_code_and_prints_summary(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("ORBIT_CONFIG", str(tmp_path / "config.toml"))

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"command_id": "cmd-123"}

    class _ClientWrapper:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, headers=None, json=None):
            return _Response()

    monkeypatch.setattr("mvp_orbit.cli.main.httpx.Client", lambda timeout=60: _ClientWrapper())
    monkeypatch.setattr(
        "mvp_orbit.cli.main._follow_command_output",
        lambda hub_url, user_token, command_id: {"status": "failed", "exit_code": 7, "failure_code": None},
    )

    result = main(
        [
            "cmd",
            "exec",
            "--hub-url",
            "http://127.0.0.1:10551",
            "--user-token",
            "user-token",
            "--token-expires-at",
            "2027-03-18T12:00:00+00:00",
            "--agent-id",
            "agent-a",
            "python3",
            "-V",
        ]
    )

    assert result == 7
    assert "[orbit] command cmd-123 failed exit=7" in capsys.readouterr().err


def test_command_output_follow_returns_timeout_code_and_prints_summary(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("ORBIT_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(
        "mvp_orbit.cli.main._follow_command_output",
        lambda hub_url, user_token, command_id: {"status": "failed", "exit_code": -15, "failure_code": "timeout"},
    )

    result = main(
        [
            "cmd",
            "output",
            "--follow",
            "--hub-url",
            "http://127.0.0.1:10551",
            "--user-token",
            "user-token",
            "--token-expires-at",
            "2027-03-18T12:00:00+00:00",
            "--command-id",
            "cmd-123",
        ]
    )

    assert result == 124
    assert "[orbit] command cmd-123 failed exit=-15 reason=timeout" in capsys.readouterr().err


def test_ensure_bootstrap_token_populates_config():
    config = OrbitConfig()
    ensure_bootstrap_token(config)
    assert config.auth.bootstrap_token


def test_command_create_request_auto_wraps_single_shell_string():
    request = _command_create_request(
        Namespace(
            agent_id="agent-a",
            package_id=None,
            command_argv=["cd /cache/models/ && HF_TOKEN=token hf download repo --local-dir out"],
            env_file=None,
            timeout_sec=3600,
            working_dir=".",
            shell=False,
        )
    )
    assert request.argv == [
        "/bin/sh",
        "-lc",
        "cd /cache/models/ && HF_TOKEN=token hf download repo --local-dir out",
    ]


def test_command_create_request_supports_explicit_shell_mode():
    request = _command_create_request(
        Namespace(
            agent_id="agent-a",
            package_id=None,
            command_argv=["cd", "/cache/models/", "&&", "echo", "ok"],
            env_file=None,
            timeout_sec=3600,
            working_dir=".",
            shell=True,
        )
    )
    assert request.argv == ["/bin/sh", "-lc", "cd /cache/models/ && echo ok"]


def test_connect_token_expires_in_roughly_seven_days(tmp_path):
    store = HubStore(tmp_path / "hub.sqlite3", tmp_path / "objects")
    client = TestClient(create_app(store=store, bootstrap_token=BOOTSTRAP_TOKEN))

    before = datetime.now(timezone.utc)
    response = client.post(
        "/api/connect",
        json={"user_id": "alice"},
        headers={"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"},
    )
    after = datetime.now(timezone.utc)

    assert response.status_code == 200
    expires_at = datetime.fromisoformat(response.json()["expires_at"])
    assert before + timedelta(days=7) <= expires_at + timedelta(seconds=1)
    assert expires_at <= after + timedelta(days=7, seconds=1)
