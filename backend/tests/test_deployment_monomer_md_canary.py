from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.postgres_database import postgres_connection
from app.services import deployment_monomer_md_canary as canary
from app.services.monomer_md_repository import (
    create_monomer_md_job_postgres,
    mark_monomer_md_job_completed_postgres,
    mark_monomer_md_job_failed_postgres,
)
from app.services.monomer_md_worker_client import (
    MonomerMdWorkerError,
    MonomerMdWorkerSubmission,
)


OPERATION_ID = "pull-deploy-canary-0001"
SOURCE_SHA = "a" * 40
BYTEFF2_COMMIT = "b" * 40


class FakeCanaryWorker:
    def __init__(self) -> None:
        self.job_root = "/private/monomer-md-runs"
        self.submissions: list[str] = []
        self.active_jobs: set[str] = set()
        self.artifact_jobs: set[str] = set()
        self.delete_calls: list[str] = []
        self.health_error = False

    def get_health(self) -> dict[str, Any]:
        if self.health_error:
            raise MonomerMdWorkerError("simulated Worker outage")
        return {
            "status": "ok",
            "mode": "real",
            "source_sha": SOURCE_SHA,
            "accepting_jobs": True,
            "draining": False,
            "db_configured": True,
            "runtime_ready": True,
            "active_jobs": len(self.active_jobs),
            "max_active_jobs": 1,
            "default_steps": 300,
            "max_steps": 300,
            "job_root": self.job_root,
        }

    def submit_job(self, payload) -> MonomerMdWorkerSubmission:  # type: ignore[no-untyped-def]
        self.submissions.append(payload.job_id)
        self.active_jobs.add(payload.job_id)
        self.artifact_jobs.add(payload.job_id)
        return MonomerMdWorkerSubmission(
            worker_id="worker-1",
            worker_job_id=payload.job_id,
            worker_version="test-worker",
        )

    def delete_artifacts(self, job_id: str) -> dict[str, Any]:
        self.delete_calls.append(job_id)
        if job_id in self.active_jobs:
            raise MonomerMdWorkerError(
                "cannot delete artifacts for an active monomer MD job"
            )
        deleted = job_id in self.artifact_jobs
        self.artifact_jobs.discard(job_id)
        return {
            "job_id": job_id,
            "deleted": deleted,
            "artifact_root": f"{self.job_root}/{job_id}",
            "message": (
                "artifacts deleted"
                if deleted
                else "artifacts were already absent"
            ),
        }

    def finish(self, job_id: str) -> None:
        self.active_jobs.discard(job_id)


@pytest.fixture
def canary_state_directory(tmp_path: Path) -> Path:
    state_directory = tmp_path / "monomer-md-canaries"
    state_directory.mkdir(mode=0o700)
    return state_directory


def _submit(
    postgres_dsn: str,
    state_directory: Path,
    worker: FakeCanaryWorker,
    *,
    capability: str | None = None,
) -> dict[str, Any]:
    return canary.submit_canary(
        dsn=postgres_dsn,
        state_directory=state_directory,
        operation_id=OPERATION_ID,
        source_sha=SOURCE_SHA,
        expected_byteff2_commit=BYTEFF2_COMMIT,
        max_active_jobs=1,
        worker_client=worker,
        capability=capability,
    )


def _marker(state_directory: Path) -> dict[str, Any]:
    return canary.read_canary_marker(
        state_directory,
        OPERATION_ID,
        SOURCE_SHA,
        BYTEFF2_COMMIT,
    )


def _complete(
    postgres_dsn: str,
    worker: FakeCanaryWorker,
    job_id: str,
) -> None:
    worker.finish(job_id)
    with postgres_connection(postgres_dsn) as connection:
        mark_monomer_md_job_completed_postgres(
            connection,
            job_id=job_id,
            result_data={
                "summary": {"n_steps": 300},
                "not_equilibrated": True,
                "physical_density_estimate": False,
                "warnings": ["300-step deployment canary; not physical"],
            },
            artifacts={
                "state": {"path": "npt_state.csv"},
                "trajectory": {"path": "npt.dcd"},
            },
            artifact_root=f"{worker.job_root}/{job_id}",
            completed_steps=300,
            byteff2_git_sha=BYTEFF2_COMMIT,
        )


