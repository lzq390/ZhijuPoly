from uuid import uuid4
import asyncio
import json
from typing import get_args

import pytest
from pydantic import ValidationError

from polymer_md.contracts import (Artifact, Forcefield, ModelConfiguration, NativeConfiguration, NativeProperty, StageRun,
                                  ScientificRequest, StartAuthorizationRequest, SubmitRequest, WorkerSnapshot, request_sha256)
from app.services.polymer_md_repository import PolymerMdError, validate_science, submit_request


def test_scientific_contract_limits_and_real_chemistry_validation():
    request = ScientificRequest(smiles="*CC*", seed=None)
    assert validate_science(request).smiles == "*CC*"
    for change in ({"n_atom":5000,"n_chain":5}, {"n_atom":4000,"n_chain":5,"calculation":"basic_tc","tc_rep":4}, {"max_additional":3}, {"eq_ns":float("nan")}, {"forcefield":"OPLS-AA"}):
        with pytest.raises(ValidationError):
            ScientificRequest(smiles="*CC*", **change)
    for smiles in ("*[NH2+]CC*", "*[CH]CC*", "*CC[Na]*", "*CC.*", "*CC*\x00"):
        with pytest.raises((ValueError,PolymerMdError)):
            validate_science(ScientificRequest(smiles=smiles))


def test_legacy_request_keeps_exact_eighteen_fields_and_nested_wire_hash():
    science = ScientificRequest(smiles="*CC*", seed=1729)
    explicit_null = ScientificRequest(smiles="*CC*", seed=1729, native_configuration=None, model_configuration=None)
    expected_hash = "e69e9d0e7eaa7d258ea761b1be5c5597545e5d393bc16252a8bfad3d2c3217cc"
    assert len(science.model_dump()) == 18
    assert "native_configuration" not in science.model_dump(mode="json")
    assert science.model_dump() == explicit_null.model_dump()
    assert request_sha256(science) == request_sha256(explicit_null) == expected_hash
    submit = SubmitRequest(job_id=str(uuid4()), owner_user_id=str(uuid4()), attempt_token="a" * 64,
                           request_sha256=expected_hash, enqueue_sequence=1, scientific_request=science)
    for wire in (submit.model_dump(), submit.model_dump(mode="json"), json.loads(submit.model_dump_json())):
        assert len(wire["scientific_request"]) == 18
        assert "native_configuration" not in wire["scientific_request"]
        assert request_sha256(SubmitRequest.model_validate(wire).scientific_request) == expected_hash


def test_native_choices_are_in_the_hash_and_survive_nested_round_trip():
    initial = ScientificRequest(smiles="*CC*", seed=1729, native_configuration={"properties": ["density"]})
    assert len(initial.model_dump(mode="json")) == 19
    assert request_sha256(initial) == "1071c66ce0c39ac4ee5432cd66dff978d2dff1be5c9826a3d70f8a31a2007366"
    assert request_sha256(initial) == request_sha256(ScientificRequest.model_validate({**initial.model_dump(), "model_configuration": None}))
    assert "model_configuration" not in initial.model_dump(mode="json")
    assert request_sha256(initial) != request_sha256(ScientificRequest(smiles="*CC*", seed=1729))
    for field, value in {"opt_method": "HF", "opt_basis": "6-31G(d)", "charge": "ESP",
                         "charge_method": "wb97m-d3bj", "charge_basis": "6-31G(d,p)",
                         "sp_method": "HF", "sp_basis": "6-31G(d)", "polar_method": "HF",
                         "polar_basis": "6-31G(d)", "properties": ["Rg"]}.items():
        native = {**initial.native_configuration.model_dump(), field: value}
        changed = ScientificRequest(smiles="*CC*", seed=1729, native_configuration=native)
        assert request_sha256(changed) != request_sha256(initial), field
    assert request_sha256(ScientificRequest(smiles="*CC*", seed=1729, forcefield="GAFF")) != request_sha256(ScientificRequest(smiles="*CC*", seed=1729))
    submit = SubmitRequest(job_id=str(uuid4()), owner_user_id=str(uuid4()), attempt_token="a" * 64,
                           request_sha256=request_sha256(initial), enqueue_sequence=1, scientific_request=initial)
    restored = SubmitRequest.model_validate_json(submit.model_dump_json())
    assert restored.scientific_request.native_configuration == initial.native_configuration
    assert request_sha256(restored.scientific_request) == request_sha256(initial)


def test_native_schema_only_accepts_closed_literals_and_property_subsets():
    for forcefield in get_args(Forcefield):
        assert ScientificRequest(smiles="*CC*", forcefield=forcefield).forcefield == forcefield
    for target in get_args(NativeProperty):
        calculation = "basic_tc" if target in {"thermal_conductivity", "thermal_diffusivity"} else "basic"
        assert ScientificRequest(smiles="*CC*", calculation=calculation,
                                 native_configuration={"properties": [target]}).native_configuration.properties == [target]
    molecular = ["qm_total_energy", "qm_homo", "qm_lumo", "qm_dipole", "qm_polarizability"]
    assert ScientificRequest(smiles="*CC*", native_configuration={"properties": molecular}).calculation == "basic"
    for invalid in ({"properties": []}, {"properties": ["density", "density"]},
                    {"properties": ["electronic_corrected_dielectric"]}, {"properties": ["unmapped_diffusivity"]},
                    {"properties": ["density"], "opt_method": "HF; shell"},
                    {"properties": ["density"], "opt_basis": "6-31G(d)\nset basis other"},
                    {"properties": ["density"], "charge": "RESP2"},
                    {"properties": ["density"], "solver_path": "/private/engine"}):
        with pytest.raises(ValidationError):
            NativeConfiguration.model_validate(invalid)
    for calculation, properties in (("basic", ["thermal_conductivity"]), ("basic_tc", ["density"])):
        with pytest.raises(ValidationError, match="thermal_conductivity"):
            ScientificRequest(smiles="*CC*", calculation=calculation, native_configuration={"properties": properties})
    a = ScientificRequest(smiles="*CC*", native_configuration={"properties": ["density", "Rg"]})
    b = ScientificRequest(smiles="*CC*", native_configuration={"properties": ["Rg", "density"]})
    assert request_sha256(a) == request_sha256(b)


