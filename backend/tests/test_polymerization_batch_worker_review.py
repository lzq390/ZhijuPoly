from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from app.auth.context import current_owner_id, service_context

from test_auth_isolation import auth_database
from test_private_batch_support import batch_environment, admin_connection

from app.services.deployment_control import enable_drain
from app.services.polymerization_batch import worker as worker_module
from app.services.polymerization_batch.execution import classify_isolated, generate_isolated
from app.services.polymerization_batch.models import BatchError, BatchJobCreate, BatchSettings
from app.services.polymerization_batch.service import BatchService
from app.services.polymerization_batch.storage import digest, file_manifest, write_json
from app.services.polymerization_batch.worker import BatchWorker


@pytest.fixture
def review_batch(tmp_path, auth_database):
    with batch_environment(tmp_path, auth_database) as (service, worker, _user, _settings):
        config = service.config
        worker.engine = {"fingerprint": "worker-review"}
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
        service.repository.create_import(import_id, files, owner_user_id=current_owner_id())
        snapshot_path = source / "snapshot.json"
        write_json(snapshot_path, {"engine": worker.engine, "tables": tables})
        service.repository.begin_preview(import_id, revision, owner_user_id=current_owner_id())
        service.repository.save_preview(import_id, revision, {
            "can_submit": True,
            "snapshot": file_manifest(config.storage_root, snapshot_path, "application/json"),
            "statistics": {"raw_pairs": 1, "valid_pairs": 1, "unique_pairs": 1},
        }, owner_user_id=current_owner_id())
        request = BatchJobCreate(import_id=import_id, preview_revision=revision)
        yield service, worker, request


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
    job = service.repository.get_job(retry["job_id"], owner_user_id=current_owner_id())
    assert digest(service.root / job["options"]["snapshot_path"]) == job["options"]["snapshot_sha256"]
    assert (service.root / "jobs" / job["id"] / "source_a.csv").is_file()
    with original() as conn:
        assert conn.execute("SELECT count(*) AS n FROM polymerization_batch.jobs").fetchone()["n"] == 1


@pytest.mark.parametrize("kind", ["classify", "export"])
def test_lost_chunk_commit_ack_preserves_committed_artifact(review_batch, monkeypatch, kind):
    service, worker, request = review_batch
    job_id = service.create_job(request, uuid4().hex)["job_id"]
    if kind == "export":
        with admin_connection(service) as conn:
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
    committed = worker.repository.chunks(job_id, completed_only=True, owner_user_id=current_owner_id())
    assert len(committed) == 1
    artifact = committed[0]["artifact"]
    assert digest(service.root / artifact["path"]) == artifact["sha256"]
    worker.recover()
    assert worker.repository.chunks(job_id, completed_only=True, owner_user_id=current_owner_id()) == committed
    assert worker.repository.get_job(job_id, owner_user_id=current_owner_id())["terminal_intent"] is None
    if kind == "export":
        handle, _ = service.artifact(job_id, "results.zip")
        with handle:
            assert handle.read() == b"PK published export"
        assert worker.repository.get_job(job_id, owner_user_id=current_owner_id())["status"] == "completed"


@pytest.mark.parametrize("active", [False, True])
def test_recovery_during_drain_only_fences_outstanding_executions(review_batch, active):
    service, worker, request = review_batch
    job_id = service.create_job(request, uuid4().hex)["job_id"]
    worker.repository.claim(worker.worker_id, worker.engine["fingerprint"])
    with admin_connection(service) as conn:
        if not active:
            # Cancellation can release an export token while its uncommitted
            # attempt still awaits the next claim. It does not block drain.
            conn.execute("UPDATE polymerization_batch.jobs SET execution_token=NULL WHERE id=%s", (job_id,))
        enable_drain(conn, reason="review", activated_by="review", release_sha="a" * 40)
    before_job = worker.repository.get_job(job_id, owner_user_id=current_owner_id())
    before_chunks = worker.repository.chunks(job_id, owner_user_id=current_owner_id())
    worker.recover()
    if active:
        assert worker.repository.get_job(job_id, owner_user_id=current_owner_id())["execution_token"] is None
        assert all(item["attempt_token"] is None for item in worker.repository.chunks(job_id, owner_user_id=current_owner_id()))
    else:
        assert worker.repository.get_job(job_id, owner_user_id=current_owner_id()) == before_job
        assert worker.repository.chunks(job_id, owner_user_id=current_owner_id()) == before_chunks
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
    assert worker.repository.get_job(job_id, owner_user_id=current_owner_id())["terminal_intent"] == "failed"
    worker.execute(*worker.repository.claim(worker.worker_id, worker.engine["fingerprint"]))
    failed = worker.repository.get_job(job_id, owner_user_id=current_owner_id())
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


