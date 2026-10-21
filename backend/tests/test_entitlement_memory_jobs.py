"""Offline lifecycle regressions; no database, models, provider, or GPU calls."""
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
from datetime import datetime, timezone
from threading import Event, Lock
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from app.auth.context import Identity, user_context
from app.models import (ConditionalGenerationTgRequest, ConditionalGenerationTgResponse,
    ConditionalGenerationCandidate, PolytaoGenerationResponse, PolytaoCandidate,
    ReverseDesignTgRequest, ReverseDesignTgResponse)
from app.services.entitlement_memory import (
    MemorySubmission, ExecutionFenced, get_memory_job, revoke_not_dispatched,
    submission_for_request, _facts)
from task_usage.entitlement_policy import example_policy, load_policy, normalize_spec, qualify
from task_usage.entitlements import EntitlementConflict

OWNER = "00000000-0000-0000-0000-000000000002"
OTHER = "00000000-0000-0000-0000-000000000003"


class Repository:
    """Durable state double shared by independent manager instances."""
    def __init__(self):
        self.rows = {}
        self.lock = Lock()
        self.fail_settle = False
        self.lose_settle_ack = False
        self.refuse_claim = False
        self.settle_calls = 0
        self.dispatch_calls = 0
        self.release_calls = 0

    def lookup(self, *, owner_user_id, module, idempotency_key, request_fingerprint):
        row = self.rows.get((owner_user_id, module, idempotency_key))
        if row is not None and row["request_fingerprint"] != request_fingerprint:
            raise EntitlementConflict()
        return deepcopy(row)

    def reserve(self, **kw):
        with self.lock:
            row = self.lookup(**{k: kw[k] for k in (
                "owner_user_id", "module", "idempotency_key", "request_fingerprint")})
            if row:
                return {"created": False, "operation": row}
            row = dict(operation_id=uuid4().hex, owner_user_id=kw["owner_user_id"],
                module=kw["module"], request_fingerprint=kw["request_fingerprint"],
                policy_version=kw["policy_version"], spec=kw["spec"], generation=1,
                native_task_id=kw["native_task_id"], state="reserved", executor_id=None,
                outcome=None, qualified=False, result_json=None)
            self.rows[(kw["owner_user_id"], kw["module"], kw["idempotency_key"])] = row
            return {"created": True, "operation": deepcopy(row)}

    def _row(self, kw):
        return next(r for r in self.rows.values() if
                    r["operation_id"] == kw["operation_id"] and
                    r["owner_user_id"] == kw["owner_user_id"])

    def begin_dispatch(self, **kw):
        with self.lock:
            row = self._row(kw)
            ok = row["state"] == "reserved" and row["generation"] == kw["generation"]
            if ok:
                row.update(state="dispatching", generation=2, executor_id=kw["executor_id"])
                self.dispatch_calls += 1
            return {"transitioned": ok, "operation": deepcopy(row)}

    def claim_running(self, **kw):
        with self.lock:
            row = self._row(kw)
            ok = not self.refuse_claim and row["state"] == "dispatching" and (
                row["generation"], row["executor_id"]) == (kw["generation"], kw["executor_id"])
            if ok:
                row["state"] = "running"
            return {"transitioned": ok, "operation": deepcopy(row)}

    def settle(self, **kw):
        with self.lock:
            self.settle_calls += 1
            if self.fail_settle:
                raise OSError("synthetic persistence outage")
            row = self._row(kw)
            ok = row["state"] in {"dispatching", "running"} and row["generation"] == kw["generation"]
            if ok:
                row.update(state="succeeded" if kw["qualified"] else "released",
                    outcome=kw["outcome"], qualified=kw["qualified"], result_json=deepcopy(kw["result_json"]))
            if self.lose_settle_ack:
                self.lose_settle_ack = False
                raise OSError("synthetic acknowledgement loss after commit")
            return {"transitioned": ok, "operation": deepcopy(row)}

    def release(self, **kw):
        with self.lock:
            row = self._row(kw)
            proof = kw["proof"]
            safe = row["state"] == "reserved" and proof["kind"] == "not_dispatched"
            safe |= (row["state"] == "dispatching" and
                     proof["kind"] == "dispatch_rejected_before_execution" and
                     proof["executor_id"] == row["executor_id"])
            ok = safe and row["generation"] == kw["generation"]
            if ok:
                self.release_calls += 1
                row.update(state="released", outcome=kw["reason"])
            return {"transitioned": ok, "operation": deepcopy(row)}

    def get_by_native(self, *, owner_user_id, module, native_task_id):
        return deepcopy(next((r for r in self.rows.values() if
            (r["owner_user_id"], r["module"], r["native_task_id"]) ==
            (owner_user_id, module, native_task_id)), None))


