from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator
from uuid import UUID

from fastapi import HTTPException

SYSTEM_USER_ID = "00000000-0000-0000-0000-000000000001"


@dataclass(frozen=True, slots=True)
class Identity:
    user_id: str
    session_id: str = ""
    request_id: str = ""
    must_change_password: bool = False

    def __post_init__(self):
        object.__setattr__(self, "user_id", str(UUID(str(self.user_id))))


_identity: ContextVar[Identity | None] = ContextVar("nexpoly_identity", default=None)
_service: ContextVar[bool] = ContextVar("nexpoly_service_access", default=False)
_database_settings: ContextVar[object | None] = ContextVar("nexpoly_database_settings", default=None)


def current_identity(required: bool = True) -> Identity | None:
    value = _identity.get()
    if value is None and required:
        raise HTTPException(401, "请先登录。")
    return value


def current_owner_id(required: bool = True) -> str | None:
    identity = current_identity(required)
    return identity.user_id if identity is not None else None


def is_service_context() -> bool:
    return _service.get()


def database_settings():
    return _database_settings.get()


@contextmanager
def user_context(identity: Identity, settings=None) -> Iterator[Identity]:
    token = _identity.set(identity)
    service_token = _service.set(False)
    settings_token = _database_settings.set(settings) if settings is not None else None
    try:
        yield identity
    finally:
        if settings_token is not None:
            _database_settings.reset(settings_token)
        _service.reset(service_token)
        _identity.reset(token)


@contextmanager
def service_context(settings=None) -> Iterator[None]:
    """Explicit trusted service access; retain actor identity for audit only."""
    token = _service.set(True)
    settings_token = _database_settings.set(settings) if settings is not None else None
    try:
        yield
    finally:
        if settings_token is not None:
            _database_settings.reset(settings_token)
        _service.reset(token)
