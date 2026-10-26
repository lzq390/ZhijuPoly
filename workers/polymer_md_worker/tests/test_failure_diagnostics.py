"""Failed-stage evidence survives scientific-directory deletion without real MD."""
import asyncio
import json
from pathlib import Path
import sys

import httpx
import pytest

from polymer_md.contracts import StageRun
from workers.polymer_md_worker.app.journal import sha256_file
from workers.polymer_md_worker.app.main import create_app
from test_manager import FakeScope, request, supervisor, until


@pytest.fixture
def anyio_backend():
    return "asyncio"


def failed_files_script(directory: Path, text: str):
    files = {"engine.stdout": text, "engine.log": "thermo " + text,
        "input.in": "units real\natom_style full\nread_data input.data\nrun 10\n",
        "input.data": "synthetic test-only input data\n", "last.data": "do not copy last state",
        "trajectory.xtc": "do not copy trajectory", "checkpoint.json": "do not copy checkpoint"}
    return "\n".join(["from pathlib import Path", f"directory=Path({str(directory)!r})",
        "directory.mkdir(parents=True,exist_ok=True)", f"files={files!r}",
        "for name,body in files.items(): (directory/name).write_text(body)"])


@pytest.mark.anyio
async def test_failure_evidence_is_durable_and_downloadable_before_retry_deletes_source(tmp_path):
    manager, calls = supervisor(tmp_path)
    original = manager.command_builder
    payload = request()
    observed = []

    def command(request_path, stage, work, result_path):
        base = original(request_path, stage, work, result_path)
        if stage != "eq1":
            return base
        source = work / stage
        if calls.count(stage) == 1:
            return [sys.executable, "-c", failed_files_script(source, "first failed engine output") + "\nraise SystemExit(3)"]
        # The original science runner clears this directory at technical retry.
        snapshot = manager.get(payload.job_id)
        diagnostic = next(a for a in snapshot["artifacts"] if a["stage_run_id"] == "eq1-1" and a["filename"] == "engine.stdout")
        path, _ = manager.artifact(payload.job_id, diagnostic["artifact_id"])
        assert path.read_text() == "first failed engine output"
        assert json.loads((work.parent / "journal.json").read_text())["snapshot"]["artifacts"]
        observed.append(diagnostic["artifact_id"])
        prefix = f"import shutil;shutil.rmtree({str(source)!r})\n"
        return [*base[:-1], prefix + base[-1]]

    manager.command_builder = command
    await manager.start()
    try:
        await manager.submit(payload)
        await until(lambda: manager.get(payload.job_id)["status"] == "completed")
        assert observed and calls.count("eq1") == 2
        snapshot = manager.get(payload.job_id)
        diagnostics = [a for a in snapshot["artifacts"] if a["stage_run_id"] == "eq1-1" and a["kind"].startswith("failure_")]
        assert {a["filename"] for a in diagnostics} == {"engine.stdout", "engine.log", "input.in", "input.data"}
        for descriptor in diagnostics:
            path, _ = manager.artifact(payload.job_id, descriptor["artifact_id"])
            assert "/stages/eq1-1/failure-diagnostics/" in str(path)
            assert descriptor["storage_ref"].startswith(f"{payload.job_id}/{payload.attempt_token}/stages/eq1-1/")
            assert sha256_file(path) == descriptor["sha256"]
        assert not (manager._directory(payload) / "science/eq1").exists()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(manager=manager)), base_url="http://worker") as client:
            response = await client.get(f"/jobs/{payload.job_id}/artifacts/{observed[0]}", params={"attempt_token": payload.attempt_token})
            assert response.status_code == 200 and response.text == "first failed engine output"
    finally:
        await manager.stop()


@pytest.mark.anyio
async def test_recovery_kills_interrupted_process_then_freezes_its_native_logs(tmp_path):
    scope = FakeScope()
    manager, calls = supervisor(tmp_path, scope=scope)
    original = manager.command_builder
    payload = request()
    source = manager._directory(payload) / "science/eq2"

    def command(request_path, stage, work, result_path):
        base = original(request_path, stage, work, result_path)
        if stage == "eq2":
            return [sys.executable, "-c", failed_files_script(source, "interrupted native output") + "\nimport time;time.sleep(60)"]
        return base

    manager.command_builder = command
    await manager.start()
    recovered = None
    try:
        await manager.submit(payload)
        await until(lambda: (source / "engine.stdout").exists())
        # Emulate supervisor death without its graceful stop path. The synthetic
        # child remains alive until the replacement manager's recovery kills it.
        manager.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await manager.task
        manager.root_lock.release()
        manager.analysis_executor.shutdown(wait=True)
        recovered, new_calls = supervisor(tmp_path, scope=scope)
        recovered.draining = True
        await recovered.start()
        snapshot = recovered.get(payload.job_id)
        old_run = next(run for run in snapshot["stage_runs"] if run["stage_run_id"] == "eq2-1")
        assert old_run["status"] == "failed" and old_run["results"]["failure_diagnostics"]["capture_complete"]
        assert not new_calls
        assert all(process.poll() is not None for process in scope.processes.values())
        descriptor = next(a for a in snapshot["artifacts"] if a["stage_run_id"] == "eq2-1" and a["filename"] == "engine.stdout")
        path, _ = recovered.artifact(payload.job_id, descriptor["artifact_id"])
        (source / "engine.stdout").write_text("new attempt would overwrite source")
        assert path.read_text() == "interrupted native output"
        await recovered.cancel(payload.job_id, payload)
    finally:
        for unit in scope.processes:
            scope.terminate(unit)
        if recovered:
            await recovered.stop()
        elif not manager.task.cancelled():
            await manager.stop()


