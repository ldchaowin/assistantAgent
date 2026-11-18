"""Wire protocol helpers: one JSON object per line."""
from __future__ import annotations

import json
from typing import Any


class ProtocolError(ValueError):
    """Malformed line or unexpected message shape."""


def encode(message: dict[str, Any]) -> bytes:
    return (
        json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def decode(line: bytes | str) -> dict[str, Any]:
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    line = line.strip()
    if not line:
        raise ProtocolError("empty line")
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("message is not an object")
    return obj


def is_event(message: dict[str, Any]) -> bool:
    return "evt" in message


def error_of(message: dict[str, Any]) -> tuple[str, str]:
    return (
        str(message.get("err", "ERROR")),
        str(message.get("msg", "")),
    )