def _fail(
    postgres_dsn: str,
    worker: FakeCanaryWorker,
    job_id: str,
) -> None:
    worker.finish(job_id)
    with postgres_connection(postgres_dsn) as connection:
        mark_monomer_md_job_failed_postgres(
            connection,
            job_id,
            "simulated terminal failure",
            "test_failure",
        )


def _validate(
    postgres_dsn: str,
    state_directory: Path,
    capability: str,
) -> dict[str, Any]:
    return canary.validate_completed_canary(
        dsn=postgres_dsn,
        state_directory=state_directory,
        operation_id=OPERATION_ID,
        source_sha=SOURCE_SHA,
        expected_byteff2_commit=BYTEFF2_COMMIT,
        capability=capability,
    )


def _cleanup(
    postgres_dsn: str,
    state_directory: Path,
    worker: FakeCanaryWorker,
    capability: str,
    *,
    operation_id: str = OPERATION_ID,
    source_sha: str = SOURCE_SHA,
    expected_byteff2_commit: str = BYTEFF2_COMMIT,
) -> dict[str, Any]:
    return canary.cleanup_canary(
        dsn=postgres_dsn,
        state_directory=state_directory,
        operation_id=operation_id,
        source_sha=source_sha,
        expected_byteff2_commit=expected_byteff2_commit,
        capability=capability,
        worker_client=worker,
    )


def _job_row(postgres_dsn: str, job_id: str) -> dict[str, Any] | None:
    with postgres_connection(postgres_dsn) as connection:
        row = connection.execute(
            """
            SELECT to_jsonb(job_row) AS document
            FROM md.monomer_md_jobs AS job_row
            WHERE job_id = %s
            """,
            (job_id,),
        ).fetchone()
        return row["document"] if row is not None else None


def _logical_table_snapshot(postgres_dsn: str) -> str:
    with postgres_connection(postgres_dsn) as connection:
        row = connection.execute(
            """
            SELECT COALESCE(
              jsonb_agg(to_jsonb(job_row) ORDER BY job_id),
              '[]'::jsonb
            )::text AS snapshot
            FROM md.monomer_md_jobs AS job_row
            """
        ).fetchone()
        assert row is not None
        return str(row["snapshot"])


def _sequence_snapshot(postgres_dsn: str) -> list[tuple[object, ...]]:
    with postgres_connection(postgres_dsn) as connection:
        rows = connection.execute(
            """
            SELECT schemaname, sequencename, start_value, min_value, max_value,
                   increment_by, cycle, cache_size, last_value
            FROM pg_sequences
            WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY schemaname, sequencename
            """
        ).fetchall()
        return [
            (
                row["schemaname"],
                row["sequencename"],
                row["start_value"],
                row["min_value"],
                row["max_value"],
                row["increment_by"],
                row["cycle"],
                row["cache_size"],
                row["last_value"],
            )
            for row in rows
        ]


def test_canary_success_restores_exact_table_sequences_and_worker_artifacts(
    postgres_dsn: str,
    canary_state_directory: Path,
) -> None:
    worker = FakeCanaryWorker()
    before_table = _logical_table_snapshot(postgres_dsn)
    before_sequences = _sequence_snapshot(postgres_dsn)

    submitted = _submit(postgres_dsn, canary_state_directory, worker)
    job_id = submitted["job_id"]
    capability = submitted["capability"]
    _complete(postgres_dsn, worker, job_id)

    validated = _validate(
        postgres_dsn,
        canary_state_directory,
        capability,
    )
    cleaned = _cleanup(
        postgres_dsn,
        canary_state_directory,
        worker,
        capability,
    )

    assert validated["status"] == "validated"
    assert cleaned["status"] == "cleaned"
    assert cleaned["validated"] is True
    assert _marker(canary_state_directory)["phase"] == "cleaned"
    assert _job_row(postgres_dsn, job_id) is None
    assert worker.artifact_jobs == set()
    assert _logical_table_snapshot(postgres_dsn) == before_table
    assert _sequence_snapshot(postgres_dsn) == before_sequences

    repeated_cleanup = _cleanup(
        postgres_dsn,
        canary_state_directory,
        worker,
        capability,
    )
    repeated_submit = _submit(
        postgres_dsn,
        canary_state_directory,
        worker,
    )
    assert repeated_cleanup["status"] == "cleaned"
    assert repeated_submit["status"] == "cleaned"
    assert repeated_submit["capability"] == capability
    assert worker.submissions == [job_id]


