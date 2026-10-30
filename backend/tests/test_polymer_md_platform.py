"""Full-application integration boundaries without starting DBs or engines."""
import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app import main
from app.routers import polymer_md
from app.services import deployment_control, polymer_md_schema
from app.services.polymer_md_repository import PolymerMdError


@pytest.mark.parametrize("enabled", [False, True])
def test_full_application_requires_the_enabled_schema_profile(monkeypatch, enabled):
    observed = []
    monkeypatch.setattr(main, "run_preflight", lambda _settings, **kw: observed.append(kw) or {"schema_target": main.SCHEMA_TARGET_POLYMER_MD if enabled else main.SCHEMA_TARGET_ISOLATION})
    monkeypatch.setattr(main, "preflight_blockers", lambda _report: [])
    app = SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(polymer_md_dev_enabled=enabled),
        auth=SimpleNamespace(assert_application_ready=lambda: None)))
    main._run_database_startup_preflight(app, required=False)
    assert observed == [{"mode": "runtime", "strict": True,
        "schema_target": main.SCHEMA_TARGET_CURRENT_ISOLATION}]
    assert not app.state.database_preflight_errors


def test_enabling_md_cannot_start_a_healthy_application_without_0023(monkeypatch):
    monkeypatch.setattr(main, "run_preflight", lambda *_a, **_kw: {})
    monkeypatch.setattr(main, "preflight_blockers", lambda _report: ["0023 missing"])
    app = SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(polymer_md_dev_enabled=True),
        auth=SimpleNamespace(assert_application_ready=lambda: None)))
    with pytest.raises(RuntimeError, match="0023 missing"):
        main._run_database_startup_preflight(app, required=False)


LIMITS = {"cpu_count": 16, "memory_bytes": 32 * 1024**3, "gpu_count": 0,
    "stage_timeout_seconds": 24 * 3600, "job_timeout_seconds": 96 * 3600,
    "max_job_bytes": 20 * 1024**3, "max_total_atoms": 20000, "max_replicated_atoms": 60000}

ANALYSIS_HEALTH = {"protocol_version": 1, "service": "polymer-md-analysis", "ready": True,
    "active": 0, "queued": 0, "request_timeout_seconds": 90}


def status_app(health, *, submit=False, schema=True, enabled=True, analysis_health=None):
    app = FastAPI()
    app.include_router(polymer_md.router)
    app.state.settings = SimpleNamespace(polymer_md_dev_enabled=enabled, polymer_md_submit_enabled=submit)
    app.state.polymer_md_repository = SimpleNamespace(schema_ready=lambda: schema)

    async def worker_health():
        return health

    async def reader_health():
        if isinstance(analysis_health, Exception):
            raise analysis_health
        return analysis_health

    app.state.polymer_md_worker_client = SimpleNamespace(health=worker_health) if health is not None else None
    app.state.polymer_md_analysis_client = SimpleNamespace(health=reader_health) if analysis_health is not None else None
    return app


def test_status_reports_worker_policy_and_pause_without_claiming_acceptance():
    health = {"ready": False, "analysis_ready": True, "admission_reason": "maintenance",
        "maintenance": {"enabled": True, "persisted": True, "updated_at": "2026-10-19T10:00:00+00:00",
                        "private_path": "/private/state.json"},
        "resource_limits": {**LIMITS, "private_path": "/private/policy.json"},
        "runtime": {"scientific_acceptance": "pending_full_PE_PEO_and_sensitivity_benchmarks",
                    "adapter_scientific_validation": "pending", "accepted_protocols": [],
                    "lammps": "/private/lammps"}}
    with TestClient(status_app(health, analysis_health=ANALYSIS_HEALTH)) as client:
        response = client.get("/api/v1/polymer-md/status")
    value = response.json()
    assert value["admission_reason"] == "maintenance" and not value["submit_enabled"]
    assert value["trajectory_available"] and value["distance_available"]
    assert value["resource_limits"] == LIMITS
    assert value["maintenance"] == {"enabled": True, "persisted": True, "updated_at": "2026-10-19T10:00:00+00:00"}
    assert value["acceptance_summary"]["adapter_scientific_validation"] == "pending"
    assert value["acceptance_summary"]["accepted_protocol_count"] == 0
    assert value["acceptance_summary"]["scientific_acceptance"].startswith("pending")
    assert "/private" not in response.text


@pytest.mark.parametrize("analysis_health", [
    None,
    {**ANALYSIS_HEALTH, "ready": False},
    PolymerMdError("analysis_service_unavailable", "/private/analysis.sock", 503),
    asyncio.TimeoutError(),
], ids=["unconfigured", "not_ready", "unavailable", "timeout"])
def test_unavailable_analysis_does_not_fall_back_to_worker_or_close_execution(analysis_health):
    health = {"ready": True, "analysis_ready": True}
    with TestClient(status_app(health, submit=True, analysis_health=analysis_health)) as client:
        response = client.get("/api/v1/polymer-md/capabilities")
    assert response.status_code == 200
    value = response.json()
    assert value["submit_enabled"] and value["admission_reason"] is None
    assert not value["trajectory_available"] and not value["distance_available"]
    assert value["analysis"] == {"status": "unavailable", "timeout_seconds": 90}
    assert "/private" not in response.text