def test_postequilibrium_targets_use_existing_workflows_and_reject_single_chain_sp():
    assert len(get_args(NativeProperty)) == 25
    for target in ("sp_total", "sp_ced"):
        with pytest.raises(ValidationError, match="at least two polymer chains"):
            ScientificRequest(smiles="*CC*", n_chain=1, native_configuration={"properties": [target]})
        assert ScientificRequest(smiles="*CC*", n_chain=2, native_configuration={"properties": [target]}).calculation == "basic"
    assert ScientificRequest(smiles="*CC*", n_chain=1, native_configuration={"properties": ["tg"]}).calculation == "basic"
    for properties, calculation in ((["tg", "sp_total", "sp_ced"], "basic"),
                                    (["tg", "sp_total", "thermal_conductivity"], "basic_tc")):
        science = ScientificRequest(smiles="*CC*", n_chain=2, calculation=calculation, native_configuration={"properties": properties})
        assert science.native_configuration.properties == sorted(properties)
        assert "posteq_configuration" not in science.model_dump()
    model = {"version": 1, "mode": "blend", "components": [
        {"id": "A", "smiles": "*CC*", "n_chain": 1, "chain_length": {"mode": "dp", "value": 10}},
        {"id": "B", "smiles": "*CCO*", "n_chain": 1, "chain_length": {"mode": "dp", "value": 10}}]}
    assert ScientificRequest(smiles="*CC*", n_chain=2, model_configuration=model,
                             native_configuration={"properties": ["sp_total"]})
    # Sampling settings are a platform protocol, not silently accepted user fields.
    with pytest.raises(ValidationError):
        ScientificRequest(smiles="*CC*", native_configuration={"properties": ["tg"]},
                          posteq_configuration={"version": 1, "tg_min_temp_k": 200})
    for name in ("sp", "sp_analyze", "tg", "tg_analyze"):
        assert StageRun(stage_run_id=name + "-1", stage=name, status="pending").stage == name


def model_configuration(mode="random"):
    components = [{"id": "A", "smiles": "*CC*"}, {"id": "B", "smiles": "*CCO*"}]
    base = {"version": 1, "mode": mode, "components": components}
    if mode == "homopolymer":
        return {**base, "components": components[:1], "chain_length": {"mode": "dp", "value": 20}}
    if mode == "block":
        return {**base, "blocks": [{"component_id": "A", "dp": 10}, {"component_id": "B", "dp": 20}, {"component_id": "A", "dp": 10}]}
    if mode == "blend":
        return {**base, "components": [{**component, "n_chain": 2, "chain_length": {"mode": "dp", "value": 20}}
                                       for component in components]}
    return {**base, "chain_length": {"mode": "dp", "value": 20}, **({"ratios": [.4, .6]} if mode == "random" else {})}


@pytest.mark.parametrize("mode", ["homopolymer", "alternating", "random", "block", "blend"])
def test_model_modes_have_explicit_exact_round_trip_and_hashed_configuration(mode):
    configuration = model_configuration(mode)
    science = ScientificRequest(smiles="*CC*", n_chain=4, model_configuration=configuration, seed=1729)
    wire = science.model_dump(mode="json")
    assert wire["model_configuration"] == configuration
    assert len(wire) == 19 and "native_configuration" not in wire
    assert request_sha256(science) != request_sha256(ScientificRequest(smiles="*CC*", n_chain=4, seed=1729))
    submit = SubmitRequest(job_id=str(uuid4()), owner_user_id=str(uuid4()), attempt_token="a" * 64,
                           request_sha256=request_sha256(science), enqueue_sequence=1, scientific_request=science)
    restored = SubmitRequest.model_validate_json(submit.model_dump_json())
    assert restored.scientific_request.model_configuration == science.model_configuration
    assert "chain_length" not in wire["model_configuration"]["components"][0] or mode == "blend"
    assert validate_science(science).model_configuration.mode == mode


@pytest.mark.parametrize("change", [
    {"mode": "crosslinked"}, {"unknown": True}, {"version": 2}, {"version": True}, {"version": 1.0},
    {"components": [{"id": "A", "smiles": "*CC*"}, {"id": "A", "smiles": "*CCO*"}]},
    {"components": [{"id": "../A", "smiles": "*CC*"}, {"id": "B", "smiles": "*CCO*"}]},
    {"components": [{"id": "A", "smiles": "CC"}, {"id": "B", "smiles": "*CCO*"}]},
    {"ratios": [0, 1]}, {"ratios": [.4, .4]}, {"ratios": [.5]}, {"ratios": [True, False]},
    {"chain_length": {"mode": "dp", "value": 2.5}}, {"chain_length": {"mode": "atoms", "value": 99}},
    {"chain_length": {"mode": "dp", "value": True}}, {"chain_length": {"mode": "molecular_weight", "value": float("nan")}},
    {"blocks": [{"component_id": "A", "dp": 2}, {"component_id": "B", "dp": 2}]},
])
def test_model_configuration_rejects_ambiguous_or_unsafe_targets(change):
    with pytest.raises(ValidationError):
        ModelConfiguration.model_validate({**model_configuration(), **change})


