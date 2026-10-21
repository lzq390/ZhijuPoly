from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.auth.context import Identity, user_context
from app.entitlement_middleware import EntitlementAccessMiddleware, RequestRateGuard
from app.services.entitlement_sync import entitled_sync, _facts
from task_usage import entitlements as core
from task_usage.entitlement_policy import load_policy

OWNER = "00000000-0000-4000-8000-000000000011"
OTHER = "00000000-0000-4000-8000-000000000012"
class Body(BaseModel):
    smiles: str = "CC"
    properties: list[str] = ["Tg"]

class Repo:
    def __init__(self, policy):
        self.policy=policy;self.ops={};self.calls=[];self.tier="member";self.fail_received=False
    def lookup(self, **kw):
        self.calls.append("lookup")
        for op in self.ops.values():
            if all(op[k]==kw[k] for k in ("owner_user_id","module","idempotency_key")):
                if op["request_fingerprint"] != kw["request_fingerprint"]:
                    raise core.EntitlementConflict()
                return deepcopy(op)
    def reserve(self, **kw):
        old=self.lookup(**{k:kw[k] for k in ("owner_user_id","module","idempotency_key","request_fingerprint")})
        if old:return {"created":False,"operation":old}
        op={**kw,"operation_id":str(uuid4()),"generation":1,"state":"reserved","result_json":None,"qualified":False}
        self.ops[op["operation_id"]]=op
        return {"created":True,"operation":deepcopy(op)}
    def _move(self,state,**kw):
        op=self.ops[kw["operation_id"]]
        if op["state"] in core.TERMINAL_STATES:return {"transitioned":False,"operation":deepcopy(op)}
        op.update(kw);op["state"]=state
        return {"transitioned":True,"operation":deepcopy(op)}
    def begin_dispatch(self,**kw):
        kw["generation"]+=1
        return self._move("dispatching",**kw)
    def claim_running(self,**kw):return self._move("running",**kw)
    def settle(self,**kw):return self._move("succeeded" if kw["qualified"] else "released",**kw)
    def release(self,**kw):return self._move("released",**kw)
    def mark_unknown(self,**kw):return self._move("unknown",**kw)
    def get(self,**kw):
        op=self.ops.get(kw["operation_id"])
        return deepcopy(op) if op and op["owner_user_id"]==kw["owner_user_id"] else None
    def get_policy(self,**kw):return deepcopy(self.policy)
    def resolve_access_tier(self,**kw):return self.tier
    def record_request(self,**kw):
        if self.fail_received and kw["phase"]=="received":raise RuntimeError("synthetic database unavailable")
        self.calls.append(kw)

@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED","true")
    root=Path(__file__).resolve().parents[2]
    policy=load_policy(json.loads((root/"config/examples/entitlements-example-v1.json").read_text()))
    repo=Repo(policy); app=FastAPI()
    app.state.entitlement_repository=repo;app.state.entitlement_policy=policy
    app.state.entitlement_observation_degraded=False
    app.add_middleware(EntitlementAccessMiddleware,repository=repo,policy=policy)
    return app,repo

def mount(app, action=None, sync=False):
    calls=[]
    async def endpoint(request_body: Body, request: Request):
        calls.append(request_body.model_dump())
        return action(request_body,request) if action else {"predictions":{"Tg":123.4}}
    def synchronous(request_body: Body, request: Request):
        calls.append(request_body.model_dump())
        return {"predictions":{"Tg":123.4}}
    app.post("/api/v1/predict")(entitled_sync("predict")(synchronous if sync else endpoint))
    return calls

def post(client, body=None, key="test-key-001"):
    return client.post("/api/v1/predict",json=body or {"smiles":"CC","properties":["Tg"]},
                       headers={} if key is None else {"Idempotency-Key":key})

