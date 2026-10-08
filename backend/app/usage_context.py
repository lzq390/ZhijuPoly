"""Metadata-only execution context and opt-in, failure-visible usage collection.

HTTP transport lifetime is not execution lifetime. Child spans and explicit producer
retentions keep their root open until the real executor/iterator finishes.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
from threading import RLock
from time import monotonic
from typing import Any, Iterator
from uuid import UUID, uuid4

from app.auth.context import Identity, user_context
from task_usage.ledger import METRICS, STATUSES, TERMINAL_STATUSES, metering_enabled

logger = logging.getLogger("nexpoly.usage")
_context: ContextVar[UsageContext | None] = ContextVar("usage_context", default=None)
_collector: ContextVar[UsageCollector | None] = ContextVar("usage_collector", default=None)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class UsageContext:
    owner_user_id: str
    module: str
    task_id: str
    attempt_id: str = "1"
    parent_task_id: str | None = None
    root_task_id: str | None = None
    kind: str = "task"
    source: str = "runtime"

    def __post_init__(self):
        object.__setattr__(self, "owner_user_id", str(UUID(str(self.owner_user_id))))
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.module):
            raise ValueError("Invalid usage module")
        object.__setattr__(self, "root_task_id", self.root_task_id or self.task_id)


def current_usage_context() -> UsageContext | None:
    return _context.get()


def current_usage_collector() -> UsageCollector | None:
    return _collector.get()


@contextmanager
def usage_scope(context: UsageContext | None, *, collector: UsageCollector | None = None) -> Iterator[None]:
    context_token = _context.set(context)
    collector_token = _collector.set(collector if collector is not None else _collector.get())
    try:
        yield
    finally:
        _collector.reset(collector_token)
        _context.reset(context_token)


class UsageMeteringUnavailable(RuntimeError):
    """Safe public failure; never include repository exception text or DSNs."""

    def __init__(self):
        super().__init__("Usage metering is temporarily unavailable")


class UsageCollector:
    def __init__(self, dsn: str | None = None, *, enabled: bool | None = None,
                 repository=None, settings=None):
        self.enabled = metering_enabled() if enabled is None else enabled
        self.dsn, self.settings, self._repository = dsn, settings, repository
        self._lock = RLock()
        self._pending: OrderedDict[tuple[str, ...], tuple[UsageContext, dict[str, Any]]] = OrderedDict()
        self._roots: dict[tuple[str, str], UsageSpan] = {}
        self._degraded = False
        self._error_count = 0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"enabled": self.enabled, "degraded": self._degraded,
                    "pending_terminal_events": len(self._pending), "write_failures": self._error_count}

    def _write(self, context: UsageContext, event: dict[str, Any], connection=None):
        if self._repository is None:
            from app.services.usage_repository import UsageRepository
            self._repository = UsageRepository(self.dsn)
        if connection is not None:
            return self._repository.record_event(connection=connection, **event)
        # A copied producer can finish after HTTP identity has unwound. Rebind only
        # its server-captured owner; no session, cookies, or provider data is stored.
        with user_context(Identity(context.owner_user_id), self.settings):
            return self._repository.record_event(**event)

    def _failed(self, context: UsageContext, event: dict[str, Any]) -> None:
        self._degraded = True
        self._error_count += 1
        logger.error("usage_write_failed module=%s task_id=%s attempt_id=%s kind=%s status=%s",
                     context.module, context.task_id, context.attempt_id, context.kind, event["status"])

    def record(self, context: UsageContext, *, connection=None, defer_terminal: bool = False, **fields) -> bool:
        if not self.enabled:
            return False
        event = {**asdict(context), **fields}
        # Ledger event keys are unique per owner, across every task and kind.
        # Call sites may use a local stable key; namespace it exactly once here.
        identity = [context.module, context.task_id, context.attempt_id, context.kind,
                    str(event["event_key"])]
        event["event_key"] = "usage:" + hashlib.sha256(
            json.dumps(identity, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        key = (context.owner_user_id, context.module, context.task_id, context.attempt_id,
               context.kind, str(event["event_key"]))
        with self._lock:
            try:
                # Caller-owned transactions must remain atomic and never replay
                # unrelated events inside that transaction.
                if connection is None:
                    for pending_key, (pending_context, pending) in list(self._pending.items()):
                        self._write(pending_context, pending)
                        del self._pending[pending_key]
                self._write(context, event, connection=connection)
            except Exception:
                self._failed(context, event)
                if defer_terminal and connection is None:
                    # Only immutable metadata, stable event key/timestamps/absolute
                    # totals. Next real write retries these exact observations.
                    self._pending[key] = (context, event)
                    return False
                raise UsageMeteringUnavailable() from None
            self._degraded = bool(self._pending)
            return True

    def _root(self, context: UsageContext) -> UsageSpan | None:
        with self._lock:
            return self._roots.get((context.owner_user_id, context.root_task_id))


def emit_usage_event(context: UsageContext, *, status: str, event_key: str,
                     occurred_at: datetime | None = None, submitted_at: datetime | None = None,
                     started_at: datetime | None = None, finished_at: datetime | None = None,
                     metrics: dict[str, int | None] | None = None, connection=None) -> bool:
    collector = current_usage_collector()
    if collector is None or not collector.enabled:
        return False
    return collector.record(context, connection=connection, status=status, event_key=event_key,
                            occurred_at=occurred_at or _now(), submitted_at=submitted_at,
                            started_at=started_at, finished_at=finished_at, metrics=metrics)


def _status_for_exception(error: BaseException) -> str:
    name = type(error).__name__.lower()
    if "timeout" in name or getattr(error, "status_code", None) in {408, 504}:
        return "timed_out"
    if "cancel" in name or isinstance(error, GeneratorExit):
        return "interrupted"
    return "failed"


class UsageSpan:
    def __init__(self, context: UsageContext | None, collector: UsageCollector | None):
        self.context, self.collector = context, collector
        self._lock = RLock()
        self._started_at = None
        self._started_clock = None
        self._finished = False
        self._requested_status = None
        self._business_outcome = None
        self._children = 0
        self._metrics: dict[str, int | None] = {}
        self._parent_root = None
        self._scope = None

    @property
    def enabled(self) -> bool:
        return self.context is not None and self.collector is not None and self.collector.enabled

    def start(self, *, bind: bool = False) -> UsageSpan:
        if bind:
            raise ValueError("Use 'with span' or usage_scope; start never retains a cross-context token")
        if not self.enabled:
            return self
        with self._lock:
            if self._started_at is not None:
                return self
            started_at, clock = _now(), monotonic()
            is_root = self.context.kind == "task" and self.context.parent_task_id is None
            parent_root = None if is_root else self.collector._root(self.context)
            if parent_root is not None:
                # Retain before a potentially blocking durable write, so a
                # concurrent HTTP close cannot finish the root during start.
                parent_root._retain()
            try:
                self.collector.record(self.context, status="running", event_key="running",
                                      occurred_at=started_at, submitted_at=started_at,
                                      started_at=started_at, finished_at=None, metrics=None)
            except BaseException:
                if parent_root is not None:
                    parent_root._release()
                raise
            self._parent_root = parent_root
            self._started_at = started_at
            # Execution is the actual local call after the durable admission;
            # repository latency is not local-model execution time.
            self._started_clock = monotonic() if self.context.kind == "execution" else clock
            if is_root:
                with self.collector._lock:
                    self.collector._roots[(self.context.owner_user_id, self.context.task_id)] = self
        return self

    def set_metrics(self, metrics: dict[str, int | None]) -> None:
        if not self.enabled:
            return
        if set(metrics) - set(METRICS) or any(v is not None and
                (isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 2**63-1)
                for v in metrics.values()):
            raise ValueError("Usage metrics must be nullable absolute nonnegative integers")
        with self._lock:
            for key, value in metrics.items():
                prior = self._metrics.get(key)
                if value is not None and (prior is None or value >= prior):
                    self._metrics[key] = value
                elif key not in self._metrics:
                    self._metrics[key] = None

    def _retain(self) -> None:
        with self._lock:
            if self._finished:
                raise UsageMeteringUnavailable()
            self._children += 1

    def _release(self) -> None:
        with self._lock:
            self._children -= 1
            if self._children < 0:
                raise RuntimeError("Usage retention released twice")
            if self._children == 0 and self._requested_status is not None:
                self._finish_locked()

    def finish(self, status: str = "completed", metrics: dict[str, int | None] | None = None) -> bool:
        if not self.enabled:
            return False
        if status not in TERMINAL_STATUSES:
            raise ValueError("A usage span must finish with a terminal status")
        if metrics is not None:
            self.set_metrics(metrics)
        with self._lock:
            if self._finished or self._started_at is None:
                return False
            if self._requested_status is None:
                self._requested_status = status
            if self._children:
                return False
            return self._finish_locked()

    def mark_outcome(self, status: str) -> None:
        if status not in TERMINAL_STATUSES or status == "completed":
            raise ValueError("Only an explicitly known unsuccessful business outcome may refine the HTTP result")
        with self._lock:
            if not self._finished:
                self._business_outcome = status

    def _finish_locked(self) -> bool:
        if self._finished:
            return False
        finished_at = max(_now(), self._started_at)
        elapsed = max(0, int((monotonic() - self._started_clock) * 1000))
        metric = "execution_ms" if self.context.kind == "execution" else "observed_wall_ms"
        self._metrics.setdefault(metric, elapsed)
        terminal = (self._business_outcome if self._requested_status == "completed" and self._business_outcome
                    else self._requested_status)
        # Some established APIs report a proven timeout as HTTP 422/503. Refine
        # only that explicit business outcome; ordinary failures and successful
        # fallbacks do not inherit a child span's timeout. Cancellation wins.
        if self._business_outcome == "timed_out" and terminal in {"failed", "rejected"}:
            terminal = "timed_out"
        stored = self.collector.record(self.context, defer_terminal=True,
            status=terminal, event_key=terminal,
            occurred_at=finished_at, submitted_at=self._started_at, started_at=self._started_at,
            finished_at=finished_at, metrics=dict(self._metrics))
        self._finished = True
        if self.context.kind == "task" and self.context.parent_task_id is None:
            with self.collector._lock:
                self.collector._roots.pop((self.context.owner_user_id, self.context.task_id), None)
        if self._parent_root is not None:
            self._parent_root._release()
        return stored

    def __enter__(self) -> UsageSpan:
        self.start()
        self._scope = usage_scope(self.context, collector=self.collector)
        self._scope.__enter__()
        return self

    def __exit__(self, error_type, error, traceback):
        try:
            self.finish(_status_for_exception(error) if error is not None else "completed")
        finally:
            if self._scope is not None:
                self._scope.__exit__(error_type, error, traceback)
        return False


def usage_span(module: str | None = None, *, kind: str = "execution", task_id: str | None = None,
               attempt_id: str = "1") -> UsageSpan:
    parent, collector = current_usage_context(), current_usage_collector()
    if parent is None or collector is None or not collector.enabled:
        return UsageSpan(None, collector)
    context = UsageContext(parent.owner_user_id, module or parent.module,
                           task_id or f"{kind}:{uuid4()}", attempt_id,
                           parent_task_id=parent.task_id, root_task_id=parent.root_task_id, kind=kind)
    return UsageSpan(context, collector)


class UsageRetention:
    def __init__(self, root: UsageSpan | None):
        self.root, self.released = root, False
        self._lock = RLock()
        if root is not None:
            root._retain()

    def release(self):
        with self._lock:
            if self.released:
                return
            self.released = True
            if self.root is not None:
                self.root._release()


def retain_usage_execution() -> UsageRetention:
    context, collector = current_usage_context(), current_usage_collector()
    root = collector._root(context) if context is not None and collector is not None else None
    return UsageRetention(root)


def mark_current_usage_outcome(status: str) -> None:
    """Mark a known failure at a final business-to-HTTP boundary.

    Explicit timeouts can refine legacy 422/503 results without changing HTTP.
    Child failures alone are not sufficient: a successful fallback stays success.
    No request/response/SSE content is inspected or stored by this hook.
    """
    context, collector = current_usage_context(), current_usage_collector()
    if context is not None and collector is not None and collector.enabled:
        root = collector._root(context)
        if root is not None:
            root.mark_outcome(status)
