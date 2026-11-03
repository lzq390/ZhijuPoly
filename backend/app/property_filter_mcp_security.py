"""Development-only credentials and request guards; independent of app settings."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from app.property_filter_mcp_contract import MCPFailure, TOOL_NAMES


def read_private_file(path: Path, *, max_bytes: int = 65536) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise ValueError("Credential file must be a private regular file (0600)")
        value = handle.read(max_bytes + 1)
        if len(value.encode("utf-8")) > max_bytes:
            raise ValueError("Credential file is too large")
        return value


@dataclass(frozen=True)
class MCPConfig:
    dsn: str
    clients_file: Path
    host: str = "127.0.0.1"
    port: int = 18110
    allowed_hosts: tuple[str, ...] = ("127.0.0.1:18110", "localhost:18110")
    allowed_origins: tuple[str, ...] = ()
    rate_limit: int = 60
    max_concurrency: int = 2
    statement_timeout_ms: int = 20000
    tool_timeout_seconds: float = 30.0
    lock_timeout_ms: int = 2000

    def __post_init__(self) -> None:
        if not self.dsn.strip():
            raise ValueError("An explicit MCP DSN is required")
        if not self.allowed_hosts or any("*" in host for host in self.allowed_hosts):
            raise ValueError("Explicit allowed hosts are required")
        if any("*" in origin for origin in self.allowed_origins):
            raise ValueError("Origins must be explicit")
        for value, maximum in ((self.rate_limit, 60), (self.max_concurrency, 2),
                               (self.statement_timeout_ms, 20000), (self.lock_timeout_ms, 2000),
                               (self.tool_timeout_seconds, 30)):
            if not 0 < value <= maximum:
                raise ValueError("MCP limits may only be tightened")

    @classmethod
    def from_env(cls) -> MCPConfig:
        prefix = "NEXPOLY_MCP_"
        dsn, dsn_file = os.environ.get(prefix + "DSN"), os.environ.get(prefix + "DSN_FILE")
        if bool(dsn) == bool(dsn_file):
            raise ValueError("Set exactly one of NEXPOLY_MCP_DSN or NEXPOLY_MCP_DSN_FILE")
        clients_file = os.environ.get(prefix + "CLIENTS_FILE")
        if not clients_file:
            raise ValueError("NEXPOLY_MCP_CLIENTS_FILE is required")
        port = int(os.environ.get(prefix + "PORT", "18110"))
        split = lambda value: tuple(item.strip() for item in value.split(",") if item.strip())
        return cls(
            dsn=dsn or read_private_file(Path(dsn_file), max_bytes=8192).strip(),
            clients_file=Path(clients_file), host=os.environ.get(prefix + "HOST", "127.0.0.1"), port=port,
            allowed_hosts=split(os.environ.get(prefix + "ALLOWED_HOSTS", f"127.0.0.1:{port},localhost:{port}")),
            allowed_origins=split(os.environ.get(prefix + "ALLOWED_ORIGINS", "")),
            rate_limit=int(os.environ.get(prefix + "RATE_LIMIT", "60")),
            max_concurrency=int(os.environ.get(prefix + "MAX_CONCURRENCY", "2")),
            statement_timeout_ms=int(os.environ.get(prefix + "STATEMENT_TIMEOUT_MS", "20000")),
            tool_timeout_seconds=float(os.environ.get(prefix + "TOOL_TIMEOUT_SECONDS", "30")),
            lock_timeout_ms=int(os.environ.get(prefix + "LOCK_TIMEOUT_MS", "2000")),
        )


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

    @field_validator("tools")
    @classmethod
    def known_tools(cls, value: frozenset[str]) -> frozenset[str]:
        if not value <= TOOL_NAMES:
            raise ValueError("Unknown tool permission")
        return value


class ClientRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clients: list[ClientCredential] = Field(max_length=128)

    @classmethod
    def read(cls, path: Path) -> ClientRegistry:
        registry = cls.model_validate_json(read_private_file(path))
        hashes = [client.token_sha256 for client in registry.clients]
        if len(set(hashes)) != len(hashes):
            raise ValueError("Duplicate credentials")
        return registry

    def authenticate(self, token: str) -> ClientCredential | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.now(timezone.utc)
        for client in self.clients:
            if hmac.compare_digest(digest, client.token_sha256) and client.expires_at > now:
                return client
        return None


class CallLimiter:
    """Single-process sliding window and active-call accounting; no await between checks."""
    def __init__(self, config: MCPConfig):
        self.config = config
        self.calls: dict[str, deque[float]] = defaultdict(deque)
        self.active: dict[str, int] = defaultdict(int)

    def acquire(self, client_id: str) -> None:
        now = time.monotonic()
        calls = self.calls[client_id]
        while calls and calls[0] <= now - 60:
            calls.popleft()
        if len(calls) >= self.config.rate_limit:
            raise MCPFailure("RATE_LIMITED", "Client call limit reached; retry after the one-minute window.")
        if self.active[client_id] >= self.config.max_concurrency:
            raise MCPFailure("CONCURRENCY_LIMITED", "Client concurrent call limit reached; retry when another call completes.")
        calls.append(now)
        self.active[client_id] += 1

    def release(self, client_id: str) -> None:
        self.active[client_id] = max(0, self.active[client_id] - 1)


class RequestGuard:
    def __init__(self, app: Any, config: MCPConfig):
        self.app = app
        self.config = config

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex
        scope.setdefault("state", {})["mcp_request_id"] = request_id
        original_send = send

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), (b"x-request-id", request_id.encode())]}
            await original_send(message)

        headers = Headers(scope=scope)
        if headers.get("host", "").lower() not in self.config.allowed_hosts:
            await JSONResponse({"error": "invalid_host"}, status_code=421)(scope, receive, send)
            return
        origin = headers.get("origin")
        if origin is not None and origin not in self.config.allowed_origins:
            await JSONResponse({"error": "invalid_origin"}, status_code=403)(scope, receive, send)
            return
        if scope.get("query_string"):
            await JSONResponse({"error": "query_parameters_not_supported"}, status_code=400)(scope, receive, send)
            return
        if scope["path"] != "/healthz":
            authorization = headers.get("authorization", "")
            scheme, _, token = authorization.partition(" ")
            credential = None
            try:
                registry = ClientRegistry.read(self.config.clients_file)
            except (OSError, ValueError):
                await JSONResponse({"error": "credentials_unavailable"}, status_code=503)(scope, receive, send)
                return
            if scheme.lower() == "bearer" and 1 <= len(token) <= 256:
                credential = registry.authenticate(token)
            if credential is None:
                await JSONResponse({"error": "invalid_token"}, status_code=401,
                                   headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
                return
            scope.setdefault("state", {})["mcp_client"] = credential
        await self.app(scope, receive, send)
