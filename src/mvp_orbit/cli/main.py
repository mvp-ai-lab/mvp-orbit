from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import httpx

from mvp_orbit.cli.package import build_file_package
from mvp_orbit.config import OrbitConfig, ensure_hub_secrets, load_config, save_config
from mvp_orbit.core.canonical import object_id_for_json
from mvp_orbit.core.models import CommandObject, RunCreateRequest, RunStatus, SignedTaskObject, TaskObject, utc_now
from mvp_orbit.core.signing import generate_keypair_b64, public_key_from_private_key_b64, sign_payload
from mvp_orbit.integrations.object_store import (
    GitHubGhCliBackend,
    HuggingFaceCliBackend,
    ObjectStore,
    build_backend_from_config,
)


def _read_private_key(value: str) -> str:
    maybe_file = Path(value)
    if maybe_file.exists():
        return maybe_file.read_text(encoding="utf-8").strip()
    return value.strip()


def _headers(api_token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    return headers


def _set_if_missing(args: argparse.Namespace, name: str, value) -> None:
    if not hasattr(args, name) or value is None:
        return
    current = getattr(args, name)
    if current is None or current == "":
        setattr(args, name, value)


def _set_env_if_missing(name: str, value: str | None) -> None:
    if value and not os.getenv(name):
        os.environ[name] = value


def _apply_config_defaults(args: argparse.Namespace, config: OrbitConfig) -> None:
    _set_if_missing(args, "store_provider", config.storage.provider)
    _set_if_missing(args, "github_owner", config.github.owner)
    _set_if_missing(args, "github_repo", config.github.repo)
    _set_if_missing(args, "github_release_prefix", config.github.release_prefix)
    _set_if_missing(args, "gh_bin", config.github.gh_bin)
    _set_if_missing(args, "hf_repo_id", config.huggingface.repo_id)
    _set_if_missing(args, "hf_repo_type", config.huggingface.repo_type)
    _set_if_missing(args, "hf_path_prefix", config.huggingface.path_prefix)
    _set_if_missing(args, "hf_bin", config.huggingface.hf_bin)
    _set_if_missing(args, "hf_token", config.huggingface.token)
    _set_if_missing(args, "hf_private", config.huggingface.private)
    _set_if_missing(args, "hub_url", config.hub.resolved_url())
    _set_if_missing(args, "api_token", config.auth.api_token)
    _set_if_missing(args, "private_key", config.task_signing.private_key_b64)
    _set_if_missing(args, "agent_id", config.agent.id)

    _set_env_if_missing("ORBIT_STORE_PROVIDER", config.storage.provider)
    _set_env_if_missing("ORBIT_GITHUB_OWNER", config.github.owner)
    _set_env_if_missing("ORBIT_GITHUB_REPO", config.github.repo)
    _set_env_if_missing("ORBIT_GITHUB_RELEASE_PREFIX", config.github.release_prefix)
    _set_env_if_missing("ORBIT_GH_BIN", config.github.gh_bin)
    _set_env_if_missing("ORBIT_HF_REPO_ID", config.huggingface.repo_id)
    _set_env_if_missing("ORBIT_HF_REPO_TYPE", config.huggingface.repo_type)
    _set_env_if_missing("ORBIT_HF_PATH_PREFIX", config.huggingface.path_prefix)
    _set_env_if_missing("ORBIT_HF_BIN", config.huggingface.hf_bin)
    _set_env_if_missing("ORBIT_HF_TOKEN", config.huggingface.token)
    _set_env_if_missing("ORBIT_HF_PRIVATE", "true" if config.huggingface.private else "false")
    _set_env_if_missing("ORBIT_API_TOKEN", config.auth.api_token)
    _set_env_if_missing("ORBIT_TICKET_SECRET", config.auth.ticket_secret)
    _set_env_if_missing("ORBIT_TASK_PRIVATE_KEY_B64", config.task_signing.private_key_b64)
    _set_env_if_missing("ORBIT_TASK_PUBLIC_KEY_B64", config.task_signing.public_key_b64)
    _set_env_if_missing("ORBIT_HUB_HOST", config.hub.host)
    _set_env_if_missing("ORBIT_HUB_PORT", str(config.hub.port))
    _set_env_if_missing("ORBIT_HUB_DB", config.hub.db)
    _set_env_if_missing("ORBIT_HUB_URL", config.hub.resolved_url())
    _set_env_if_missing("ORBIT_AGENT_ID", config.agent.id)
    _set_env_if_missing("ORBIT_WORKSPACE_ROOT", config.agent.workspace_root)
    _set_env_if_missing("ORBIT_AGENT_POLL_SEC", str(config.agent.poll_interval_sec))
    _set_env_if_missing("ORBIT_AGENT_HEARTBEAT_SEC", str(config.agent.heartbeat_interval_sec))


def _build_object_store(args: argparse.Namespace) -> ObjectStore:
    backend = build_backend_from_config(args._orbit_config, args)
    return ObjectStore(backend)


def _load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _is_terminal_status(value: str) -> bool:
    return value in {RunStatus.SUCCEEDED.value, RunStatus.FAILED.value, RunStatus.REJECTED.value, RunStatus.CANCELED.value}


def _prompt(text: str, default: str | None = None, *, required: bool = False) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        value = input(f"{text}{suffix}: ").strip()
        if value:
            return value
        if default not in (None, ""):
            return str(default)
        if not required:
            return ""


def _validate_required(parser: argparse.ArgumentParser, args: argparse.Namespace, *names: str) -> None:
    missing = [name for name in names if not getattr(args, name, None)]
    if missing:
        parser.error(f"missing required configuration/arguments: {', '.join('--' + name.replace('_', '-') for name in missing)}")


def _prompt_storage_config(config: OrbitConfig) -> None:
    config.storage.provider = _prompt("Storage provider", config.storage.provider or "github", required=True)
    if config.storage.provider == "github":
        config.github.owner = _prompt("GitHub owner", config.github.owner, required=True)
        config.github.repo = _prompt("GitHub relay repo", config.github.repo, required=True)
        config.github.release_prefix = _prompt("GitHub release prefix", config.github.release_prefix)
        config.github.gh_bin = _prompt("gh binary", config.github.gh_bin)
        return
    if config.storage.provider == "huggingface":
        config.huggingface.repo_id = _prompt("HF repo id", config.huggingface.repo_id, required=True)
        config.huggingface.repo_type = _prompt("HF repo type", config.huggingface.repo_type)
        config.huggingface.path_prefix = _prompt("HF path prefix", config.huggingface.path_prefix)
        config.huggingface.hf_bin = _prompt("hf binary", config.huggingface.hf_bin)
        config.huggingface.private = _prompt(
            "HF create private repo (true/false)",
            "true" if config.huggingface.private else "false",
        ).lower() in {"1", "true", "yes", "y"}
        return
    raise SystemExit(f"unsupported storage provider: {config.storage.provider}")


def cmd_init_hub(args: argparse.Namespace) -> int:
    config_path, config = load_config(args.config)

    _prompt_storage_config(config)
    config.hub.host = _prompt("Hub bind host", config.hub.host)
    config.hub.port = int(_prompt("Hub bind port", str(config.hub.port)))
    config.hub.db = _prompt("Hub sqlite path", config.hub.db)
    config.hub.url = _prompt("Hub public URL", config.hub.resolved_url())

    ensure_hub_secrets(config)
    saved_path = save_config(config, config_path)

    print(f"Wrote Hub config to {saved_path}")
    print(f"Hub URL: {config.hub.resolved_url()}")
    print(f"ORBIT_API_TOKEN={config.auth.api_token}")
    print(f"ORBIT_TICKET_SECRET={config.auth.ticket_secret}")
    print(f"ORBIT_TASK_PRIVATE_KEY_B64={config.task_signing.private_key_b64}")
    print(f"ORBIT_TASK_PUBLIC_KEY_B64={config.task_signing.public_key_b64}")
    print("Distribute the API token, ticket secret, and task keypair to every node that should submit tasks.")
    return 0


def cmd_init_node(args: argparse.Namespace) -> int:
    config_path, config = load_config(args.config)

    default_agent_id = args.agent_id or config.agent.id
    config.agent.id = _prompt("Agent ID", default_agent_id, required=True)
    config.hub.url = _prompt("Hub URL", config.hub.resolved_url(), required=True)
    _prompt_storage_config(config)
    config.auth.api_token = _prompt("Hub API token", config.auth.api_token, required=True)
    config.auth.ticket_secret = _prompt("Run ticket secret", config.auth.ticket_secret, required=True)
    config.task_signing.private_key_b64 = _read_private_key(
        _prompt(
            "Task private key",
            config.task_signing.private_key_b64,
            required=True,
        )
    )
    config.task_signing.public_key_b64 = public_key_from_private_key_b64(config.task_signing.private_key_b64)
    config.agent.workspace_root = _prompt("Workspace root", config.agent.workspace_root)
    config.agent.poll_interval_sec = float(_prompt("Poll interval seconds", str(config.agent.poll_interval_sec)))
    config.agent.heartbeat_interval_sec = float(
        _prompt("Heartbeat interval seconds", str(config.agent.heartbeat_interval_sec))
    )

    saved_path = save_config(config, config_path)
    print(f"Wrote node config to {saved_path}")
    print(f"Agent ID: {config.agent.id}")
    print(f"Hub URL: {config.hub.resolved_url()}")
    print("This node can upload packages, commands, signed tasks, submit runs, and execute work as an agent.")
    return 0


def cmd_init_agent(args: argparse.Namespace) -> int:
    return cmd_init_node(args)


def cmd_package_upload(args: argparse.Namespace) -> int:
    build = build_file_package(args.source_dir, tmp_dir=args.tmp_dir)
    try:
        store = _build_object_store(args)
        package_id = store.put_package(build.archive_path.read_bytes())
        print(json.dumps({"package_id": package_id, "file_count": build.file_count}, ensure_ascii=False))
        return 0
    finally:
        shutil.rmtree(build.archive_path.parent, ignore_errors=True)


def cmd_command_upload(args: argparse.Namespace) -> int:
    command = CommandObject.model_validate(_load_json(args.file))
    store = _build_object_store(args)
    command_id = store.put_command(command)
    print(json.dumps({"command_id": command_id}, ensure_ascii=False))
    return 0


def cmd_task_upload(args: argparse.Namespace) -> int:
    if args.file:
        task = TaskObject.model_validate(_load_json(args.file))
    else:
        if not args.package_id or not args.command_id:
            raise SystemExit("--package-id and --command-id are required when --file is not provided")
        constraints = _load_json(args.constraints_file) if args.constraints_file else {}
        task = TaskObject(
            package_id=args.package_id,
            command_id=args.command_id,
            constraints=constraints,
            created_by=args.created_by,
            created_at=utc_now(),
        )

    task_payload = task.model_dump(mode="json", exclude_none=True)
    task_id = object_id_for_json(task_payload)
    signature = sign_payload(task_payload, _read_private_key(args.private_key))
    signed_task = SignedTaskObject(
        task_id=task_id,
        task=task,
        task_signature=signature,
        signer=args.signer,
    )
    store = _build_object_store(args)
    store.put_signed_task(signed_task)
    print(
        json.dumps(
            {"task_id": task_id, "package_id": task.package_id, "command_id": task.command_id},
            ensure_ascii=False,
        )
    )
    return 0


def cmd_run_submit(args: argparse.Namespace) -> int:
    request = RunCreateRequest(
        agent_id=args.agent_id,
        task_id=args.task_id,
    )
    with httpx.Client(timeout=20) as client:
        response = client.post(
            f"{args.hub_url}/api/runs",
            headers=_headers(args.api_token),
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False))
    return 0


