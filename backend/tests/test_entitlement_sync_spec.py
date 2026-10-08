"""Offline adversarial bounds: real routes, fake durable repo, no scientific/provider IO."""
from contextlib import nullcontext
from io import BytesIO
from types import SimpleNamespace

import anyio
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from rdkit import Chem
from starlette.datastructures import UploadFile

from app.auth.context import Identity, user_context
from app.auth.middleware import AuthenticationMiddleware
from app.auth.policy import capabilities
from app.auth.service import csrf_token
from app.models import OnlineKnowledgeSearchResponse
from app.services.entitlement_sync import _facts, _request_data
from task_usage.entitlement_policy import qualify, normalize_spec
from test_entitlement_http import OWNER, setup


KEY={"Idempotency-Key":"synthetic-spec-key-001"}
QUERY={"smiles":"CC", "match_mode":"property", "property_name":"Tg", "top_k":10, "similarity_threshold":.7}


def mount_query(app, monkeypatch):
    from app.routers import query
    calls=[]
    def run(body, _app):
        calls.append(body.model_dump())
        return {"match_type":body.match_mode,"query_time_ms":0,"total":0,"results":[],
                "predicted_property_name":body.property_name if body.match_mode=="property" else None,
                "predicted_property_value":123.0 if body.match_mode=="property" else None}
    monkeypatch.setattr(query,"_query_smiles_sync",run)
    app.include_router(query.router)
    return calls


@pytest.mark.parametrize("mode",["property","similarity"])
def test_real_property_query_cannot_bypass_counting_with_legacy_mode(setup,monkeypatch,mode):
    app,repo=setup;calls=mount_query(app,monkeypatch)
    body={**QUERY,"match_mode":mode}
    with user_context(Identity(OWNER)),TestClient(app) as client:
        assert client.post("/api/v1/query/smiles",json=body).status_code==400
        for _ in range(16):
            reply=client.post("/api/v1/query/smiles",json=body,headers=KEY)
            assert reply.status_code==200,reply.text
        conflict=client.post("/api/v1/query/smiles",json={**body,"property_name":"Tm"},headers=KEY)
        assert conflict.status_code==409
    assert len(calls)==1 and len(repo.ops)==1
    op=next(iter(repo.ops.values()))
    assert op["module"]=="query" and op["spec"]["property_count"]==1
    assert normalize_spec(repo.policy,"query",op["spec"])["pool_id"]=="chemistry_small"
    assert op["qualified"] and op["outcome"]=="no_match"


@pytest.mark.parametrize("mode",["structure","exact"])
def test_real_structural_query_remains_rate_only_without_idempotency_key(setup,monkeypatch,mode):
    app,repo=setup;calls=mount_query(app,monkeypatch)
    with user_context(Identity(OWNER)),TestClient(app) as client:
        response=client.post("/api/v1/query/smiles",json={**QUERY,"match_mode":mode})
    assert response.status_code==200 and len(calls)==1 and not repo.ops
    assert calls[0]["match_mode"]=="structure"


@pytest.mark.parametrize("override",[{"top_k":101},{"similarity_threshold":1.01},
    {"pool_id":None},{"qualified":True},{"match_mode":"property","property_count":0}])
def test_query_client_cannot_override_server_spec_or_facts(setup,monkeypatch,override):
    app,repo=setup;calls=mount_query(app,monkeypatch)
    with user_context(Identity(OWNER)),TestClient(app) as client:
        response=client.post("/api/v1/query/smiles",json={**QUERY,**override},headers=KEY)
    assert response.status_code==422 and not calls and not repo.ops


@pytest.mark.parametrize("change",[{"predicted_property_name":"wrong"},{"match_type":"structure"},
    {"predicted_property_value":None},{"predicted_property_value":True},
    {"predicted_property_value":float("inf")},{"total":1}])
def test_query_malformed_delivery_is_not_a_success_receipt(setup,change):
    _,repo=setup
    result={"match_type":"property","predicted_property_name":"Tg","predicted_property_value":12,
            "total":0,"results":[]}
    result.update(change)
    value=_facts("query",QUERY,result)
    spec={"match_mode":"property","property_count":1,"top_k":10,"similarity_threshold":.7}
    decision=qualify(repo.policy,"query",spec,value,"completed")
    assert not decision["qualified"] and decision["units"]==0


def molblock(smiles):
    mol=Chem.MolFromSmiles(smiles)
    mol.AddConformer(Chem.Conformer(mol.GetNumAtoms()))
    return Chem.MolToMolBlock(mol)


