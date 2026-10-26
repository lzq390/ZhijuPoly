"""Authenticated API acceptance on an isolated main22 -> 0023 PostgreSQL database."""
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace
from uuid import uuid4

import anyio
from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest

from app.routers.polymer_md import router, internal_router
from app.services.polymer_md_repository import PolymerMdRepository, submit_request
from app.services.polymer_md_repository import PolymerMdError
from polymer_md.contracts import StartAuthorizationRequest, WorkerSnapshot
from test_auth_isolation import auth_database
from test_private_http_support import authenticated_client


@pytest.fixture(scope="module")
def polymer_auth_database(auth_database):
    from app.postgres_migrations import apply_postgres_migrations
    from app.services.polymer_md_schema import apply_polymer_md_dev_migration
    apply_postgres_migrations(auth_database["admin"], allowed_kinds={"expand"})
    apply_polymer_md_dev_migration(auth_database["admin"], environment="dev")
    return auth_database


class Worker:
    def __init__(self):
        self.calls=[]

    async def health(self):
        return {"protocol_version":1,"start_authorization_version":1,"ready":True}

    async def stage_json(self,request,stage_run_id,**kwargs):
        self.calls.append((request.job_id,stage_run_id,kwargs))
        return {"job_id":request.job_id,"stage_run_id":stage_run_id,"frame_count":1,"points":[]}

    async def stage_logs(self,request,stage_run_id,**kwargs):
        self.calls.append((request.job_id,stage_run_id,kwargs))
        return {"job_id":request.job_id,"stage_run_id":stage_run_id,"available":True,"lines":["real transport test"],"truncated":False}

    @asynccontextmanager
    async def artifact_stream(self,request,artifact_id):
        yield httpx.Response(200,content=b"{}")

    async def delete(self,request,**kwargs):
        return {**{key:getattr(request,key) for key in ('job_id','attempt_token','request_sha256','enqueue_sequence')},"deleted":True,"cleanup_confirmed":True}


@pytest.fixture
def polymer_api(polymer_auth_database,monkeypatch,tmp_path):
    db=polymer_auth_database
    monkeypatch.setenv("APP_SERVICE_POSTGRES_DSN",db["service"])
    with psycopg.connect(db["admin"]) as c:
        c.execute("TRUNCATE polymer_md.jobs CASCADE")
    app=FastAPI()
    app.state.settings=SimpleNamespace(polymer_md_dev_enabled=True,polymer_md_submit_enabled=True,polymer_md_start_authorization_token="test-only-token",polymer_md_download_spool_root=str(tmp_path))
    app.state.polymer_md_repository=PolymerMdRepository(db["api"])
    app.state.polymer_md_worker_client=Worker()
    app.state.polymer_md_reconciler=None
    app.state.polymer_md_download_limiter=anyio.CapacityLimiter(2)
    app.include_router(router);app.include_router(internal_router)
    first=authenticated_client(app,db)
    second=authenticated_client(app,db)
    return app,first,second,db


def test_real_auth_owner_fencing_seed_replay_and_service_start(polymer_api):
    app,alice,bob,db=polymer_api
    guest=TestClient(app)
    assert guest.get("/api/v1/polymer-md/jobs").status_code==401
    assert alice.get("/api/v1/polymer-md/capabilities").json()["submit_enabled"]
    payload={"smiles":"*CC*"}
    created=alice.post("/api/v1/polymer-md/jobs",json=payload,headers={"Idempotency-Key":"polymer-browser-request"})
    assert created.status_code==202,created.text
    job=created.json();job_id=job["job_id"]
    assert "attempt_token" not in job and job["scientific_request"]["seed"]>0
    replay=alice.post("/api/v1/polymer-md/jobs",json=payload,headers={"Idempotency-Key":"polymer-browser-request"})
    assert replay.json()["scientific_request"]["seed"]==job["scientific_request"]["seed"]
    for path in (f"/api/v1/polymer-md/jobs/{job_id}",f"/api/v1/polymer-md/jobs/{job_id}/artifacts",f"/api/v1/polymer-md/jobs/{job_id}/stages/eq-1/trajectory"):
        assert bob.get(path).status_code==404
    assert bob.post(f"/api/v1/polymer-md/jobs/{job_id}/cancel").status_code==404
    assert bob.delete(f"/api/v1/polymer-md/jobs/{job_id}").status_code==404
    repo=app.state.polymer_md_repository
    internal=repo.get_service_job(job_id);repo.claim_dispatch(job_id,internal["attempt_token"])
    auth=StartAuthorizationRequest(**submit_request(internal).model_dump(),worker_instance_id="a"*32)
    path=f"/internal/polymer-md/jobs/{job_id}/authorize-start"
    assert guest.post(path,json=auth.model_dump(mode="json")).status_code==403
    response=guest.post(path,json=auth.model_dump(mode="json"),headers={"Authorization":"Bearer test-only-token"})
    assert response.status_code==200,response.text
    assert response.json()["authorized"]
    old=auth.model_copy(update={"worker_instance_id":"b"*32})
    assert guest.post(path,json=old.model_dump(mode="json"),headers={"Authorization":"Bearer test-only-token"}).status_code==409