@pytest.fixture
def policy():
    return load_policy(example_policy())


def handle(repo, policy, module="conditional_generation", key="test-operation-1", fingerprint="a" * 64):
    spec = {"candidate_count": 2, "top_k": 1, "temperature": 1.0, "delta_tg": 5.0}
    if module == "polytao":
        spec = {"operation": "generate", "candidate_count": 2, "top_k": 1,
                "temperature": 1.0, "top_p": 1.0, "max_length": 16}
    elif module == "reverse_design":
        spec = {"candidate_size": 1}
    return MemorySubmission(repo, policy, module, OWNER, key, fingerprint,
        spec=spec, normalize=normalize_spec, qualify=qualify)


def response(module, count=1):
    if module == "conditional_generation":
        return ConditionalGenerationTgResponse(input_smiles="*CC*", normalized_input_smiles="*CC*",
            delta_tg=5, query_time_ms=1, requested_count=2, returned_count=count, attempts=2,
            results=[ConditionalGenerationCandidate(rank=i+1, generated_smiles="*"+"C"*(i+3)+"*")
                     for i in range(count)])
    if module == "polytao":
        return PolytaoGenerationResponse(prompt="synthetic", query_time_ms=1, requested_count=2,
            returned_count=count, attempts=1, results=[PolytaoCandidate(rank=i+1,
            generated_smiles="C"*(i+2), raw_smiles="C"*(i+2)) for i in range(count)])
    return ReverseDesignTgResponse(target_tg=70, query_time_ms=1,
        candidate_pool_size=0, sampled_candidate_count=0, total=0, results=[])


def manager_and_submit(monkeypatch, module):
    from app.services import conditional_generation_jobs as cg, polytao_jobs as pt, reverse_design_jobs as rd
    source = {"conditional_generation": cg, "polytao": pt, "reverse_design": rd}[module]
    # Exercise real managers and Futures, replacing only authenticated admission
    # (which needs a database); copy the real owner context into the worker.
    def submitter(executor, run, *args, **kwargs):
        context = copy_context()
        return executor.submit(context.run, run, *args)
    monkeypatch.setattr(source, "submit_private_job", submitter)
    if module == "conditional_generation":
        manager = cg.ConditionalGenerationJobManager()
        create = lambda entitlement, runner: manager.create_job(
            ConditionalGenerationTgRequest(smiles="*CC*", delta_tg=5, candidate_count=2, top_k=1),
            runner, entitlement=entitlement)
    elif module == "polytao":
        manager = pt.PolytaoJobManager()
        create = lambda entitlement, runner: manager.create_job(input_smiles=None,
            canonical_smiles=None, prompt="synthetic", requested_count=2,
            runner=runner, entitlement=entitlement)
    else:
        manager = rd.ReverseDesignJobManager()
        create = lambda entitlement, runner: manager.create_job(
            ReverseDesignTgRequest(smiles="*CC*", target_tg=70, candidate_size=1),
            lambda *_args: runner(), entitlement=entitlement)
    return manager, create


