from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from mvp_orbit.agent.runtime import AgentRuntime
from mvp_orbit.core.models import (
    CommandCompletionRequest,
    CommandLease,
    CommandOutputAppendRequest,
    ShellCompletionRequest,
    ShellEventAppendRequest,
    ShellSessionLease,
)

logger = logging.getLogger(__name__)


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
            shell_status = self._poll_shell_once(client)
            if shell_status != "idle":
                return shell_status
            return self._poll_command_once(client)
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

    def _poll_command_once(self, client: httpx.Client) -> str:
        response = client.get(f"{self.hub_url}/api/agents/{self.agent_id}/commands/next", headers=self._headers())
        if response.status_code == 204:
            return "idle"
        response.raise_for_status()
        lease = CommandLease.model_validate(response.json())
        logger.info(
            "agent %s leased command %s package_id=%s",
            self.agent_id,
            lease.command_id,
            lease.package_id or "-",
        )
        outcome = self.runtime.handle_command(
            lease,
            fetch_package=lambda package_id: self._fetch_package(client, package_id),
            append_output=lambda stream, data: self._append_command_output(client, lease.command_id, stream, data),
            heartbeat=lambda: self._command_heartbeat(client, lease.command_id),
        )
        self._complete_command(client, lease.command_id, outcome)
        logger.info(
            "agent %s reported command %s status=%s exit_code=%s",
            self.agent_id,
            lease.command_id,
            outcome.status.value,
            outcome.exit_code,
        )
        return outcome.status.value

    def _poll_shell_once(self, client: httpx.Client) -> str:
        response = client.get(f"{self.hub_url}/api/agents/{self.agent_id}/shells/next", headers=self._headers())
        if response.status_code == 204:
            return "idle"
        response.raise_for_status()
        lease = ShellSessionLease.model_validate(response.json())
        logger.info(
            "agent %s leased shell session %s package_id=%s",
            self.agent_id,
            lease.session_id,
            lease.package_id or "-",
        )
        outcome = self.runtime.handle_shell_session(
            lease,
            fetch_package=lambda package_id: self._fetch_package(client, package_id),
            get_inputs=lambda after_seq: self._consume_shell_inputs(client, lease.session_id, after_seq),
            append_event=lambda stream, data: self._append_shell_event(client, lease.session_id, stream, data),
            heartbeat=lambda: self._shell_heartbeat(client, lease.session_id),
            should_close=lambda: self._shell_should_close(client, lease.session_id),
        )
        self._complete_shell(client, lease.session_id, outcome)
        logger.info(
            "agent %s reported shell session %s status=%s exit_code=%s",
            self.agent_id,
            lease.session_id,
            outcome.status.value,
            outcome.exit_code,
        )
        return outcome.status.value

    def _fetch_package(self, client: httpx.Client, package_id: str) -> bytes:
        logger.info("agent %s requesting package %s from hub", self.agent_id, package_id)
        response = client.get(f"{self.hub_url}/api/packages/{package_id}", headers=self._headers())
        response.raise_for_status()
        return response.content

    def _append_command_output(self, client: httpx.Client, command_id: str, stream: str, data: str) -> None:
        request = CommandOutputAppendRequest(stream=stream, data=data)
        try:
            response = client.post(
                f"{self.hub_url}/api/commands/{command_id}/output",
                headers=self._headers(),
                json=request.model_dump(mode="json"),
            )
            response.raise_for_status()
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            logger.warning(
                "agent %s failed to append command %s %s output: %s",
                self.agent_id,
                command_id,
                stream,
                exc,
            )

    def _command_heartbeat(self, client: httpx.Client, command_id: str) -> bool:
        response = client.post(f"{self.hub_url}/api/commands/{command_id}/heartbeat", headers=self._headers())
        response.raise_for_status()
        payload = response.json()
        return payload.get("cancel_requested_at") is not None

    def _complete_command(self, client: httpx.Client, command_id: str, outcome) -> None:
        request = CommandCompletionRequest(
            status=outcome.status,
            exit_code=outcome.exit_code,
            failure_code=outcome.failure_code,
        )
        response = client.post(
            f"{self.hub_url}/api/commands/{command_id}/complete",
            headers=self._headers(),
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()

    def _consume_shell_inputs(self, client: httpx.Client, session_id: str, after_seq: int) -> list[tuple[int, str]]:
        response = client.get(
            f"{self.hub_url}/api/shells/{session_id}/inputs",
            headers=self._headers(),
            params={"after_seq": after_seq},
        )
        response.raise_for_status()
        payload = response.json()
        return [(int(item["seq"]), str(item["data"])) for item in payload.get("inputs", [])]

    def _append_shell_event(self, client: httpx.Client, session_id: str, stream: str, data: str) -> None:
        request = ShellEventAppendRequest(stream=stream, data=data)
        try:
            response = client.post(
                f"{self.hub_url}/api/shells/{session_id}/events",
                headers=self._headers(),
                json=request.model_dump(mode="json"),
            )
            response.raise_for_status()
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            logger.warning(
                "agent %s failed to append shell session %s %s event: %s",
                self.agent_id,
                session_id,
                stream,
                exc,
            )

    def _shell_heartbeat(self, client: httpx.Client, session_id: str) -> bool:
        response = client.post(f"{self.hub_url}/api/shells/{session_id}/heartbeat", headers=self._headers())
        response.raise_for_status()
        return True

    def _shell_should_close(self, client: httpx.Client, session_id: str) -> bool:
        response = client.get(f"{self.hub_url}/api/shells/{session_id}", headers=self._headers())
        response.raise_for_status()
        payload = response.json()
        return payload.get("close_requested_at") is not None

    def _complete_shell(self, client: httpx.Client, session_id: str, outcome) -> None:
        request = ShellCompletionRequest(
            status=outcome.status,
            exit_code=outcome.exit_code,
            failure_code=outcome.failure_code,
        )
        response = client.post(
            f"{self.hub_url}/api/shells/{session_id}/complete",
            headers=self._headers(),
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