def test_native_selections_persist_and_replay_without_default_substitution(polymer_api):
    app,alice,bob,db=polymer_api
    native={"opt_method":"HF","opt_basis":"6-31G(d)","charge":"Mulliken",
            "charge_method":"HF","charge_basis":"6-31G(d)","sp_method":"HF",
            "sp_basis":"6-311G(d,p)","polar_method":"HF","polar_basis":"6-311+G(2d,p)",
            "properties":["qm_homo"]}
    payload={"smiles":"*CC*","forcefield":"Dreiding","native_configuration":native}
    response=alice.post('/api/v1/polymer-md/jobs',json=payload,headers={'Idempotency-Key':'native-scientific-request'})
    assert response.status_code==202,response.text
    job=response.json()
    assert job['scientific_request']['forcefield']=='Dreiding'
    assert job['scientific_request']['native_configuration']==native
    replay=alice.post('/api/v1/polymer-md/jobs',json=payload,headers={'Idempotency-Key':'native-scientific-request'})
    assert replay.status_code==202 and replay.json()['job_id']==job['job_id']
    changed={**payload,'native_configuration':{**native,'sp_method':'wb97m-d3bj'}}
    conflict=alice.post('/api/v1/polymer-md/jobs',json=changed,headers={'Idempotency-Key':'native-scientific-request'})
    assert conflict.status_code==409 and conflict.json()['code']=='idempotency_conflict'
    assert bob.get('/api/v1/polymer-md/jobs/'+job['job_id']).status_code==404
    internal=app.state.polymer_md_repository.get_service_job(job['job_id'])
    worker_request=submit_request(internal)
    assert worker_request.scientific_request.native_configuration.sp_method=='HF'
    assert worker_request.scientific_request.forcefield=='Dreiding'


def model_payload(mode):
    components = [{"id": "A", "smiles": "*CC*"}, {"id": "B", "smiles": "*OCC*"}]
    model = {"version": 1, "mode": mode, "components": components}
    if mode == "homopolymer":
        model.update(components=components[:1], chain_length={"mode": "dp", "value": 20})
    elif mode == "block":
        model["blocks"] = [{"component_id": "A", "dp": 10}, {"component_id": "B", "dp": 20}, {"component_id": "A", "dp": 10}]
    elif mode == "blend":
        model["components"] = [{**component, "n_chain": 2, "chain_length": {"mode": "molecular_weight", "value": 1000}}
                               for component in components]
    else:
        model["chain_length"] = {"mode": "dp", "value": 20}
        if mode == "random":
            model["ratios"] = [.4, .6]
    return {"smiles": "*CC*", "n_chain": 4, "model_configuration": model}


@pytest.mark.parametrize("mode", ["homopolymer", "alternating", "random", "block", "blend"])
def test_model_selections_persist_owner_scope_canonical_replay_and_worker_wire(polymer_api, mode):
    app, alice, bob, db = polymer_api
    payload = model_payload(mode)
    headers = {"Idempotency-Key": "polymer-model-selection-" + mode}
    response = alice.post('/api/v1/polymer-md/jobs', json=payload, headers=headers)
    assert response.status_code == 202, response.text
    job = response.json()
    saved = job['scientific_request']['model_configuration']
    assert saved['mode'] == mode and saved['components'][0]['smiles'] == job['scientific_request']['smiles']
    if len(saved['components']) > 1:
        assert saved['components'][1]['smiles'] == '*CCO*'
    replay = alice.post('/api/v1/polymer-md/jobs', json=payload, headers=headers)
    assert replay.status_code == 202 and replay.json()['job_id'] == job['job_id'] and not replay.json()['created']
    canonical_payload = {**payload, 'model_configuration': saved}
    assert alice.post('/api/v1/polymer-md/jobs', json=canonical_payload, headers=headers).json()['job_id'] == job['job_id']
    assert bob.get('/api/v1/polymer-md/jobs/' + job['job_id']).status_code == 404
    changed = {**payload, 'pressure': 2}
    assert alice.post('/api/v1/polymer-md/jobs', json=changed, headers=headers).status_code == 409
    internal = app.state.polymer_md_repository.get_service_job(job['job_id'])
    assert submit_request(internal).scientific_request.model_configuration.model_dump(mode='json') == saved


