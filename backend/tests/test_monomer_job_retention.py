from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.postgres_database import postgres_connection
from app.routers.monomer_dft import (
    MonomerDftPublicError,
    monomer_dft_public_error_handler,
    router as monomer_dft_router,
)
from app.routers.monomer_md import router as monomer_md_router
from app.services.monomer_job_retention import (
    MonomerDftJobDeletionService,
    MonomerJobDeletionConflict,
    MonomerJobRetentionReaper,
    MonomerJobStorageUnavailable,
    MonomerMdJobDeletionService,
)
from app.services.monomer_md_worker_client import MonomerMdWorkerError
from app.services.monomer_md_repository import (
    create_monomer_md_job_postgres,
    delete_monomer_md_job_cas_postgres,
    list_expired_monomer_md_jobs_postgres,
)


def _md_job(status: str = "completed") -> dict:
    now = datetime.now(timezone.utc)
    return {
        "job_id": "a" * 32,
        "status": status,
        "finished_at": now,
        "updated_at": now,
        "terminal_at": now,
    }


class _MdWorker:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.deleted: list[str] = []

    def delete_artifacts(self, job_id: str):
        if self.error is not None:
            raise self.error
        self.deleted.append(job_id)
        return {"job_id": job_id, "storage_state": "absent"}


def test_md_two_phase_delete_and_terminal_guard() -> None:
    worker = _MdWorker()
    service = MonomerMdJobDeletionService(dsn="unused", worker=worker)
    current = _md_job()
    service._get = lambda _job_id: current  # type: ignore[method-assign]
    service._delete_cas = lambda expected: expected is current  # type: ignore[method-assign]

    assert asyncio.run(service.delete(current["job_id"])) is True
    assert worker.deleted == [current["job_id"]]

    active = _md_job("cancel_requested")
    service._get = lambda _job_id: active  # type: ignore[method-assign]
    with pytest.raises(MonomerJobDeletionConflict):
        asyncio.run(service.delete(active["job_id"]))


def test_md_worker_failure_keeps_database_record() -> None:
    worker = _MdWorker(MonomerMdWorkerError("offline"))
    service = MonomerMdJobDeletionService(dsn="unused", worker=worker)
    current = _md_job()
    service._get = lambda _job_id: current  # type: ignore[method-assign]
    service._delete_cas = lambda _expected: pytest.fail("CAS must not run")  # type: ignore[method-assign]

    with pytest.raises(MonomerJobStorageUnavailable):
        asyncio.run(service.delete(current["job_id"]))


class _DftRepository:
    def __init__(self, job: dict | None) -> None:
        self.job = job
        self.cas_expected = None

    def get_job(self, _job_id: str):
        return self.job

    def delete_job_cas(self, expected: dict):
        self.cas_expected = expected
        return True


class _DftWorker:
    def __init__(self) -> None:
        self.expected = None

    async def purge_job(self, expected: dict):
        self.expected = expected
        return {"job_id": expected["job_id"], "storage_state": "absent"}


def test_dft_delete_passes_immutable_identity_to_worker_and_cas() -> None:
    now = datetime.now(timezone.utc)
    job = {
        "job_id": "00000000-0000-4000-8000-000000000001",
        "status": "failed",
        "request_sha256": "1" * 64,
        "_attempt_token": "2" * 64,
        "_enqueue_sequence": 7,
        "finished_at": now,
        "updated_at": now,
    }
    repository = _DftRepository(job)
    worker = _DftWorker()
    service = MonomerDftJobDeletionService(
        repository=repository,  # type: ignore[arg-type]
        worker=worker,  # type: ignore[arg-type]
    )

    assert asyncio.run(service.delete(job["job_id"])) is True
    assert worker.expected is job
    assert repository.cas_expected is job


def test_reaper_rotates_past_per_job_failure() -> None:
    now = datetime.now(timezone.utc) - timedelta(days=31)
    candidates = [
        {
            "job_id": f"{index:032x}",
            "status": "completed",
            "finished_at": now + timedelta(seconds=index),
            "updated_at": now + timedelta(seconds=index),
            "terminal_at": now + timedelta(seconds=index),
        }
        for index in range(3)
    ]
    deleted: list[str] = []

    @contextmanager
    def leader():
        yield True

    def list_candidates(cursor_at, cursor_id, limit):
        rows = candidates
        if cursor_at is not None:
            rows = [
                item
                for item in rows
                if (item["terminal_at"], item["job_id"]) > (cursor_at, cursor_id)
            ]
        return rows[:limit]

    async def delete(candidate):
        if candidate["job_id"] == candidates[0]["job_id"]:
            raise MonomerJobDeletionConflict("bad identity")
        deleted.append(candidate["job_id"])

    reaper = MonomerJobRetentionReaper(
        name="test",
        enabled=True,
        retention_days=30,
        leader_guard=leader,
        list_candidates=list_candidates,
        delete_candidate=delete,
    )
    result = asyncio.run(reaper.sweep())

    assert (result.scanned, result.deleted, result.failed) == (3, 2, 1)
    assert deleted == [candidates[1]["job_id"], candidates[2]["job_id"]]
    assert reaper.status == "degraded"