def test_model_cross_field_consistency_and_mode_specific_fields():
    for configuration, change in [
        (model_configuration(), {"smiles": "*CO*"}),
        (model_configuration("blend"), {"n_chain": 3}),
        ({**model_configuration("block"), "blocks": [{"component_id": "A", "dp": 2}, {"component_id": "missing", "dp": 2}]}, {}),
        ({**model_configuration("blend"), "chain_length": {"mode": "dp", "value": 10}}, {}),
        ({**model_configuration("alternating"), "ratios": [.5, .5]}, {}),
        ({**model_configuration("homopolymer"), "components": model_configuration()["components"]}, {}),
    ]:
        with pytest.raises(ValidationError):
            ScientificRequest(**{"smiles": "*CC*", "n_chain": 4, "model_configuration": configuration, **change})
    for value in (100, 5000):
        assert ModelConfiguration.model_validate({**model_configuration("homopolymer"), "chain_length": {"mode": "atoms", "value": value}})
    assert ModelConfiguration.model_validate({**model_configuration("homopolymer"), "chain_length": {"mode": "molecular_weight", "value": 5000.5}})
    with pytest.raises(ValidationError, match="molecular-only subsets are legacy-only"):
        ScientificRequest(smiles="*CC*", model_configuration=model_configuration("homopolymer"),
                          native_configuration={"properties": ["qm_homo"]})
    assert ScientificRequest(smiles="*CC*", native_configuration={"properties": ["qm_homo"]})


def test_every_component_is_chemically_validated_and_canonicalized_before_hashing():
    invalid = {**model_configuration(), "components": [{"id": "A", "smiles": "*CC*"}, {"id": "B", "smiles": "*[NH2+]CC*"}]}
    with pytest.raises(PolymerMdError, match="中性闭壳层"):
        validate_science(ScientificRequest(smiles="*CC*", model_configuration=invalid))
    configuration = {**model_configuration(), "components": [{"id": "A", "smiles": "*OCC*"}, {"id": "B", "smiles": "*OCCC*"}]}
    original = ScientificRequest(smiles="*OCC*", model_configuration=configuration, seed=7)
    canonical = validate_science(original)
    assert canonical.smiles == canonical.model_configuration.components[0].smiles == "*CCO*"
    assert request_sha256(validate_science(canonical)) == request_sha256(canonical)
    assert original.smiles == "*OCC*"  # Normalization never mutates caller input.


def test_chemical_atom_budget_uses_dp_blocks_and_blend_counts_not_legacy_placeholder():
    small = {**model_configuration("homopolymer"), "chain_length": {"mode": "dp", "value": 2}}
    accepted = ScientificRequest(smiles="*CC*", n_atom=5000, n_chain=20, model_configuration=small)
    assert validate_science(accepted).model_configuration.chain_length.value == 2
    from polymer_md.model_configuration import estimate_model_atoms
    stats = {"A": {"atoms": 6, "mass": 28.054}, "B": {"atoms": 7, "mass": 44.053}}
    caps = [{"atoms": 4, "mass": 15.035}] * 2
    for mode, expected in (("homopolymer", 128 * 4), ("alternating", 268 * 4), ("random", 140 * 4),
                           ("block", 268 * 4), ("blend", 128 * 2 + 148 * 2)):
        science = ScientificRequest(smiles="*CC*", n_chain=4, model_configuration=model_configuration(mode))
        assert estimate_model_atoms(science.model_dump(mode="json"), stats, caps) == expected
    large = {**model_configuration("homopolymer"), "chain_length": {"mode": "dp", "value": 5000}}
    with pytest.raises(PolymerMdError, match="原子数上限"):
        validate_science(ScientificRequest(smiles="*CC*", n_chain=1, model_configuration=large))
    medium = {**model_configuration("homopolymer"), "chain_length": {"mode": "dp", "value": 2000}}
    with pytest.raises(PolymerMdError, match="原子数上限"):
        validate_science(ScientificRequest(smiles="*CC*", n_chain=1, tc_rep=5, calculation="basic_tc", model_configuration=medium))


def test_worker_hash_seed_cleanup_and_manifest_fences():
    science = ScientificRequest(smiles="*CC*",seed=17)
    submit = SubmitRequest(job_id=str(uuid4()),owner_user_id=str(uuid4()),attempt_token="a"*64,request_sha256=request_sha256(science),enqueue_sequence=1,scientific_request=science)
    with pytest.raises(ValidationError):
        SubmitRequest.model_validate({**submit.model_dump(),"request_sha256":"b"*64})
    base = {k:getattr(submit,k) for k in ("job_id","attempt_token","request_sha256","enqueue_sequence")}
    base.update(worker_instance_id="c"*32,status="failed")
    with pytest.raises(ValidationError):
        WorkerSnapshot(**base)
    accepted = WorkerSnapshot(**base,cleanup_confirmed=True)
    assert accepted.quality_status == "not_evaluated"
    with pytest.raises(ValidationError):
        WorkerSnapshot(**base,cleanup_confirmed=True,results={"properties":[{"value":float("nan")}]})
    with pytest.raises(ValidationError):
        WorkerSnapshot(**base,cleanup_confirmed=True,artifacts=[{"artifact_id":"a","stage_run_id":"missing","kind":"log","filename":"x.log","media_type":"text/plain","size_bytes":0,"sha256":"0"*64}])


def test_hard_off_settings_do_not_require_worker_or_runtime(monkeypatch):
    from app.config import Settings
    for key in ("POLYMER_MD_DEV_ENABLED","POLYMER_MD_SUBMIT_ENABLED","POLYMER_MD_WORKER_UDS","POLYMER_MD_START_AUTHORIZATION_TOKEN"):
        monkeypatch.delenv(key,raising=False)
    settings = Settings()
    assert not settings.polymer_md_dev_enabled
    assert not settings.polymer_md_submit_enabled
    with pytest.raises(ValueError):
        Settings(polymer_md_dev_enabled=True,polymer_md_environment="production")