def test_submit_response_loss_recovers_same_owned_job_without_resubmission(
    postgres_dsn: str,
    canary_state_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = FakeCanaryWorker()
    original_response = canary._canary_response
    response_lost = False

    def lose_first_submitted_response(
        marker: dict[str, Any],
        status_value: str,
        *,
        include_capability: bool = False,
    ) -> dict[str, Any]:
        nonlocal response_lost
        if status_value == "submitted" and not response_lost:
            response_lost = True
            raise ConnectionResetError("simulated committed response loss")
        return original_response(
            marker,
            status_value,
            include_capability=include_capability,
        )

    monkeypatch.setattr(
        canary,
        "_canary_response",
        lose_first_submitted_response,
    )
    with pytest.raises(ConnectionResetError, match="committed response loss"):
        _submit(postgres_dsn, canary_state_directory, worker)

    retried = _submit(postgres_dsn, canary_state_directory, worker)
    assert retried["status"] == "submitted"
    assert worker.submissions == [retried["job_id"]]

    _fail(postgres_dsn, worker, retried["job_id"])
    _cleanup(
        postgres_dsn,
        canary_state_directory,
        worker,
        retried["capability"],
    )


def test_validation_commit_response_loss_is_retry_idempotent(
    postgres_dsn: str,
    canary_state_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = FakeCanaryWorker()
    submitted = _submit(postgres_dsn, canary_state_directory, worker)
    _complete(postgres_dsn, worker, submitted["job_id"])
    original_write = canary._atomic_write_marker
    response_lost = False

    def lose_validated_response(
        path: Path,
        marker: dict[str, Any],
    ) -> None:
        nonlocal response_lost
        original_write(path, marker)
        if marker["phase"] == "validated" and not response_lost:
            response_lost = True
            raise ConnectionResetError("simulated validation response loss")

    monkeypatch.setattr(canary, "_atomic_write_marker", lose_validated_response)
    with pytest.raises(ConnectionResetError, match="validation response loss"):
        _validate(
            postgres_dsn,
            canary_state_directory,
            submitted["capability"],
        )
    assert _marker(canary_state_directory)["phase"] == "validated"

    retried = _validate(
        postgres_dsn,
        canary_state_directory,
        submitted["capability"],
    )
    assert retried["status"] == "validated"
    _cleanup(
        postgres_dsn,
        canary_state_directory,
        worker,
        submitted["capability"],
    )


def test_cleanup_intent_crash_before_delete_recovers_exact_row(
    postgres_dsn: str,
    canary_state_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = FakeCanaryWorker()
    submitted = _submit(postgres_dsn, canary_state_directory, worker)
    _complete(postgres_dsn, worker, submitted["job_id"])
    _validate(
        postgres_dsn,
        canary_state_directory,
        submitted["capability"],
    )
    original_write = canary._atomic_write_marker
    crashed = False

    def crash_after_cleanup_intent(
        path: Path,
        marker: dict[str, Any],
    ) -> None:
        nonlocal crashed
        original_write(path, marker)
        if marker["phase"] == "cleanup-intent" and not crashed:
            crashed = True
            raise RuntimeError("simulated crash before row delete")

    monkeypatch.setattr(
        canary,
        "_atomic_write_marker",
        crash_after_cleanup_intent,
    )
    with pytest.raises(RuntimeError, match="before row delete"):
        _cleanup(
            postgres_dsn,
            canary_state_directory,
            worker,
            submitted["capability"],
        )

    marker = _marker(canary_state_directory)
    assert marker["phase"] == "cleanup-intent"
    assert marker["row_sha256"] is not None
    assert _job_row(postgres_dsn, submitted["job_id"]) is not None
    assert worker.artifact_jobs == set()
    delete_call_count = len(worker.delete_calls)
    recovered_submit = _submit(
        postgres_dsn,
        canary_state_directory,
        worker,
    )
    assert recovered_submit["status"] == "cleanup-intent"
    assert recovered_submit["capability"] == submitted["capability"]
    assert len(worker.delete_calls) == delete_call_count
    assert _job_row(postgres_dsn, submitted["job_id"]) is not None

    cleaned = _cleanup(
        postgres_dsn,
        canary_state_directory,
        worker,
        submitted["capability"],
    )
    assert cleaned["status"] == "cleaned"
    assert _job_row(postgres_dsn, submitted["job_id"]) is None


def test_cleanup_commit_crash_before_cleaned_marker_recovers_absent_row(
    postgres_dsn: str,
    canary_state_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = FakeCanaryWorker()
    submitted = _submit(postgres_dsn, canary_state_directory, worker)
    _complete(postgres_dsn, worker, submitted["job_id"])
    _validate(
        postgres_dsn,
        canary_state_directory,
        submitted["capability"],
    )
    original_write = canary._atomic_write_marker
    crashed = False

    def crash_before_cleaned_marker(
        path: Path,
        marker: dict[str, Any],
    ) -> None:
        nonlocal crashed
        if marker["phase"] == "cleaned" and not crashed:
            crashed = True
            raise RuntimeError("simulated crash after row delete commit")
        original_write(path, marker)

    monkeypatch.setattr(
        canary,
        "_atomic_write_marker",
        crash_before_cleaned_marker,
    )
    with pytest.raises(RuntimeError, match="after row delete commit"):
        _cleanup(
            postgres_dsn,
            canary_state_directory,
            worker,
            submitted["capability"],
        )

    assert _marker(canary_state_directory)["phase"] == "cleanup-intent"
    assert _job_row(postgres_dsn, submitted["job_id"]) is None
    recovered = _cleanup(
        postgres_dsn,
        canary_state_directory,
        worker,
        submitted["capability"],
    )
    assert recovered["status"] == "cleaned"


def test_cleanup_requires_exact_capability_operation_source_and_asset(
    postgres_dsn: str,
    canary_state_directory: Path,
) -> None:
    worker = FakeCanaryWorker()
    submitted = _submit(postgres_dsn, canary_state_directory, worker)
    job_id = submitted["job_id"]

    invalid_identities = [
        {"capability": "0" * 64},
        {"source_sha": "c" * 40},
        {"operation_id": "pull-deploy-canary-0002"},
        {"expected_byteff2_commit": "d" * 40},
    ]
    for overrides in invalid_identities:
        arguments = {
            "capability": submitted["capability"],
            **overrides,
        }
        with pytest.raises(canary.DeploymentMonomerMdCanaryError):
            _cleanup(
                postgres_dsn,
                canary_state_directory,
                worker,
                **arguments,
            )

    assert worker.delete_calls == []
    assert _job_row(postgres_dsn, job_id) is not None
    _fail(postgres_dsn, worker, job_id)
    _cleanup(
        postgres_dsn,
        canary_state_directory,
        worker,
        submitted["capability"],
    )


def test_validated_row_digest_drift_refuses_artifact_or_database_deletion(
    postgres_dsn: str,
    canary_state_directory: Path,
) -> None:
    worker = FakeCanaryWorker()
    submitted = _submit(postgres_dsn, canary_state_directory, worker)
    _complete(postgres_dsn, worker, submitted["job_id"])
    _validate(
        postgres_dsn,
        canary_state_directory,
        submitted["capability"],
    )
    with postgres_connection(postgres_dsn) as connection:
        connection.execute(
            """
            UPDATE md.monomer_md_jobs
            SET progress_message = 'unexpected post-validation mutation',
                updated_at = now()
            WHERE job_id = %s
            """,
            (submitted["job_id"],),
        )

    with pytest.raises(
        canary.DeploymentMonomerMdCanaryError,
        match="changed before cleanup",
    ):
        _cleanup(
            postgres_dsn,
            canary_state_directory,
            worker,
            submitted["capability"],
        )

    assert worker.delete_calls == []
    assert submitted["job_id"] in worker.artifact_jobs
    assert _job_row(postgres_dsn, submitted["job_id"]) is not None


def test_pending_active_terminal_and_unowned_rows_fail_closed_or_clean_exactly(
    postgres_dsn: str,
    canary_state_directory: Path,
) -> None:
    pending_worker = FakeCanaryWorker()
    pending_worker.health_error = True
    with pytest.raises(canary.DeploymentMonomerMdCanaryBusy):
        _submit(postgres_dsn, canary_state_directory, pending_worker)
    pending_marker = _marker(canary_state_directory)
    assert _job_row(postgres_dsn, pending_marker["job_id"])["status"] == "pending"
    pending_worker.health_error = False
    _cleanup(
        postgres_dsn,
        canary_state_directory,
        pending_worker,
        pending_marker["capability"],
    )
    assert _job_row(postgres_dsn, pending_marker["job_id"]) is None

    active_worker = FakeCanaryWorker()
    unauthenticated_retry = _submit(
        postgres_dsn,
        canary_state_directory,
        active_worker,
    )
    assert unauthenticated_retry["status"] == "cleaned"
    assert unauthenticated_retry["validated"] is False
    active = _submit(
        postgres_dsn,
        canary_state_directory,
        active_worker,
        capability=pending_marker["capability"],
    )
    with pytest.raises(canary.DeploymentMonomerMdCanaryBusy):
        _cleanup(
            postgres_dsn,
            canary_state_directory,
            active_worker,
            active["capability"],
        )
    assert _job_row(postgres_dsn, active["job_id"])["status"] == "submitted"
    _fail(postgres_dsn, active_worker, active["job_id"])
    _cleanup(
        postgres_dsn,
        canary_state_directory,
        active_worker,
        active["capability"],
    )
    assert _job_row(postgres_dsn, active["job_id"]) is None

    unowned_operation = "pull-deploy-canary-unknown"
    unowned_job_id = canary.derive_canary_job_id(
        unowned_operation,
        SOURCE_SHA,
    )
    with postgres_connection(postgres_dsn) as connection:
        create_monomer_md_job_postgres(
            connection,
            job_id=unowned_job_id,
            input_smiles="CCO",
            canonical_smiles="CCO",
            requested_steps=300,
            config_json={},
            components={},
        )
    unowned_worker = FakeCanaryWorker()
    with pytest.raises(canary.DeploymentMonomerMdCanaryError):
        canary.submit_canary(
            dsn=postgres_dsn,
            state_directory=canary_state_directory,
            operation_id=unowned_operation,
            source_sha=SOURCE_SHA,
            expected_byteff2_commit=BYTEFF2_COMMIT,
            max_active_jobs=1,
            worker_client=unowned_worker,
        )
    unowned_marker = canary.read_canary_marker(
        canary_state_directory,
        unowned_operation,
        SOURCE_SHA,
        BYTEFF2_COMMIT,
    )
    with pytest.raises(canary.DeploymentMonomerMdCanaryError):
        canary.cleanup_canary(
            dsn=postgres_dsn,
            state_directory=canary_state_directory,
            operation_id=unowned_operation,
            source_sha=SOURCE_SHA,
            expected_byteff2_commit=BYTEFF2_COMMIT,
            capability=unowned_marker["capability"],
            worker_client=unowned_worker,
        )
    assert _job_row(postgres_dsn, unowned_job_id) is not None
    assert unowned_worker.delete_calls == []
