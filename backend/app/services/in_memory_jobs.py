from __future__ import annotations

import copy
import re
import sys
from dataclasses import dataclass, fields, is_dataclass
from threading import Lock
from time import monotonic
from typing import Any, Callable, Generic, TypeVar
from uuid import uuid4

from pydantic import BaseModel


DEFAULT_JOB_RETENTION_SECONDS = 24 * 60 * 60
DEFAULT_MAX_RETAINED_JOBS = 1000
DEFAULT_MAX_RETAINED_BYTES = 256 * 1024 * 1024
DEFAULT_RECORD_RESERVE_BYTES = 4096

_NAMESPACE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_INSTANCE_PATTERN = re.compile(r"^[0-9a-f]{16}$")
_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_LEGACY_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
# Conditional-generation and PolyTAO historically returned ``uuid4().hex``
# values without separators.  Match the UUID4 version/variant bits as well as
# the 32-character lowercase representation so arbitrary malformed tokens keep
# their 404 semantics.
_LEGACY_UUID4_HEX_PATTERN = re.compile(
    r"^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$"
)

T = TypeVar("T")
R = TypeVar("R")


class JobLookupError(KeyError):
    """Base class for public job lookup outcomes."""


class JobNotFoundError(JobLookupError):
    """The identifier is malformed or belongs to another API namespace."""


class JobGoneError(JobLookupError):
    """The identifier was valid, but its process-local record is no longer here."""


class JobStoreCapacityError(RuntimeError):
    """The bounded store cannot retain another record without evicting active work."""


@dataclass(slots=True)
class JobStoreStats:
    jobs: int
    bytes: int


@dataclass(slots=True)
class _StoredJob(Generic[T]):
    namespace: str
    value: T
    sequence: int
    size_bytes: int
    reserved_floor_bytes: int
    terminal: bool = False
    terminal_at: float | None = None
    reapable: bool = False


