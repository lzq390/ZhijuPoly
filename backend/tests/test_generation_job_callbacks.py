"""Real ownerless Future callbacks; no database, model, provider, or GPU calls."""
from collections import defaultdict
from contextlib import nullcontext
from copy import deepcopy
from threading import Event, current_thread

import pytest

from app.auth.context import (
    Identity, current_identity, database_settings, is_service_context,
    service_context, user_context,
)
from app.models import ConditionalGenerationTgRequest
from app.services import conditional_generation_jobs as cg, polytao_jobs as pt, private_execution
from app.services.in_memory_jobs import JobNotFoundError
from app.usage_context import UsageCollector, usage_scope
from task_usage.entitlement_policy import example_policy, load_policy
from test_entitlement_memory_jobs import (
    OWNER, OTHER, Repository as EntitlementRepository,
    handle as entitlement_handle, response,
)


class UsageRepository:
    def __init__(self):
        self.events = {}
        self.calls = []
        self.fail_terminal = False
        self.lose_terminal_ack = False

    def record_event(self, **event):
        self.calls.append(deepcopy(event))
        terminal = event["status"] not in {"queued", "running"}
        if terminal and self.fail_terminal:
            raise OSError("synthetic terminal write outage")
        key = event["owner_user_id"], event["event_key"]
        inserted = key not in self.events
        self.events.setdefault(key, deepcopy(event))
        if terminal and self.lose_terminal_ack:
            self.lose_terminal_ack = False
            raise OSError("synthetic terminal acknowledgement lost after commit")
        return {"inserted": inserted}


class Jobs:
    def __init__(self, monkeypatch, module, *, metering=True, quota=False):
        monkeypatch.setenv("USAGE_METERING_ENABLED", str(metering).lower())
        monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", str(quota).lower())
        # Only the account lookup needs a DB; retain real admission, executor,
        # identity restoration, usage handling, and manager callbacks.
        monkeypatch.setattr(private_execution, "authorize_memory_start", lambda _: True)
        self.module, self.quota = module, quota
        self.settings = object()
        self.usage_repo = UsageRepository()
        self.collector = UsageCollector(repository=self.usage_repo, enabled=metering,
                                        settings=self.settings)
        self.entitlement_repo = EntitlementRepository()
        self.policy = load_policy(example_policy())
        self.manager = (cg.ConditionalGenerationJobManager(max_active_jobs=1)
                        if module == "conditional_generation"
                        else pt.PolytaoJobManager(max_active_jobs=1))
        self.callback_done = defaultdict(Event)
        self.callback_errors = []
        self.callback_contexts = []
        self.finish_contexts = []
        self.handles = []
        self.releases = []
        self.executions = []
        callback, finish = self.manager._on_future_done, self.manager._finish_future

        def observe_callback(job_id, future, *args, **kwargs):
            before = current_identity(required=False)
            try:
                callback(job_id, future, *args, **kwargs)
            except Exception as error:
                self.callback_errors.append(error)
                raise
            finally:
                self.callback_contexts.append((before, current_identity(required=False),
                                               current_thread().name))
                self.callback_done[job_id].set()

        def observe_finish(*args, **kwargs):
            self.finish_contexts.append((current_identity(required=False), database_settings(),
                                         is_service_context()))
            return finish(*args, **kwargs)

        monkeypatch.setattr(self.manager, "_on_future_done", observe_callback)
        monkeypatch.setattr(self.manager, "_finish_future", observe_finish)

    def create(self, runner):
        entitlement = None
        if self.quota:
            entitlement = entitlement_handle(self.entitlement_repo, self.policy,
                self.module, key=f"callback-{len(self.handles)}")
            self.handles.append(entitlement)
        if self.module == "conditional_generation":
            return self.manager.create_job(ConditionalGenerationTgRequest(
                smiles="*CC*", delta_tg=5, candidate_count=2, top_k=1), runner,
                entitlement=entitlement)
        return self.manager.create_job(input_smiles=None, canonical_smiles=None,
            prompt="synthetic", requested_count=2, runner=runner, entitlement=entitlement)

    def complete(self, outcome="completed"):
        entered, release = Event(), Event()
        self.releases.append(release)

        def runner():
            self.executions.append(1)
            entered.set()
            assert release.wait(5), "synthetic runner was not released"
            if outcome == "failed":
                raise ValueError("synthetic generation failed")
            return response(self.module)

        with user_context(Identity(OWNER, session_id="not-retained"), self.settings), \
                usage_scope(None, collector=self.collector):
            accepted = self.create(runner)
            assert entered.wait(5)
            future = self.manager._futures[accepted.job_id]
        assert current_identity(required=False) is None
        release.set()
        assert self.callback_done[accepted.job_id].wait(5)
        future.result(timeout=5)
        return accepted.job_id, future

    def cancel_queued(self, *, cancelling_identity=None):
        entered, release = Event(), Event()
        self.releases.append(release)

        def occupy_executor():
            entered.set()
            assert release.wait(5)

        blocker = self.manager._executor.submit(occupy_executor)
        assert entered.wait(5)
        with user_context(Identity(OWNER), self.settings), usage_scope(None, collector=self.collector):
            accepted = self.create(lambda: pytest.fail("cancelled task must not run"))
            future = self.manager._futures[accepted.job_id]
        # Shutdown cancels pending Futures without an HTTP user context.
        cancellation_scope = (nullcontext() if cancelling_identity is None else
                              user_context(cancelling_identity, object()))
        with cancellation_scope:
            self.manager.cancel_pending()
        assert self.callback_done[accepted.job_id].wait(5)
        assert future.cancelled()
        release.set()
        blocker.result(timeout=5)
        return accepted.job_id, future

    def visible(self, job_id):
        with user_context(Identity(OWNER), self.settings):
            return self.manager.get_job(job_id)

    def assert_other_owner_denied(self, job_id):
        with user_context(Identity(OTHER)), service_context():
            with pytest.raises(JobNotFoundError):
                self.manager.get_job(job_id)
            with pytest.raises(JobNotFoundError):
                self.manager.cancel_job(job_id)

    def close(self):
        for release in self.releases:
            release.set()
        self.manager._executor.shutdown(wait=True, cancel_futures=True)


