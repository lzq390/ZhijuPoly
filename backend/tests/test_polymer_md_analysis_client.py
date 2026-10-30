import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.routers import polymer_md as routes
from app.services.polymer_md_analysis_client import PolymerMdAnalysisClient
from app.services.polymer_md_repository import PolymerMdError


@pytest.fixture
def anyio_backend():
    return "asyncio"


def transport_client(handler):
    return httpx.AsyncClient(base_url="http://analysis", transport=httpx.MockTransport(handler))


@pytest.mark.anyio
@pytest.mark.parametrize("error,code,status", [
    (httpx.ReadTimeout("private socket path"), "analysis_timeout", 504),
    (httpx.ConnectError("private socket path"), "analysis_service_unavailable", 503),
])
async def test_transport_errors_are_local_analysis_errors(error, code, status):
    async def fail(_request):
        raise error
    client = transport_client(fail)
    service = PolymerMdAnalysisClient("/unused.sock", client=client, health_client=client)
    try:
        with pytest.raises(PolymerMdError) as result:
            await service.analyze("distance", {})
        assert (result.value.code, result.value.status) == (code, status)
        assert "private" not in str(result.value) and "对账" not in str(result.value)
    finally:
        await service.close()


@pytest.mark.anyio
@pytest.mark.parametrize("status,body,code", [
    (429, {"code": "analysis_busy"}, "analysis_busy"),
    (504, {"code": "analysis_timeout"}, "analysis_timeout"),
    (409, {"code": "artifact_integrity_mismatch"}, "artifact_integrity_mismatch"),
    (500, {"code": "private_error", "message": "/secret/token"}, "analysis_service_unavailable"),
])
async def test_service_error_codes_do_not_expose_private_messages(status, body, code):
    client = transport_client(lambda _request: httpx.Response(status, json=body))
    service = PolymerMdAnalysisClient("/unused.sock", client=client, health_client=client)
    try:
        with pytest.raises(PolymerMdError) as result:
            await service.analyze("distance", {})
        assert result.value.code == code and "/secret" not in str(result.value)
    finally:
        await service.close()


@pytest.mark.anyio
async def test_disconnect_closes_analysis_request_without_calling_simulation():
    started, released, disconnect = asyncio.Event(), asyncio.Event(), asyncio.Event()
    paths = []
    async def slow(request):
        paths.append(request.url.path)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()
    async def wait_disconnected():
        await disconnect.wait()
    client = transport_client(slow)
    service = PolymerMdAnalysisClient("/unused.sock", client=client, health_client=client)
    task = asyncio.create_task(service.analyze("distance", {}, wait_disconnected=wait_disconnected))
    try:
        await asyncio.wait_for(started.wait(), 1)
        disconnect.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert released.is_set() and paths == ["/distance"]
    finally:
        await service.close()


@pytest.mark.anyio
async def test_health_is_independent_of_occupied_calculation_connection():
    started, release = asyncio.Event(), asyncio.Event()
    async def slow(_request):
        started.set()
        await release.wait()
        return httpx.Response(200, json={})
    health = {"protocol_version": 1, "service": "polymer-md-analysis", "ready": True,
              "active": 1, "queued": 0, "request_timeout_seconds": 90}
    service = PolymerMdAnalysisClient("/unused.sock", client=transport_client(slow),
        health_client=transport_client(lambda _request: httpx.Response(200, json=health)))
    task = asyncio.create_task(service.analyze("distance", {}))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert await asyncio.wait_for(service.health(), .5) == health
    finally:
        release.set()
        await task
        await service.close()


def sample_job():
    job_id, token = str(uuid4()), "a" * 64
    return {"job_id": job_id, "attempt_token": token, "artifact_state": "available",
        "stage_runs": [{"stage_run_id": "eq1-1", "stage": "eq1", "status": "completed"}],
        "artifacts": [{"artifact_id": "eq1-1-0", "stage_run_id": "eq1-1", "filename": "trajectory.xtc",
            "kind": "trajectory", "media_type": "application/octet-stream", "size_bytes": 12,
            "sha256": "b" * 64, "storage_ref": f"{job_id}/{token}/science/eq1/trajectory.xtc"}]}


@pytest.mark.anyio
@pytest.mark.parametrize("changed", ["attempt", "deleted", "manifest", "owner"])
async def test_result_rechecks_access_and_artifact_fence(monkeypatch, changed):
    job = sample_job()
    calls = []
    class Service:
        async def analyze(self, operation, payload, **kwargs):
            calls.append(payload)
            return {"job_id": job["job_id"], "stage_run_id": "eq1-1", "frame_index": 0}
    async def stage_job(*_args):
        if changed in {"deleted", "owner"}:
            raise PolymerMdError("stage_not_found", "not found", 404)
        current = {**job, "artifacts": [dict(item) for item in job["artifacts"]]}
        if changed == "attempt":
            current["attempt_token"] = "c" * 64
        else:
            current["artifacts"][0]["sha256"] = "c" * 64
        return current
    monkeypatch.setattr(routes, "_stage_job", stage_job)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(polymer_md_analysis_client=Service())), is_disconnected=lambda: None)
    with pytest.raises(PolymerMdError):
        await routes._analyze_stage(request, job, "eq1-1", "trajectory", frame_index=0, limit=2000)
    assert len(calls) == 1 and calls[0]["artifacts"][0]["storage_ref"] == job["artifacts"][0]["storage_ref"]


@pytest.mark.anyio
async def test_unavailable_analysis_never_falls_back_to_worker():
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(polymer_md_analysis_client=None)))
    with pytest.raises(PolymerMdError) as result:
        await routes._analyze_stage(request, sample_job(), "eq1-1", "distance", distance={})
    assert result.value.code == "analysis_service_unavailable"