def test_artifact_storage_reference_is_relative_and_attempt_bound():
    base=dict(artifact_id='log',stage_run_id='eq3-1',kind='log',filename='engine.log',size_bytes=0,sha256='0'*64)
    for reference in ('/tmp/leak','a/../b','a/./b','a//b','a\\b','a/secret name'):
        with pytest.raises(ValidationError):
            Artifact(**base,storage_ref=reference)
    with pytest.raises(ValidationError,match='another execution attempt'):
        WorkerSnapshot(job_id=str(uuid4()),attempt_token='a'*64,request_sha256='b'*64,enqueue_sequence=1,worker_instance_id='c'*32,status='queued',stage_runs=[dict(stage_run_id='eq3-1',stage='eq3')],artifacts=[{**base,'storage_ref':f'{uuid4()}/'+('a'*64)+'/science/engine.log'}])


def test_worker_cleanup_receipt_requires_confirmed_exact_execution_identity():
    import httpx
    from app.services.polymer_md_worker_client import PolymerMdWorkerClient
    science=ScientificRequest(smiles='*CC*',seed=7)
    request=SubmitRequest(job_id=str(uuid4()),owner_user_id=str(uuid4()),attempt_token='a'*64,request_sha256=request_sha256(science),enqueue_sequence=1,scientific_request=science)
    valid={**{key:getattr(request,key) for key in ('job_id','attempt_token','request_sha256','enqueue_sequence')},'deleted':True,'cleanup_confirmed':True}
    async def exercise():
        for reply in ({'deleted':True},{**valid,'cleanup_confirmed':False},{**valid,'enqueue_sequence':2},{**valid,'attempt_token':'b'*64},valid):
            async with httpx.AsyncClient(base_url='http://worker',transport=httpx.MockTransport(lambda _:httpx.Response(200,json=reply))) as transport:
                worker=PolymerMdWorkerClient('/private/worker.sock',client=transport)
                if reply==valid:
                    assert await worker.delete(request)==valid
                else:
                    with pytest.raises(PolymerMdError):
                        await worker.delete(request)
    asyncio.run(exercise())


def test_analysis_not_found_and_busy_are_distinct_from_execution_uncertainty():
    import httpx
    from app.services.polymer_md_worker_client import PolymerMdWorkerClient
    science=ScientificRequest(smiles='*CC*',seed=7)
    request=SubmitRequest(job_id=str(uuid4()),owner_user_id=str(uuid4()),attempt_token='a'*64,request_sha256=request_sha256(science),enqueue_sequence=1,scientific_request=science)
    async def exercise():
        for status in (404,429):
            async with httpx.AsyncClient(base_url='http://worker',transport=httpx.MockTransport(lambda _:httpx.Response(status,json={'error':{'message':'/private/never-relay'}}))) as transport:
                worker=PolymerMdWorkerClient('/private/worker.sock',client=transport)
                for logs in (False,True):
                    with pytest.raises(PolymerMdError) as error:
                        if logs:
                            await worker.stage_logs(request,'eq3-1')
                        else:
                            await worker.stage_json(request,'eq3-1')
                    assert error.value.status==status
                    assert error.value.code==('analysis_busy' if status==429 else 'stage_not_found' if logs else 'trajectory_not_found')
                    assert '/private' not in str(error.value)
                # A status alone never proves execution or deletion cleanup.
                with pytest.raises(PolymerMdError) as error:
                    await worker.json('POST','/jobs',payload=request.model_dump())
                assert error.value.status==503 and error.value.code=='worker_response_unknown'
                with pytest.raises(PolymerMdError) as error:
                    await worker.delete(request)
                assert error.value.status==503
    asyncio.run(exercise())


def test_large_artifact_hash_wait_has_its_own_bounded_transport_timeout():
    import httpx
    from app.services.polymer_md_worker_client import PolymerMdWorkerClient
    science = ScientificRequest(smiles='*CC*', seed=7)
    request = SubmitRequest(job_id=str(uuid4()), owner_user_id=str(uuid4()), attempt_token='a'*64,
        request_sha256=request_sha256(science), enqueue_sequence=1, scientific_request=science)
    observed = []
    def transport(request):
        observed.append(request.extensions['timeout'])
        return httpx.Response(200, json={'ready': True})
    async def exercise():
        async with httpx.AsyncClient(base_url='http://worker', transport=httpx.MockTransport(transport), timeout=30) as client:
            worker = PolymerMdWorkerClient('/private/worker.sock', client=client)
            await worker.json('GET', '/health')
            async with worker.artifact_stream(request, 'large-trajectory') as response:
                assert await response.aread()
            await worker.json('GET', '/health')
    asyncio.run(exercise())
    assert observed == [
        {'connect': 30, 'read': 30, 'write': 30, 'pool': 30},
        {'connect': 30, 'read': 600, 'write': 30, 'pool': 30},
        {'connect': 30, 'read': 30, 'write': 30, 'pool': 30},
    ]


class _OfflineResult:
    def __init__(self, value): self.value = value
    def fetchone(self): return self.value
    def fetchall(self): return self.value


