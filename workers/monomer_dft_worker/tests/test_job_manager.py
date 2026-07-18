from __future__ import annotations

import asyncio
import io
import json
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from workers.monomer_dft_worker.app import artifacts as artifacts_module
from workers.monomer_dft_worker.app import job_manager as job_manager_module
from workers.monomer_dft_worker.app.artifacts import (
    BUNDLE_CREATING_NAME,
    atomic_write_bytes,
    atomic_write_json,
    build_bundle,
    describe_artifact,
    open_readonly_regular,
)
from workers.monomer_dft_worker.app.engine import (
    ComputationCancelled,
    EngineExecution,
    ScientificComputationError,
)
from workers.monomer_dft_worker.app.job_manager import (
    ArtifactDeletionFailed,
    ArtifactNotFound,
    JobConflict,
    JobManager,
    JournalPersistenceError,
    JobNotFound,
    QueueFull,
    WorkerUnavailable,
)
from workers.monomer_dft_worker.app.runtime import RuntimeProbe
from workers.monomer_dft_worker.app.schemas import (
    MAX_ARTIFACT_SIZE_BYTES,
    ArtifactDescriptor,
    JobJournalV2,
    JobSubmitRequest,
)


class ReadyRuntime:
    def __init__(self) -> None:
        self.empty_cache_calls = 0
        self.settings = SimpleNamespace(physical_gpu="3")

    def probe(self) -> RuntimeProbe:
        return RuntimeProbe(
            ready=True,
            model_loaded=True,
            model_name="aimnet2",
            model_file="model.pt",
            model_sha256="f" * 64,
            aimnet_origin="site-packages/aimnet/__init__.py",
            torch_version="2.9.1+cu128",
            cuda_runtime="12.8",
            gpu_name="Fake RTX 4090",
            visible_gpu_count=1,
            logical_device="cuda:0",
            loaded_at_unix=1.0,
            error=None,
            models={
                "aimnet2": {
                    "loaded": True,
                    "registry_key": "aimnet2-wb97m-d3_0",
                    "family": "wb97m-d3",
                    "sha256": "f" * 64,
                }
            },
            aimnet_version="test",
            aimnet_commit="9" * 40,
            aimnet_wheel_sha256="e" * 64,
            warp_version="1.11.0",
        )

    def empty_cuda_cache(self) -> None:
        self.empty_cache_calls += 1


class SafeFatalRuntime(ReadyRuntime):
    def fatal_restart_safe(self) -> bool:
        return True


class InjectedOSErrorJournalWriter:
    def __init__(self, predicate: Callable[[dict[str, Any]], bool]) -> None:
        self.predicate = predicate
        self.failed = False

    def __call__(self, path: Path, payload: dict[str, Any]) -> None:
        if not self.failed and self.predicate(payload):
            self.failed = True
            raise OSError("injected journal persistence failure")
        atomic_write_json(path, payload)


def _request(
    index: int,
    *,
    token: str | None = None,
    sequence: int | None = None,
) -> JobSubmitRequest:
    return JobSubmitRequest(
        schema_version=2,
        enqueue_sequence=sequence if sequence is not None else index + 1,
        job_id=f"job-{index}",
        attempt_token=token or f"{index + 1:032x}",
        input={"smiles": "O", "net_charge": 0, "multiplicity": 1},
        calculation_type="single_point",
        model="aimnet2",
        conformer={"seed": index, "max_iterations": 20},
        single_point={"properties": ["energy"]},
    )


class ControlledEngine:
    def __init__(self, *, blocked: bool = True) -> None:
        self.release = threading.Event()
        if not blocked:
            self.release.set()
        self.started: list[str] = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def execute(
        self,
        request,
        output_directory,
        *,
        admitted,
        progress,
        cancelled,
        provenance,
        queue_wait_ms,
    ) -> EngineExecution:
        admitted()
        with self.lock:
            self.started.append(request.job_id)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            progress("single_point", 50, None)
            while not self.release.wait(0.005):
                if cancelled():
                    raise ComputationCancelled("cancelled")
            if cancelled():
                raise ComputationCancelled("cancelled")
            output_directory.mkdir(parents=True, exist_ok=True)
            path = output_directory / "result.json"
            atomic_write_bytes(path, b'{"ok":true}')
            descriptor = describe_artifact(
                artifact_id="scientific_result",
                path=path,
                media_type="application/json",
            )
            timings = {"queue_wait_ms": queue_wait_ms, "total_ms": 1.0}
            return EngineExecution(
                result={
                    "schema_version": 1,
                    "timings": timings,
                    "provenance": provenance,
                },
                timings=timings,
                artifacts=((descriptor, path),),
            )
        finally:
            with self.lock:
                self.active -= 1


class PartialArtifactEngine:
    def __init__(self, *, fail_immediately: bool) -> None:
        self.fail_immediately = fail_immediately
        self.started = threading.Event()

    def execute(self, request, output_directory, *, admitted, cancelled, **_kwargs):
        admitted()
        output_directory.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(output_directory / "partial.bin", b"partial")
        self.started.set()
        if self.fail_immediately:
            raise RuntimeError("synthetic failure")
        while not cancelled():
            time.sleep(0.005)
        raise ComputationCancelled("cancelled")


class FaultThenSuccessEngine:
    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.started: list[str] = []

    def execute(
        self,
        request,
        output_directory,
        *,
        admitted,
        progress,
        provenance,
        queue_wait_ms,
        **_kwargs,
    ) -> EngineExecution:
        self.started.append(request.job_id)
        admitted()
        progress("single_point", 50, None)
        if len(self.started) == 1:
            raise self.failure
        output_directory.mkdir(parents=True, exist_ok=True)
        path = output_directory / "result.json"
        atomic_write_bytes(path, b'{"ok":true}')
        descriptor = describe_artifact(
            artifact_id="scientific_result",
            path=path,
            media_type="application/json",
        )
        timings = {"queue_wait_ms": queue_wait_ms, "total_ms": 1.0}
        return EngineExecution(
            result={"schema_version": 1, "timings": timings, "provenance": provenance},
            timings=timings,
            artifacts=((descriptor, path),),
        )


