"""Standalone, read-only host monitoring API with live development authentication."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
from typing import Callable, Literal
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import validate_history, validate_snapshot
from .task_history_models import validate_task_history


AUTH_URL = "http://127.0.0.1:18000/api/v1/auth/session"
COOKIE_NAME = "nexpoly_dev_session"
EXPECTED_USERNAMES = frozenset({"zhiju000", "zhiju001", "zhiju002"})
DEFAULT_CONFIG_PATH = Path.home() / ".local/state/nexpoly/server-monitoring/config.json"
MAX_AUTH_BYTES = 32 * 1024
MAX_CONFIG_BYTES = 16 * 1024
MAX_STATE_BYTES = {"latest.json": 256 * 1024, "history.json": 2 * 1024 * 1024,
                   "gpu-tasks.json": 2 * 1024 * 1024}
PREFIX = "/api/v1/server-monitoring"


class AllowedUser(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    username: Literal["zhiju000", "zhiju001", "zhiju002"]
    user_id: str

    @field_validator("user_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("user_id must be a canonical UUID")
        return value


class MonitoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    state_dir: str = str(DEFAULT_CONFIG_PATH.parent / "state")
    allowed_users: list[AllowedUser] = Field(default_factory=list, max_length=3)

    @field_validator("state_dir")
    @classmethod
    def absolute_state_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("state_dir must be an absolute fixed path")
        return value

    @model_validator(mode="after")
    def exact_allowlist(self):
        names = {user.username for user in self.allowed_users}
        ids = {user.user_id for user in self.allowed_users}
        if len(names) != len(self.allowed_users) or len(ids) != len(self.allowed_users):
            raise ValueError("allowed users must have distinct names and IDs")
        if self.enabled and names != EXPECTED_USERNAMES:
            raise ValueError("enabled monitoring requires the three configured development identities")
        return self


class _AuthUser(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: str
    username: str
    must_change_password: bool

    @field_validator("id")
    @classmethod
    def canonical_id(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("invalid authentication identity")
        return value


class _AuthSession(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    authenticated: bool
    user: _AuthUser | None = None
    session_id: str | None = None

    @field_validator("session_id")
    @classmethod
    def canonical_session(cls, value: str | None) -> str | None:
        if value is not None and str(UUID(value)) != value:
            raise ValueError("invalid session identity")
        return value


def _reject(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code})


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _decode_json(raw: bytes):
    def invalid_constant(_value):
        raise ValueError("non-finite JSON value")
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=invalid_constant)


def _read_private_json(path: Path, limit: int):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        # Replacement may unlink the already-open private inode before this
        # first stat. Zero links is valid; multiple names are still rejected.
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink not in (0, 1) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > limit):
            raise ValueError("invalid private state file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        after = os.fstat(descriptor)
        # Atomic replacement unlinks this fd's inode and changes ctime, while
        # its complete previous contents remain safe to read.
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if len(raw) > limit or len(raw) != info.st_size or identity(info) != identity(after):
            raise ValueError("state file changed during read or exceeds size limit")
        return _decode_json(raw)
    finally:
        os.close(descriptor)


def load_config(path: Path) -> MonitoringConfig:
    try:
        payload = _read_private_json(path, MAX_CONFIG_BYTES)
    except FileNotFoundError:
        return MonitoringConfig()
    return MonitoringConfig.model_validate(payload)


def _session_cookie(request: Request) -> str:
    matches = []
    for header in request.headers.getlist("cookie"):
        for part in header.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name == COOKIE_NAME:
                matches.append(value)
    if len(matches) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", matches[0]):
        raise _reject(401, "authentication_required")
    return matches[0]


async def _verify_session(cookie: str, transport: httpx.AsyncBaseTransport | None) -> _AuthSession:
    # A fresh client prevents one caller's Set-Cookie from affecting another.
    # The destination, method and forwarded credential name are never caller-selected.
    try:
        async with asyncio.timeout(3):
            async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False,
                                         transport=transport) as client:
                async with client.stream("GET", AUTH_URL, headers={"Cookie": f"{COOKIE_NAME}={cookie}"}) as response:
                    if response.status_code != 200:
                        raise ValueError("authentication source failed")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_AUTH_BYTES:
                            raise ValueError("authentication response too large")
        return _AuthSession.model_validate(_decode_json(bytes(raw)))
    except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
        raise _reject(503, "authentication_unavailable") from None


def create_app(config_path: Path | str | None = None, *,
               auth_transport: httpx.AsyncBaseTransport | None = None,
               clock: Callable[[], datetime] | None = None) -> FastAPI:
    """Create only this API; injectable transport/clock are used by isolated tests."""
    selected = Path(config_path or os.environ.get("SERVER_MONITORING_CONFIG", str(DEFAULT_CONFIG_PATH)))
    if not selected.is_absolute():
        raise ValueError("monitor configuration path must be absolute")
    config = load_config(selected)
    now = clock or (lambda: datetime.now(timezone.utc))
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(HTTPException)
    async def monitoring_error(_request: Request, error: HTTPException):
        # The shared browser transport retires identity on top-level error codes.
        content = error.detail if isinstance(error.detail, dict) else {"detail": error.detail}
        return JSONResponse(status_code=error.status_code, content=content, headers=error.headers)

    @app.middleware("http")
    async def no_cache(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Vary"] = "Cookie, X-Session-Context"
        return response

    async def authorize(request: Request):
        if not config.enabled:
            raise _reject(503, "monitoring_disabled")
        session = await _verify_session(_session_cookie(request), auth_transport)
        if not session.authenticated:
            raise _reject(401, "authentication_required")
        if session.user is None or session.session_id is None:
            raise _reject(503, "authentication_unavailable")
        user = session.user
        if user.must_change_password:
            raise _reject(403, "password_change_required")
        if not any(user.username == allowed.username and user.id == allowed.user_id
                   for allowed in config.allowed_users):
            raise _reject(403, "monitoring_forbidden")
        contexts = request.headers.getlist("x-session-context")
        if contexts != [session.session_id]:
            raise _reject(409, "session_context_mismatch")

    async def read_state(filename: str):
        try:
            payload = await asyncio.to_thread(_read_private_json, Path(config.state_dir) / filename,
                                              MAX_STATE_BYTES[filename])
            validator = {"latest.json": validate_snapshot, "history.json": validate_history,
                         "gpu-tasks.json": validate_task_history}[filename]
            model = validator(payload)
            data = model.model_dump(mode="json")
            if filename in {"history.json", "gpu-tasks.json"}:
                latest_payload = await asyncio.to_thread(_read_private_json, Path(config.state_dir) / "latest.json",
                                                        MAX_STATE_BYTES["latest.json"])
                latest = validate_snapshot(latest_payload)
                if filename == "history.json" and (data["collector_id"], data["started_at"]) != (latest.collector_id, latest.started_at):
                    raise ValueError("history belongs to a previous collector")
                if filename == "gpu-tasks.json" and set(data["gpu_uuids"]) != {gpu.uuid for gpu in latest.gpus}:
                    raise ValueError("task records do not match this host's GPU inventory")
            captured = datetime.fromisoformat(data["captured_at"].replace("Z", "+00:00"))
            age = (now() - captured).total_seconds()
            if age < 0:
                raise ValueError("future sample")
        except (OSError, ValueError, TypeError, KeyError):
            raise _reject(503, "monitoring_unavailable") from None
        freshness = "unavailable" if age > 60 else "stale" if age > 15 else "fresh"
        return {**data, "age_seconds": round(age, 3), "freshness": freshness}

    @app.get(PREFIX + "/access", dependencies=[Depends(authorize)])
    async def access():
        return {"can_read": True}

    @app.get(PREFIX + "/snapshot", dependencies=[Depends(authorize)])
    async def snapshot():
        return await read_state("latest.json")

    @app.get(PREFIX + "/history", dependencies=[Depends(authorize)])
    async def history():
        return await read_state("history.json")

    @app.get(PREFIX + "/gpu-tasks", dependencies=[Depends(authorize)])
    async def gpu_tasks():
        return await read_state("gpu-tasks.json")

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    arguments = parser.parse_args()
    import uvicorn
    uvicorn.run(create_app(arguments.config), host="127.0.0.1", port=5319,
                access_log=False, proxy_headers=False, workers=1)


if __name__ == "__main__":
    main()
