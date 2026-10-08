from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.auth.context import Identity, user_context
from app.services import usage_background as background
from app.services.usage_lifecycle import record_persistent_task, record_batch_attempt
from app.usage_context import UsageCollector, current_usage_context, usage_scope

OWNER = "11111111-1111-4111-8111-111111111111"
NOW = datetime.now(timezone.utc)


class Repository:
    def __init__(self):
        self.events = []
    def record_event(self, **event):
        self.events.append(event)
        return {"inserted": True}


@pytest.fixture
def meter(monkeypatch):
    monkeypatch.setenv("USAGE_METERING_ENABLED", "true")
    repository = Repository()
    collector = UsageCollector(repository=repository, enabled=True)
    with usage_scope(None, collector=collector):
        yield repository, collector


def test_disabled_does_not_require_identity_collector_or_database(monkeypatch):
    monkeypatch.delenv("USAGE_METERING_ENABLED", raising=False)
    assert background.begin_background_usage(owner_user_id=None, task_type=None,
        task_id=None, channel="backend_gpu") is None
    record_persistent_task(object(), module="online_knowledge", row={})
    record_batch_attempt(object(), {}, {}, status="running")


def test_native_gpu_has_one_root_unknown_physical_start_and_stable_terminal(meter):
    repo, _ = meter
    handle = background.begin_background_usage(owner_user_id=OWNER,
        task_type="conditional_generation", task_id="native1", channel="backend_gpu",
        submitted_at=NOW.isoformat())
    handle.running()
    snapshot = SimpleNamespace(status="completed", finished_at=(NOW+timedelta(seconds=2)).isoformat())
    handle.finish(snapshot)
    handle.finish(snapshot)
    assert [event["status"] for event in repo.events] == ["queued", "running", "completed"]
    assert all(event["task_id"] == "conditional_generation:native1" for event in repo.events)
    assert all(event["root_task_id"] == "conditional_generation:native1" for event in repo.events)
    assert all(event["parent_task_id"] is None for event in repo.events)
    assert all(event["started_at"] is None for event in repo.events)
    assert repo.events[-1]["metrics"] == {"observed_wall_ms": 2000}
    assert "execution_ms" not in repo.events[-1]["metrics"]
    assert repo.events[-1]["attempt_id"].startswith("backend:")


@pytest.mark.parametrize("state,expected", [("found_enough","completed"),("exhausted","completed"),
    ("cancelled","cancelled"),("failed","failed")])
def test_native_terminal_outcomes_preserve_business_meaning(meter,state,expected):
    repo,_ = meter
    handle=background.begin_background_usage(owner_user_id=OWNER,task_type="tg_reverse",
        task_id="native2",channel="reverse",submitted_at=NOW)
    handle.finish(SimpleNamespace(status=state,finished_at=NOW+timedelta(seconds=1)))
    assert repo.events[-1]["module"] == "reverse_design"
    assert repo.events[-1]["status"] == expected


def test_unknown_terminal_time_remains_unfinished(meter):
    repo,_=meter
    handle=background.begin_background_usage(owner_user_id=OWNER,task_type="polytao",
        task_id="native3",channel="backend_gpu",submitted_at=NOW)
    handle.finish(SimpleNamespace(status="running",finished_at=None))
    handle.finish(SimpleNamespace(status="failed",finished_at=None))
    assert [e["status"] for e in repo.events] == ["queued"]


def test_submission_context_survives_http_context_exit(meter):
    from app.task_control import configure_task_control
    from app.services.private_execution import submit_private_job
    configure_task_control(lambda _: True)
    class Executor:
        def submit(self, function):
            self.function = function
            self.future = Future()
            return self.future
    executor=Executor()
    observed=[]
    with user_context(Identity(OWNER)):
        future=submit_private_job(executor,lambda: observed.append(current_usage_context()),
            channel="reverse",on_disabled=lambda: pytest.fail("unexpected disabled"),
            task_type="tg_reverse",task_id="native4",submitted_at=NOW.isoformat())
    try:
        executor.function()
    finally:
        future.set_result(None)
    assert observed[0].owner_user_id == OWNER
    assert observed[0].module == "reverse_design"
    assert observed[0].task_id == "reverse_design:native4"


def test_ledger_failure_is_visible(meter):
    from app.usage_context import UsageMeteringUnavailable
    repo,collector=meter
    def fail(**event):
        raise RuntimeError("private database detail")
    repo.record_event=fail
    with pytest.raises(UsageMeteringUnavailable,match="temporarily unavailable"):
        background.begin_background_usage(owner_user_id=OWNER,task_type="polytao",
            task_id="native5",channel="backend_gpu",submitted_at=NOW)
    assert collector.snapshot()["degraded"] is True


def test_batch_expiry_and_recovery_estimate_are_not_metered_as_duration(monkeypatch):
    from app.services import usage_lifecycle as lifecycle
    monkeypatch.setenv("USAGE_METERING_ENABLED","true")
    events=[]
    monkeypatch.setattr(lifecycle,"record_event",lambda connection,**event:events.append(event))
    row=dict(id="native6",owner_user_id=OWNER,status="completed",created_at=NOW,
        updated_at=NOW+timedelta(seconds=1),finished_at=NOW+timedelta(seconds=1),
        start_authorized_at=NOW,consumed_seconds=234)
    lifecycle.record_persistent_task(object(),module="polymerization_batch",row=row)
    assert events[0]["metrics"] is None
    row["status"]="expired"
    lifecycle.record_persistent_task(object(),module="polymerization_batch",row=row)
    assert len(events)==1