class CapacityOutageEngine(ControlledEngine):
    def __init__(self) -> None:
        super().__init__(blocked=False)
        self.capacity_available = threading.Event()
        self.attempts: list[str] = []

    def execute(self, request, output_directory, **kwargs) -> EngineExecution:
        self.attempts.append(request.job_id)
        if not self.capacity_available.is_set():
            raise ScientificComputationError(
                "gpu_capacity_unavailable",
                "temporary Broker capacity outage",
                retryable=True,
            )
        return super().execute(request, output_directory, **kwargs)


class AdmissionGateEngine(ControlledEngine):
    def __init__(self) -> None:
        super().__init__(blocked=False)
        self.admission_gate = threading.Event()
        self.admission_attempted = threading.Event()

    def execute(
        self,
        request,
        output_directory,
        *,
        admitted,
        cancelled,
        **kwargs,
    ) -> EngineExecution:
        self.admission_attempted.set()
        while not self.admission_gate.wait(0.005):
            if cancelled():
                raise ComputationCancelled("GPU admission paused")
        return super().execute(
            request,
            output_directory,
            admitted=admitted,
            cancelled=cancelled,
            **kwargs,
        )


class TimingEngine(ControlledEngine):
    def __init__(self) -> None:
        super().__init__(blocked=False)

    def execute(self, request, output_directory, **kwargs) -> EngineExecution:
        execution = super().execute(request, output_directory, **kwargs)
        timings = dict(execution.timings)
        timings.update({"gpu_wait_ms": 777.0, "model_load_ms": 23.0})
        result = dict(execution.result)
        result["timings"] = dict(timings)
        return EngineExecution(
            result=result,
            timings=timings,
            artifacts=execution.artifacts,
        )