@pytest.mark.parametrize("smiles,expected",[("C1CCCCC1",200),("CCCCCCC",422),("not-a-smiles",422)])
def test_3d_uses_parsed_atom_count_including_rings_before_execution(setup,monkeypatch,smiles,expected):
    from app.routers import query
    app,repo=setup;calls=[]
    repo.policy["modules"]["structure"]["variants"]["3d"]["fields"]["atom_count"]["max"]=6
    def run(value,**kwargs):
        calls.append(value);return molblock(value),value
    monkeypatch.setattr(query,"generate_3d_molblock",run)
    app.state.settings=SimpleNamespace(structure_3d_timeout_seconds=1)
    app.include_router(query.router)
    with user_context(Identity(OWNER)),TestClient(app) as client:
        response=client.post("/api/v1/structure/3d",json={"smiles":smiles},headers=KEY)
    assert response.status_code==expected,response.text
    assert len(calls)==int(expected==200)
    if expected==200:
        op=next(iter(repo.ops.values()))
        assert op["spec"]["atom_count"]==6 and op["qualified"]
    else:assert not repo.ops


def png(width=8,height=8):
    target=BytesIO();Image.new("L",(width,height)).save(target,format="PNG");return target.getvalue()


def test_ocsr_measures_actual_bytes_pixels_and_rewinds_upload(setup):
    app,_=setup;raw=png(8,9)
    app.state.settings=SimpleNamespace(ocsr_max_image_bytes=1000)
    request=SimpleNamespace(app=app)
    upload=UploadFile(filename="anything.png",file=BytesIO(raw))
    async def measure():
        body,spec=await _request_data("ocsr",{"image":upload},request)
        assert await upload.read()==raw
        await upload.close()
        return body,spec
    body,spec=anyio.run(measure)
    assert spec=={"image_bytes":len(raw),"image_pixels":72}
    assert len(body["image_sha256"])==64 and "filename" not in body


@pytest.mark.parametrize("kind",["bytes","pixels","invalid_image"])
def test_ocsr_bounds_block_before_model_and_reservation(setup,monkeypatch,kind):
    from app.routers import query
    app,repo=setup;calls=[]
    app.state.settings=SimpleNamespace(ocsr_enabled=True,ocsr_max_image_bytes=20 if kind=="bytes" else 1000)
    if kind=="pixels":repo.policy["modules"]["ocsr"]["variants"]["default"]["fields"]["image_pixels"]["max"]=63
    monkeypatch.setattr(query,"_run_ocsr_inference",lambda *args:calls.append(args))
    app.include_router(query.router)
    data=b"invalid" if kind=="invalid_image" else png()
    with user_context(Identity(OWNER)),TestClient(app) as client:
        response=client.post("/api/v1/structure/recognize-image",files={"image":("image.png",data,"image/png")},headers=KEY)
    assert response.status_code==(413 if kind=="bytes" else 422),response.text
    assert not calls and not repo.ops


def mount_predict(app,monkeypatch):
    from app.routers import predict
    calls=[]
    app.state.settings=SimpleNamespace(model_enabled=True,model_dir_path=None)
    monkeypatch.setattr(predict,"get_available_properties",lambda _:["Tg","Tm","Density"])
    def run(smiles,properties,**kwargs):
        calls.append((smiles,properties));return {name:12.0 for name in properties}
    monkeypatch.setattr(predict,"predict_properties",run)
    app.include_router(predict.router)
    return calls


def test_prediction_distinct_properties_cannot_exceed_policy_with_duplicates(setup,monkeypatch):
    app,repo=setup;calls=mount_predict(app,monkeypatch)
    repo.policy["modules"]["predict"]["variants"]["default"]["fields"]["property_count"]["max"]=2
    with user_context(Identity(OWNER)),TestClient(app) as client:
        reply=client.post("/api/v1/predict",json={"smiles":"CC","properties":["Tg"]*100+["Tm","Density"]},headers=KEY)
    assert reply.status_code==422 and not calls and not repo.ops


def test_native_predictor_deduplicates_work_and_preserves_input_guard(monkeypatch):
    from app.services import predictor
    import app.task_control as control
    from app.services.private_quotas import PrivateQuotaSettings
    from app.utils.exceptions import InvalidSmilesError
    calls=[]
    monkeypatch.setattr(PrivateQuotaSettings,"from_environment",classmethod(lambda cls:SimpleNamespace(prediction_smiles_characters=4)))
    monkeypatch.setattr(control,"admission",lambda *_:nullcontext())
    monkeypatch.setattr(control,"authorize_memory_start",lambda _:True)
    monkeypatch.setattr(predictor,"_predict_unbounded",lambda s,p,m:calls.append((s,p)) or {"Tg":1.0})
    with user_context(Identity(OWNER)):
        assert predictor.predict("CC",["Tg"]*1000)=={"Tg":1.0}
        with pytest.raises(InvalidSmilesError):predictor.predict("CCCCC",["Tg"])
    assert calls==[("CC",["Tg"])]


def mount_online(app,monkeypatch,execution_facts=None,error=None):
    from app.routers import online_knowledge as online
    calls=[]
    monkeypatch.setattr(online,"admission",lambda *_:nullcontext())
    monkeypatch.setattr(online,"authorize_memory_start",lambda _:True)
    def run(body,request):
        calls.append(body.model_dump())
        if error:raise HTTPException(error,"synthetic uncertain provider result")
        if execution_facts is not None:request.state.online_entitlement_facts=dict(execution_facts)
        return OnlineKnowledgeSearchResponse(material=body.material,mode=body.mode,query_time_ms=0,
            totalPapers=1,max_papers=body.max_papers,exampleUsed=False,stats={})
    monkeypatch.setattr(online,"_search_online_knowledge_under_admission",run)
    app.include_router(online.router)
    return calls