@pytest.fixture
def jobs(monkeypatch):
    instances = []

    def make(module, **kwargs):
        instance = Jobs(monkeypatch, module, **kwargs)
        instances.append(instance)
        return instance

    yield make
    for instance in instances:
        instance.close()


@pytest.mark.parametrize("module", ["conditional_generation", "polytao"])
@pytest.mark.parametrize("metering", [False, True])
@pytest.mark.parametrize("quota", [False, True])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_ownerless_callbacks_keep_isolation_and_return_the_only_slot(
        jobs, module, metering, quota, outcome):
    job = jobs(module, metering=metering, quota=quota)
    first_id, first = (job.cancel_queued() if outcome == "cancelled"
                       else job.complete(outcome))
    assert job.callback_errors == []
    assert job.manager.active_jobs == job.manager.active_executions == 0
    saved = job.visible(first_id)
    assert saved.status == outcome and saved.finished_at is not None
    assert (saved.result is not None) == (outcome == "completed")
    assert job.manager._store._records[first_id].reapable
    job.assert_other_owner_denied(first_id)
    second_id, second = job.complete()
    assert first.done() and second.done() and job.manager.active_jobs == 0
    assert job.visible(first_id) == saved
    assert job.visible(second_id).result is not None
    job.assert_other_owner_denied(second_id)
    assert len(job.executions) == (1 if outcome == "cancelled" else 2)
    assert all(before is None and after is None for before, after, _ in job.callback_contexts)
    if outcome != "cancelled":
        assert all(thread != "MainThread" for _, _, thread in job.callback_contexts)
    assert all(actor.user_id == OWNER and settings is job.settings and trusted
               for actor, settings, trusted in job.finish_contexts)
    assert all(actor.session_id == "" for actor, _, _ in job.finish_contexts)
    if metering:
        terminal = [e for e in job.usage_repo.events.values() if e["finished_at"] is not None]
        assert [e["status"] for e in terminal] == [outcome, "completed"]
        assert all(e["owner_user_id"] == OWNER and e["started_at"] is None for e in terminal)
    else:
        assert job.usage_repo.events == {}
    if quota:
        assert all(h.settled for h in job.handles)
        assert job.entitlement_repo.settle_calls == 2