def _offline_repository(monkeypatch, *, tier="member", quota=False, active=True, durable=False, replay=False):
    from datetime import datetime, timezone, timedelta
    from app.services import polymer_md_repository as module
    from task_usage import entitlements
    owner = "00000000-0000-0000-0000-000000000002"
    payload = ScientificRequest(smiles="*CC*", seed=7)
    created = datetime(2026, 10, 1, tzinfo=timezone.utc)

    class Connection:
        def __init__(self):
            self.trace = []
            self.writes = []
            self.job = {"job_id": str(uuid4()), "owner_user_id": owner,
                "attempt_token": "a"*64, "request_sha256": request_sha256(payload),
                "scientific_request": payload.model_dump(mode="json"), "enqueue_sequence": 1,
                "worker_instance_id": "a"*32 if durable else None, "status": "running" if durable else "queued",
                "start_authorized_at": created + timedelta(seconds=2) if durable else None,
                "created_at": created, "started_at": None, "finished_at": None,
                "cleanup_confirmed": False, "stage_runs": [], "artifacts": []}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, query, params=()):
            if "FROM auth.users" in query:
                assert "FOR UPDATE" in query
                self.trace.append("account_lock")
                return _OfflineResult({"status": "active" if active else "disabled", "is_system": False})
            if "SELECT pg_advisory_xact_lock" in query:
                self.trace.append("capacity_lock"); return _OfflineResult(None)
            if "SELECT owner_user_id FROM polymer_md.jobs" in query:
                return _OfflineResult({"owner_user_id": owner})
            if "SELECT job_id,input_sha256" in query:
                return _OfflineResult({"job_id": self.job["job_id"], "input_sha256": request_sha256(payload)} if replay else None)
            if "SELECT count(*)" in query: return _OfflineResult({"n": 0})
            if "SELECT job_id FROM polymer_md.jobs" in query: return _OfflineResult(None)
            if query.lstrip().startswith("INSERT"):
                self.writes.append("insert"); return _OfflineResult(None)
            if "UPDATE polymer_md.jobs SET status='cancel_requested'" in query:
                self.job.update(status="cancel_requested", error_code=params[0]); self.writes.append("cancel")
                return _OfflineResult(None)
            if "UPDATE polymer_md.jobs SET worker_instance_id" in query:
                self.writes.append("grant")
                self.job["worker_instance_id"] = params[0]
                self.job["start_authorized_at"] = self.job["start_authorized_at"] or created + timedelta(seconds=2)
                return _OfflineResult({"start_authorized_at": self.job["start_authorized_at"]})
            if "UPDATE polymer_md.job_attempts" in query: return _OfflineResult(None)
            raise AssertionError("unexpected offline query: " + query)

    connection = Connection()
    def access(connection, *, owner_user_id):
        assert owner_user_id == owner and "account_lock" in connection.trace
        connection.trace.append("tier")
        return tier
    monkeypatch.setattr(entitlements, "resolve_access_tier", access)
    monkeypatch.setattr(entitlements, "enabled", lambda: quota)
    monkeypatch.setattr(module, "metering_enabled", lambda: False)
    monkeypatch.setattr(module, "validate_science", lambda request: request)
    repository = module.PolymerMdRepository("offline", connection_factory=lambda _dsn: connection)
    def get(_connection, _job, **kwargs):
        if kwargs.get("lock"): connection.trace.append("job_lock")
        return dict(connection.job)
    monkeypatch.setattr(repository, "_get", get)
    return repository, connection, payload, owner


@pytest.mark.parametrize("tier,quota,code", [("free", False, "membership_required"), ("member", True, "entitlements_unsupported")])
def test_offline_create_checks_membership_and_quota_under_account_lock(monkeypatch, tier, quota, code):
    repository, connection, payload, owner = _offline_repository(monkeypatch, tier=tier, quota=quota)
    with pytest.raises(PolymerMdError) as rejected:
        repository.create_job(payload, owner_user_id=owner, idempotency_key="offline-request")
    assert rejected.value.code == code
    assert connection.trace.index("account_lock") < connection.trace.index("tier")
    assert connection.writes == []


def test_offline_member_creation_and_original_idempotency_precede_new_quota(monkeypatch):
    repository, connection, payload, owner = _offline_repository(monkeypatch)
    _, created = repository.create_job(payload, owner_user_id=owner, idempotency_key="offline-request")
    assert created and connection.writes == ["insert", "insert"]
    repository, connection, payload, owner = _offline_repository(monkeypatch, tier="free", quota=True, replay=True)
    job, created = repository.create_job(payload, owner_user_id=owner, idempotency_key="offline-request")
    assert not created and job["job_id"] == connection.job["job_id"]
    assert "tier" not in connection.trace and connection.writes == []


@pytest.mark.parametrize("tier,quota,active,code", [
    ("free", False, True, "membership_required"), ("member", True, True, "entitlements_unsupported"),
    ("member", False, False, "account_disabled")])
def test_offline_first_start_rejection_retains_cleanup_and_execution_slot(monkeypatch, tier, quota, active, code):
    repository, connection, _, _ = _offline_repository(monkeypatch, tier=tier, quota=quota, active=active)
    request = StartAuthorizationRequest(**submit_request(connection.job).model_dump(), worker_instance_id="a"*32)
    assert repository.authorize_start(request) is None
    assert connection.job["error_code"] == code and connection.job["status"] == "cancel_requested"
    assert connection.job["start_authorized_at"] is None and not connection.job["cleanup_confirmed"]
    assert "grant" not in connection.writes
    if active:
        assert connection.trace.index("account_lock") < connection.trace.index("job_lock") < connection.trace.index("tier")


@pytest.mark.parametrize("rebind", [False, True])
def test_offline_durable_grant_replay_preserves_original_authorization(monkeypatch, rebind):
    from app.services.polymer_md_repository import submit_request
    repository, connection, _, _ = _offline_repository(monkeypatch, tier="free", quota=True, active=False, durable=True)
    original = connection.job["start_authorized_at"]
    request = StartAuthorizationRequest(**submit_request(connection.job).model_dump(),
        worker_instance_id=("b" if rebind else "a")*32,
        replaces_worker_instance_id="a"*32 if rebind else None, prior_execution_cleanup_confirmed=rebind)
    assert repository.authorize_start(request) == original
    assert "tier" not in connection.trace and connection.writes == ["grant"]


