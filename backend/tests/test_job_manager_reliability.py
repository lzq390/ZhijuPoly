from __future__ import annotations

from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from app.main import _shutdown_in_process_job_managers
from app.models import (
    ConditionalGenerationTgRequest,
    ConditionalGenerationTgResponse,
    PolytaoGenerationResponse,
    ReverseDesignTgRequest,
    ReverseDesignTgResponse,
)
from app.services.conditional_generation_jobs import (
    ConditionalGenerationJobCapacityError,
    ConditionalGenerationJobManager,
)
from app.services.gpu_runtime_registry import GpuSchedulerClosedError
from app.services.in_memory_jobs import BoundedInMemoryJobStore
from app.services.polytao_jobs import PolytaoJobCapacityError, PolytaoJobManager
from app.services.reverse_design_jobs import (
    ReverseDesignJobManager,
    ReverseDesignJobUnavailableError,
)


def _wait_until(predicate, *, timeout: float = 3.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return
        sleep(0.01)
    raise AssertionError("condition was not satisfied before timeout")


def _conditional_request() -> ConditionalGenerationTgRequest:
    return ConditionalGenerationTgRequest(smiles="*CC*", delta_tg=30, candidate_count=1)


def _conditional_result(
    request: ConditionalGenerationTgRequest,
) -> ConditionalGenerationTgResponse:
    return ConditionalGenerationTgResponse(
        input_smiles=request.smiles,
        normalized_input_smiles=request.smiles,
        delta_tg=request.delta_tg,
        query_time_ms=1.0,
        requested_count=request.candidate_count,
        returned_count=0,
        attempts=1,
        filter_counter={},
        results=[],
    )


def _polytao_result(*, structure_svg: str | None = None) -> PolytaoGenerationResponse:
    return PolytaoGenerationResponse.model_validate(
        {
            "prompt": "test prompt",
            "query_time_ms": 1.0,
            "requested_count": 1,
            "returned_count": 1,
            "attempts": 1,
            "filter_counter": {},
            "results": [
                {
                    "rank": 1,
                    "generated_smiles": "*CC*",
                    "raw_smiles": "[*]CC[*]",
                    "structure_svg": structure_svg,
                    "valid_smiles": True,
                    "sa_score": None,
                    "warnings": [],
                }
            ],
        }
    )


def _create_polytao_job(manager: PolytaoJobManager, runner, *, timeout_seconds: float = 600.0):
    return manager.create_job(
        input_smiles=None,
        canonical_smiles=None,
        prompt="test prompt",
        requested_count=1,
        runner=runner,
        timeout_seconds=timeout_seconds,
    )


def test_executor_submit_rejection_is_atomic_for_both_lanes() -> None:
    request = _conditional_request()
    conditional = ConditionalGenerationJobManager(max_workers=1)
    polytao = PolytaoJobManager(max_workers=1)
    conditional._executor.shutdown(wait=False)
    polytao._executor.shutdown(wait=False)

    with pytest.raises(RuntimeError):
        conditional.create_job(request, lambda: _conditional_result(request))
    with pytest.raises(RuntimeError):
        _create_polytao_job(polytao, _polytao_result)

    assert conditional.active_jobs == 0
    assert conditional.retained_jobs == 0
    assert polytao.active_jobs == 0
    assert polytao.retained_jobs == 0
    conditional.shutdown(wait=False)
    polytao.shutdown(wait=False)


def _reverse_design_request() -> ReverseDesignTgRequest:
    return ReverseDesignTgRequest(
        smiles="CCO",
        target_tg=120,
        similarity_threshold=0.7,
        candidate_size=1,
    )


def _reverse_design_result() -> ReverseDesignTgResponse:
    return ReverseDesignTgResponse(
        target_tg=120,
        query_time_ms=1,
        candidate_pool_size=0,
        sampled_candidate_count=0,
        total=0,
        results=[],
    )


def test_reverse_design_executor_submit_rejection_is_atomic() -> None:
    manager = ReverseDesignJobManager(max_workers=1)
    manager._executor.shutdown(wait=False)

    with pytest.raises(ReverseDesignJobUnavailableError):
        manager.create_job(
            _reverse_design_request(),
            lambda progress, cancelled: _reverse_design_result(),
        )

    assert manager.active_jobs == 0
    assert manager.active_executions == 0
    assert manager._jobs == {}
    manager.shutdown(wait=False)


def test_reverse_design_stop_accepting_rejects_without_creating_a_job() -> None:
    manager = ReverseDesignJobManager(max_workers=1)
    manager.stop_accepting()

    with pytest.raises(ReverseDesignJobUnavailableError):
        manager.create_job(
            _reverse_design_request(),
            lambda progress, cancelled: _reverse_design_result(),
        )

    assert manager.accepting is False
    assert manager.active_jobs == 0
    assert manager.active_executions == 0
    assert manager._jobs == {}
    manager.shutdown(wait=False)


def test_reverse_design_create_and_stop_accepting_are_atomic() -> None:
    manager = ReverseDesignJobManager(max_workers=1)
    submit_entered = Event()
    release_submit = Event()
    create_finished = Event()
    original_submit = manager._executor.submit
    created_jobs = []

    def pausing_submit(*args, **kwargs):
        submit_entered.set()
        assert release_submit.wait(timeout=3)
        return original_submit(*args, **kwargs)

    manager._executor.submit = pausing_submit

    def create() -> None:
        created_jobs.append(
            manager.create_job(
                _reverse_design_request(),
                lambda progress, cancelled: _reverse_design_result(),
            )
        )
        create_finished.set()

    create_thread = Thread(target=create)
    stop_thread = Thread(target=manager.stop_accepting)
    create_thread.start()
    assert submit_entered.wait(timeout=3)
    stop_thread.start()

    assert create_finished.is_set() is False
    assert stop_thread.is_alive()
    release_submit.set()
    create_thread.join(timeout=3)
    stop_thread.join(timeout=3)

    assert len(created_jobs) == 1
    manager.wait_for_job(created_jobs[0].job_id, timeout=3)
    assert manager.accepting is False
    assert manager.active_jobs == 0
    assert manager.active_executions == 0
    assert manager._jobs[created_jobs[0].job_id].future is not None
    manager.shutdown(wait=True)


def test_reverse_design_queued_cancel_never_runs_the_runner() -> None:
    manager = ReverseDesignJobManager(max_workers=1)
    first_started = Event()
    release_first = Event()
    second_called = Event()

    first = manager.create_job(
        _reverse_design_request(),
        lambda progress, cancelled: (
            first_started.set(),
            release_first.wait(timeout=3),
            _reverse_design_result(),
        )[2],
    )
    assert first_started.wait(timeout=3)
    second = manager.create_job(
        _reverse_design_request(),
        lambda progress, cancelled: (
            second_called.set(),
            _reverse_design_result(),
        )[1],
    )

    cancelled = manager.cancel_job(second.job_id)

    assert cancelled.status == "cancelled"
    assert second_called.is_set() is False
    assert manager.active_jobs == 1
    assert manager.active_executions == 1
    release_first.set()
    manager.wait_for_job(first.job_id, timeout=3)
    manager.shutdown(wait=True)


def test_reverse_design_running_cancel_stays_active_until_runner_exits() -> None:
    manager = ReverseDesignJobManager(max_workers=1)
    started = Event()
    release = Event()
    saw_cancellation = Event()

    def runner(progress, cancelled):
        started.set()
        assert release.wait(timeout=3)
        if cancelled():
            saw_cancellation.set()
        return _reverse_design_result()

    job = manager.create_job(_reverse_design_request(), runner)
    assert started.wait(timeout=3)

    cancellation_requested = manager.cancel_job(job.job_id)

    assert cancellation_requested.status == "running"
    assert manager.active_jobs == 1
    assert manager.active_executions == 1
    release.set()
    manager.wait_for_job(job.job_id, timeout=3)
    assert saw_cancellation.is_set()
    assert manager.get_job(job.job_id).status == "cancelled"
    assert manager.active_jobs == 0
    assert manager.active_executions == 0
    manager.shutdown(wait=True)


def test_reverse_design_unexpected_future_error_becomes_failed() -> None:
    manager = ReverseDesignJobManager(max_workers=1)

    def crash_before_runner(job_id, runner):
        raise RuntimeError("future crashed")

    manager._run_job = crash_before_runner
    job = manager.create_job(
        _reverse_design_request(),
        lambda progress, cancelled: _reverse_design_result(),
    )
    with pytest.raises(RuntimeError, match="future crashed"):
        manager.wait_for_job(job.job_id, timeout=3)
    _wait_until(lambda: manager.get_job(job.job_id).status == "failed")

    status = manager.get_job(job.job_id)
    assert status.status == "failed"
    assert status.finished_at is not None
    assert status.error == "Reverse-design task terminated unexpectedly: future crashed"
    assert manager.active_jobs == 0
    assert manager.active_executions == 0
    manager.shutdown(wait=True)


def test_conditional_deadline_starts_when_api_accepts_the_job() -> None:
    clock = [0.0]
    request = _conditional_request()
    first_started = Event()
    release_first = Event()
    second_called = Event()
    manager = ConditionalGenerationJobManager(
        max_workers=1,
        max_active_jobs=2,
        monotonic_fn=lambda: clock[0],
    )

    first = manager.create_job(
        request,
        lambda: (
            first_started.set(),
            release_first.wait(timeout=3),
            _conditional_result(request),
        )[2],
        timeout_seconds=100,
    )
    assert first_started.wait(timeout=3)
    second = manager.create_job(
        request,
        lambda: (second_called.set(), _conditional_result(request))[1],
        timeout_seconds=5,
    )
    clock[0] = 6.0
    release_first.set()

    _wait_until(lambda: manager.get_job(second.job_id).status == "failed")
    assert manager.get_job(second.job_id).error.startswith("GPU_QUEUE_TIMEOUT:")
    assert second_called.is_set() is False
    _wait_until(lambda: manager.get_job(first.job_id).status == "completed")
    manager.shutdown(wait=True)


def test_polytao_deadline_starts_when_api_accepts_the_job() -> None:
    clock = [0.0]
    first_started = Event()
    release_first = Event()
    second_called = Event()
    manager = PolytaoJobManager(
        max_workers=1,
        max_active_jobs=2,
        monotonic_fn=lambda: clock[0],
    )
    first = _create_polytao_job(
        manager,
        lambda: (
            first_started.set(),
            release_first.wait(timeout=3),
            _polytao_result(),
        )[2],
        timeout_seconds=100,
    )
    assert first_started.wait(timeout=3)
    second = _create_polytao_job(
        manager,
        lambda: (second_called.set(), _polytao_result())[1],
        timeout_seconds=5,
    )
    clock[0] = 6.0
    release_first.set()

    _wait_until(lambda: manager.get_job(second.job_id).status == "failed")
    assert manager.get_job(second.job_id).error_message.startswith("GPU_QUEUE_TIMEOUT:")
    assert second_called.is_set() is False
    _wait_until(lambda: manager.get_job(first.job_id).status == "completed")
    manager.shutdown(wait=True)


def test_shutdown_cancels_queued_work_but_preserves_running_success() -> None:
    first_started = Event()
    release_first = Event()
    second_called = Event()
    manager = PolytaoJobManager(max_workers=1, max_active_jobs=2)
    running = _create_polytao_job(
        manager,
        lambda: (
            first_started.set(),
            release_first.wait(timeout=3),
            _polytao_result(structure_svg="<svg>retained</svg>"),
        )[2],
    )
    assert first_started.wait(timeout=3)
    queued = _create_polytao_job(
        manager,
        lambda: (second_called.set(), _polytao_result())[1],
    )

    manager.shutdown(wait=False)
    _wait_until(lambda: manager.get_job(queued.job_id).status == "cancelled")
    release_first.set()
    _wait_until(lambda: manager.get_job(running.job_id).status == "completed")

    completed = manager.get_job(running.job_id)
    assert completed.result is not None
    assert completed.result.results[0].structure_svg == "<svg>retained</svg>"
    assert second_called.is_set() is False
    assert manager.active_executions == 0


def test_shutdown_wait_is_bounded_while_running_future_remains_active() -> None:
    started = Event()
    release = Event()
    manager = PolytaoJobManager()
    job = _create_polytao_job(
        manager,
        lambda: (
            started.set(),
            release.wait(timeout=3),
            _polytao_result(),
        )[2],
    )
    assert started.wait(timeout=3)

    began_wait = monotonic()
    assert manager.shutdown(wait=True, timeout_seconds=0.03) is False
    assert monotonic() - began_wait < 0.5
    assert manager.active_jobs == 1

    release.set()
    _wait_until(lambda: manager.get_job(job.job_id).status == "completed")
    _wait_until(lambda: manager.active_jobs == 0)


def test_lifespan_job_shutdown_obeys_two_phase_order() -> None:
    events: list[str] = []

    class Manager:
        def __init__(self, name: str) -> None:
            self.name = name

        def stop_accepting(self) -> None:
            events.append(f"{self.name}:stop")

        def cancel_pending(self):
            events.append(f"{self.name}:cancel")
            return ()

        def wait_for_futures(self, futures, *, timeout_seconds):
            assert futures == ()
            assert timeout_seconds >= 0
            events.append(f"{self.name}:wait")
            return True

        def close_executor(self, *, wait: bool) -> None:
            assert wait is False
            events.append(f"{self.name}:close")

    class Registry:
        def stop_accepting(self) -> None:
            events.append("registry:stop")

    app = SimpleNamespace(
        state=SimpleNamespace(
            conditional_generation_job_manager=Manager("conditional"),
            polytao_job_manager=Manager("polytao"),
            reverse_design_job_manager=Manager("reverse"),
            gpu_runtime_registry=Registry(),
        )
    )

    assert _shutdown_in_process_job_managers(app, grace_seconds=1) is True
    assert events == [
        "conditional:stop",
        "polytao:stop",
        "reverse:stop",
        "registry:stop",
        "conditional:cancel",
        "polytao:cancel",
        "reverse:cancel",
        "conditional:wait",
        "polytao:wait",
        "reverse:wait",
        "conditional:close",
        "polytao:close",
        "reverse:close",
    ]


@pytest.mark.parametrize("lane", ["conditional", "polytao"])
def test_future_remains_active_until_terminal_record_is_reapable(lane: str) -> None:
    store = BoundedInMemoryJobStore(instance_id="a" * 16)
    started = Event()
    release_runner = Event()
    entered_reap = Event()
    release_reap = Event()
    original_mark_reapable = store.mark_reapable

    def blocking_mark_reapable(namespace: str, job_id: str) -> None:
        entered_reap.set()
        assert release_reap.wait(timeout=3)
        original_mark_reapable(namespace, job_id)

    store.mark_reapable = blocking_mark_reapable  # type: ignore[method-assign]
    request = _conditional_request()
    if lane == "conditional":
        manager = ConditionalGenerationJobManager(max_active_jobs=1, store=store)
        manager.create_job(
            request,
            lambda: (
                started.set(),
                release_runner.wait(timeout=3),
                _conditional_result(request),
            )[2],
        )
    else:
        manager = PolytaoJobManager(max_active_jobs=1, store=store)
        _create_polytao_job(
            manager,
            lambda: (
                started.set(),
                release_runner.wait(timeout=3),
                _polytao_result(),
            )[2],
        )
    assert started.wait(timeout=3)
    release_runner.set()
    assert entered_reap.wait(timeout=3)

    # Admission must still see the lane as occupied during the store handoff.
    assert manager.active_jobs == 1
    release_reap.set()
    _wait_until(lambda: manager.active_jobs == 0)
    manager.shutdown(wait=True)


def test_scheduler_closed_is_cancelled_without_reclassifying_other_errors() -> None:
    close_manager = PolytaoJobManager()
    closed = _create_polytao_job(
        close_manager,
        lambda: (_ for _ in ()).throw(
            GpuSchedulerClosedError("runtime is shutting down", model_name="polytao")
        ),
    )
    _wait_until(lambda: close_manager.get_job(closed.job_id).status == "cancelled")
    close_manager.shutdown(wait=True)

    failure_started = Event()
    release_failure = Event()
    failure_manager = PolytaoJobManager()
    failed = _create_polytao_job(
        failure_manager,
        lambda: (
            failure_started.set(),
            release_failure.wait(timeout=3),
            (_ for _ in ()).throw(RuntimeError("actual inference failure")),
        )[2],
    )
    assert failure_started.wait(timeout=3)
    failure_manager.shutdown(wait=False)
    release_failure.set()
    _wait_until(lambda: failure_manager.get_job(failed.job_id).status == "failed")
    assert failure_manager.get_job(failed.job_id).error_message == "actual inference failure"


def test_shared_store_exposes_per_lane_retention_and_execution_counts() -> None:
    store = BoundedInMemoryJobStore(instance_id="a" * 16)
    conditional = ConditionalGenerationJobManager(store=store)
    polytao = PolytaoJobManager(store=store)
    request = _conditional_request()
    conditional_job = conditional.create_job(request, lambda: _conditional_result(request))
    polytao_job = _create_polytao_job(polytao, _polytao_result)

    _wait_until(lambda: conditional.get_job(conditional_job.job_id).status == "completed")
    _wait_until(lambda: polytao.get_job(polytao_job.job_id).status == "completed")

    assert conditional.retained_jobs == 1
    assert conditional.retained_bytes > 0
    assert polytao.retained_jobs == 1
    assert polytao.retained_bytes > 0
    assert store.stats().jobs == 2
    assert conditional.active_executions == 0
    assert polytao.active_executions == 0
    conditional.shutdown(wait=True)
    polytao.shutdown(wait=True)


def test_lane_capacity_rejects_before_retaining_a_job() -> None:
    request = _conditional_request()
    conditional_started = Event()
    release_conditional = Event()
    conditional = ConditionalGenerationJobManager(max_active_jobs=1)
    conditional.create_job(
        request,
        lambda: (
            conditional_started.set(),
            release_conditional.wait(timeout=3),
            _conditional_result(request),
        )[2],
    )
    assert conditional_started.wait(timeout=3)
    with pytest.raises(ConditionalGenerationJobCapacityError):
        conditional.create_job(request, lambda: _conditional_result(request))
    assert conditional.retained_jobs == 1

    polytao_started = Event()
    release_polytao = Event()
    polytao = PolytaoJobManager(max_active_jobs=1)
    _create_polytao_job(
        polytao,
        lambda: (
            polytao_started.set(),
            release_polytao.wait(timeout=3),
            _polytao_result(),
        )[2],
    )
    assert polytao_started.wait(timeout=3)
    with pytest.raises(PolytaoJobCapacityError):
        _create_polytao_job(polytao, _polytao_result)
    assert polytao.retained_jobs == 1

    release_conditional.set()
    release_polytao.set()
    conditional.shutdown(wait=True)
    polytao.shutdown(wait=True)