def failed_record(tmp_path):
    manager, _ = supervisor(tmp_path)
    payload = request()
    record = manager._new_record(payload)
    run = StageRun(stage_run_id="eq1-1", stage="eq1", status="failed").model_dump(mode="json")
    record["snapshot"]["stage_runs"].append(run)
    manager.records[payload.job_id] = record
    manager._save(record)
    source = manager._directory(payload) / "science/eq1"
    source.mkdir(parents=True)
    return manager, payload, record, run, source


def test_nested_component_logs_and_referenced_inputs_have_distinct_frozen_storage(tmp_path):
    manager, payload, record, run, source = failed_record(tmp_path)
    try:
        for component in ("component_1", "component_2"):
            directory = source / component
            directory.mkdir()
            (directory / "engine.stdout").write_text(f"synthetic {component} convergence failure")
            (directory / "input.in").write_text("units real\nread_data input.data\nrun 10\n")
            (directory / "input.data").write_text(f"synthetic {component} topology")
            (directory / "unreferenced.data").write_text("exclude unrelated state")
        manager._capture_failed_diagnostics(record, run)
        artifacts = record["snapshot"]["artifacts"]
        assert len(artifacts) == 6
        assert len({item["storage_ref"] for item in artifacts}) == 6
        saved = [manager.artifact(payload.job_id, item["artifact_id"])[0].read_text() for item in artifacts]
        assert "synthetic component_1 convergence failure" in saved
        assert "synthetic component_2 convergence failure" in saved
        assert "synthetic component_1 topology" in saved and "synthetic component_2 topology" in saved
        assert "exclude unrelated state" not in saved
    finally:
        manager.analysis_executor.shutdown()


def test_diagnostic_file_stage_job_budgets_and_immutable_tails(tmp_path):
    manager, payload, record, run, source = failed_record(tmp_path)
    manager.FAILURE_LOG_BYTES = 16
    manager.FAILURE_STAGE_BYTES = manager.FAILURE_JOB_BYTES = 32
    content = "first-half-" + "last-sixteen-chr!"
    (source / "engine.stdout").write_text(content)
    (source / "engine.log").write_text("z" * 80)
    (source / "input.in").write_text("units real\nread_data input.data\nrun 10\n")
    (source / "input.data").write_text("complete data")
    (source / "trajectory.xtc").write_bytes(b"excluded trajectory")
    (source / "last.data").write_text("excluded last state")
    (source / "checkpoint.json").write_text("excluded checkpoint")
    manager._capture_failed_diagnostics(record, run)
    report = run["results"]["failure_diagnostics"]
    assert report["retained_bytes"] == 32
    assert len(record["snapshot"]["artifacts"]) == 2
    assert all(a["filename"].endswith(".tail.log") and a["size_bytes"] == 16 for a in record["snapshot"]["artifacts"])
    first = record["snapshot"]["artifacts"][0]
    frozen, _ = manager.artifact(payload.job_id, first["artifact_id"])
    assert frozen.read_text() == content[-16:]
    assert report["files"][0]["truncated"] is True
    before = len(record["snapshot"]["artifacts"])
    manager._capture_failed_diagnostics(record, run)
    assert len(record["snapshot"]["artifacts"]) == before
    other = StageRun(stage_run_id="eq1-2", stage="eq1", status="failed").model_dump(mode="json")
    record["snapshot"]["stage_runs"].append(other)
    manager._capture_failed_diagnostics(record, other)
    assert not other["results"]["failure_diagnostics"]["files"]
    assert len(record["snapshot"]["artifacts"]) == before


def test_diagnostics_refuse_running_scope_and_owner_escape(tmp_path):
    manager, payload, record, run, source = failed_record(tmp_path)
    (source / "engine.stdout").write_text("safe diagnostic")
    record["running_scope"] = {"unit": "still-active.scope"}
    with pytest.raises(RuntimeError, match="before execution cleanup"):
        manager._capture_failed_diagnostics(record, run)
    record["running_scope"] = None
    outside = tmp_path / "private-external.log"
    outside.write_text("must never be copied")
    (source / "unsafe.log").symlink_to(outside)
    manager._capture_failed_diagnostics(record, run)
    assert not record["snapshot"]["artifacts"]
    assert run["results"]["failure_diagnostics"]["skipped_count"] > 0
    assert outside.read_text() == "must never be copied"


@pytest.mark.anyio
async def test_failed_terminal_job_downloads_exact_variable_referenced_native_input(tmp_path):
    manager, payload, record, run, source = failed_record(tmp_path)
    (source / "custom.in").write_text("units real\nvariable dataf string native-cell.data\nread_data ${dataf}\nrun 10\n")
    (source / "native-cell.data").write_text("the complete effective input data\n")
    (source / "custom.data").write_text("unreferenced same-stem data must not be guessed")
    (source / "last.data").write_text("unreferenced checkpoint")
    manager._capture_failed_diagnostics(record, run)
    manager._finish(record, "failed")
    artifacts = manager.get(payload.job_id)["artifacts"]
    assert {a["filename"] for a in artifacts} == {"custom.in", "native-cell.data"}
    data = next(a for a in artifacts if a["filename"] == "native-cell.data")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(manager=manager)), base_url="http://worker") as client:
            response = await client.get(f"/jobs/{payload.job_id}/artifacts/{data['artifact_id']}", params={"attempt_token": payload.attempt_token})
            assert response.status_code == 200 and response.text == "the complete effective input data\n"
    finally:
        await manager.stop()
