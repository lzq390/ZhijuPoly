"""Production opt-in and history preservation without a database or engines."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services.monomer_dft_reconciler import MonomerDftReconciler
from app.services.polymer_md_reconciler import PolymerMdReconciler
from app.services.polymerization_batch.models import BatchError, BatchSettings
from app.services.polymerization_batch.service import BatchService
from app.services.polymerization_batch.worker import BatchWorker


@pytest.fixture(autouse=True)
def isolated_flags(monkeypatch):
    for key in (
        "AUTH_PROD_HTTP_ENABLED", "POLYMER_MD_DEV_ENABLED", "POLYMER_MD_PROD_ENABLED",
        "POLYMER_MD_SUBMIT_ENABLED", "POLYMER_MD_ENVIRONMENT", "USAGE_ENTITLEMENTS_ENABLED",
        "POLYMER_MD_RETENTION_ENABLED", "MONOMER_DFT_ARTIFACT_RETENTION_ENABLED",
        "MONOMER_POLYMERIZATION_BATCH_RETENTION_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)


def test_new_switches_preserve_existing_defaults():
    settings = Settings()
    assert not settings.auth_prod_http_enabled
    assert not settings.polymer_md_prod_enabled and not settings.polymer_md_enabled
    assert settings.monomer_dft_artifact_retention_enabled
    assert settings.polymer_md_retention_enabled
    assert BatchSettings.from_env().retention_enabled


def test_production_polymer_md_requires_explicit_matching_environment(monkeypatch):
    with pytest.raises(ValueError, match="POLYMER_MD_ENVIRONMENT=dev"):
        Settings(polymer_md_dev_enabled=True, polymer_md_environment="prod")
    with pytest.raises(ValueError, match="POLYMER_MD_ENVIRONMENT=prod"):
        Settings(polymer_md_prod_enabled=True, polymer_md_environment="dev")
    with pytest.raises(ValueError):
        Settings(polymer_md_dev_enabled=True, polymer_md_prod_enabled=True, polymer_md_environment="prod")
    with pytest.raises(ValueError, match="enablement"):
        Settings(polymer_md_environment="prod", polymer_md_submit_enabled=True,
                 polymer_md_worker_uds="/run/polymer-md/worker.sock", polymer_md_start_authorization_token="test-token")
    settings = Settings(polymer_md_prod_enabled=True, polymer_md_environment="prod",
                        polymer_md_submit_enabled=True, polymer_md_worker_uds="/run/polymer-md/worker.sock",
                        polymer_md_start_authorization_token="test-token")
    assert settings.polymer_md_enabled and not settings.polymer_md_dev_enabled
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "true")
    with pytest.raises(ValueError, match="USAGE_ENTITLEMENTS_ENABLED=false"):
        Settings(polymer_md_prod_enabled=True, polymer_md_environment="prod")


def test_preservation_flags_are_strict_and_independent(monkeypatch):
    for key in ("MONOMER_DFT_ARTIFACT_RETENTION_ENABLED", "POLYMER_MD_RETENTION_ENABLED",
                "MONOMER_POLYMERIZATION_BATCH_RETENTION_ENABLED"):
        monkeypatch.setenv(key, "false")
    settings = Settings(auth_prod_http_enabled=True)
    assert settings.auth_prod_http_enabled
    assert not settings.monomer_dft_artifact_retention_enabled
    assert not settings.polymer_md_retention_enabled
    assert not BatchSettings.from_env().retention_enabled
    monkeypatch.setenv("MONOMER_POLYMERIZATION_BATCH_RETENTION_ENABLED", "invalid")
    with pytest.raises(ValueError, match="must be a boolean"):
        BatchSettings.from_env()


def test_disabled_polymer_retention_presents_no_expiry_for_old_and_new_results():
    from app.routers.polymer_md import _scientific_protocol
    from app.services.polymer_md_repository import public_job, public_results

    historical = {"retention_policy": {"artifact_retention_days": 7, "metadata_retention_days": 30}, "properties": []}
    assert public_results(historical) == historical
    assert _scientific_protocol()["retention"] == {"artifact_days": 7, "metadata_days": 30,
        "clock": "server_terminal_finished_at", "requires_confirmed_cleanup": True}
    assert _scientific_protocol(retention_enabled=False)["retention"] == {"enabled": False,
        "artifact_days": None, "metadata_days": None, "clock": "server_terminal_finished_at", "requires_confirmed_cleanup": True}
    expected = {"enabled": False, "artifact_retention_days": None, "metadata_retention_days": None}
    assert public_results(historical, retention_enabled=False)["retention_policy"] == expected
    assert public_job({"results": historical}, retention_enabled=False)["results"]["retention_policy"] == expected
    assert historical["retention_policy"]["artifact_retention_days"] == 7


def test_preserved_polymer_policy_is_consistent_across_status_list_detail_and_results(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import polymer_md
    from app.services.polymer_md_repository import public_job

    historical = {"job_id": "12345678-1234-1234-1234-123456789012", "snapshot_revision": 1,
        "status": "completed", "quality_status": "not_evaluated", "results": {
            "retention_policy": {"artifact_retention_days": 7, "metadata_retention_days": 30}}}
    api = FastAPI()
    api.include_router(polymer_md.router)
    api.state.settings = SimpleNamespace(polymer_md_enabled=True, polymer_md_dev_enabled=False,
        polymer_md_retention_enabled=False, polymer_md_submit_enabled=False)
    api.state.polymer_md_repository = SimpleNamespace(schema_ready=lambda: True,
        get_job=lambda *_args, **_kwargs: historical,
        list_jobs=lambda **_kwargs: {"items": [public_job(historical)], "total": 1, "page": 1, "page_size": 20})
    api.state.polymer_md_worker_client = None
    api.state.polymer_md_analysis_client = None
    monkeypatch.setattr(polymer_md, "current_owner_id", lambda: "owner")
    with TestClient(api) as client:
        status = client.get("/api/v1/polymer-md/status").json()
        assert status["scientific_protocol"]["retention"]["enabled"] is False
        documents = [client.get("/api/v1/polymer-md/jobs").json()["items"][0],
            client.get("/api/v1/polymer-md/jobs/" + historical["job_id"]).json(),
            client.get("/api/v1/polymer-md/jobs/" + historical["job_id"] + "/results").json()]
    for value in documents:
        assert value["results"]["retention_policy"] == {"enabled": False,
            "artifact_retention_days": None, "metadata_retention_days": None}


@pytest.mark.parametrize("retention_enabled", [False, True])
def test_dft_preservation_still_reconciles_explicit_deletions(retention_enabled):
    events = []

    class Repository:
        @contextmanager
        def reconciliation_leader(self):
            yield True

        def list_reconcilable_jobs(self, **_):
            return []

        def list_expired_artifact_jobs(self, **_):
            events.append("scan-expired")
            return [{"job_id": "old-history"}]

        def request_artifact_deletion_for_service(self, job_id):
            events.append(("expire", job_id))

        def list_pending_artifact_deletions(self, **_):
            return [{"job_id": "explicit-delete"}]

        def mark_artifacts_deleted(self, job_id):
            events.append(("deleted", job_id))

    async def delete(job_id):
        events.append(("worker-delete", job_id))

    async def run():
        reconciler = MonomerDftReconciler(repository=Repository(), worker=SimpleNamespace(delete_artifacts=delete),
            interval_seconds=1, artifact_retention_enabled=retention_enabled)
        reconciler._last_sweep = -60
        await reconciler.run_once()

    asyncio.run(run())
    assert ("scan-expired" in events) is retention_enabled
    assert (("expire", "old-history") in events) is retention_enabled
    assert ("worker-delete", "explicit-delete") in events
    assert ("deleted", "explicit-delete") in events


@pytest.mark.parametrize("retention_enabled", [False, True])
def test_polymer_preservation_keeps_explicit_deletion_queue(monkeypatch, retention_enabled):
    events = []
    job = {"job_id": "explicit-delete", "attempt_token": "attempt", "delete_job_requested": True}

    class Repository:
        def schema_ready(self):
            return True

        @contextmanager
        def reconciliation_leader(self):
            yield True

        def list_active(self):
            return []

        def mark_expired(self):
            events.append("mark-expired")

        def list_deletions(self):
            return [job]

        def complete_deletion(self, *args, **kwargs):
            events.append(("deleted", args, kwargs))

    async def delete(request, **kwargs):
        events.append(("worker-delete", request, kwargs))

    monkeypatch.setattr("app.services.polymer_md_reconciler.submit_request", lambda value: value)
    asyncio.run(PolymerMdReconciler(Repository(), SimpleNamespace(delete=delete), retention_enabled=retention_enabled).run_once())
    assert ("mark-expired" in events) is retention_enabled
    assert ("worker-delete", job, {"entire_job": True}) in events
    assert ("deleted", ("explicit-delete", "attempt"), {"delete_job": True}) in events


@pytest.mark.parametrize("retention_enabled", [False, True])
def test_retained_batch_results_remain_readable_past_old_expiry(tmp_path, monkeypatch, retention_enabled):
    root = tmp_path
    job_id = "a" * 32
    parent = root / "jobs" / job_id
    parent.mkdir(parents=True)
    (parent / "results.zip").write_bytes(b"PK-retained")
    (parent / "preview.jsonl").write_text('{"smiles":"CC"}\n')
    (parent / "preview-index.json").write_text(json.dumps({"count": 1, "offsets": [0]}))
    old = datetime.now(timezone.utc) - timedelta(days=90)
    job = {"id": job_id, "status": "completed", "expires_at": old,
           "artifacts": {name: {"path": f"jobs/{job_id}/{name}"}
                         for name in ("results.zip", "preview.jsonl", "preview-index.json")}}
    service = BatchService("unused", BatchSettings(storage_root=root, retention_enabled=retention_enabled))
    service.repository = SimpleNamespace(get_job=lambda *args, **kwargs: job)
    monkeypatch.setattr("app.services.polymerization_batch.service.current_owner_id", lambda: "owner")
    if retention_enabled:
        with pytest.raises(BatchError) as failure:
            service.artifact(job_id, "results.zip")
        assert failure.value.status == 410
        with pytest.raises(BatchError):
            service.results(job_id, 0, 10)
    else:
        handle, _ = service.artifact(job_id, "results.zip")
        with handle:
            assert handle.read() == b"PK-retained"
        assert service.results(job_id, 0, 10) == {"items": [{"smiles": "CC"}], "total": 1, "next_offset": None}
        job["status"] = "expired"
        with pytest.raises(BatchError):
            service.artifact(job_id, "results.zip")


@pytest.mark.parametrize("retention_enabled", [False, True])
def test_batch_preservation_retains_history_but_cleans_temporary_inputs_and_orphans(tmp_path, monkeypatch, retention_enabled):
    job_id, import_id = "a" * 32, "b" * 32
    history = tmp_path / "jobs" / job_id
    expired_import = tmp_path / "imports" / import_id
    for parent in (history, expired_import):
        parent.mkdir(parents=True)
        (parent / "data").write_text("keep history")
    orphan = history / "attempts" / "orphan"
    referenced = history / "attempts" / "referenced"
    for parent in (orphan, referenced):
        parent.mkdir(parents=True)
        os.utime(parent, (1, 1))
    queries = []

    class Connection:
        def execute(self, query, args=()):
            queries.append(query)
            if "SELECT id FROM polymerization_batch.imports" in query:
                rows = [{"id": import_id}]
            elif "SELECT id FROM polymerization_batch.jobs" in query:
                rows = [{"id": job_id}]
            else:
                rows = []
            return SimpleNamespace(fetchall=lambda: rows, fetchone=lambda: {"exists": 1})

    @contextmanager
    def connection():
        yield Connection()

    worker = BatchWorker("unused", BatchSettings(storage_root=tmp_path, retention_enabled=retention_enabled))
    worker.repository = SimpleNamespace(connection=connection,
        chunks_for_service=lambda *_args, **_kwargs: [{"artifact": {"path": f"jobs/{job_id}/attempts/referenced/result.json"}}])
    monkeypatch.setattr("app.services.deployment_control.get_drain_state", lambda *_args, **_kwargs: SimpleNamespace(enabled=False))
    worker.cleanup()
    assert history.exists() is (not retention_enabled)
    assert not expired_import.exists()
    assert not orphan.exists()
    assert referenced.exists() is (not retention_enabled)
    assert any("SET status='expired'" in query for query in queries) is retention_enabled
