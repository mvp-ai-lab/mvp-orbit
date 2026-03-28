from __future__ import annotations

import logging
import os
from pathlib import Path
from datetime import datetime

from mvp_orbit.agent.runtime import AgentRuntime
from mvp_orbit.agent.service import AgentService
from mvp_orbit.core.models import utc_now


def _required(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"missing env var: {name}")
    return value


def main() -> None:
    agent_id = _required("ORBIT_AGENT_ID")
    hub_url = _required("ORBIT_HUB_URL")
    user_token = _required("ORBIT_USER_TOKEN")
    expires_at = datetime.fromisoformat(_required("ORBIT_TOKEN_EXPIRES_AT"))
    if expires_at <= utc_now():
        raise RuntimeError("user token expired; run `orbit connect` again")
    logging.basicConfig(
        level=getattr(logging, os.getenv("ORBIT_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    workspace_root = os.getenv("ORBIT_WORKSPACE_ROOT")
    if workspace_root:
        root = Path(workspace_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        os.chdir(root)
    base_workspace = Path.cwd()
    logger = logging.getLogger(__name__)
    logger.info("starting agent %s hub=%s workspace=%s", agent_id, hub_url, base_workspace)

    runtime = AgentRuntime(
        agent_id=agent_id,
        base_workspace=base_workspace,
        command_output_chunk_bytes=int(os.getenv("ORBIT_COMMAND_OUTPUT_CHUNK_BYTES", "4096")),
        command_output_flush_interval_sec=float(os.getenv("ORBIT_COMMAND_OUTPUT_FLUSH_SEC", "0.1")),
    )
    service = AgentService(
        agent_id=agent_id,
        hub_url=hub_url,
        runtime=runtime,
        user_token=user_token,
    )
    service.run_forever()


if __name__ == "__main__":
    main()
