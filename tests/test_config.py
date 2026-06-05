from __future__ import annotations

from argparse import Namespace
from datetime import datetime
import json
import logging

from fastapi.testclient import TestClient

from mvp_orbit.cli.main import _command_create_request, build_parser, main, prepare_args
from mvp_orbit.config import load_config
from mvp_orbit.core.logging import OrbitFormatter, log_kv
from mvp_orbit.hub.app import create_app
from mvp_orbit.hub.store import HubStore


def _write_runtime_config(path):
    expires_at = "2027-03-18T12:00:00+00:00"
    path.write_text(
        f"""
[hub]
url = "http://127.0.0.1:10551"

[auth]
member_token = "user-token"
expires_at = "{expires_at}"

[client]
id = "client-a"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return expires_at


def test_prepare_args_uses_new_command_defaults(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    expires_at = _write_runtime_config(config_path)
    monkeypatch.setenv("ORBIT_CONFIG", str(config_path))
    parser = build_parser()

    exec_args = prepare_args(parser, parser.parse_args(["exec", "client-b", "--", "python3", "-V"]))
    assert exec_args.hub_url == "http://127.0.0.1:10551"
    assert exec_args.member_token == "user-token"
    assert exec_args.token_expires_at == expires_at
    assert exec_args.target == "client-b"

    put_args = prepare_args(parser, parser.parse_args(["put", "client-b", "local.txt", "remote.txt"]))
    assert put_args.hub_url == "http://127.0.0.1:10551"
    assert put_args.member_token == "user-token"


def test_top_level_help_contains_only_new_commands(capsys):
    parser = build_parser()
    try:
        parser.parse_args(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    output = capsys.readouterr().out
    assert "{host,join,join-requests,approve,reject,peers,exec,sh,put,get}" in output
    assert "cmd" not in output
    assert "package" not in output
    assert "connect" not in output


def test_join_writes_config_and_starts_client_loop(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("ORBIT_CONFIG", str(config_path))
    store = HubStore(tmp_path / "hub.sqlite3", tmp_path / "objects")
    client = TestClient(create_app(store=store))

    class _ClientWrapper:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            return self.inner

        def __exit__(self, exc_type, exc, tb):
            return False

    started = []
    monkeypatch.setattr("mvp_orbit.cli.main.httpx.Client", lambda timeout=20: _ClientWrapper(client))
    monkeypatch.setattr("mvp_orbit.cli.main._run_client_loop", lambda config: started.append(config.client.id) or 0)

    assert main(["join", "--host", "http://127.0.0.1:10551", "--alias", "client-a", "--channel", "team-a"]) == 0

    _, config = load_config(config_path)
    assert config.hub.url == "http://127.0.0.1:10551"
    assert config.client.id == "client-a"
    assert config.auth.member_token
    assert started == ["client-a"]
    assert json.loads(capsys.readouterr().out)["started"] is True


def test_exec_follows_output_by_default(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    _write_runtime_config(config_path)
    monkeypatch.setenv("ORBIT_CONFIG", str(config_path))

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
            assert json["client_id"] == "client-b"
            assert json["argv"] == ["python3", "-V"]
            return _Response()

    followed: list[tuple[str, str, str]] = []
    monkeypatch.setattr("mvp_orbit.cli.main.httpx.Client", lambda timeout=60: _ClientWrapper())
    monkeypatch.setattr(
        "mvp_orbit.cli.main._follow_command_output",
        lambda hub_url, member_token, command_id: (
            followed.append((hub_url, member_token, command_id))
            or {"status": "succeeded", "exit_code": 0, "failure_code": None}
        ),
    )

    assert main(["exec", "client-b", "--", "python3", "-V"]) == 0
    assert followed == [("http://127.0.0.1:10551", "user-token", "cmd-123")]


def test_command_create_request_auto_wraps_single_shell_string():
    request = _command_create_request(
        Namespace(
            client_id="client-b",
            command_argv=["cd /cache/models/ && echo ok"],
            env_file=None,
            timeout_sec=3600,
            working_dir=".",
            shell=False,
        )
    )
    assert request.argv == ["/bin/sh", "-lc", "cd /cache/models/ && echo ok"]


def test_command_create_request_supports_explicit_shell_mode():
    request = _command_create_request(
        Namespace(
            client_id="client-b",
            command_argv=["cd", "/cache/models/", "&&", "echo", "ok"],
            env_file=None,
            timeout_sec=3600,
            working_dir=".",
            shell=True,
        )
    )
    assert request.argv == ["/bin/sh", "-lc", "cd /cache/models/ && echo ok"]


def test_orbit_log_formatter_outputs_compact_key_value_message(caplog):
    logger = logging.getLogger("mvp_orbit.client.runtime")
    formatter = OrbitFormatter(component="client", color=False)
    with caplog.at_level(logging.INFO, logger="mvp_orbit.client.runtime"):
        log_kv(logger, logging.INFO, "command.start", client_id="client-a", argv="python3 -V")
    assert caplog.records
    line = formatter.format(caplog.records[-1])
    assert line.startswith("[")
    assert "] INFO" in line
    assert " client │ " in line
    assert "client.runtime" in line
    assert " │ command.start" in line
    assert "client_id=client-a" in line
    assert 'argv="python3 -V"' in line
