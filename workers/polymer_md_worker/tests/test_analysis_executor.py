"""HTTP cancellation must not release a still-running native reader's lease."""
import asyncio
import threading

import httpx
import pytest

from workers.polymer_md_worker.app.main import create_app
from test_manager import request, supervisor, until


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_http_analysis_one_running_one_waiting_overflow_and_cancelled_client(tmp_path):
    manager, _ = supervisor(tmp_path)
    payload = request()
    started, release = threading.Event(), threading.Event()
    calls = []

    def reader(*args):
        calls.append(args)
        started.set()
        assert release.wait(10)
        return {"test_only": True}

    manager._analyze_one = reader
    await manager.start()
    await manager.cancel(payload.job_id, payload)
    app = create_app(manager=manager)
    path = f"/jobs/{payload.job_id}"
    analysis_path = path + f"/stages/eq3-1/distance?attempt_token={payload.attempt_token}"
    body = {"atom_id_1": 1, "atom_id_2": 2}
    tasks = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
            first = asyncio.create_task(client.post(analysis_path, json=body))
            tasks.append(first)
            await until(started.is_set)
            second = asyncio.create_task(client.post(analysis_path, json=body))
            tasks.append(second)
            await until(lambda: manager.analysis_inflight == 2)
            overflow = await client.post(analysis_path, json=body)
            assert overflow.status_code == 429 and overflow.json()["error"]["code"] == "analysis_busy"
            assert len(calls) == 1  # The second reader is queued, never concurrent.
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert manager.analysis_inflight == 2 and manager.readers[payload.job_id] == 2
            assert (await client.request("DELETE", path, json=payload.model_dump(mode="json"))).status_code == 409
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            await until(lambda: manager.analysis_inflight == 1)
            assert manager.readers[payload.job_id] == 1 and len(calls) == 1
            release.set()
            await until(lambda: manager.analysis_inflight == 0)
            assert not manager.readers
            assert (await client.request("DELETE", path, json=payload.model_dump(mode="json"))).status_code == 200
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.stop()


@pytest.mark.anyio
async def test_queued_analysis_runs_after_first_and_shutdown_waits_for_native_reader(tmp_path):
    manager, _ = supervisor(tmp_path)
    payload = request()
    started, release = threading.Event(), threading.Event()
    order = []

    def reader(_job, _stage, _operation, number):
        order.append(number)
        started.set()
        assert release.wait(10)
        return number

    manager._analyze_one = reader
    await manager.start()
    await manager.cancel(payload.job_id, payload)
    first = asyncio.create_task(manager.analyze(payload.job_id, "eq3-1", "trajectory", 1))
    await until(started.is_set)
    second = asyncio.create_task(manager.analyze(payload.job_id, "eq3-1", "trajectory", 2))
    await until(lambda: manager.analysis_inflight == 2)
    assert order == [1]
    release.set()
    assert await first == 1 and await second == 2
    await until(lambda: manager.analysis_inflight == 0)
    assert order == [1, 2] and not manager.readers
    # A disconnected running reader still prevents shutdown/root-lock release.
    release.clear()
    started.clear()
    third = asyncio.create_task(manager.analyze(payload.job_id, "eq3-1", "trajectory", 3))
    await until(started.is_set)
    third.cancel()
    with pytest.raises(asyncio.CancelledError):
        await third
    stopped = asyncio.create_task(manager.stop())
    await asyncio.sleep(.05)
    assert not stopped.done() and manager.root_lock.fd is not None
    release.set()
    await stopped
    assert not manager.readers and manager.root_lock.fd is None
