from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from pydantic import ValidationError

from mvp_orbit.core.models import TicketPayload


class TicketError(Exception):
    pass


class ReplayError(TicketError):
    pass


class ExpiredTicketError(TicketError):
    pass


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    pad = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode((raw + pad).encode("ascii"))


class RunTicketManager:
    def __init__(self, secret: str):
        if len(secret) < 16:
            raise ValueError("ticket secret must be at least 16 chars")
        self._secret = secret.encode("utf-8")

    def issue(
        self,
        *,
        run_id: str,
        agent_id: str,
        task_id: str,
        expires_at: datetime,
    ) -> tuple[str, TicketPayload]:
        payload = TicketPayload(
            run_id=run_id,
            agent_id=agent_id,
            task_id=task_id,
            nonce=secrets.token_hex(16),
            issued_at=datetime.now(timezone.utc),
            expires_at=expires_at,
        )
        raw_payload = payload.model_dump_json(exclude_none=True).encode("utf-8")
        sig = hmac.new(self._secret, raw_payload, hashlib.sha256).digest()
        token = f"{_b64url_encode(raw_payload)}.{_b64url_encode(sig)}"
        return token, payload

    def verify(self, token: str) -> TicketPayload:
        try:
            payload_part, sig_part = token.split(".", 1)
        except ValueError as exc:
            raise TicketError("malformed ticket") from exc

        payload_raw = _b64url_decode(payload_part)
        sig_raw = _b64url_decode(sig_part)
        expected_sig = hmac.new(self._secret, payload_raw, hashlib.sha256).digest()
        if not hmac.compare_digest(sig_raw, expected_sig):
            raise TicketError("invalid ticket signature")

        try:
            payload = TicketPayload.model_validate_json(payload_raw)
        except ValidationError as exc:
            raise TicketError("invalid ticket payload") from exc

        if payload.expires_at < datetime.now(timezone.utc):
            raise ExpiredTicketError("ticket expired")
        return payload


class ReplayGuard:
    def __init__(self) -> None:
        self._seen: set[str] = set()

    def consume(self, nonce: str) -> None:
        if nonce in self._seen:
            raise ReplayError(f"nonce already consumed: {nonce}")
        self._seen.add(nonce)
