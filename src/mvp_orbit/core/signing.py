from __future__ import annotations

import base64
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from mvp_orbit.core.canonical import canonical_json_bytes


class SignatureError(Exception):
    pass


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def generate_keypair_b64() -> tuple[str, str]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    return _b64encode(private_key.private_bytes_raw()), _b64encode(public_key.public_bytes_raw())


def public_key_from_private_key_b64(private_key_b64: str) -> str:
    private_key = Ed25519PrivateKey.from_private_bytes(_b64decode(private_key_b64))
    return _b64encode(private_key.public_key().public_bytes_raw())


def sign_bytes(payload: bytes, private_key_b64: str) -> str:
    private_key = Ed25519PrivateKey.from_private_bytes(_b64decode(private_key_b64))
    return _b64encode(private_key.sign(payload))


def verify_bytes_signature(payload: bytes, signature_b64: str, public_key_b64: str) -> None:
    public_key = Ed25519PublicKey.from_public_bytes(_b64decode(public_key_b64))
    try:
        public_key.verify(_b64decode(signature_b64), payload)
    except InvalidSignature as exc:
        raise SignatureError("invalid ed25519 signature") from exc


def sign_payload(payload: dict[str, Any], private_key_b64: str) -> str:
    return sign_bytes(canonical_json_bytes(payload), private_key_b64)


def verify_payload_signature(payload: dict[str, Any], signature_b64: str, public_key_b64: str) -> None:
    verify_bytes_signature(canonical_json_bytes(payload), signature_b64, public_key_b64)
