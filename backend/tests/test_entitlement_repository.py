"""Entitlement contracts on a disposable PostgreSQL; no provider/science/runtime DB."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
from pathlib import Path
from uuid import uuid4
import copy,json,hashlib,os
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.conninfo import make_conninfo
import pytest
from task_usage import entitlements as ent
from task_usage.entitlement_policy import example_policy
from app.services.entitlement_schema import validate_entitlement_schema
from app.services.entitlement_repository import EntitlementRepository
from app.auth.entitlement_backup import entitlement_backup_seal
from app.auth.isolation_ledger import validate_application_migration_ledger
from app.migration_policy import validate_migration_manifest_entries
from test_usage_repository import usage_db,ROOT,as_role

@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED","true")

@pytest.fixture(scope="module")
def entitlement_db(usage_db):
    with psycopg.connect(usage_db,row_factory=dict_row) as c:
        c.execute((ROOT/"backend/migrations/postgres"/(ent.MIGRATION_VERSION+".sql")).read_text())
        c.execute("DELETE FROM governance.schema_migrations")
        for entry in validate_migration_manifest_entries(ROOT/"backend/migrations/postgres"):
            c.execute("INSERT INTO governance.schema_migrations(version,checksum) VALUES(%s,%s)",(entry.version,entry.checksum))
    yield usage_db

@pytest.fixture
def owner(entitlement_db):
    value=str(uuid4())
    with psycopg.connect(entitlement_db) as c:
        c.execute("INSERT INTO auth.users(user_id,username,password_hash,must_change_password) VALUES(%s,%s,'!',false)",(value,"ent-owner-"+uuid4().hex))
    return value

@contextmanager
def connection(dsn,owner,role="nexpoly_api"):
    with psycopg.connect(dsn,row_factory=dict_row) as c:
        as_role(c,owner,role)
        yield c

def policy(dsn,owner,*,limit=10,**changes):
    p=example_policy();p["version"]="test-"+uuid4().hex
    p["tiers"]["member"]["pools"]["chemistry_small"]=limit
    p.update(changes)
    with connection(dsn,owner,"nexpoly_service") as c: ent.install_policy(c,p)
    return p

def reserve(c,owner,p,*,key=None,module="predict",spec=None,now=None):
    key=key or uuid4().hex
    return ent.reserve(c,owner_user_id=owner,module=module,idempotency_key=key,
        request_fingerprint=hashlib.sha256(key.encode()).hexdigest(),policy_version=p["version"],
        spec=spec or {"property_count":1},now=now)

def advance(c,op):
    kw=dict(owner_user_id=op["owner_user_id"],operation_id=op["operation_id"],generation=op["generation"],executor_id="executor:"+op["operation_id"])
    dispatch=ent.begin_dispatch(c,**kw);assert dispatch["transitioned"]
    kw["generation"]=dispatch["operation"]["generation"]
    claimed=ent.claim_running(c,**kw);assert claimed["transitioned"]
    assert not ent.claim_running(c,**kw)["transitioned"]
    return claimed["operation"]

def settle(c,op,**kw):
    return ent.settle(c,owner_user_id=op["owner_user_id"],operation_id=op["operation_id"],generation=op["generation"],
        outcome=kw.pop("outcome","service_completed"),qualified=kw.pop("qualified",True),
        result_json=kw.pop("result_json",{"value":1}),**kw)

def test_disabled_hooks_never_touch_connection(monkeypatch):
    monkeypatch.delenv("USAGE_ENTITLEMENTS_ENABLED")
    class Forbidden:
        def __getattribute__(self,name): raise AssertionError("disabled DB access")
    assert ent.reserve(Forbidden(),owner_user_id="bad",module="bad",idempotency_key="",request_fingerprint="",policy_version="",spec={})["disabled"]
    assert ent.get(Forbidden(),owner_user_id="bad",operation_id="bad") is None
    assert ent.record_request(Forbidden(),owner_user_id="bad",module="bad",request_id="bad",phase="received",occurred_at=None)["disabled"]
    assert EntitlementRepository("never-connect").summary(owner_user_id="bad")["enabled"] is False

def test_readiness_exact_and_policy_immutable(entitlement_db,owner):
    p=policy(entitlement_db,owner)
    with connection(entitlement_db,owner,"nexpoly_service") as c:
        assert validate_entitlement_schema(c)["validated"]
        assert validate_application_migration_ledger(c,require_usage=True,require_entitlements=True)[-1]["version"]==ent.MIGRATION_VERSION
        assert not ent.install_policy(c,p)["installed"]
        q=copy.deepcopy(p);q["tiers"]["member"]["pools"]["md"]=9
        with pytest.raises(ent.EntitlementConflict): ent.install_policy(c,q)
    with connection(entitlement_db,owner) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            ent.install_policy(c,{**p,"version":"forbidden-"+uuid4().hex})

def test_free_tier_is_independent_of_quota_flag(entitlement_db,owner,monkeypatch):
    with connection(entitlement_db,owner,"nexpoly_service") as c:
        assert ent.get_owner_tier(c,owner_user_id=owner)=="member"
        ent.set_owner_tier(c,owner_user_id=owner,tier="free")
    monkeypatch.delenv("USAGE_ENTITLEMENTS_ENABLED")
    with connection(entitlement_db,owner) as c:
        assert ent.get_owner_tier(c,owner_user_id=owner)=="member"  # disabled quota hook only
        assert EntitlementRepository().resolve_access_tier(connection=c,owner_user_id=owner)=="free"
        with pytest.raises(psycopg.errors.InsufficientPrivilege): c.execute("UPDATE usage_entitlements.owner_entitlements SET tier='member'")

def test_success_result_and_exact_retry(entitlement_db,owner):
    p=policy(entitlement_db,owner)
    with connection(entitlement_db,owner) as c:
        first=reserve(c,owner,p,key="stable")
        assert first["created"]
        again=reserve(c,owner,{"version":"missing-current-policy"},key="stable",spec={"property_count":999})
        assert not again["created"] and again["operation"]==first["operation"]
        op=advance(c,first["operation"])
        result=settle(c,op)
        assert result["transitioned"] and result["operation"]["state"]=="succeeded"
        assert not settle(c,op)["transitioned"]
        with pytest.raises(ent.EntitlementConflict): settle(c,op,result_json={"value":2})
        s=ent.summary(c,owner_user_id=owner,policy_version=p["version"])
        pool=next(r for r in s["pools"] if r["pool_id"]=="chemistry_small")
        assert (s["operation_count"],s["execution_attempts"],pool["success_used"],pool["reserved"])==(1,1,1,0)
        assert ent.lookup(c,owner_user_id=owner,module="predict",idempotency_key="stable",request_fingerprint=hashlib.sha256(b"stable").hexdigest())["result_json"]=={"value":1}

def test_duplicate_key_different_request_conflicts(entitlement_db,owner):
    p=policy(entitlement_db,owner)
    with connection(entitlement_db,owner) as c:
        reserve(c,owner,p,key="same")
        with pytest.raises(ent.EntitlementConflict):
            ent.lookup(c,owner_user_id=owner,module="predict",idempotency_key="same",request_fingerprint="0"*64)

def test_six_threads_same_key_one_reservation(entitlement_db,owner):
    p=policy(entitlement_db,owner)
    def call(_):
        with connection(entitlement_db,owner) as c: return reserve(c,owner,p,key="concurrent")
    with ThreadPoolExecutor(max_workers=6) as workers: results=list(workers.map(call,range(12)))
    assert sum(r["created"] for r in results)==1
    assert len({r["operation"]["operation_id"] for r in results})==1

def test_six_threads_distinct_requests_exact_capacity(entitlement_db,owner):
    p=policy(entitlement_db,owner,limit=3)
    def call(i):
        try:
            with connection(entitlement_db,owner) as c: return reserve(c,owner,p,key="parallel-"+str(i))["created"]
        except ent.EntitlementLimitExceeded: return False
    with ThreadPoolExecutor(max_workers=6) as workers: results=list(workers.map(call,range(12)))
    assert sum(results)==3

def test_pool_shared_across_modules_and_policy_versions(entitlement_db,owner):
    p=policy(entitlement_db,owner,limit=1);q=policy(entitlement_db,owner,limit=1)
    with connection(entitlement_db,owner) as c: reserve(c,owner,p)
    with connection(entitlement_db,owner) as c:
        with pytest.raises(ent.EntitlementLimitExceeded):
            reserve(c,owner,q,module="retrosynthesis",spec={"num_beams":1,"num_return_sequences":1,"max_new_tokens":16})

def test_unknown_retains_then_proven_result_settles(entitlement_db,owner):
    p=policy(entitlement_db,owner,limit=1)
    with connection(entitlement_db,owner) as c:
        op=advance(c,reserve(c,owner,p)["operation"])
        unknown=ent.mark_unknown(c,owner_user_id=owner,operation_id=op["operation_id"],generation=op["generation"],reason="transport_unknown")["operation"]
        assert unknown["state"]=="unknown"
    with connection(entitlement_db,owner) as c:
        with pytest.raises(ent.EntitlementConflict):
            ent.release(c,owner_user_id=owner,operation_id=op["operation_id"],generation=op["generation"],reason="not_dispatched",proof={"kind":"not_dispatched"})
    with connection(entitlement_db,owner) as c:
        with pytest.raises(ent.EntitlementLimitExceeded): reserve(c,owner,p)
    with connection(entitlement_db,owner) as c: assert settle(c,op)["operation"]["state"]=="succeeded"

def test_unqualified_result_replay_and_native_never_rebound(entitlement_db,owner):
    p=policy(entitlement_db,owner)
    with connection(entitlement_db,owner) as c:
        op=reserve(c,owner,p,key="retained")["operation"]
        bound=ent.bind_native(c,owner_user_id=owner,operation_id=op["operation_id"],generation=1,native_task_id="native-one")["operation"]
        op=advance(c,bound)
        done=settle(c,op,qualified=False,outcome="clarification",result_json={"message":"clarify"})
        assert done["operation"]["state"]=="released"
        assert not reserve(c,owner,p,key="retained")["created"]
        with pytest.raises(ent.EntitlementConflict): ent.bind_native(c,owner_user_id=owner,operation_id=op["operation_id"],generation=2,native_task_id="native-two")
        assert ent.get_by_native(c,owner_user_id=owner,module="predict",native_task_id="native-one")["result_json"]=={"message":"clarify"}

def test_owner_and_default_deny(entitlement_db,owner):
    p=policy(entitlement_db,owner)
    with connection(entitlement_db,owner) as c: op=reserve(c,owner,p)["operation"]
    with connection(entitlement_db,str(uuid4())) as c:
        assert ent.get(c,owner_user_id=owner,operation_id=op["operation_id"]) is None
    with connection(entitlement_db,"") as c: assert not c.execute("SELECT * FROM usage_entitlements.operations").fetchall()

def test_transaction_rollback_keeps_native_result_and_success_atomic(entitlement_db,owner):
    p=policy(entitlement_db,owner)
    with connection(entitlement_db,owner) as c: op=advance(c,reserve(c,owner,p)["operation"])
    with pytest.raises(RuntimeError):
        with connection(entitlement_db,owner) as c:
            c.execute("CREATE TEMP TABLE synthetic_native_result(value integer)")
            c.execute("INSERT INTO synthetic_native_result VALUES(1)")
            settle(c,op)
            raise RuntimeError("synthetic rollback")
    with connection(entitlement_db,owner) as c:
        got=ent.get(c,owner_user_id=owner,operation_id=op["operation_id"])
        assert got["state"]=="running" and got["result_json"] is None

def test_result_byte_limit_keeps_unsettled(entitlement_db,owner):
    p=policy(entitlement_db,owner,result_max_bytes=32)
    with connection(entitlement_db,owner) as c: op=advance(c,reserve(c,owner,p)["operation"])
    with connection(entitlement_db,owner) as c:
        with pytest.raises(ent.EntitlementLimitExceeded): settle(c,op,result_json={"value":"x"*100})
    with connection(entitlement_db,owner) as c: assert ent.get(c,owner_user_id=owner,operation_id=op["operation_id"])["state"]=="running"

def test_month_boundary_and_zero_pools(entitlement_db,owner):
    p=policy(entitlement_db,owner,limit=1)
    jan=datetime(2026,1,31,23,59,59,tzinfo=timezone.utc)
    with connection(entitlement_db,owner) as c:
        reserve(c,owner,p,now=jan)
        reserve(c,owner,p,now=jan+timedelta(seconds=1))
        s=ent.summary(c,owner_user_id=owner,policy_version=p["version"],now=jan)
        assert len(s["pools"])==8
        assert next(x for x in s["pools"] if x["pool_id"]=="chemistry_small")["reserved"]==1

def test_http_requests_execution_and_success_are_distinct(entitlement_db,owner):
    p=policy(entitlement_db,owner)
    at=datetime.now(timezone.utc)
    with connection(entitlement_db,owner) as c:
        op=reserve(c,owner,p)["operation"]
        for code in [200,409,422]:
            rid=str(uuid4())
            args=dict(owner_user_id=owner,module="predict",request_id=rid,phase="received",occurred_at=at)
            assert ent.record_request(c,**args)["inserted"]
            assert not ent.record_request(c,**args)["inserted"]
            ent.record_request(c,owner_user_id=owner,module="predict",request_id=rid,phase="finished",occurred_at=at+timedelta(seconds=1),http_status=code)
        op=advance(c,op);settle(c,op)
        requests=ent.list_request_counts(c,owner_user_id=owner)
        usage=ent.summary(c,owner_user_id=owner,policy_version=p["version"])
        assert requests["request_count"]==3 and requests["finished_count"]==3
        assert usage["operation_count"]==usage["execution_attempts"]==1

def test_projection_direct_and_temp_trigger_attacks_rejected(entitlement_db,owner):
    p=policy(entitlement_db,owner)
    with connection(entitlement_db,owner) as c: op=reserve(c,owner,p)["operation"]
    for nested in [False,True]:
        with connection(entitlement_db,owner) as c:
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                if not nested:
                    c.execute("UPDATE usage_entitlements.operations SET state='released',qualified=false,settled_at=updated_at WHERE operation_id=%s",(op["operation_id"],))
                else:
                    c.execute("CREATE TEMP TABLE attack_scope(i integer)")
                    c.execute("""CREATE FUNCTION pg_temp.ent_attack() RETURNS trigger LANGUAGE plpgsql AS $$
                      BEGIN UPDATE usage_entitlements.operations SET state='released',qualified=false,settled_at=updated_at;
                      RETURN NEW; END; $$""")
                    c.execute("CREATE TRIGGER attack BEFORE INSERT ON attack_scope FOR EACH ROW EXECUTE FUNCTION pg_temp.ent_attack()")
                    c.execute("INSERT INTO attack_scope VALUES(1)")

def test_readiness_and_seal_detect_tamper(entitlement_db,owner):
    p=policy(entitlement_db,owner)
    with connection(entitlement_db,owner) as c: reserve(c,owner,p)
    with psycopg.connect(entitlement_db,row_factory=dict_row) as c:
        before=entitlement_backup_seal(c)
        assert len(before["tables"])==5
        c.execute("ALTER TABLE usage_entitlements.operations DISABLE ROW LEVEL SECURITY")
        with pytest.raises(ValueError): validate_entitlement_schema(c)
        c.rollback()
        assert entitlement_backup_seal(c)==before

def test_active_cap_includes_old_month_other_pool_and_unknown(entitlement_db,owner):
    p=policy(entitlement_db,owner,active_operations_per_owner=1)
    with connection(entitlement_db,owner) as c:
        op=advance(c,reserve(c,owner,p,now=datetime(2025,1,1,tzinfo=timezone.utc))["operation"])
        ent.mark_unknown(c,owner_user_id=owner,operation_id=op["operation_id"],generation=op["generation"],reason="observer_lost")
    with connection(entitlement_db,owner) as c:
        with pytest.raises(ent.EntitlementLimitExceeded):
            reserve(c,owner,p,module="online_knowledge",spec={"max_papers":1,"mode":"synthesis"})

@pytest.mark.parametrize("cap",["rows","bytes"])
def test_storage_admission_cap(entitlement_db,owner,cap):
    changes={"operation_max_rows":1} if cap=="rows" else {"operation_max_bytes":32,"result_max_bytes":32}
    p=policy(entitlement_db,owner,**changes)
    if cap=="rows":
        with connection(entitlement_db,owner) as c:
            op=reserve(c,owner,p)["operation"]
            ent.release(c,owner_user_id=owner,operation_id=op["operation_id"],generation=1,
                reason="not_dispatched",proof={"kind":"not_dispatched"})
    with connection(entitlement_db,owner) as c:
        with pytest.raises(ent.EntitlementLimitExceeded): reserve(c,owner,p)

def test_dispatch_rejection_proof_and_stale_fence(entitlement_db,owner):
    p=policy(entitlement_db,owner)
    with connection(entitlement_db,owner) as c:
        op=reserve(c,owner,p)["operation"]
        dispatched=ent.begin_dispatch(c,owner_user_id=owner,operation_id=op["operation_id"],generation=1,executor_id="only-this-executor")["operation"]
        assert dispatched["generation"]==2
        with pytest.raises(ent.EntitlementConflict):
            ent.claim_running(c,owner_user_id=owner,operation_id=op["operation_id"],generation=1,executor_id="only-this-executor")
        released=ent.release(c,owner_user_id=owner,operation_id=op["operation_id"],generation=2,
            reason="dispatch_rejected",proof={"kind":"dispatch_rejected_before_execution","executor_id":"only-this-executor"})
        assert released["transitioned"] and released["operation"]["state"]=="released"
        assert not ent.release(c,owner_user_id=owner,operation_id=op["operation_id"],generation=2,
            reason="dispatch_rejected",proof={"kind":"dispatch_rejected_before_execution","executor_id":"only-this-executor"})["transitioned"]

@pytest.mark.parametrize("tamper",["checksum","unknown"])
def test_runtime_rejects_changed_or_future_ledger(entitlement_db,tamper):
    with psycopg.connect(entitlement_db,row_factory=dict_row) as c:
        if tamper=="checksum": c.execute("UPDATE governance.schema_migrations SET checksum=%s WHERE version=%s",("0"*64,ent.MIGRATION_VERSION))
        else: c.execute("INSERT INTO governance.schema_migrations(version,checksum) VALUES('9999_unapproved',%s)",("0"*64,))
        with pytest.raises(ValueError): validate_application_migration_ledger(c,require_usage=False,require_entitlements=False)
        c.rollback()

def test_access_tier_missing_migrated_table_fails(entitlement_db,owner):
    with psycopg.connect(entitlement_db,row_factory=dict_row) as c:
        c.execute("ALTER TABLE usage_entitlements.owner_entitlements RENAME TO hidden_owner_entitlements")
        with pytest.raises(ValueError,match="missing"):
            EntitlementRepository().resolve_access_tier(connection=c,owner_user_id=owner)
        c.rollback()

def test_backup_seal_detects_result_content_tamper(entitlement_db,owner):
    p=policy(entitlement_db,owner)
    with connection(entitlement_db,owner) as c: op=advance(c,reserve(c,owner,p)["operation"]);settle(c,op)
    with psycopg.connect(entitlement_db,row_factory=dict_row) as c:
        before=entitlement_backup_seal(c)
        c.execute("ALTER TABLE usage_entitlements.operations DISABLE TRIGGER entitlement_projection_guard")
        c.execute("UPDATE usage_entitlements.operations SET result_json=%s WHERE operation_id=%s",(psycopg.types.json.Jsonb({"value":"tampered"}),op["operation_id"]))
        c.execute("ALTER TABLE usage_entitlements.operations ENABLE TRIGGER entitlement_projection_guard")
        assert entitlement_backup_seal(c)!=before
        c.rollback()
        assert entitlement_backup_seal(c)==before

def test_real_pg16_backup_restore_preserves_all_entitlement_tables(entitlement_db,owner,tmp_path):
    from app.auth.backup import backup_and_verify
    from app.services.structure_library import CreateStructureRecord, save_record
    with connection(entitlement_db,owner) as c:
        saved=save_record(c,owner,CreateStructureRecord(name="combined-backup",smiles="CC",
            ket={"root":{"nodes":[{"$ref":"mol0"}]},"mol0":{"type":"molecule","atoms":[{"label":"C","location":[0,0,0]}],"bonds":[]}},
            editor_version="3.8.0",client_request_id=uuid4()))
    p=policy(entitlement_db,owner)
    with connection(entitlement_db,owner) as c:
        op=advance(c,reserve(c,owner,p)["operation"]);settle(c,op,result_json={"value":"retained"})
        ent.record_request(c,owner_user_id=owner,module="predict",request_id=str(uuid4()),phase="received",occurred_at=datetime.now(timezone.utc))
    name="ent_restore_"+uuid4().hex
    restore=make_conninfo(entitlement_db,dbname=name)
    with psycopg.connect(entitlement_db,autocommit=True) as c: c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        receipt=backup_and_verify(entitlement_db,restore,tmp_path/"backup")
        document=json.loads(receipt.read_text())
        assert document["restore_verified"] and len(document["entitlement_state"]["tables"])==5
        assert document["backup_state"]["structure_library.records"]["rows"]>=1
        with connection(restore,owner) as c:
            got=ent.get(c,owner_user_id=owner,operation_id=op["operation_id"])
            assert got["state"]=="succeeded" and got["result_json"]=={"value":"retained"}
            assert c.execute("SELECT name FROM structure_library.records WHERE id=%s",(saved["id"],)).fetchone()["name"]=="combined-backup"
    finally:
        with psycopg.connect(entitlement_db,autocommit=True) as c: c.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))

def test_public_state_constants_contract():
    assert ent.STATES=={"reserved","dispatching","running","unknown","succeeded","released"}
    assert ent.TERMINAL_STATES=={"succeeded","released"}
    assert ent.TERMINAL_STATES < ent.STATES
    assert ent.MIGRATION_VERSION=="0022_usage_entitlements"
    assert not hasattr(ent,"TERMINAL_MIGRATION_VERSION")

@pytest.mark.parametrize("cadence,at,expected_start,expected_end",[
    ("day","2026-01-31T23:59:59+00:00","2026-01-31T00:00:00+00:00","2026-02-01T00:00:00+00:00"),
    ("week","2026-01-04T23:59:59+00:00","2025-12-29T00:00:00+00:00","2026-01-05T00:00:00+00:00"),
    ("month","2026-01-31T23:59:59+00:00","2026-01-01T00:00:00+00:00","2026-02-01T00:00:00+00:00"),
])
def test_utc_period_boundaries_and_frozen_replay(entitlement_db,owner,cadence,at,expected_start,expected_end):
    p=policy(entitlement_db,owner,period=cadence)
    instant=datetime.fromisoformat(at)
    with connection(entitlement_db,owner) as c:
        op=reserve(c,owner,p,key="frozen",now=instant)["operation"]
        assert op["period_start"]==expected_start and op["period_end"]==expected_end
        replay=reserve(c,owner,{"version":"not-current"},key="frozen",now=instant+timedelta(days=40))
        assert not replay["created"] and replay["operation"]==op
        s=ent.summary(c,owner_user_id=owner,policy_version=p["version"],now=instant)
        assert s["period"]==cadence and s["operation_count"]==1
        assert s["period_start"]==expected_start and s["period_end"]==expected_end

@pytest.mark.parametrize("cadence",["day","week","month"])
def test_same_utc_window_other_policy_version_does_not_reset(entitlement_db,owner,cadence):
    p=policy(entitlement_db,owner,period=cadence,limit=1)
    q=policy(entitlement_db,owner,period=cadence,limit=1)
    at=datetime(2026,3,15,12,tzinfo=timezone.utc)
    with connection(entitlement_db,owner) as c: reserve(c,owner,p,now=at)
    with connection(entitlement_db,owner) as c:
        with pytest.raises(ent.EntitlementLimitExceeded): reserve(c,owner,q,now=at)

def test_cross_cadence_conflict_does_not_reset_success(entitlement_db,owner):
    monthly=policy(entitlement_db,owner,period="month")
    daily=policy(entitlement_db,owner,period="day")
    at=datetime.now(timezone.utc)
    with connection(entitlement_db,owner) as c:
        op=advance(c,reserve(c,owner,monthly,now=at)["operation"]);settle(c,op)
        summary=ent.summary(c,owner_user_id=owner,policy_version=daily["version"],now=at)
        assert summary["admission_blocked_by_period"]
        assert summary["conflicting_periods"][0]["states"]=={"succeeded":1}
    with connection(entitlement_db,owner) as c:
        with pytest.raises(ent.EntitlementConflict,match="existing live admission"):
            reserve(c,owner,daily,now=at)
    next_month=datetime.fromisoformat(op["period_end"])
    with connection(entitlement_db,owner) as c:
        assert reserve(c,owner,daily,now=next_month)["created"]

def test_released_old_window_allows_cadence_switch(entitlement_db,owner):
    monthly=policy(entitlement_db,owner,period="month");daily=policy(entitlement_db,owner,period="day")
    at=datetime.now(timezone.utc)
    with connection(entitlement_db,owner) as c:
        op=reserve(c,owner,monthly,now=at)["operation"]
        ent.release(c,owner_user_id=owner,operation_id=op["operation_id"],generation=1,reason="not_dispatched",proof={"kind":"not_dispatched"})
        assert reserve(c,owner,daily,now=at)["created"]

def test_old_period_unknown_visible_and_account_active_all_windows(entitlement_db,owner):
    p=policy(entitlement_db,owner,period="week",active_operations_per_owner=4)
    with connection(entitlement_db,owner) as c:
        op=advance(c,reserve(c,owner,p,now=datetime(2025,1,1,tzinfo=timezone.utc))["operation"])
        ent.mark_unknown(c,owner_user_id=owner,operation_id=op["operation_id"],generation=op["generation"],reason="observer_lost")
        reserve(c,owner,p,module="online_knowledge",spec={"max_papers":1,"mode":"synthesis"})
        summary=ent.summary(c,owner_user_id=owner,policy_version=p["version"])
        assert summary["operation_count"]==1
        assert summary["active_across_periods"]==2 and summary["unknown_across_periods"]==1
        assert summary["held_outside_current_period"]==1 and summary["active_remaining"]==2
        assert summary["held_periods"][0]["states"]=={"unknown":1}
        pool=next(x for x in summary["pools"] if x["pool_id"]=="chemistry_small")
        assert pool["reserved"]==0 and pool["held_outside_current_period"]==1
        assert not pool["admission_blocked_by_period"]  # expired old window still occupies account active capacity

def test_off_saved_results_readable_and_owner_isolated(entitlement_db,owner,monkeypatch):
    p=policy(entitlement_db,owner)
    with connection(entitlement_db,owner) as c:
        op=advance(c,reserve(c,owner,p)["operation"]);settle(c,op,result_json={"value":"owner-only"})
    monkeypatch.delenv("USAGE_ENTITLEMENTS_ENABLED")
    repo=EntitlementRepository()
    with connection(entitlement_db,owner) as c:
        got=repo.read_saved_operation(connection=c,owner_user_id=owner,operation_id=op["operation_id"])
        assert got["result_json"]=={"value":"owner-only"}
        assert len(repo.read_saved_operations(connection=c,owner_user_id=owner,limit=101))==1
        assert ent.get(c,owner_user_id=owner,operation_id=op["operation_id"]) is None
    other=str(uuid4())
    with connection(entitlement_db,other) as c:
        assert repo.read_saved_operation(connection=c,owner_user_id=owner,operation_id=op["operation_id"]) is None
        assert repo.read_saved_operations(connection=c,owner_user_id=owner)==[]

def test_auth_initialization_without_entitlement_ledger_compatible(entitlement_db,owner,monkeypatch):
    name="ent_preinit_"+uuid4().hex;dsn=make_conninfo(entitlement_db,dbname=name)
    with psycopg.connect(entitlement_db,autocommit=True) as c: c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        monkeypatch.delenv("USAGE_ENTITLEMENTS_ENABLED")
        with psycopg.connect(dsn,row_factory=dict_row) as c:
            repo=EntitlementRepository()
            assert repo.resolve_access_tier(connection=c,owner_user_id=owner)=="member"
            assert repo.read_saved_operation(connection=c,owner_user_id=owner,operation_id=str(uuid4())) is None
            assert repo.read_saved_operations(connection=c,owner_user_id=owner)==[]
    finally:
        with psycopg.connect(entitlement_db,autocommit=True) as c: c.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))

def test_worker_release_contract_exactly_matches_full_manifest():
    from task_usage.entitlement_access import release_contract
    expected=[{"version":entry.version,"checksum":entry.checksum}
              for entry in validate_migration_manifest_entries(ROOT/"backend/migrations/postgres")]
    contract=release_contract()
    assert contract["ledger"]==expected
    assert contract["entitlement_sql_sha256"]==expected[-1]["checksum"]

def test_worker_access_resolver_is_always_on_and_service_can_lock_account(entitlement_db,owner,monkeypatch):
    with connection(entitlement_db,owner,"nexpoly_service") as c:
        assert ent.set_owner_tier(c,owner_user_id=owner,tier="free")["changed"]
        assert not ent.set_owner_tier(c,owner_user_id=owner,tier="free")["changed"]
    monkeypatch.delenv("USAGE_ENTITLEMENTS_ENABLED")
    with connection(entitlement_db,owner,"nexpoly_service") as c:
        c.execute("SELECT user_id FROM auth.users WHERE user_id=%s FOR UPDATE",(owner,))
        assert ent.resolve_access_tier(c,owner_user_id=owner)=="free"
    with connection(entitlement_db,owner) as c:
        assert ent.resolve_access_tier(c,owner_user_id=owner)=="free"
        with pytest.raises(ent.EntitlementPolicyError,match="explicit service/admin"):
            from task_usage import _entitlement_store
            _entitlement_store.set_owner_tier(c,owner_user_id=owner,tier="member")

@pytest.mark.parametrize("invalid",["absent","system"])
def test_tier_configuration_rejects_nonuser_accounts(entitlement_db,invalid):
    target=str(uuid4())
    if invalid=="system":
        with psycopg.connect(entitlement_db) as c:
            c.execute("INSERT INTO auth.users(user_id,username,password_hash,must_change_password,is_system,status) VALUES(%s,%s,'!',false,true,'disabled')",(target,"ent-system-"+uuid4().hex))
    with connection(entitlement_db,target,"nexpoly_service") as c:
        with pytest.raises(ent.EntitlementPolicyError,match="existing non-system"):
            ent.set_owner_tier(c,owner_user_id=target,tier="free")
        assert c.execute("SELECT count(*) AS n FROM usage_entitlements.owner_entitlements WHERE owner_user_id=%s",(target,)).fetchone()["n"]==0

@pytest.mark.parametrize("tamper",["checksum","future","missing_table","rls"])
def test_worker_access_resolver_rejects_claimed_schema_drift(entitlement_db,owner,tamper,monkeypatch):
    monkeypatch.delenv("USAGE_ENTITLEMENTS_ENABLED")
    with psycopg.connect(entitlement_db,row_factory=dict_row) as c:
        if tamper=="checksum": c.execute("UPDATE governance.schema_migrations SET checksum=%s WHERE version=%s",("0"*64,ent.MIGRATION_VERSION))
        elif tamper=="future": c.execute("INSERT INTO governance.schema_migrations(version,checksum) VALUES('9999_unknown',%s)",("0"*64,))
        elif tamper=="missing_table": c.execute("ALTER TABLE usage_entitlements.owner_entitlements RENAME TO hidden_access_tiers")
        else: c.execute("ALTER TABLE usage_entitlements.owner_entitlements DISABLE ROW LEVEL SECURITY")
        with pytest.raises(ent.EntitlementPolicyError): ent.resolve_access_tier(c,owner_user_id=owner)
        c.rollback()

@pytest.mark.parametrize("first",["start","downgrade"])
def test_downgrade_and_first_start_share_existing_account_lock(entitlement_db,owner,first,monkeypatch):
    from threading import Event
    import time
    entered=Event();second_pid=[]
    # Feature-off access remains enforced. Explicit administrative configuration
    # is separate from disabled quota hooks and uses the private service entry.
    from task_usage import _entitlement_store
    monkeypatch.delenv("USAGE_ENTITLEMENTS_ENABLED")
    first_connection=psycopg.connect(entitlement_db,row_factory=dict_row)
    as_role(first_connection,owner,"nexpoly_service")
    first_pid=first_connection.info.backend_pid
    if first=="start":
        first_connection.execute("SELECT user_id FROM auth.users WHERE user_id=%s FOR UPDATE",(owner,))
        assert ent.resolve_access_tier(first_connection,owner_user_id=owner)=="member"
    else:
        _entitlement_store.set_owner_tier(first_connection,owner_user_id=owner,tier="free")
    def second():
        with connection(entitlement_db,owner,"nexpoly_service") as c:
            second_pid.append(c.info.backend_pid);entered.set()
            if first=="start":
                return _entitlement_store.set_owner_tier(c,owner_user_id=owner,tier="free")
            c.execute("SELECT user_id FROM auth.users WHERE user_id=%s FOR UPDATE",(owner,))
            return ent.resolve_access_tier(c,owner_user_id=owner)
    with ThreadPoolExecutor(max_workers=1) as workers:
        future=workers.submit(second)
        try:
            assert entered.wait(3)
            deadline=time.monotonic()+3;blocked=False
            with psycopg.connect(entitlement_db,autocommit=True) as observer:
                while time.monotonic()<deadline:
                    blockers=observer.execute("SELECT pg_blocking_pids(%s)",(second_pid[0],)).fetchone()[0]
                    if first_pid in blockers: blocked=True;break
                    time.sleep(.02)
            assert blocked and not future.done(),"Tier mutation/start must serialize at the existing account row"
            first_connection.commit()
            result=future.result(timeout=5)
            assert result=="free" if first=="downgrade" else result["changed"]
        finally:
            first_connection.close()
    with connection(entitlement_db,owner,"nexpoly_service") as c:
        assert ent.resolve_access_tier(c,owner_user_id=owner)=="free"

@pytest.mark.parametrize("tamper",["library_checksum","library_rls","library_owner_policy","library_worker_grant"])
def test_entitlement_upgrade_keeps_structure_library_guards(entitlement_db,tamper):
    from app.auth.backup import validate_backup_schema
    from app.auth.schema import validate_isolation_schema
    with psycopg.connect(entitlement_db,row_factory=dict_row) as c:
        assert any(p["relation"]=="structure_library.records" for p in validate_isolation_schema(c)["rls_policies"])
        assert validate_backup_schema(c)["migration_ledger"][-1]["version"]==ent.MIGRATION_VERSION
        if tamper=="library_checksum":
            c.execute("UPDATE governance.schema_migrations SET checksum=%s WHERE version='0020_structure_library'",("0"*64,))
        elif tamper=="library_rls":
            c.execute("ALTER TABLE structure_library.records DISABLE ROW LEVEL SECURITY")
        elif tamper=="library_owner_policy":
            c.execute("ALTER POLICY owner_access ON structure_library.records USING(true)")
        else:
            c.execute("GRANT USAGE ON SCHEMA structure_library TO nexpoly_service")
        with pytest.raises(ValueError): validate_backup_schema(c)
        c.rollback()

def test_full_ledger_cannot_omit_structure_library_expansion(entitlement_db):
    with psycopg.connect(entitlement_db,row_factory=dict_row) as c:
        c.execute("DELETE FROM governance.schema_migrations WHERE version='0020_structure_library'")
        with pytest.raises(ValueError):
            validate_application_migration_ledger(c,require_usage=True,require_entitlements=True)
        c.rollback()

def test_current_preflight_full_entitlement_chain_keeps_library_target(entitlement_db,monkeypatch):
    from app.config import Settings
    from app.postgres_preflight import run_preflight,SCHEMA_TARGET_CURRENT_ISOLATION,SCHEMA_TARGET_STRUCTURE_LIBRARY
    suffix=uuid4().hex
    roles={"api":"ent_preflight_api_"+suffix,"service":"ent_preflight_service_"+suffix}
    password="synthetic-"+uuid4().hex
    dsns={kind:make_conninfo(entitlement_db,user=role,password=password) for kind,role in roles.items()}
    future="9999_unapproved_"+suffix
    try:
        with psycopg.connect(entitlement_db) as c:
            for kind,role in roles.items():
                c.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE {}").format(
                    sql.Identifier(role),sql.Literal(password),sql.Identifier("nexpoly_"+kind)))
        settings=Settings(app_postgres_dsn=dsns["api"],pi_postgres_dsn=dsns["api"],
            model_enabled=False,gen_model_enabled=False,retro_model_enabled=False,gpu_broker_enabled=False)
        report=run_preflight(settings,dsn=dsns["service"],mode="schema",strict=True,
            schema_target=SCHEMA_TARGET_CURRENT_ISOLATION)
        assert report["strict_ok"] and report["current_readiness"],report["strict_errors"]
        assert report["requested_schema_target"]==SCHEMA_TARGET_CURRENT_ISOLATION
        assert report["schema_target"]==SCHEMA_TARGET_STRUCTURE_LIBRARY
        assert report["structure_library_schema_ready"] is True
        assert report["migrations"]["required"][-1]=="0020_structure_library"
        assert report["migrations"]["applied"][-1]==ent.MIGRATION_VERSION
        # An unknown suffix must not be treated as a supported newer boundary.
        with psycopg.connect(entitlement_db) as c:
            c.execute("INSERT INTO governance.schema_migrations(version,checksum) VALUES(%s,%s)",(future,"0"*64))
        rejected=run_preflight(settings,dsn=dsns["service"],mode="schema",strict=True,
            schema_target=SCHEMA_TARGET_CURRENT_ISOLATION)
        assert not rejected["strict_ok"] and not rejected["current_readiness"]
        assert "isolation_contract_error" in rejected
        assert not rejected.get("structure_library_schema_ready",False)
    finally:
        with psycopg.connect(entitlement_db) as c:
            c.execute("DELETE FROM governance.schema_migrations WHERE version=%s",(future,))
            for role in roles.values():
                c.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