def cmd_run_status(args: argparse.Namespace) -> int:
    with httpx.Client(timeout=20) as client:
        response = client.get(f"{args.hub_url}/api/runs/{args.run_id}", headers=_headers(args.api_token))
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


def cmd_run_cancel(args: argparse.Namespace) -> int:
    with httpx.Client(timeout=20) as client:
        response = client.post(f"{args.hub_url}/api/runs/{args.run_id}/cancel", headers=_headers(args.api_token))
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


def cmd_run_logs(args: argparse.Namespace) -> int:
    store = _build_object_store(args)
    if args.follow:
        seen: set[str] = set()
        with httpx.Client(timeout=20) as client:
            while True:
                response = client.get(f"{args.hub_url}/api/runs/{args.run_id}", headers=_headers(args.api_token))
                response.raise_for_status()
                run_record = response.json()
                new_logs = []
                for log_id in run_record.get("log_ids", []):
                    if log_id in seen:
                        continue
                    seen.add(log_id)
                    new_logs.append(store.get_log(log_id))
                for log in sorted(new_logs, key=lambda item: item.seq):
                    target = sys.stderr if log.stream == "stderr" else sys.stdout
                    print(log.data, end="", file=target, flush=True)
                if _is_terminal_status(run_record["status"]):
                    break
                time.sleep(args.poll_interval_sec)
        return 0

    with httpx.Client(timeout=20) as client:
        response = client.get(f"{args.hub_url}/api/runs/{args.run_id}", headers=_headers(args.api_token))
        response.raise_for_status()
        run_record = response.json()

    logs = [store.get_log(log_id).model_dump(mode="json") for log_id in run_record.get("log_ids", [])]
    logs.sort(key=lambda item: item["seq"])
    print(json.dumps({"run_id": args.run_id, "logs": logs}, ensure_ascii=False, indent=2))
    return 0


