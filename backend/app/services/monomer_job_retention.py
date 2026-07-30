from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Callable, ContextManager, Literal

from app.postgres_database import postgres_connection

from .monomer_dft_repository import (
    ACTIVE_STATUSES as DFT_ACTIVE_STATUSES,
    TERMINAL_STATUSES as DFT_TERMINAL_STATUSES,
    MonomerDftRepository,
)
from .monomer_dft_worker_client import MonomerDftWorkerClient, MonomerDftWorkerError
from .monomer_md_repository import (
    MONOMER_MD_ACTIVE_STATUSES,
    MONOMER_MD_TERMINAL_STATUSES,
    delete_monomer_md_job_cas_postgres,
    get_monomer_md_job_postgres,
    list_expired_monomer_md_jobs_postgres,
)
from .monomer_md_worker_client import MonomerMdWorkerClient, MonomerMdWorkerError


logger = logging.getLogger(__name__)
MD_RETENTION_ADVISORY_LOCK_ID = 742_128_925_057_015
RETENTION_INTERVAL_SECONDS = 60.0
RETENTION_MAX_DELETED = 100
RETENTION_MAX_SCANNED = 1_000
RETENTION_TIME_BUDGET_SECONDS = 30.0


class MonomerJobDeletionError(RuntimeError):
    status_code = 503


class MonomerJobDeletionConflict(MonomerJobDeletionError):
    status_code = 409


class MonomerJobStorageUnavailable(MonomerJobDeletionError):
    status_code = 503


class MonomerMdJobDeletionService:
    def __init__(
        self,
        *,
        dsn: str,
        worker: MonomerMdWorkerClient | None,
    ) -> None:
        self._dsn = dsn
        self._worker = worker

    def _get(self, job_id: str) -> dict[str, Any] | None:
        with postgres_connection(self._dsn) as connection:
            return get_monomer_md_job_postgres(connection, job_id)

    def _delete_cas(self, expected: dict[str, Any]) -> bool:
        with postgres_connection(self._dsn) as connection:
            return delete_monomer_md_job_cas_postgres(
                connection,
                job_id=expected["job_id"],
                expected_status=expected["status"],
                expected_finished_at=expected["finished_at"],
                expected_updated_at=expected["updated_at"],
            )

    async def delete(
        self,
        job_id: str,
        *,
        expected: dict[str, Any] | None = None,
    ) -> bool:
        current = expected or await asyncio.to_thread(self._get, job_id)
        if current is None:
            return False
        if current["status"] in MONOMER_MD_ACTIVE_STATUSES:
            raise MonomerJobDeletionConflict(
                "active monomer MD jobs cannot be permanently deleted"
            )
        if current["status"] not in MONOMER_MD_TERMINAL_STATUSES:
            raise MonomerJobDeletionConflict(
                "monomer MD job is not in a deletable terminal state"
            )
        if self._worker is None:
            raise MonomerJobStorageUnavailable(
                "monomer MD worker storage cleanup is not configured"
            )
        try:
            await asyncio.to_thread(self._worker.delete_artifacts, job_id)
        except MonomerMdWorkerError as exc:
            if exc.status_code == 409:
                raise MonomerJobDeletionConflict(str(exc)) from exc
            raise MonomerJobStorageUnavailable(str(exc)) from exc

        if await asyncio.to_thread(self._delete_cas, current):
            return True
        reread = await asyncio.to_thread(self._get, job_id)
        if reread is None:
            return False
        raise MonomerJobDeletionConflict(
            "monomer MD job changed while permanent deletion was in progress"
        )


class MonomerDftJobDeletionService:
    def __init__(
        self,
        *,
        repository: MonomerDftRepository,
        worker: MonomerDftWorkerClient | None,
    ) -> None:
        self._repository = repository
        self._worker = worker

    async def delete(
        self,
        job_id: str,
        *,
        expected: dict[str, Any] | None = None,
    ) -> bool:
        current = expected or await asyncio.to_thread(
            self._repository.get_job, job_id
        )
        if current is None:
            return False
        if current["status"] in DFT_ACTIVE_STATUSES:
            raise MonomerJobDeletionConflict(
                "active DFT jobs cannot be permanently deleted"
            )
        if current["status"] not in DFT_TERMINAL_STATUSES:
            raise MonomerJobDeletionConflict(
                "DFT job is not in a deletable terminal state"
            )
        if self._worker is None:
            raise MonomerJobStorageUnavailable(
                "monomer DFT worker storage cleanup is not configured"
            )
        try:
            await self._worker.purge_job(current)
        except MonomerDftWorkerError as exc:
            if exc.status_code == 409:
                raise MonomerJobDeletionConflict(str(exc)) from exc
            raise MonomerJobStorageUnavailable(str(exc)) from exc

        if await asyncio.to_thread(self._repository.delete_job_cas, current):
            return True
        reread = await asyncio.to_thread(self._repository.get_job, job_id)
        if reread is None:
            return False
        raise MonomerJobDeletionConflict(
            "DFT job changed while permanent deletion was in progress"
        )


@dataclass(slots=True)
class RetentionSweep:
    scanned: int = 0
    deleted: int = 0
    failed: int = 0
    duration_seconds: float = 0.0


RetentionStatus = Literal["disabled", "standby", "ready", "degraded"]


