"""Synthetic scope/DBus failures; no scientific engine or real systemd operation."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pytest

from workers.polymer_md_worker.app import process_control
from workers.polymer_md_worker.app.journal import read_json
from workers.polymer_md_worker.app.manager import WorkerError, utcnow
from workers.polymer_md_worker.app.process_control import ScopeControl, ScopeInspectionError
from test_manager import FakeScope, request, supervisor, until


@pytest.fixture
def anyio_backend():
    return "asyncio"


class MonitoredScope(FakeScope):
    """Declare a synthetic granted identity, then exercise supervisor decisions."""

    def __init__(self, *, failures=0, bounded=True, gated=True, slow=False, missing_view=False):
        super().__init__()
        self.failures, self.bounded, self.gated, self.slow = failures, bounded, gated, slow
        self.missing_view = missing_view
        self.inspections = 0
        self.terminations = 0
        self.manager = None
        self.record = None
        self.release_inspection = threading.Event()

    def launch(self, **kwargs):
        # Strip the production gate for this deliberately synthetic subprocess.
        command = kwargs["command"]
        if "--unit" in command:
            kwargs["command"] = command[command.index("--") + 1:]
        process = super().launch(**kwargs)
        identity = self.record["running_scope"]
        identity.update(launch_protocol="synthetic-test", gate_granted=self.gated,
                        cgroup=f"/synthetic/{kwargs['unit']}", cgroup_identity={"device": 1, "inode": 2},
                        entry_pid=process.pid, entry_start_ticks=1)
        self.manager.execution_gate = True
        return process

    def inspect(self, unit):
        self.inspections += 1
        if self.slow:
            self.release_inspection.wait(timeout=2)
        if self.inspections <= self.failures:
            if self.missing_view:
                return {"LoadState": "not-found", "ControlGroup": ""}
            raise ScopeInspectionError(exception_type="ConnectionError", returncode=1,
                stderr="Failed to connect to bus: Connection reset by peer; Bearer PRIVATE-CREDENTIAL")
        return {"LoadState": "loaded", "ActiveState": "active", "SubState": "running",
                "ControlGroup": self.record["running_scope"]["cgroup"]}

    def workload_is_bounded(self, identity):
        assert identity["entry_pid"] > 0 and identity["cgroup_identity"]
        return self.bounded

    def terminate(self, unit, cgroup=None):
        self.terminations += 1
        return super().terminate(unit, cgroup)


def stage_supervisor(tmp_path, scope, *, stage_timeout=5, job_timeout=10, grace=.15, command=None):
    manager, calls = supervisor(tmp_path, scope=scope, delay="terminal_qm")
    manager.settings = replace(manager.settings, stage_timeout_seconds=stage_timeout, job_timeout_seconds=job_timeout)
    manager.MONITOR_GRACE_SECONDS = grace
    payload = request()
    record = manager._new_record(payload)
    record["authorization"] = {"authorized": True}
    record["snapshot"].update(status="running", started_at=utcnow())
    manager.records[payload.job_id] = record
    manager.cancel_events[payload.job_id] = asyncio.Event()
    manager.scope_ready, manager.recovering = True, False
    scope.manager, scope.record = manager, record
    if command:
        manager.command_builder = command
    manager._save(record)
    return manager, record, payload, calls


def close_synthetic(manager, scope):
    scope.release_inspection.set()
    for process in scope.processes.values():
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
    manager.analysis_executor.shutdown(wait=True)


@pytest.mark.anyio
@pytest.mark.parametrize("missing_view", [False, True])
async def test_temporary_bus_loss_keeps_same_execution_and_recovers_admission(tmp_path, missing_view):
    scope = MonitoredScope(failures=2, missing_view=missing_view)
    manager, record, payload, calls = stage_supervisor(tmp_path, scope)
    task = asyncio.create_task(manager._stage(record, "terminal_qm"))
    try:
        await until(lambda: manager.monitor_degraded)
        assert not (await manager.health())["accepting_jobs"]
        with pytest.raises(WorkerError, match="not ready"):
            await manager.submit(request().model_copy(update={"enqueue_sequence": 2}))
        assert await task is True
        assert calls == ["terminal_qm"] and len(scope.launches) == 1
        assert scope.terminations == 1  # Normal cleanup only, after synthetic result.
        assert not manager.monitor_degraded and manager.fatal is None
        assert (await manager.health())["accepting_jobs"]
        run = read_json(manager._directory(payload) / "journal.json")["snapshot"]["stage_runs"][0]
        assert run["status"] == "completed" and run["attempt"] == 1
        diagnostics = run["results"]["execution_diagnostics"]
        assert diagnostics["failure_count"] == 2 and diagnostics["recovered_at"]
        assert diagnostics["process_returncode_before_cleanup"] == 0
        assert diagnostics["process_returncode_after_cleanup"] == 0
        assert "PRIVATE-CREDENTIAL" not in json.dumps(diagnostics)
        expected = "unit_not_found" if missing_view else "bus_connection_unavailable"
        assert diagnostics["recent_failures"][0]["control"]["stderr_categories"] == [expected]
    finally:
        await task
        close_synthetic(manager, scope)


@pytest.mark.anyio
async def test_persistent_bus_loss_stops_safely_without_scientific_retry(tmp_path):
    scope = MonitoredScope(failures=1000)
    manager, record, payload, calls = stage_supervisor(tmp_path, scope, grace=.12)
    try:
        assert await manager._stage(record, "terminal_qm") is False
        state = manager.get(payload.job_id)
        assert state["status"] == "failed" and state["cleanup_confirmed"]
        assert len(state["stage_runs"]) == 1 and calls == ["terminal_qm"]
        assert state["stage_runs"][0]["errors"] == [{"code": "scope_monitor_unavailable", "retryable": False}]
        assert manager.fatal == "scope_monitor_unavailable"
        assert not (await manager.health())["accepting_jobs"]
        diag = state["stage_runs"][0]["results"]["execution_diagnostics"]
        assert diag["process_returncode_before_cleanup"] is None
        assert diag["process_returncode_after_cleanup"] != 0
        assert diag["failure_count"] > manager.MONITOR_HISTORY_LIMIT
        assert len(diag["recent_failures"]) == manager.MONITOR_HISTORY_LIMIT
        assert all(process.poll() is not None for process in scope.processes.values())
    finally:
        close_synthetic(manager, scope)


@pytest.mark.anyio
@pytest.mark.parametrize("gated,bounded", [(False, True), (True, False)])
async def test_no_monitor_grace_before_gate_or_without_local_budget_evidence(tmp_path, gated, bounded):
    scope = MonitoredScope(failures=100, gated=gated, bounded=bounded)
    manager, record, payload, calls = stage_supervisor(tmp_path, scope)
    try:
        assert await manager._stage(record, "terminal_qm") is False
        assert scope.inspections == 1 and len(scope.launches) == 1
        assert manager.get(payload.job_id)["stage_runs"][0]["errors"][0]["code"] == "scope_monitor_unavailable"
        assert not (await manager.health())["accepting_jobs"]
    finally:
        close_synthetic(manager, scope)


@pytest.mark.anyio
@pytest.mark.parametrize("reason", ["cancelled", "stage_timeout", "job_timeout", "storage_quota_exceeded"])
async def test_monitor_grace_preserves_cancellation_and_original_budgets(tmp_path, reason):
    scope = MonitoredScope(failures=1000)
    manager, record, payload, _ = stage_supervisor(tmp_path, scope,
        stage_timeout=.06 if reason == "stage_timeout" else 5,
        job_timeout=.06 if reason == "job_timeout" else 10, grace=1)
    task = asyncio.create_task(manager._stage(record, "terminal_qm"))
    try:
        await until(lambda: manager.monitor_degraded)
        if reason == "cancelled":
            await manager.cancel(payload.job_id, payload)
        elif reason == "storage_quota_exceeded":
            manager.settings = replace(manager.settings, max_job_bytes=1)
        assert await task is False
        state = manager.get(payload.job_id)
        assert state["cleanup_confirmed"] and len(state["stage_runs"]) == 1
        if reason == "cancelled":
            assert state["status"] == "cancelled"
        else:
            expected = "storage_quota_exceeded" if reason == "storage_quota_exceeded" else "timeout"
            assert state["status"] == "failed" and state["errors"][-1]["code"] == expected
    finally:
        await task
        close_synthetic(manager, scope)


@pytest.mark.anyio
async def test_slow_inspection_cannot_block_stage_deadline(tmp_path):
    scope = MonitoredScope(slow=True)
    manager, record, payload, _ = stage_supervisor(tmp_path, scope, stage_timeout=.04)
    started = time.monotonic()
    try:
        assert await manager._stage(record, "terminal_qm") is False
        assert time.monotonic() - started < .4
        assert manager.get(payload.job_id)["errors"][-1]["code"] == "timeout"
    finally:
        close_synthetic(manager, scope)


@pytest.mark.anyio
async def test_monitor_failure_retains_ownership_when_cleanup_is_unconfirmed(tmp_path):
    scope = MonitoredScope(failures=1000)
    scope.confirm = False
    manager, record, payload, _ = stage_supervisor(tmp_path, scope, grace=.03)
    try:
        assert await manager._stage(record, "terminal_qm") is False
        state = manager.get(payload.job_id)
        assert state["status"] == "running" and not state["cleanup_confirmed"]
        assert record["running_scope"] and len(scope.launches) == 1
        assert state["errors"][-1]["code"] == "stage_cleanup_unconfirmed"
        assert not (await manager.health())["accepting_jobs"]
        saved = read_json(manager._directory(payload) / "journal.json")
        assert saved["snapshot"]["stage_runs"][0]["results"]["execution_diagnostics"]["failure_count"]
    finally:
        close_synthetic(manager, scope)


@pytest.mark.anyio
async def test_scope_recovery_cannot_clear_an_unrelated_fatal_or_enable_analysis(tmp_path):
    scope = MonitoredScope(failures=2)
    manager, record, _, _ = stage_supervisor(tmp_path, scope)
    task = asyncio.create_task(manager._stage(record, "terminal_qm"))
    try:
        await until(lambda: manager.monitor_degraded)
        manager.fatal = "independent_failure"
        assert await task is True
        assert manager.fatal == "independent_failure"
        health = await manager.health()
        assert not health["accepting_jobs"] and not health["analysis_ready"]
    finally:
        await task
        close_synthetic(manager, scope)


@pytest.mark.anyio
async def test_maintenance_drain_keeps_completed_analysis_available(tmp_path):
    scope = MonitoredScope()
    manager, _, _, _ = stage_supervisor(tmp_path, scope)
    try:
        manager.draining = True
        health = await manager.health()
        assert health["analysis_ready"] and not health["accepting_jobs"]
        manager.stopping = True
        assert not (await manager.health())["analysis_ready"]
    finally:
        close_synthetic(manager, scope)


@pytest.mark.anyio
async def test_engine_exit_code_is_persisted_separately_from_monitor_errors(tmp_path):
    scope = MonitoredScope()
    manager, record, payload, _ = stage_supervisor(tmp_path, scope,
        command=lambda *args: [sys.executable, "-c", "raise SystemExit(7)"])
    try:
        assert await manager._stage(record, "terminal_qm") is False
        state = manager.get(payload.job_id)
        assert len(state["stage_runs"]) == 2 and manager.fatal is None
        for run in state["stage_runs"]:
            assert run["errors"][0]["code"] == "engine_process_failed"
            assert run["results"]["execution_diagnostics"]["process_returncode_before_cleanup"] == 7
            assert run["results"]["execution_diagnostics"]["process_returncode_after_cleanup"] == 7
    finally:
        close_synthetic(manager, scope)


def test_systemctl_diagnostics_are_typed_bounded_and_redacted(monkeypatch):
    scope = ScopeControl()
    secret = "Bearer PRIVATE-CREDENTIAL postgres://private@host/db " * 1000
    monkeypatch.setattr(scope, "_call", lambda *a, **k: subprocess.CompletedProcess(a, 1,
        stdout="LoadState=loaded\n", stderr="Failed to connect to bus: Connection reset by peer " + secret))
    with pytest.raises(ScopeInspectionError) as captured:
        scope.inspect("nexpoly-polymer-md-test.scope")
    diagnostic = captured.value.diagnostic
    assert diagnostic["returncode"] == 1
    assert diagnostic["stderr_categories"] == ["bus_connection_unavailable"]
    assert len(json.dumps(diagnostic)) < 400 and "PRIVATE" not in json.dumps(diagnostic)

    def timeout(*a, **k):
        assert k["timeout"] == 2
        raise subprocess.TimeoutExpired("private-command " + secret, 2, stderr="Timed out " + secret)

    monkeypatch.setattr(scope, "_call", timeout)
    with pytest.raises(ScopeInspectionError) as captured:
        scope.inspect("nexpoly-polymer-md-test.scope")
    assert captured.value.diagnostic["exception_type"] == "TimeoutExpired"
    assert captured.value.diagnostic["stderr_categories"] == ["control_timeout"]
    assert "PRIVATE" not in str(captured.value) + json.dumps(captured.value.diagnostic)


def bounded_cgroup(tmp_path, monkeypatch):
    scope = ScopeControl()
    monkeypatch.setattr(process_control, "CGROUP_ROOT", tmp_path)
    group = f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice/nexpoly-polymer-md-test.scope"
    target = tmp_path / group.lstrip("/")
    target.mkdir(parents=True)
    for name, text in {"cpu.max": "1600000 100000", "memory.max": str(32 * 1024**3),
                       "pids.max": "4096", "cgroup.events": "populated 1\nfrozen 0\n"}.items():
        (target / name).write_text(text)
    monkeypatch.setattr(process_control, "process_start_ticks", lambda pid: 123)
    monkeypatch.setattr(scope, "_process_cgroup", lambda pid: group)
    identity = {"unit": target.name, "cgroup": group, "entry_pid": 99999, "entry_start_ticks": 123}
    identity["cgroup_identity"] = scope.capture_workload_identity(identity)
    identity["gate_granted"] = True
    assert scope.workload_is_bounded(identity)
    return scope, identity, target


@pytest.mark.parametrize("name,value", [("cpu.max", "max 100000"), ("cpu.max", "1600001 100000"),
    ("memory.max", str(32 * 1024**3 + 1)), ("pids.max", "4097"), ("cgroup.events", "populated 0")])
def test_local_workload_evidence_rejects_relaxed_limits(tmp_path, monkeypatch, name, value):
    scope, identity, target = bounded_cgroup(tmp_path, monkeypatch)
    (target / name).write_text(value)
    assert not scope.workload_is_bounded(identity)


def test_local_workload_evidence_fences_cgroup_and_process_replacement(tmp_path, monkeypatch):
    scope, identity, target = bounded_cgroup(tmp_path, monkeypatch)
    retired = target.with_name("retired")
    target.rename(retired)
    shutil.copytree(retired, target)
    assert not scope.workload_is_bounded(identity)
    identity["cgroup_identity"] = scope.capture_workload_identity(identity)
    monkeypatch.setattr(process_control, "process_start_ticks", lambda pid: 456)
    assert not scope.workload_is_bounded(identity)