def cmd_run_result(args: argparse.Namespace) -> int:
    with httpx.Client(timeout=20) as client:
        response = client.get(f"{args.hub_url}/api/runs/{args.run_id}", headers=_headers(args.api_token))
        response.raise_for_status()
        run_record = response.json()

    result_id = run_record.get("result_id")
    if not result_id:
        print(json.dumps({"run_id": args.run_id, "result": None}, ensure_ascii=False, indent=2))
        return 0

    store = _build_object_store(args)
    result = store.get_result(result_id).model_dump(mode="json")
    print(json.dumps({"run_id": args.run_id, "result": result}, ensure_ascii=False, indent=2))
    return 0


def cmd_relay_clean(args: argparse.Namespace) -> int:
    if not args.yes:
        raise SystemExit("relay clean is destructive; re-run with --yes")
    backend = build_backend_from_config(args._orbit_config, args)
    if isinstance(backend, GitHubGhCliBackend):
        deleted = backend.purge_managed_releases()
        payload = {
            "provider": "github",
            "repo": f"{args.github_owner}/{args.github_repo}",
            "release_prefix": args.github_release_prefix,
            "deleted_releases": deleted,
        }
    elif isinstance(backend, HuggingFaceCliBackend):
        deleted = backend.purge_managed_paths()
        payload = {
            "provider": "huggingface",
            "repo_id": args.hf_repo_id,
            "path_prefix": args.hf_path_prefix,
            "deleted_patterns": deleted,
        }
    else:
        raise SystemExit(f"relay clean is not supported for backend: {type(backend).__name__}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_agent_run(args: argparse.Namespace) -> int:
    from mvp_orbit.agent.main import main as agent_main

    agent_main()
    return 0


def cmd_hub_serve(args: argparse.Namespace) -> int:
    from mvp_orbit.hub.app import main as hub_main

    hub_main()
    return 0


def cmd_keys_generate(args: argparse.Namespace) -> int:
    private_key, public_key = generate_keypair_b64()
    print(f"ORBIT_TASK_PRIVATE_KEY_B64={private_key}")
    print(f"ORBIT_TASK_PUBLIC_KEY_B64={public_key}")
    return 0


def _add_storage_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--store-provider",
        choices=["github", "huggingface"],
        default=os.getenv("ORBIT_STORE_PROVIDER"),
        required=False,
    )
    parser.add_argument("--github-owner", default=os.getenv("ORBIT_GITHUB_OWNER"), required=False)
    parser.add_argument("--github-repo", default=os.getenv("ORBIT_GITHUB_REPO"), required=False)
    parser.add_argument(
        "--github-release-prefix",
        default=os.getenv("ORBIT_GITHUB_RELEASE_PREFIX"),
    )
    parser.add_argument("--gh-bin", default=os.getenv("ORBIT_GH_BIN"))
    parser.add_argument("--hf-repo-id", default=os.getenv("ORBIT_HF_REPO_ID"), required=False)
    parser.add_argument(
        "--hf-repo-type",
        choices=["model", "dataset", "space"],
        default=os.getenv("ORBIT_HF_REPO_TYPE"),
    )
    parser.add_argument("--hf-path-prefix", default=os.getenv("ORBIT_HF_PATH_PREFIX"))
    parser.add_argument("--hf-bin", default=os.getenv("ORBIT_HF_BIN"))
    parser.add_argument("--hf-token", default=os.getenv("ORBIT_HF_TOKEN"))
    parser.add_argument("--hf-private", dest="hf_private", action=argparse.BooleanOptionalAction, default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orbit", description="mvp-orbit CLI")
    parser.add_argument("--config", default=os.getenv("ORBIT_CONFIG"), help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="interactive configuration setup")
    init_sub = init.add_subparsers(dest="init_command", required=True)
    init_hub = init_sub.add_parser("hub", help="interactively create/update Hub config")
    init_hub.set_defaults(func=cmd_init_hub)
    init_node = init_sub.add_parser("node", help="interactively create/update a node config for submit + agent execution")
    init_node.add_argument("--agent-id", default=None)
    init_node.set_defaults(func=cmd_init_node)
    init_agent = init_sub.add_parser("agent", help="alias for `orbit init node`")
    init_agent.add_argument("--agent-id", default=None)
    init_agent.set_defaults(func=cmd_init_agent)

    package = sub.add_parser("package", help="package object commands")
    package_sub = package.add_subparsers(dest="package_command", required=True)
    package_upload = package_sub.add_parser("upload", help="build and upload a file package")
    package_upload.add_argument("--source-dir", required=True)
    package_upload.add_argument("--tmp-dir", default=None)
    _add_storage_args(package_upload)
    package_upload.set_defaults(func=cmd_package_upload)

    command = sub.add_parser("command", help="command object commands")
    command_sub = command.add_subparsers(dest="command_command", required=True)
    command_upload = command_sub.add_parser("upload", help="upload a command object from JSON")
    command_upload.add_argument("--file", required=True)
    _add_storage_args(command_upload)
    command_upload.set_defaults(func=cmd_command_upload)

    task = sub.add_parser("task", help="task object commands")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_upload = task_sub.add_parser("upload", help="upload a signed task object")
    task_upload.add_argument("--file", default=None, help="optional full task JSON file")
    task_upload.add_argument("--package-id", default=None)
    task_upload.add_argument("--command-id", default=None)
    task_upload.add_argument("--constraints-file", default=None)
    task_upload.add_argument("--created-by", default=os.getenv("USER"))
    task_upload.add_argument("--private-key", default=None)
    task_upload.add_argument("--signer", default=None)
    _add_storage_args(task_upload)
    task_upload.set_defaults(func=cmd_task_upload)

    run = sub.add_parser("run", help="run commands")
    run_sub = run.add_subparsers(dest="run_command", required=True)

    run_submit = run_sub.add_parser("submit", help="submit a run to Hub")
    run_submit.add_argument("--hub-url", default=None)
    run_submit.add_argument("--agent-id", default=None)
    run_submit.add_argument("--task-id", required=True)
    run_submit.add_argument("--api-token", default=os.getenv("ORBIT_API_TOKEN"))
    run_submit.set_defaults(func=cmd_run_submit)

    run_status = run_sub.add_parser("status", help="show run status")
    run_status.add_argument("--hub-url", default=None)
    run_status.add_argument("--run-id", required=True)
    run_status.add_argument("--api-token", default=os.getenv("ORBIT_API_TOKEN"))
    run_status.set_defaults(func=cmd_run_status)

    run_cancel = run_sub.add_parser("cancel", help="cancel a queued or running run")
    run_cancel.add_argument("--hub-url", default=None)
    run_cancel.add_argument("--run-id", required=True)
    run_cancel.add_argument("--api-token", default=os.getenv("ORBIT_API_TOKEN"))
    run_cancel.set_defaults(func=cmd_run_cancel)

    run_logs = run_sub.add_parser("logs", help="fetch logs via Hub ids + GitHub objects")
    run_logs.add_argument("--hub-url", default=None)
    run_logs.add_argument("--run-id", required=True)
    run_logs.add_argument("--api-token", default=os.getenv("ORBIT_API_TOKEN"))
    run_logs.add_argument("--follow", action="store_true")
    run_logs.add_argument("--poll-interval-sec", type=float, default=2.0)
    _add_storage_args(run_logs)
    run_logs.set_defaults(func=cmd_run_logs)

    run_result = run_sub.add_parser("result", help="fetch result via Hub id + GitHub object")
    run_result.add_argument("--hub-url", default=None)
    run_result.add_argument("--run-id", required=True)
    run_result.add_argument("--api-token", default=os.getenv("ORBIT_API_TOKEN"))
    _add_storage_args(run_result)
    run_result.set_defaults(func=cmd_run_result)

    relay = sub.add_parser("relay", help="relay repository maintenance commands")
    relay_sub = relay.add_subparsers(dest="relay_command", required=True)
    relay_clean = relay_sub.add_parser("clean", help="delete mvp-orbit managed releases in the relay repo")
    relay_clean.add_argument("--yes", action="store_true", help="confirm deletion")
    _add_storage_args(relay_clean)
    relay_clean.set_defaults(func=cmd_relay_clean)

    agent = sub.add_parser("agent", help="agent commands")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_run = agent_sub.add_parser("run", help="run the polling agent")
    agent_run.set_defaults(func=cmd_agent_run)

    keys = sub.add_parser("keys", help="key management commands")
    keys_sub = keys.add_subparsers(dest="keys_command", required=True)
    keys_generate = keys_sub.add_parser("generate", help="generate task signing keypair")
    keys_generate.set_defaults(func=cmd_keys_generate)

    hub = sub.add_parser("hub", help="hub commands")
    hub_sub = hub.add_subparsers(dest="hub_command", required=True)
    hub_serve = hub_sub.add_parser("serve", help="serve the Hub API")
    hub_serve.set_defaults(func=cmd_hub_serve)

    return parser


def prepare_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> argparse.Namespace:
    config_path, config = load_config(args.config)
    args.config = str(config_path)
    args._orbit_config = config
    _apply_config_defaults(args, config)

    needs_store = (
        (args.command == "package" and args.package_command == "upload")
        or (args.command == "command" and args.command_command == "upload")
        or (args.command == "task" and args.task_command == "upload")
        or (args.command == "run" and args.run_command in {"logs", "result"})
        or (args.command == "relay" and args.relay_command == "clean")
    )
    if needs_store:
        provider = args.store_provider or "github"
        if provider == "github":
            _validate_required(parser, args, "github_owner", "github_repo")
        elif provider == "huggingface":
            _validate_required(parser, args, "hf_repo_id")
        else:
            parser.error(f"unsupported --store-provider: {provider}")

    if args.command == "task" and args.task_command == "upload":
        _validate_required(parser, args, "private_key")

    if args.command == "run" and args.run_command in {"submit", "status", "cancel", "logs", "result"}:
        _validate_required(parser, args, "hub_url")
    if args.command == "run" and args.run_command == "submit":
        _validate_required(parser, args, "agent_id")

    return args


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = prepare_args(parser, parser.parse_args(argv))
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
