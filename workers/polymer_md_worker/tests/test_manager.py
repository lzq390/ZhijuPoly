"""Supervisor tests use deliberately synthetic subprocesses, never scientific engines."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from polymer_md.contracts import ScientificRequest, StageRun, SubmitRequest, request_sha256
from workers.polymer_md_worker.app.config import WorkerSettings
from workers.polymer_md_worker.app.journal import read_json
from workers.polymer_md_worker.app.manager import JobManager, WorkerError


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeScope:
    def __init__(self):
        self.processes = {}
        self.confirm = True
        self.launches = []

    def probe(self):
        return {"ready": True}

    def launch(self, *, unit, command, cwd, log, **kwargs):
        journal = next(cwd.parent.glob("journal.json"))
        state = read_json(journal)
        assert state["authorization"] is not None
        assert state["running_scope"]["unit"] == unit
        process = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
        self.processes[unit] = process
        self.launches.append(unit)
        return process

    def inspect(self, unit):
        return {"LoadState": "loaded", "ControlGroup": ""}

    def terminate(self, unit, cgroup=None):
        if not self.confirm:
            return False
        process = self.processes.get(unit)
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        return True


class FakeAuthorization:
    configured = True

    def __init__(self):
        self.calls = 0

    async def authorize(self, request, instance, **kwargs):
        self.calls += 1
        return {"authorized": True, "attempt_token": request.attempt_token}


def request(calculation="basic"):
    scientific = ScientificRequest(smiles="*CC*", n_atom=100, n_chain=1, seed=12, calculation=calculation)
    return SubmitRequest(job_id=str(uuid4()), owner_user_id=str(uuid4()), attempt_token="a" * 64,
                         request_sha256=request_sha256(scientific), enqueue_sequence=1, scientific_request=scientific)


def supervisor(tmp_path, *, scope=None, authorizer=None, fail=None, delay=None):
    calls = []

    def command(request_path, stage, work_dir, result_path):
        calls.append(stage)
        incoming = read_json(work_dir / ".input-checkpoint.json")
        assert incoming["stage"] == stage
        if stage != "terminal_qm":
            assert Path(incoming["checkpoint"]).is_file()
            assert "/checkpoints/" in incoming["checkpoint"]
        if fail == stage and calls.count(stage) == 1:
            return [sys.executable, "-c", "raise SystemExit(3)"]
        is_analysis = stage.startswith("analyze")
        equilibrated = stage != "analyze_0" if is_analysis else True
        result = {"stage": stage, "quality_status": "passed" if equilibrated else "not_converged",
                  "equilibrated": equilibrated, "tc_eligible": equilibrated,
                  "results": {"properties": [{"key": "density" if is_analysis else "diagnostic", "value": 1,
                      "unit": "test-only", "quality_status": "passed"}],
                      "series": [{"key": "density", "unit": "test-only", "points": [{"x": 1, "y": 1}]}] if is_analysis else [],
                      "trajectory": {"path": "private.xtc"}}, "artifacts": []}
        script = "\n".join([
            "import hashlib,json,time", "from pathlib import Path",
            f"time.sleep({0.5 if delay == stage else 0})",
            f"p=Path({str(work_dir / (stage + '.checkpoint'))!r});p.write_text('test checkpoint')",
            f"result={result!r}",
            "result['checkpoint']={'path':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}",
            f"Path({str(result_path)!r}).write_text(json.dumps(result))",
        ])
        return [sys.executable, "-c", script]

    manager = JobManager(WorkerSettings(job_root=tmp_path / "jobs", runner_python=sys.executable,
        authorization_url="unused", authorization_token="unused", poll_seconds=0.01),
        scope=scope or FakeScope(), authorizer=authorizer or FakeAuthorization(),
        runtime_probe=lambda: {"ready": True}, command_builder=command,
        analysis_probe=lambda: True)  # Synthetic reader tests do not execute scientific analysis.
    return manager, calls


async def until(condition):
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("supervisor did not reach expected state")


@pytest.mark.anyio
async def test_pipeline_scientific_iterations_and_technical_retry(tmp_path):
    manager, calls = supervisor(tmp_path, fail="monomer_qm")
    await manager.start()
    payload = request("basic_tc")
    try:
        await manager.submit(payload)
        await until(lambda: manager.get(payload.job_id)["status"] == "completed")
        result = manager.get(payload.job_id)
        assert calls.count("terminal_qm") == 1
        assert calls.count("monomer_qm") == 2
        assert calls.count("additional_1") == 1
        assert "additional_2" not in calls
        assert calls[-2:] == ["tc", "tc_analyze"]
        assert result["cleanup_confirmed"] is True
        assert result["quality_status"] == "passed"
        assert result["results"]["resource_limits"] == {
            "cpu_count": 16, "memory_bytes": 32 * 1024**3, "gpu_count": 0,
            "stage_timeout_seconds": 24 * 3600, "job_timeout_seconds": 96 * 3600,
            "max_job_bytes": 20 * 1024**3, "max_total_atoms": 20000, "max_replicated_atoms": 60000}
        assert result["results"]["retention_policy"] == {"artifact_retention_days": 7, "metadata_retention_days": 30}
        assert manager.authorizer.calls == 1
        assert "private.xtc" not in json.dumps(result)
        before = result["revision"]
        replay, created = await manager.submit(payload)
        assert not created and replay["revision"] == before
    finally:
        await manager.stop()


@pytest.mark.anyio
async def test_unknown_cancel_tombstone_and_purge_replay(tmp_path):
    manager, calls = supervisor(tmp_path)
    await manager.start()
    payload = request()
    try:
        result = await manager.cancel(payload.job_id, payload)
        assert result["status"] == "cancelled" and result["cleanup_confirmed"]
        replay, created = await manager.submit(payload)
        assert replay["status"] == "cancelled" and not created
        assert not calls
        await manager.delete(payload.job_id, payload, purge=True)
        assert (await manager.delete(payload.job_id, payload, purge=True))["deleted"] is True
        with pytest.raises(WorkerError, match="permanently deleted"):
            await manager.submit(payload)
    finally:
        await manager.stop()


@pytest.mark.anyio
async def test_restart_reuses_completed_stages_and_retries_interrupted_stage(tmp_path):
    manager, calls = supervisor(tmp_path, delay="eq2")
    await manager.start()
    payload = request()
    await manager.submit(payload)
    await until(lambda: "eq2" in calls)
    await manager.stop()
    assert manager.get(payload.job_id)["status"] == "running"
    directory = manager._directory(payload)
    (directory / "science/terminal_qm.checkpoint").write_text("damaged original")
    (directory / "science/eq1.checkpoint").unlink()
    recovered, new_calls = supervisor(tmp_path)
    await recovered.start()
    try:
        await until(lambda: recovered.get(payload.job_id)["status"] == "completed")
        assert "terminal_qm" not in new_calls and "eq1" not in new_calls
        assert new_calls[0] == "eq2"
        stages = recovered.get(payload.job_id)["stage_runs"]
        assert [s["attempt"] for s in stages if s["stage"] == "eq2"] == [1, 2]
        assert recovered.authorizer.calls == 1
        assert (directory / "science/terminal_qm.checkpoint").read_text() == "test checkpoint"
        assert (directory / "science/eq1.checkpoint").read_text() == "test checkpoint"
    finally:
        await recovered.stop()


@pytest.mark.anyio
async def test_unconfirmed_scope_never_publishes_terminal_or_frees_worker(tmp_path):
    scope = FakeScope()
    manager, calls = supervisor(tmp_path, scope=scope, delay="terminal_qm")
    await manager.start()
    payload = request()
    try:
        await manager.submit(payload)
        await until(lambda: bool(calls))
        scope.confirm = False
        await manager.cancel(payload.job_id, payload)
        await until(lambda: manager.fatal is not None)
        result = manager.get(payload.job_id)
        assert result["status"] == "cancel_requested"
        assert result["cleanup_confirmed"] is False
        assert result["finished_at"] is None
        assert (await manager.health())["accepting_jobs"] is False
    finally:
        scope.confirm = True
        for unit in scope.processes:
            scope.terminate(unit)
        await manager.stop()


@pytest.mark.anyio
async def test_artifact_read_lease_blocks_delete_and_checkpoint_tamper_fails(tmp_path):
    manager, _ = supervisor(tmp_path)
    await manager.start()
    payload = request()
    try:
        await manager.submit(payload)
        await until(lambda: manager.get(payload.job_id)["status"] == "completed")
        await manager.acquire_read(payload.job_id)
        with pytest.raises(WorkerError, match="in progress"):
            await manager.delete(payload.job_id, payload)
        await manager.release_read(payload.job_id)
        manifest = manager.get(payload.job_id)["artifacts"]
        assert manifest[0]["storage_ref"].startswith(f"{payload.job_id}/{payload.attempt_token}/stages/")
        file, _ = manager.artifact(payload.job_id, manifest[0]["artifact_id"])
        file.write_text("changed")
        with pytest.raises(WorkerError, match="content changed"):
            manager.artifact(payload.job_id, manifest[0]["artifact_id"])
        await manager.delete(payload.job_id, payload)
        assert manager.get(payload.job_id)["artifacts"] == []
    finally:
        await manager.stop()


@pytest.mark.anyio
async def test_frozen_checkpoint_corruption_fails_without_reexecution(tmp_path):
    manager, calls = supervisor(tmp_path, delay="eq2")
    payload = request()
    await manager.start()
    await manager.submit(payload)
    await until(lambda: "eq2" in calls)
    await manager.stop()
    directory = manager._directory(payload)
    checkpoint = manager.records[payload.job_id]["completed_stages"]["eq1"]["checkpoint"]
    (directory / checkpoint["path"]).write_text("damaged frozen state")
    recovered, new_calls = supervisor(tmp_path)
    await recovered.start()
    try:
        await until(lambda: recovered.get(payload.job_id)["status"] == "failed")
        assert not new_calls
        assert recovered.get(payload.job_id)["quality_status"] == "failed"
        assert recovered.get(payload.job_id)["errors"][-1]["code"] == "checkpoint_integrity_mismatch"
    finally:
        await recovered.stop()


@pytest.mark.anyio
async def test_tc_execution_failure_preserves_immediately_published_basic_results(tmp_path):
    manager, calls = supervisor(tmp_path)
    command = manager.command_builder

    def fail_tc(request_path, stage, work_dir, result_path):
        if stage == "tc":
            calls.append(stage)
            # These observations must already be visible when RNEMD begins.
            snapshot = manager.get(payload.job_id)
            assert snapshot["results"]["quality_summary"]["basic"]["quality_status"] == "passed"
            assert any(prop["key"] == "density" for prop in snapshot["results"]["properties"])
            return [sys.executable, "-c", "raise SystemExit(3)"]
        return command(request_path, stage, work_dir, result_path)

    manager.command_builder = fail_tc
    payload = request("basic_tc")
    await manager.start()
    try:
        await manager.submit(payload)
        await until(lambda: manager.get(payload.job_id)["status"] == "failed")
        result = manager.get(payload.job_id)
        assert result["quality_status"] == "failed"
        assert result["results"]["quality_summary"]["basic"]["quality_status"] == "passed"
        assert [item["key"] for item in result["results"]["series"]] == ["density"]
        assert calls.count("tc") == 2
    finally:
        await manager.stop()


def test_trajectory_requires_unchanged_manifested_native_files(tmp_path):
    from workers.polymer_md_worker.app.journal import sha256_file
    manager, _ = supervisor(tmp_path)
    payload = request()
    record = manager._new_record(payload)
    manager.records[payload.job_id] = record
    directory = manager._directory(payload)
    work_dir = directory / "science"
    stage_dir = work_dir / "eq3"
    stage_dir.mkdir(parents=True)
    for filename in ("checkpoint.json", "native.xtc", "topology.pdb"):
        (stage_dir / filename).write_text("test-only bytes")
    run = StageRun(stage_run_id="eq3-1", stage="eq3", status="running").model_dump(mode="json")
    record["snapshot"]["stage_runs"].append(run)
    result = {"stage": "eq3", "quality_status": "passed", "checkpoint": {
        "path": "eq3/checkpoint.json", "sha256": sha256_file(stage_dir / "checkpoint.json")},
        "results": {"trajectory": {"xtc_file": "eq3/native.xtc", "pdb_file": "eq3/topology.pdb"}},
        "artifacts": [{"path": "eq3/" + filename} for filename in ("native.xtc", "topology.pdb")]}
    manager._accept_stage(record, run, result, work_dir, directory)
    assert manager.stage_result(payload.job_id, "eq3-1")[0] == work_dir
    (stage_dir / "topology.pdb").write_text("damaged topology")
    with pytest.raises(WorkerError, match="content changed"):
        manager.stage_result(payload.job_id, "eq3-1")


@pytest.mark.anyio
async def test_real_scope_execution_gate_requires_durable_scope_membership(tmp_path):
    from workers.polymer_md_worker.app.process_control import ScopeControl
    scope = ScopeControl()
    if not scope.probe()["ready"]:
        pytest.skip("systemd user scopes unavailable in this test host")
    manager, calls = supervisor(tmp_path, scope=scope)
    # Exercise the production gate with synthetic subprocess observations.
    manager.execution_gate = True
    payload = request()
    await manager.start()
    try:
        await manager.submit(payload)
        await until(lambda: manager.get(payload.job_id)["status"] in {"completed", "failed"} or manager.fatal is not None)
        assert manager.get(payload.job_id)["status"] == "completed"
        assert calls.count("terminal_qm") == 1
        entry = read_json(manager._directory(payload) / "stages/eq3-1/execution-entry.json")
        assert entry["unit"].endswith("-eq3-1.scope")
    finally:
        await manager.stop()


@pytest.mark.anyio
async def test_drain_finishes_active_job_and_holds_queued_jobs(tmp_path):
    manager, calls = supervisor(tmp_path, delay="terminal_qm")
    first = request()
    second = request().model_copy(update={"enqueue_sequence": 2})
    await manager.start()
    try:
        await manager.submit(first)
        await until(lambda: "terminal_qm" in calls)
        await manager.submit(second)
        manager.draining = True
        await until(lambda: manager.get(first.job_id)["status"] == "completed")
        await asyncio.sleep(.05)
        assert manager.get(second.job_id)["status"] == "queued"
        assert calls.count("terminal_qm") == 1
        manager.draining = False
        manager.wake.set()
        await until(lambda: manager.get(second.job_id)["status"] == "completed")
        assert calls.count("terminal_qm") == 2
    finally:
        await manager.stop()


@pytest.mark.anyio
async def test_crash_before_launcher_identity_is_persisted_cannot_release_resources(tmp_path):
    manager, _ = supervisor(tmp_path)
    identity = {"unit": "nexpoly-polymer-md-unknown.scope", "launch_protocol": "gated-v1",
                "gate_path": str(tmp_path / "gate.json"), "entry_path": str(tmp_path / "not-created.json")}
    assert not await manager._terminate(identity)
    assert read_json(tmp_path / "gate.json") == {"allowed": False}