def finish_manager(manager):
    # Let the accepted synthetic work finish before asking native shutdown to
    # cancel pending work. Immediate shutdown intentionally cancels reverse jobs.
    manager._executor.shutdown(wait=True, cancel_futures=False)
    manager.shutdown(wait=True, timeout_seconds=2)


def request(repo, policy):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        entitlement_repository=repo, entitlement_policy=policy)), state=SimpleNamespace(), headers={"idempotency-key": "test-operation-1"})


@pytest.mark.parametrize("module", ["conditional_generation", "polytao"])
def test_real_manager_persists_then_retrieves_after_memory_loss(monkeypatch, policy, module):
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "true")
    repo = Repository()
    manager, create = manager_and_submit(monkeypatch, module)
    executions = []
    with user_context(Identity(OWNER)):
        first = create(handle(repo, policy, module), lambda: executions.append(1) or response(module))
        finish_manager(manager)
        assert len(executions) == 1
        operation = next(iter(repo.rows.values()))
        assert operation["state"] == ("released" if module == "reverse_design" else "succeeded")
        assert operation["outcome"] == ("rate_only" if module == "reverse_design" else "partial")
        assert repo.dispatch_calls == 1
        # A different process has no manager entry, but can return the durable typed result.
        def gone(_): raise KeyError(first.job_id)
        restored = get_memory_job(request(repo, policy), module, first.job_id, gone)
        assert restored.result is not None
        assert restored.job_id == first.job_id
    with user_context(Identity(OTHER)):
        with pytest.raises(KeyError):
            get_memory_job(request(repo, policy), module, first.job_id, gone)


@pytest.mark.parametrize("module", ["conditional_generation", "polytao"])
def test_duplicate_reservation_race_never_dispatches_second_job(monkeypatch, policy, module):
    repo = Repository()
    manager, create = manager_and_submit(monkeypatch, module)
    first_handle, second_handle = handle(repo, policy, module), handle(repo, policy, module)
    with user_context(Identity(OWNER)):
        first = create(first_handle, lambda: response(module))
        finish_manager(manager)
        second_manager, second_create = manager_and_submit(monkeypatch, module)
        try:
            second = second_create(second_handle, lambda: pytest.fail("duplicate execution"))
            assert second.job_id == first.job_id
            assert repo.dispatch_calls == 1
        finally:
            finish_manager(second_manager)


@pytest.mark.parametrize("module", ["conditional_generation", "polytao"])
@pytest.mark.parametrize("fault", ["outage", "ack_loss"])
def test_unconfirmed_terminal_is_hidden_and_retry_does_not_compute(monkeypatch, policy, module, fault):
    repo = Repository()
    repo.fail_settle = fault == "outage"
    repo.lose_settle_ack = fault == "ack_loss"
    manager, create = manager_and_submit(monkeypatch, module)
    calls = []
    with user_context(Identity(OWNER)):
        accepted = create(handle(repo, policy, module), lambda: calls.append(1) or response(module))
        finish_manager(manager)
        if fault == "outage":
            pending = manager.get_job(accepted.job_id)
            assert pending.status == "running" and pending.result is None
            assert next(iter(repo.rows.values()))["state"] == "running"
        repo.fail_settle = False
        done = manager.get_job(accepted.job_id)
        assert done.status == "completed" and done.result is not None
        assert len(calls) == 1
        assert next(iter(repo.rows.values()))["qualified"] is True


def test_executor_enqueue_then_throw_cannot_execute_or_hold_quota(policy):
    repo = Repository()
    entitlement = handle(repo, policy)
    assert entitlement.bind("synthetic-job")
    called, worker_done = Event(), Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        def submitter(pool, function, *args, **kwargs):
            def invoke():
                try:
                    function(*args)
                except ExecutionFenced:
                    pass
                finally:
                    worker_done.set()
            pool.submit(invoke)
            raise RuntimeError("synthetic executor failed after enqueue")
        with pytest.raises(RuntimeError):
            entitlement.submit(submitter, executor, lambda: called.set())
        assert worker_done.wait(2)
    assert not called.is_set()
    assert next(iter(repo.rows.values()))["state"] == "released"
    assert repo.release_calls == 1