def test_offline_tier_unavailable_never_authorizes_or_releases(monkeypatch):
    from app.services.polymer_md_repository import submit_request
    from task_usage import entitlements
    repository, connection, payload, owner = _offline_repository(monkeypatch)
    def unavailable(*args, **kwargs): raise entitlements.EntitlementPolicyError("offline failure")
    monkeypatch.setattr(entitlements, "resolve_access_tier", unavailable)
    with pytest.raises(entitlements.EntitlementPolicyError):
        repository.create_job(payload, owner_user_id=owner, idempotency_key="offline-request")
    request = StartAuthorizationRequest(**submit_request(connection.job).model_dump(), worker_instance_id="a"*32)
    with pytest.raises(entitlements.EntitlementPolicyError): repository.authorize_start(request)
    assert connection.writes == [] and connection.job["start_authorized_at"] is None
    assert connection.job["status"] == "queued" and not connection.job["cleanup_confirmed"]


@pytest.mark.parametrize("tamper", [None, "missing", "checksum", "unknown", "duplicate", "legacy_md"])
def test_offline_worker_tier_accepts_only_exact22_or_approved23(monkeypatch, tamper):
    from task_usage.entitlement_access import accepted_release_ledgers, resolve_access_tier, OPTIONAL_MIGRATION
    from app.auth.isolation_ledger import POLYMER_CHECKSUM
    from task_usage.entitlements import EntitlementPolicyError
    import copy
    assert OPTIONAL_MIGRATION["checksum"] == POLYMER_CHECKSUM
    for ledger in accepted_release_ledgers():
        observed = copy.deepcopy(ledger)
        if tamper == "missing": observed.pop(19)
        elif tamper == "checksum": observed[-1]["checksum"] = "0"*64
        elif tamper == "unknown": observed.append({"version": "9999_unknown", "checksum": "0"*64})
        elif tamper == "duplicate": observed.append(dict(observed[-1]))
        elif tamper == "legacy_md": observed = observed[:19] + [{"version": "0020_polymer_md_jobs", "checksum": POLYMER_CHECKSUM}]
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def execute(self, query, params=()): self.query=query
            def __iter__(self): return iter(observed)
            def fetchone(self):
                if "to_regclass" in self.query: return {"tiers": "present", "ledger": "present"}
                if "relkind" in self.query: return {"relkind": "r", "relrowsecurity": True, "relforcerowsecurity": True}
                return {"tier": "free"}
        class Connection:
            def cursor(self, **kwargs): return Cursor()
        if tamper:
            with pytest.raises(EntitlementPolicyError): resolve_access_tier(Connection(), owner_user_id=str(uuid4()))
        else:
            assert resolve_access_tier(Connection(), owner_user_id=str(uuid4())) == "free"


