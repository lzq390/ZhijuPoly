from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from numbers import Integral
from threading import Lock
from typing import Any

from .monomer_dft_schema import (
    MonomerDftSchemaState,
    probe_monomer_dft_schema,
)


CONTROL_KEY = "production"
ACTIVE_STATUSES = ("pending", "submitted", "running")
MONOMER_DFT_ACTIVE_STATUSES = (
    "pending",
    "queued",
    "running",
    "cancel_requested",
)
FULL_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class DrainState:
    enabled: bool
    reason: str | None
    release_sha: str | None
    activated_at: datetime | None
    activated_by: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ActiveJobSummary:
    counts: dict[str, int]
    active_jobs_schema_version: int = 1

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def validate_release_sha(value: object, *, field_name: str = "release_sha") -> str:
    if not isinstance(value, str) or FULL_SHA_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"{field_name} must be a full lowercase 40-character Git SHA"
        )
    return value


class InflightApiWriteTracker:
    """Process-local admission counter for public API write requests."""

    def __init__(self) -> None:
        self._active = 0
        self._lock = Lock()

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def enter(self) -> None:
        with self._lock:
            self._active += 1

    def exit(self) -> None:
        with self._lock:
            if self._active <= 0:
                raise RuntimeError("inflight API write counter underflow")
            self._active -= 1

    @contextmanager
    def track(self):
        self.enter()
        try:
            yield
        finally:
            self.exit()


def get_drain_state(connection: Any, *, lock: bool = False) -> DrainState:
    suffix = " FOR UPDATE" if lock else ""
    row = connection.execute(
        """
        SELECT drain_enabled, reason, release_sha, activated_at, activated_by, updated_at
        FROM governance.deployment_control
        WHERE control_key = %s
        """ + suffix,
        (CONTROL_KEY,),
    ).fetchone()
    if row is None:
        raise RuntimeError("governance.deployment_control is not initialized; apply Postgres migrations")
    return DrainState(
        enabled=bool(row["drain_enabled"]),
        reason=row["reason"],
        release_sha=row["release_sha"],
        activated_at=row["activated_at"],
        activated_by=row["activated_by"],
        updated_at=row["updated_at"],
    )


def enable_drain(
    connection: Any,
    *,
    reason: str,
    activated_by: str,
    release_sha: str,
) -> DrainState:
    clean_reason = reason.strip()
    clean_actor = activated_by.strip()
    if not clean_reason or not clean_actor:
        raise ValueError("Drain reason and activated_by must be non-empty")
    release_sha = validate_release_sha(release_sha)
    current = get_drain_state(connection, lock=True)
    if current.enabled and (
        current.release_sha != release_sha or current.activated_by != clean_actor
    ):
        raise RuntimeError(
            "Deployment drain is already owned by "
            f"{current.activated_by or 'an unknown actor'} for {current.release_sha or 'maintenance'}"
        )
    row = connection.execute(
        """
        UPDATE governance.deployment_control
        SET drain_enabled = true,
            reason = %s,
            release_sha = %s,
            activated_at = now(),
            activated_by = %s,
            updated_at = now()
        WHERE control_key = %s
        RETURNING drain_enabled, reason, release_sha, activated_at, activated_by, updated_at
        """,
        (clean_reason, release_sha, clean_actor, CONTROL_KEY),
    ).fetchone()
    return _drain_state_from_row(row)


def disable_drain(
    connection: Any,
    *,
    expected_activated_by: str,
    expected_release_sha: str,
) -> DrainState:
    expected_activated_by = expected_activated_by.strip()
    if not expected_activated_by:
        raise ValueError("expected_activated_by must be non-empty")
    expected_release_sha = validate_release_sha(
        expected_release_sha,
        field_name="expected_release_sha",
    )
    current = get_drain_state(connection, lock=True)
    if not current.enabled:
        return current
    if (
        current.activated_by != expected_activated_by
        or current.release_sha != expected_release_sha
    ):
        raise RuntimeError(
            "Refusing to resume a deployment drain owned by "
            f"{current.activated_by or 'an unknown actor'} for "
            f"{current.release_sha or 'maintenance'}"
        )
    row = connection.execute(
        """
        UPDATE governance.deployment_control
        SET drain_enabled = false,
            reason = NULL,
            release_sha = NULL,
            activated_at = NULL,
            activated_by = NULL,
            updated_at = now()
        WHERE control_key = %s
        RETURNING drain_enabled, reason, release_sha, activated_at, activated_by, updated_at
        """,
        (CONTROL_KEY,),
    ).fetchone()
    return _drain_state_from_row(row)


def count_active_postgres_jobs(connection: Any) -> ActiveJobSummary:
    counts = {
        "monomer_md": _count_statuses(connection, "md", "monomer_md_jobs"),
        "online_knowledge": _count_statuses(connection, "online_knowledge", "jobs"),
    }
    dft_schema = probe_monomer_dft_schema(connection)
    if dft_schema.state is MonomerDftSchemaState.INVALID:
        raise RuntimeError(
            "monomer DFT deployment schema is invalid: "
            f"{dft_schema.reason}"
        )
    if dft_schema.state is MonomerDftSchemaState.READY:
        counts["monomer_dft"] = _count_statuses(
            connection,
            "monomer_dft",
            "jobs",
            statuses=MONOMER_DFT_ACTIVE_STATUSES,
        )
        return ActiveJobSummary(
            counts=counts,
            active_jobs_schema_version=2,
        )
    return ActiveJobSummary(counts=counts, active_jobs_schema_version=1)