def test_inherited_resource_lock_blocks_recovery_after_parent_descriptor_closes(tmp_path):
    import subprocess
    import sys
    config = BatchSettings(enabled=True, storage_root=tmp_path / 'batch')
    original = BatchWorker('unused', config)
    replacement = BatchWorker('unused', config)
    child = None
    try:
        with original._resource_lease() as descriptor:
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], pass_fds=(descriptor,))
        # The old worker's descriptor is closed; its live child still owns it.
        with pytest.raises(BlockingIOError):
            replacement.recover()
        child.kill()
        child.wait(timeout=5)
        with replacement._resource_lease():
            pass
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_popen_failure_closes_worker_resource_descriptor(tmp_path, monkeypatch):
    from app.services.polymerization_batch import execution
    config = BatchSettings(enabled=True, storage_root=tmp_path / 'batch')
    original, replacement = BatchWorker('unused', config), BatchWorker('unused', config)
    def fail(*_args, **_kwargs):
        raise OSError('cannot start child')
    monkeypatch.setattr(execution.subprocess, 'Popen', fail)
    scratch = config.storage_root / 'scratch'
    with pytest.raises(OSError, match='cannot start child'):
        with original._resource_lease() as descriptor:
            execution.run_isolated({}, config, scratch, resource_lock_fd=descriptor)
    assert list(scratch.iterdir()) == []
    with replacement._resource_lease():
        pass