class TerminatingRuntime(ReadyRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.termination_reasons: list[str] = []

    def terminate_active(self, reason: str) -> bool:
        self.termination_reasons.append(reason)
        return True


class TimeoutThenSuccessEngine(ControlledEngine):
    def execute(
        self,
        request,
        output_directory,
        *,
        admitted,
        progress,
        cancelled,
        provenance,
        queue_wait_ms,
    ) -> EngineExecution:
        if not self.started:
            with self.lock:
                self.started.append(request.job_id)
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            admitted()
            progress("single_point", 50, None)
            try:
                while not cancelled():
                    time.sleep(0.005)
                raise ComputationCancelled("terminated after timeout")
            finally:
                with self.lock:
                    self.active -= 1
        return super().execute(
            request,
            output_directory,
            admitted=admitted,
            progress=progress,
            cancelled=cancelled,
            provenance=provenance,
            queue_wait_ms=queue_wait_ms,
        )


class AdmissionUncertainRuntime(ReadyRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.admission_uncertain = False


class ShutdownAdmissionEngine:
    def __init__(self, runtime: AdmissionUncertainRuntime) -> None:
        self.runtime = runtime
        self.started = threading.Event()
        self.attempts = 0

    def execute(self, _request, _output_directory, *, cancelled, **_kwargs):
        self.attempts += 1
        self.started.set()
        while not cancelled():
            time.sleep(0.005)
        self.runtime.admission_uncertain = True
        raise ComputationCancelled("admission ownership remained unresolved")


async def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.005)


def _manager(tmp_path: Path, engine, **kwargs) -> JobManager:
    return JobManager(
        job_root=tmp_path / "runs",
        engine=engine,
        runtime=kwargs.pop("runtime", ReadyRuntime()),
        worker_version="test",
        fatal_exit=kwargs.pop("fatal_exit", None),
        **kwargs,
    )


def test_fifo_capacity_one_running_plus_eight_queued_and_idempotency(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        engine = ControlledEngine()
        manager = _manager(tmp_path, engine)
        await manager.start()
        first, created = manager.submit(_request(0))
        assert created and first.status == "queued"
        await _wait_until(lambda: engine.started == ["job-0"])
        for index in range(1, 9):
            manager.submit(_request(index))
        with pytest.raises(QueueFull):
            manager.submit(_request(9))

        same, created = manager.submit(_request(0))
        assert created is False and same.status == "running"
        with pytest.raises(JobConflict):
            manager.submit(_request(0, token="b" * 32))
        assert [
            manager.get(f"job-{index}").queue_position for index in range(1, 9)
        ] == list(range(1, 9))

        engine.release.set()
        await _wait_until(
            lambda: all(manager.get(f"job-{i}").status == "completed" for i in range(9))
        )
        assert engine.started == [f"job-{index}" for index in range(9)]
        assert engine.max_active == 1
        provenance = manager.get("job-0").result["provenance"]
        assert provenance["aimnet_commit"] == "9" * 40
        assert provenance["aimnet_wheel_sha256"] == "e" * 64
        assert provenance["model_family"] == "wb97m-d3"
        assert provenance["gpu_physical_device"] == "3"
        await manager.stop()

    asyncio.run(scenario())


def test_gpu_capacity_outage_keeps_fifo_head_queued_cancelable_and_accumulates_wait(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        engine = CapacityOutageEngine()
        manager = _manager(tmp_path, engine)
        await manager.start()
        manager.submit(_request(0))
        manager.submit(_request(1))

        await _wait_until(lambda: len(engine.attempts) >= 2)
        head = manager.get("job-0")
        assert head.status == "queued"
        assert head.queue_position == 1
        assert manager.get("job-1").queue_position == 2
        assert manager.health_state()["active_jobs"] == 0
        journal = tmp_path / "runs" / "job-0" / f"{1:032x}" / "journal.json"
        assert JobJournalV2.model_validate_json(journal.read_bytes()).snapshot.status == "queued"

        engine.capacity_available.set()
        await _wait_until(lambda: manager.get("job-1").status == "completed")
        assert manager.get("job-0").timings["gpu_wait_ms"] >= 200.0
        await manager.stop()

        cancel_root = tmp_path / "cancel"
        cancel_engine = CapacityOutageEngine()
        cancel_manager = _manager(cancel_root, cancel_engine)
        await cancel_manager.start()
        cancel_manager.submit(_request(0))
        cancel_manager.submit(_request(1))
        await _wait_until(lambda: bool(cancel_engine.attempts))
        cancelled = cancel_manager.cancel("job-0")
        assert cancelled.status == "cancelled"
        await _wait_until(lambda: "job-1" in cancel_engine.attempts)
        assert cancel_manager.get("job-1").status == "queued"
        assert cancel_manager.get("job-1").queue_position == 1
        await cancel_manager.stop()

    asyncio.run(scenario())


def test_shutdown_during_unresolved_admission_is_bounded_and_preserves_fifo_head(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime = AdmissionUncertainRuntime()
        engine = ShutdownAdmissionEngine(runtime)
        manager = _manager(tmp_path, engine, runtime=runtime)
        await manager.start()
        manager.submit(_request(0))
        assert await asyncio.to_thread(engine.started.wait, 1.0)

        await asyncio.wait_for(manager.stop(), timeout=0.5)

        snapshot = manager.get("job-0")
        assert snapshot.status == "queued"
        assert snapshot.queue_position == 1
        assert engine.attempts == 1
        state = manager.health_state()
        assert state["fatal"] is True
        assert state["fatal_reason"] == "gpu_admission_uncertain"
        assert state["accepting_jobs"] is False
        with pytest.raises(WorkerUnavailable):
            manager.submit(_request(1))
        journal = tmp_path / "runs" / "job-0" / f"{1:032x}" / "journal.json"
        assert JobJournalV2.model_validate_json(journal.read_bytes()).snapshot.status == "queued"

    asyncio.run(scenario())


def test_idempotent_replay_precedes_scientific_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path, ControlledEngine())
    request = _request(0)
    submitted, created = manager.submit(request, chemistry_validated=True)
    assert created is True
    assert submitted.artifact_state == "none"

    validation_calls = 0

    def fail_if_revalidated(_request: JobSubmitRequest) -> None:
        nonlocal validation_calls
        validation_calls += 1
        raise AssertionError("an existing idempotent job must not be revalidated")

    monkeypatch.setattr(manager, "validate_submission", fail_if_revalidated)
    replayed, created = manager.submit(request)
    assert created is False
    assert replayed.job_id == request.job_id
    assert replayed.artifact_state == "none"
    assert validation_calls == 0

    with pytest.raises(JobConflict):
        manager.submit(_request(0, token="b" * 32))
    assert validation_calls == 0

    journal = JobJournalV2.model_validate_json(
        next((tmp_path / "runs").rglob("journal.json")).read_bytes()
    )
    assert "artifact_state" not in journal.snapshot.model_dump(mode="json")
    assert journal.artifact_state == "none"


def test_fenced_unknown_cancel_is_durable_and_late_submit_replays_cancelled(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        request = _request(0, sequence=41)
        engine = ControlledEngine(blocked=False)
        manager = _manager(tmp_path, engine)
        await manager.start()

        cancelled = manager.cancel(request.job_id, request)
        assert cancelled.status == "cancelled"
        assert cancelled.finished_at is not None
        assert cancelled.request == request
        assert cancelled.artifact_state == "none"
        assert manager.health_state()["queued_jobs"] == 0
        journal_path = (
            tmp_path
            / "runs"
            / request.job_id
            / request.attempt_token
            / "journal.json"
        )
        journal = JobJournalV2.model_validate_json(journal_path.read_bytes())
        assert journal.snapshot.status == "cancelled"
        assert journal.enqueue_sequence == 41

        repeated = manager.cancel(request.job_id, request)
        assert repeated == cancelled
        replayed, created = manager.submit(request)
        assert created is False
        assert replayed.status == "cancelled"
        assert engine.started == []
        await manager.stop()

        recovered_engine = ControlledEngine(blocked=False)
        recovered = _manager(tmp_path, recovered_engine)
        await recovered.start()
        restored = recovered.get(request.job_id)
        assert restored.status == "cancelled"
        assert restored.request == request
        replayed, created = recovered.submit(request)
        assert created is False
        assert replayed.status == "cancelled"
        assert recovered_engine.started == []
        await recovered.stop()

    asyncio.run(scenario())


def test_fenced_cancel_rejects_every_identity_or_scientific_request_mismatch(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, ControlledEngine(blocked=False))
    original = _request(0, sequence=51)
    manager.cancel(original.job_id, original)

    def changed(**updates: Any) -> JobSubmitRequest:
        payload = original.model_dump(mode="json")
        payload.update(updates)
        return JobSubmitRequest.model_validate(payload)

    with pytest.raises(JobConflict):
        manager.cancel(original.job_id, changed(attempt_token="f" * 32))
    with pytest.raises(JobConflict):
        manager.cancel(original.job_id, changed(enqueue_sequence=52))

    scientific_payload = original.model_dump(mode="json")
    scientific_payload.pop("request_sha256")
    scientific_payload["input"] = {
        "smiles": "N",
        "net_charge": 0,
        "multiplicity": 1,
    }
    scientific_change = JobSubmitRequest.model_validate(scientific_payload)
    with pytest.raises(JobConflict):
        manager.cancel(original.job_id, scientific_change)

    other_job = _request(1, sequence=51)
    with pytest.raises(JobConflict):
        manager.cancel(other_job.job_id, other_job)
    with pytest.raises(JobConflict):
        manager.cancel("different-path-id", original)
    with pytest.raises(JobNotFound):
        manager.cancel("legacy-unknown")


def test_fenced_cancel_journal_failure_never_publishes_in_memory_identity(
    tmp_path: Path,
) -> None:
    writer = InjectedOSErrorJournalWriter(
        lambda payload: payload["snapshot"]["status"] == "cancelled"
    )
    manager = _manager(
        tmp_path,
        ControlledEngine(blocked=False),
        journal_writer=writer,
    )
    request = _request(0, sequence=61)

    with pytest.raises(JournalPersistenceError):
        manager.cancel(request.job_id, request)
    with pytest.raises(JobNotFound):
        manager.get(request.job_id)
    assert request.enqueue_sequence not in manager._sequence_to_job
    assert manager.health_state()["fatal_reason"] == "journal_persistence_failed"


def test_drain_stops_dequeue_and_resume_continues(tmp_path: Path) -> None:
    async def scenario() -> None:
        engine = ControlledEngine()
        manager = _manager(tmp_path, engine)
        await manager.start()
        manager.submit(_request(0))
        manager.submit(_request(1))
        await _wait_until(lambda: engine.started == ["job-0"])
        response = manager.drain()
        assert response.accepting_jobs is False
        engine.release.set()
        await _wait_until(lambda: manager.get("job-0").status == "completed")
        await asyncio.sleep(0.03)
        assert engine.started == ["job-0"]
        assert manager.get("job-1").status == "queued"
        with pytest.raises(WorkerUnavailable):
            manager.submit(_request(2))
        manager.resume()
        await _wait_until(lambda: manager.get("job-1").status == "completed")
        await manager.stop()

    asyncio.run(scenario())


def test_drain_wins_queued_gpu_admission_and_preserves_fifo_head(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        engine = AdmissionGateEngine()
        manager = _manager(tmp_path, engine)
        await manager.start()
        manager.submit(_request(0))
        manager.submit(_request(1))
        await _wait_until(engine.admission_attempted.is_set)

        drained = manager.drain()
        assert drained.active_jobs == 0
        await _wait_until(lambda: manager._running_job_id is None)
        assert manager.get("job-0").status == "queued"
        assert manager.get("job-0").queue_position == 1
        assert manager.get("job-1").queue_position == 2
        assert manager.health_state()["active_jobs"] == 0
        await asyncio.sleep(0.05)
        assert engine.started == []

        engine.admission_gate.set()
        manager.resume()
        await _wait_until(lambda: manager.get("job-1").status == "completed")
        assert engine.started == ["job-0", "job-1"]
        await manager.stop()

    asyncio.run(scenario())


def test_calculation_timeout_starts_only_after_gpu_admission(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime = TerminatingRuntime()
        engine = AdmissionGateEngine()
        manager = _manager(
            tmp_path,
            engine,
            runtime=runtime,
            single_point_timeout_seconds=0.03,
        )
        await manager.start()
        manager.submit(_request(0))
        await _wait_until(engine.admission_attempted.is_set)

        await asyncio.sleep(0.08)
        queued = manager.get("job-0")
        assert queued.status == "queued"
        assert queued.queue_position == 1
        assert manager.health_state()["fatal"] is False
        assert runtime.termination_reasons == []

        engine.admission_gate.set()
        await _wait_until(lambda: manager.get("job-0").status == "completed")
        assert runtime.termination_reasons == []
        await manager.stop()

    asyncio.run(scenario())


def test_gpu_wait_is_frozen_at_admission_without_child_double_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock_lock = threading.Lock()
    tick = 0
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def deterministic_now() -> datetime:
        nonlocal tick
        with clock_lock:
            current = origin + timedelta(seconds=tick)
            tick += 1
            return current

    monkeypatch.setattr(job_manager_module, "_utcnow", deterministic_now)

    async def scenario() -> None:
        manager = _manager(tmp_path, TimingEngine())
        await manager.start()
        manager.submit(_request(0))
        await _wait_until(lambda: manager.get("job-0").status == "completed")
        snapshot = manager.get("job-0")
        assert snapshot.timings["queue_wait_ms"] == 1000.0
        assert snapshot.timings["gpu_wait_ms"] == 1000.0
        assert snapshot.timings["model_load_ms"] == 23.0
        assert snapshot.result["timings"] == snapshot.timings
        await manager.stop()

    asyncio.run(scenario())


def test_live_and_recovered_fifo_are_ordered_by_authoritative_sequence(
    tmp_path: Path,
) -> None:
    async def live_scenario() -> None:
        engine = ControlledEngine()
        manager = _manager(tmp_path / "live", engine)
        await manager.start()
        manager.submit(_request(0, sequence=1))
        await _wait_until(lambda: engine.started == ["job-0"])
        manager.submit(_request(1, sequence=30))
        manager.submit(_request(2, sequence=10))
        assert manager.get("job-2").queue_position == 1
        assert manager.get("job-1").queue_position == 2
        engine.release.set()
        await _wait_until(
            lambda: all(
                manager.get(job_id).status == "completed"
                for job_id in ("job-0", "job-1", "job-2")
            )
        )
        assert engine.started == ["job-0", "job-2", "job-1"]
        await manager.stop()

    asyncio.run(live_scenario())

    initial = _manager(tmp_path / "recovered", ControlledEngine())
    initial.submit(_request(3, sequence=300))
    initial.submit(_request(4, sequence=100))
    for index in (3, 4):
        journal_path = (
            tmp_path
            / "recovered"
            / "runs"
            / f"job-{index}"
            / f"{index + 1:032x}"
            / "journal.json"
        )
        raw = json.loads(journal_path.read_text(encoding="utf-8"))
        assert raw["snapshot"]["queue_position"] is None

    async def recovery_scenario() -> None:
        engine = ControlledEngine()
        manager = _manager(tmp_path / "recovered", engine)
        await manager.start()
        await _wait_until(lambda: engine.started == ["job-4"])
        engine.release.set()
        await _wait_until(lambda: manager.get("job-3").status == "completed")
        assert engine.started == ["job-4", "job-3"]
        await manager.stop()

    asyncio.run(recovery_scenario())


def test_queued_and_running_cancel_are_cooperative(tmp_path: Path) -> None:
    async def scenario() -> None:
        engine = ControlledEngine()
        manager = _manager(tmp_path, engine)
        await manager.start()
        manager.submit(_request(0))
        manager.submit(_request(1))
        await _wait_until(lambda: manager.get("job-0").status == "running")
        assert manager.cancel("job-1").status == "cancelled"
        assert manager.cancel("job-0").status == "cancel_requested"
        await _wait_until(lambda: manager.get("job-0").status == "cancelled")
        assert manager.get("job-1").status == "cancelled"
        await manager.stop()

    asyncio.run(scenario())


def test_journal_recovery_uses_attempt_layout_and_recovers_fifo(tmp_path: Path) -> None:
    initial = _manager(tmp_path, ControlledEngine())
    initial.submit(_request(0))
    journal = tmp_path / "runs" / "job-0" / f"{1:032x}" / "journal.json"
    assert journal.is_file()

    async def scenario() -> None:
        engine = ControlledEngine()
        recovered = _manager(tmp_path, engine)
        await recovered.start()
        await _wait_until(lambda: recovered.get("job-0").status == "running")
        assert recovered.get("job-0").stage in {"validating", "single_point"}
        engine.release.set()
        await _wait_until(lambda: recovered.get("job-0").status == "completed")
        await recovered.stop()

    asyncio.run(scenario())


def test_journal_recovery_marks_interrupted_running_job_retryable(
    tmp_path: Path,
) -> None:
    initial = _manager(tmp_path, ControlledEngine())
    initial.submit(_request(0))
    journal = tmp_path / "runs" / "job-0" / f"{1:032x}" / "journal.json"
    payload = json.loads(journal.read_text())
    payload["snapshot"].update(
        {"status": "running", "stage": "hessian", "progress_percent": 80}
    )
    atomic_write_json(journal, payload)
    partial_directory = journal.parent / "artifacts"
    partial_directory.mkdir()
    atomic_write_bytes(partial_directory / "partial.bin", b"partial")

    async def scenario() -> None:
        recovered = _manager(tmp_path, ControlledEngine(blocked=False))
        await recovered.start()
        snapshot = recovered.get("job-0")
        assert snapshot.status == "failed"
        assert snapshot.error is not None
        assert snapshot.error.code == "worker_restarted"
        assert snapshot.error.retryable is True
        assert not partial_directory.exists()
        await recovered.stop()

    asyncio.run(scenario())


def test_failed_and_cancelled_jobs_remove_unmanifested_partial_artifacts(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        failed_engine = PartialArtifactEngine(fail_immediately=True)
        failed_manager = _manager(tmp_path / "failed", failed_engine)
        await failed_manager.start()
        failed_manager.submit(_request(0))
        await _wait_until(lambda: failed_manager.get("job-0").status == "failed")
        failed_attempt = (
            tmp_path / "failed" / "runs" / "job-0" / f"{1:032x}" / "artifacts"
        )
        assert not failed_attempt.exists()
        await failed_manager.stop()

        cancelled_engine = PartialArtifactEngine(fail_immediately=False)
        cancelled_manager = _manager(tmp_path / "cancelled", cancelled_engine)
        await cancelled_manager.start()
        cancelled_manager.submit(_request(0))
        await _wait_until(cancelled_engine.started.is_set)
        cancelled_manager.cancel("job-0")
        await _wait_until(lambda: cancelled_manager.get("job-0").status == "cancelled")
        cancelled_attempt = (
            tmp_path / "cancelled" / "runs" / "job-0" / f"{1:032x}" / "artifacts"
        )
        assert not cancelled_attempt.exists()
        await cancelled_manager.stop()

    asyncio.run(scenario())


def test_partial_artifact_cleanup_runs_off_the_uds_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        manager = _manager(tmp_path, PartialArtifactEngine(fail_immediately=True))
        await manager.start()
        event_loop_thread = threading.get_ident()
        cleanup_started = threading.Event()
        allow_cleanup = threading.Event()
        cleanup_threads: list[int] = []
        original_cleanup = manager._cleanup_partial_artifacts

        def tracked_cleanup(snapshot) -> None:
            cleanup_threads.append(threading.get_ident())
            cleanup_started.set()
            assert allow_cleanup.wait(2.0)
            original_cleanup(snapshot)

        monkeypatch.setattr(manager, "_cleanup_partial_artifacts", tracked_cleanup)
        manager.submit(_request(0))
        await _wait_until(cleanup_started.is_set)

        # The event loop can still serve health/state work while filesystem
        # cleanup is deliberately blocked in its bounded worker thread.
        assert manager.health_state()["active_jobs"] == 1
        assert cleanup_threads and cleanup_threads[0] != event_loop_thread
        allow_cleanup.set()
        await _wait_until(lambda: manager.get("job-0").status == "failed")
        await manager.stop()

    asyncio.run(scenario())


def test_submit_journal_oserror_never_publishes_or_enqueues_job(tmp_path: Path) -> None:
    async def scenario() -> None:
        writer = InjectedOSErrorJournalWriter(
            lambda payload: payload["snapshot"]["status"] == "queued"
        )
        manager = _manager(
            tmp_path,
            ControlledEngine(blocked=False),
            journal_writer=writer,
        )
        await manager.start()

        with pytest.raises(JournalPersistenceError):
            manager.submit(_request(0))

        assert manager.list().total == 0
        state = manager.health_state()
        assert state["fatal"] is True
        assert state["fatal_reason"] == "journal_persistence_failed"
        assert state["draining"] is True
        assert state["queued_jobs"] == 0
        await asyncio.sleep(0.15)
        assert manager._fatal_exit_scheduled is False
        await _wait_until(
            lambda: (
                manager._dispatcher_task is not None and manager._dispatcher_task.done()
            )
        )
        assert manager._dispatcher_task is not None
        assert manager._dispatcher_task.exception() is None
        await manager.stop()

    asyncio.run(scenario())


def test_progress_journal_oserror_fails_job_and_drains_worker(tmp_path: Path) -> None:
    async def scenario() -> None:
        fatal_calls: list[bool] = []
        writer = InjectedOSErrorJournalWriter(
            lambda payload: (
                payload["snapshot"]["status"] == "running"
                and payload["snapshot"]["stage"] == "single_point"
            )
        )
        manager = _manager(
            tmp_path,
            ControlledEngine(blocked=False),
            journal_writer=writer,
            fatal_exit=lambda: fatal_calls.append(True),
        )
        await manager.start()
        manager.submit(_request(0))

        await _wait_until(lambda: manager.get("job-0").status == "failed")
        snapshot = manager.get("job-0")
        assert snapshot.error is not None
        assert snapshot.error.code == "journal_persistence_failed"
        state = manager.health_state()
        assert state["fatal"] is True
        assert state["fatal_reason"] == "journal_persistence_failed"
        assert state["draining"] is True
        await asyncio.sleep(0.15)
        assert fatal_calls == []
        await _wait_until(
            lambda: (
                manager._dispatcher_task is not None and manager._dispatcher_task.done()
            )
        )
        assert manager._dispatcher_task is not None
        assert manager._dispatcher_task.exception() is None
        await manager.stop()

    asyncio.run(scenario())


def test_terminal_journal_oserror_never_publishes_completion(tmp_path: Path) -> None:
    async def scenario() -> None:
        writer = InjectedOSErrorJournalWriter(
            lambda payload: payload["snapshot"]["status"] == "completed"
        )
        engine = ControlledEngine(blocked=False)
        manager = _manager(tmp_path, engine, journal_writer=writer)
        await manager.start()
        manager.submit(_request(0))

        await _wait_until(lambda: manager.health_state()["fatal"])
        await _wait_until(lambda: engine.active == 0)
        await _wait_until(
            lambda: (
                manager._dispatcher_task is not None and manager._dispatcher_task.done()
            )
        )
        snapshot = manager.get("job-0")
        assert snapshot.status == "running"
        assert snapshot.artifacts == []
        journal = tmp_path / "runs" / "job-0" / f"{1:032x}" / "journal.json"
        assert (
            json.loads(journal.read_text(encoding="utf-8"))["snapshot"]["status"]
            == "running"
        )
        assert not (journal.parent / "artifacts").exists()
        state = manager.health_state()
        assert state["fatal_reason"] == "journal_persistence_failed"
        assert state["draining"] is True
        assert manager._dispatcher_task is not None
        assert manager._dispatcher_task.exception() is None
        await manager.stop()

    asyncio.run(scenario())


def test_delete_journal_oserror_preserves_manifest_and_files(tmp_path: Path) -> None:
    async def scenario() -> None:
        writer = InjectedOSErrorJournalWriter(
            lambda payload: (
                payload["snapshot"]["status"] == "completed"
                and payload["artifact_state"] == "deleting"
            )
        )
        manager = _manager(
            tmp_path,
            ControlledEngine(blocked=False),
            journal_writer=writer,
        )
        await manager.start()
        manager.submit(_request(0))
        await _wait_until(lambda: manager.get("job-0").status == "completed")
        artifact_path = (
            tmp_path / "runs" / "job-0" / f"{1:032x}" / "artifacts" / "result.json"
        )

        with pytest.raises(JournalPersistenceError):
            manager.delete_artifacts("job-0")

        assert manager.get("job-0").artifacts
        assert artifact_path.read_bytes() == b'{"ok":true}'
        state = manager.health_state()
        assert state["fatal"] is True
        assert state["fatal_reason"] == "journal_persistence_failed"
        assert state["draining"] is True
        await manager.stop()

    asyncio.run(scenario())


def test_timeout_drains_without_process_exit_and_never_overlaps(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        engine = ControlledEngine()
        fatal_calls: list[bool] = []
        manager = _manager(
            tmp_path,
            engine,
            fatal_exit=lambda: fatal_calls.append(True),
            single_point_timeout_seconds=0.03,
        )
        await manager.start()
        manager.submit(_request(0))
        manager.submit(_request(1))
        await _wait_until(lambda: manager.get("job-0").status == "failed")
        assert manager.get("job-0").error.code == "calculation_timeout"
        assert manager.health_state()["fatal"] is True
        assert manager.get("job-1").status == "queued"
        await asyncio.sleep(0.03)
        assert fatal_calls == []
        await _wait_until(lambda: engine.active == 0)
        assert engine.started == ["job-0"]
        assert engine.max_active == 1
        await manager.stop()

    asyncio.run(scenario())


def test_safely_terminated_timeout_fails_only_attempt_and_worker_continues(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime = TerminatingRuntime()
        engine = TimeoutThenSuccessEngine(blocked=False)
        manager = _manager(
            tmp_path,
            engine,
            runtime=runtime,
            single_point_timeout_seconds=0.03,
        )
        await manager.start()
        manager.submit(_request(0))
        manager.submit(_request(1))

        await _wait_until(lambda: manager.get("job-1").status == "completed")
        failed = manager.get("job-0")
        assert failed.status == "failed"
        assert failed.error is not None
        assert failed.error.code == "calculation_timeout"
        assert runtime.termination_reasons == ["timeout"]
        state = manager.health_state()
        assert state["fatal"] is False
        assert state["draining"] is False
        assert engine.started == ["job-0", "job-1"]
        assert engine.max_active == 1
        await manager.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure",
    (
        MemoryError("synthetic allocation failure"),
        RuntimeError("CUDA out of memory while allocating a tensor"),
    ),
    ids=("memory-error", "cuda-oom"),
)
def test_gpu_oom_clears_cache_and_allows_next_fifo_job(
    tmp_path: Path,
    failure: BaseException,
) -> None:
    async def scenario() -> None:
        runtime = ReadyRuntime()
        engine = FaultThenSuccessEngine(failure)
        fatal_calls: list[bool] = []
        manager = _manager(
            tmp_path,
            engine,
            runtime=runtime,
            fatal_exit=lambda: fatal_calls.append(True),
        )
        await manager.start()
        manager.submit(_request(0))
        manager.submit(_request(1))

        await _wait_until(lambda: manager.get("job-1").status == "completed")
        failed = manager.get("job-0")
        assert failed.status == "failed"
        assert failed.error is not None
        assert failed.error.code == "gpu_oom"
        assert failed.error.retryable is True
        assert runtime.empty_cache_calls == 1
        state = manager.health_state()
        assert state["fatal"] is False
        assert state["draining"] is False
        assert fatal_calls == []
        assert engine.started == ["job-0", "job-1"]
        await manager.stop()

    asyncio.run(scenario())


def test_illegal_cuda_access_fails_closed_and_never_dequeues_next_job(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime = ReadyRuntime()
        engine = FaultThenSuccessEngine(
            RuntimeError("CUDA illegal memory access was encountered")
        )
        fatal_calls: list[bool] = []
        manager = _manager(
            tmp_path,
            engine,
            runtime=runtime,
            fatal_exit=lambda: fatal_calls.append(True),
        )
        await manager.start()
        manager.submit(_request(0))
        manager.submit(_request(1))

        await _wait_until(lambda: manager.get("job-0").status == "failed")
        failed = manager.get("job-0")
        assert failed.error is not None
        assert failed.error.code == "cuda_fatal"
        assert failed.error.retryable is True
        state = manager.health_state()
        assert state["fatal"] is True
        assert state["fatal_reason"] == "cuda_fatal"
        assert state["draining"] is True
        assert state["accepting_jobs"] is False
        await asyncio.sleep(0.03)
        assert fatal_calls == []
        assert manager.get("job-1").status == "queued"
        assert engine.started == ["job-0"]
        assert runtime.empty_cache_calls == 0
        await manager.stop()

    asyncio.run(scenario())


def test_proven_safe_cuda_fatal_requests_host_exit_after_terminal_journal(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fatal_calls: list[dict[str, Any]] = []
        runtime = SafeFatalRuntime()
        manager: JobManager

        def fatal_exit() -> None:
            journal = (
                tmp_path
                / "runs"
                / "job-0"
                / f"{1:032x}"
                / "journal.json"
            )
            fatal_calls.append(json.loads(journal.read_text(encoding="utf-8")))

        manager = _manager(
            tmp_path,
            FaultThenSuccessEngine(
                RuntimeError("CUDA illegal memory access was encountered")
            ),
            runtime=runtime,
            fatal_exit=fatal_exit,
        )
        await manager.start()
        manager.submit(_request(0))

        await _wait_until(lambda: bool(fatal_calls))
        assert fatal_calls[0]["snapshot"]["status"] == "failed"
        assert fatal_calls[0]["snapshot"]["error"]["code"] == "cuda_fatal"
        assert manager._fatal_exit_scheduled is True
        await manager.stop()

    asyncio.run(scenario())


def test_artifacts_require_manifest_id_checksum_and_terminal_delete(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        engine = ControlledEngine(blocked=False)
        manager = _manager(tmp_path, engine)
        await manager.start()
        manager.submit(_request(0))
        await _wait_until(lambda: manager.get("job-0").status == "completed")
        access = manager.artifact("job-0", "scientific_result")
        artifact_path = access.path
        with access.stream:
            assert access.stream.read() == b'{"ok":true}'
        with pytest.raises(ArtifactNotFound):
            manager.artifact("job-0", "../../journal.json")

        artifact_path.write_bytes(b"tampered")
        with pytest.raises(RuntimeError, match="checksum"):
            manager.artifact("job-0", "scientific_result")
        artifact_path.unlink()
        artifact_path.symlink_to("/etc/passwd")
        with pytest.raises(ArtifactNotFound):
            manager.artifact("job-0", "scientific_result")
        artifact_path.unlink()
        deleted = manager.delete_artifacts("job-0")
        assert deleted.deleted is True
        assert manager.get("job-0").artifacts == []
        await manager.stop()

    asyncio.run(scenario())


def test_open_artifact_fd_survives_concurrent_delete(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = _manager(tmp_path, ControlledEngine(blocked=False))
        await manager.start()
        manager.submit(_request(0))
        await _wait_until(lambda: manager.get("job-0").status == "completed")

        access = manager.artifact("job-0", "scientific_result")
        manager.delete_artifacts("job-0")
        with access.stream:
            assert access.stream.read() == b'{"ok":true}'
        assert not access.path.exists()
        await manager.stop()

    asyncio.run(scenario())


def test_open_artifact_fd_is_immune_to_symlink_replacement(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = _manager(tmp_path, ControlledEngine(blocked=False))
        await manager.start()
        manager.submit(_request(0))
        await _wait_until(lambda: manager.get("job-0").status == "completed")

        access = manager.artifact("job-0", "scientific_result")
        external = tmp_path / "external-secret"
        external.write_bytes(b"must-not-be-streamed")
        access.path.unlink()
        access.path.symlink_to(external)
        with access.stream:
            assert access.stream.read() == b'{"ok":true}'
        with pytest.raises(ArtifactNotFound):
            manager.artifact("job-0", "scientific_result")
        access.path.unlink()
        manager.delete_artifacts("job-0")
        await manager.stop()

    asyncio.run(scenario())


def test_open_bundle_fd_survives_concurrent_artifact_delete(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = _manager(tmp_path, ControlledEngine(blocked=False))
        await manager.start()
        manager.submit(_request(0))
        await _wait_until(lambda: manager.get("job-0").status == "completed")

        access = manager.bundle("job-0")
        manager.delete_artifacts("job-0")
        with access.stream:
            bundle_bytes = access.stream.read()
        assert not access.path.exists()
        with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as archive:
            assert archive.namelist() == ["result.json"]
            assert archive.read("result.json") == b'{"ok":true}'
        await manager.stop()

    asyncio.run(scenario())


def test_deletion_tombstones_recover_after_crash_between_rename_and_remove(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workers.monomer_dft_worker.app import job_manager as job_manager_module

    async def scenario() -> None:
        manager = _manager(tmp_path, ControlledEngine(blocked=False))
        await manager.start()
        manager.submit(_request(0))
        await _wait_until(lambda: manager.get("job-0").status == "completed")
        bundle = manager.bundle("job-0")
        bundle.stream.close()

        real_rmtree = job_manager_module.shutil.rmtree
        failures = 0

        def fail_once(path, *args, **kwargs):
            nonlocal failures
            if Path(path).name == ".artifacts.deleting" and failures == 0:
                failures += 1
                raise OSError("injected crash window")
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(job_manager_module.shutil, "rmtree", fail_once)
        with pytest.raises(ArtifactDeletionFailed):
            manager.delete_artifacts("job-0")
        attempt = tmp_path / "runs" / "job-0" / f"{1:032x}"
        envelope = JobJournalV2.model_validate_json(
            (attempt / "journal.json").read_bytes()
        )
        assert envelope.artifact_state == "deleting"
        assert not (attempt / "artifacts").exists()
        assert (attempt / ".artifacts.deleting").is_dir()
        assert not (attempt / "artifact_bundle.zip").exists()
        assert (attempt / ".artifact_bundle.zip.deleting").is_file()
        await manager.stop()

        recovered = _manager(tmp_path, ControlledEngine(blocked=False))
        await recovered.start()
        assert recovered.get("job-0").artifacts == []
        envelope = JobJournalV2.model_validate_json(
            (attempt / "journal.json").read_bytes()
        )
        assert envelope.artifact_state == "deleted"
        assert envelope.artifact_manifest
        assert not (attempt / ".artifacts.deleting").exists()
        assert not (attempt / ".artifact_bundle.zip.deleting").exists()
        await recovered.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "name",
    (
        "../escape.json",
        "..\\escape.json",
        "/absolute.json",
        "\\absolute.json",
        ".",
        "..",
        "NUL",
        "con.txt",
        "contains space.json",
        "trailing.",
    ),
)
def test_artifact_descriptor_rejects_unsafe_portable_names(name: str) -> None:
    with pytest.raises(Exception):
        ArtifactDescriptor(
            artifact_id="result",
            name=name,
            media_type="application/json",
            size_bytes=0,
            sha256="0" * 64,
        )


def test_zip_builder_streams_verified_fds_and_rejects_unsafe_member_names(
    tmp_path: Path,
) -> None:
    artifact_path = tmp_path / "result.json"
    atomic_write_bytes(artifact_path, b'{"ok":true}')
    descriptor = describe_artifact(
        artifact_id="result",
        path=artifact_path,
        media_type="application/json",
    )
    bundle_path = tmp_path / "bundle.zip"
    with open_readonly_regular(artifact_path) as stream:
        bundle_stream = build_bundle(bundle_path, [(descriptor, stream)])
    bundle_stream.close()
    with zipfile.ZipFile(bundle_path) as archive:
        assert archive.namelist() == ["result.json"]
        assert archive.read("result.json") == b'{"ok":true}'

    unsafe = descriptor.model_copy(update={"name": "..\\escape.json"})
    with open_readonly_regular(artifact_path) as stream:
        with pytest.raises(RuntimeError, match="unsafe artifact name"):
            build_bundle(tmp_path / "unsafe.zip", [(unsafe, stream)])
    assert not (tmp_path / "unsafe.zip").exists()


def test_zip_builder_enforces_separate_transfer_and_expanded_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_path = tmp_path / "payload.bin"
    atomic_write_bytes(artifact_path, b"0123456789")
    descriptor = describe_artifact(
        artifact_id="payload",
        path=artifact_path,
        media_type="application/octet-stream",
    )
    monkeypatch.setattr(artifacts_module, "MAX_BUNDLE_EXPANDED_BYTES", 9)
    with open_readonly_regular(artifact_path) as stream:
        with pytest.raises(RuntimeError, match="expanded content"):
            build_bundle(tmp_path / "expanded.zip", [(descriptor, stream)])
    assert not (tmp_path / BUNDLE_CREATING_NAME).exists()

    monkeypatch.setattr(artifacts_module, "MAX_BUNDLE_EXPANDED_BYTES", 1024)
    monkeypatch.setattr(artifacts_module, "MAX_BUNDLE_SIZE_BYTES", 8)
    with open_readonly_regular(artifact_path) as stream:
        with pytest.raises(RuntimeError, match="transfer limit"):
            build_bundle(tmp_path / "transfer.zip", [(descriptor, stream)])
    assert not (tmp_path / BUNDLE_CREATING_NAME).exists()


def test_startup_recovers_fixed_bundle_creating_tombstone(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = _manager(tmp_path, ControlledEngine(blocked=False))
        await manager.start()
        manager.submit(_request(0))
        await _wait_until(lambda: manager.get("job-0").status == "completed")
        attempt = tmp_path / "runs" / "job-0" / f"{1:032x}"
        creating = attempt / BUNDLE_CREATING_NAME
        atomic_write_bytes(creating, b"interrupted zip body")
        await manager.stop()

        recovered = _manager(tmp_path, ControlledEngine(blocked=False))
        await recovered.start()
        assert not creating.exists()
        assert recovered.get("job-0").artifact_state == "available"
        await recovered.stop()

    asyncio.run(scenario())


def test_artifact_limits_and_case_insensitive_bundle_names(tmp_path: Path) -> None:
    with pytest.raises(Exception):
        ArtifactDescriptor(
            artifact_id="too_large",
            name="too-large.bin",
            media_type="application/octet-stream",
            size_bytes=MAX_ARTIFACT_SIZE_BYTES + 1,
            sha256="0" * 64,
        )

    oversized = tmp_path / "oversized.bin"
    with oversized.open("wb") as stream:
        stream.truncate(MAX_ARTIFACT_SIZE_BYTES + 1)
    with pytest.raises(RuntimeError, match="64 MiB"):
        describe_artifact(
            artifact_id="oversized",
            path=oversized,
            media_type="application/octet-stream",
        )

    first_path = tmp_path / "Result.JSON"
    second_path = tmp_path / "result.json"
    atomic_write_bytes(first_path, b"first")
    atomic_write_bytes(second_path, b"second")
    first = describe_artifact(
        artifact_id="first", path=first_path, media_type="application/json"
    )
    second = describe_artifact(
        artifact_id="second", path=second_path, media_type="application/json"
    )
    with open_readonly_regular(first_path) as first_stream:
        with open_readonly_regular(second_path) as second_stream:
            with pytest.raises(RuntimeError, match="case-insensitive duplicate"):
                build_bundle(
                    tmp_path / "duplicate-case.zip",
                    [(first, first_stream), (second, second_stream)],
                )
