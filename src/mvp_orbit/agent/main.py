from __future__ import annotations

import os
from pathlib import Path

from mvp_orbit.agent.runtime import AgentRuntime
from mvp_orbit.agent.service import AgentService


def _required(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"missing env var: {name}")
    return value


def main() -> None:
    agent_id = _required("ORBIT_AGENT_ID")
    hub_url = _required("ORBIT_HUB_URL")
    workspace_root = os.getenv("ORBIT_WORKSPACE_ROOT")
    if workspace_root:
        root = Path(workspace_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        os.chdir(root)
    base_workspace = Path.cwd()

    runtime = AgentRuntime(
        agent_id=agent_id,
        base_workspace=base_workspace,
        heartbeat_interval_sec=float(os.getenv("ORBIT_AGENT_HEARTBEAT_SEC", "5")),
    )
    service = AgentService(
        agent_id=agent_id,
        hub_url=hub_url,
        runtime=runtime,
        api_token=os.getenv("ORBIT_API_TOKEN"),
        poll_interval_sec=float(os.getenv("ORBIT_AGENT_POLL_SEC", "5")),
    )
    service.run_forever()


if __name__ == "__main__":
    main()