@pytest.mark.parametrize("case", ['charged_second', 'invalid_ratio', 'unknown_field', 'actual_atom_budget', 'replicated_budget'])
def test_model_admission_rejects_invalid_components_or_chemistry_aware_budgets(polymer_api, case):
    app, alice, bob, db = polymer_api
    payload = model_payload('random')
    if case == 'charged_second':
        payload['model_configuration']['components'][1]['smiles'] = '*[NH2+]CC*'
    elif case == 'invalid_ratio':
        payload['model_configuration']['ratios'] = [.4, .4]
    elif case == 'unknown_field':
        payload['model_configuration']['fake_execution'] = True
    else:
        payload = model_payload('homopolymer')
        payload['n_chain'] = 1
        payload['model_configuration']['chain_length'] = {'mode': 'dp', 'value': 5000 if case == 'actual_atom_budget' else 2000}
        if case == 'replicated_budget':
            payload.update(calculation='basic_tc', tc_rep=5)
    response = alice.post('/api/v1/polymer-md/jobs', json=payload, headers={'Idempotency-Key': 'polymer-model-invalid-' + case})
    assert response.status_code == 422, response.text
    assert alice.get('/api/v1/polymer-md/jobs').json()['total'] == 0


def test_thermal_diffusivity_has_an_explicit_native_target_and_rnemd_dependency(polymer_api):
    app, alice, bob, db = polymer_api
    payload = {**model_payload('homopolymer'), 'calculation': 'basic_tc',
               'native_configuration': {'properties': ['thermal_diffusivity']}}
    response = alice.post('/api/v1/polymer-md/jobs', json=payload, headers={'Idempotency-Key': 'polymer-native-thermal-diffusivity'})
    assert response.status_code == 202, response.text
    assert response.json()['scientific_request']['native_configuration']['properties'] == ['thermal_diffusivity']
    assert response.json()['scientific_request']['calculation'] == 'basic_tc'


@pytest.mark.parametrize("properties,calculation", [(["tg"], "basic"), (["sp_total", "sp_ced"], "basic"),
    (["tg", "sp_total", "sp_ced"], "basic"), (["tg", "sp_total", "sp_ced", "thermal_conductivity"], "basic_tc")])
def test_postequilibrium_targets_persist_exactly_with_owner_fenced_idempotent_replay(polymer_api, properties, calculation):
    app, alice, bob, db = polymer_api
    payload = {**model_payload('homopolymer'), 'n_chain': 2, 'calculation': calculation,
               'native_configuration': {'properties': properties}}
    headers = {'Idempotency-Key': 'polymer-posteq-target-combination'}
    response = alice.post('/api/v1/polymer-md/jobs', json=payload, headers=headers)
    assert response.status_code == 202, response.text
    job = response.json()
    assert job['scientific_request']['native_configuration']['properties'] == sorted(properties)
    assert job['scientific_request']['calculation'] == calculation
    assert 'posteq_configuration' not in job['scientific_request']
    replay = alice.post('/api/v1/polymer-md/jobs', json=payload, headers=headers)
    assert replay.status_code == 202 and replay.json()['job_id'] == job['job_id'] and not replay.json()['created']
    assert bob.get('/api/v1/polymer-md/jobs/' + job['job_id']).status_code == 404
    changed = {**payload, 'native_configuration': {'properties': ['density']}, 'calculation': 'basic'}
    assert alice.post('/api/v1/polymer-md/jobs', json=changed, headers=headers).status_code == 409
    internal = submit_request(app.state.polymer_md_repository.get_service_job(job['job_id']))
    assert internal.scientific_request.native_configuration.properties == sorted(properties)


@pytest.mark.parametrize("target", ["sp_total", "sp_ced"])
def test_sp_rejects_a_one_chain_cell_before_persisting_a_task(polymer_api, target):
    app, alice, bob, db = polymer_api
    payload = {**model_payload('homopolymer'), 'n_chain': 1,
               'native_configuration': {'properties': [target]}}
    response = alice.post('/api/v1/polymer-md/jobs', json=payload,
                          headers={'Idempotency-Key': 'polymer-reject-single-chain-sp'})
    assert response.status_code == 422, response.text
    assert alice.get('/api/v1/polymer-md/jobs').json()['total'] == 0


def test_accepted_request_replays_during_worker_outage(polymer_api):
    app,alice,bob,db=polymer_api
    payload={"smiles":"*CC*"};headers={"Idempotency-Key":"polymer-outage-replay"}
    first=alice.post('/api/v1/polymer-md/jobs',json=payload,headers=headers)
    assert first.status_code==202
    async def unavailable():
        raise PolymerMdError('worker_unavailable','response unknown',503)
    app.state.polymer_md_worker_client.health=unavailable
    replay=alice.post('/api/v1/polymer-md/jobs',json=payload,headers=headers)
    assert replay.status_code==202 and not replay.json()['created']
    assert replay.json()['job_id']==first.json()['job_id']
    assert replay.json()['scientific_request']['seed']==first.json()['scientific_request']['seed']
    conflict=alice.post('/api/v1/polymer-md/jobs',json={**payload,'temp':350},headers=headers)
    assert conflict.status_code==409 and conflict.json()['code']=='idempotency_conflict'


