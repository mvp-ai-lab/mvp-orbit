from __future__ import annotations

import hashlib
import json
import re
from typing import Any


OBJECT_ID_PREFIX = "sha256-"
OBJECT_ID_PATTERN = re.compile(r"^sha256-[0-9a-f]{64}$")


def canonical_json_bytes(value: Any) -> bytes:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def sha256_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def object_id_for_bytes(data: bytes) -> str:
    return f"{OBJECT_ID_PREFIX}{sha256_digest(data)}"


def object_id_for_json(payload: dict[str, Any]) -> str:
    return object_id_for_bytes(canonical_json_bytes(payload))


def require_matching_object_id(expected_object_id: str, data: bytes) -> None:
    actual_object_id = object_id_for_bytes(data)
    if actual_object_id != expected_object_id:
        raise ValueError(f"object_id mismatch: expected={expected_object_id} actual={actual_object_id}")


def require_matching_json_object_id(expected_object_id: str, payload: dict[str, Any]) -> None:
    actual_object_id = object_id_for_json(payload)
    if actual_object_id != expected_object_id:
        raise ValueError(f"object_id mismatch: expected={expected_object_id} actual={actual_object_id}")


def validate_object_id(object_id: str) -> None:
    if not OBJECT_ID_PATTERN.match(object_id):
        raise ValueError(f"invalid object_id: {object_id}")