def test_retention_days_are_strict() -> None:
    with pytest.raises(ValueError, match="MONOMER_MD_JOB_RETENTION_DAYS"):
        Settings(monomer_md_job_retention_days=0)
    with pytest.raises(ValueError, match="MONOMER_DFT_JOB_RETENTION_DAYS"):
        Settings(monomer_dft_job_retention_days=3651)


def test_md_retention_uses_terminal_time_fallback_and_cas(
    postgres_dsn: str,
) -> None:
    old_finished_id = "1" * 32
    old_fallback_id = "2" * 32
    recent_id = "3" * 32
    with postgres_connection(postgres_dsn) as connection:
        for job_id in (old_finished_id, old_fallback_id, recent_id):
            create_monomer_md_job_postgres(
                connection,
                job_id=job_id,
                input_smiles="CCO",
                canonical_smiles="CCO",
                requested_steps=300,
            )
        connection.execute(
            """
            UPDATE md.monomer_md_jobs
            SET status = 'completed',
                created_at = now() - interval '31 days',
                finished_at = now() - interval '30 days',
                updated_at = now() - interval '30 days'
            WHERE job_id = %s
            """,
            (old_finished_id,),
        )
        connection.execute(
            """
            UPDATE md.monomer_md_jobs
            SET status = 'failed',
                created_at = now() - interval '32 days',
                finished_at = NULL,
                updated_at = now() - interval '31 days'
            WHERE job_id = %s
            """,
            (old_fallback_id,),
        )
        connection.execute(
            """
            UPDATE md.monomer_md_jobs
            SET status = 'cancelled',
                created_at = now() - interval '31 days',
                finished_at = now() - interval '29 days',
                updated_at = now() - interval '29 days'
            WHERE job_id = %s
            """,
            (recent_id,),
        )
        candidates = list_expired_monomer_md_jobs_postgres(
            connection,
            retention_days=30,
            limit=100,
        )
        assert [item["job_id"] for item in candidates] == [
            old_fallback_id,
            old_finished_id,
        ]
        expected = candidates[0]
        assert delete_monomer_md_job_cas_postgres(
            connection,
            job_id=expected["job_id"],
            expected_status=expected["status"],
            expected_finished_at=expected["finished_at"],
            expected_updated_at=expected["updated_at"],
        )
        assert not delete_monomer_md_job_cas_postgres(
            connection,
            job_id=expected["job_id"],
            expected_status=expected["status"],
            expected_finished_at=expected["finished_at"],
            expected_updated_at=expected["updated_at"],
        )
    worker = _MdWorker()
    service = MonomerMdJobDeletionService(dsn=postgres_dsn, worker=worker)
    assert asyncio.run(service.delete(old_finished_id)) is True
    assert worker.deleted == [old_finished_id]


class _ApiDeletion:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.job_ids: list[str] = []

    async def delete(self, job_id: str):
        self.job_ids.append(job_id)
        if self.error is not None:
            raise self.error
        return True


def test_public_md_delete_is_idempotent_and_maps_conflict() -> None:
    service = _ApiDeletion()
    app = FastAPI()
    app.state.settings = Settings()
    app.state.monomer_md_job_deletion_service = service
    app.include_router(monomer_md_router)
    client = TestClient(app)
    job_id = "a" * 32

    assert client.delete(f"/api/v1/monomer-md/jobs/{job_id}").status_code == 204
    assert client.delete("/api/v1/monomer-md/jobs/not-a-job").status_code == 204
    assert service.job_ids == [job_id]

    service.error = MonomerJobDeletionConflict("active")
    response = client.delete(f"/api/v1/monomer-md/jobs/{'b' * 32}")
    assert response.status_code == 409


def test_public_dft_delete_returns_204_and_structured_503() -> None:
    service = _ApiDeletion()
    app = FastAPI()
    app.state.settings = Settings()
    app.state.monomer_dft_repository = object()
    app.state.monomer_dft_job_deletion_service = service
    app.add_exception_handler(
        MonomerDftPublicError,
        monomer_dft_public_error_handler,
    )
    app.include_router(monomer_dft_router)
    client = TestClient(app)
    job_id = "00000000-0000-4000-8000-000000000001"

    assert client.delete(f"/api/v1/monomer-dft/jobs/{job_id}").status_code == 204
    service.error = MonomerJobStorageUnavailable("offline")
    response = client.delete(f"/api/v1/monomer-dft/jobs/{job_id}")
    assert response.status_code == 503
    assert response.json()["code"] == "storage_cleanup_unavailable"