def test_capabilities_include_real_defaults_protocol_and_only_public_runtime(polymer_api):
    app,alice,bob,db=polymer_api
    async def health():
        return {'protocol_version':1,'start_authorization_version':1,'ready':True,'runtime':{
            'source_sha':'a'*40,'patch_version':'nexpoly-md-1','runtime_fingerprint':'b'*64,
            'verified_files':[{'path':'/private/engine','sha256':'c'*64}],
            'python':'/private/python','lammps':'/private/lammps','accepted_protocols':['pe-benchmark-v1','/private/leak']}}
    app.state.polymer_md_worker_client.health=health
    response=alice.get('/api/v1/polymer-md/capabilities');value=response.json()
    assert value['parameter_defaults']['seed'] is None and 'smiles' not in value['parameter_defaults']
    assert value['parameter_defaults']['eq_ns']==5 and value['parameter_defaults']['max_additional']==2
    assert value['parameter_schema']['properties']['max_additional']['maximum']==2
    assert value['supported_model_modes']==['homopolymer','alternating','random','block','blend']
    assert value['model_configuration_spec']['component_limit']==4
    assert value['native_configuration_schema']['properties']['properties']['maxItems']==25
    assert value['post_equilibrium_protocols']['tg']['temperature_platforms']==71
    assert value['post_equilibrium_protocols']['sp']['sampling_ps']==500
    assert value['post_equilibrium_protocols']['sp']['minimum_chain_count']==2
    assert value['scientific_protocol']['gpu_count']==0
    assert value['scientific_protocol']['max_mpi_processes']==16
    assert value['scientific_protocol']['mpi_selection']['monomer_mm_processes']==1
    assert value['scientific_protocol']['equilibrium_analysis']['burn_in_ps']==2000
    assert value['scientific_protocol']['rnemd']['burn_in_ps']==800
    assert value['runtime']=={'source_sha':'a'*40,'patch_version':'nexpoly-md-1','runtime_fingerprint':'b'*64,
        'scientific_acceptance':'pending','adapter_scientific_validation':'pending','accepted_protocols':['pe-benchmark-v1']}
    assert 'verified_files' not in response.text and '/private' not in response.text


def test_deployment_activity_includes_the_accepted_fifo_until_confirmed_cleanup(polymer_api):
    from app.services.deployment_control import count_active_postgres_jobs
    app,alice,_bob,db=polymer_api
    response=alice.post('/api/v1/polymer-md/jobs',json={'smiles':'*CC*'},headers={'Idempotency-Key':'platform-drain-queue'})
    assert response.status_code==202
    job_id=response.json()['job_id']
    # Closing admission must not mutate/stop the durable queue or remove its
    # deployment ownership. Operators may let it finish or cancel normally.
    app.state.settings.polymer_md_submit_enabled=False
    with psycopg.connect(db['admin'],row_factory=dict_row) as connection:
        for status in ('pending','queued','running','cancel_requested'):
            connection.execute('UPDATE polymer_md.jobs SET status=%s WHERE job_id=%s',(status,job_id))
            summary=count_active_postgres_jobs(connection)
            assert summary.active_jobs_schema_version==4 and summary.counts['polymer_md']==1
        connection.execute("UPDATE polymer_md.jobs SET status='cancelled',cleanup_confirmed=true WHERE job_id=%s",(job_id,))
        assert count_active_postgres_jobs(connection).counts['polymer_md']==0
        token=connection.execute('SELECT attempt_token FROM polymer_md.jobs WHERE job_id=%s',(job_id,)).fetchone()['attempt_token']
        connection.execute("INSERT INTO polymer_md.stage_runs(job_id,stage_run_id,attempt_token,stage,attempt,status,quality_status) VALUES(%s,'eq3-1',%s,'eq3',1,'running','not_evaluated')",(job_id,token))
        assert count_active_postgres_jobs(connection).counts['polymer_md']==1
        connection.execute("UPDATE polymer_md.stage_runs SET status='cancelled' WHERE job_id=%s",(job_id,))
        assert count_active_postgres_jobs(connection).counts['polymer_md']==0