def test_resolved_fastapi_signature_and_replay_without_current_policy(setup):
    app,repo=setup;calls=mount(app)
    with user_context(Identity(OWNER)),TestClient(app) as client:
        one=post(client)
        assert one.status_code==200,one.text
        opid=one.headers["x-usage-operation-id"]
        app.state.entitlement_policy=None
        two=post(client)
        assert two.status_code==200 and two.headers["x-usage-replayed"]=="true"
        assert two.headers["x-usage-operation-id"]==opid
    assert len(calls)==1 and len(repo.ops)==1
    op=repo.ops[opid]
    assert op["qualified"] and op["state"]=="succeeded"
    received=[x for x in repo.calls if isinstance(x,dict) and x["phase"]=="received"]
    finished=[x for x in repo.calls if isinstance(x,dict) and x["phase"]=="finished"]
    assert len(received)==len(finished)==2
    assert len({x["request_id"] for x in received})==2
    assert {x["operation_id"] for x in finished}=={opid}

@pytest.mark.parametrize("sync",[False,True])
def test_default_off_preserves_endpoint_without_quota_calls(setup,monkeypatch,sync):
    app,repo=setup;calls=mount(app,sync=sync)
    monkeypatch.delenv("USAGE_ENTITLEMENTS_ENABLED")
    with user_context(Identity(OWNER)),TestClient(app) as client:
        response=post(client,key=None)
    assert response.status_code==200 and len(calls)==1
    assert not repo.ops and not repo.calls

def test_key_required_and_same_key_payload_conflict(setup):
    app,repo=setup;calls=mount(app)
    with user_context(Identity(OWNER)),TestClient(app) as client:
        assert post(client,key=None).status_code==400
        assert post(client).status_code==200
        assert post(client,{"smiles":"CCC","properties":["Tg"]}).status_code==409
    assert len(calls)==1

def test_free_account_is_not_upgraded_by_disabled_collection(setup,monkeypatch):
    app,repo=setup;calls=mount(app);repo.tier="free"
    monkeypatch.delenv("USAGE_ENTITLEMENTS_ENABLED")
    with user_context(Identity(OWNER)),TestClient(app) as client:
        response=post(client,key=None)
    assert response.status_code==403 and response.json()["detail"]["code"]=="membership_required"
    assert not calls and not repo.ops

@pytest.mark.parametrize("kind",["large","nan","missing_property"])
def test_invalid_or_unretainable_result_never_consumes_success(setup,kind):
    app,repo=setup;repo.policy["result_max_bytes"]=500
    data={"predictions":{"Tg":float("nan") if kind=="nan" else 12}}
    if kind=="large":data["padding"]="x"*2000
    if kind=="missing_property":data["predictions"]={}
    calls=mount(app,lambda *_:data)
    with user_context(Identity(OWNER)),TestClient(app) as client:
        response=post(client)
        retry=post(client)
    assert response.status_code==retry.status_code
    assert len(calls)==1
    op=next(iter(repo.ops.values()))
    assert not op["qualified"] and op["state"]=="released"
    assert op["result_json"] is not None

def test_local_failure_is_durable_and_not_executed_again(setup):
    app,repo=setup
    def failure(*_):raise HTTPException(422,"synthetic invalid input")
    calls=mount(app,failure)
    with user_context(Identity(OWNER)),TestClient(app) as client:
        assert post(client).status_code==422
        assert post(client).status_code==422
    assert len(calls)==1 and next(iter(repo.ops.values()))["state"]=="released"

def test_unconfirmed_execution_stays_unknown_and_is_not_reexecuted(setup):
    app,repo=setup
    def failure(*_):raise RuntimeError("synthetic uncertain result")
    calls=mount(app,failure)
    with user_context(Identity(OWNER)),TestClient(app,raise_server_exceptions=False) as client:
        assert post(client).status_code==500
        response=post(client)
        assert response.status_code==409
        assert response.json()["detail"]["reservation_held"]
    assert len(calls)==1 and next(iter(repo.ops.values()))["state"]=="unknown"

