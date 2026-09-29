"""Shared admission/start contract; domain stores remain task-state authorities."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Callable

from fastapi import HTTPException

from app.auth.context import SYSTEM_USER_ID, current_identity, current_owner_id, database_settings
from app.task_observability import TaskExecutionContext, log_task_event

_lock = Lock()
_users: Counter[tuple[str, str]] = Counter()
_global: Counter[str] = Counter()
_start_checker: Callable[[str], bool] | None = None
# GPU physical/global admission is still owned by GpuRuntimeRegistry/Broker.
_limits = {"online": 2, "ai": 2, "preflight": 2, "cpu": 2, "reverse": 2, "backend_gpu": 9}


def configure_task_control(start_checker: Callable[[str], bool]) -> None:
    global _start_checker
    _start_checker = start_checker


@dataclass
class AdmissionLease:
    channel: str
    owner_user_id: str
    released: bool = False
    context: TaskExecutionContext | None = None

    def release(self) -> None:
        with _lock:
            if not self.released:
                _users[(self.channel, self.owner_user_id)] -= 1
                if not _users[(self.channel, self.owner_user_id)]:
                    del _users[(self.channel, self.owner_user_id)]
                _global[self.channel] -= 1
                self.released = True
                if self.context is not None:
                    log_task_event(self.context, "admission_released")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.release()


def acquire_admission(channel: str, owner_user_id: str | None = None, *,
                      task_type: str | None = None, task_id: str | None = None) -> AdmissionLease:
    owner = owner_user_id or current_owner_id()
    if channel not in _limits:
        raise ValueError(f"unknown execution channel: {channel}")
    identity = current_identity(required=False)
    context = TaskExecutionContext(owner, identity.request_id if identity else None,
                                   task_type or channel, channel, task_id)
    with _lock:
        if _users[(channel, owner)] >= 1:
            log_task_event(context, "admission_rejected", reason="user_capacity")
            raise HTTPException(429, {"code": "user_capacity", "message": "此计算通道已有未完成任务。"})
        if _global[channel] >= _limits[channel]:
            log_task_event(context, "admission_rejected", reason="channel_capacity")
            raise HTTPException(429, {"code": "channel_capacity", "message": "计算通道繁忙，请稍后重试。"})
        _users[(channel, owner)] += 1
        _global[channel] += 1
    log_task_event(context, "admission_acquired")
    return AdmissionLease(channel, owner, context=context)


@contextmanager
def admission(channel: str, owner_user_id: str | None = None):
    with acquire_admission(channel, owner_user_id) as lease:
        yield lease


def authorize_memory_start(owner_user_id: str) -> bool:
    settings = database_settings()
    if settings is not None:
        from app.auth.service import AuthService
        return AuthService(settings).authorize_memory_start(owner_user_id)
    if _start_checker is None:
        raise RuntimeError("execution authorization is not configured")
    return bool(_start_checker(owner_user_id))


_START_TARGETS = {
    ("md.monomer_md_jobs", "job_id"),
    ("online_knowledge.jobs", "job_id"),
    ("polymerization_batch.jobs", "id"),
}


def authorize_start(connection, *, owner_user_id: str, table: str, key_column: str, key: str, attempt_token=None) -> bool:
    """Caller commits authorization before invoking any executor/network work.

    Lock order: domain admission lock, user row, parent task, child resources.
    An existing authorization survives account disable; ownership never changes.
    """
    if (table, key_column) not in _START_TARGETS or attempt_token is not None:
        raise ValueError("unsupported task start target")
    from psycopg import sql
    user = connection.execute("SELECT status,is_system FROM auth.users WHERE user_id=%s FOR UPDATE", (owner_user_id,)).fetchone()
    if user is None:
        return False
    target = sql.Identifier(*table.split("."))
    column = sql.Identifier(key_column)
    row = connection.execute(sql.SQL("SELECT owner_user_id,start_authorized_at FROM {} WHERE {}=%s FOR UPDATE").format(target, column), (key,)).fetchone()
    if row is None or str(row["owner_user_id"]) != str(owner_user_id):
        return False
    if row["start_authorized_at"] is not None:
        return True
    if user["status"] != "active" and not (user["is_system"] and str(owner_user_id) == SYSTEM_USER_ID):
        return False
    connection.execute(sql.SQL("UPDATE {} SET start_authorized_at=now() WHERE {}=%s").format(target, column), (key,))
    return True