def test_failed_claim_cannot_execute_or_settle_another_fence(monkeypatch, policy):
    repo = Repository()
    repo.refuse_claim = True
    manager, create = manager_and_submit(monkeypatch, "conditional_generation")
    with user_context(Identity(OWNER)):
        accepted = create(handle(repo, policy), lambda: pytest.fail("unfenced execution"))
        finish_manager(manager)
        assert manager.get_job(accepted.job_id).status == "running"
    assert repo.settle_calls == 0
    assert repo.release_calls == 0
    assert next(iter(repo.rows.values()))["state"] == "dispatching"


@pytest.mark.parametrize("module", ["conditional_generation", "polytao"])
def test_zero_candidates_are_durable_but_not_success(monkeypatch, policy, module):
    repo = Repository()
    manager, create = manager_and_submit(monkeypatch, module)
    with user_context(Identity(OWNER)):
        create(handle(repo, policy, module), lambda: response(module, 0))
        finish_manager(manager)
    operation = next(iter(repo.rows.values()))
    assert operation["state"] == "released"
    assert operation["outcome"] == "no_candidates"
    assert operation["result_json"]["result"]["returned_count"] == 0


def test_result_over_cap_is_explicit_failure_not_success(monkeypatch, policy):
    policy["result_max_bytes"] = 4096
    repo = Repository()
    manager, create = manager_and_submit(monkeypatch, "conditional_generation")
    generated = response("conditional_generation")
    generated.results[0].structure_svg = "x" * 5000
    with user_context(Identity(OWNER)):
        create(handle(repo, policy), lambda: generated)
        finish_manager(manager)
    operation = next(iter(repo.rows.values()))
    assert operation["qualified"] is False
    assert operation["outcome"] == "result_retention_limit"
    assert operation["result_json"]["status"] == "failed"
    assert operation["result_json"]["result"] is None


def test_unknown_missing_memory_is_visible_and_never_released(monkeypatch, policy):
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "true")
    repo = Repository()
    entitlement = handle(repo, policy)
    entitlement.bind("synthetic-job")
    entitlement.operation = repo.begin_dispatch(**{
        "owner_user_id": OWNER, "operation_id": entitlement.operation["operation_id"],
        "generation": 1, "executor_id": entitlement.executor_id})["operation"]
    assert not revoke_not_dispatched(repo, entitlement.operation)
    with user_context(Identity(OWNER)):
        def gone(_): raise KeyError("synthetic-job")
        with pytest.raises(HTTPException) as failure:
            get_memory_job(request(repo, policy), "conditional_generation", "synthetic-job", gone)
    assert failure.value.status_code == 409
    assert failure.value.detail["reservation_held"] is True
    assert repo.release_calls == 0


def test_not_dispatched_recovery_revokes_future_dispatch(policy):
    repo = Repository()
    entitlement = handle(repo, policy)
    entitlement.bind("synthetic-job")
    assert revoke_not_dispatched(repo, entitlement.operation)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(ExecutionFenced):
            entitlement.submit(lambda *args, **kwargs: pytest.fail("revoked submit"),
                               executor, lambda: pytest.fail("revoked execution"))


def test_replay_lookup_precedes_current_policy_and_rejects_changed_request(monkeypatch, policy):
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "true")
    repo = Repository()
    req = request(repo, policy)
    body = ConditionalGenerationTgRequest(smiles="*CC*", delta_tg=5, candidate_count=2, top_k=1)
    with user_context(Identity(OWNER)):
        submission = submission_for_request(req, "conditional_generation", body)
        submission.bind("synthetic-job")
        req.app.state.entitlement_policy = None
        replay = submission_for_request(req, "conditional_generation", body)
        assert replay.replay().job_id == "synthetic-job"
        with pytest.raises(HTTPException) as error:
            submission_for_request(req, "conditional_generation", body.model_copy(update={"delta_tg": 6}))
        assert error.value.status_code == 409


