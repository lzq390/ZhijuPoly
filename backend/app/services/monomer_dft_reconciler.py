from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .monomer_dft_repository import (
    MonomerDftJobNotFound,
    MonomerDftRepository,
    MonomerDftStaleAttempt,
    WORKER_STAGES,
    sanitize_public_text,
)
from .monomer_dft_worker_client import MonomerDftWorkerClient, MonomerDftWorkerError


logger = logging.getLogger(__name__)


class MonomerDftReconciler:
    def __init__(
        self,
        *,
        repository: MonomerDftRepository,
        worker: MonomerDftWorkerClient,
        interval_seconds: float,
        artifact_retention_days: int = 30,
    ) -> None:
        self._repository = repository
        self._worker = worker
        self._interval_seconds = max(0.25, float(interval_seconds))
        self._artifact_retention_days = max(1, int(artifact_retention_days))
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._last_sweep = 0.0

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="monomer-dft-reconciler")

    def kick(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        task = self._task
        self._task = None
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=max(2.0, self._interval_seconds + 1.0))
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def reconcile_job(self, job: dict[str, Any]) -> dict[str, Any] | None:
        job_id = str(job.get("job_id") or "")
        attempt_token = str(job.get("_attempt_token") or "")
        status = str(job.get("status") or "")
        if not job_id or not attempt_token:
            return None
        try:
            if status == "pending":
                claimed = await asyncio.to_thread(
                    self._repository.claim_pending_dispatch,
                    job_id=job_id,
                    attempt_token=attempt_token,
                )
                if not claimed:
                    # A concurrent cancel/state transition won the row lock;
                    # never send the stale pending snapshot to the Worker.
                    return await asyncio.to_thread(self._repository.get_job, job_id)
                snapshot = await self._worker.submit_job(job)
            elif status in {"queued", "running"}:
                snapshot = await self._worker.get_job(job_id)
            elif status == "cancel_requested":
                snapshot = await self._worker.cancel_job(job)
            else:
                return job
            return await asyncio.to_thread(
                self._repository.apply_worker_snapshot,
                job_id=job_id,
                attempt_token=attempt_token,
                snapshot=snapshot,
            )
        except MonomerDftWorkerError as exc:
            if status == "cancel_requested" and exc.status_code == 404:
                # Unknown to this Worker is not proof that a dispatched
                # attempt stopped.  Keep the durable cancellation intent and
                # retry until a fenced Worker snapshot proves a terminal state.
                await asyncio.to_thread(
                    self._repository.record_dispatch_error,
                    job_id=job_id,
                    attempt_token=attempt_token,
                    code=exc.code,
                    message=str(exc),
                    retryable=True,
                    details=exc.details,
                )
                return None
            if not exc.retryable:
                stage = str(job.get("stage") or "")
                if stage not in WORKER_STAGES:
                    stage = "validating"
                snapshot = {
                    "schema_version": 2,
                    "job_id": job_id,
                    "attempt_token": attempt_token,
                    "request_sha256": job.get("request_sha256"),
                    "enqueue_sequence": job.get("_enqueue_sequence"),
                    "status": "failed",
                    "stage": stage,
                    "progress_percent": float(job.get("progress_percent") or 0.0),
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                        "retryable": False,
                        "details": exc.details,
                    },
                    "timings": {},
                    "artifacts": [],
                }
                return await asyncio.to_thread(
                    self._repository.apply_worker_snapshot,
                    job_id=job_id,
                    attempt_token=attempt_token,
                    snapshot=snapshot,
                )
            await asyncio.to_thread(
                self._repository.record_dispatch_error,
                job_id=job_id,
                attempt_token=attempt_token,
                code=exc.code,
                message=str(exc),
                retryable=True,
                details=exc.details,
            )
            return None
        except (MonomerDftJobNotFound, MonomerDftStaleAttempt):
            return None

    async def run_once(self) -> None:
        async with self._run_lock:
            checker = getattr(self._repository, "schema_ready", None)
            if checker is not None:
                try:
                    schema_ready = bool(await asyncio.to_thread(checker))
                except Exception:
                    schema_ready = False
                if not schema_ready:
                    # ``schema_ready`` only reads PostgreSQL catalogs and the
                    # migration ledger.  Do not touch a monomer_dft relation
                    # when 0013 is absent or has ceased to match the governed
                    # fingerprint, even if a readiness transition races this
                    # already-running reconciliation task.
                    return
            leader_guard = self._repository.reconciliation_leader()
            is_leader = await asyncio.to_thread(leader_guard.__enter__)
            try:
                if not is_leader:
                    return
                jobs = await asyncio.to_thread(self._repository.list_reconcilable_jobs, limit=100)
                for job in jobs:
                    try:
                        reconciled = await self.reconcile_job(job)
                        if reconciled is None:
                            # A retryable/unknown outcome must fence later submissions;
                            # otherwise a transient failure can reorder durable FIFO.
                            break
                    except Exception as exc:  # pragma: no cover - loop isolation
                        logger.warning(
                            "Monomer DFT reconciliation failed: %s",
                            sanitize_public_text(exc, fallback="reconciliation error", limit=240),
                        )
                        break
                now = time.monotonic()
                if now - self._last_sweep >= 60.0:
                    await self._sweep_expired_artifacts()
                    self._last_sweep = now
                await self._reconcile_artifact_deletions()
            finally:
                await asyncio.shield(
                    asyncio.to_thread(leader_guard.__exit__, None, None, None)
                )

    async def _sweep_expired_artifacts(self) -> None:
        jobs = await asyncio.to_thread(
            self._repository.list_expired_artifact_jobs,
            retention_days=self._artifact_retention_days,
            limit=100,
        )
        for job in jobs:
            job_id = str(job.get("job_id") or "")
            if not job_id:
                continue
            try:
                await asyncio.to_thread(self._repository.request_artifact_deletion, job_id)
            except MonomerDftJobNotFound:
                continue
            except Exception as exc:  # pragma: no cover - loop isolation
                logger.warning(
                    "Monomer DFT artifact expiry request failed: %s",
                    sanitize_public_text(exc, fallback="artifact expiry error", limit=240),
                )
                continue

    async def _reconcile_artifact_deletions(self) -> None:
        jobs = await asyncio.to_thread(
            self._repository.list_pending_artifact_deletions,
            limit=100,
        )
        for job in jobs:
            job_id = str(job.get("job_id") or "")
            if not job_id:
                continue
            try:
                await self._worker.delete_artifacts(job_id)
            except MonomerDftWorkerError as exc:
                # A Worker 404 is not proof that durable files are absent. Keep the
                # request pending so a restored/restarted Worker can retry it.
                logger.warning(
                    "Monomer DFT artifact deletion remains pending for job %s (%s)",
                    job_id,
                    exc.code,
                )
                continue
            try:
                await asyncio.to_thread(self._repository.mark_artifacts_deleted, job_id)
            except MonomerDftJobNotFound:
                continue

    async def _run(self) -> None:
        while not self._stop.is_set():
            # This cycle coalesces every kick observed before it starts.  A
            # kick that arrives during I/O must remain set so the next cycle
            # runs immediately rather than being lost to a post-I/O clear.
            self._wake.clear()
            try:
                await self.run_once()
            except Exception as exc:  # pragma: no cover - startup/database availability
                logger.warning(
                    "Monomer DFT reconciliation cycle failed: %s",
                    sanitize_public_text(exc, fallback="reconciliation error", limit=240),
                )
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval_seconds)
            except asyncio.TimeoutError:
                pass