@pytest.mark.parametrize("observed",[None,{}, {"completion_proven":True},
    {"validated":True,"completion_proven":True,"successful_extractions":True,"failed_extractions":0},
    {"validated":True,"completion_proven":False,"successful_extractions":1,"failed_extractions":0}])
def test_online_unknown_counts_or_completion_keep_one_reservation_and_do_not_reexecute(setup,monkeypatch,observed):
    app,repo=setup;calls=mount_online(app,monkeypatch,observed)
    with user_context(Identity(OWNER)),TestClient(app) as client:
        first=client.post("/api/v1/online-knowledge/search",json={"material":"synthetic","max_papers":2},headers=KEY)
        again=client.post("/api/v1/online-knowledge/search",json={"material":"synthetic","max_papers":2},headers=KEY)
    assert first.status_code==503 and again.status_code==409
    assert len(calls)==1 and len(repo.ops)==1
    op=next(iter(repo.ops.values()))
    assert op["state"]=="unknown" and not op["qualified"] and again.json()["detail"]["reservation_held"]


@pytest.mark.parametrize("failed,expected",[(0,"succeeded"),(1,"released")])
def test_online_completed_facts_distinguish_delivery_from_partial(setup,monkeypatch,failed,expected):
    app,repo=setup
    observed={"validated":True,"completion_proven":True,"successful_extractions":1,"failed_extractions":failed}
    calls=mount_online(app,monkeypatch,observed)
    with user_context(Identity(OWNER)),TestClient(app) as client:
        response=client.post("/api/v1/online-knowledge/search",json={"material":"synthetic","max_papers":2},headers=KEY)
    assert response.status_code==200 and len(calls)==1
    assert next(iter(repo.ops.values()))["state"]==expected


def test_online_502_after_provider_start_keeps_unknown_instead_of_free_retry(setup,monkeypatch):
    app,repo=setup;calls=mount_online(app,monkeypatch,error=502)
    with user_context(Identity(OWNER)),TestClient(app) as client:
        assert client.post("/api/v1/online-knowledge/search",json={"material":"synthetic"},headers=KEY).status_code==502
        assert client.post("/api/v1/online-knowledge/search",json={"material":"synthetic"},headers=KEY).status_code==409
    assert len(calls)==1 and next(iter(repo.ops.values()))["state"]=="unknown"


@pytest.mark.parametrize("override",[{"max_papers":2001},{"max_papers":3},
    {"material":"x"*1001},{"successful_extractions":1},{"validated":True}])
def test_online_large_work_and_client_facts_rejected_before_dispatch(setup,monkeypatch,override):
    app,repo=setup;calls=mount_online(app,monkeypatch)
    repo.policy["modules"]["online_knowledge"]["variants"]["default"]["fields"]["max_papers"]["max"]=2
    with user_context(Identity(OWNER)),TestClient(app) as client:
        response=client.post("/api/v1/online-knowledge/search",json={"material":"synthetic","max_papers":2,**override},headers=KEY)
    assert response.status_code==422 and not calls and not repo.ops


class FakeAuth:
    settings=SimpleNamespace(cookie_name="synthetic_session",allowed_origins=["https://client.invalid"])
    def __init__(self,logged_in):self.logged_in=logged_in
    def resolve(self,token):
        return {"session_id":"synthetic-session","must_change_password":False} if self.logged_in else None
    def identity(self,session):return Identity(OWNER,session_id=session["session_id"])
    def assert_application_ready(self):pass


@pytest.mark.parametrize("tier,enabled,expected",[("guest",True,401),("guest",False,401),
    ("free",True,403),("free",False,403),("member",True,200),("member",False,200)])
def test_existing_guest_free_member_access_is_not_upgraded_by_counts(setup,monkeypatch,tier,enabled,expected):
    app,repo=setup;calls=mount_predict(app,monkeypatch)
    repo.tier="free" if tier=="free" else "member"
    if not enabled:monkeypatch.delenv("USAGE_ENTITLEMENTS_ENABLED")
    app.add_middleware(AuthenticationMiddleware,auth_service=FakeAuth(tier!="guest"))
    token="synthetic-token"
    headers={**KEY,"Origin":"https://client.invalid","X-Session-Context":"synthetic-session","X-CSRF-Token":csrf_token(token)}
    with TestClient(app) as client:
        client.cookies.set("synthetic_session",token)
        response=client.post("/api/v1/predict",json={"smiles":"CC","properties":["Tg"]},headers=headers)
    assert response.status_code==expected,response.text
    assert len(calls)==int(tier=="member")
    assert len(repo.ops)==int(tier=="member" and enabled)
    if tier in {"guest","free"}:
        assert not capabilities(False)["prediction.use"]
        assert capabilities(False)["examples.view"]
