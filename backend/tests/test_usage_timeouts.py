"""Synthetic real-ASGI timeout accounting; no GPU, child process, DB or API calls."""
from __future__ import annotations
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.auth.context import Identity, current_owner_id, user_context
from app.routers import query, monomer_retrosynthesis
from app.services import structure_3d
from app.services.gpu_runtime_registry import GpuQueueTimeoutError, GpuQueueStoppedError, GpuQueueFullError
from app.usage_context import (UsageCollector, UsageContext, UsageSpan, usage_scope, usage_span,
                               mark_current_usage_outcome)
from app.usage_middleware import UsageMeteringMiddleware
from app.utils.exceptions import InvalidSmilesError, ModelArtifactError

OWNER = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"

class Repository:
    def __init__(self): self.events = []
    def record_event(self, **event):
        assert current_owner_id() == OWNER == event["owner_user_id"]
        self.events.append(event)
        return {"inserted":True}

class IdentityMiddleware:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        with user_context(Identity(OWNER)):
            await self.app(scope,receive,send)


def application(*, enabled=True):
    repo = Repository()
    collector = UsageCollector(enabled=enabled,repository=repo)
    app = FastAPI()
    app.state.settings = SimpleNamespace(structure_3d_timeout_seconds=1,ocsr_enabled=True,
        ocsr_max_image_bytes=1024,retro_model_enabled=True)
    app.include_router(query.router)
    app.include_router(monomer_retrosynthesis.router)
    app.add_middleware(UsageMeteringMiddleware,collector=collector)
    app.add_middleware(IdentityMiddleware)
    return app,repo


def failing(error):
    def execute(*args,**kwargs):raise error
    return execute


@pytest.mark.parametrize("enabled",[True,False])
def test_real_3d_deadline_keeps_422_but_records_timeout_after_process_join(monkeypatch,enabled):
    calls = []
    class Process:
        def start(self):calls.append("start")
        def terminate(self):calls.append("terminate")
        def join(self):calls.append("join")
    context = SimpleNamespace(Queue=lambda:object(),Process=lambda **kwargs:Process())
    monkeypatch.setattr(structure_3d,"multiprocessing",SimpleNamespace(get_context=lambda kind:context))
    ticks = iter([10.0,12.0])
    monkeypatch.setattr(structure_3d,"time",SimpleNamespace(monotonic=lambda:next(ticks)))
    app,repo=application(enabled=enabled)
    with TestClient(app) as client:
        response=client.post("/api/v1/structure/3d",json={"smiles":"CC"})
    assert response.status_code == 422
    assert response.json() == {"detail":"3D generation timed out"}
    assert calls == ["start","terminate","join"]
    assert ([event["status"] for event in repo.events] if enabled else repo.events) == (
        ["running","timed_out"] if enabled else [])


def test_ordinary_invalid_smiles_with_timeout_words_remains_rejected(monkeypatch):
    monkeypatch.setattr(query,"generate_3d_molblock",failing(InvalidSmilesError("3D generation timed out")))
    app,repo=application()
    with TestClient(app) as client:
        response=client.post("/api/v1/structure/3d",json={"smiles":"CC"})
    assert response.status_code == 422
    assert repo.events[-1]["status"] == "rejected"


@pytest.mark.parametrize("module",["ocsr","retrosynthesis"])
@pytest.mark.parametrize("error_type,http_status,usage_status",[
    (GpuQueueTimeoutError,503,"timed_out"),(GpuQueueStoppedError,503,"failed"),
    (GpuQueueFullError,429,"rejected"),(RuntimeError,503,"failed"),(ModelArtifactError,503,"failed")])
def test_only_typed_gpu_queue_timeout_changes_usage_not_http(monkeypatch,module,error_type,http_status,usage_status):
    error = (error_type("synthetic timeout words",model_name=module,retry_after_seconds=3)
             if issubclass(error_type,(GpuQueueTimeoutError,GpuQueueStoppedError,GpuQueueFullError))
             else error_type("synthetic timeout words"))
    target = query if module == "ocsr" else monomer_retrosynthesis
    attr = "_run_ocsr_inference" if module == "ocsr" else "_run_retrosynthesis_inference"
    monkeypatch.setattr(target,attr,failing(error))
    app,repo=application()
    with TestClient(app) as client:
        if module == "ocsr":
            response=client.post("/api/v1/structure/recognize-image",files={"image":("fixture.png",b"synthetic","image/png")})
        else:
            response=client.post("/api/v1/monomer-retrosynthesis",json={"smiles":"CC","num_beams":2,"num_return_sequences":1})
    assert response.status_code == http_status
    if error_type in {GpuQueueTimeoutError,GpuQueueStoppedError,GpuQueueFullError}:
        assert response.json() == {"detail":str(error)}
        assert response.headers["retry-after"] == "3"
    assert [event["status"] for event in repo.events] == ["running",usage_status]
    assert repo.events[-1]["module"] == module
    assert "synthetic timeout words" not in str(repo.events)


@pytest.mark.parametrize("transport_status,expected",[
    ("failed","timed_out"),("rejected","timed_out"),("completed","timed_out"),
    ("interrupted","interrupted"),("cancelled","cancelled")])
def test_explicit_timeout_refines_transport_failure_but_preserves_cancellation(transport_status,expected):
    repo=Repository();collector=UsageCollector(enabled=True,repository=repo)
    context=UsageContext(OWNER,"structure","request:timeout-state")
    span=UsageSpan(context,collector)
    with usage_scope(context,collector=collector):
        span.start()
        mark_current_usage_outcome("timed_out")
        span.finish(transport_status)
    assert repo.events[-1]["status"] == expected


def test_handled_child_timeout_followed_by_success_is_not_promoted_to_root():
    repo=Repository();collector=UsageCollector(enabled=True,repository=repo)
    span=UsageSpan(UsageContext(OWNER,"predict","request:recovered"),collector)
    with span:
        try:
            with usage_span("predict"):
                raise TimeoutError("synthetic child timeout")
        except TimeoutError:
            with usage_span("predict"):
                pass
    assert [event["status"] for event in repo.events] == [
        "running","running","timed_out","running","completed","completed"]
