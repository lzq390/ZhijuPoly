from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from app.services.deployment_control import enable_drain
from app.services.polymerization_batch import worker as worker_module
from app.services.polymerization_batch.execution import classify_isolated, generate_isolated
from app.services.polymerization_batch.models import BatchError, BatchJobCreate, BatchSettings
from app.services.polymerization_batch.service import BatchService
from app.services.polymerization_batch.storage import digest, file_manifest, write_json
from app.services.polymerization_batch.worker import BatchWorker


@pytest.fixture
def review_batch(tmp_path, postgres_dsn):
    # postgres_dsn belongs to conftest's newly created temporary test database.
    config = BatchSettings(enabled=True, storage_root=tmp_path / "batch")
    service = BatchService(postgres_dsn, config)
    worker = BatchWorker(postgres_dsn, config)
    worker.engine = {"fingerprint": "worker-review"}
    with service.repository.connection() as conn:
        conn.execute("UPDATE governance.deployment_control SET drain_enabled=false,reason=NULL,release_sha=NULL,activated_by=NULL")
    worker.heartbeat(force=True)
    import_id, revision = uuid4().hex, uuid4().hex
    source = config.storage_root / "imports" / import_id
    source.mkdir(parents=True)
    files, tables = {}, {}
    for role, smiles in (("a", "CC"), ("b", "NN")):
        path = source / f"{role}.csv"
        path.write_text(f"SMILES\n{smiles}\n")
        files[role] = {**file_manifest(config.storage_root, path, "text/csv"), "filename": path.name, "format": "csv"}
        tables[role] = {"unique_smiles": [smiles], "rows": [{"canonical_smiles": smiles}]}
    service.repository.create_import(import_id, files)
    snapshot_path = source / "snapshot.json"
    write_json(snapshot_path, {"engine": worker.engine, "tables": tables})
    service.repository.begin_preview(import_id, revision)
    service.repository.save_preview(import_id, revision, {
        "can_submit": True,
        "snapshot": file_manifest(config.storage_root, snapshot_path, "application/json"),
        "statistics": {"raw_pairs": 1, "valid_pairs": 1, "unique_pairs": 1},
    })
    request = BatchJobCreate(import_id=import_id, preview_revision=revision)
    return service, worker, request


def test_lost_job_commit_ack_preserves_inputs_for_idempotent_retry(review_batch, monkeypatch):
    service, _, request = review_batch
    original = service.repository.connection
    acknowledgement_lost = False

    @contextmanager
    def disconnect_after_commit():
        nonlocal acknowledgement_lost
        with original() as conn:
            yield conn
            inserted = conn.execute("SELECT count(*) AS n FROM polymerization_batch.jobs").fetchone()["n"]
        if inserted and not acknowledgement_lost:
            acknowledgement_lost = True
            raise RuntimeError("commit acknowledged by server, connection lost before client received it")

    monkeypatch.setattr(service.repository, "connection", disconnect_after_commit)
    key = uuid4().hex
    with pytest.raises(RuntimeError, match="connection lost"):
        service.create_job(request, key)
    retry = service.create_job(request, key)
    job = service.repository.get_job(retry["job_id"])
    assert digest(service.root / job["options"]["snapshot_path"]) == job["options"]["snapshot_sha256"]
    assert (service.root / "jobs" / job["id"] / "source_a.csv").is_file()
    with original() as conn:
        assert conn.execute("SELECT count(*) AS n FROM polymerization_batch.jobs").fetchone()["n"] == 1


@pytest.mark.parametrize("kind", ["classify", "export"])
def test_lost_chunk_commit_ack_preserves_committed_artifact(review_batch, monkeypatch, kind):
    service, worker, request = review_batch
    job_id = service.create_job(request, uuid4().hex)["job_id"]
    if kind == "export":
        with service.repository.connection() as conn:
            conn.execute("UPDATE polymerization_batch.chunks SET status='skipped' WHERE job_id=%s AND kind<>'export'", (job_id,))
    job, chunk = worker.repository.claim(worker.worker_id, worker.engine["fingerprint"])

    def compute(payload, *args, **kwargs):
        if kind == "classify":
            return {"rows": [], "errors": {}}
        output = service.root / payload["directory"]
        output.mkdir(parents=True)
        archive = output / "results.zip"
        archive.write_bytes(b"PK published export")
        return {"status": "completed", "summary": {}, "artifacts": {
            "results.zip": file_manifest(service.root, archive, "application/zip"),
        }}

    monkeypatch.setattr(worker_module, "run_isolated", compute)
    original = worker.repository.finish_unit

    def disconnect_after_commit(*args, **kwargs):
        assert original(*args, **kwargs)
        raise RuntimeError("commit acknowledgement lost")

    monkeypatch.setattr(worker.repository, "finish_unit", disconnect_after_commit)
    worker.execute(job, chunk)
    committed = worker.repository.chunks(job_id, completed_only=True)
    assert len(committed) == 1
    artifact = committed[0]["artifact"]
    assert digest(service.root / artifact["path"]) == artifact["sha256"]
    worker.repository.recover()
    assert worker.repository.chunks(job_id, completed_only=True) == committed
    assert worker.repository.get_job(job_id)["terminal_intent"] is None
    if kind == "export":
        handle, _ = service.artifact(job_id, "results.zip")
        with handle:
            assert handle.read() == b"PK published export"
        assert worker.repository.get_job(job_id)["status"] == "completed"


