"""Meter backend-owned jobs without deriving physical execution from queue time."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
from functools import lru_cache
from uuid import uuid4

from task_usage.ledger import metering_enabled
from app.usage_context import (
    UsageContext, current_usage_collector, emit_usage_event, usage_scope,
)


@lru_cache(maxsize=1)
def backend_instance_id():
    return uuid4().hex


def _time(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Background usage requires an aware timestamp")
    return result


class BackgroundUsage:
    def __init__(self, *, owner_user_id, module, task_id, submitted_at, channel):
        native = f"{module}:{task_id}"
        self.context = UsageContext(owner_user_id=owner_user_id, module=module,
            task_id=native, attempt_id=f"backend:{backend_instance_id()}:1", root_task_id=native)
        self.collector = current_usage_collector()
        if self.collector is None:
            raise RuntimeError("Usage collector unavailable for background submission")
        self.submitted_at = _time(submitted_at) if submitted_at is not None else datetime.now(timezone.utc)
        self.channel = channel
        self.started_at = None
        self._terminal = None

    def scope(self):
        return usage_scope(self.context, collector=self.collector)

    def _emit(self, status, occurred, *, finished_at=None, metrics=None):
        fields = dict(status=status,
            event_key=f"{self.context.task_id}:{self.context.attempt_id}:{status}",
            occurred_at=occurred, submitted_at=self.submitted_at,
            started_at=self.started_at, finished_at=finished_at, metrics=metrics)
        if finished_at is not None:
            # Computing has ended. A metering outage must never block native
            # Future cleanup; preserve this exact payload for the collector retry.
            self.collector.record(self.context, defer_terminal=True, **fields)
        else:
            with self.scope():
                emit_usage_event(self.context, **fields)

    def running(self):
        observed = datetime.now(timezone.utc)
        # GPU managers enter their executor before acquiring a physical slot.
        # The GPU registry records that separate execution span.
        if self.channel != "backend_gpu":
            self.started_at = observed
        self._emit("running", observed)

    def finish(self, snapshot):
        if self._terminal is not None:
            return
        status = snapshot.status
        status = {"found_enough": "completed", "exhausted": "completed"}.get(status, status)
        if status not in {"completed", "failed", "cancelled", "timed_out", "interrupted", "rejected"}:
            return  # A queued/running observation cannot establish termination.
        message = getattr(snapshot, "error", None) or getattr(snapshot, "error_message", None)
        if status == "failed" and isinstance(message, str) and message.startswith("GPU_QUEUE_TIMEOUT"):
            status = "timed_out"
        if snapshot.finished_at is None:
            return  # Never invent the end of an unobserved execution.
        finished = _time(snapshot.finished_at)
        metrics = {"observed_wall_ms": max(0, round((finished-self.submitted_at).total_seconds()*1000))}
        self._emit(status, finished, finished_at=finished, metrics=metrics)
        self._terminal = status

    def rejected(self):
        if self._terminal is not None:
            return
        now = datetime.now(timezone.utc)
        self._emit("rejected", now, finished_at=now)
        self._terminal = "rejected"


def begin_background_usage(*, owner_user_id, task_type, task_id, channel, submitted_at=None):
    if not metering_enabled():
        return None
    module = {"tg_reverse": "reverse_design"}.get(task_type, task_type)
    if module not in {"conditional_generation", "polytao", "reverse_design"} or not task_id:
        raise ValueError("Unknown background usage module")
    handle = BackgroundUsage(owner_user_id=owner_user_id, module=module, task_id=task_id,
                             submitted_at=submitted_at, channel=channel)
    try:
        handle._emit("queued", handle.submitted_at)
    except BaseException:
        # Dispatch has not happened. Preserve the proven rejection even if the
        # queued commit acknowledgement was lost; never leave a false live task.
        handle.rejected()
        raise
    return handle


def background_usage_scope(handle):
    return nullcontext() if handle is None else handle.scope()


def finish_background_usage(future, snapshot):
    handle = getattr(future, "_nexpoly_usage", None)
    if handle is not None:
        handle.finish(snapshot)
