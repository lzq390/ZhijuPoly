"""Database-independent safeguards shared by the development MCP services."""
from __future__ import annotations

import hashlib
import hmac
import os
import stat
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.datastructures import Headers
from starlette.responses import JSONResponse


class MCPFailure(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


def read_private_file(path: Path, *, max_bytes: int = 65536) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        metadata = os.fstat(handle.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                or metadata.st_uid != os.geteuid() or metadata.st_nlink != 1):
            raise ValueError("Credential file must be a private regular file (0600)")
        value = handle.read(max_bytes + 1)
        if len(value.encode("utf-8")) > max_bytes:
            raise ValueError("Credential file is too large")
        return value


class ClientCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    client_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    token_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: datetime
    tools: frozenset[str]

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Credential expiry requires a timezone")
        return value


class ClientRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clients: list[ClientCredential] = Field(max_length=128)

    @classmethod
    def read(cls, path: Path, *, tool_names: frozenset[str] | None = None) -> ClientRegistry:
        registry = cls.model_validate_json(read_private_file(path))
        hashes = [item.token_sha256 for item in registry.clients]
        if len(set(hashes)) != len(hashes):
            raise ValueError("Duplicate credentials")
        if tool_names is not None and any(not item.tools <= tool_names for item in registry.clients):
            raise ValueError("Unknown tool permission")
        return registry

    def authenticate(self, token: str) -> ClientCredential | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.now(timezone.utc)
        for item in self.clients:
            if hmac.compare_digest(digest, item.token_sha256) and item.expires_at > now:
                return item
        return None


class CallLimiter:
    """Single-process accounting. Call acquire/release on the event-loop thread."""
    def __init__(self, config: Any, *, failure_type: type[Exception] = MCPFailure):
        self.config, self.failure_type = config, failure_type
        self.calls: dict[str, deque[float]] = defaultdict(deque)
        self.active: dict[str, int] = defaultdict(int)

    def acquire(self, client_id: str) -> None:
        now, calls = time.monotonic(), self.calls[client_id]
        while calls and calls[0] <= now - 60:
            calls.popleft()
        if len(calls) >= self.config.rate_limit:
            raise self.failure_type("RATE_LIMITED", "Client call limit reached; retry after the one-minute window.")
        if self.active[client_id] >= self.config.max_concurrency:
            raise self.failure_type("CONCURRENCY_LIMITED", "Client concurrent call limit reached; retry when another call completes.")
        calls.append(now)
        self.active[client_id] += 1

    def release(self, client_id: str) -> None:
        self.active[client_id] = max(0, self.active[client_id] - 1)


class RequestGuard:
    def __init__(self, app: Any, config: Any, *, tool_names: frozenset[str] | None = None,
                 registry_type: type[ClientRegistry] = ClientRegistry):
        self.app, self.config = app, config
        self.tool_names, self.registry_type = tool_names, registry_type

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uuid4().hex
        scope.setdefault("state", {})["mcp_request_id"] = request_id
        original_send = send

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), (b"x-request-id", request_id.encode())]}
            await original_send(message)

        headers = Headers(scope=scope)
        if headers.get("host", "").lower() not in self.config.allowed_hosts:
            return await JSONResponse({"error": "invalid_host"}, status_code=421)(scope, receive, send)
        origin = headers.get("origin")
        if origin is not None and origin not in self.config.allowed_origins:
            return await JSONResponse({"error": "invalid_origin"}, status_code=403)(scope, receive, send)
        if scope.get("query_string"):
            return await JSONResponse({"error": "query_parameters_not_supported"}, status_code=400)(scope, receive, send)
        if scope["path"] != "/healthz":
            scheme, _, token = headers.get("authorization", "").partition(" ")
            try:
                if self.tool_names is None:
                    registry = self.registry_type.read(self.config.clients_file)
                else:
                    registry = self.registry_type.read(self.config.clients_file, tool_names=self.tool_names)
            except (OSError, ValueError):
                return await JSONResponse({"error": "credentials_unavailable"}, status_code=503)(scope, receive, send)
            credential = registry.authenticate(token) if scheme.lower() == "bearer" and 1 <= len(token) <= 256 else None
            if credential is None:
                return await JSONResponse({"error": "invalid_token"}, status_code=401,
                                          headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
            scope["state"]["mcp_client"] = credential
        await self.app(scope, receive, send)
