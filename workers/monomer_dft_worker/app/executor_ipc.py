"""Small, auditable executor IPC codec.

Only JSON objects are accepted.  In particular, the worker protocol never
deserializes pickle, torch.save/PT, or arbitrary Python objects.
"""

from __future__ import annotations

import json
import socket
import struct
from typing import Any


EXECUTOR_PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024 * 1024
_HEADER = struct.Struct("!I")


class ExecutorProtocolError(RuntimeError):
    pass


def _json_bytes(message: dict[str, Any]) -> bytes:
    if not isinstance(message, dict):
        raise ExecutorProtocolError("executor frame root must be a JSON object")
    try:
        payload = json.dumps(
            message,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExecutorProtocolError("executor frame is not strict JSON") from exc
    if not payload or len(payload) > MAX_FRAME_BYTES:
        raise ExecutorProtocolError("executor frame exceeds the protocol limit")
    return payload


def send_frame(stream: socket.socket, message: dict[str, Any]) -> None:
    payload = _json_bytes(message)
    stream.sendall(_HEADER.pack(len(payload)) + payload)


def _read_exact(stream: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.recv(remaining)
        if not chunk:
            raise EOFError("executor IPC peer closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def receive_frame(stream: socket.socket) -> dict[str, Any]:
    (size,) = _HEADER.unpack(_read_exact(stream, _HEADER.size))
    if size < 2 or size > MAX_FRAME_BYTES:
        raise ExecutorProtocolError("invalid executor frame length")
    payload = _read_exact(stream, size)
    try:
        message = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutorProtocolError("executor frame is not valid UTF-8 JSON") from exc
    if not isinstance(message, dict):
        raise ExecutorProtocolError("executor frame root must be a JSON object")
    return message


def protocol_message(message_type: str, **payload: Any) -> dict[str, Any]:
    return {
        "executor_protocol_version": EXECUTOR_PROTOCOL_VERSION,
        "type": message_type,
        **payload,
    }


def validate_message(message: dict[str, Any], expected_type: str | None = None) -> str:
    if message.get("executor_protocol_version") != EXECUTOR_PROTOCOL_VERSION:
        raise ExecutorProtocolError("unsupported executor protocol version")
    message_type = message.get("type")
    if not isinstance(message_type, str) or not message_type:
        raise ExecutorProtocolError("executor message type is missing")
    if expected_type is not None and message_type != expected_type:
        raise ExecutorProtocolError(
            f"expected executor message {expected_type!r}, got {message_type!r}"
        )
    return message_type
