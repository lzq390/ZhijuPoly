"""Durable native job contracts against disposable PostgreSQL, synthetic inference."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace as NS
from uuid import uuid4
import pytest
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from fastapi import HTTPException
from app.auth.context import Identity,current_owner_id,is_service_context,user_context
from app.models import MonomerRetrosynthesisResponse
from app.services.retrosynthesis_models import ResolveTargetRequest, RetrosynthesisJobRequest
from app.services.retrosynthesis_targets import canonical_target,resolve_target,validate_token_budget
from app.services.retrosynthesis_repository import RetrosynthesisRepository,public_job
from app.services.retrosynthesis_jobs import RetrosynthesisJobManager
from app.services.gpu_runtime_registry import GpuRuntimeRegistry
from app.task_control import configure_task_control

A="10000000-0000-0000-0000-000000000001"
B="10000000-0000-0000-0000-000000000002"
ROOT=Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def retrosynthesis_db():
    base=os.getenv("ISOLATION_TEST_ADMIN_DSN") or os.getenv("USAGE_TEST_ADMIN_DSN")
    if not base:
        pytest.skip("Disposable PostgreSQL 16 administrator DSN required")
    name="retrosynthesis_test_"+uuid4().hex
    literature_role="retro_lit_"+uuid4().hex
    with psycopg.connect(base,autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    dsn=make_conninfo(base,dbname=name)
    try:
        with psycopg.connect(dsn) as c:
            from app.migration_policy import validate_migration_manifest_entries
            entries={entry.version:entry for entry in validate_migration_manifest_entries(ROOT/"backend/migrations/postgres")}
            for migration in sorted((ROOT/"backend/migrations/postgres").glob("*.sql")):
                if migration.name.startswith("0018_"):
                    c.execute("INSERT INTO auth.users(user_id,username,password_hash,must_change_password) VALUES(%s,'retro-a','!',false),(%s,'retro-b','!',false)",(A,B))
                    c.execute("SELECT set_config('nexpoly.legacy_owner',%s,true)",(A,))
                source=migration.read_text()
                if migration.name.startswith("0024_"):
                    source=source.replace("nexpoly_literature_import",literature_role)
                c.execute(source)
                entry=entries[migration.stem]
                c.execute("INSERT INTO governance.schema_migrations(version,checksum) VALUES(%s,%s)",(entry.version,entry.checksum))
        yield dsn
    finally:
        with psycopg.connect(base,autocommit=True) as c:
            c.execute(sql.SQL("DROP DATABASE {} WITH(FORCE)").format(sql.Identifier(name)))
            c.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(literature_role)))


@pytest.fixture
def repo(retrosynthesis_db,monkeypatch):
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED","false")
    monkeypatch.setenv("USAGE_METERING_ENABLED","false")
    configure_task_control(lambda _owner:True)
    with psycopg.connect(retrosynthesis_db) as c:
        c.execute("DELETE FROM retrosynthesis.jobs")
    @contextmanager
    def connect(_dsn):
        with psycopg.connect(retrosynthesis_db,row_factory=dict_row) as c:
            role="nexpoly_service" if is_service_context() else "nexpoly_api"
            c.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            if not is_service_context():
                c.execute("SELECT set_config('app.user_id',%s,true)",(current_owner_id(),))
            yield c
    return RetrosynthesisRepository(retrosynthesis_db,connection_factory=connect)


def body(smiles="CC"):
    return RetrosynthesisJobRequest(smiles=smiles).model_dump(mode="json")


def make_manager(repo,monkeypatch,*,runner=None):
    registry=GpuRuntimeRegistry()
    registry.register("retrosynthesis",enabled=True,loader=object)
    app=NS(state=NS(settings=NS(retro_jobs_enabled=True,retro_model_id="fixture",retro_device="cpu",gpu_async_queue_timeout_seconds=600),gpu_runtime_registry=registry))
    manager=RetrosynthesisJobManager(app,repo)
    if runner:
        monkeypatch.setattr("app.services.retrosynthesis_jobs.predict_monomer_precursors",runner)
    assert manager.start()
    return manager


def finish(manager):
    assert manager.wait_for_futures(tuple(manager._futures.values()),timeout_seconds=10)
    manager.stop_accepting()
    futures=manager.cancel_pending()
    assert manager.wait_for_futures(futures,timeout_seconds=10)
    manager.close_executor(wait=True)


def response(*args,**kwargs):
    return MonomerRetrosynthesisResponse(input_smiles=kwargs["smiles"],canonical_smiles=kwargs["smiles"],target_role=kwargs["target_role"],inferred_target_role=kwargs["target_role"] if kwargs["target_role"]!="auto" else "other",model_id="/internal/fixture",device="cpu",query_time_ms=1,total=0,candidates=[])


@pytest.mark.parametrize("value",["", "*CC", "CC.O", "not-a-molecule"])
def test_target_requires_one_complete_molecule(value):
    with pytest.raises(HTTPException) as exc:
        canonical_target(value)
    assert exc.value.status_code==422


def test_resolve_smiles_never_touches_database_or_model():
    result=resolve_target(None,A,ResolveTargetRequest(source="smiles",smiles="Nc1ccc(N)cc1"),structure_library_enabled=False)
    assert result["status"]=="resolved"
    assert result["target"]["inferred_target_role"]=="diamine"
    assert "<svg" in result["target"]["preview_svg"]


def test_input_token_budget_has_no_truncation(monkeypatch):
    calls=[]
    def tokenize(value,**kwargs):
        calls.append(kwargs)
        return {"input_ids":list(range(513))}
    monkeypatch.setattr("app.services.retrosynthesis_targets._tokenizer",lambda _model:tokenize)
    with pytest.raises(HTTPException) as exc:
        validate_token_budget("CC","fixture")
    assert exc.value.status_code==422
    assert calls==[{"truncation":False,"add_special_tokens":True}]


def test_durable_idempotency_conflict_and_rls(repo):
    with user_context(Identity(A)):
        original,created=repo.create(A,body(),"stable-native-task")
        replay,again=repo.create(A,body(),"stable-native-task")
        assert created and not again and replay["job_id"]==original["job_id"]
        with pytest.raises(HTTPException) as exc:
            repo.create(A,body("CCC"),"stable-native-task")
        assert exc.value.status_code==409
        with repo.connection() as c:
            assert c.execute("SELECT count(*) AS n FROM retrosynthesis.jobs").fetchone()["n"]==1
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("UPDATE retrosynthesis.jobs SET status='running'")
    with user_context(Identity(B)):
        assert repo.get(A,job_id=original["job_id"]) is None
        with pytest.raises(HTTPException) as exc:
            public_job(repo.get(B,job_id=original["job_id"]))
        assert exc.value.status_code==404
        second,_=repo.create(B,body(),"stable-native-task")
        assert second["job_id"]!=original["job_id"]


def test_concurrent_same_identity_creates_one_record(repo):
    def create(_):
        with user_context(Identity(A)):
            row,created=repo.create(A,body(),"concurrent-native-task")
            return row["job_id"],created
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(create,range(6)))
    assert len({value[0] for value in results})==1
    assert sum(value[1] for value in results)==1


def test_persistent_completion_replays_without_model_and_true_role(repo,monkeypatch):
    calls=[]
    def run(*args,**kwargs):
        calls.append(1)
        return response(*args,**kwargs)
    with user_context(Identity(A)):
        manager=make_manager(repo,monkeypatch,runner=run)
        request=body("Nc1ccc(N)cc1")
        request["target_role"]="other"
        first=manager.create(A,request,"completed-native-task")
        finish(manager)
        completed=public_job(repo.get(A,job_id=first["job_id"]))
        assert completed["status"]=="completed"
        assert completed["result"]["target_role"]=="other"
        assert completed["result"]["inferred_target_role"]=="diamine"
        assert "device" not in completed["result"]
        assert completed["result"]["model_id"]=="fixture"
        restarted=make_manager(repo,monkeypatch,runner=lambda **_:pytest.fail("cannot recompute"))
        try:
            assert restarted.create(A,request,"completed-native-task")["job_id"]==first["job_id"]
        finally:
            finish(restarted)
        assert calls==[1]


def test_single_manager_and_restart_recovery_never_replays(repo,monkeypatch):
    with user_context(Identity(A)):
        one=make_manager(repo,monkeypatch)
        second=RetrosynthesisJobManager(one.app,repo)
        assert not second.start()
        finish(second)
        pending,_=repo.create(A,body(),"pending-restart-task")
        queued,_=repo.create(A,body(),"queued-restart-task")
        assert repo.queued(queued,one.instance_id)
        finish(one)
        restarted=make_manager(repo,monkeypatch)
        try:
            assert repo.get(A,job_id=pending["job_id"])["status"]=="interrupted"
            assert repo.get(A,job_id=queued["job_id"])["status"]=="interrupted"
        finally:
            finish(restarted)


def test_running_recovery_is_unknown_and_blocks_new_execution(repo,monkeypatch):
    with user_context(Identity(A)):
        row,_=repo.create(A,body(),"unknown-native-task")
        assert repo.queued(row,"old-executor")
        assert repo.running(row,"old-executor")
        manager=RetrosynthesisJobManager(NS(state=NS(settings=NS(retro_jobs_enabled=True))),repo)
        try:
            assert not manager.start()
            recovered=repo.get(A,job_id=row["job_id"])
            assert recovered["status"]=="unknown" and recovered["finished_at"] is None
            with pytest.raises(HTTPException) as exc:
                manager.create(A,body(),"new-task-blocked")
            assert exc.value.status_code==503
            # Only the original identity-fenced execution can supply a late result.
            assert not repo.finish(row,"another-executor",status="failed",error_code="forged")
            assert repo.finish(row,"old-executor",status="failed",error_code="proven_failure")
        finally:
            finish(manager)


def test_enqueue_then_raise_never_invokes_model(repo,monkeypatch):
    calls=[]
    with user_context(Identity(A)):
        manager=make_manager(repo,monkeypatch,runner=lambda **_:calls.append(1))
        original=manager._executor.submit
        def failing_submit(function,*args,**kwargs):
            original(function,*args,**kwargs)
            raise RuntimeError("lost local enqueue acknowledgement")
        monkeypatch.setattr(manager._executor,"submit",failing_submit)
        with pytest.raises(RuntimeError):
            manager.create(A,body(),"submit-ack-lost-task")
        finish(manager)
        assert calls==[]
        assert repo.get(A,key="submit-ack-lost-task")["status"]=="interrupted"


def test_result_commit_retry_never_recomputes(repo,monkeypatch):
    calls,commits=[],[]
    original=repo.finish
    def flaky(*args,**kwargs):
        if kwargs.get("status")=="completed":
            commits.append(1)
            if len(commits)==1:
                raise psycopg.OperationalError("synthetic commit failure")
        return original(*args,**kwargs)
    monkeypatch.setattr(repo,"finish",flaky)
    with user_context(Identity(A)):
        manager=make_manager(repo,monkeypatch,runner=lambda **kwargs:calls.append(1) or response(**kwargs))
        result=manager.create(A,body(),"commit-retry-native-task")
        finish(manager)
        assert repo.get(A,job_id=result["job_id"])["status"]=="completed"
    assert len(calls)==1 and len(commits)==2


@pytest.mark.parametrize("quota",[False,True])
@pytest.mark.parametrize("metering",[False,True])
def test_usage_and_entitlement_switch_combinations(repo,monkeypatch,quota,metering):
    from app.usage_context import UsageCollector,usage_scope
    from task_usage import entitlements as core
    from task_usage.ledger import record_event
    from task_usage.entitlement_policy import example_policy
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED",str(quota).lower())
    monkeypatch.setenv("USAGE_METERING_ENABLED",str(metering).lower())
    policy=example_policy()
    policy["version"]="retro-test-policy-"+uuid4().hex
    monkeypatch.setattr("task_usage.entitlement_config.configured_policy",lambda:policy)
    class UsageRepository:
        def record_event(self,**event):
            with repo.connection() as c:
                return record_event(c,**event)
    collector=UsageCollector(enabled=metering,repository=UsageRepository())
    with user_context(Identity(A)),usage_scope(None,collector=collector):
        if quota:
            with repo.connection(trusted=True) as c:
                core.install_policy(c,policy)
        manager=make_manager(repo,monkeypatch,runner=response)
        result=manager.create(A,body(),"switch-combination-"+uuid4().hex)
        finish(manager)
        assert repo.get(A,job_id=result["job_id"])["status"]=="completed"
        with repo.connection() as c:
            usage=c.execute("SELECT * FROM usage.attempts WHERE task_id=%s",("retrosynthesis:"+result["job_id"],)).fetchall()
            assert bool(usage)==metering
            if metering:
                assert len(usage)==1 and usage[0]["status"]=="completed" and usage[0]["finished_at"] is not None
            operations=c.execute("SELECT * FROM usage_entitlements.operations WHERE module='retrosynthesis' AND native_task_id=%s",(result["job_id"],)).fetchall()
            assert bool(operations)==quota
            if quota:
                assert len(operations)==1 and operations[0]["state"]=="succeeded"
    assert not collector.snapshot()["degraded"]


def test_http_contract_prepare_confirmation_lookup_and_replay(repo,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers.monomer_retrosynthesis import router
    calls=[]
    with user_context(Identity(A)):
        manager=make_manager(repo,monkeypatch,runner=lambda **kwargs:calls.append(1) or response(**kwargs))
        manager.app.state.settings.retro_model_enabled=True
        manager.app.state.settings.structure_library_enabled=False
        app=FastAPI()
        app.state=manager.app.state
        app.state.retrosynthesis_repository=repo
        app.state.retrosynthesis_job_manager=manager
        app.include_router(router)
        monkeypatch.setattr("app.routers.monomer_retrosynthesis.validate_token_budget",lambda *_:None)
        with TestClient(app) as client:
            resolved=client.post("/api/v1/monomer-retrosynthesis/targets/resolve",json={"source":"smiles","smiles":"CC"})
            assert resolved.status_code==200 and calls==[]
            invalid=client.post("/api/v1/monomer-retrosynthesis/jobs",json={"smiles":"CC.O"},headers={"Idempotency-Key":"http-confirmation-key"})
            assert invalid.status_code==422 and calls==[]
            assert client.post("/api/v1/monomer-retrosynthesis/jobs",json={"smiles":"CC"}).status_code==400
            submitted=client.post("/api/v1/monomer-retrosynthesis/jobs",json={"smiles":"CC"},headers={"Idempotency-Key":"http-confirmation-key"})
            assert submitted.status_code==202,submitted.text
            job_id=submitted.json()["job_id"]
            finish(manager)
            lookup=client.get("/api/v1/monomer-retrosynthesis/jobs/lookup",params={"idempotency_key":"http-confirmation-key"})
            assert lookup.status_code==200 and lookup.json()["job_id"]==job_id
            result=client.get("/api/v1/monomer-retrosynthesis/jobs/"+job_id)
            assert result.status_code==200 and result.json()["status"]=="completed"
            assert client.get("/api/v1/monomer-retrosynthesis/jobs/"+job_id,params={"wait_seconds":21}).status_code==422
            manager.app.state.settings.retro_jobs_enabled=False
            replay=client.post("/api/v1/monomer-retrosynthesis/jobs",json={"smiles":"CC"},headers={"Idempotency-Key":"http-confirmation-key"})
            assert replay.status_code==202 and replay.json()["job_id"]==job_id
            assert calls==[1]
            assert client.post("/api/v1/monomer-retrosynthesis/jobs",json={"smiles":"CCC"},headers={"Idempotency-Key":"http-confirmation-key"}).status_code==409
            assert client.get("/api/v1/monomer-retrosynthesis/status").json()["enabled"] is False
        with user_context(Identity(B)),TestClient(app) as other:
            assert other.get("/api/v1/monomer-retrosynthesis/jobs/"+job_id).status_code==404
            assert other.get("/api/v1/monomer-retrosynthesis/jobs/lookup",params={"idempotency_key":"http-confirmation-key"}).status_code==404


def test_http_replay_survives_saved_source_deletion(repo,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers.monomer_retrosynthesis import router
    with user_context(Identity(A)):
        source={"type":"saved_structure","record_id":str(uuid4()),"label":"Original saved structure"}
        stored=body()
        stored["source"]=source
        row,_=repo.create(A,stored,"deleted-source-replay")
        app=FastAPI()
        app.state.settings=NS(retro_model_enabled=False,retro_jobs_enabled=False)
        app.state.retrosynthesis_repository=repo
        app.include_router(router)
        with TestClient(app) as client:
            request={"smiles":"CC","source":{**source,"label":"Changed name"}}
            replay=client.post("/api/v1/monomer-retrosynthesis/jobs",json=request,headers={"Idempotency-Key":"deleted-source-replay"})
            assert replay.status_code==202,replay.text
            assert replay.json()["job_id"]==str(row["job_id"])


def test_create_rechecks_disabled_and_free_account_with_quotas_off(repo):
    for field,value,code in [("status","disabled","retrosynthesis_account_disabled"),("override_tier","free","retrosynthesis_membership_required")]:
        table="auth.users" if field=="status" else "billing.accounts"
        column="user_id" if field=="status" else "owner_user_id"
        with psycopg.connect(repo.dsn) as c:
            c.execute(sql.SQL("UPDATE {} SET {}=%s WHERE {}=%s").format(sql.Identifier(*table.split(".")),sql.Identifier(field),sql.Identifier(column)),(value,A))
        try:
            with user_context(Identity(A)),pytest.raises(HTTPException) as exc:
                repo.create(A,body(),"authorization-check-"+uuid4().hex)
            assert exc.value.status_code==403 and exc.value.detail["code"]==code
        finally:
            with psycopg.connect(repo.dsn) as c:
                c.execute(sql.SQL("UPDATE {} SET {}=%s WHERE {}=%s").format(sql.Identifier(*table.split(".")),sql.Identifier(field),sql.Identifier(column)),("active" if field=="status" else None,A))
    with user_context(Identity(A)),repo.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM retrosynthesis.jobs").fetchone()["n"]==0


def test_account_disabled_after_dispatch_never_invokes_model(repo,monkeypatch):
    entered,release=Event(),Event()
    calls=[]
    with user_context(Identity(A)):
        manager=make_manager(repo,monkeypatch,runner=lambda **_:calls.append(1))
        def loader():
            entered.set()
            assert release.wait(10)
            return object()
        registry=GpuRuntimeRegistry()
        registry.register("retrosynthesis",enabled=True,loader=loader)
        manager.app.state.gpu_runtime_registry=registry
        result=manager.create(A,body(),"disable-before-start")
        assert entered.wait(5)
        with psycopg.connect(repo.dsn) as c:
            c.execute("UPDATE auth.users SET status='disabled' WHERE user_id=%s",(A,))
        try:
            release.set()
            finish(manager)
            saved=repo.get(A,job_id=result["job_id"])
            assert saved["status"]=="interrupted" and saved["start_authorized_at"] is None
            assert calls==[]
        finally:
            release.set()
            with psycopg.connect(repo.dsn) as c:
                c.execute("UPDATE auth.users SET status='active' WHERE user_id=%s",(A,))


def test_canonical_precursor_multiset_deduplication_preserves_multiplicity():
    from rdkit import Chem
    from app.services.monomer_retrosynthesis import _decode_candidates
    runtime=NS(tokenizer=NS(decode=lambda sequence,**_:sequence))
    candidates=_decode_candidates(output=NS(sequences=["CC.O","O.CC","CC.CC","CC"]),runtime=runtime,target_mol=Chem.MolFromSmiles("CCCCCC"),inferred_role="other")
    assert [item.reactants_smiles for item in candidates]==["CC.O","CC.CC","CC"]


def test_exact_name_resolution_ambiguity_and_no_guess(repo):
    with psycopg.connect(repo.dsn) as c:
        c.execute("INSERT INTO pi.monomer_iupac(smiles,iupac_name) VALUES('CC','retro unique name'),('CCC','retro ambiguous name'),('CCCC','retro ambiguous name') ON CONFLICT(smiles) DO UPDATE SET iupac_name=EXCLUDED.iupac_name")
    with user_context(Identity(A)),repo.connection() as c:
        matched=resolve_target(c,A,ResolveTargetRequest(source="name",name="Retro Unique Name"),structure_library_enabled=False)
        assert matched["status"]=="resolved" and matched["target"]["canonical_smiles"]=="CC"
        choices=resolve_target(c,A,ResolveTargetRequest(source="name",name="retro ambiguous name"),structure_library_enabled=False)
        assert choices["status"]=="needs_clarification" and len(choices["candidates"])==2
        missing=resolve_target(c,A,ResolveTargetRequest(source="name",name="unknown chemical description"),structure_library_enabled=False)
        assert missing["status"]=="needs_clarification" and missing["candidates"]==[]


def test_saved_structure_resolution_respects_owner_and_availability(repo):
    from app.services.structure_library import CreateStructureRecord,save_record
    payload=dict(name="retrosynthesis saved target",smiles="CC",ket={"root":{"nodes":[{"$ref":"mol0"}]},"mol0":{"type":"molecule","atoms":[{"label":"C","location":[0,0,0]}],"bonds":[]}},editor_version="3.8.0",client_request_id=uuid4())
    with user_context(Identity(A)),repo.connection() as c:
        saved=save_record(c,A,CreateStructureRecord(**payload))
        resolved=resolve_target(c,A,ResolveTargetRequest(source="saved_structure",record_id=saved["id"]),structure_library_enabled=True)
        assert resolved["target"]["canonical_smiles"]=="CC" and resolved["target"]["source"]["record_id"]==saved["id"]
        with pytest.raises(HTTPException) as exc:
            resolve_target(c,A,ResolveTargetRequest(source="saved_structure",record_id=saved["id"]),structure_library_enabled=False)
        assert exc.value.status_code==503
    with user_context(Identity(B)),repo.connection() as c:
        with pytest.raises(HTTPException) as exc:
            resolve_target(c,B,ResolveTargetRequest(source="saved_structure",record_id=saved["id"]),structure_library_enabled=True)
        assert exc.value.status_code==404
        page=resolve_target(c,B,ResolveTargetRequest(source="saved_structure",query="retrosynthesis saved"),structure_library_enabled=True)
        assert page["candidates"]==[]


def test_storage_capacity_is_enforced_without_entitlements(repo,monkeypatch):
    monkeypatch.setattr("app.services.retrosynthesis_repository.MAX_OWNER_ROWS",1)
    with user_context(Identity(A)):
        repo.create(A,body(),"capacity-first-task")
        with pytest.raises(HTTPException) as exc:
            repo.create(A,body(),"capacity-second-task")
        assert exc.value.status_code==429 and exc.value.detail["code"]=="retrosynthesis_storage_capacity"


def test_deployment_inventory_counts_unknown_and_rejects_partial_migration(repo):
    from app.auth.context import service_context
    from app.services.deployment_control import count_active_postgres_jobs
    with user_context(Identity(A)):
        for status in ("pending","queued","running","unknown","failed"):
            row,_=repo.create(A,body(),"inventory-"+status)
            with repo.connection(trusted=True) as c:
                c.execute("UPDATE retrosynthesis.jobs SET status=%s,finished_at=CASE WHEN %s='failed' THEN now() END WHERE job_id=%s",(status,status,row["job_id"]))
        with repo.connection(trusted=True) as c:
            summary=count_active_postgres_jobs(c)
            assert summary.active_jobs_schema_version==5 and summary.counts["retrosynthesis"]==4
        with psycopg.connect(repo.dsn,row_factory=dict_row) as c:
            c.execute("DELETE FROM governance.schema_migrations WHERE version='0026_monomer_retrosynthesis_jobs'")
            with pytest.raises(RuntimeError,match="retrosynthesis deployment schema is invalid"):
                count_active_postgres_jobs(c)
            c.rollback()
            c.execute("ALTER TABLE retrosynthesis.jobs DISABLE ROW LEVEL SECURITY")
            with pytest.raises(RuntimeError,match="retrosynthesis deployment schema is invalid"):
                count_active_postgres_jobs(c)
            c.rollback()
            c.execute("DROP TABLE retrosynthesis.jobs")
            with pytest.raises(RuntimeError,match="retrosynthesis deployment schema is invalid"):
                count_active_postgres_jobs(c)
            c.rollback()