class MonomerJobRetentionReaper:
    """Independent, bounded retention loop for exactly one job module."""

    def __init__(
        self,
        *,
        name: str,
        enabled: bool,
        retention_days: int,
        leader_guard: Callable[[], ContextManager[bool]],
        list_candidates: Callable[
            [datetime | None, str | None, int], list[dict[str, Any]]
        ],
        delete_candidate: Callable[[dict[str, Any]], Any],
        interval_seconds: float = RETENTION_INTERVAL_SECONDS,
        configuration_error: str | None = None,
    ) -> None:
        self._name = name
        self.enabled = enabled
        self.retention_days = retention_days
        self._leader_guard = leader_guard
        self._list_candidates = list_candidates
        self._delete_candidate = delete_candidate
        self._interval_seconds = interval_seconds
        self._configuration_error = configuration_error
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._cursor: tuple[datetime, str] | None = None
        self.status: RetentionStatus = (
            "disabled"
            if not enabled
            else "degraded"
            if configuration_error
            else "standby"
        )
        self.last_sweep_at: datetime | None = None
        self.last_success_at: datetime | None = None
        self.last_error: str | None = configuration_error
        self.last_sweep = RetentionSweep()

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_retention_enabled": self.enabled,
            "job_retention_days": self.retention_days,
            "job_retention_status": self.status,
            "job_retention_last_sweep_at": (
                self.last_sweep_at.isoformat() if self.last_sweep_at else None
            ),
        }

    def start(self) -> None:
        if (
            not self.enabled
            or self._configuration_error is not None
            or self._task is not None
        ):
            return
        if self._stop.is_set():
            self._stop = asyncio.Event()
            self._wake = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(), name=f"{self._name}-retention-reaper"
        )

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        task = self._task
        if task is not None:
            await task
        self._task = None

    def kick(self) -> None:
        if self.enabled:
            self._wake.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.sweep()
            except Exception as exc:  # pragma: no cover - defensive loop fence
                self.status = "degraded"
                self.last_error = str(exc)
                logger.exception("%s retention sweep crashed", self._name)
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self._interval_seconds
                )
                self._wake.clear()
            except TimeoutError:
                pass

    async def sweep(self) -> RetentionSweep:
        if not self.enabled:
            self.status = "disabled"
            return RetentionSweep()
        if self._configuration_error is not None:
            self.status = "degraded"
            self.last_error = self._configuration_error
            return RetentionSweep(failed=1)
        started = monotonic()
        result = RetentionSweep()
        try:
            with self._leader_guard() as acquired:
                if not acquired:
                    self.status = "standby"
                    return result
                while (
                    result.scanned < RETENTION_MAX_SCANNED
                    and result.deleted < RETENTION_MAX_DELETED
                    and monotonic() - started < RETENTION_TIME_BUDGET_SECONDS
                ):
                    cursor_at, cursor_id = self._cursor or (None, None)
                    batch_limit = min(
                        100, RETENTION_MAX_SCANNED - result.scanned
                    )
                    candidates = await asyncio.to_thread(
                        self._list_candidates,
                        cursor_at,
                        cursor_id,
                        batch_limit,
                    )
                    if not candidates:
                        self._cursor = None
                        break
                    for candidate in candidates:
                        terminal_at = candidate.get("terminal_at")
                        if terminal_at is None:
                            terminal_at = (
                                candidate.get("finished_at")
                                or candidate.get("updated_at")
                            )
                        if isinstance(terminal_at, str):
                            terminal_at = datetime.fromisoformat(terminal_at)
                        if not isinstance(terminal_at, datetime):
                            result.failed += 1
                            continue
                        self._cursor = (terminal_at, str(candidate["job_id"]))
                        result.scanned += 1
                        try:
                            await self._delete_candidate(candidate)
                            result.deleted += 1
                        except MonomerJobStorageUnavailable as exc:
                            result.failed += 1
                            self.last_error = str(exc)
                            # A connectivity/storage proof outage affects every
                            # candidate, so stop this sweep and retry later.
                            break
                        except MonomerJobDeletionError as exc:
                            result.failed += 1
                            self.last_error = str(exc)
                            logger.warning(
                                "%s retention could not delete job %s: %s",
                                self._name,
                                candidate.get("job_id"),
                                exc,
                            )
                        if (
                            result.deleted >= RETENTION_MAX_DELETED
                            or result.scanned >= RETENTION_MAX_SCANNED
                            or monotonic() - started
                            >= RETENTION_TIME_BUDGET_SECONDS
                        ):
                            break
                    else:
                        continue
                    break
            self.last_sweep_at = datetime.now(timezone.utc)
            self.last_success_at = self.last_sweep_at
            self.status = "ready" if result.failed == 0 else "degraded"
        except Exception as exc:
            self.last_sweep_at = datetime.now(timezone.utc)
            self.status = "degraded"
            self.last_error = str(exc)
            logger.exception("%s retention sweep failed", self._name)
        result.duration_seconds = max(0.0, monotonic() - started)
        self.last_sweep = result
        logger.info(
            "%s retention sweep scanned=%d deleted=%d failed=%d duration=%.3fs",
            self._name,
            result.scanned,
            result.deleted,
            result.failed,
            result.duration_seconds,
        )
        return result


@contextmanager
def monomer_md_retention_leader(dsn: str):
    with postgres_connection(dsn) as connection:
        row = connection.execute(
            "SELECT pg_try_advisory_lock(%s) AS acquired",
            (MD_RETENTION_ADVISORY_LOCK_ID,),
        ).fetchone()
        acquired = bool(row and row["acquired"])
        try:
            yield acquired
        finally:
            if acquired:
                connection.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (MD_RETENTION_ADVISORY_LOCK_ID,),
                )


def list_monomer_md_retention_candidates(
    dsn: str,
    retention_days: int,
    after_terminal_at: datetime | None,
    after_job_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    with postgres_connection(dsn) as connection:
        return list_expired_monomer_md_jobs_postgres(
            connection,
            retention_days=retention_days,
            limit=limit,
            after_terminal_at=after_terminal_at,
            after_job_id=after_job_id,
        )
