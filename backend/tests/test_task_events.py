import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.auth.context import Identity, current_owner_id
from app.routers.task_events import task_events
from app.services.task_events import TaskEventHub, event_frames, read_snapshot

A = Identity("11111111-1111-4111-8111-111111111111")
B = Identity("22222222-2222-4222-8222-222222222222")


def test_hub_shares_owner_checks_suppresses_unchanged_and_releases_subscribers():
    async def run():
        loop = asyncio.get_running_loop()
        checked = asyncio.Queue()
        states = {A.user_id: "queued", B.user_id: "completed"}
        def reader(actor, settings, module):
            loop.call_soon_threadsafe(checked.put_nowait, actor.user_id)
            return [{"job_id": actor.user_id, "status": states[actor.user_id]}]
        hub = TaskEventHub(reader, interval=0.001)
        async with hub.subscribe(A, None, "md") as first:
            initial = await asyncio.wait_for(first.get(), 2)
            async with hub.subscribe(A, None, "md") as second, hub.subscribe(B, None, "md") as foreign:
                assert await second.get() == initial
                assert len(hub.watches) == 2
                assert (await foreign.get())["jobs"][0]["job_id"] == B.user_id
                for _ in range(8):
                    await asyncio.wait_for(checked.get(), 2)
                assert first.empty() and second.empty() and foreign.empty()
                states[A.user_id] = "running"
                assert (await asyncio.wait_for(first.get(), 2))["jobs"][0]["status"] == "running"
                assert (await asyncio.wait_for(second.get(), 2))["jobs"][0]["status"] == "running"
                assert foreign.empty()
            assert len(hub.watches) == 1
        assert hub.watches == {}
    asyncio.run(run())


def test_database_failure_is_redacted_and_recovery_emits_snapshot():
    async def run():
        failing = True
        def reader(*args):
            if failing:
                raise RuntimeError("password=private-do-not-send")
            return []
        hub = TaskEventHub(reader, interval=0.001)
        stream = event_frames(hub, A, None, "dft")
        first = await anext(stream)
        assert b"event: unavailable" in first and b"private-do-not-send" not in first
        failing = False
        assert b"event: snapshot" in await asyncio.wait_for(anext(stream), 2)
        await stream.aclose()
        assert not hub.watches
    asyncio.run(run())


@pytest.mark.parametrize("module,table", [("md", "md.monomer_md_jobs"), ("dft", "monomer_dft.jobs")])
def test_snapshot_uses_owner_context_and_only_read_queries(monkeypatch, module, table):
    queries = []
    @contextmanager
    def connection(dsn):
        assert current_owner_id() == A.user_id
        assert dsn == "test-api-connection"
        class Connection:
            def execute(self, query, params=()):
                queries.append((query, params))
                return SimpleNamespace(fetchall=lambda: [{"job_id": "owned", "status": "running"}])
        yield Connection()
    monkeypatch.setattr("app.services.task_events.postgres_connection", connection)
    assert read_snapshot("test-api-connection", A, None, module)[0]["job_id"] == "owned"
    query, params = queries[-1]
    assert table in query and "owner_user_id = %s::uuid" in query and params == (A.user_id,)
    assert "updated_at" not in query and "progress_percent" not in query and "attempt_token" not in query
    assert all(q.lstrip().startswith(("SELECT", "SET LOCAL")) for q, _ in queries)


def test_guest_cannot_create_a_subscription():
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    with pytest.raises(HTTPException) as failure:
        asyncio.run(task_events(request, "md"))
    assert failure.value.status_code == 401
    assert not hasattr(request.app.state, "task_event_hub")
