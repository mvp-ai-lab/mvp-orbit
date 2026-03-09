from __future__ import annotations

from mvp_orbit.cli.main import SetupWizard, build_parser, main, prepare_args
from mvp_orbit.config import OrbitConfig, apply_node_shared_config, encode_node_shared_config, ensure_hub_secrets, load_config
from mvp_orbit.core.signing import public_key_from_private_key_b64


def test_init_hub_writes_default_config(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("ORBIT_CONFIG", str(config_path))

    answers = iter(
        [
            "github",
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
    assert "ORBIT HUB SETUP" in output
    assert "[ Hub Ready ]" in output
    assert str(config_path) in output
    assert "[ Recommended For Nodes ]" in output
    assert "[ Detailed Values ]" in output
    assert "ORBIT_TASK_PRIVATE_KEY_B64" in output
    assert "ORBIT_TASK_PUBLIC_KEY_B64" in output
    assert "ORBIT_NODE_SHARED_CONFIG" in output
    assert "Paste it directly when `orbit init node` asks" in output


def test_init_node_writes_submitter_and_agent_config(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("ORBIT_CONFIG", str(config_path))

    answers = iter(
        [
            "agent-a",
            "http://127.0.0.1:10551",
            "github",
            "GeoffreyChen777",
            "mvp-orbit-relay",
            "",
            "",
            "api-token",
            "ticket-secret",
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            "",
            "",
            "",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    try:
        main(["init", "node"])
    except SystemExit as exc:
        assert exc.code == 0

    _, config = load_config(config_path)
    assert config.agent.id == "agent-a"
    assert config.hub.url == "http://127.0.0.1:10551"
    assert config.auth.api_token == "api-token"
    assert config.auth.ticket_secret == "ticket-secret"
    assert config.task_signing.private_key_b64 == "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    assert config.task_signing.public_key_b64 == public_key_from_private_key_b64(config.task_signing.private_key_b64)


def test_init_node_renders_wizard_summary(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("ORBIT_CONFIG", str(config_path))

    answers = iter(
        [
            "agent-a",
            "http://127.0.0.1:10551",
            "github",
            "GeoffreyChen777",
            "mvp-orbit-relay",
            "",
            "",
            "api-token",
            "ticket-secret",
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            "",
            "",
            "",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    try:
        main(["init", "node"])
    except SystemExit as exc:
        assert exc.code == 0

    output = capsys.readouterr().out
    assert "ORBIT NODE SETUP" in output
    assert "[ Node Ready ]" in output


def test_node_shared_config_round_trip():
    config = OrbitConfig()
    config.storage.provider = "github"
    config.github.owner = "GeoffreyChen777"
    config.github.repo = "mvp-orbit-relay"
    config.hub.url = "http://127.0.0.1:10551"
    ensure_hub_secrets(config)

    shared_config = encode_node_shared_config(config)
    restored = apply_node_shared_config(OrbitConfig(), shared_config)

    assert restored.storage.provider == "github"
    assert restored.github.owner == "GeoffreyChen777"
    assert restored.github.repo == "mvp-orbit-relay"
    assert restored.hub.url == "http://127.0.0.1:10551"
    assert restored.auth.api_token == config.auth.api_token
    assert restored.auth.ticket_secret == config.auth.ticket_secret
    assert restored.task_signing.private_key_b64 == config.task_signing.private_key_b64
    assert restored.task_signing.public_key_b64 == config.task_signing.public_key_b64


def test_init_node_accepts_shared_config(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("ORBIT_CONFIG", str(config_path))

    shared = OrbitConfig()
    shared.storage.provider = "github"
    shared.github.owner = "GeoffreyChen777"
    shared.github.repo = "mvp-orbit-relay"
    shared.hub.url = "http://127.0.0.1:10551"
    ensure_hub_secrets(shared)
    shared_config = encode_node_shared_config(shared)

    answers = iter(
        [
            "agent-a",
            "",
            "",
            "",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    try:
        main(["init", "node", "--shared-config", shared_config])
    except SystemExit as exc:
        assert exc.code == 0

    _, config = load_config(config_path)
    assert config.agent.id == "agent-a"
    assert config.hub.url == "http://127.0.0.1:10551"
    assert config.storage.provider == "github"
    assert config.github.owner == "GeoffreyChen777"
    assert config.github.repo == "mvp-orbit-relay"
    assert config.auth.api_token == shared.auth.api_token
    assert config.auth.ticket_secret == shared.auth.ticket_secret
    assert config.task_signing.private_key_b64 == shared.task_signing.private_key_b64
    assert config.task_signing.public_key_b64 == shared.task_signing.public_key_b64


def test_init_node_interactive_prompts_for_shared_config(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("ORBIT_CONFIG", str(config_path))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    shared = OrbitConfig()
    shared.storage.provider = "github"
    shared.github.owner = "GeoffreyChen777"
    shared.github.repo = "mvp-orbit-relay"
    shared.hub.url = "http://127.0.0.1:10551"
    ensure_hub_secrets(shared)
    shared_config = encode_node_shared_config(shared)

    confirm_calls: list[str] = []
    text_answers = iter([shared_config, "agent-a", "", "", ""])

    class FakeQuestion:
        def __init__(self, answer):
            self.answer = answer

        def ask(self):
            return self.answer

    def fake_confirm(message: str, **kwargs):
        confirm_calls.append(message)
        return FakeQuestion(True)

    def fake_text(message: str, **kwargs):
        return FakeQuestion(next(text_answers))

    monkeypatch.setattr("mvp_orbit.cli.main.questionary.confirm", fake_confirm)
    monkeypatch.setattr("mvp_orbit.cli.main.questionary.text", fake_text)

    try:
        main(["init", "node"])
    except SystemExit as exc:
        assert exc.code == 0

    _, config = load_config(config_path)
    assert confirm_calls == ["Do you have an ORBIT_NODE_SHARED_CONFIG string?"]
    assert config.agent.id == "agent-a"
    assert config.hub.url == "http://127.0.0.1:10551"
    assert config.storage.provider == "github"
    assert config.auth.api_token == shared.auth.api_token


def test_setup_wizard_prompt_normalizes_none_default_for_questionary(monkeypatch):
    captured: dict[str, str] = {}

    class FakeQuestion:
        def ask(self) -> str:
            return "agent-a"

    def fake_text(message: str, *, default: str, **kwargs):
        captured["message"] = message
        captured["default"] = default
        return FakeQuestion()

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr("mvp_orbit.cli.main.questionary.text", fake_text)

    wizard = SetupWizard("title", "subtitle")
    value = wizard.prompt("Agent ID", None, required=True)

    assert value == "agent-a"
    assert captured == {"message": "Agent ID", "default": ""}


def test_prepare_args_uses_config_defaults(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[github]
owner = "GeoffreyChen777"
repo = "mvp-orbit-relay"
release_prefix = "mvp-orbit"

[storage]
provider = "github"

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
    monkeypatch.delenv("ORBIT_API_TOKEN", raising=False)
    monkeypatch.delenv("ORBIT_TASK_PRIVATE_KEY_B64", raising=False)
    monkeypatch.delenv("ORBIT_AGENT_ID", raising=False)
    monkeypatch.delenv("ORBIT_HUB_URL", raising=False)

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
    assert task_args.store_provider == "github"
    assert task_args.github_owner == "GeoffreyChen777"
    assert task_args.github_repo == "mvp-orbit-relay"


def test_prepare_args_uses_huggingface_config_defaults(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[storage]
provider = "huggingface"

[huggingface]
repo_id = "org/orbit-relay"
repo_type = "dataset"
path_prefix = "objects"
hf_bin = "hf"
private = false

[hub]
url = "http://127.0.0.1:10551"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ORBIT_CONFIG", str(config_path))
    monkeypatch.delenv("ORBIT_STORE_PROVIDER", raising=False)
    monkeypatch.delenv("ORBIT_HF_REPO_ID", raising=False)
    monkeypatch.delenv("ORBIT_HF_REPO_TYPE", raising=False)
    monkeypatch.delenv("ORBIT_HF_PATH_PREFIX", raising=False)
    monkeypatch.delenv("ORBIT_HF_BIN", raising=False)
    monkeypatch.delenv("ORBIT_HF_TOKEN", raising=False)
    monkeypatch.delenv("ORBIT_HF_PRIVATE", raising=False)

    parser = build_parser()
    args = prepare_args(
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

    assert args.store_provider == "huggingface"
    assert args.hf_repo_id == "org/orbit-relay"
    assert args.hf_repo_type == "dataset"
    assert args.hf_path_prefix == "objects"
    assert args.hf_private is False
