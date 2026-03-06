from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from mvp_orbit.agent.runtime import AgentRuntime
from mvp_orbit.core.models import RunCompletionRequest, RunHeartbeatRequest, RunHeartbeatResponse, RunLease


@dataclass
class AgentService:
    agent_id: str
    hub_url: str
    runtime: AgentRuntime
    api_token: str | None = None
    poll_interval_sec: float = 5.0

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def poll_once(self, client: httpx.Client | None = None) -> str:
        own_client = False
        if client is None:
            own_client = True
            client = httpx.Client(timeout=20)
        assert client is not None

        try:
            response = client.get(f"{self.hub_url}/api/agents/{self.agent_id}/next", headers=self._headers())
            if response.status_code == 204:
                return "idle"
            response.raise_for_status()

            lease = RunLease.model_validate(response.json())
            outcome = self.runtime.handle_run(lease, heartbeat=lambda phase: self._heartbeat(client, lease.run_id, phase))
            self._complete(client, lease.run_id, outcome)
            return outcome.status.value
        except (httpx.RequestError, httpx.HTTPStatusError):
            return "poll_error"
        finally:
            if own_client:
                client.close()

    def run_forever(self) -> None:
        with httpx.Client(timeout=20) as client:
            while True:
                self.poll_once(client=client)
                time.sleep(self.poll_interval_sec)

    def _heartbeat(self, client: httpx.Client, run_id: str, phase: str) -> bool:
        request = RunHeartbeatRequest(phase=phase)
        response = client.post(
            f"{self.hub_url}/api/runs/{run_id}/heartbeat",
            headers=self._headers(),
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        payload = RunHeartbeatResponse.model_validate(response.json())
        return payload.cancel_requested

    def _complete(self, client: httpx.Client, run_id: str, outcome) -> None:
        request = RunCompletionRequest(
            status=outcome.status,
            log_ids=outcome.log_ids,
            result_id=outcome.result_id,
            artifact_ids=outcome.artifact_ids,
            failure_code=outcome.failure_code,
        )
        response = client.post(
            f"{self.hub_url}/api/runs/{run_id}/complete",
            headers=self._headers(),
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
