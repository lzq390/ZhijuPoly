"""Explicit configuration and private backend session for PolyTAO MCP."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.mcp_security import ClientCredential, ClientRegistry, MCPFailure, read_private_file
from app.polytao_mcp_contract import MODEL_TOOL_NAMES, UI_TOOL_NAMES


class PolytaoClientCredential(ClientCredential):
    owner_client_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,64}$")


class PolytaoClientRegistry(ClientRegistry):
    clients: list[PolytaoClientCredential] = Field(max_length=128)

    @model_validator(mode="after")
    def fixed_confirmation_owners(self):
        by_id = {item.client_id: item for item in self.clients}
        if len(by_id) != len(self.clients):
            raise ValueError("Duplicate client identities")
        for item in self.clients:
            if item.owner_client_id is None:
                if not item.tools <= MODEL_TOOL_NAMES:
                    raise ValueError("Model clients cannot receive confirmation tools")
            else:
                owner = by_id.get(item.owner_client_id)
                if (owner is None or owner.owner_client_id is not None or item.client_id == owner.client_id
                        or not item.tools <= UI_TOOL_NAMES):
                    raise ValueError("UI clients require a fixed model owner and UI-only tools")
        return self

    def owner_for(self, client_id: str) -> str:
        for item in self.clients:
            if item.client_id == client_id:
                return item.owner_client_id or client_id
        raise MCPFailure("PERMISSION_DENIED", "MCP client is absent from the active registry.")


def backend_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or any(ch.isspace() for ch in value)):
        raise ValueError("Backend URL must be one explicit HTTP(S) origin")
    return value.rstrip("/")


@dataclass(frozen=True)
class PolytaoMCPConfig:
    backend_url: str
    account_uuid: str
    session_file: Path
    clients_file: Path
    journal_file: Path
    host: str = "127.0.0.1"
    port: int = 18111
    allowed_hosts: tuple[str, ...] = ("127.0.0.1:18111", "localhost:18111")
    allowed_origins: tuple[str, ...] = ()
    rate_limit: int = 60
    max_concurrency: int = 2
    tool_timeout_seconds: float = 25.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "backend_url", backend_origin(self.backend_url))
        object.__setattr__(self, "account_uuid", str(UUID(self.account_uuid)))
        if not self.allowed_hosts or any("*" in host for host in self.allowed_hosts):
            raise ValueError("Explicit MCP hosts required")
        if any("*" in origin for origin in self.allowed_origins):
            raise ValueError("Explicit MCP origins required")
        if any(not 0 < value <= maximum for value, maximum in (
                (self.rate_limit, 60), (self.max_concurrency, 2), (self.tool_timeout_seconds, 25))):
            raise ValueError("MCP limits may only be tightened")

    @classmethod
    def from_env(cls) -> PolytaoMCPConfig:
        prefix = "NEXPOLY_POLYTAO_MCP_"
        required = {name: os.environ.get(prefix + name, "") for name in
                    ("BACKEND_URL", "ACCOUNT_UUID", "SESSION_FILE", "CLIENTS_FILE", "JOURNAL_FILE")}
        if not all(required.values()):
            raise ValueError("Explicit backend, account, session, clients and journal configuration required")
        port = int(os.environ.get(prefix + "PORT", "18111"))
        split = lambda value: tuple(item.strip() for item in value.split(",") if item.strip())
        return cls(backend_url=required["BACKEND_URL"], account_uuid=required["ACCOUNT_UUID"],
                   session_file=Path(required["SESSION_FILE"]), clients_file=Path(required["CLIENTS_FILE"]),
                   journal_file=Path(required["JOURNAL_FILE"]), host=os.environ.get(prefix + "HOST", "127.0.0.1"),
                   port=port, allowed_hosts=split(os.environ.get(prefix + "ALLOWED_HOSTS", f"127.0.0.1:{port},localhost:{port}")),
                   allowed_origins=split(os.environ.get(prefix + "ALLOWED_ORIGINS", "")),
                   rate_limit=int(os.environ.get(prefix + "RATE_LIMIT", "60")),
                   max_concurrency=int(os.environ.get(prefix + "MAX_CONCURRENCY", "2")),
                   tool_timeout_seconds=float(os.environ.get(prefix + "TOOL_TIMEOUT_SECONDS", "25")))


class BackendSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_uuid: str
    backend_url: str
    cookie: str = Field(min_length=1, max_length=4096)
    session_id: str = Field(min_length=1, max_length=128)
    csrf_token: str = Field(min_length=1, max_length=256)
    expires_at: datetime

    @field_validator("cookie", "session_id", "csrf_token")
    @classmethod
    def safe_header(cls, value: str) -> str:
        if not value.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Invalid session header")
        return value

    @classmethod
    def read(cls, config: PolytaoMCPConfig) -> BackendSession:
        try:
            if config.session_file.parent.stat().st_mode & 0o077:
                raise ValueError("Session directory must be private")
            session = cls.model_validate_json(read_private_file(config.session_file))
            if (str(UUID(session.account_uuid)) != config.account_uuid
                    or backend_origin(session.backend_url) != config.backend_url):
                raise ValueError("Session target mismatch")
            if session.expires_at.tzinfo is None or session.expires_at <= datetime.now(timezone.utc):
                raise MCPFailure("SESSION_EXPIRED", "Development account session expired; ask the operator to renew the same account.")
            return session
        except MCPFailure:
            raise
        except (OSError, ValueError):
            raise MCPFailure("SESSION_UNAVAILABLE", "Development account session is unavailable or does not match the pinned account and backend.") from None

    def headers(self, *, post: bool = False) -> dict[str, str]:
        headers = {"Cookie": self.cookie, "X-Session-Context": self.session_id, "Accept": "application/json"}
        if post:
            headers.update({"X-CSRF-Token": self.csrf_token, "Origin": backend_origin(self.backend_url)})
        return headers
