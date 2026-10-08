"""Real memory managers and PostgreSQL, synthetic model runners only."""
from types import SimpleNamespace as NS
from uuid import uuid4
import pytest
from fastapi import HTTPException
from psycopg.conninfo import make_conninfo
from app.auth.context import Identity,user_context
from app.services.entitlement_repository import EntitlementRepository
from app.services.entitlement_memory import MemorySubmission,get_memory_job,revoke_not_dispatched
from task_usage import entitlements as core
from task_usage.entitlement_policy import example_policy,normalize_spec,qualify
from test_entitlement_repository import enabled,entitlement_db,usage_db,connection
from test_entitlement_memory_jobs import manager_and_submit,finish_manager,response

@pytest.fixture
def api(entitlement_db):
    owner=str(uuid4());p=example_policy();p["version"]="memory-pg-"+uuid4().hex
    with connection(entitlement_db,owner,"nexpoly_service") as active:
        core.install_policy(active,p)
    repo=EntitlementRepository(make_conninfo(entitlement_db,options="-c role=nexpoly_api"))
    req=NS(app=NS(state=NS(entitlement_repository=repo,entitlement_policy=p)))
    settings=NS(application_dsn=repo.dsn,service_dsn=make_conninfo(entitlement_db,options="-c role=nexpoly_service"))
    with user_context(Identity(owner),settings):
        yield NS(owner=owner,policy=p,repo=repo,request=req,dsn=entitlement_db)

def submission(api,module,key="stable-memory-key"):
    spec={"candidate_count":2,"top_k":1,"temperature":1.0,"delta_tg":5.0}
    if module=="polytao":spec={"operation":"generate","candidate_count":2,"temperature":1.,"top_k":1,"top_p":1.,"max_length":16}
    return MemorySubmission(api.repo,api.policy,module,api.owner,key,"a"*64,
                            spec=spec,normalize=normalize_spec,qualify=qualify)

def pool(api):
    summary=api.repo.summary(owner_user_id=api.owner,policy_version=api.policy["version"])
    return summary,next(v for v in summary["pools"] if v["pool_id"]=="generation")

@pytest.mark.parametrize("module",["conditional_generation","polytao"])
def test_real_future_and_sql_result_survive_memory_loss_without_second_execution(api,monkeypatch,module):
    manager,create=manager_and_submit(monkeypatch,module);calls=[]
    try:
        first=create(submission(api,module),lambda:calls.append(1) or response(module))
        finish_manager(manager)
        op=api.repo.get_by_native(owner_user_id=api.owner,module=module,native_task_id=first.job_id)
        assert op["state"]=="succeeded" and op["qualified"] and op["outcome"]=="partial"
        assert op["result_json"]["job_id"]==first.job_id
        def gone(_):raise KeyError(first.job_id)
        restored=get_memory_job(api.request,module,first.job_id,gone)
        assert restored.status=="completed" and restored.result.returned_count==1
        second,again=manager_and_submit(monkeypatch,module)
        try:
            replay=again(submission(api,module),lambda:pytest.fail("same key cannot run model twice"))
            assert replay.job_id==first.job_id
        finally:finish_manager(second)
        summary,balance=pool(api)
        assert (summary["execution_attempts"],balance["success_used"],balance["reserved"])==(1,1,0)
        assert calls==[1]
        with user_context(Identity(str(uuid4()))):
            with pytest.raises(KeyError):get_memory_job(api.request,module,first.job_id,gone)
    finally:finish_manager(manager)

@pytest.mark.parametrize("module",["conditional_generation","polytao"])
def test_failed_commit_never_exposes_success_and_retries_only_known_result(api,monkeypatch,module):
    original=core.settle;calls=[]
    def fail(active,**kwargs):
        original(active,**kwargs)
        raise RuntimeError("synthetic transaction rollback after write")
    monkeypatch.setattr(core,"settle",fail)
    manager,create=manager_and_submit(monkeypatch,module)
    try:
        first=create(submission(api,module),lambda:calls.append(1) or response(module))
        finish_manager(manager)
        pending=manager.get_job(first.job_id)
        assert pending.status=="running" and pending.result is None
        saved=api.repo.get_by_native(owner_user_id=api.owner,module=module,native_task_id=first.job_id)
        assert saved["state"]=="running" and saved["result_json"] is None
        assert (pool(api)[1]["success_used"],pool(api)[1]["reserved"])==(0,1)
        monkeypatch.setattr(core,"settle",original)
        result=manager.get_job(first.job_id)
        assert result.status=="completed" and result.result.returned_count==1
        assert (pool(api)[1]["success_used"],pool(api)[1]["reserved"])==(1,0)
        assert calls==[1]
    finally:finish_manager(manager)

def test_generation_modules_share_one_success_pool(api,monkeypatch):
    api.policy["version"]="memory-one-"+uuid4().hex
    api.policy["tiers"]["member"]["pools"]["generation"]=1
    with connection(api.dsn,api.owner,"nexpoly_service") as active:core.install_policy(active,api.policy)
    one,create=manager_and_submit(monkeypatch,"conditional_generation")
    try:
        create(submission(api,"conditional_generation"),lambda:response("conditional_generation"))
        finish_manager(one)
        two,other=manager_and_submit(monkeypatch,"polytao")
        try:
            with pytest.raises(HTTPException) as error:
                other(submission(api,"polytao","other-logical-task"),lambda:pytest.fail("pool exhausted"))
            assert error.value.status_code==429
        finally:finish_manager(two)
        assert pool(api)[1]["success_used"]==1
    finally:finish_manager(one)

def test_reserved_revocation_fences_late_dispatch_and_unknown_cannot_be_revoked(api):
    handle=submission(api,"polytao");assert handle.bind("synthetic-native-one")
    assert revoke_not_dispatched(api.repo,handle.operation)
    with pytest.raises(core.EntitlementConflict):
        api.repo.begin_dispatch(owner_user_id=api.owner,operation_id=handle.operation["operation_id"],generation=1,executor_id="late")
    second=submission(api,"polytao","another-task");assert second.bind("synthetic-native-two")
    op=api.repo.begin_dispatch(owner_user_id=api.owner,operation_id=second.operation["operation_id"],generation=1,executor_id="unknown")["operation"]
    op=api.repo.mark_unknown(owner_user_id=api.owner,operation_id=op["operation_id"],generation=op["generation"],reason="unknown_process_outcome")["operation"]
    assert not revoke_not_dispatched(api.repo,op)
    assert pool(api)[1]["unknown"]==1
