"""Terminal journals require only the worker HTTP dependency prefix."""
import asyncio
import builtins
import importlib
import json
import sys

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def http_only(monkeypatch):
    forbidden = {"numpy", "pandas", "scipy", "mdtraj"}
    attempts = []
    original = builtins.__import__
    original_module = importlib.import_module

    def guarded_import(name, *args, **kwargs):
        if name.partition(".")[0] in forbidden:
            attempts.append(name)
            raise ModuleNotFoundError("excluded from the HTTP worker prefix: " + name, name=name)
        return original(name, *args, **kwargs)

    def guarded_module(name, *args, **kwargs):
        if name.partition(".")[0] in forbidden:
            attempts.append(name)
            raise ModuleNotFoundError("excluded from the HTTP worker prefix: " + name, name=name)
        return original_module(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", guarded_module)
    # Other offline tests may have imported analysis during collection. Force
    # any terminal-path analysis import to run its imports under this guard.
    monkeypatch.delitem(sys.modules, "workers.polymer_md_worker.app.scientific_analysis", raising=False)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    for package in sorted(forbidden):
        with pytest.raises(ModuleNotFoundError):
            __import__(package)
    attempts.clear()
    return attempts


def native_payload():
    from polymer_md.contracts import NativeConfiguration, request_sha256
    from test_manager import request
    payload = request()
    scientific = payload.scientific_request.model_copy(update={
        "native_configuration": NativeConfiguration(properties=["qm_homo", "qm_lumo"])})
    return payload.model_copy(update={"scientific_request": scientific,
        "request_sha256": request_sha256(scientific)})


def assert_terminal(manager, payload, status, reason):
    from workers.polymer_md_worker.app.journal import read_json
    result = manager.get(payload.job_id)
    assert result["status"] == status and result["cleanup_confirmed"]
    assert result["finished_at"] is not None and manager.fatal is None
    records = {item["key"]: item for item in result["results"]["properties"]}
    assert set(records) == {"qm_homo", "qm_lumo"}
    assert all(item["value"] is None and item["quality_status"] == "failed"
               and item["reason"] == reason for item in records.values())
    from polymer_md.contracts import WorkerSnapshot
    persisted = read_json(manager._directory(payload) / "journal.json")["snapshot"]
    assert WorkerSnapshot.model_validate(persisted).model_dump(mode="json") == result
    json.dumps(result, allow_nan=False)
    assert "workers.polymer_md_worker.app.scientific_analysis" not in sys.modules


@pytest.mark.anyio
async def test_unknown_cancellation_is_durable_without_scientific_packages(tmp_path, http_only):
    from test_manager import supervisor
    manager, calls = supervisor(tmp_path)
    payload = native_payload()
    try:
        await manager.cancel(payload.job_id, payload)
        assert_terminal(manager, payload, "cancelled", "job_cancelled")
        replay, created = await manager.submit(payload)
        assert not created and replay["status"] == "cancelled"
        assert not calls and not http_only
    finally:
        manager.analysis_executor.shutdown()


@pytest.mark.anyio
async def test_supervisor_failure_is_terminal_without_scientific_packages(tmp_path, monkeypatch, http_only):
    from test_manager import supervisor
    manager, _ = supervisor(tmp_path)
    payload = native_payload()
    manager.records[payload.job_id] = manager._new_record(payload)
    manager.cancel_events[payload.job_id] = asyncio.Event()

    async def failed_execution(record):
        manager.stopping = True
        raise RuntimeError("synthetic supervisor failure before any engine")

    monkeypatch.setattr(manager, "_execute", failed_execution)
    try:
        await manager._run()
        assert_terminal(manager, payload, "failed", "supervisor_error")
        assert not http_only
    finally:
        manager.analysis_executor.shutdown()


@pytest.mark.anyio
async def test_normal_completion_is_terminal_without_scientific_packages(tmp_path, monkeypatch, http_only):
    from test_manager import supervisor
    manager, _ = supervisor(tmp_path)
    payload = native_payload()
    record = manager._new_record(payload)
    manager.records[payload.job_id] = record
    manager.cancel_events[payload.job_id] = asyncio.Event()
    stages = []

    async def completed_stage(record, stage):
        # Synthetic stage receipts exercise real supervisor completion without
        # executing a scientific command or claiming a measured property.
        stages.append(stage)
        record["completed_stages"][stage] = {"result": {"quality_status": "passed"}}
        return True

    monkeypatch.setattr(manager, "_stage", completed_stage)
    try:
        await manager._execute(record)
        assert stages == ["terminal_qm", "monomer_qm"]
        assert_terminal(manager, payload, "completed", "selected_property_not_produced")
        assert not http_only
    finally:
        manager.analysis_executor.shutdown()


def test_trajectory_analysis_requires_the_optional_scientific_runtime(tmp_path, monkeypatch, http_only):
    from test_manager import supervisor
    from workers.polymer_md_worker.app.manager import WorkerError
    manager, _ = supervisor(tmp_path)
    monkeypatch.setattr(manager, "stage_result", lambda *args: (tmp_path, {}))
    try:
        with pytest.raises(WorkerError) as failure:
            manager._analyze_one("synthetic-job", "synthetic-stage", "trajectory")
        assert failure.value.code == "trajectory_runtime_unavailable"
        assert failure.value.status_code == 503
        assert http_only == ["numpy"]
    finally:
        manager.analysis_executor.shutdown()


@pytest.mark.anyio
async def test_http_only_analysis_health_and_admission_fail_closed_before_queueing(tmp_path, monkeypatch, http_only):
    import httpx
    from test_manager import supervisor
    from workers.polymer_md_worker.app.main import create_app
    manager, _ = supervisor(tmp_path)
    # Use the production import probe while the sealed runner is synthetically
    # ready: it cannot establish analysis availability in the HTTP interpreter.
    manager.analysis_probe = manager._analysis_runtime_probe
    manager.recovering = False
    manager.scope_ready = True
    payload = native_payload()
    enqueued = []

    def must_not_enqueue(*args, **kwargs):
        enqueued.append(True)
        raise AssertionError("unavailable native analysis must not enter the executor")

    monkeypatch.setattr(manager.analysis_executor, "submit", must_not_enqueue)
    try:
        await manager.cancel(payload.job_id, payload)
        health = await manager.health()
        assert health["runtime_ready"] and health["accepting_jobs"]
        assert health["analysis_ready"] is False
        app = create_app(manager=manager)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
            response = await client.post(
                f"/jobs/{payload.job_id}/stages/eq3-1/distance?attempt_token={payload.attempt_token}",
                json={"atom_id_1": 1, "atom_id_2": 2})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "trajectory_runtime_unavailable"
        assert manager.analysis_inflight == 0 and not manager.readers and not enqueued
        assert http_only == ["numpy", "numpy"]
    finally:
        manager.analysis_executor.shutdown()


@pytest.mark.anyio
async def test_analysis_requires_ready_sealed_runtime_even_with_a_synthetic_reader(tmp_path, monkeypatch):
    from test_manager import supervisor
    from workers.polymer_md_worker.app.manager import WorkerError
    manager, _ = supervisor(tmp_path)
    manager.recovering = False
    manager.scope_ready = True
    manager.runtime_probe = lambda: {"ready": False}
    payload = native_payload()
    enqueued = []

    def must_not_enqueue(*args, **kwargs):
        enqueued.append(True)
        raise AssertionError("unready sealed runtime must not enter the executor")

    monkeypatch.setattr(manager.analysis_executor, "submit", must_not_enqueue)
    try:
        await manager.cancel(payload.job_id, payload)
        assert (await manager.health())["analysis_ready"] is False
        with pytest.raises(WorkerError) as failure:
            await manager.analyze(payload.job_id, "eq3-1", "trajectory", 0, 1)
        assert failure.value.code == "trajectory_runtime_unavailable"
        assert failure.value.status_code == 503
        assert manager.analysis_inflight == 0 and not manager.readers and not enqueued
    finally:
        manager.analysis_executor.shutdown()


@pytest.mark.anyio
async def test_health_rechecks_admission_state_after_dependency_probe(tmp_path):
    from test_manager import supervisor
    manager, _ = supervisor(tmp_path)
    manager.recovering = False
    manager.scope_ready = True

    def stopping_during_probe():
        manager.stopping = True
        return True

    manager.analysis_probe = stopping_during_probe
    try:
        health = await manager.health()
        assert health["analysis_ready"] is False
        assert health["ready"] is False and health["accepting_jobs"] is False
        assert health["admission_reason"] == "stopping"
    finally:
        manager.analysis_executor.shutdown()