@pytest.mark.parametrize("module", ["conditional_generation", "polytao"])
def test_cancellation_callback_restores_owner_settings_then_unrelated_caller_context(jobs, module):
    job = jobs(module)
    cancelling_actor = Identity(OTHER, session_id="other-session")
    job_id, _ = job.cancel_queued(cancelling_identity=cancelling_actor)
    assert job.callback_errors == [] and job.manager.active_jobs == 0
    assert job.callback_contexts == [(cancelling_actor, cancelling_actor, "MainThread")]
    assert job.finish_contexts == [(Identity(OWNER), job.settings, True)]
    assert current_identity(required=False) is None and database_settings() is None
    assert job.visible(job_id).status == "cancelled"
    job.assert_other_owner_denied(job_id)


@pytest.mark.parametrize("module", ["conditional_generation", "polytao"])
@pytest.mark.parametrize("quota", [False, True])
@pytest.mark.parametrize("fault", ["outage", "ack_loss"])
def test_deferred_terminal_retries_identical_payload_without_rerunning(jobs, module, quota, fault):
    job = jobs(module, quota=quota)
    job.usage_repo.fail_terminal = fault == "outage"
    job.usage_repo.lose_terminal_ack = fault == "ack_loss"
    first_id, _ = job.complete()
    assert job.callback_errors == [] and job.manager.active_jobs == 0
    assert job.visible(first_id).status == "completed"
    assert job.manager._store._records[first_id].reapable
    assert job.collector.snapshot()["pending_terminal_events"] == 1
    pending = deepcopy(next(iter(job.collector._pending.values()))[1])
    assert pending["task_id"] == f"{module}:{first_id}"
    job.usage_repo.fail_terminal = False
    job.complete()
    assert len(job.executions) == 2
    assert job.collector.snapshot()["pending_terminal_events"] == 0
    attempts = [e for e in job.usage_repo.calls if e["event_key"] == pending["event_key"]]
    assert len(attempts) == 2 and all(e == pending for e in attempts)
    committed = [e for e in job.usage_repo.events.values() if e["event_key"] == pending["event_key"]]
    assert committed == [pending]
    job.assert_other_owner_denied(first_id)


@pytest.mark.parametrize("module", ["conditional_generation", "polytao"])
@pytest.mark.parametrize("fault", ["outage", "ack_loss"])
def test_unconfirmed_settlement_keeps_result_and_reservation_but_not_execution_slot(jobs, module, fault):
    job = jobs(module, quota=True)
    repo = job.entitlement_repo
    repo.fail_settle = fault == "outage"
    repo.lose_settle_ack = fault == "ack_loss"
    first_id, _ = job.complete()
    handle = job.handles[0]
    assert job.callback_errors == [] and job.manager.active_jobs == 0
    assert not handle.settled and handle._pending is not None
    retained = job.manager._store._records[first_id]
    assert retained.terminal and not retained.reapable and retained.value.result is not None
    assert first_id in job.manager._entitlements
    assert repo.release_calls == 0
    assert repo.settle_calls == 1
    job.assert_other_owner_denied(first_id)
    if fault == "outage":
        hidden = job.visible(first_id)
        assert hidden.status == "running" and hidden.result is None and hidden.finished_at is None
        assert next(iter(repo.rows.values()))["state"] == "running"
        assert not retained.reapable
    repo.fail_settle = False
    done = job.visible(first_id)
    assert done.status == "completed" and done.result is not None
    assert handle.settled and handle._pending is None and retained.reapable
    assert first_id not in job.manager._entitlements
    assert len(job.executions) == 1 and repo.release_calls == 0
    second_id, _ = job.complete()
    assert job.visible(second_id).status == "completed"
    assert len(job.executions) == 2 and job.manager.active_jobs == 0


@pytest.mark.parametrize("module", ["conditional_generation", "polytao"])
def test_retention_cleanup_error_never_leaks_finished_execution_slot(jobs, monkeypatch, module):
    job = jobs(module)

    def fail_retention(*args):
        raise RuntimeError("synthetic retention cleanup failure")

    monkeypatch.setattr(job.manager._store, "mark_reapable", fail_retention)
    first_id, future = job.complete()
    assert future.done() and job.manager.active_jobs == 0
    assert len(job.callback_errors) == 1
    assert job.visible(first_id).status == "completed"
    assert not job.manager._store._records[first_id].reapable
    assert any(e["status"] == "completed" for e in job.usage_repo.events.values())
    job.assert_other_owner_denied(first_id)