@pytest.mark.parametrize("active,status", [(0, "ready"), (1, "busy")])
def test_independent_analysis_is_available_while_execution_is_paused(active, status):
    health = {"ready": False, "analysis_ready": False, "admission_reason": "maintenance"}
    analysis_health = {**ANALYSIS_HEALTH, "active": active}
    with TestClient(status_app(health, submit=True, analysis_health=analysis_health)) as client:
        response = client.get("/api/v1/polymer-md/capabilities")
    assert response.status_code == 200
    value = response.json()
    assert not value["submit_enabled"] and value["admission_reason"] == "maintenance"
    assert value["trajectory_available"] and value["distance_available"]
    assert value["analysis"] == {"status": status, "timeout_seconds": 90}


@pytest.mark.parametrize("health,options,reason", [
    ({"ready": True}, {}, "submission_disabled"),
    ({"ready": True}, {"enabled": False}, "feature_disabled"),
    ({"ready": True}, {"schema": False}, "schema_not_ready"),
    (None, {"submit": True}, "worker_unavailable"),
    ({"ready": False, "draining": True}, {"submit": True}, "maintenance"),
    ({"ready": False, "admission_reason": {"path": "/private"}}, {"submit": True}, "worker_not_ready"),
    ({"ready": True}, {"submit": True}, None),
])
def test_legacy_or_unavailable_worker_does_not_invent_limits(health, options, reason):
    with TestClient(status_app(health, **options)) as client:
        value = client.get("/api/v1/polymer-md/capabilities").json()
    assert value["admission_reason"] == reason
    assert value["resource_limits"] is None


@pytest.mark.parametrize("invalid", [float("nan"), True, -1, "86400"])
def test_invalid_worker_policy_is_not_published_as_effective(invalid):
    assert polymer_md._effective_resource_limits({"resource_limits": {**LIMITS, "stage_timeout_seconds": invalid}}) is None


def test_activity_inventory_preserves_legacy_and_fails_closed_on_partial_md(monkeypatch):
    monkeypatch.setattr(deployment_control, "probe_monomer_dft_schema", lambda _c: SimpleNamespace(
        state=deployment_control.MonomerDftSchemaState.READY))

    class Connection:
        present = False

        def execute(self, statement, params=None):
            if "information_schema.tables" in statement:
                self.row = {"present": True}
            elif "to_regnamespace('polymer_md')" in statement:
                self.row = {"present": self.present}
            else:
                self.row = {"count": 0}
            return self

        def fetchone(self):
            return self.row

    connection = Connection()
    monkeypatch.setattr(polymer_md_schema, "validate_polymer_md_schema", lambda _c: pytest.fail("legacy DB must not validate missing MD schema"))
    assert deployment_control.count_active_postgres_jobs(connection).active_jobs_schema_version == 3
    connection.present = True

    def invalid(_connection):
        raise ValueError("partial 0023")

    monkeypatch.setattr(polymer_md_schema, "validate_polymer_md_schema", invalid)
    with pytest.raises(ValueError, match="partial 0023"):
        deployment_control.count_active_postgres_jobs(connection)


def test_quota_policy_closes_new_compute_but_keeps_read_and_original_replay(monkeypatch):
    from task_usage import entitlements
    from uuid import uuid4
    monkeypatch.setattr(entitlements, "enabled", lambda: True)
    monkeypatch.setattr(polymer_md, "current_owner_id", lambda: "00000000-0000-0000-0000-000000000002")
    health = {"ready": True, "analysis_ready": True, "protocol_version": 1}
    app = status_app(health, submit=True, analysis_health=ANALYSIS_HEALTH)
    job = {"job_id": str(uuid4()), "attempt_token": "a"*64, "artifacts": []}
    app.state.polymer_md_repository.get_job = lambda *args, **kwargs: job
    app.state.polymer_md_repository.find_idempotent_job = lambda *args, **kwargs: None
    app.state.polymer_md_reconciler = None
    with TestClient(app) as client:
        status = client.get("/api/v1/polymer-md/capabilities").json()
        assert not status["submit_enabled"] and status["admission_reason"] == "entitlements_unsupported"
        assert status["trajectory_available"] and status["distance_available"]
        assert client.get("/api/v1/polymer-md/jobs/" + job["job_id"]).status_code == 200
        response = client.post("/api/v1/polymer-md/jobs", json={"smiles": "*CC*"}, headers={"Idempotency-Key": "offline-request"})
        assert response.status_code == 503 and response.json()["code"] == "entitlements_unsupported"
        app.state.polymer_md_repository.find_idempotent_job = lambda *args, **kwargs: job
        response = client.post("/api/v1/polymer-md/jobs", json={"smiles": "*CC*"}, headers={"Idempotency-Key": "offline-request"})
        assert response.status_code == 202 and response.json()["created"] is False
