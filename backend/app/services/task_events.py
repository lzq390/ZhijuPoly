"""Read-only, owner-scoped task notifications; never acquire compute permits."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
from typing import Callable

from starlette.concurrency import run_in_threadpool

from app.auth.context import Identity, user_context
from app.postgres_database import postgres_connection


def read_snapshot(dsn: str, identity: Identity, auth_settings, module: str) -> list[dict]:
    # The captured user context applies RLS even in a background thread. Explicit
    # owner predicates are retained as an independent authorization boundary.
    with user_context(identity, auth_settings), postgres_connection(dsn) as connection:
        connection.execute("SET LOCAL statement_timeout = '3000ms'")
        if module == "md":
            rows = connection.execute("""
                SELECT job_id, status, artifact_deleted_at IS NOT NULL AS artifacts_deleted,
                       CASE WHEN queue_sequence IS NULL THEN NULL ELSE
                         (SELECT count(*) + 1 FROM md.monomer_md_jobs q
                          WHERE q.owner_user_id = j.owner_user_id AND q.run_mode = 'formal'
                            AND q.status IN ('pending','submitted','running','cancel_requested')
                            AND q.queue_sequence < j.queue_sequence) END AS queue_position
                FROM md.monomer_md_jobs j WHERE owner_user_id = %s::uuid
                ORDER BY job_id LIMIT 10001
            """, (identity.user_id,)).fetchall()
        elif module == "dft":
            rows = connection.execute("""
                SELECT job_id, status, queue_position, current_attempt AS attempt,
                       CASE WHEN artifacts_deleted_at IS NOT NULL THEN 'deleted'
                            WHEN artifacts_delete_requested_at IS NOT NULL THEN 'delete_requested'
                            WHEN EXISTS (SELECT 1 FROM monomer_dft.artifacts a
                                         WHERE a.job_id = j.job_id AND a.available) THEN 'available'
                            ELSE 'none' END AS artifacts_state
                FROM monomer_dft.jobs j WHERE owner_user_id = %s::uuid
                ORDER BY job_id LIMIT 10001
            """, (identity.user_id,)).fetchall()
        else:
            raise ValueError("Unknown task module")
    if len(rows) > 10000:
        raise ValueError("Task notification snapshot exceeds its bounded capacity")
    return [{**dict(row), "job_id": str(row["job_id"])} for row in rows]


@dataclass
class Watch:
    subscribers: set = field(default_factory=set)
    latest: dict | None = None
    task: asyncio.Task | None = None


class TaskEventHub:
    def __init__(self, reader: Callable, interval: float = 3.0):
        self.reader = reader
        self.interval = interval
        self.watches: dict[tuple[str, str], Watch] = {}

    @asynccontextmanager
    async def subscribe(self, identity: Identity, settings, module: str):
        key = identity.user_id, module
        watch = self.watches.setdefault(key, Watch())
        queue = asyncio.Queue(maxsize=1)
        watch.subscribers.add(queue)
        if watch.latest is not None:
            queue.put_nowait(watch.latest)
        if watch.task is None:
            watch.task = asyncio.create_task(self._watch(watch, identity, settings, module))
        try:
            yield queue
        finally:
            watch.subscribers.discard(queue)
            if not watch.subscribers:
                self.watches.pop(key, None)
                watch.task.cancel()
                try:
                    await watch.task
                except asyncio.CancelledError:
                    pass

    async def _watch(self, watch: Watch, identity: Identity, settings, module: str):
        previous = None
        while True:
            try:
                jobs = await run_in_threadpool(self.reader, identity, settings, module)
                message = {"module": module, "jobs": jobs}
            except Exception:
                # Do not expose SQL, connection strings, or private error text.
                message = {"module": module, "unavailable": True}
            signature = json.dumps(message, sort_keys=True, separators=(",", ":"))
            if signature != previous:
                previous = signature
                watch.latest = message
                for queue in tuple(watch.subscribers):
                    if queue.full():
                        queue.get_nowait()
                    queue.put_nowait(message)
            await asyncio.sleep(self.interval)


async def event_frames(hub, identity, settings, module):
    async with hub.subscribe(identity, settings, module) as queue:
        while True:
            try:
                value = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield b": keepalive\n\n"
                continue
            event = "unavailable" if value.get("unavailable") else "snapshot"
            yield f"event: {event}\ndata: {json.dumps(value, separators=(',', ':'))}\n\n".encode()