@pytest.mark.parametrize("active", [False, True])
def test_recovery_during_drain_only_fences_outstanding_executions(review_batch, active):
    service, worker, request = review_batch
    job_id = service.create_job(request, uuid4().hex)["job_id"]
    worker.repository.claim(worker.worker_id, worker.engine["fingerprint"])
    with service.repository.connection() as conn:
        if not active:
            # Cancellation can release an export token while its uncommitted
            # attempt still awaits the next claim. It does not block drain.
            conn.execute("UPDATE polymerization_batch.jobs SET execution_token=NULL WHERE id=%s", (job_id,))
        enable_drain(conn, reason="review", activated_by="review", release_sha="a" * 40)
    before_job = worker.repository.get_job(job_id)
    before_chunks = worker.repository.chunks(job_id)
    worker.repository.recover()
    if active:
        assert worker.repository.get_job(job_id)["execution_token"] is None
        assert all(item["attempt_token"] is None for item in worker.repository.chunks(job_id))
    else:
        assert worker.repository.get_job(job_id) == before_job
        assert worker.repository.chunks(job_id) == before_chunks
    assert worker.repository.claim(worker.worker_id, worker.engine["fingerprint"]) is None


def test_attempt_directory_failure_reaches_failed_terminal_state(review_batch, monkeypatch):
    service, worker, request = review_batch
    job_id = service.create_job(request, uuid4().hex)["job_id"]
    original = Path.mkdir

    def full_disk(path, *args, **kwargs):
        if path.parent.name == "attempts":
            raise OSError("No space left on device")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", full_disk)
    worker.execute(*worker.repository.claim(worker.worker_id, worker.engine["fingerprint"]))
    assert worker.repository.get_job(job_id)["terminal_intent"] == "failed"
    worker.execute(*worker.repository.claim(worker.worker_id, worker.engine["fingerprint"]))
    failed = worker.repository.get_job(job_id)
    assert failed["status"] == "failed" and failed["stage"] == "finished"
    assert failed["execution_token"] is None


@pytest.mark.parametrize("kind", ["classify", "generate"])
def test_overall_timeout_stops_without_bisecting_remaining_pairs(kind):
    calls = []

    def timed_out(request):
        calls.append(request)
        raise BatchError("累计计算时间超过限额。", "job_timeout")

    with pytest.raises(BatchError, match="累计计算时间"):
        if kind == "classify":
            classify_isolated(["a", "b", "c"], timed_out)
        else:
            list(generate_isolated(["a", "b"], ["c", "d"], timed_out))
    assert len(calls) == 1


def test_child_exit_during_cancellation_preserves_cancellation(tmp_path, monkeypatch):
    import subprocess
    import sys
    from app.services.polymerization_batch import execution

    original_start, original_kill = subprocess.Popen, execution.os.killpg
    children = []

    def start(arguments, **kwargs):
        child = original_start([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    def exit_before_kill(pid, sig):
        original_kill(pid, sig)
        children[0].wait(timeout=5)
        raise ProcessLookupError("child exited after poll")

    def cancel():
        raise execution.ExecutionStopped("cancelled")

    monkeypatch.setattr(execution.subprocess, "Popen", start)
    monkeypatch.setattr(execution.os, "killpg", exit_before_kill)
    with pytest.raises(execution.ExecutionStopped, match="cancelled"):
        execution.run_isolated({}, BatchSettings(), tmp_path, guard=cancel)
    assert children[0].poll() is not None
    assert list(tmp_path.iterdir()) == []
