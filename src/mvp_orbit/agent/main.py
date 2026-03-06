from __future__ import annotations

import os

from mvp_orbit.agent.runtime import AgentRuntime
from mvp_orbit.agent.service import AgentService
from mvp_orbit.core.tickets import ReplayGuard, RunTicketManager
from mvp_orbit.integrations.object_store import GitHubGhCliBackend, ObjectStore


def _required(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"missing env var: {name}")
    return value


def build_object_store() -> ObjectStore:
    backend = GitHubGhCliBackend(
        owner=_required("ORBIT_GITHUB_OWNER"),
        repo=_required("ORBIT_GITHUB_REPO"),
        release_prefix=os.getenv("ORBIT_GITHUB_RELEASE_PREFIX", "mvp-orbit"),
        gh_bin=os.getenv("ORBIT_GH_BIN", "gh"),
    )
    return ObjectStore(backend)


def main() -> None:
    agent_id = _required("ORBIT_AGENT_ID")
    hub_url = _required("ORBIT_HUB_URL")
    ticket_secret = _required("ORBIT_TICKET_SECRET")
    public_key = _required("ORBIT_TASK_PUBLIC_KEY_B64")

    runtime = AgentRuntime(
        agent_id=agent_id,
        ticket_manager=RunTicketManager(ticket_secret),
        replay_guard=ReplayGuard(),
        object_store=build_object_store(),
        verify_public_key_b64=public_key,
        workspace_root=os.getenv("ORBIT_WORKSPACE_ROOT", "./.orbit-workspaces"),
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
