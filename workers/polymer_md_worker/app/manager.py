from __future__ import annotations

import asyncio
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Callable
from uuid import uuid4

from polymer_md.contracts import Artifact, StageRun, SubmitRequest, TERMINAL_STATUSES, WorkerSnapshot
from .config import WorkerSettings
from .execution_gate import process_start_ticks
from .journal import RootLock, atomic_json, read_json, restore_file, safe_file, sha256_file
from .process_control import ScopeControl, ScopeIdentityError, ScopeInspectionError, scientific_environment
from .start_authorization import StartAuthorizer
from .native_execution import molecular_only, FORCEFIELDS, complete_requested_results


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkerError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code, self.status_code = code, status_code


class JobManager:
    """One CPU execution at a time; immutable stage success, bounded technical retry."""

    FAILURE_LOG_BYTES = 2 * 1024**2
    FAILURE_INPUT_BYTES = 8 * 1024**2
    FAILURE_STAGE_BYTES = 16 * 1024**2
    FAILURE_JOB_BYTES = 64 * 1024**2
    FAILURE_STAGE_FILES = 16
    MONITOR_GRACE_SECONDS = 30.0
    MONITOR_HISTORY_LIMIT = 8

    def __init__(self, settings: WorkerSettings, *, scope=None, authorizer=None,
                 runtime_probe: Callable[[], dict] | None = None, command_builder=None,
                 analysis_probe: Callable[[], bool] | None = None):
        self.settings = settings
        self.instance_id = uuid4().hex
        self.scope = scope or ScopeControl()
        self.authorizer = authorizer or StartAuthorizer(settings.authorization_url, settings.authorization_token)
        self.runtime_probe = runtime_probe or self._runtime_probe
        self.analysis_probe = analysis_probe or self._analysis_runtime_probe
        self.command_builder = command_builder or self._command
        self.execution_gate = command_builder is None
        self.root_lock = RootLock(settings.job_root)
        self.records: dict[str, dict] = {}
        self.cancel_events: dict[str, asyncio.Event] = {}
        self.wake = asyncio.Event()
        self.lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.stopping = False
        self.draining = False
        self.maintenance_updated_at = None
        self.maintenance_persisted = False
        self.release_digest = None
        self.recovering = True
        self.fatal: str | None = None
        self.scope_ready = False
        self.monitor_degraded = False
        self.executing: str | None = None
        self._deleting: set[str] = set()
        self.readers: dict[str, int] = {}
        self.analysis_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="polymer-md-analysis")
        self.analysis_inflight = 0
        self.analysis_drained = asyncio.Event()
        self.analysis_drained.set()

    def _runtime_probe(self) -> dict:
        try:
            from .runtime import probe_runtime
            return probe_runtime(self.settings.runtime_manifest)
        except Exception as exc:
            return {"ready": False, "errors": ["runtime_probe_unavailable"], "error_type": type(exc).__name__}

    @staticmethod
    def _analysis_runtime_probe() -> bool:
        # Trajectory readers run in this HTTP interpreter's thread pool, not
        # the separately sealed scientific runner prefix. Verify its imports.
        try:
            for package in ("numpy", "pandas", "scipy", "mdtraj"):
                importlib.import_module(package)
        except Exception:
            return False
        return True

    async def _analysis_ready(self, runtime: dict | None = None) -> bool:
        if self.stopping or self.recovering or self.fatal:
            return False
        if runtime is None:
            runtime = await asyncio.to_thread(self.runtime_probe)
        if not runtime.get("ready"):
            return False
        available = await asyncio.to_thread(self.analysis_probe)
        return bool(available and not (self.stopping or self.recovering or self.fatal))

    def _directory(self, request: SubmitRequest) -> Path:
        return self.settings.job_root / request.job_id / request.attempt_token

    def _save(self, record: dict) -> None:
        # Validate before publishing. Journal failure fences all new execution.
        record["snapshot"]["revision"] = record["snapshot"].get("revision", 0) + 1
        try:
            WorkerSnapshot.model_validate(record["snapshot"])
            request = SubmitRequest.model_validate(record["request"])
            atomic_json(self._directory(request) / "journal.json", record)
        except BaseException:
            self.fatal = "journal_persistence_failed"
            raise

    def _snapshot(self, record: dict) -> dict:
        return WorkerSnapshot.model_validate(record["snapshot"]).model_dump(mode="json")

    async def start(self):
        self.root_lock.acquire()
        try:
            release = Path(__file__).resolve().parents[3]
            if (release / "release.json").exists() or (release / "release.json").is_symlink():
                from .release_contract import verify_release
                self.release_digest = verify_release(release)["source_digest"]
            self._load_maintenance()
            from .external_execution import assert_no_external_execution
            try:
                assert_no_external_execution(self.settings.job_root)
            except (OSError, ValueError, RuntimeError):
                self.fatal = "external_cleanup_unconfirmed"
            self.scope_ready = bool((await asyncio.to_thread(self.scope.probe)).get("ready"))
            await self._load()
            self.recovering = False
            self.task = asyncio.create_task(self._run(), name="polymer-md-cpu-supervisor")
        except BaseException:
            self.root_lock.release()
            raise

    def _load_maintenance(self):
        path = self.settings.job_root / ".maintenance.json"
        if path.exists() or path.is_symlink():
            value = read_json(path)
            if (value.get("version") != 1 or type(value.get("draining")) is not bool
                    or not isinstance(value.get("updated_at"), str)):
                raise ValueError("worker maintenance state is invalid")
            self.draining = value["draining"]
            self.maintenance_updated_at = value["updated_at"]
        else:
            self.draining = self.draining or self.settings.start_drained
            self._persist_maintenance(self.draining)
        self.maintenance_persisted = True

    def _persist_maintenance(self, draining: bool):
        updated = utcnow()
        try:
            atomic_json(self.settings.job_root / ".maintenance.json",
                        {"version": 1, "draining": draining, "updated_at": updated})
        except BaseException:
            self.fatal = "maintenance_persistence_failed"
            raise
        self.maintenance_updated_at = updated
        self.maintenance_persisted = True

    async def set_draining(self, draining: bool):
        async with self.lock:
            self._persist_maintenance(draining)
            self.draining = draining
            self.wake.set()

    def resource_limits(self):
        return {"cpu_count": 16, "memory_bytes": 32 * 1024**3, "gpu_count": 0,
                "stage_timeout_seconds": self.settings.stage_timeout_seconds,
                "job_timeout_seconds": self.settings.job_timeout_seconds,
                "max_job_bytes": self.settings.max_job_bytes, "max_total_atoms": 20000,
                "max_replicated_atoms": 60000}

    def _time_limits(self, record):
        # Budgets are execution facts: a release must not extend an old job.
        stored = record["snapshot"].get("results", {}).get("resource_limits", {})
        values = (stored.get("stage_timeout_seconds", 8 * 3600),
                  stored.get("job_timeout_seconds", 48 * 3600))
        if any(type(v) not in {int, float} or not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError("persisted worker time budget is invalid")
        return values

    async def stop(self):
        self.stopping = True
        self.wake.set()
        # Shutdown is interruption, not a user's cancellation. Persisted running
        # stages are recovered/retried after proving the old scope empty.
        if self.task:
            await self.task
        # A disconnected HTTP client cannot stop an already running native reader.
        # Keep its lease and the Worker root lock until that reader really exits.
        await asyncio.to_thread(self.analysis_executor.shutdown, wait=True, cancel_futures=True)
        await self.analysis_drained.wait()
        self.root_lock.release()

    async def _load(self):
        sequences = set()
        for directory in sorted(self.settings.job_root.iterdir()):
            if directory.name.startswith("."):
                continue
            if directory.is_symlink() or not directory.is_dir():
                raise RuntimeError("unsafe worker job root entry")
            if not (directory / "current.json").exists():
                candidates = [entry for entry in directory.iterdir() if entry.is_dir() and not entry.is_symlink()
                              and len(entry.name) == 64 and all(c in "0123456789abcdef" for c in entry.name)
                              and (entry / "journal.json").is_file()]
                if len(candidates) != 1:
                    raise RuntimeError("attempt pointer missing and cannot be recovered unambiguously")
                atomic_json(directory / "current.json", {"attempt_token": candidates[0].name})
            pointer = read_json(directory / "current.json")
            token = pointer.get("attempt_token", "")
            if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
                raise RuntimeError("invalid current attempt pointer")
            record = read_json(directory / token / "journal.json")
            request = SubmitRequest.model_validate(record["request"])
            snapshot = WorkerSnapshot.model_validate(record["snapshot"])
            if request.job_id != directory.name or request.attempt_token != token:
                raise RuntimeError("journal identity differs from its directory")
            if any(getattr(request, key) != getattr(snapshot, key) for key in
                   ("job_id", "attempt_token", "request_sha256", "enqueue_sequence")):
                raise RuntimeError("journal request and snapshot identity differ")
            if request.enqueue_sequence in sequences:
                raise RuntimeError("duplicate durable enqueue sequence")
            sequences.add(request.enqueue_sequence)
            if snapshot.status in TERMINAL_STATUSES and record.get("running_scope"):
                raise RuntimeError("terminal journal still owns an execution scope")
            self.records[request.job_id] = record
            self.cancel_events[request.job_id] = asyncio.Event()
            if record.get("purging"):
                if snapshot.status not in TERMINAL_STATUSES:
                    raise RuntimeError("active job cannot have a purge intent")
                shutil.rmtree(directory)
                del self.records[request.job_id]
                del self.cancel_events[request.job_id]
                continue
            if record.get("artifacts_deleting"):
                self._finish_artifact_delete(record)
            if snapshot.status in TERMINAL_STATUSES:
                continue
            identity = record.get("running_scope")
            if identity:
                confirmed = await self._terminate(identity)
                if not confirmed:
                    self._hold(record, "recovery_cleanup_unconfirmed")
                    continue
                record["running_scope"] = None
                current = next((s for s in reversed(record["snapshot"]["stage_runs"]) if s["status"] == "running"), None)
                if current:
                    current.update(status="failed", finished_at=utcnow())
                    current["errors"].append({"code": "worker_interrupted", "retryable": True})
                    self._publish_runner_log(record, current)
                    self._capture_failed_diagnostics(record, current)
            elif record["snapshot"]["stage_runs"]:
                # Older Workers may have cleanly stopped after marking a stage
                # failed, before this diagnostic-preservation feature existed.
                current = record["snapshot"]["stage_runs"][-1]
                if current["status"] == "failed" and current["stage"] not in record["completed_stages"]:
                    self._publish_runner_log(record, current)
                    self._capture_failed_diagnostics(record, current)
            if snapshot.status == "cancel_requested":
                self.cancel_events[request.job_id].set()
                self._finish(record, "cancelled")
            else:
                # The old instance remains externally visible until Backend
                # authorizes an exact compare-and-swap ownership replacement.
                record["resume_pending"] = True
                record["needs_rebind"] = snapshot.worker_instance_id
                self._save(record)

    def _hold(self, record: dict, code: str):
        self.fatal = self.fatal or code
        record["snapshot"]["cleanup_confirmed"] = False
        record["snapshot"]["errors"].append({"code": code, "retryable": True})
        self._save(record)

    async def health(self) -> dict:
        runtime = await asyncio.to_thread(self.runtime_probe)
        analysis_ready = await self._analysis_ready(runtime)
        accepting = bool(runtime.get("ready") and self.scope_ready and self.authorizer.configured
                         and not (self.draining or self.recovering or self.fatal or self.stopping or self.monitor_degraded))
        reason = ("cleanup_unconfirmed" if self.fatal else "stopping" if self.stopping else
                  "recovering" if self.recovering else "maintenance" if self.draining else
                  "runtime_not_ready" if not runtime.get("ready") else
                  "scope_not_ready" if not self.scope_ready else
                  "authorization_unavailable" if not self.authorizer.configured else
                  "monitor_unavailable" if self.monitor_degraded else None)
        return {"protocol_version": 1, "ready": accepting, "start_authorization_version": 1 if self.authorizer.configured else 0,
                "worker_instance_id": self.instance_id, "runtime_ready": bool(runtime.get("ready")),
                "analysis_ready": analysis_ready,
                "worker_release_digest": self.release_digest,
                "admission_reason": reason,
                "maintenance": {"enabled": self.draining, "updated_at": self.maintenance_updated_at,
                                "persisted": self.maintenance_persisted},
                "resource_limits": self.resource_limits(),
                "scope_ready": self.scope_ready, "accepting_jobs": accepting,
                "monitor_degraded": self.monitor_degraded,
                "draining": self.draining, "recovering": self.recovering, "fatal_error": self.fatal,
                "queued_jobs": sum(r["snapshot"]["status"] == "queued" or r.get("resume_pending", False) for r in self.records.values()),
                "running_jobs": int(self.executing is not None), "max_queued_jobs": self.settings.max_queued_jobs,
                "max_running_jobs": 1, "cpu_count": 16, "memory_bytes": 32 * 1024**3, "gpu_count": 0,
                "analysis_inflight": self.analysis_inflight, "max_running_analyses": 1, "max_pending_analyses": 1,
                "runtime": runtime}

    async def capabilities(self) -> dict:
        return {"protocol_version": 1, "worker_instance_id": self.instance_id, "execution": "local_cpu_mpi",
                "forcefields": list(FORCEFIELDS), "calculations": ["basic", "basic_tc"],
                "polymer_types": ["homopolymer", "alternating", "random", "block", "blend"], "max_total_atoms": 20000,
                "max_replicated_atoms": 60000, "cpu_count": 16, "gpu_count": 0,
                "technical_retries_per_stage": 1, "stage_timeout_seconds": self.settings.stage_timeout_seconds,
                "job_timeout_seconds": self.settings.job_timeout_seconds,
                "resource_limits": self.resource_limits(),
                "ready": (await self.health())["accepting_jobs"]}

    def _same(self, record: dict, request: SubmitRequest):
        if record["request"] != request.model_dump(mode="json"):
            raise WorkerError("attempt_conflict", "job already belongs to a different execution identity")

    async def submit(self, request: SubmitRequest) -> tuple[dict, bool]:
        async with self.lock:
            if (self.settings.job_root / ".tombstones" / f"{request.job_id}.json").exists():
                raise WorkerError("job_purged", "job was permanently deleted")
            existing = self.records.get(request.job_id)
            if existing:
                self._same(existing, request)
                return self._snapshot(existing), False
            if request.job_id in self._deleting:
                raise WorkerError("job_deleting", "job cleanup is in progress")
            state = await self.health()
            if not state["accepting_jobs"]:
                raise WorkerError("worker_not_ready", "worker runtime, scope or authorization is not ready", 503)
            if state["queued_jobs"] >= self.settings.max_queued_jobs:
                raise WorkerError("queue_full", "worker queue is full", 429)
            if any(r["request"]["enqueue_sequence"] == request.enqueue_sequence for r in self.records.values()):
                raise WorkerError("enqueue_conflict", "enqueue sequence already belongs to another job")
            record = self._new_record(request)
            self._save(record)
            atomic_json(self.settings.job_root / request.job_id / "current.json", {"attempt_token": request.attempt_token})
            self.records[request.job_id] = record
            self.cancel_events[request.job_id] = asyncio.Event()
            self.wake.set()
            return self._snapshot(record), True

    def _new_record(self, request: SubmitRequest) -> dict:
        return {"journal_version": 1, "request": request.model_dump(mode="json"), "created_at": utcnow(),
                "authorization": None, "running_scope": None, "completed_stages": {}, "artifact_paths": {},
                "snapshot": WorkerSnapshot(job_id=request.job_id, attempt_token=request.attempt_token,
                    request_sha256=request.request_sha256, enqueue_sequence=request.enqueue_sequence,
                    worker_instance_id=self.instance_id, status="queued", results={
                        "worker_release_digest": self.release_digest,
                        "resource_limits": self.resource_limits(),
                        "retention_policy": {"artifact_retention_days": 7, "metadata_retention_days": 30},
                    }).model_dump(mode="json")}

    def get(self, job_id: str) -> dict:
        record = self.records.get(job_id)
        if not record:
            raise WorkerError("job_not_found", "unknown job", 404)
        return self._snapshot(record)

    def assert_attempt(self, job_id: str, attempt_token: str):
        record = self.records.get(job_id)
        if not record:
            raise WorkerError("job_not_found", "unknown job", 404)
        if record["request"]["attempt_token"] != attempt_token:
            raise WorkerError("attempt_conflict", "request belongs to another attempt")

    async def cancel(self, job_id: str, request: SubmitRequest) -> dict:
        if request.job_id != job_id:
            raise WorkerError("attempt_conflict", "cancel path and request identity differ")
        async with self.lock:
            if (self.settings.job_root / ".tombstones" / f"{job_id}.json").exists():
                raise WorkerError("job_purged", "job was permanently deleted")
            record = self.records.get(job_id)
            if not record:
                if any(r["request"]["enqueue_sequence"] == request.enqueue_sequence for r in self.records.values()):
                    raise WorkerError("enqueue_conflict", "enqueue sequence belongs to another job")
                # Durable tombstone defeats a late identical submit.
                record = self._new_record(request)
                self._finish(record, "cancelled")
                atomic_json(self.settings.job_root / job_id / "current.json", {"attempt_token": request.attempt_token})
                self.records[job_id] = record
                self.cancel_events[job_id] = asyncio.Event()
                return self._snapshot(record)
            self._same(record, request)
            if record["snapshot"]["status"] in TERMINAL_STATUSES:
                return self._snapshot(record)
            self.cancel_events[job_id].set()
            if self.executing != job_id and not record.get("running_scope"):
                self._finish(record, "cancelled")
            else:
                record["snapshot"]["status"] = "cancel_requested"
                self._save(record)
            self.wake.set()
            return self._snapshot(record)

    def _finish(self, record: dict, status: str, *, quality_status=None, error=None):
        if record.get("running_scope"):
            raise RuntimeError("cannot publish terminal state before resource cleanup")
        snapshot = record["snapshot"]
        record["resume_pending"] = False
        snapshot.update(status=status, finished_at=utcnow(), cleanup_confirmed=True)
        if quality_status:
            snapshot["quality_status"] = quality_status
        elif status == "failed":
            snapshot["quality_status"] = "failed"
        if error:
            snapshot["errors"].append(error)
        reason = (error or {}).get("code")
        if not reason:
            reason = ("job_cancelled" if status == "cancelled" else "equilibrium_not_converged"
                      if snapshot["quality_status"] == "not_converged" else "job_failed"
                      if status == "failed" else "selected_property_not_produced")
        snapshot["results"] = complete_requested_results(record["request"]["scientific_request"],
                                                         snapshot["results"], reason=reason)
        self._save(record)

    async def _run(self):
        while not self.stopping:
            self.wake.clear()
            queued = sorted((r for r in self.records.values() if r["snapshot"]["status"] == "queued" or r.get("resume_pending")),
                            key=lambda r: r["request"]["enqueue_sequence"])
            if queued and not self.fatal and not self.draining:
                record = queued[0]
                if not (await asyncio.to_thread(self.runtime_probe)).get("ready"):
                    try:
                        await asyncio.wait_for(self.wake.wait(), timeout=max(1, self.settings.poll_seconds))
                    except asyncio.TimeoutError:
                        pass
                    continue
                self.executing = record["request"]["job_id"]
                try:
                    await self._execute(record)
                except Exception:
                    identity = record.get("running_scope")
                    if identity and not await self._terminate(identity):
                        self._hold(record, "executor_cleanup_unconfirmed")
                    elif self.fatal != "journal_persistence_failed":
                        record["running_scope"] = None
                        self._finish(record, "failed", error={"code": "supervisor_error", "retryable": True})
                finally:
                    self.executing = None
                continue
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.settings.poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def _execute(self, record: dict):
        request = SubmitRequest.model_validate(record["request"])
        if self.cancel_events[request.job_id].is_set():
            self._finish(record, "cancelled")
            return
        if not record["authorization"] or record.get("needs_rebind"):
            try:
                if record.get("needs_rebind"):
                    authorization = await self.authorizer.authorize(request, self.instance_id,
                        replaces_worker_instance_id=record["needs_rebind"], prior_execution_cleanup_confirmed=True)
                else:
                    authorization = await self.authorizer.authorize(request, self.instance_id)
            except Exception:
                # Authorization failure is a hold before any engine start.
                record["snapshot"]["errors"] = [{"code": "start_authorization_unavailable", "retryable": True}]
                self._save(record)
                await asyncio.sleep(min(5, self.settings.poll_seconds * 4))
                return
            record["authorization"] = authorization
            record["needs_rebind"] = None
            record["resume_pending"] = False
            record["snapshot"]["worker_instance_id"] = self.instance_id
            self._save(record)
        snapshot = record["snapshot"]
        snapshot.update(status="running", cleanup_confirmed=False, started_at=snapshot["started_at"] or utcnow())
        self._save(record)
        for stage in ["terminal_qm", "monomer_qm"]:
            if not await self._stage(record, stage):
                return
        if molecular_only(request.scientific_request.model_dump(mode="json")):
            quality = record["completed_stages"]["monomer_qm"]["result"]["quality_status"]
            self._finish(record, "completed", quality_status=quality)
            return
        for stage in ["build", "eq1", "eq2", "eq3", "analyze_0"]:
            if not await self._stage(record, stage):
                return
        latest = record["completed_stages"]["analyze_0"]["result"]
        for iteration in range(1, request.scientific_request.max_additional + 1):
            if latest.get("equilibrated") is True:
                break
            for stage in (f"additional_{iteration}", f"analyze_{iteration}"):
                if not await self._stage(record, stage):
                    return
            latest = record["completed_stages"][f"analyze_{iteration}"]["result"]
        if not latest.get("equilibrated"):
            self._finish(record, "completed", quality_status="not_converged")
            return
        native = request.scientific_request.native_configuration
        selected = set(native.properties) if native is not None else set()
        # Each extension starts from the latest qualified equilibrium cell.
        # A technical failure retains all already published observations and
        # follows the existing terminal/retry policy before any later extension.
        if selected & {"sp_total", "sp_ced"}:
            for stage in ("sp", "sp_analyze"):
                if not await self._stage(record, stage):
                    return
        if request.scientific_request.calculation == "basic_tc" and latest.get("tc_eligible") is True:
            for stage in ("tc", "tc_analyze"):
                if not await self._stage(record, stage):
                    return
        elif request.scientific_request.calculation == "basic_tc":
            snapshot["quality_status"] = self._combined_quality(snapshot["quality_status"], "warning")
            snapshot["results"].setdefault("quality_summary", {})["thermal"] = {
                "quality_status": "warning", "reason": "tc_ineligible"}
            snapshot["errors"].append({"code": "tc_ineligible", "retryable": False})
        if "tg" in selected:
            for stage in ("tg", "tg_analyze"):
                if not await self._stage(record, stage):
                    return
        snapshot["results"] = self._public_results(snapshot["results"])
        self._finish(record, "completed")

    def _command(self, request_path: Path, stage: str, work_dir: Path, result_path: Path) -> list[str]:
        command = [self.settings.runner_python, "-m", "workers.polymer_md_worker.app.scientific_runner",
                "--request", str(request_path), "--stage", stage, "--work-dir", str(work_dir), "--result", str(result_path)]
        incoming = read_json(work_dir / ".input-checkpoint.json")
        if incoming["stage"] != stage:
            raise ValueError("stage checkpoint input identity mismatch")
        if incoming.get("checkpoint"):
            command += ["--checkpoint", incoming["checkpoint"], "--checkpoint-sha256", incoming["sha256"]]
        return command

    def _restore_checkpoints(self, record, work_dir, directory):
        for completed in record["completed_stages"].values():
            checkpoint = completed.get("checkpoint")
            if not checkpoint:
                raise ValueError("completed stage has no frozen checkpoint")
            frozen = safe_file(directory, checkpoint["path"])
            if sha256_file(frozen) != checkpoint["sha256"]:
                raise ValueError("frozen checkpoint integrity mismatch")
            original = completed["result"]["checkpoint"]
            if original["sha256"] != checkpoint["sha256"]:
                raise ValueError("checkpoint identity mismatch")
            try:
                current = safe_file(work_dir, original["path"])
                intact = sha256_file(current) == checkpoint["sha256"]
            except ValueError:
                intact = False
            if not intact:
                restored = restore_file(work_dir, original["path"], frozen)
                if sha256_file(restored) != checkpoint["sha256"]:
                    raise ValueError("restored checkpoint integrity mismatch")

    @staticmethod
    def _predecessor(record, stage):
        fixed = {"terminal_qm": None, "monomer_qm": "terminal_qm", "build": "monomer_qm",
                 "eq1": "build", "eq2": "eq1", "eq3": "eq2", "analyze_0": "eq3", "tc_analyze": "tc",
                 "sp_analyze": "sp", "tg_analyze": "tg"}
        if stage in fixed:
            return fixed[stage]
        if stage.startswith("additional_"):
            return f"analyze_{int(stage.rsplit('_', 1)[1]) - 1}"
        if stage.startswith("analyze_"):
            return f"additional_{int(stage.rsplit('_', 1)[1])}"
        if stage in {"sp", "tc", "tg"}:
            analyses = [name for name, entry in record["completed_stages"].items()
                        if name.startswith("analyze_") and entry.get("result", {}).get("equilibrated") is True]
            if not analyses:
                raise ValueError("post-equilibration stages require a qualified analysis checkpoint")
            return max(analyses, key=lambda name: int(name.rsplit("_", 1)[1]))
        raise ValueError("unknown scientific stage")

    def _stage_input(self, record, stage, work_dir, directory):
        self._restore_checkpoints(record, work_dir, directory)
        predecessor = self._predecessor(record, stage)
        incoming = {"stage": stage, "checkpoint": None}
        if predecessor:
            checkpoint = record["completed_stages"][predecessor]["checkpoint"]
            incoming.update(checkpoint=str(safe_file(directory, checkpoint["path"])), sha256=checkpoint["sha256"])
        atomic_json(work_dir / ".input-checkpoint.json", incoming)

    async def _terminate(self, identity: dict) -> bool:
        try:
            if identity.get("boot_id") and identity["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
                return True  # No process from the persisted prior boot can survive.
            if identity.get("launch_protocol") == "gated-v1":
                # Revoke a pending entry before killing a launcher whose scope may
                # still be registering. An entry never starts MPI without a gate.
                atomic_json(Path(identity["gate_path"]), {"allowed": False})
                pid, ticks = identity.get("launcher_pid"), identity.get("launcher_start_ticks")
                if (not pid or ticks is None) and Path(identity["entry_path"]).is_file():
                    entry = read_json(Path(identity["entry_path"]))
                    if entry.get("unit") == identity["unit"] and entry.get("nonce") == identity["nonce"]:
                        pid, ticks = entry.get("pid"), entry.get("start_ticks")
                if not pid or ticks is None:
                    if not identity.get("launch_failed_before_spawn"):
                        return False  # Crash in Popen -> journal gap: do not free quota.
                elif not await asyncio.to_thread(self.scope.kill_launcher, pid, ticks):
                    return False
            return await asyncio.to_thread(self.scope.terminate, identity["unit"], identity.get("cgroup"))
        except Exception:
            return False

    def _execution_diagnostic(self, run: dict, category: str, exc: Exception) -> None:
        diagnostics = run["results"].setdefault("execution_diagnostics", {})
        name = type(exc).__name__
        detail = {"observed_at": utcnow(), "category": category,
                  "exception_type": name[:80] if re.fullmatch(r"[A-Za-z0-9_]+", name) else "Exception"}
        if isinstance(exc, ScopeInspectionError):
            detail["control"] = dict(exc.diagnostic)
        diagnostics["failure_count"] = diagnostics.get("failure_count", 0) + 1
        recent = diagnostics.setdefault("recent_failures", [])
        recent.append(detail)
        del recent[:-self.MONITOR_HISTORY_LIMIT]

    def _monitor_failure(self, code: str) -> None:
        self.monitor_degraded = True
        if self.fatal is None:
            self.fatal = code

    async def _stage(self, record: dict, stage: str) -> bool:
        request = SubmitRequest.model_validate(record["request"])
        snapshot = record["snapshot"]
        stage_timeout, job_timeout = self._time_limits(record)
        directory = self._directory(request)
        work_dir = directory / "science"
        work_dir.mkdir(mode=0o700, exist_ok=True)
        existing = record["completed_stages"].get(stage)
        if existing:
            try:
                self._restore_checkpoints(record, work_dir, directory)
            except (ValueError, OSError, KeyError):
                self._finish(record, "failed", error={"code": "checkpoint_integrity_mismatch", "stage": stage})
                return False
            return True
        previous = [s for s in snapshot["stage_runs"] if s["stage"] == stage]
        for attempt in range(len(previous) + 1, 3):
            if self.stopping:
                return False
            if self.cancel_events[request.job_id].is_set():
                self._finish(record, "cancelled")
                return False
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(snapshot["started_at"])).total_seconds()
            if elapsed >= job_timeout:
                self._finish(record, "failed", error={"code": "job_timeout", "retryable": False})
                return False
            try:
                self._stage_input(record, stage, work_dir, directory)
            except (ValueError, OSError, KeyError):
                self._finish(record, "failed", error={"code": "checkpoint_integrity_mismatch", "stage": stage})
                return False
            stage_id = f"{stage}-{attempt}"
            run = StageRun(stage_run_id=stage_id, stage=stage, attempt=attempt, status="running", started_at=utcnow()).model_dump(mode="json")
            run["results"]["resource_limits"] = deepcopy(snapshot["results"].get("resource_limits", {}))
            run["results"]["worker_release_digest"] = self.release_digest
            snapshot["stage_runs"].append(run)
            snapshot["stage"] = stage
            snapshot["results"].pop("current_step", None)
            snapshot["results"].pop("total_steps", None)
            stage_dir = directory / "stages" / stage_id
            stage_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            request_path, result_path = stage_dir / "request.json", stage_dir / "result.json"
            atomic_json(request_path, request.scientific_request.model_dump(mode="json"))
            unit = f"nexpoly-polymer-md-{request.job_id.replace('-', '')}-{request.attempt_token[:12]}-{stage_id}.scope"
            record["running_scope"] = {"unit": unit, "cgroup": None, "stage_run_id": stage_id,
                                       "launch_intent_at": utcnow(), "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
            if self.execution_gate:
                record["running_scope"].update(launch_protocol="gated-v1", nonce=uuid4().hex,
                    gate_path=str(stage_dir / "execution-gate.json"), entry_path=str(stage_dir / "execution-entry.json"))
            self._save(record)  # identity/authorization are durable before spawn
            process = None
            outcome = None
            inspection = None
            outage_started = None
            try:
                log_path = stage_dir / "runner.log"
                with log_path.open("ab") as log:
                    command = self.command_builder(request_path, stage, work_dir, result_path)
                    if self.execution_gate:
                        identity = record["running_scope"]
                        command = [sys.executable, "-m", "workers.polymer_md_worker.app.execution_gate",
                            "--identity", identity["entry_path"], "--gate", identity["gate_path"],
                            "--unit", unit, "--nonce", identity["nonce"], "--expires", str(time.time() + 60), "--", *command]
                    process = self.scope.launch(unit=unit, command=command,
                        cwd=work_dir, log=log, environment=scientific_environment(), cpu_count=16, memory_bytes=32 * 1024**3)
                    record["running_scope"]["launcher_pid"] = process.pid
                    record["running_scope"]["launcher_start_ticks"] = process_start_ticks(process.pid)
                    self._save(record)
                    start = time.monotonic()
                    while process.poll() is None:
                        if self.cancel_events[request.job_id].is_set():
                            outcome = "cancelled"
                            break
                        if self.stopping:
                            outcome = "interrupted"
                            break
                        total = (datetime.now(timezone.utc) - datetime.fromisoformat(snapshot["started_at"])).total_seconds()
                        if time.monotonic() - start >= stage_timeout or total >= job_timeout:
                            outcome = "timeout"
                            break
                        if self._job_size(directory) > self.settings.max_job_bytes:
                            outcome = "storage_quota_exceeded"
                            break
                        if outage_started is not None and time.monotonic() - outage_started >= self.MONITOR_GRACE_SECONDS:
                            outcome = "scope_monitor_unavailable"
                            self._monitor_failure(outcome)
                            break
                        # A read-only systemctl call must not suspend cancellation
                        # or extend the original execution/time/storage budgets.
                        if inspection is None:
                            inspection = asyncio.create_task(asyncio.to_thread(self.scope.inspect, unit))
                        remaining = min(stage_timeout - (time.monotonic() - start), job_timeout - total)
                        if outage_started is not None:
                            remaining = min(remaining, self.MONITOR_GRACE_SECONDS - (time.monotonic() - outage_started))
                        done, _ = await asyncio.wait({inspection}, timeout=max(0, min(self.settings.poll_seconds, remaining)))
                        if not done:
                            self._read_progress(record, run, work_dir)
                            continue
                        completed_inspection, inspection = inspection, None
                        try:
                            state = completed_inspection.result()
                            identity = record["running_scope"]
                            if (identity.get("gate_granted") and identity.get("cgroup")
                                    and not state.get("ControlGroup") and process.poll() is None):
                                # A reexecuting manager can temporarily lose its
                                # unit view while the pinned kernel scope lives.
                                raise ScopeInspectionError(exception_type="IncompleteScopeResponse",
                                    stderr="unit not found" if state.get("LoadState") == "not-found" else "")
                        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                            self._execution_diagnostic(run, "scope_inspection_failed", exc)
                            if process.poll() is not None:
                                break
                            self.monitor_degraded = True
                            identity = record["running_scope"]
                            # The entry must already be behind the granted gate,
                            # in the same pinned kernel cgroup with its fixed
                            # CPU/memory/task limits. Startup never gets a grace.
                            bounded = (self.execution_gate and identity.get("gate_granted")
                                       and await asyncio.to_thread(self.scope.workload_is_bounded, identity))
                            run["results"]["execution_diagnostics"]["local_workload_confirmed"] = bool(bounded)
                            if not bounded:
                                outcome = "scope_monitor_unavailable"
                                self._monitor_failure(outcome)
                                self._save(record)
                                break
                            if outage_started is None:
                                outage_started = time.monotonic()
                                run["results"]["execution_diagnostics"]["outage_started_at"] = utcnow()
                                run["results"]["execution_diagnostics"]["grace_seconds"] = self.MONITOR_GRACE_SECONDS
                            self._save(record)
                            self._read_progress(record, run, work_dir)
                            await asyncio.sleep(min(self.settings.poll_seconds, max(0, remaining)))
                            continue
                        if process.poll() is not None:
                            break
                        identity = record["running_scope"]
                        if identity.get("cgroup") and state.get("ControlGroup") != identity["cgroup"]:
                            raise ScopeIdentityError("systemd scope membership changed")
                        if outage_started is not None:
                            if not await asyncio.to_thread(self.scope.workload_is_bounded, identity):
                                raise ScopeIdentityError("workload identity or budget changed during monitoring outage")
                            outage_started = None
                            self.monitor_degraded = False
                            run["results"]["execution_diagnostics"]["recovered_at"] = utcnow()
                            self._save(record)
                        if state.get("ControlGroup") and not record["running_scope"].get("cgroup"):
                            record["running_scope"]["cgroup"] = state["ControlGroup"]
                            self._save(record)
                        if self.execution_gate and not record["running_scope"].get("gate_granted"):
                            identity = record["running_scope"]
                            entry_path = Path(identity["entry_path"])
                            if identity.get("cgroup") and entry_path.is_file():
                                entry = read_json(entry_path)
                                if (entry.get("unit") != unit or entry.get("nonce") != identity["nonce"]
                                        or process_start_ticks(entry["pid"]) != entry.get("start_ticks")):
                                    raise ValueError("invalid execution entry identity")
                                memberships = Path(f"/proc/{entry['pid']}/cgroup").read_text().splitlines()
                                if f"0::{identity['cgroup']}" not in memberships:
                                    raise ValueError("execution entry is outside owned cgroup")
                                identity.update(entry_pid=entry["pid"], entry_start_ticks=entry["start_ticks"])
                                identity["cgroup_identity"] = await asyncio.to_thread(self.scope.capture_workload_identity, identity)
                                identity["gate_granted"] = True
                                self._save(record)  # Scope membership is durable before computation is released.
                                atomic_json(Path(identity["gate_path"]), {**entry, "allowed": True})
                        self._read_progress(record, run, work_dir)
                        await asyncio.sleep(self.settings.poll_seconds)
                    if outcome is None and process.returncode != 0:
                        outcome = "engine_process_failed"
            except Exception as exc:
                outcome = "scope_identity_unconfirmed" if isinstance(exc, ScopeIdentityError) else "stage_supervisor_failed"
                self._execution_diagnostic(run, outcome, exc)
                self._monitor_failure(outcome)
                if self.execution_gate and process is None:
                    record["running_scope"]["launch_failed_before_spawn"] = True
            finally:
                if inspection is not None:
                    # Cancelling the waiter does not interrupt systemctl's
                    # read-only thread; its own two-second bound still applies.
                    if inspection.done() and not inspection.cancelled():
                        inspection.exception()
                    else:
                        inspection.cancel()
            diagnostics = run["results"].setdefault("execution_diagnostics", {})
            diagnostics["process_returncode_before_cleanup"] = process.poll() if process else None
            if not await self._terminate(record["running_scope"]):
                self._hold(record, "stage_cleanup_unconfirmed")
                return False
            if process:
                try:
                    await asyncio.to_thread(process.wait, timeout=5)
                except subprocess.SubprocessError as exc:
                    self._execution_diagnostic(run, "launcher_cleanup_unconfirmed", exc)
                    self._hold(record, "launcher_cleanup_unconfirmed")
                    return False
                diagnostics["process_returncode_after_cleanup"] = process.returncode
            record["running_scope"] = None
            if self.fatal is None:
                # Cleanup itself has now confirmed control authority and an
                # empty scope, including when the engine exited in the grace.
                self.monitor_degraded = False
            run["finished_at"] = utcnow()
            self._publish_runner_log(record, run)
            if outcome is not None:
                self._capture_failed_diagnostics(record, run)
            if outcome == "cancelled":
                run["status"] = "cancelled"
                self._finish(record, "cancelled")
                return False
            if outcome == "interrupted":
                run["status"] = "failed"
                run["errors"].append({"code": "worker_interrupted", "retryable": True})
                record["resume_pending"] = True
                self._save(record)
                return False
            if outcome is None:
                try:
                    result = read_json(result_path)
                    self._accept_stage(record, run, result, work_dir, directory)
                    self._save(record)
                    if result.get("quality_status") == "failed":
                        self._finish(record, "completed", quality_status="failed")
                        return False
                    return True
                except Exception as exc:
                    outcome = "stage_result_invalid"
                    self._execution_diagnostic(run, outcome, exc)
                    self._capture_failed_diagnostics(record, run)
            run["status"] = "failed"
            monitor_failure = outcome in {"scope_monitor_unavailable", "scope_identity_unconfirmed", "stage_supervisor_failed"}
            run["errors"].append({"code": outcome, "retryable": attempt == 1 and not monitor_failure})
            self._save(record)
            if outcome in {"timeout", "storage_quota_exceeded"} or monitor_failure:
                self._finish(record, "failed", error={"code": outcome, "stage": stage, "retryable": False})
                return False
        self._finish(record, "failed", error={"code": "stage_retries_exhausted", "stage": stage, "retryable": False})
        return False

    def _publish_runner_log(self, record, run):
        request = SubmitRequest.model_validate(record["request"])
        directory = self._directory(request)
        relative = f"stages/{run['stage_run_id']}/runner.log"
        artifact_id = f"{run['stage_run_id']}-runner-log"
        if artifact_id in record["artifact_paths"] or not (directory / relative).is_file():
            return
        path = safe_file(self.settings.job_root, f"{request.job_id}/{request.attempt_token}/{relative}")
        descriptor = Artifact(artifact_id=artifact_id, stage_run_id=run["stage_run_id"], filename="runner.log",
            kind="log", media_type="text/plain", size_bytes=path.stat().st_size, sha256=sha256_file(path),
            storage_ref=f"{request.job_id}/{request.attempt_token}/{relative}").model_dump(mode="json")
        record["snapshot"]["artifacts"].append(descriptor)
        record["artifact_paths"][artifact_id] = relative

    def _capture_failed_diagnostics(self, record, run):
        """Freeze bounded, stage-local evidence only after proving the scope empty."""
        if record.get("running_scope"):
            raise RuntimeError("cannot preserve scientific files before execution cleanup")
        if run["results"].get("failure_diagnostics", {}).get("capture_complete"):
            return
        request = SubmitRequest.model_validate(record["request"])
        directory = self._directory(request)
        work = directory / "science"
        source_dir = work / run["stage"]
        report = {"capture_complete": True, "files": [], "skipped": [], "skipped_count": 0, "retained_bytes": 0,
                  "limits": {"log_file_bytes": self.FAILURE_LOG_BYTES, "input_file_bytes": self.FAILURE_INPUT_BYTES,
                      "stage_bytes": self.FAILURE_STAGE_BYTES, "job_bytes": self.FAILURE_JOB_BYTES,
                      "stage_files": self.FAILURE_STAGE_FILES}}
        run["results"]["failure_diagnostics"] = report
        def skip(reason, filename=None):
            report["skipped_count"] += 1
            if len(report["skipped"]) < 64:
                report["skipped"].append({"reason": reason, **({"filename": filename} if filename else {})})
        try:
            safe_file(self.settings.job_root, f"{request.job_id}/{request.attempt_token}/journal.json")
        except (ValueError, OSError):
            skip("unsafe_attempt_owner_directory")
            return
        if not source_dir.exists():
            return
        if work.is_symlink() or source_dir.is_symlink() or not source_dir.is_dir():
            skip("unsafe_scientific_directory")
            return
        used = sum(a["size_bytes"] for a in record["snapshot"]["artifacts"] if a["kind"] in {"failure_log", "failure_input"})
        try:
            remaining = min(self.FAILURE_STAGE_BYTES, max(0, self.FAILURE_JOB_BYTES - used),
                            max(0, self.settings.max_job_bytes - self._job_size(directory)))
        except (ValueError, OSError):
            skip("unsafe_job_directory_or_storage_unavailable")
            return
        candidates = []
        referenced_data = set()
        paths = []
        for parent, directories, filenames in os.walk(source_dir, followlinks=False):
            depth = len(Path(parent).relative_to(source_dir).parts)
            directories[:] = sorted(name for name in directories if depth < 4
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", name)
                and not (Path(parent) / name).is_symlink())
            for name in sorted(filenames):
                paths.append(Path(parent) / name)
                if len(paths) >= 4096:
                    skip("diagnostic_scan_limit")
                    break
            if len(paths) >= 4096:
                break
        for path in paths:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", path.name):
                continue
            if path.suffix == ".in":
                try:
                    source = safe_file(self.settings.job_root, str(path.relative_to(self.settings.job_root)))
                    if source.stat().st_size > self.FAILURE_INPUT_BYTES:
                        skip("input_file_limit", path.name)
                        continue
                    text = source.read_text(encoding="utf-8")
                    if "\x00" not in text and re.search(r"(?m)^\s*units\s+real\s*(?:#.*)?$", text) and re.search(r"(?m)^\s*read_data\s+", text):
                        candidates.append((path, "failure_input"))
                        variables = dict(re.findall(r"(?m)^\s*variable\s+([A-Za-z0-9_]+)\s+string\s+([^\s#]+)", text))
                        for value in re.findall(r"(?m)^\s*read_data\s+([^\s#]+)", text):
                            value = re.sub(r"\$\{([A-Za-z0-9_]+)\}", lambda match: variables.get(match[1], match[0]), value).strip("\"'")
                            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}\.data", value):
                                referenced_data.add(path.parent / value)
                except (ValueError, OSError, UnicodeError):
                    skip("unsafe_or_invalid_input", path.name)
            elif path.suffix in {".log", ".stdout", ".out"}:
                candidates.append((path, "failure_log"))
        # Only stage-local read_data targets of actual generated inputs qualify.
        # Unreferenced last data, trajectories, restarts and checkpoints are excluded.
        candidates.extend((path, "failure_input") for path in referenced_data if path.exists())
        def priority(item):
            path, kind = item
            first = {"engine.stdout": 0, "engine.log": 1, "input.in": 2, "input.data": 3}.get(path.name)
            return (first if first is not None else 4 if kind == "failure_input" else 5, str(path.relative_to(source_dir)))
        for path, kind in sorted(candidates, key=priority):
            if len(report["files"]) >= self.FAILURE_STAGE_FILES or len(record["snapshot"]["artifacts"]) >= 500 or remaining <= 0:
                skip("diagnostic_or_job_budget", path.name)
                continue
            try:
                source = safe_file(self.settings.job_root, str(path.relative_to(self.settings.job_root)))
                size = source.stat().st_size
                limit = self.FAILURE_LOG_BYTES if kind == "failure_log" else self.FAILURE_INPUT_BYTES
                if kind == "failure_input" and size > min(limit, remaining):
                    skip("complete_input_exceeds_budget", path.name)
                    continue
                retained = min(size, limit, remaining)
                truncated = retained < size
                tag = hashlib.sha256(str(path.relative_to(source_dir)).encode()).hexdigest()[:12]
                filename = path.name[:220] + ".tail.log" if truncated else path.name
                relative = f"stages/{run['stage_run_id']}/failure-diagnostics/{tag}/{filename}"
                target = directory / relative
                parent = self.settings.job_root
                storage_ref = f"{request.job_id}/{request.attempt_token}/{relative}"
                for component in Path(storage_ref).parts[:-1]:
                    parent /= component
                    if parent.is_symlink():
                        raise ValueError("unsafe diagnostic destination")
                    parent.mkdir(mode=0o700, exist_ok=True)
                if target.is_symlink():
                    raise ValueError("unsafe diagnostic destination")
                temporary = parent / (".capture-" + uuid4().hex)
                try:
                    with source.open("rb") as original, temporary.open("xb") as output:
                        os.fchmod(output.fileno(), 0o600)
                        before = os.fstat(original.fileno())
                        if not stat.S_ISREG(before.st_mode):
                            raise ValueError("diagnostic source must be a regular file")
                        original.seek(max(0, before.st_size - retained) if truncated else 0)
                        pending = retained
                        while pending:
                            chunk = original.read(min(pending, 1024 * 1024))
                            if not chunk:
                                raise ValueError("diagnostic source changed")
                            output.write(chunk)
                            pending -= len(chunk)
                        after = os.fstat(original.fileno())
                        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                            raise ValueError("diagnostic source changed")
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, target)
                    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(parent_fd)
                    finally:
                        os.close(parent_fd)
                finally:
                    temporary.unlink(missing_ok=True)
                artifact_id = f"{run['stage_run_id']}-failure-{tag}"
                artifact = Artifact(artifact_id=artifact_id, stage_run_id=run["stage_run_id"], filename=filename,
                    kind=kind, media_type="text/plain", size_bytes=target.stat().st_size, sha256=sha256_file(target),
                    storage_ref=storage_ref).model_dump(mode="json")
                record["snapshot"]["artifacts"].append(artifact)
                record["artifact_paths"][artifact_id] = relative
                report["files"].append({"filename": path.name, "artifact_id": artifact_id, "source_size_bytes": size,
                                        "retained_bytes": retained, "truncated": truncated})
                report["retained_bytes"] += retained
                remaining -= retained
            except (ValueError, OSError):
                skip("unsafe_unstable_or_unreadable_source", path.name)

    def _read_progress(self, record, run, work_dir):
        path = work_dir / "progress.json"
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 16384:
            return
        try:
            data = read_json(path)
            if data.get("stage") != run["stage"]:
                return
            current, total = data.get("current_step"), data.get("total_steps")
            if not isinstance(current, int) or isinstance(current, bool) or not isinstance(total, int) or isinstance(total, bool) or not 0 <= current <= total or total <= 0:
                return
            progress = {"current_step": current, "total_steps": total}
            if run["results"].get("progress") != progress:
                run["results"]["progress"] = progress
                record["snapshot"]["results"].update(progress)
                self._save(record)
        except (ValueError, OSError, TypeError):
            return

    @staticmethod
    def _job_size(directory: Path) -> int:
        def walk_error(error):
            if not isinstance(error, FileNotFoundError):
                raise error

        total = 0
        for parent, directories, filenames in os.walk(directory, followlinks=False, onerror=walk_error):
            for names, count_bytes in ((directories, False), (filenames, True)):
                for name in names:
                    try:
                        metadata = (Path(parent) / name).lstat()
                    except FileNotFoundError:
                        # Psi4 routinely removes scratch while the engine runs.
                        # The next quota scan observes the then-current tree.
                        continue
                    if stat.S_ISLNK(metadata.st_mode):
                        raise ValueError("symbolic links are not allowed in scientific workdir")
                    if count_bytes:
                        total += metadata.st_size
        return total

    def _accept_stage(self, record, run, result, work_dir, directory):
        if not isinstance(result, dict) or result.get("stage") != run["stage"]:
            raise ValueError("stage result identity mismatch")
        quality = result.get("quality_status")
        if quality not in {"not_evaluated", "passed", "warning", "not_converged", "failed"}:
            raise ValueError("stage result quality missing")
        # All stage data must be JSON finite; never publish mock/fabricated values.
        json.dumps(result, allow_nan=False)
        checkpoint = result.get("checkpoint")
        if not checkpoint:
            raise ValueError("scientific stage requires a checkpoint")
        source = safe_file(work_dir, checkpoint["path"])
        if sha256_file(source) != checkpoint["sha256"]:
            raise ValueError("checkpoint digest mismatch")
        frozen_relative = f"checkpoints/{run['stage_run_id']}/{source.name}"
        target = restore_file(directory, frozen_relative, source)
        checkpoint = {"path": str(target.relative_to(directory)), "sha256": checkpoint["sha256"]}
        artifacts, paths = [], {}
        request = SubmitRequest.model_validate(record["request"])
        for index, item in enumerate(result.get("artifacts", [])):
            source = safe_file(work_dir, item["path"])
            artifact_id = f"{run['stage_run_id']}-{index}"
            relative = str(source.relative_to(directory))
            artifact = Artifact(artifact_id=artifact_id, stage_run_id=run["stage_run_id"],
                filename=source.name, kind=item.get("kind", "data"), media_type=item.get("media_type", "application/octet-stream"),
                storage_ref=f"{request.job_id}/{request.attempt_token}/{relative}",
                size_bytes=source.stat().st_size, sha256=sha256_file(source)).model_dump(mode="json")
            artifacts.append(artifact)
            paths[artifact_id] = relative
        payload = result.get("results", result)
        if not isinstance(payload, dict):
            raise ValueError("scientific stage results must be an object")
        public_results = self._public_results(payload)
        if run["results"].get("resource_limits"):
            public_results["resource_limits"] = deepcopy(run["results"]["resource_limits"])
        if run["results"].get("worker_release_digest"):
            public_results["worker_release_digest"] = run["results"]["worker_release_digest"]
        if run["results"].get("execution_diagnostics"):
            public_results["execution_diagnostics"] = deepcopy(run["results"]["execution_diagnostics"])
        if payload.get("trajectory"):
            public_results["trajectory_available"] = True
        quality_details = self._public_results(payload.get("quality", {}))
        validated_run = StageRun.model_validate({**run, "status": "completed", "quality_status": quality,
            "results": public_results, "quality": {**quality_details, "equilibrated": result.get("equilibrated"), "tc_eligible": result.get("tc_eligible")}}).model_dump(mode="json")
        candidate = deepcopy(record["snapshot"])
        candidate["stage_runs"] = [validated_run if old["stage_run_id"] == run["stage_run_id"] else old
                                   for old in record["snapshot"]["stage_runs"]]
        candidate["artifacts"] = record["snapshot"]["artifacts"] + artifacts
        self._publish_stage_results({"request": record["request"], "snapshot": candidate},
                                    validated_run, result, public_results)
        WorkerSnapshot.model_validate(candidate)
        run.update(validated_run)
        record["snapshot"]["artifacts"].extend(artifacts)
        record["snapshot"]["results"] = candidate["results"]
        record["snapshot"]["quality_status"] = candidate["quality_status"]
        record["artifact_paths"].update(paths)
        result["stage_run_id"] = run["stage_run_id"]
        record["completed_stages"][run["stage"]] = {"stage_run_id": run["stage_run_id"], "result": result, "checkpoint": checkpoint}

    @staticmethod
    def _combined_quality(*qualities):
        order = {"not_evaluated": 0, "passed": 1, "warning": 2, "not_converged": 3, "failed": 4}
        return max(qualities, key=lambda value: order[value])

    def _publish_stage_results(self, record, run, result, public_results):
        """Publish useful observations after each stage, even if a later stage fails."""
        snapshot = record["snapshot"]
        summary = snapshot["results"]
        native = record.get("request", {}).get("scientific_request", {}).get("native_configuration")
        if native:
            selected = set(native["properties"])
            public_results = deepcopy(public_results)
            public_results["properties"] = [item for item in public_results.get("properties", []) if item.get("key") in selected]
            summary["native_configuration"] = native
        summary.pop("current_step", None)
        summary.pop("total_steps", None)
        if run["stage"] == "build":
            summary["model"] = deepcopy(public_results)
        category = ("basic" if run["stage"].startswith("analyze_") else
                    {"tc_analyze": "thermal", "sp_analyze": "sp", "tg_analyze": "glass_transition"}.get(run["stage"]))
        if category == "basic":
            # A new production segment supersedes the preceding analysis as a
            # whole. Retaining earlier passed keys would mix physical windows
            # and hide a later warning or missing observation. Stage runs retain
            # every prior analysis; RNEMD below still adds to the current basic set.
            prior_qm = [item for item in summary.get("properties", []) if native and item.get("key", "").startswith("qm_")]
            summary["properties"] = self._merge_properties(prior_qm, public_results.get("properties", []))
            summary["series"] = self._merge_series([], public_results.get("series", []))
        else:
            if "properties" in public_results:
                summary["properties"] = self._merge_properties(summary.get("properties", []), public_results["properties"])
            if "series" in public_results:
                summary["series"] = self._merge_series(summary.get("series", []), public_results["series"])
        if category:
            quality = result["quality_status"]
            # Include reported property limitations, including optical approximations.
            if quality == "passed" and any(item.get("quality_status") != "passed" for item in public_results.get("properties", [])):
                quality = "warning"
            summary.setdefault("quality_summary", {})[category] = {
                **public_results.get("quality", {}), "stage_run_id": run["stage_run_id"], "quality_status": quality}
            if category == "basic":
                summary.update(equilibrated=result.get("equilibrated") is True,
                               tc_eligible=result.get("tc_eligible") is True)
            snapshot["quality_status"] = self._combined_quality(
                *(item["quality_status"] for item in summary["quality_summary"].values()))

    @staticmethod
    def _merge_series(before, incoming):
        values = {item["key"]: item for item in before if isinstance(item, dict) and "key" in item}
        for item in incoming:
            if not isinstance(item, dict) or "key" not in item:
                raise ValueError("series lacks a key")
            values[item["key"]] = item
        return list(values.values())

    @staticmethod
    def _merge_properties(before, incoming):
        values = {item["key"]: item for item in before if isinstance(item, dict) and "key" in item}
        for item in incoming:
            if not isinstance(item, dict) or "key" not in item:
                raise ValueError("property lacks a key")
            previous = values.get(item["key"])
            if previous and previous.get("quality_status") == "passed" and item.get("quality_status") != "passed":
                continue
            values[item["key"]] = item
        return list(values.values())

    @classmethod
    def _public_results(cls, value):
        if isinstance(value, dict):
            return {key: cls._public_results(item) for key, item in value.items()
                    if key not in {"trajectory", "checkpoint", "artifacts", "path", "file", "filename", "work_dir", "directory", "storage_ref"}
                    and not key.endswith(("_path", "_file", "_dir"))}
        if isinstance(value, list):
            return [cls._public_results(item) for item in value]
        if isinstance(value, str) and value.startswith(("/", "file://", "unix://")):
            return "[private]"
        return value

    async def acquire_read(self, job_id):
        async with self.lock:
            record = self.records.get(job_id)
            if not record:
                raise WorkerError("job_not_found", "unknown job", 404)
            if record.get("artifacts_deleting") or record.get("artifacts_deleted") or job_id in self._deleting:
                raise WorkerError("artifact_not_found", "artifacts have been deleted", 404)
            self.readers[job_id] = self.readers.get(job_id, 0) + 1

    async def release_read(self, job_id):
        async with self.lock:
            count = self.readers.get(job_id, 0)
            if count <= 1:
                self.readers.pop(job_id, None)
            else:
                self.readers[job_id] = count - 1

    async def analyze(self, job_id: str, stage_run_id: str, operation: str, *arguments):
        """One native reader plus one queued reader, independent of MD capacity.

        Own the read lease until the concurrent Future finishes. asyncio's
        cancellation of a wrapper Future cannot terminate a native thread.
        """
        loop = asyncio.get_running_loop()
        analysis_ready = await self._analysis_ready()
        async with self.lock:
            if self.stopping:
                raise WorkerError("worker_stopping", "worker is shutting down", 503)
            record = self.records.get(job_id)
            if not record:
                raise WorkerError("job_not_found", "unknown job", 404)
            if record.get("artifacts_deleting") or record.get("artifacts_deleted") or job_id in self._deleting:
                raise WorkerError("artifact_not_found", "artifacts have been deleted", 404)
            if not analysis_ready or self.recovering or self.fatal:
                raise WorkerError("trajectory_runtime_unavailable", "trajectory runtime is not ready", 503)
            if self.analysis_inflight >= 2:
                raise WorkerError("analysis_busy", "one analysis is running and one is already waiting", 429)
            self.analysis_inflight += 1
            self.analysis_drained.clear()
            self.readers[job_id] = self.readers.get(job_id, 0) + 1
            try:
                future = self.analysis_executor.submit(self._analyze_one, job_id, stage_run_id, operation, *arguments)
            except Exception:
                self.analysis_inflight -= 1
                self.readers[job_id] -= 1
                if not self.readers[job_id]:
                    self.readers.pop(job_id)
                if not self.analysis_inflight:
                    self.analysis_drained.set()
                raise WorkerError("analysis_unavailable", "analysis executor is unavailable", 503) from None

            def finished(_future):
                loop.call_soon_threadsafe(lambda: asyncio.create_task(self._analysis_finished(job_id)))

            future.add_done_callback(finished)
        wrapped = asyncio.wrap_future(future)
        # Consume an eventual error even after the HTTP waiter disconnects.
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            future.cancel()  # Only queued work is cancellable; running work retains its lease.
            raise

    async def _analysis_finished(self, job_id):
        async with self.lock:
            count = self.readers.get(job_id, 0)
            if count <= 1:
                self.readers.pop(job_id, None)
            else:
                self.readers[job_id] = count - 1
            self.analysis_inflight -= 1
            if not self.analysis_inflight:
                self.analysis_drained.set()

    def _analyze_one(self, job_id, stage_run_id, operation, *arguments):
        work_dir, result = self.stage_result(job_id, stage_run_id)
        try:
            from .scientific_analysis import read_trajectory, analyze_distance
        except ImportError as exc:
            raise WorkerError("trajectory_runtime_unavailable", "trajectory runtime is not ready", 503) from exc
        if operation == "trajectory":
            return read_trajectory(work_dir, result, *arguments)
        if operation == "distance":
            return analyze_distance(work_dir, result, *arguments)
        raise ValueError("unknown analysis operation")

    def artifact(self, job_id: str, artifact_id: str) -> tuple[Path, dict]:
        record = self.records.get(job_id)
        if not record:
            raise WorkerError("job_not_found", "unknown job", 404)
        descriptor = next((a for a in record["snapshot"]["artifacts"] if a["artifact_id"] == artifact_id), None)
        if not descriptor or record.get("artifacts_deleting"):
            raise WorkerError("artifact_not_found", "unknown artifact", 404)
        path = safe_file(self._directory(SubmitRequest.model_validate(record["request"])), record["artifact_paths"][artifact_id])
        if path.stat().st_size != descriptor["size_bytes"] or sha256_file(path) != descriptor["sha256"]:
            raise WorkerError("artifact_integrity_mismatch", "artifact content changed", 409)
        return path, descriptor

    def stage_result(self, job_id: str, stage_run_id: str) -> tuple[Path, dict]:
        record = self.records.get(job_id)
        if not record:
            raise WorkerError("job_not_found", "unknown job", 404)
        run = next((s for s in record["snapshot"]["stage_runs"] if s["stage_run_id"] == stage_run_id and s["status"] == "completed"), None)
        if not run:
            raise WorkerError("stage_not_found", "unknown completed stage", 404)
        directory = self._directory(SubmitRequest.model_validate(record["request"]))
        if record.get("artifacts_deleted") or record.get("artifacts_deleting"):
            raise WorkerError("artifact_not_found", "artifacts have been deleted", 404)
        original = record["completed_stages"][run["stage"]]["result"]
        metadata = original.get("results", original).get("trajectory")
        if not isinstance(metadata, dict) or not all(metadata.get(key) for key in ("xtc_file", "pdb_file")):
            raise WorkerError("trajectory_not_found", "completed stage has no trajectory", 404)
        for key, relative in metadata.items():
            if not key.endswith("_file"):
                continue
            try:
                source = safe_file(directory / "science", relative)
                registered = str(source.relative_to(directory))
                artifact_id = next((key for key, path in list(record["artifact_paths"].items()) if path == registered), None)
                if not artifact_id:
                    raise ValueError("trajectory source has no artifact manifest")
                self.artifact(job_id, artifact_id)
            except (ValueError, OSError):
                raise WorkerError("artifact_integrity_mismatch", "trajectory input is missing or changed", 409) from None
        return directory / "science", original

    def logs(self, job_id: str, stage_run_id: str, tail_lines: int) -> dict:
        record = self.records.get(job_id)
        if not record:
            raise WorkerError("job_not_found", "unknown job", 404)
        run = next((item for item in record["snapshot"]["stage_runs"] if item["stage_run_id"] == stage_run_id), None)
        if not run:
            raise WorkerError("stage_not_found", "unknown stage", 404)
        if not 1 <= tail_lines <= 1000:
            raise ValueError("invalid log tail length")
        if record.get("artifacts_deleted") or record.get("artifacts_deleting"):
            raise WorkerError("artifact_not_found", "stage logs have been deleted", 404)
        result = {"stage_run_id": stage_run_id, "stage": run["stage"], "status": run["status"],
                  "available": False, "lines": [], "truncated": False, "tail_lines": tail_lines, "byte_limit": 262144}
        directory = self._directory(SubmitRequest.model_validate(record["request"]))
        relative = f"stages/{stage_run_id}/runner.log"
        if not (directory / relative).exists():
            return result
        path = safe_file(directory, relative)
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            start = max(0, size - result["byte_limit"])
            stream.seek(start)
            payload = stream.read(result["byte_limit"])
        lines = payload.decode("utf-8", errors="replace").splitlines()
        if start and lines:
            lines = lines[1:]  # Never present a partial first line as complete output.
        result.update(available=True, truncated=bool(start or len(lines) > tail_lines))
        private_roots = {str(directory), str(self.settings.job_root), str(Path(__file__).resolve().parents[3])}
        cleaned = []
        for line in lines[-tail_lines:]:
            for root in sorted(private_roots, key=len, reverse=True):
                line = line.replace(root, "[private]")
            line = line.replace(record["request"]["attempt_token"], "[private]")
            # Tracebacks and third-party engines may emit their installation paths.
            line = re.sub(r"(?<![A-Za-z0-9])/(?:[^\s\"'<>]+)", "[private]", line)
            line = re.sub(r"(?:\[private\]){2,}", "[private]", line)
            cleaned.append(line)
        result["lines"] = cleaned
        return result

    def _finish_artifact_delete(self, record):
        directory = self._directory(SubmitRequest.model_validate(record["request"]))
        # Remove science, logs and checkpoints only after exact task cleanup.
        for name in ("science", "stages", "checkpoints"):
            path = directory / name
            if path.is_symlink():
                raise RuntimeError("unsafe cleanup directory")
            shutil.rmtree(path, ignore_errors=False) if path.exists() else None
        record["snapshot"]["artifacts"] = []
        record["artifact_paths"] = {}
        record["artifacts_deleting"] = False
        record["artifacts_deleted"] = True
        self._save(record)

    async def delete(self, job_id: str, request: SubmitRequest, *, purge=False) -> dict:
        async with self.lock:
            record = self.records.get(job_id)
            if not record:
                tombstone = self.settings.job_root / ".tombstones" / f"{job_id}.json"
                if tombstone.exists() and read_json(tombstone) == self._fence(request):
                    return {"protocol_version": 1, **self._fence(request), "deleted": True, "cleanup_confirmed": True}
                raise WorkerError("job_not_found", "unknown job", 404)
            self._same(record, request)
            if self.readers.get(job_id):
                raise WorkerError("artifact_in_use", "artifact download or analysis is in progress")
            if record["snapshot"]["status"] not in TERMINAL_STATUSES or not record["snapshot"]["cleanup_confirmed"]:
                raise WorkerError("job_active", "active or unconfirmed job cannot be deleted")
            record["artifacts_deleting"] = True
            self._save(record)
            self._finish_artifact_delete(record)
            if purge:
                atomic_json(self.settings.job_root / ".tombstones" / f"{job_id}.json", self._fence(request))
                record["purging"] = True
                self._save(record)
                self._deleting.add(job_id)
                shutil.rmtree(self.settings.job_root / job_id)
                del self.records[job_id]
                del self.cancel_events[job_id]
                self._deleting.discard(job_id)
            return {"protocol_version": 1, "job_id": job_id, "attempt_token": request.attempt_token,
                    "request_sha256": request.request_sha256, "enqueue_sequence": request.enqueue_sequence,
                    "cleanup_confirmed": True, "deleted": True}

    @staticmethod
    def _fence(request):
        return {key: getattr(request, key) for key in ("job_id", "attempt_token", "request_sha256", "enqueue_sequence")}