def test_offline_usage_records_proven_lifecycle_without_inventing_execution(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.services import polymer_md_repository as module
    events=[]
    monkeypatch.setattr(module, "metering_enabled", lambda: True)
    monkeypatch.setattr(module, "record_event", lambda connection, **event: events.append(event))
    now=datetime(2026,10,1,tzinfo=timezone.utc)
    job={"job_id":str(uuid4()),"owner_user_id":str(uuid4()),"status":"cancel_requested",
        "created_at":now,"start_authorized_at":now+timedelta(seconds=2),
        "finished_at":now+timedelta(seconds=5),"cleanup_confirmed":False}
    module._record_usage(None,job)
    assert events==[]
    job["status"]="completed"
    module._record_usage(None,job)
    assert events==[]
    job["cleanup_confirmed"]=True
    module._record_usage(None,job)
    first=events[-1]
    module._record_usage(None,job)
    assert events[-1]==first and first["metrics"]=={"queue_ms":2000,"observed_wall_ms":5000}
    assert first["attempt_id"]=="1" and "execution_ms" not in first["metrics"]


def _offline_keys_and_checks():
    from app.services.polymer_md_schema import _CHECKS
    primary={"jobs":["job_id"],"job_attempts":["job_id","attempt"],"stage_runs":["job_id","stage_run_id"],"artifacts":["job_id","artifact_id"]}
    rows=[dict(relname=name,contype="p",columns=columns,target=None,target_columns=[],confdeltype=" ",convalidated=True,condeferrable=False,condeferred=False) for name,columns in primary.items()]
    foreign=[("jobs",["owner_user_id"],"auth.users",["user_id"],"a"),
        ("job_attempts",["job_id"],"polymer_md.jobs",["job_id"],"c"),
        ("stage_runs",["job_id"],"polymer_md.jobs",["job_id"],"c"),
        ("stage_runs",["job_id","attempt_token"],"polymer_md.job_attempts",["job_id","attempt_token"],"a"),
        ("artifacts",["job_id"],"polymer_md.jobs",["job_id"],"c"),
        ("artifacts",["job_id","stage_run_id"],"polymer_md.stage_runs",["job_id","stage_run_id"],"c"),
        ("artifacts",["job_id","attempt_token"],"polymer_md.job_attempts",["job_id","attempt_token"],"a")]
    rows.extend(dict(relname=name,contype="f",columns=columns,target=target,target_columns=target_columns,confdeltype=delete,convalidated=True,condeferrable=False,condeferred=False) for name,columns,target,target_columns,delete in foreign)
    checks=[dict(relname=name,convalidated=True,expression=expression) for name,expressions in _CHECKS.items() for expression in expressions]
    return rows,checks


@pytest.mark.parametrize("tamper", ["owner_target", "cascade", "deferred", "unvalidated", "cleanup", "grouping"])
def test_offline_schema_rejects_equal_count_key_and_check_tampering(tamper):
    from app.services.polymer_md_schema import _validate_keys_and_checks
    rows,checks=_offline_keys_and_checks()
    class Connection:
        def execute(self, query, params=()): return _OfflineResult(rows if "IN ('p','f')" in query else checks)
    _validate_keys_and_checks(Connection())
    if tamper=="owner_target": rows[4]["target"]="polymer_md.jobs"
    elif tamper=="cascade": rows[5]["confdeltype"]="a"
    elif tamper=="deferred": rows[4]["condeferrable"]=True
    elif tamper=="unvalidated": rows[4]["convalidated"]=False
    elif tamper=="cleanup": checks[9]["expression"]="status <> ALL(ARRAY['completed','failed','cancelled']) OR true"
    else:
        checks[-1]["expression"]="(storage_ref IS NULL OR length(storage_ref)>=1) AND length(storage_ref)<=1024"
    with pytest.raises(ValueError): _validate_keys_and_checks(Connection())


def test_offline_catalog_expression_normalization_preserves_boolean_security():
    from app.services.polymer_md_schema import _expression, _CHECKS
    from pathlib import Path
    assert _expression("((length(idempotency_key) >= 8) AND (length(idempotency_key) <= 128))") == _expression(_CHECKS["jobs"][0])
    assert _expression("status <> ALL (ARRAY['completed'::text,'failed'::text,'cancelled'::text]) OR cleanup_confirmed") == _expression(_CHECKS["jobs"][-1])
    assert _expression("a OR (b AND c)") != _expression("(a OR b) AND c")
    assert _expression("storage_ref ~~ (job_id::text || '/'::text || attempt_token || '/%'::text)") == _expression("storage_ref LIKE job_id::text || '/' || attempt_token || '/%'")
    source=(Path(__file__).parents[1]/"migrations/postgres/0023_polymer_md_jobs.sql").read_text()
    assert r"'(^|/)\.\.?(/|$)'" in source and r"'(^|/)\.\.?(/|$)'" in _CHECKS["artifacts"][-1]



def _offline_acl_catalog():
    from types import SimpleNamespace
    schema=[dict(role=role,privilege_type="USAGE",is_grantable=False) for role in ("nexpoly_api","nexpoly_service","nexpoly_mutable_audit")]
    objects=[dict(relname=table,relkind="r",role=role,privilege_type=privilege,is_grantable=False)
        for table in ("jobs","job_attempts","stage_runs","artifacts")
        for role,privileges in {"nexpoly_api":["SELECT"],"nexpoly_mutable_audit":["SELECT"],"nexpoly_service":["SELECT","INSERT","UPDATE","DELETE"]}.items() for privilege in privileges]
    objects.extend(dict(relname="jobs_enqueue_sequence_seq",relkind="S",role=role,privilege_type=privilege,is_grantable=False)
        for role,privileges in {"nexpoly_service":["USAGE","SELECT"],"nexpoly_mutable_audit":["SELECT"]}.items() for privilege in privileges)
    active="status = ANY (ARRAY['pending'::text,'queued'::text,'running'::text,'cancel_requested'::text])"
    specs=[("jobs",True,True,["job_id"],None),("jobs",True,False,["enqueue_sequence"],None),
        ("jobs",True,False,["attempt_token"],None),("jobs",True,False,["owner_user_id","idempotency_key"],None),
        ("job_attempts",True,True,["job_id","attempt"],None),("job_attempts",True,False,["attempt_token"],None),
        ("job_attempts",True,False,["job_id","attempt_token"],None),
        ("stage_runs",True,True,["job_id","stage_run_id"],None),("artifacts",True,True,["job_id","artifact_id"],None),
        ("jobs",True,False,["owner_user_id"],active),("jobs",False,False,["enqueue_sequence"],active)]
    indexes=[dict(relname=name,indisunique=unique,indisprimary=primary,columns=columns,predicate=predicate,
        indisvalid=True,indisready=True,indimmediate=True,no_includes=True,no_expressions=True,amname="btree") for name,unique,primary,columns,predicate in specs]
    data=SimpleNamespace(schema=schema,objects=objects,indexes=indexes,authority=False,column_grants=False,sequences=[{"relname":"jobs_enqueue_sequence_seq"}])
    class Connection:
        def execute(self, query, params=()):
            if "FROM pg_namespace n CROSS JOIN" in query: value=data.schema
            elif "SELECT EXISTS(SELECT FROM pg_roles" in query: value={"excess":data.authority}
            elif "CROSS JOIN LATERAL aclexplode" in query: value=data.objects
            elif "pg_attribute a JOIN pg_class" in query: value={"excess":data.column_grants}
            elif "FROM pg_index" in query: value=data.indexes
            elif "c.relkind='S'" in query: value=data.sequences
            else: raise AssertionError("unexpected metadata query: " + query)
            return _OfflineResult(value)
    return data,Connection()


@pytest.mark.parametrize("tamper", ["public", "grant_option", "missing_service", "owner", "column", "sequence", "active_predicate", "invalid_index"])
def test_offline_schema_rejects_acl_or_index_drift(tamper):
    from app.services.polymer_md_schema import _validate_acl_and_indexes
    data,connection=_offline_acl_catalog()
    _validate_acl_and_indexes(connection)
    if tamper=="public": data.objects.append(dict(relname="jobs",relkind="r",role="PUBLIC",privilege_type="SELECT",is_grantable=False))
    elif tamper=="grant_option": data.objects[0]["is_grantable"]=True
    elif tamper=="missing_service": data.objects.pop(2)
    elif tamper=="owner": data.authority=True
    elif tamper=="column": data.column_grants=True
    elif tamper=="sequence": data.sequences.append({"relname":"unapproved_sequence"})
    elif tamper=="active_predicate": data.indexes[-2]["predicate"]="status = ANY(ARRAY['pending','queued','running'])"
    else: data.indexes[-2]["indisvalid"]=False
    with pytest.raises(ValueError): _validate_acl_and_indexes(connection)


@pytest.mark.parametrize("installed", [False, True])
def test_offline_explicit23_migration_requires_full22_and_revalidates_retry(monkeypatch, installed):
    from app.services import polymer_md_schema as schema
    from app.auth import isolation_ledger, schema as isolation_schema
    from app import postgres_database
    from types import SimpleNamespace
    calls=[];statements=[]
    class Connection:
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def execute(self,query,params=()):
            statements.append(query)
            if query.startswith("SELECT version FROM governance.schema_migrations"):
                value={"version":schema.VERSION} if installed else None
            elif query.startswith("SELECT to_regnamespace"):
                value={"schema":None}
            else: value=None
            return SimpleNamespace(fetchone=lambda:value)
    monkeypatch.setattr(postgres_database,"postgres_connection",lambda *args,**kwargs:Connection())
    monkeypatch.setattr(isolation_ledger,"validate_service_migration_ledger",lambda connection,through:calls.append(("ledger",through)))
    monkeypatch.setattr(isolation_ledger,"validate_application_migration_ledger",lambda connection,**kwargs:calls.append(("private",kwargs)))
    monkeypatch.setattr(isolation_schema,"validate_isolation_schema",lambda connection:calls.append(("isolation",True)))
    monkeypatch.setattr(schema,"validate_polymer_md_schema",lambda connection:calls.append(("polymer",True)))
    result=schema.apply_polymer_md_dev_migration("offline",environment="dev")
    assert result.applied is (not installed) and result.checksum==schema.CHECKSUM
    assert ("private",{"require_usage":True,"require_entitlements":True}) in calls
    boundaries=[value for kind,value in calls if kind=="ledger"]
    assert boundaries==([schema.VERSION] if installed else [isolation_ledger.ENTITLEMENT_VERSION,schema.VERSION])
    assert any(query.startswith("CREATE") or "CREATE SCHEMA polymer_md" in query for query in statements) is (not installed)
    assert sum(query.startswith("INSERT INTO governance.schema_migrations") for query in statements)==int(not installed)


_OFFLINE_LEGACY_ASSET_TABLES = (
    "polymerization_batch.imports", "polymerization_batch.jobs",
    "md.monomer_md_jobs", "monomer_dft.jobs",
)


def _offline_asset_backup_check(tmp_path, monkeypatch, backup_state, consumer):
    from app.auth import assets, cutover
    if consumer == "archive":
        return assets.archive_assets({}, tmp_path, backup_state)
    backup = tmp_path / "synthetic-backup.dump"
    backup.write_bytes(b"Synthetic test fixture; not a database dump.\n")
    identity = {"name": "offline-synthetic", "system_id": "synthetic-system"}
    receipt = {"restore_verified": True, "backup_path": str(backup),
        "backup_sha256": assets.file_sha256(backup), "source": identity,
        "business_data": {}, "auth_metadata": {}, "backup_state": backup_state,
        "assets": {}}
    path = tmp_path / "synthetic-receipt.json"
    path.write_text(json.dumps(receipt))
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, query):
            assert query == "SELECT current_database() name,system_identifier::text system_id FROM pg_control_system()"
            return _OfflineResult(identity)
    monkeypatch.setattr(cutover.psycopg, "connect", lambda *args, **kwargs: Connection())
    return cutover.validate_backup_receipt(path, "offline-synthetic-no-database")


