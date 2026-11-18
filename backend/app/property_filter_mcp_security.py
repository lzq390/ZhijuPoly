"""Development-only property-filter configuration and shared MCP guards."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator

from app.mcp_security import (
    CallLimiter as SharedCallLimiter,
    ClientCredential as SharedClientCredential,
    ClientRegistry as SharedClientRegistry,
    RequestGuard as SharedRequestGuard,
    read_private_file,
)
from app.property_filter_mcp_contract import MCPFailure, TOOL_NAMES


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



class ClientCredential(SharedClientCredential):
    @field_validator("tools")
    @classmethod
    def known_tools(cls, value: frozenset[str]) -> frozenset[str]:
        if not value <= TOOL_NAMES:
            raise ValueError("Unknown tool permission")
        return value


class ClientRegistry(SharedClientRegistry):
    clients: list[ClientCredential] = Field(max_length=128)


class CallLimiter(SharedCallLimiter):
    def __init__(self, config: MCPConfig):
        super().__init__(config, failure_type=MCPFailure)


class RequestGuard(SharedRequestGuard):
    def __init__(self, app: Any, config: MCPConfig):
        super().__init__(app, config, registry_type=ClientRegistry)
