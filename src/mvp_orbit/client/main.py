from __future__ import annotations

import logging
import os
from pathlib import Path
from datetime import datetime

from mvp_orbit.client.runtime import ClientRuntime
from mvp_orbit.client.service import ClientService
from mvp_orbit.core.logging import configure_logging, log_kv
from mvp_orbit.core.models import utc_now


def _required(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"missing env var: {name}")
    return value


def main() -> None:
    client_id = _required("ORBIT_CLIENT_ID")
    hub_url = _required("ORBIT_HUB_URL")
    member_token = _required("ORBIT_MEMBER_TOKEN")
    expires_at = datetime.fromisoformat(_required("ORBIT_TOKEN_EXPIRES_AT"))
    if expires_at <= utc_now():
        raise RuntimeError("member token expired; run `orbit join` again")
    configure_logging("client")
    workspace_root = os.getenv("ORBIT_WORKSPACE_ROOT")
    if workspace_root:
        root = Path(workspace_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        os.chdir(root)
    base_workspace = Path.cwd()
    logger = logging.getLogger(__name__)
    log_kv(logger, logging.INFO, "client.start", client_id=client_id, hub_url=hub_url, workspace=base_workspace)

    runtime = ClientRuntime(
        client_id=client_id,
        base_workspace=base_workspace,
        command_output_chunk_bytes=int(os.getenv("ORBIT_COMMAND_OUTPUT_CHUNK_BYTES", "4096")),
        command_output_flush_interval_sec=float(os.getenv("ORBIT_COMMAND_OUTPUT_FLUSH_SEC", "0.1")),
    )
    service = ClientService(
        client_id=client_id,
        hub_url=hub_url,
        runtime=runtime,
        member_token=member_token,
        heartbeat_interval_sec=float(os.getenv("ORBIT_HEARTBEAT_SEC", "15")),
    )
    service.run_forever()


if __name__ == "__main__":
    main()