def test_drain_keeps_completed_artifact_analysis_available_without_admitting_jobs(polymer_api):
    app,alice,bob,db=polymer_api
    app.state.settings.polymer_md_submit_enabled=False

    async def drained_health():
        return {'protocol_version':1,'start_authorization_version':1,'ready':False,
                'draining':True,'analysis_ready':True}

    app.state.polymer_md_worker_client.health=drained_health
    capabilities=alice.get('/api/v1/polymer-md/capabilities').json()
    assert not capabilities['worker_ready'] and not capabilities['submit_enabled']
    assert capabilities['trajectory_available'] and capabilities['distance_available']
    response=alice.post('/api/v1/polymer-md/jobs',json={'smiles':'*CC*'},headers={'Idempotency-Key':'drained-no-new-computation'})
    assert response.status_code==503
    assert app.state.polymer_md_worker_client.calls==[]

    async def stopped_health():
        return {'protocol_version':1,'start_authorization_version':1,'ready':False,
                'analysis_ready':False}

    app.state.polymer_md_worker_client.health=stopped_health
    capabilities=alice.get('/api/v1/polymer-md/capabilities').json()
    assert not capabilities['trajectory_available'] and not capabilities['distance_available']


def test_running_stage_logs_and_separate_results_stages_are_owner_scoped(polymer_api):
    app,alice,bob,db=polymer_api
    created=alice.post('/api/v1/polymer-md/jobs',json={'smiles':'*CC*'},headers={'Idempotency-Key':'polymer-running-log'})
    job_id=created.json()['job_id'];repo=app.state.polymer_md_repository
    job=repo.get_service_job(job_id);repo.claim_dispatch(job_id,job['attempt_token']);submit=submit_request(job)
    # Closing new admission must keep authorization/reconciliation available to
    # an already accepted job so maintenance can drain the durable queue.
    app.state.settings.polymer_md_submit_enabled=False
    guest=TestClient(app)
    response=guest.post(f'/internal/polymer-md/jobs/{job_id}/authorize-start',json=StartAuthorizationRequest(**submit.model_dump(),worker_instance_id='a'*32).model_dump(mode='json'),headers={'Authorization':'Bearer test-only-token'})
    assert response.status_code==200
    repo.apply_snapshot(WorkerSnapshot(**{key:getattr(submit,key) for key in ('job_id','attempt_token','request_sha256','enqueue_sequence')},worker_instance_id='a'*32,status='running',stage_runs=[{'stage_run_id':'monomer_qm-1','stage':'monomer_qm','status':'running'}]))
    for path in ('results','stages','stages/monomer_qm-1/logs'):
        url=f'/api/v1/polymer-md/jobs/{job_id}/'+path
        assert bob.get(url).status_code==404
        assert alice.get(url).status_code==200
    logs=alice.get(f'/api/v1/polymer-md/jobs/{job_id}/stages/monomer_qm-1/logs?tail_lines=7')
    assert logs.json()['stage_run_id']=='monomer_qm-1' and app.state.polymer_md_worker_client.calls[-1][2]['tail_lines']==7
    assert alice.get(f'/api/v1/polymer-md/jobs/{job_id}/stages/monomer_qm-1/logs?tail_lines=1001').status_code==422
    assert alice.get(f'/api/v1/polymer-md/jobs/{job_id}/stages/other/logs').status_code==404


def test_stage_scoped_artifacts_and_verified_download(polymer_api):
    app,alice,bob,db=polymer_api
    response=alice.post("/api/v1/polymer-md/jobs",json={"smiles":"*CC*","seed":7},headers={"Idempotency-Key":"polymer-artifacts-request"})
    assert response.status_code==202,response.text
    job_id=response.json()["job_id"];repo=app.state.polymer_md_repository
    internal=repo.get_service_job(job_id);repo.claim_dispatch(job_id,internal["attempt_token"])
    submit=submit_request(internal)
    repo.authorize_start(StartAuthorizationRequest(**submit.model_dump(),worker_instance_id="a"*32))
    stage={"stage_run_id":"eq-1","stage":"eq","status":"completed","quality_status":"passed"}
    artifact={"artifact_id":"eq-json","stage_run_id":"eq-1","kind":"json","filename":"eq.json","media_type":"application/json","size_bytes":2,"sha256":hashlib.sha256(b"{}").hexdigest()}
    snapshot=WorkerSnapshot(**{k:getattr(submit,k) for k in ("job_id","attempt_token","request_sha256","enqueue_sequence")},worker_instance_id="a"*32,status="completed",cleanup_confirmed=True,stage_runs=[stage],artifacts=[artifact])
    repo.apply_snapshot(snapshot)
    assert bob.get(f"/api/v1/polymer-md/jobs/{job_id}/artifacts/eq-json").status_code==404
    valid=alice.get(f"/api/v1/polymer-md/jobs/{job_id}/artifacts/eq-json")
    assert valid.status_code==200 and valid.content==b"{}"
    assert valid.headers['cache-control']=='private, no-store'
    assert alice.get(f"/api/v1/polymer-md/jobs/{job_id}/stages/other/trajectory").status_code==404
    frame=alice.get(f"/api/v1/polymer-md/jobs/{job_id}/stages/eq-1/trajectory")
    assert frame.status_code==200 and frame.json()['job_id']==job_id
    assert alice.delete(f"/api/v1/polymer-md/jobs/{job_id}/artifacts").status_code==200
    assert alice.get(f"/api/v1/polymer-md/jobs/{job_id}/artifacts/eq-json").status_code==404
    assert alice.get(f"/api/v1/polymer-md/jobs/{job_id}").status_code==200


