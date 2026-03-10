from __future__ import annotations

from mvp_orbit.cli.main import build_parser, main, prepare_args
from mvp_orbit.config import OrbitConfig, apply_node_shared_config, encode_node_shared_config, ensure_hub_token, load_config


def test_init_hub_writes_default_config(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("ORBIT_CONFIG", str(config_path))
    answers = iter(["0.0.0.0", "10551", "", "", "http://127.0.0.1:10551"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    assert main(["init", "hub"]) == 0

    _, config = load_config(config_path)
    assert config.hub.host == "0.0.0.0"
    assert config.hub.port == 10551
    assert config.hub.url == "http://127.0.0.1:10551"
    assert config.auth.api_token

    output = capsys.readouterr().out
    assert "ORBIT HUB SETUP" in output
    assert "ORBIT_NODE_SHARED_CONFIG=" in output
    assert "ORBIT_API_TOKEN=" in output


def test_node_shared_config_round_trip():
    config = OrbitConfig()
    config.hub.url = "http://127.0.0.1:10551"
    ensure_hub_token(config)

    shared = encode_node_shared_config(config)
    restored = apply_node_shared_config(OrbitConfig(), shared)

    assert restored.hub.url == "http://127.0.0.1:10551"
    assert restored.auth.api_token == config.auth.api_token


def test_init_node_accepts_shared_config(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("ORBIT_CONFIG", str(config_path))

    base = OrbitConfig()
    base.hub.url = "http://127.0.0.1:10551"
    ensure_hub_token(base)
    shared = encode_node_shared_config(base)

    answers = iter(["agent-a", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    assert main(["init", "node", "--shared-config", shared]) == 0

    _, config = load_config(config_path)
    assert config.agent.id == "agent-a"
    assert config.hub.url == "http://127.0.0.1:10551"
    assert config.auth.api_token == base.auth.api_token


def test_prepare_args_uses_config_defaults(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[hub]
url = "http://127.0.0.1:10551"

[auth]
api_token = "api-token"

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
    assert exec_args.api_token == "api-token"
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
    assert package_args.api_token == "api-token"

    shell_list_args = prepare_args(
        parser,
        parser.parse_args(
            [
                "shell",
                "list",
            ]
        ),
    )
    assert shell_list_args.hub_url == "http://127.0.0.1:10551"
    assert shell_list_args.api_token == "api-token"