def count_in_memory_jobs(app: Any) -> ActiveJobSummary:
    state = getattr(app, "state", None)
    if state is None:
        raise RuntimeError("application runtime state is unavailable")

    inflight_tracker = _required_runtime_component(state, "inflight_api_writes")
    conditional_manager = _required_runtime_component(
        state,
        "conditional_generation_job_manager",
    )
    reverse_design_manager = _required_runtime_component(
        state,
        "reverse_design_job_manager",
    )
    polytao_manager = _required_runtime_component(state, "polytao_job_manager")
    gpu_registry = _required_runtime_component(state, "gpu_runtime_registry")
    counts = {
        "inflight_api_writes": _count_inflight_api_writes(inflight_tracker),
        "conditional_generation": _count_manager_activity(
            conditional_manager,
            "conditional-generation job manager",
        ),
        "reverse_design": _count_manager_activity(
            reverse_design_manager,
            "reverse-design job manager",
        ),
        "polytao": _count_manager_activity(
            polytao_manager,
            "PolyTAO job manager",
        ),
        "gpu_inference": _count_gpu_inference(gpu_registry),
        "gpu_waiting": _count_gpu_waiting(gpu_registry),
    }
    return ActiveJobSummary(counts=counts)


def aggregate_active_jobs(connection: Any, app: Any | None = None) -> ActiveJobSummary:
    persistent = count_active_postgres_jobs(connection)
    counts = dict(persistent.counts)
    if app is not None:
        counts.update(count_in_memory_jobs(app).counts)
    return ActiveJobSummary(
        counts=counts,
        active_jobs_schema_version=persistent.active_jobs_schema_version,
    )


def _drain_state_from_row(row: Any) -> DrainState:
    if row is None:
        raise RuntimeError("governance.deployment_control is not initialized; apply Postgres migrations")
    return DrainState(
        enabled=bool(row["drain_enabled"]),
        reason=row["reason"],
        release_sha=row["release_sha"],
        activated_at=row["activated_at"],
        activated_by=row["activated_by"],
        updated_at=row["updated_at"],
    )


def _table_exists(connection: Any, schema: str, table: str) -> bool:
    row = connection.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema, table),
    ).fetchone()
    return row is not None


def _count_statuses(
    connection: Any,
    schema: str,
    table: str,
    *,
    statuses: tuple[str, ...] = ACTIVE_STATUSES,
) -> int:
    if not _table_exists(connection, schema, table):
        raise RuntimeError(
            f"required deployment job table is unavailable: {schema}.{table}"
        )
    # schema/table are fixed internal constants, never request input.
    row = connection.execute(
        f"SELECT COUNT(*) AS count FROM {schema}.{table} WHERE status = ANY(%s)",
        (list(statuses),),
    ).fetchone()
    return int(row["count"])


def _required_runtime_component(state: Any, name: str) -> Any:
    try:
        component = getattr(state, name)
    except AttributeError as exc:
        raise RuntimeError(
            f"required deployment runtime component is missing: {name}"
        ) from exc
    if component is None:
        raise RuntimeError(
            f"required deployment runtime component is unavailable: {name}"
        )
    return component


def _read_nonnegative_counter(component: Any, name: str, component_label: str) -> int:
    try:
        value = getattr(component, name)
        if callable(value):
            value = value()
    except Exception as exc:
        raise RuntimeError(
            f"failed to read {component_label} counter: {name}"
        ) from exc
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise RuntimeError(
            f"{component_label} counter {name} must be a non-negative integer"
        )
    normalized = int(value)
    if normalized < 0:
        raise RuntimeError(
            f"{component_label} counter {name} must be a non-negative integer"
        )
    return normalized


def _count_manager_activity(manager: Any, component_label: str) -> int:
    """Read the manager's public drain counters without inspecting job storage.

    ``active_jobs`` represents logical pending/running work and
    ``active_executions`` represents live futures.  They normally overlap, so
    the maximum is the conservative non-double-counting admission value.
    """

    active_jobs = _read_nonnegative_counter(manager, "active_jobs", component_label)
    active_executions = _read_nonnegative_counter(
        manager,
        "active_executions",
        component_label,
    )
    return max(active_jobs, active_executions)


def _count_gpu_inference(registry: Any) -> int:
    return _read_nonnegative_counter(
        registry,
        "active_inferences",
        "GPU runtime registry",
    )


def _count_gpu_waiting(registry: Any) -> int:
    return _read_nonnegative_counter(
        registry,
        "waiting_inferences",
        "GPU runtime registry",
    )


def _count_inflight_api_writes(tracker: Any) -> int:
    return _read_nonnegative_counter(
        tracker,
        "active",
        "inflight API write tracker",
    )