def test_chunk_retry_ordinal_and_unknown_recovery_elapsed(monkeypatch):
    from app.services import usage_lifecycle as lifecycle
    monkeypatch.setenv("USAGE_METERING_ENABLED","true")
    events=[]
    monkeypatch.setattr(lifecycle,"record_event",lambda connection,**event:events.append(event))
    job=dict(id="native7",owner_user_id=OWNER,execution_started_at=NOW)
    chunk=dict(chunk_id="g0-0",attempt=2)
    lifecycle.record_batch_attempt(object(),job,chunk,status="interrupted",finished_at=NOW+timedelta(seconds=1))
    event=events[0]
    assert event["attempt_id"]=="2"
    assert event["kind"]=="execution"
    assert event["parent_task_id"]==event["root_task_id"]=="polymerization_batch:native7"
    assert event["metrics"] is None


@pytest.mark.parametrize("enabled,expected_queries", [(False,1),(True,2)])
def test_online_completion_uses_original_connection_and_disabled_has_no_extra_sql(monkeypatch,enabled,expected_queries):
    from app.services.online_knowledge import postgres_history_repository as online
    monkeypatch.setenv("USAGE_METERING_ENABLED",str(enabled).lower())
    monkeypatch.setattr(online,"_check_storage_quota",lambda *args,**kwargs:None)
    events=[]
    monkeypatch.setattr(online,"record_persistent_task",lambda connection,**event:events.append((connection,event)))
    row=dict(job_id="online1",owner_user_id=OWNER,status="completed",created_at=NOW,
             updated_at=NOW+timedelta(seconds=1),start_authorized_at=NOW)
    class Connection:
        def __init__(self):self.queries=[]
        def execute(self,query,params):
            self.queries.append(query)
            return SimpleNamespace(fetchone=lambda:row)
    connection=Connection()
    online.mark_online_job_completed_postgres(connection,"online1",{},owner_user_id=OWNER)
    assert len(connection.queries)==expected_queries
    assert len(events)==int(enabled)
    if enabled:
        assert events[0][0] is connection
        assert events[0][1]["row"]["owner_user_id"]==OWNER


def test_online_recovery_maps_only_changed_rows_to_interrupted(monkeypatch):
    from app.auth.context import service_context
    from app.services.online_knowledge import postgres_history_repository as online
    monkeypatch.setenv("USAGE_METERING_ENABLED","true")
    events=[]
    monkeypatch.setattr(online,"record_persistent_task",lambda connection,**event:events.append(event))
    rows=[dict(job_id="online2",owner_user_id=OWNER,status="failed",created_at=NOW,
               updated_at=NOW+timedelta(seconds=1),start_authorized_at=NOW)]
    class Connection:
        def execute(self,query):
            assert "WHERE status IN ('pending', 'running') RETURNING" in query
            return SimpleNamespace(rowcount=1,fetchall=lambda:rows)
    with service_context():
        assert online.fail_interrupted_online_jobs_postgres(Connection())==1
    assert len(events)==1 and events[0]["status"]=="interrupted"


def test_online_ledger_failure_propagates_to_callers_transaction(monkeypatch):
    from app.services.online_knowledge import postgres_history_repository as online
    monkeypatch.setenv("USAGE_METERING_ENABLED","true")
    monkeypatch.setattr(online,"_check_storage_quota",lambda *args,**kwargs:None)
    def fail(*args,**kwargs):raise RuntimeError("ledger failed")
    monkeypatch.setattr(online,"record_persistent_task",fail)
    class Connection:
        def __enter__(self):return self
        def __exit__(self,kind,*args):self.rollback_required=kind is not None
        def execute(self,*args):return SimpleNamespace(fetchone=lambda:{"owner_user_id":OWNER})
    connection=Connection()
    with pytest.raises(RuntimeError,match="ledger failed"):
        with connection:
            online.mark_online_job_completed_postgres(connection,"online3",{},owner_user_id=OWNER)
    assert connection.rollback_required


def test_terminal_failure_is_pending_and_native_cleanup_can_continue(meter):
    repo,collector=meter
    handle=background.begin_background_usage(owner_user_id=OWNER,task_type="polytao",
        task_id="terminal_retry",channel="backend_gpu",submitted_at=NOW)
    original=repo.record_event
    def fail(**event):raise RuntimeError("unavailable")
    repo.record_event=fail
    snapshot=SimpleNamespace(status="completed",finished_at=NOW+timedelta(seconds=2))
    handle.finish(snapshot)
    handle.finish(snapshot)
    assert collector.snapshot()["pending_terminal_events"]==1
    assert collector.snapshot()["degraded"] is True
    repo.record_event=original
    background.begin_background_usage(owner_user_id=OWNER,task_type="polytao",
        task_id="next_write",channel="backend_gpu",submitted_at=NOW+timedelta(seconds=3))
    completed=[e for e in repo.events if e["status"]=="completed"]
    assert len(completed)==1
    assert completed[0]["finished_at"]==snapshot.finished_at
    assert completed[0]["metrics"]=={"observed_wall_ms":2000}
    assert collector.snapshot()["pending_terminal_events"]==0


def test_online_background_provider_inherits_persisted_native_task(meter,monkeypatch):
    from app.routers import online_knowledge as online
    repo,collector=meter
    observed=[]
    monkeypatch.setattr(online,"_execute_online_knowledge_job",lambda *args:observed.append(current_usage_context()))
    class Lease:
        released=False
        def release(self):self.released=True
    lease=Lease()
    online._run_online_knowledge_job("job9","unused",None,None,Identity(OWNER),lease,None,collector)
    assert lease.released
    assert observed[0].owner_user_id==OWNER
    assert observed[0].task_id==observed[0].root_task_id=="online_knowledge:job9"
    assert observed[0].attempt_id=="1"
    assert repo.events==[]  # The repository already records its native lifecycle.
