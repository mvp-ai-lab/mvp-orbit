from __future__ import annotations

from mvp_orbit.cli.main import build_parser, main, prepare_args
from mvp_orbit.config import load_config


def test_init_hub_writes_default_config(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("ORBIT_CONFIG", str(config_path))

    answers = iter(
        [
            "GeoffreyChen777",
            "mvp-orbit-relay",
            "",
            "",
            "0.0.0.0",
            "10551",
            "",
            "http://127.0.0.1:10551",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    try:
        main(["init", "hub"])
    except SystemExit as exc:
        assert exc.code == 0

    _, config = load_config(config_path)
    assert config.github.owner == "GeoffreyChen777"
    assert config.github.repo == "mvp-orbit-relay"
    assert config.hub.host == "0.0.0.0"
    assert config.hub.port == 10551
    assert config.hub.url == "http://127.0.0.1:10551"
    assert config.auth.api_token
    assert config.auth.ticket_secret
    assert config.task_signing.private_key_b64
    assert config.task_signing.public_key_b64

    output = capsys.readouterr().out
    assert str(config_path) in output


def test_prepare_args_uses_config_defaults(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[github]
owner = "GeoffreyChen777"
repo = "mvp-orbit-relay"
release_prefix = "mvp-orbit"

[hub]
url = "http://127.0.0.1:10551"

[auth]
api_token = "api-token"

[task_signing]
private_key_b64 = "private-key"

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
                "run",
                "submit",
                "--task-id",
                "task-1",
            ]
        ),
    )
    assert args.hub_url == "http://127.0.0.1:10551"
    assert args.api_token == "api-token"
    assert args.agent_id == "agent-a"

    task_args = prepare_args(
        parser,
        parser.parse_args(
            [
                "task",
                "upload",
                "--package-id",
                "pkg-1",
                "--command-id",
                "cmd-1",
            ]
        ),
    )
    assert task_args.private_key == "private-key"
    assert task_args.github_owner == "GeoffreyChen777"
    assert task_args.github_repo == "mvp-orbit-relay"