def test_readonly_mount_download_hides_storage_ref_rejects_symlink_and_bad_hash(polymer_api,tmp_path):
    app,alice,bob,db=polymer_api
    created=alice.post("/api/v1/polymer-md/jobs",json={"smiles":"*CC*","seed":7},headers={"Idempotency-Key":"polymer-mounted-request"})
    job_id=created.json()["job_id"];repo=app.state.polymer_md_repository
    job=repo.get_service_job(job_id);repo.claim_dispatch(job_id,job["attempt_token"])
    submit=submit_request(job)
    repo.authorize_start(StartAuthorizationRequest(**submit.model_dump(),worker_instance_id="a"*32))
    relative=f'{job_id}/{job["attempt_token"]}/science/eq3/result.json'
    artifact={"artifact_id":"eq-json","stage_run_id":"eq3-1","kind":"json","filename":"result.json","media_type":"application/json","size_bytes":2,"sha256":hashlib.sha256(b"{}").hexdigest(),"storage_ref":relative}
    snapshot=WorkerSnapshot(**{k:getattr(submit,k) for k in ("job_id","attempt_token","request_sha256","enqueue_sequence")},worker_instance_id="a"*32,status="completed",cleanup_confirmed=True,stage_runs=[{"stage_run_id":"eq3-1","stage":"eq3","status":"completed"}],artifacts=[artifact])
    repo.apply_snapshot(snapshot)
    root=tmp_path/'mounted';path=root/relative;path.parent.mkdir(parents=True);path.write_bytes(b"{}")
    app.state.settings.polymer_md_artifact_root=str(root)
    url=f"/api/v1/polymer-md/jobs/{job_id}/artifacts/eq-json"
    assert bob.get(url).status_code==404
    assert alice.get(url).content==b"{}"
    for endpoint in (f"/api/v1/polymer-md/jobs/{job_id}",f"/api/v1/polymer-md/jobs/{job_id}/artifacts","/api/v1/polymer-md/jobs"):
        text=alice.get(endpoint).text
        assert 'storage_ref' not in text and job['attempt_token'] not in text
    path.write_bytes(b"[]")
    assert alice.get(url).json()['code']=='artifact_integrity_mismatch'
    path.unlink();target=tmp_path/'outside';target.write_bytes(b"{}");path.symlink_to(target)
    assert alice.get(url).json()['code']=='artifact_storage_unavailable'
    assert list(tmp_path.glob('polymer-md-*'))==[]


def test_explicit_development_migration_is_governed_and_idempotent(polymer_api):
    app,alice,bob,db=polymer_api
    from app.services.polymer_md_schema import apply_polymer_md_dev_migration
    with pytest.raises(ValueError,match='development'):
        apply_polymer_md_dev_migration(db['admin'],environment='production')
    # This database is disposable. Recreate exactly the optional expansion
    # through the public migration function rather than manual ledger inserts.
    with psycopg.connect(db['admin']) as c:
        c.execute('DROP SCHEMA polymer_md CASCADE')
        c.execute("DELETE FROM governance.schema_migrations WHERE version='0023_polymer_md_jobs'")
    assert apply_polymer_md_dev_migration(db['admin'],environment='dev').applied
    assert not apply_polymer_md_dev_migration(db['admin'],environment='dev').applied
    assert app.state.polymer_md_repository.schema_ready()


def test_0023_preserves_existing_readiness_and_backup_covers_all_children(polymer_api,tmp_path):
    app,alice,bob,db=polymer_api
    from app.auth.cutover import backup_state_seal
    from app.auth.backup import validate_backup_schema
    from app.auth.assets import archive_assets
    from app.auth.schema import validate_isolation_schema
    from app.auth.isolation_ledger import validate_service_migration_ledger
    created=alice.post("/api/v1/polymer-md/jobs",json={"smiles":"*CC*"},headers={"Idempotency-Key":"polymer-backup-request"})
    assert created.status_code==202
    with psycopg.connect(db["admin"],row_factory=dict_row) as c:
        assert validate_service_migration_ledger(c, through='0023_polymer_md_jobs')[-1]['version']=='0023_polymer_md_jobs'
        validate_isolation_schema(c, '0023_polymer_md_jobs')
        assert validate_backup_schema(c)['current_readiness']
        seal=backup_state_seal(c)
    assert seal['polymer_md.jobs']['rows']==1
    assert seal['polymer_md.job_attempts']['rows']==1
    assert 'polymer_md.stage_runs' in seal and 'polymer_md.artifacts' in seal
    output=tmp_path/'backup';output.mkdir()
    with pytest.raises(ValueError,match='polymer_md'):
        archive_assets({},output,seal)