def test_native_response_headers_include_durable_unknown(setup):
    app,repo=setup;opid=str(uuid4())
    @app.post("/api/v1/monomer-md/jobs",status_code=202)
    def native(request:Request):
        request.state.usage_operation_id=opid
        request.state.usage_operation_state="unknown"
        return {"job_id":"synthetic","status":"pending"}
    with user_context(Identity(OWNER)),TestClient(app) as client:
        response=client.post("/api/v1/monomer-md/jobs")
    assert response.status_code==202
    assert response.headers["x-usage-operation-id"]==opid
    assert response.headers["x-usage-state"]=="unknown"

def test_admission_observation_failure_blocks_business(setup):
    app,repo=setup;repo.fail_received=True;calls=mount(app)
    with user_context(Identity(OWNER)),TestClient(app) as client:
        response=post(client)
    assert response.status_code==503 and not calls and not repo.ops

def test_tier_resolution_failure_is_closed_even_with_collection_off(setup,monkeypatch):
    app,repo=setup;calls=mount(app)
    def fail(**_):raise RuntimeError("synthetic unavailable tier")
    repo.resolve_access_tier=fail
    monkeypatch.delenv("USAGE_ENTITLEMENTS_ENABLED")
    with user_context(Identity(OWNER)),TestClient(app) as client:
        assert post(client,key=None).status_code==503
    assert not calls

def test_unknown_online_counts_are_not_invented():
    request=SimpleNamespace(state=SimpleNamespace(online_entitlement_facts={"completion_proven":True}))
    assert _facts("online_knowledge",{}, {"exampleUsed":False},request) is None

def test_rate_guard_boundary_and_owner_isolation():
    guard=RequestRateGuard()
    assert guard.accept(OWNER,1,now=10)
    assert not guard.accept(OWNER,1,now=69.9)
    assert guard.accept(OTHER,1,now=11)
    assert guard.accept(OWNER,1,now=70)

@pytest.mark.parametrize("module",["predict","query","monomer_polymerization","monomer_retrosynthesis","online_knowledge"])
def test_real_router_signature_import_and_openapi(module):
    import importlib
    app=FastAPI();app.include_router(importlib.import_module("app.routers."+module).router)
    document=app.openapi()
    assert document["paths"]


def test_saved_result_is_owner_only_and_free_to_read_while_disabled(setup,monkeypatch):
    from app.routers.entitlements import router
    app,repo=setup;mount(app);app.include_router(router)
    repo.read_saved_operation=repo.get
    with user_context(Identity(OWNER)),TestClient(app) as client:
        response=post(client)
        opid=response.headers["x-usage-operation-id"]
        before=len(repo.ops)
        monkeypatch.delenv("USAGE_ENTITLEMENTS_ENABLED")
        app.state.entitlement_policy=None
        repo.tier="free"
        recovered=client.get("/api/v1/usage/operations/"+opid+"/result")
        assert recovered.status_code==200,recovered.text
        assert recovered.json()["result"]["payload"]["predictions"]=={"Tg":123.4}
        assert "owner_user_id" not in recovered.json()["operation"]
        assert len(repo.ops)==before
    with user_context(Identity(OTHER)),TestClient(app) as client:
        assert client.get("/api/v1/usage/operations/"+opid+"/result").status_code==404


def test_storage_commit_error_holds_operation_instead_of_automatic_retry(setup):
    app,repo=setup;calls=mount(app)
    def fail_settle(**_):raise core.EntitlementPolicyError("synthetic commit failure")
    repo.settle=fail_settle
    with user_context(Identity(OWNER)),TestClient(app) as client:
        failed=post(client)
        assert failed.status_code==503
        assert failed.headers["x-usage-state"]=="unknown"
        assert post(client).status_code==409
    assert len(calls)==1 and next(iter(repo.ops.values()))["state"]=="unknown"