@pytest.mark.parametrize("consumer", ["archive", "receipt"])
@pytest.mark.parametrize("table", _OFFLINE_LEGACY_ASSET_TABLES)
@pytest.mark.parametrize("missing", ["table", "rows"])
def test_offline_asset_backup_requires_legacy_seals(tmp_path, monkeypatch, consumer, table, missing):
    state = {name: {"rows": 0} for name in _OFFLINE_LEGACY_ASSET_TABLES}
    if missing == "table": del state[table]
    else: del state[table]["rows"]
    with pytest.raises(KeyError):
        _offline_asset_backup_check(tmp_path, monkeypatch, state, consumer)


@pytest.mark.parametrize("consumer", ["archive", "receipt"])
def test_offline_asset_backup_requires_rows_for_present_polymer_seal(tmp_path, monkeypatch, consumer):
    state = {name: {"rows": 0} for name in _OFFLINE_LEGACY_ASSET_TABLES}
    state["polymer_md.jobs"] = {}
    with pytest.raises(KeyError):
        _offline_asset_backup_check(tmp_path, monkeypatch, state, consumer)


@pytest.mark.parametrize("consumer", ["archive", "receipt"])
@pytest.mark.parametrize("polymer_present", [False, True])
def test_offline_asset_backup_accepts_complete_legacy_and_empty_polymer_seals(tmp_path, monkeypatch, consumer, polymer_present):
    state = {name: {"rows": 0} for name in _OFFLINE_LEGACY_ASSET_TABLES}
    if polymer_present: state["polymer_md.jobs"] = {"rows": 0}
    result = _offline_asset_backup_check(tmp_path, monkeypatch, state, consumer)
    if consumer == "archive":
        assert result == {}
    else:
        assert result["backup_state"] == state and result["assets"] == {}
        assert result["restore_verified"] is True


@pytest.mark.parametrize("consumer", ["archive", "receipt"])
def test_offline_asset_backup_requires_files_for_present_polymer_rows(tmp_path, monkeypatch, consumer):
    state = {name: {"rows": 0} for name in _OFFLINE_LEGACY_ASSET_TABLES}
    state["polymer_md.jobs"] = {"rows": 1}
    with pytest.raises(ValueError):
        _offline_asset_backup_check(tmp_path, monkeypatch, state, consumer)