def test_disabled_never_reads_identity_repository_or_headers(monkeypatch):
    monkeypatch.delenv("USAGE_ENTITLEMENTS_ENABLED", raising=False)
    class Untouchable:
        def __getattr__(self, _): pytest.fail("disabled path read state")
    assert submission_for_request(Untouchable(), "conditional_generation", Untouchable()) is None


def test_terminal_get_race_prefers_durable_failure(monkeypatch, policy):
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "true")
    policy["result_max_bytes"] = 4096
    repo = Repository()
    manager, create = manager_and_submit(monkeypatch, "conditional_generation")
    generated = response("conditional_generation")
    generated.results[0].structure_svg = "x" * 5000
    with user_context(Identity(OWNER)):
        accepted = create(handle(repo, policy), lambda: generated)
        finish_manager(manager)
        lookup = repo.get_by_native
        observations = []
        def stale_once(**kw):
            value = lookup(**kw)
            observations.append(1)
            if len(observations) == 1:
                value.update(state="running", result_json=None)
            return value
        monkeypatch.setattr(repo, "get_by_native", stale_once)
        result = get_memory_job(request(repo, policy), "conditional_generation",
                                accepted.job_id, manager.get_job)
        assert result.status == "failed" and result.result is None
        assert result.error == "result_retention_limit"
        assert len(observations) == 2


@pytest.mark.parametrize("module", ["conditional_generation", "polytao"])
def test_proven_native_failure_is_persisted_without_success(monkeypatch, policy, module):
    repo = Repository()
    manager, create = manager_and_submit(monkeypatch, module)
    def fail():
        raise ValueError("synthetic model failure")
    with user_context(Identity(OWNER)):
        create(handle(repo, policy, module), fail)
        finish_manager(manager)
    operation = next(iter(repo.rows.values()))
    assert operation["state"] == "released"
    assert operation["qualified"] is False
    assert operation["result_json"]["status"] == "failed"


@pytest.mark.parametrize("module", ["conditional_generation", "polytao"])
def test_cancelled_queued_future_releases_once_without_execution(monkeypatch, policy, module):
    repo = Repository()
    manager, create = manager_and_submit(monkeypatch, module)
    blocker_entered, unblock = Event(), Event()
    def occupy():
        blocker_entered.set()
        assert unblock.wait(3)
    manager._executor.submit(occupy)
    assert blocker_entered.wait(2)
    try:
        with user_context(Identity(OWNER)):
            accepted = create(handle(repo, policy, module),
                              lambda: pytest.fail("cancelled queued job executed"))
            cancelled = manager.cancel_job(accepted.job_id)
            assert cancelled.status == "cancelled"
        operation = next(iter(repo.rows.values()))
        assert operation["state"] == "released"
        assert operation["outcome"] == "cancelled"
        assert operation["qualified"] is False
        assert repo.settle_calls == 1
    finally:
        unblock.set()
        finish_manager(manager)


def test_policy_out_of_bounds_rejects_before_reservation(monkeypatch, policy):
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "true")
    repo = Repository()
    body = ConditionalGenerationTgRequest(smiles="*CC*", delta_tg=2_000_000)
    with user_context(Identity(OWNER)):
        with pytest.raises(HTTPException) as failure:
            submission_for_request(request(repo, policy), "conditional_generation", body)
    assert failure.value.status_code == 422
    assert failure.value.detail["code"] == "usage_specification_unsupported"
    assert repo.rows == {}


def test_rate_only_reverse_design_does_not_require_key_or_reserve(monkeypatch):
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED","true")
    class Forbidden:
        def __getattribute__(self,name):raise AssertionError("rate-only operation must not access quota identity")
    assert submission_for_request(Forbidden(),"reverse_design",Forbidden()) is None
    marker=object()
    assert get_memory_job(Forbidden(),"reverse_design","native-job",lambda _:marker) is marker