class BoundedInMemoryJobStore:
    """Process-local job retention shared by all in-process async GPU lanes.

    Active records are never evicted. Terminal records become eligible only
    after their executor Future has completed, preventing a logically
    cancelled but still-running task from losing its record.
    """

    def __init__(
        self,
        *,
        retention_seconds: float = DEFAULT_JOB_RETENTION_SECONDS,
        max_jobs: int = DEFAULT_MAX_RETAINED_JOBS,
        max_bytes: int = DEFAULT_MAX_RETAINED_BYTES,
        instance_id: str | None = None,
        monotonic_fn: Callable[[], float] = monotonic,
        record_reserve_bytes: int = DEFAULT_RECORD_RESERVE_BYTES,
    ) -> None:
        self.retention_seconds = max(0.0, float(retention_seconds))
        self.max_jobs = max(1, int(max_jobs))
        self.max_bytes = max(1, int(max_bytes))
        self.instance_id = instance_id or uuid4().hex[:16]
        self.record_reserve_bytes = max(0, int(record_reserve_bytes))
        if not _INSTANCE_PATTERN.fullmatch(self.instance_id):
            raise ValueError("job store instance_id must be 16 lowercase hexadecimal characters")
        self._monotonic = monotonic_fn
        self._records: dict[str, _StoredJob[Any]] = {}
        self._total_bytes = 0
        self._next_sequence = 0
        self._lock = Lock()

    def create(self, namespace: str, factory: Callable[[str], T]) -> T:
        self._validate_namespace(namespace)
        job_id = f"{namespace}.{self.instance_id}.{uuid4().hex}"
        value = factory(job_id)
        size_bytes = _deep_sizeof(value) + self.record_reserve_bytes
        with self._lock:
            self._prune_expired_locked()
            if not self._can_fit_after_reaping_locked(
                additional_jobs=1,
                additional_bytes=size_bytes,
            ):
                raise JobStoreCapacityError(
                    "In-memory job retention capacity is full; active jobs cannot be evicted"
                )
            self._evict_to_fit_locked(additional_jobs=1, additional_bytes=size_bytes)
            if len(self._records) + 1 > self.max_jobs or self._total_bytes + size_bytes > self.max_bytes:
                raise JobStoreCapacityError(
                    "In-memory job retention capacity is full; active jobs cannot be evicted"
                )
            self._next_sequence += 1
            self._records[job_id] = _StoredJob(
                namespace=namespace,
                value=value,
                sequence=self._next_sequence,
                size_bytes=size_bytes,
                reserved_floor_bytes=size_bytes,
            )
            self._total_bytes += size_bytes
        return value

    def read(self, namespace: str, job_id: str, reader: Callable[[T], R]) -> R:
        with self._lock:
            self._prune_expired_locked()
            record = self._lookup_locked(namespace, job_id)
            return reader(record.value)

    def mutate(
        self,
        namespace: str,
        job_id: str,
        mutator: Callable[[T], R],
        *,
        terminal: bool | None = None,
    ) -> R:
        with self._lock:
            self._prune_expired_locked()
            record = self._lookup_locked(namespace, job_id)
            old_value = copy.deepcopy(record.value)
            old_size = record.size_bytes
            old_terminal = record.terminal
            old_terminal_at = record.terminal_at
            try:
                result = mutator(record.value)
                new_size = max(_deep_sizeof(record.value), record.reserved_floor_bytes)
                if terminal is not None:
                    if terminal and not record.terminal:
                        record.terminal_at = self._monotonic()
                    elif not terminal:
                        record.terminal_at = None
                    record.terminal = terminal
                record.size_bytes = new_size
                self._total_bytes += new_size - old_size
                if not self._can_fit_after_reaping_locked(protected_job_id=job_id):
                    raise JobStoreCapacityError(
                        "In-memory job result exceeds the shared retention capacity"
                    )
                self._evict_to_fit_locked(protected_job_id=job_id)
                if len(self._records) > self.max_jobs or self._total_bytes > self.max_bytes:
                    raise JobStoreCapacityError(
                        "In-memory job result exceeds the shared retention capacity"
                    )
                return result
            except Exception:
                self._total_bytes += old_size - record.size_bytes
                record.value = old_value
                record.size_bytes = old_size
                record.terminal = old_terminal
                record.terminal_at = old_terminal_at
                raise

    def mark_reapable(self, namespace: str, job_id: str) -> None:
        with self._lock:
            try:
                record = self._lookup_locked(namespace, job_id)
            except JobLookupError:
                return
            record.reapable = True
            self._prune_expired_locked()
            self._evict_to_fit_locked()

    def delete(self, namespace: str, job_id: str) -> None:
        with self._lock:
            record = self._lookup_locked(namespace, job_id)
            self._remove_locked(job_id, record)

    def stats(self, namespace: str | None = None) -> JobStoreStats:
        if namespace is not None:
            self._validate_namespace(namespace)
        with self._lock:
            self._prune_expired_locked()
            selected = [
                record
                for record in self._records.values()
                if namespace is None or record.namespace == namespace
            ]
            return JobStoreStats(
                jobs=len(selected),
                bytes=sum(record.size_bytes for record in selected),
            )

    def _lookup_locked(self, namespace: str, job_id: str) -> _StoredJob[Any]:
        self._validate_namespace(namespace)
        raw_job_id = str(job_id)
        # Legacy Conditional and PostgreSQL-backed PolyTAO jobs used bare
        # UUIDs. They cannot survive this process-local cutover, so clients
        # with an already-open job receive the explicit expired response.
        if (
            _LEGACY_UUID_PATTERN.fullmatch(raw_job_id)
            or _LEGACY_UUID4_HEX_PATTERN.fullmatch(raw_job_id)
        ):
            raise JobGoneError(job_id)
        parts = raw_job_id.split(".")
        if (
            len(parts) != 3
            or parts[0] != namespace
            or not _INSTANCE_PATTERN.fullmatch(parts[1])
            or not _TOKEN_PATTERN.fullmatch(parts[2])
        ):
            raise JobNotFoundError(job_id)
        if parts[1] != self.instance_id:
            raise JobGoneError(job_id)
        record = self._records.get(job_id)
        if record is None:
            raise JobGoneError(job_id)
        return record

    def _prune_expired_locked(self) -> None:
        if not self._records:
            return
        now = self._monotonic()
        expired = [
            (job_id, record)
            for job_id, record in self._records.items()
            if record.terminal
            and record.reapable
            and record.terminal_at is not None
            and now - record.terminal_at >= self.retention_seconds
        ]
        for job_id, record in expired:
            self._remove_locked(job_id, record)

    def _evict_to_fit_locked(
        self,
        *,
        additional_jobs: int = 0,
        additional_bytes: int = 0,
        protected_job_id: str | None = None,
    ) -> None:
        while (
            len(self._records) + additional_jobs > self.max_jobs
            or self._total_bytes + additional_bytes > self.max_bytes
        ):
            candidates = [
                (job_id, record)
                for job_id, record in self._records.items()
                if job_id != protected_job_id and record.terminal and record.reapable
            ]
            if not candidates:
                return
            job_id, record = min(
                candidates,
                key=lambda item: (
                    item[1].terminal_at if item[1].terminal_at is not None else float("inf"),
                    item[1].sequence,
                ),
            )
            self._remove_locked(job_id, record)

    def _can_fit_after_reaping_locked(
        self,
        *,
        additional_jobs: int = 0,
        additional_bytes: int = 0,
        protected_job_id: str | None = None,
    ) -> bool:
        """Check the best possible fit without mutating retained history.

        This preflight keeps capacity failures atomic: an oversized new job or
        result must not evict otherwise valid terminal records before it is
        rejected.
        """
        candidates = [
            record
            for job_id, record in self._records.items()
            if job_id != protected_job_id and record.terminal and record.reapable
        ]
        minimum_jobs = len(self._records) - len(candidates) + additional_jobs
        minimum_bytes = (
            self._total_bytes
            - sum(record.size_bytes for record in candidates)
            + additional_bytes
        )
        return minimum_jobs <= self.max_jobs and minimum_bytes <= self.max_bytes

    def _remove_locked(self, job_id: str, record: _StoredJob[Any]) -> None:
        if self._records.get(job_id) is not record:
            return
        self._records.pop(job_id, None)
        self._total_bytes = max(0, self._total_bytes - record.size_bytes)

    @staticmethod
    def _validate_namespace(namespace: str) -> None:
        if not _NAMESPACE_PATTERN.fullmatch(str(namespace)):
            raise ValueError("job namespace must contain lowercase letters, digits, or underscores")


def _deep_sizeof(value: Any, seen: set[int] | None = None) -> int:
    """Estimate retained Python memory while handling Pydantic and slot dataclasses."""
    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return 0
    seen.add(identity)

    size = sys.getsizeof(value)
    if value is None or isinstance(value, (bool, int, float, complex, str, bytes, bytearray)):
        return size
    if isinstance(value, BaseModel):
        return size + _deep_sizeof(value.model_dump(mode="python"), seen)
    if is_dataclass(value) and not isinstance(value, type):
        return size + sum(_deep_sizeof(getattr(value, item.name), seen) for item in fields(value))
    if isinstance(value, dict):
        return size + sum(
            _deep_sizeof(key, seen) + _deep_sizeof(item, seen)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return size + sum(_deep_sizeof(item, seen) for item in value)
    if hasattr(value, "__dict__"):
        return size + _deep_sizeof(vars(value), seen)
    return size
