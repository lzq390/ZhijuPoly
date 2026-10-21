"""Private SQL-backed implementation of the public entitlement protocol."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
import hashlib
import json
import re
from uuid import UUID, uuid4
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from task_usage.entitlements import EntitlementConflict, EntitlementLimitExceeded, EntitlementPolicyError
from task_usage.entitlement_policy import load_policy, normalize_spec, policy_sha256

_FIELDS = ["owner_user_id","operation_id","module","key_hash","request_fingerprint","policy_version","tier","pool_id","spec_id","spec","period_start","period_end","generation","native_task_id","executor_id","state","outcome","qualified","result_json","result_ref","created_at","updated_at","settled_at"]
_TERMINAL = {"succeeded","released"}
_MODULE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_OUTCOME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

def _json(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",",":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError, OverflowError):
        raise EntitlementConflict("Noncanonical result or specification", code="invalid_payload") from None

def _text(value, label, maximum=256):
    if not isinstance(value, str) or not 1<=len(value)<=maximum or any(ord(c)<32 for c in value):
        raise EntitlementConflict("Invalid "+label, code="invalid_payload")
    return value

def _owner(value):
    try: return str(UUID(str(value)))
    except (ValueError,TypeError,AttributeError): raise EntitlementConflict("Invalid owner",code="invalid_payload") from None

def _identity(owner_user_id,module,key,request_fingerprint):
    owner=_owner(owner_user_id)
    if not isinstance(module,str) or not _MODULE.fullmatch(module): raise EntitlementConflict("Invalid module",code="invalid_payload")
    _text(key,"idempotency key",256)
    if not isinstance(request_fingerprint,str) or not _HEX.fullmatch(request_fingerprint):
        raise EntitlementConflict("Invalid request fingerprint",code="invalid_payload")
    return owner,hashlib.sha256(key.encode()).hexdigest()

def _rows(connection, statement, params=()):
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(statement,params)
        return [dict(row) for row in cursor] if cursor.description else []

def _one(connection,statement,params=()):
    rows=_rows(connection,statement,params)
    return rows[0] if rows else None

def _dto(row):
    if row is None: return None
    def scalar(v):
        if isinstance(v,UUID): return str(v)
        if isinstance(v,datetime): return v.astimezone(timezone.utc).isoformat()
        return v
    return {name:scalar(row[name]) for name in _FIELDS}

def _time(connection,now=None):
    value=now if now is not None else _one(connection,"SELECT clock_timestamp() AS at")["at"]
    if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise EntitlementConflict("An aware timestamp is required",code="invalid_payload")
    return value.astimezone(timezone.utc)

def _period(at, cadence="month"):
    midnight=at.astimezone(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
    if cadence=="day":
        return midnight,midnight+timedelta(days=1)
    if cadence=="week":
        start=midnight-timedelta(days=midnight.weekday())
        return start,start+timedelta(days=7)
    if cadence!="month":
        raise EntitlementPolicyError("Unsupported entitlement period")
    start=midnight.replace(day=1)
    end=start.replace(year=start.year+1,month=1) if start.month==12 else start.replace(month=start.month+1)
    return start,end


def _lock_owner(connection,owner):
    # New entitlement storage scope only; no auth/account/job locks.
    _rows(connection,"SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("entitlement-owner:"+owner,))

def _lock_pool(connection,owner,pool,start):
    stamp=datetime.fromisoformat(start) if isinstance(start,str) else start
    _rows(connection,"SELECT pg_advisory_xact_lock(hashtextextended('entitlement-pool:'||%s||':'||%s||':'||extract(epoch FROM %s::timestamptz)::text,0))",(owner,pool,stamp))

def get_owner_tier(connection,*,owner_user_id):
    row=_one(connection,"SELECT tier FROM usage_entitlements.owner_entitlements WHERE owner_user_id=%s",(_owner(owner_user_id),))
    return row["tier"] if row else "member"

def get_policy(connection,*,policy_version):
    _text(policy_version,"policy version",128)
    row=_one(connection,"SELECT document,policy_sha256 FROM usage_entitlements.policy_versions WHERE version=%s",(policy_version,))
    if not row: raise EntitlementPolicyError("Frozen policy is not installed")
    try:
        policy=load_policy(row["document"])
        if policy_sha256(policy)!=row["policy_sha256"]: raise EntitlementPolicyError("Policy digest mismatch")
        return policy
    except (ValueError,TypeError) as exc:
        raise EntitlementPolicyError("Installed policy is invalid") from exc

def install_policy(connection,policy):
    try:
        policy=load_policy(policy)
        digest=policy_sha256(policy)
    except (ValueError,TypeError) as exc: raise EntitlementPolicyError("Policy is invalid") from exc
    inserted=_one(connection,"""INSERT INTO usage_entitlements.policy_versions(version,policy_sha256,document)
        VALUES(%s,%s,%s) ON CONFLICT(version) DO NOTHING RETURNING version""",(policy["version"],digest,Jsonb(policy)))
    current=_one(connection,"SELECT policy_sha256,document FROM usage_entitlements.policy_versions WHERE version=%s",(policy["version"],))
    if current["policy_sha256"]!=digest or current["document"]!=policy:
        raise EntitlementConflict("Policy version is already bound",code="policy_version_conflict")
    return {"installed":inserted is not None,"policy_version":policy["version"],"policy_sha256":digest}

def set_owner_tier(connection,*,owner_user_id,tier):
    owner=_owner(owner_user_id)
    if tier not in {"member","free"}: raise EntitlementPolicyError("Unknown entitlement tier")
    authority=_one(connection,"""SELECT
      pg_has_role(current_user,'nexpoly_service','USAGE')
      OR (SELECT rolsuper FROM pg_roles WHERE rolname=current_user)
      OR pg_has_role(current_user,(SELECT relowner FROM pg_class
        WHERE oid='usage_entitlements.owner_entitlements'::regclass),'MEMBER') AS allowed""")
    if not authority["allowed"]:
        raise EntitlementPolicyError("Only an explicit service/admin tier configuration call is allowed")
    # Same existing account lock as first-start authorization; no new auth GRANT.
    # Admission/settlement never takes this auth lock.
    account=_one(connection,"SELECT user_id,is_system FROM auth.users WHERE user_id=%s FOR UPDATE",(owner,))
    if account is None or account["is_system"]:
        raise EntitlementPolicyError("Tier configuration requires an existing non-system account")
    _lock_owner(connection,owner)
    row=_one(connection,"""INSERT INTO usage_entitlements.owner_entitlements(owner_user_id,tier)
      VALUES(%s,%s) ON CONFLICT(owner_user_id) DO UPDATE SET tier=EXCLUDED.tier,updated_at=clock_timestamp()
      WHERE usage_entitlements.owner_entitlements.tier<>EXCLUDED.tier RETURNING tier""",(owner,tier))
    return {"changed":row is not None,"tier":tier}

def lookup(connection,*,owner_user_id,module,idempotency_key,request_fingerprint):
    owner,key_hash=_identity(owner_user_id,module,idempotency_key,request_fingerprint)
    row=_one(connection,"SELECT * FROM usage_entitlements.operations WHERE owner_user_id=%s AND module=%s AND key_hash=%s",(owner,module,key_hash))
    if row and row["request_fingerprint"]!=request_fingerprint:
        raise EntitlementConflict("The idempotency key belongs to a different request")
    return _dto(row)

def get(connection,*,owner_user_id,operation_id):
    return _dto(_one(connection,"SELECT * FROM usage_entitlements.operations WHERE owner_user_id=%s AND operation_id=%s",(_owner(owner_user_id),_owner(operation_id))))

def get_by_native(connection,*,owner_user_id,module,native_task_id):
    return _dto(_one(connection,"SELECT * FROM usage_entitlements.operations WHERE owner_user_id=%s AND module=%s AND native_task_id=%s",(_owner(owner_user_id),module,_text(native_task_id,"native task ID"))))

def _emit(connection, operation, *, action, generation, proof=None):
    payload={"operation":operation}
    if proof is not None: payload["proof"]=proof
    _json(payload)
    event_key=action+":"+operation["operation_id"]+":"+str(generation)
    try:
        inserted=_one(connection,"""INSERT INTO usage_entitlements.operation_events
          (owner_user_id,operation_id,event_key,action,expected_generation,payload,payload_sha256)
          VALUES(%s,%s,%s,%s,%s,%s,encode(sha256(convert_to(%s::jsonb::text,'UTF8')),'hex'))
          ON CONFLICT(owner_user_id,event_key) DO NOTHING RETURNING event_key""",
          (operation["owner_user_id"],operation["operation_id"],event_key,action,generation,Jsonb(payload),Jsonb(payload)))
    except Exception as exc:
        if getattr(exc,"sqlstate",None)=="P0001":
            raise EntitlementLimitExceeded("Entitlement pool or storage capacity exhausted") from exc
        if getattr(exc,"sqlstate",None) in {"23514","23505","55000"}:
            raise EntitlementConflict("Entitlement transition was rejected",code="invalid_transition") from exc
        raise
    if inserted is None:
        prior=_one(connection,"SELECT payload FROM usage_entitlements.operation_events WHERE owner_user_id=%s AND event_key=%s",(operation["owner_user_id"],event_key))
        if not prior or prior["payload"]!=payload: raise EntitlementConflict("Conflicting transition replay",code="transition_conflict")
    return inserted is not None

def reserve(connection,*,owner_user_id,module,idempotency_key,request_fingerprint,policy_version,spec,native_task_id=None,now=None):
    owner,key_hash=_identity(owner_user_id,module,idempotency_key,request_fingerprint)
    # Before policy/capacity: a retained old key remains valid across policy changes.
    existing=lookup(connection,owner_user_id=owner,module=module,idempotency_key=idempotency_key,request_fingerprint=request_fingerprint)
    if existing: return {"created":False,"operation":existing}
    _lock_owner(connection,owner)
    existing=lookup(connection,owner_user_id=owner,module=module,idempotency_key=idempotency_key,request_fingerprint=request_fingerprint)
    if existing: return {"created":False,"operation":existing}
    policy=get_policy(connection,policy_version=policy_version)
    try: normalized=normalize_spec(policy,module,spec)
    except (ValueError,TypeError) as exc: raise EntitlementPolicyError("Specification is not allowed") from exc
    if normalized["mode"]!="count" or not normalized["pool_id"]:
        raise EntitlementPolicyError("This module/specification is not success-counted")
    at=_time(connection,now);start,end=_period(at,policy["period"])
    _lock_pool(connection,owner,normalized["pool_id"],start)
    overlap=_one(connection,"""SELECT operation_id FROM usage_entitlements.operations
      WHERE owner_user_id=%s AND pool_id=%s AND state<>'released'
        AND period_end>%s AND period_start<%s AND period_end>%s
        AND (period_start<>%s OR period_end<>%s) LIMIT 1""",
        (owner,normalized["pool_id"],at,end,start,start,end))
    if overlap:
        raise EntitlementConflict("An existing live admission window prevents changing the pool period",
                                  code="period_policy_conflict")
    operation=dict(owner_user_id=owner,operation_id=str(uuid4()),module=module,key_hash=key_hash,
        request_fingerprint=request_fingerprint,policy_version=policy_version,
        tier=get_owner_tier(connection,owner_user_id=owner),pool_id=normalized["pool_id"],
        spec_id=normalized["spec_id"],spec=normalized["canonical_spec"],
        period_start=start.isoformat(),period_end=end.isoformat(),generation=1,
        native_task_id=None if native_task_id is None else _text(native_task_id,"native task ID"),
        executor_id=None,state="reserved",outcome=None,qualified=None,result_json=None,result_ref=None,
        created_at=at.isoformat(),updated_at=at.isoformat(),settled_at=None)
    _emit(connection,operation,action="reserve",generation=0)
    return {"created":True,"operation":get(connection,owner_user_id=owner,operation_id=operation["operation_id"])}

def _generation(value):
    if type(value) is not int or value<1: raise EntitlementConflict("Invalid generation",code="stale_generation")
    return value

def transition(connection,*,owner_user_id,operation_id,generation,action,**fields):
    owner,operation_id=_owner(owner_user_id),_owner(operation_id); generation=_generation(generation)
    _lock_owner(connection,owner)
    old=get(connection,owner_user_id=owner,operation_id=operation_id)
    if old is None: raise EntitlementConflict("Operation is not available",code="operation_not_found")
    _lock_pool(connection,owner,old["pool_id"],old["period_start"])
    row=_one(connection,"SELECT * FROM usage_entitlements.operations WHERE owner_user_id=%s AND operation_id=%s FOR UPDATE",(owner,operation_id))
    old=_dto(row)
    # Exact repeats return an observation, never permission to re-execute.
    if action=="bind_native" and old["native_task_id"]==fields.get("native_task_id"):
        return {"transitioned":False,"operation":old}
    if action=="begin_dispatch" and old["executor_id"]==fields.get("executor_id") and old["state"]!="reserved" and generation in {old["generation"],old["generation"]-1}:
        return {"transitioned":False,"operation":old}
    if action=="claim_running" and old["executor_id"]==fields.get("executor_id") and old["state"] in {"running","unknown","succeeded","released"} and generation==old["generation"]:
        return {"transitioned":False,"operation":old}
    if old["generation"]!=generation: raise EntitlementConflict("Stale generation",code="stale_generation")
    if action=="settle" and old["state"] in _TERMINAL:
        expected={key:fields.get(key) for key in ("outcome","qualified","result_json","result_ref")}
        if all(old[key]==value for key,value in expected.items()):
            return {"transitioned":False,"operation":old}
        raise EntitlementConflict("Conflicting terminal result",code="transition_conflict")
    if action=="release" and old["state"]=="released" and old["outcome"]==fields.get("reason"):
        prior=_one(connection,"SELECT payload FROM usage_entitlements.operation_events WHERE owner_user_id=%s AND event_key=%s",
            (owner,"release:"+operation_id+":"+str(generation)))
        if prior and prior["payload"].get("proof")==fields.get("proof"):
            return {"transitioned":False,"operation":old}
        raise EntitlementConflict("Conflicting release proof",code="transition_conflict")
    if action=="mark_unknown" and old["state"]=="unknown" and old["outcome"]==fields.get("reason"):
        return {"transitioned":False,"operation":old}
    if old["state"] in _TERMINAL: raise EntitlementConflict("Operation is terminal",code="invalid_transition")
    operation=dict(old)
    at=_time(connection).isoformat()
    operation["updated_at"]=max(at,old["updated_at"])
    proof=None
    if action=="bind_native":
        if old["native_task_id"] is not None or old["state"]!="reserved": raise EntitlementConflict("Native identity cannot be rebound",code="native_binding_conflict")
        operation["native_task_id"]=_text(fields["native_task_id"],"native task ID")
    elif action=="begin_dispatch":
        if old["state"]!="reserved": raise EntitlementConflict("Operation cannot be dispatched",code="invalid_transition")
        operation.update(state="dispatching",generation=generation+1,executor_id=_text(fields["executor_id"],"executor ID"))
    elif action=="claim_running":
        if old["state"]!="dispatching" or old["executor_id"]!=fields.get("executor_id"): raise EntitlementConflict("Execution fence mismatch",code="invalid_transition")
        operation["state"]="running"
    elif action=="mark_unknown":
        reason=_text(fields["reason"],"reason",64)
        if not _OUTCOME.fullmatch(reason): raise EntitlementConflict("Invalid reason",code="invalid_payload")
        operation.update(state="unknown",outcome=reason)
    elif action=="settle":
        outcome=fields["outcome"]; qualified=fields["qualified"]
        if type(qualified) is not bool or not isinstance(outcome,str) or not _OUTCOME.fullmatch(outcome) or outcome in {"unknown","pending","running","cancel_requested"}:
            raise EntitlementConflict("Settlement needs a qualified terminal outcome",code="invalid_payload")
        if qualified and old["state"] not in {"running","unknown"}: raise EntitlementConflict("Successful execution was never claimed",code="invalid_transition")
        if qualified and fields.get("result_json") is None and fields.get("result_ref") is None: raise EntitlementConflict("Successful result must be retained",code="result_required")
        if old["state"]=="unknown" and fields.get("result_json") is None and fields.get("result_ref") is None: raise EntitlementConflict("Unknown execution requires a proven terminal result",code="result_required")
        operation.update(state="succeeded" if qualified else "released",outcome=outcome,qualified=qualified,
            result_json=fields.get("result_json"),result_ref=fields.get("result_ref"),settled_at=operation["updated_at"])
    elif action=="release":
        reason=_text(fields["reason"],"reason",64)
        if not _OUTCOME.fullmatch(reason): raise EntitlementConflict("Invalid reason",code="invalid_payload")
        proof=fields.get("proof")
        if not isinstance(proof,dict): raise EntitlementConflict("A structured termination proof is required",code="release_proof_required")
        operation.update(state="released",outcome=reason,qualified=False,settled_at=operation["updated_at"])
    else: raise EntitlementConflict("Unknown transition",code="invalid_transition")
    changed=_emit(connection,operation,action=action,generation=generation,proof=proof)
    return {"transitioned":changed,"operation":get(connection,owner_user_id=owner,operation_id=operation_id)}

def list_operations(connection,*,owner_user_id,module=None,limit=50,offset=0):
    if type(limit) is not int or not 1<=limit<=200 or type(offset) is not int or not 0<=offset<=100000: raise ValueError("Invalid pagination")
    owner=_owner(owner_user_id);args=[owner];where="owner_user_id=%s"
    if module is not None: where+=" AND module=%s";args.append(module)
    return [_dto(row) for row in _rows(connection,"SELECT * FROM usage_entitlements.operations WHERE "+where+" ORDER BY created_at DESC,operation_id LIMIT %s OFFSET %s",(*args,limit,offset))]

def summary(connection,*,owner_user_id,policy_version=None,now=None):
    owner=_owner(owner_user_id);tier=get_owner_tier(connection,owner_user_id=owner)
    policy=get_policy(connection,policy_version=policy_version) if policy_version else None
    cadence=policy["period"] if policy else "month"
    at=_time(connection,now);start,end=_period(at,cadence)
    rows=_rows(connection,"""SELECT pool_id,state,period_start,period_end,count(*) AS n
      FROM usage_entitlements.operations WHERE owner_user_id=%s
      GROUP BY pool_id,state,period_start,period_end""",(owner,))
    groups={};active_by_pool={};unknown_by_pool={};outside_by_pool={};held={};conflicts={}
    for row in rows:
        pool,state,count=row["pool_id"],row["state"],int(row["n"])
        same=row["period_start"]==start and row["period_end"]==end
        key=(pool,row["period_start"].isoformat(),row["period_end"].isoformat())
        if same:
            groups.setdefault(pool,{})[state]=groups.setdefault(pool,{}).get(state,0)+count
        if state not in _TERMINAL:
            active_by_pool[pool]=active_by_pool.get(pool,0)+count
            if state=="unknown": unknown_by_pool[pool]=unknown_by_pool.get(pool,0)+count
            if not same:
                outside_by_pool[pool]=outside_by_pool.get(pool,0)+count
                held.setdefault(key,{})[state]=count
        # A different still-live admission window cannot be reset by switching cadence.
        if not same and state!="released" and row["period_end"]>at and row["period_start"]<end and row["period_end"]>start:
            conflicts.setdefault(key,{})[state]=count
    limits=policy["tiers"][tier]["pools"] if policy else {pool:None for pool in set(groups)|set(active_by_pool)}
    def periods(source,pool=None):
        return [dict(pool_id=k[0],period_start=k[1],period_end=k[2],states=states,
                     reserved=sum(v for s,v in states.items() if s not in _TERMINAL),
                     unknown=states.get("unknown",0))
                for k,states in sorted(source.items()) if pool is None or k[0]==pool]
    pools=[]
    for pool,limit in limits.items():
        states=groups.get(pool,{})
        success=states.get("succeeded",0)
        reserved=sum(v for k,v in states.items() if k not in _TERMINAL)
        blocked=periods(conflicts,pool)
        pools.append(dict(pool_id=pool,limit=limit,success_used=success,reserved=reserved,
            unknown=states.get("unknown",0),remaining=None if limit is None else max(0,limit-success-reserved),states=states,
            active_across_periods=active_by_pool.get(pool,0),unknown_across_periods=unknown_by_pool.get(pool,0),
            held_outside_current_period=outside_by_pool.get(pool,0),
            admission_blocked_by_period=bool(blocked),conflicting_periods=blocked))
    executed=_one(connection,"""SELECT count(*) AS n FROM usage_entitlements.operation_events e
      JOIN usage_entitlements.operations o USING(owner_user_id,operation_id)
      WHERE e.owner_user_id=%s AND o.period_start=%s AND o.period_end=%s AND e.action='claim_running'""",(owner,start,end))["n"]
    active=sum(active_by_pool.values())
    active_limit=policy["active_operations_per_owner"] if policy else None
    return dict(enabled=True,policy_version=policy_version,tier=tier,period=cadence,
        period_start=start.isoformat(),period_end=end.isoformat(),
        pools=pools,operation_count=sum(sum(s.values()) for s in groups.values()),execution_attempts=int(executed),
        active_across_periods=active,unknown_across_periods=sum(unknown_by_pool.values()),
        active_limit=active_limit,active_remaining=None if active_limit is None else max(0,active_limit-active),
        held_outside_current_period=sum(outside_by_pool.values()),held_periods=periods(held),
        admission_blocked_by_period=bool(conflicts),conflicting_periods=periods(conflicts))


def record_request(connection,*,owner_user_id,module,request_id,phase,occurred_at,http_status=None,outcome=None,operation_id=None):
    owner=_owner(owner_user_id);request_id=_owner(request_id)
    if not isinstance(module,str) or not _MODULE.fullmatch(module) or phase not in {"received","finished"}: raise ValueError("Invalid request observation")
    at=_time(connection,occurred_at)
    if phase=="received" and (http_status is not None or outcome is not None): raise ValueError("Received is not a terminal observation")
    if http_status is not None and (type(http_status) is not int or not 100<=http_status<=599): raise ValueError("Invalid HTTP status")
    if outcome is not None and (not isinstance(outcome,str) or not _OUTCOME.fullmatch(outcome)): raise ValueError("Invalid request outcome")
    if operation_id is not None:
        operation_id=_owner(operation_id)
        operation=get(connection,owner_user_id=owner,operation_id=operation_id)
        if operation is None or operation["module"]!=module: raise EntitlementConflict("Request operation is not owned",code="operation_not_found")
    values=dict(owner_user_id=owner,request_id=request_id,phase=phase,module=module,operation_id=operation_id,
        http_status=http_status,outcome=outcome,occurred_at=at)
    inserted=_one(connection,"""INSERT INTO usage_entitlements.request_observations
        (owner_user_id,request_id,phase,module,operation_id,http_status,outcome,occurred_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(owner_user_id,request_id,phase) DO NOTHING RETURNING request_id""",tuple(values.values()))
    row=_one(connection,"SELECT * FROM usage_entitlements.request_observations WHERE owner_user_id=%s AND request_id=%s AND phase=%s",(owner,request_id,phase))
    for key,value in values.items():
        observed=str(row[key]) if isinstance(row[key],UUID) else row[key]
        if observed!=value: raise EntitlementConflict("Conflicting request observation",code="request_observation_conflict")
    return {"inserted":inserted is not None}

def list_request_counts(connection,*,owner_user_id,start=None,end=None,module=None,policy_version=None):
    owner=_owner(owner_user_id)
    if start is None or end is None:
        cadence=get_policy(connection,policy_version=policy_version)["period"] if policy_version else "month"
        a,b=_period(_time(connection),cadence);start=start or a;end=end or b
    if not isinstance(start,datetime): start=datetime.fromisoformat(start)
    if not isinstance(end,datetime): end=datetime.fromisoformat(end)
    start=_time(connection,start);end=_time(connection,end)
    if start>=end: raise ValueError("Invalid request observation period")
    where="owner_user_id=%s";args=[owner]
    if module is not None: where+=" AND module=%s";args.append(module)
    rows=_rows(connection,"""WITH requests AS (
      SELECT request_id,min(occurred_at) FILTER(WHERE phase='received') AS received_at,
        min(occurred_at) AS observed_at,max(http_status) FILTER(WHERE phase='finished') AS http_status,
        bool_or(phase='finished') AS finished
      FROM usage_entitlements.request_observations WHERE """+where+"""
      GROUP BY request_id)
      SELECT count(*) AS requests,count(*) FILTER(WHERE finished) AS finished,http_status
      FROM requests WHERE coalesce(received_at,observed_at)>=%s AND coalesce(received_at,observed_at)<%s
      GROUP BY http_status""",(*args,start,end))
    return dict(enabled=True,request_count=sum(int(r["requests"]) for r in rows),
        finished_count=sum(int(r["finished"]) for r in rows),
        by_status={str(r["http_status"]):int(r["requests"]) for r in rows if r["http_status"] is not None},
        start=start.isoformat(),end=end.isoformat())
