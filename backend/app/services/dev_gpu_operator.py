from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import stat
from typing import Literal, TypedDict


SCHEMA_VERSION = 1
MAX_RESPONSE_BYTES = 64 * 1024
SOURCE_RE = re.compile(r"^[0-9a-f]{40}$")
CONTROLLER_STATE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
GpuOperatorPhase = Literal[
    "stopped",
    "recovering",
    "queued",
    "starting",
    "ready",
    "failed",
    "unavailable",
]
VALID_PHASES = {
    "stopped",
    "recovering",
    "queued",
    "starting",
    "ready",
    "failed",
    "unavailable",
}


class DevGpuOperatorError(RuntimeError):
    pass


class DevGpuOperatorStatus(TypedDict):
    schema_version: int
    operator_available: bool
    phase: GpuOperatorPhase
    controller_status: str
    can_recover: bool
    operation_id: str | None
    message: str
    source_sha: str
    source_tree: str
    updated_at: str


class DevGpuOperatorClient:
    def __init__(
        self,
        *,
        socket_path: str,
        timeout_seconds: float,
        expected_source_sha: str,
        expected_source_tree: str,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.timeout_seconds = timeout_seconds
        self.expected_source_sha = expected_source_sha
        self.expected_source_tree = expected_source_tree

    def _validate_socket(self) -> None:
        try:
            parent_metadata = self.socket_path.parent.lstat()
            socket_metadata = self.socket_path.lstat()
        except OSError as exc:
            raise DevGpuOperatorError("GPU operator socket is unavailable") from exc
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(parent_metadata.st_mode) != 0o700
        ):
            raise DevGpuOperatorError("GPU operator directory is unavailable")
        if (
            not stat.S_ISSOCK(socket_metadata.st_mode)
            or socket_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(socket_metadata.st_mode) != 0o600
        ):
            raise DevGpuOperatorError("GPU operator socket is unavailable")

    def _validate_status(self, value: object) -> DevGpuOperatorStatus:
        if not isinstance(value, dict):
            raise DevGpuOperatorError("GPU operator returned an invalid status")
        required = {
            "schema_version",
            "operator_available",
            "phase",
            "controller_status",
            "can_recover",
            "operation_id",
            "message",
            "source_sha",
            "source_tree",
            "updated_at",
        }
        if set(value) != required or value.get("schema_version") != SCHEMA_VERSION:
            raise DevGpuOperatorError("GPU operator returned an incompatible status")
        phase = value.get("phase")
        controller_status = value.get("controller_status")
        operation_id = value.get("operation_id")
        message = value.get("message")
        source_sha = value.get("source_sha")
        source_tree = value.get("source_tree")
        updated_at = value.get("updated_at")
        if (
            value.get("operator_available") is not True
            or phase not in VALID_PHASES
            or not isinstance(controller_status, str)
            or CONTROLLER_STATE_RE.fullmatch(controller_status) is None
            or not isinstance(value.get("can_recover"), bool)
            or (
                operation_id is not None
                and (
                    not isinstance(operation_id, str)
                    or re.fullmatch(r"^[0-9a-f]{32}$", operation_id) is None
                )
            )
            or not isinstance(message, str)
            or not message
            or len(message) > 512
            or not isinstance(source_sha, str)
            or SOURCE_RE.fullmatch(source_sha) is None
            or not isinstance(source_tree, str)
            or SOURCE_RE.fullmatch(source_tree) is None
            or not isinstance(updated_at, str)
            or not updated_at
            or len(updated_at) > 64
        ):
            raise DevGpuOperatorError("GPU operator returned an invalid status")
        if (
            source_sha != self.expected_source_sha
            or source_tree != self.expected_source_tree
        ):
            raise DevGpuOperatorError("GPU operator source identity differs from Backend")
        return value  # type: ignore[return-value]

    def request(
        self, command: Literal["status", "recover"]
    ) -> DevGpuOperatorStatus:
        self._validate_socket()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(self.timeout_seconds)
        try:
            client.connect(str(self.socket_path))
            client.sendall(
                (
                    json.dumps(
                        {"schema_version": SCHEMA_VERSION, "command": command},
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode()
            )
            payload = bytearray()
            while b"\n" not in payload:
                chunk = client.recv(4096)
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise DevGpuOperatorError("GPU operator response is too large")
        except (OSError, TimeoutError) as exc:
            raise DevGpuOperatorError("GPU operator is unavailable") from exc
        finally:
            client.close()
        try:
            response = json.loads(bytes(payload).split(b"\n", 1)[0])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DevGpuOperatorError(
                "GPU operator returned an invalid response"
            ) from exc
        if not isinstance(response, dict) or response.get("ok") is not True:
            error = response.get("error") if isinstance(response, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            raise DevGpuOperatorError(str(message or "GPU operator request failed"))
        return self._validate_status(response.get("result"))
