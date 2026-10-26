"""Durable FIFO dispatch and recovery; transport failures retain task quota."""
from __future__ import annotations

import asyncio
from contextvars import copy_context
import logging

from .polymer_md_repository import PolymerMdError, submit_request

logger = logging.getLogger(__name__)


class PolymerMdReconciler:
    def __init__(self, repository, worker, *, interval_seconds=3.0):
        self.repository, self.worker = repository, worker
        self.interval = interval_seconds
        self.task = None
        self.wake = asyncio.Event()
        self.stopping = False

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run(), name="polymer-md-reconciler")

    def kick(self):
        self.wake.set()

    async def stop(self):
        self.stopping = True
        self.wake.set()
        if self.task:
            await self.task
            self.task = None

    async def run_once(self):
        if not await asyncio.to_thread(self.repository.schema_ready):
            return
        guard = self.repository.reconciliation_leader()
        context = copy_context()
        # Keep the service ContextVar in the context in which the guard was
        # entered; do not reset it from another asyncio.to_thread Context.
        acquired = await asyncio.to_thread(context.run, guard.__enter__)
        try:
            if not acquired:
                return
            jobs = await asyncio.to_thread(self.repository.list_active)
            for job in jobs:
                try:
                    if job["status"] == "pending":
                        if not await asyncio.to_thread(self.repository.claim_dispatch, job["job_id"], job["attempt_token"]):
                            continue
                    snapshot = await self.worker.snapshot(submit_request(job), cancel=job["status"] == "cancel_requested")
                    await asyncio.to_thread(self.repository.apply_snapshot, snapshot)
                except PolymerMdError as exc:
                    await asyncio.to_thread(self.repository.record_error, job["job_id"], job["attempt_token"], exc.code, str(exc))
                    break
            await asyncio.to_thread(self.repository.mark_expired)
            for job in await asyncio.to_thread(self.repository.list_deletions):
                try:
                    await self.worker.delete(submit_request(job),entire_job=job["delete_job_requested"])
                    await asyncio.to_thread(self.repository.complete_deletion,job["job_id"],job["attempt_token"],delete_job=job["delete_job_requested"])
                except PolymerMdError:
                    # Keep the persisted request for a later exact-attempt retry.
                    continue
        finally:
            await asyncio.to_thread(context.run, guard.__exit__, None, None, None)

    async def run(self):
        while not self.stopping:
            try:
                await self.run_once()
            except Exception:
                logger.warning("polymer MD reconciliation failed; execution quota retained", exc_info=False)
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