class MonomerDftReadinessController:
    """Continuously bind reconciliation to the governed schema readiness.

    The application may start safely on the 0012 schema.  Applying 0013 while
    it remains online transitions the reconciler to running without requiring
    a process restart.  A later failed probe or readiness regression stops it,
    while ``MonomerDftReconciler.run_once`` supplies a second per-cycle gate.
    """

    def __init__(
        self,
        *,
        repository: MonomerDftRepository,
        reconciler: MonomerDftReconciler,
        interval_seconds: float,
    ) -> None:
        self._repository = repository
        self._reconciler = reconciler
        self._interval_seconds = max(0.25, float(interval_seconds))
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._transition_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._ready = False

    @property
    def schema_ready(self) -> bool:
        return self._ready

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(
                self._run(),
                name="monomer-dft-readiness-controller",
            )

    def kick(self) -> None:
        self._wake.set()

    async def refresh(self) -> bool:
        """Apply one idempotent readiness transition and return its state."""

        async with self._transition_lock:
            try:
                ready = bool(await asyncio.to_thread(self._repository.schema_ready))
            except Exception:
                ready = False
            if ready:
                # start() is idempotent and also repairs an unexpectedly
                # completed task while the schema remains ready.
                self._reconciler.start()
                self._reconciler.kick()
            else:
                await self._reconciler.stop()
            self._ready = ready
            return ready

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        task = self._task
        self._task = None
        if task is not None and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)
        self._ready = False
        await self._reconciler.stop()

    async def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                await self.refresh()
            except Exception as exc:  # pragma: no cover - defensive task isolation
                self._ready = False
                logger.warning(
                    "Monomer DFT readiness transition failed: %s",
                    sanitize_public_text(
                        exc,
                        fallback="readiness transition error",
                        limit=240,
                    ),
                )
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self._interval_seconds,
                )
            except asyncio.TimeoutError:
                pass