def test_0023_real_dump_restore_preserves_four_tables_private_files_and_owner_downloads(polymer_api,tmp_path,monkeypatch):
    """Synthetic observations exercise actual 0023 backup, files and real auth.

    Every source file is local to this disposable fixture. No real Worker job,
    scientific runtime, active jobroot or persistent development DB is opened.
    """
    from app.auth.assets import asset_snapshot,file_sha256
    from app.auth.backup import backup_and_verify,validate_backup_schema
    from app.auth.context import Identity,user_context
    from app.auth.cutover import backup_state_seal,auth_metadata_seal,private_data_seal
    from app.auth.schema import validate_isolation_schema,validate_runtime_role
    from app.postgres_database import postgres_connection
    from app.services.polymer_md_schema import validate_polymer_md_schema
    from test_multiuser_restore import _postgres_tools
    app,alice,bob,db=polymer_api
    source_root=tmp_path/'synthetic-source-jobs';source_root.mkdir(mode=0o700)
    source_clients=(alice,bob);expected=[]
    repository=app.state.polymer_md_repository
    for index,client in enumerate(source_clients):
        response=client.post('/api/v1/polymer-md/jobs',json={'smiles':'*CC*','seed':7+index},headers={'Idempotency-Key':f'polymer-restore-owner-{index}'})
        assert response.status_code==202,response.text
        job=repository.get_service_job(response.json()['job_id'])
        repository.claim_dispatch(job['job_id'],job['attempt_token'])
        request=submit_request(job)
        repository.authorize_start(StartAuthorizationRequest(**request.model_dump(),worker_instance_id='a'*32))
        content=(json.dumps({'fixture':'synthetic-backup-acceptance','owner_index':index})+'\n').encode()
        relative=f'{job["job_id"]}/{job["attempt_token"]}/science/eq3/result.json'
        path=source_root/relative;path.parent.mkdir(mode=0o700,parents=True);path.write_bytes(content);path.chmod(0o600)
        snapshot=WorkerSnapshot(**{key:getattr(request,key) for key in ('job_id','attempt_token','request_sha256','enqueue_sequence')},
            worker_instance_id='a'*32,status='completed',cleanup_confirmed=True,
            stage_runs=[{'stage_run_id':'eq3-1','stage':'eq3','status':'completed','results':{'fixture':'synthetic-backup-acceptance'}}],
            artifacts=[{'artifact_id':'result','stage_run_id':'eq3-1','kind':'analysis','filename':'result.json',
                        'media_type':'application/json','size_bytes':len(content),'sha256':hashlib.sha256(content).hexdigest(),'storage_ref':relative}])
        repository.apply_snapshot(snapshot)
        expected.append({'job_id':job['job_id'],'owner':client.test_owner_id,'relative':relative,'content':content})
    with psycopg.connect(db['admin'],row_factory=dict_row) as connection:
        major=connection.info.server_version//10000
        before=backup_state_seal(connection)
        before_auth=auth_metadata_seal(connection)
        before_private=private_data_seal(connection)
        before_schema=validate_backup_schema(connection)
        users=[dict(connection.execute('SELECT user_id,username FROM auth.users WHERE user_id=%s',(item['owner'],)).fetchone()) for item in expected]
    for relation in ('jobs','job_attempts','stage_runs','artifacts'):
        assert before['polymer_md.'+relation]['rows']==2
    tools=_postgres_tools(major)
    assert tools['pg_dump'].parent==tools['pg_restore'].parent
    monkeypatch.setenv('POSTGRES_BIN',str(tools['pg_dump'].parent))
    destination='polymer_md_restore_'+uuid4().hex
    restored={kind:make_conninfo(dsn,dbname=destination) for kind,dsn in db.items()}
    try:
        with psycopg.connect(db['admin'],autocommit=True) as connection:
            connection.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(destination)))
        receipt_path=backup_and_verify(db['admin'],restored['admin'],tmp_path/'backup',asset_roots={'polymer_md':source_root})
        receipt=json.loads(receipt_path.read_text())
        assert receipt['restore_verified'] and receipt['backup_state']==before
        assert receipt['isolation_schema']==before_schema
        assert receipt['auth_metadata']==before_auth and receipt['business_data']==before_private
        assert receipt['isolation_schema']['migration_ledger'][-1]['version']=='0023_polymer_md_jobs'
        assert receipt_path.stat().st_mode&0o777==0o600
        assert Path(receipt['backup_path']).stat().st_mode&0o777==0o600
        asset=receipt['assets']['polymer_md']
        assert file_sha256(Path(asset['archive_path']))==asset['archive_sha256']
        unpack=tmp_path/'restored-files';unpack.mkdir(mode=0o700)
        with tarfile.open(asset['archive_path'],'r:gz') as archive:
            archive.extractall(unpack,filter='data')
        restored_root=unpack/'assets'
        assert asset_snapshot(restored_root)==asset['files']==asset_snapshot(source_root)
        with psycopg.connect(restored['admin'],row_factory=dict_row) as connection:
            assert backup_state_seal(connection)==before
            assert auth_metadata_seal(connection)==before_auth
            assert private_data_seal(connection)==before_private
            validate_isolation_schema(connection);validate_polymer_md_schema(connection)
            flags=connection.execute("SELECT relrowsecurity,relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='polymer_md' AND c.relkind='r'").fetchall()
            assert len(flags)==4 and all(row['relrowsecurity'] and row['relforcerowsecurity'] for row in flags)
        # Restored real API/auth/service logins use the restored database. The
        # download must succeed with no Worker transport, from restored files.
        monkeypatch.setenv('APP_SERVICE_POSTGRES_DSN',restored['service'])
        restored_app=FastAPI()
        restored_app.state.settings=SimpleNamespace(polymer_md_dev_enabled=True,polymer_md_submit_enabled=False,
            polymer_md_start_authorization_token='test-only-token',polymer_md_artifact_root=str(restored_root),
            polymer_md_download_spool_root=str(tmp_path/'unused-spool'))
        restored_app.state.polymer_md_repository=PolymerMdRepository(restored['api'])
        restored_app.state.polymer_md_worker_client=None
        restored_app.state.polymer_md_reconciler=None
        restored_app.state.polymer_md_download_limiter=anyio.CapacityLimiter(2)
        restored_app.include_router(router)
        clients=[]
        try:
            for user in users:
                clients.append(authenticated_client(restored_app,restored,user={**user,'_activated':True}))
            restored_app.state.auth.assert_application_ready()
            for index,client in enumerate(clients):
                item=expected[index];foreign=expected[1-index]
                # Existing session hashes also retain their original identity.
                token=source_clients[index].cookies.get('nexpoly_test_session')
                session=restored_app.state.auth.resolve(token)
                assert str(session['user_id'])==item['owner']
                with user_context(Identity(item['owner']),restored_app.state.auth.settings),postgres_connection(restored['api']) as connection:
                    assert validate_runtime_role(connection,'nexpoly_api')['privilege_group']=='nexpoly_api'
                    for table in ('jobs','job_attempts','stage_runs','artifacts'):
                        rows=connection.execute(sql.SQL('SELECT job_id::text AS job_id FROM polymer_md.{}').format(sql.Identifier(table))).fetchall()
                        assert rows==[{'job_id':item['job_id']}],table
                listing=client.get('/api/v1/polymer-md/jobs')
                assert listing.status_code==200 and listing.json()['total']==1
                assert listing.json()['items'][0]['job_id']==item['job_id']
                assert 'storage_ref' not in listing.text
                url=f'/api/v1/polymer-md/jobs/{item["job_id"]}/artifacts/result'
                download=client.get(url)
                assert download.status_code==200 and download.content==item['content']
                assert download.headers['cache-control']=='private, no-store'
                for suffix in ('','/artifacts/result','/results','/stages'):
                    denied=client.get(f'/api/v1/polymer-md/jobs/{foreign["job_id"]}'+suffix)
                    absent=client.get(f'/api/v1/polymer-md/jobs/{uuid4()}'+suffix)
                    assert denied.status_code==absent.status_code==404
                    assert denied.json()==absent.json()
            with postgres_connection(restored['api']) as connection:
                for table in ('jobs','job_attempts','stage_runs','artifacts'):
                    assert connection.execute(sql.SQL('SELECT count(*) AS n FROM polymer_md.{}').format(sql.Identifier(table))).fetchone()['n']==0
            target=restored_root/expected[0]['relative']
            target.write_bytes(b'x'*len(expected[0]['content']))
            corrupted=clients[0].get(f'/api/v1/polymer-md/jobs/{expected[0]["job_id"]}/artifacts/result')
            assert corrupted.status_code==502 and corrupted.json()['code']=='artifact_integrity_mismatch'
        finally:
            for client in clients:client.close()
        with psycopg.connect(db['admin'],row_factory=dict_row) as connection:
            assert backup_state_seal(connection)==before
            assert auth_metadata_seal(connection)==before_auth
        assert asset_snapshot(source_root)==asset['files']
    finally:
        with psycopg.connect(db['admin'],autocommit=True) as connection:
            connection.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(destination)))
    with psycopg.connect(db['admin']) as connection:
        assert not connection.execute('SELECT 1 FROM pg_database WHERE datname=%s',(destination,)).fetchone()