def test_cancel_waits_for_actual_child_exit_before_return(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.services.polymerization_batch import execution
    entered, exited = Event(), Event()
    class Child:
        pid = 12345
        def poll(self):
            return 0 if exited.is_set() else None
        def wait(self, timeout=None):
            assert timeout is None
            entered.set()
            assert exited.wait(3)
            return 0
    monkeypatch.setattr(execution.subprocess, 'Popen', lambda *_args, **_kwargs: Child())
    monkeypatch.setattr(execution.os, 'killpg', lambda *_args: None)
    def cancelled():
        raise execution.ExecutionStopped('cancelled')
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(execution.run_isolated, {}, BatchSettings(), tmp_path, guard=cancelled)
        assert entered.wait(2)
        assert not future.done()
        assert list(tmp_path.iterdir())  # Scratch cannot disappear before exit.
        exited.set()
        with pytest.raises(execution.ExecutionStopped):
            future.result(2)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('kind', ['classify','export'])
@pytest.mark.parametrize('failure', ['child_cleanup','scratch_cleanup'])
def test_uncertain_cleanup_retains_execution_token_until_confirmed_recovery(review_batch, monkeypatch, caplog, kind, failure):
    import json
    import logging
    from app.services.polymerization_batch.execution import CleanupPendingError
    caplog.set_level(logging.INFO, logger="nexpoly.task_control")
    service, worker, request = review_batch
    job_id = service.create_job(request, uuid4().hex)['job_id']
    if kind == 'export':
        with admin_connection(service) as connection:
            connection.execute("UPDATE polymerization_batch.chunks SET status='skipped' WHERE job_id=%s AND kind<>'export'", (job_id,))
    job, chunk = worker.repository.claim(worker.worker_id, worker.engine['fingerprint'])
    original_remove = worker_module.shutil.rmtree
    def compute(_payload, _config, scratch, **_options):
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / 'proof').write_text('pending')
        if failure == 'child_cleanup':
            raise CleanupPendingError('process cleanup cannot be confirmed')
        if kind == 'classify':
            return {'rows':[], 'errors':{}}
        return {'status':'completed', 'summary':{}, 'artifacts':{}}
    def cannot_clean(path, *args, **kwargs):
        if Path(path).name == 'scratch':
            raise PermissionError('scratch cleanup denied')
        return original_remove(path, *args, **kwargs)
    monkeypatch.setattr(worker_module, 'run_isolated', compute)
    if failure == 'scratch_cleanup':
        monkeypatch.setattr(worker_module.shutil, 'rmtree', cannot_clean)
    with pytest.raises(CleanupPendingError):
        worker.execute(job, chunk)
    pending = worker.repository.get_job(job_id, owner_user_id=current_owner_id())
    assert pending['execution_token'] == job['execution_token']
    assert pending['status'] == 'running'
    events = [json.loads(record.message) for record in caplog.records if record.name == "nexpoly.task_control"]
    assert any(event['event'] == 'cleanup_pending' and event['task_id'] == job_id for event in events)
    assert not any(event['event'] == 'resources_released' for event in events)
    assert job['execution_token'] not in '\n'.join(record.message for record in caplog.records)
    assert worker.repository.claim(worker.worker_id, worker.engine['fingerprint']) is None
    with pytest.raises(BatchError) as capacity:
        service.create_job(request, uuid4().hex)
    assert capacity.value.code == 'user_capacity'
    if failure == 'scratch_cleanup':
        with pytest.raises(CleanupPendingError):
            worker.recover()
        assert worker.repository.get_job(job_id, owner_user_id=current_owner_id())['execution_token'] == job['execution_token']
        monkeypatch.setattr(worker_module.shutil, 'rmtree', original_remove)
    worker.recover()
    recovered = worker.repository.get_job(job_id, owner_user_id=current_owner_id())
    assert recovered['execution_token'] is None
    assert not (service.root / 'jobs' / job_id / 'attempts' / job['execution_token'] / 'scratch').exists()
    events = [json.loads(record.message) for record in caplog.records if record.name == "nexpoly.task_control"]
    assert any(event['event'] == 'resources_released' and event['reason'] == 'worker_recovered' for event in events)


def test_missing_scratch_descendant_does_not_prove_root_cleanup(tmp_path, monkeypatch):
    from app.services.polymerization_batch.execution import CleanupPendingError
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    (scratch / 'retained').write_text('still allocated')
    def disappearing_descendant(_path):
        raise FileNotFoundError('a descendant disappeared while removing it')
    monkeypatch.setattr(worker_module.shutil, 'rmtree', disappearing_descendant)
    with pytest.raises(CleanupPendingError):
        BatchWorker._clean_scratch(scratch)
    assert (scratch / 'retained').exists()


def test_worker_retry_log_does_not_expose_private_attempt_path(tmp_path, monkeypatch, caplog):
    import logging
    from app.services.polymerization_batch.execution import CleanupPendingError
    worker = BatchWorker('unused', BatchSettings(enabled=True, storage_root=tmp_path))
    private_token = uuid4().hex
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def execute(self, *_args):
            return self
        def fetchone(self):
            return {'acquired': True}
    def failed_recovery():
        worker.stopping = True
        try:
            raise PermissionError(13, 'cleanup denied', str(tmp_path / 'attempts' / private_token))
        except PermissionError as error:
            raise CleanupPendingError('cleanup is pending') from error
    monkeypatch.setattr(worker_module.psycopg, 'connect', lambda *_args, **_kwargs: Connection())
    monkeypatch.setattr(worker, 'recover', failed_recovery)
    monkeypatch.setattr(worker.repository, 'heartbeat_worker', lambda *_args: None)
    monkeypatch.setattr(worker_module.time, 'sleep', lambda _seconds: None)
    caplog.set_level(logging.WARNING, logger=worker_module.__name__)
    worker.run()
    assert 'CleanupPendingError' in caplog.text
    assert private_token not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
