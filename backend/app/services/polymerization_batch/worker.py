from __future__ import annotations

import json
import fcntl
from contextlib import contextmanager
from threading import RLock
import logging
import os
import shutil
import signal
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from functools import wraps
from app.auth.context import service_context
from app.task_observability import TaskExecutionContext, log_task_event

import psycopg
from psycopg.rows import dict_row

from .chemistry import engine_fingerprint
from .execution import CleanupPendingError, ExecutionStopped, classify_isolated, generate_isolated, run_isolated
from .models import BatchError, BatchSettings
from .repository import BatchRepository, WORKER_LOCK
from .storage import digest, file_manifest, read_json, resolve_file, write_json


logger = logging.getLogger(__name__)


def _service_operation(function):
    @wraps(function)
    def run(*args, **kwargs):
        with service_context():
            return function(*args, **kwargs)
    return run


def _resource_operation(function):
    @wraps(function)
    def run(self, *args, **kwargs):
        with self._resource_lease():
            return function(self, *args, **kwargs)
    return run


class BatchWorker:
    def __init__(self, dsn: str, config: BatchSettings):
        self.config, self.root = config, config.storage_root
        self.repository = BatchRepository(dsn, config)
        self.worker_id = uuid4().hex
        self.stopping = False
        self.engine = {}
        self.lock_connection = None
        self.last_heartbeat = 0.0
        self._resource_file = None
        self._resource_mutex = RLock()

    @contextmanager
    def _resource_lease(self):
        # This deployment has one local storage_root shared by its batch workers.
        # The child inherits this open-file-description: losing the DB session
        # or the parent process cannot release physical capacity before it exits.
        with self._resource_mutex:
            if self._resource_file is not None:
                yield self._resource_file.fileno()
                return
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / ".worker-execution.lock").open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._resource_file = handle
                try:
                    yield handle.fileno()
                finally:
                    self._resource_file = None
                    # Only close our descriptor. LOCK_UN would also unlock the
                    # descriptor inherited by a still-running child.

    @staticmethod
    def _execution_context(job: dict, chunk: dict | None = None):
        return TaskExecutionContext(owner_user_id=str(job["owner_user_id"]), request_id=None,
                                    task_type="polymerization_batch", channel="batch", task_id=job["id"],
                                    attempt_id=chunk["chunk_id"] if chunk else None)

    @staticmethod
    def _clean_scratch(path: Path, context=None):
        try:
            try:
                shutil.rmtree(path)
            except FileNotFoundError:
                pass
            # A disappearing descendant is not proof the scratch root is gone.
            try:
                path.lstat()
            except FileNotFoundError:
                return
            raise OSError("Batch scratch directory remains after cleanup")
        except OSError as exc:
            if context is not None:
                log_task_event(context, "cleanup_pending", reason="scratch_cleanup_failed")
            raise CleanupPendingError("Batch scratch cleanup is still pending") from exc

    @_service_operation
    @_resource_operation
    def recover(self):
        # Holding the inherited OS lock proves every previous child has exited.
        # Files must also be gone before dropping any execution fencing token.
        with self.repository.connection() as conn:
            active = conn.execute("SELECT id,owner_user_id,execution_token FROM polymerization_batch.jobs WHERE execution_token IS NOT NULL").fetchall()
        for job in active:
            self._clean_scratch(self.root / "jobs" / job["id"] / "attempts" / job["execution_token"] / "scratch", self._execution_context(job))
        self.repository.recover(cleanup_confirmed=True)
        for job in active:
            log_task_event(self._execution_context(job), "resources_released", reason="worker_recovered")

    @_service_operation
    def heartbeat(self, *, force=False):
        if self.lock_connection is not None:
            self.lock_connection.execute("SELECT 1")
        if force or time.monotonic() - self.last_heartbeat > 5:
            self.repository.heartbeat_worker(self.worker_id, self.engine, self.config.enabled, "批量计算 worker 已就绪。" if self.config.enabled else "批量聚合当前未启用。")
            self.last_heartbeat = time.monotonic()

    @_service_operation
    @_resource_operation
    def execute(self, job: dict, chunk: dict) -> None:
        context = self._execution_context(job, chunk)
        log_task_event(context, "execution_started")
        started = time.monotonic()
        directory = self.root / "jobs" / job["id"] / "attempts" / job["execution_token"]
        scratch = directory / "scratch"
        last_check = 0.0
        published = False
        publication_uncertain = False
        def guard():
            nonlocal last_check
            if time.monotonic() - last_check < 0.8:
                return
            self.heartbeat()
            state = self.repository.heartbeat_execution(job["id"], job["execution_token"])
            last_check = time.monotonic()
            if self.stopping:
                raise ExecutionStopped("worker stopping")
            if state["terminal_intent"] and chunk["kind"] != "export":
                raise ExecutionStopped("task cancellation requested")
            if (chunk["kind"] != "export" or not job["terminal_intent"]) and job["consumed_seconds"] + time.monotonic() - started > self.config.job_seconds:
                raise BatchError("累计计算时间超过限额。", "job_timeout", 422)
        try:
            directory.mkdir(parents=True)
            snapshot_path = resolve_file(self.root, job["options"]["snapshot_path"])
            if digest(snapshot_path) != job["options"]["snapshot_sha256"]:
                raise BatchError("输入快照校验失败。", "artifact_corrupt", 503)
            snapshot = read_json(snapshot_path)
            completed = self.repository.chunks_for_service(job["id"], completed_only=True)
            classification = [item["artifact"] for item in completed if item["kind"] == "classify"]
            def call(payload):
                guard()
                return run_isolated({**payload, "engine": job["engine"], "classification": classification,
                                     "target": job["options"]["target_class"]}, self.config, scratch, guard=guard, resource_lock_fd=self._resource_file.fileno())
            counts, exported = {}, None
            if chunk["kind"] == "classify":
                data = classify_isolated(chunk["payload"]["smiles"], call)
                path = directory / "classification.json"
                write_json(path, data)
                counts = {"classified_unique_monomers": len(data["rows"]) + len(data["errors"]), "classification_errors": len(data["errors"])}
            elif chunk["kind"] == "generate":
                path = directory / "pairs.jsonl"
                a_counts = Counter(row["canonical_smiles"] for row in snapshot["tables"]["a"]["rows"] if row["canonical_smiles"])
                b_counts = Counter(row["canonical_smiles"] for row in snapshot["tables"]["b"]["rows"] if row["canonical_smiles"])
                counts = {"processed_pairs": 0, "computed_unique_pairs": 0, "candidate_count": 0, "pair_errors": 0}
                existing_bytes = sum(item["artifact"]["size_bytes"] for item in completed)
                with path.open("wb") as handle:
                    for pair in generate_isolated(chunk["payload"]["a"], chunk["payload"]["b"], call):
                        encoded = (json.dumps(pair, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode()
                        if existing_bytes + handle.tell() + len(encoded) > self.config.result_bytes:
                            raise BatchError("分片结果超过存储限额。", "result_limit", 413)
                        handle.write(encoded)
                        multiplicity = a_counts[pair["a"]] * b_counts[pair["b"]]
                        counts["processed_pairs"] += multiplicity
                        counts["computed_unique_pairs"] += 1
                        counts["candidate_count"] += len(pair["candidates"]) * multiplicity
                        counts["pair_errors"] += multiplicity if pair["status"] == "error" else 0
                    handle.flush()
                    os.fsync(handle.fileno())
            else:
                # Only the public snapshot and source manifests are sent to the
                # exporter; datetime DB fields never enter the JSON protocol.
                export_job = {key: job[key] for key in ("id", "options", "engine", "summary", "terminal_intent", "error_code", "message")}
                exported = run_isolated({"action": "export", "engine": job["engine"], "job": export_job,
                                         "snapshot": job["options"]["snapshot_path"],
                                         "chunks": [{"kind": item["kind"], "artifact": item["artifact"]} for item in completed],
                                         "directory": str((directory / "output").relative_to(self.root))},
                                        self.config, scratch, guard=guard, timeout=max(600, self.config.subprocess_seconds), resource_lock_fd=self._resource_file.fileno())
                path = directory / "manifest.json"
                write_json(path, exported)
            guard()
            artifact = file_manifest(self.root, path, "application/json")
            self._clean_scratch(scratch, context)
            publication_uncertain = True
            published = self.repository.finish_unit(job, chunk, artifact, time.monotonic() - started, counts, exported)
            publication_uncertain = False
            log_task_event(context, "resources_released", reason="completed" if published else "publication_fenced")
        except CleanupPendingError:
            # Leave the existing state and token occupied. A later recovery may
            # clear them only after obtaining the OS lock and removing scratch.
            publication_uncertain = True
            log_task_event(context, "cleanup_pending", reason="cleanup_unconfirmed")
            raise
        except ExecutionStopped:
            # Shutdown checkpoints by releasing this uncommitted attempt. A
            # cancellation already stored in jobs is finalized on the next claim.
            self._clean_scratch(scratch, context)
            self.repository.release_execution(job, time.monotonic() - started)
            log_task_event(context, "resources_released", reason="worker_stopped" if self.stopping else "cancelled")
        except Exception as exc:
            self._clean_scratch(scratch, context)
            logger.warning("batch execution failed: job=%s chunk=%s error_type=%s", job["id"], chunk["chunk_id"], type(exc).__name__)
            self.repository.stop_execution(job, elapsed=time.monotonic() - started,
                                           code=getattr(exc, "code", "execution_error"), message=str(exc),
                                           finalize=chunk["kind"] != "export")
            log_task_event(context, "resources_released", reason="execution_error")
        finally:
            # A lost commit acknowledgement must never delete a committed
            # shard. Recovery reads its DB reference; cleanup reaps true orphans.
            if not published and not publication_uncertain:
                shutil.rmtree(directory, ignore_errors=True)

    @_service_operation
    def cleanup(self):
        with self.repository.connection() as conn:
            from app.services.deployment_control import get_drain_state
            if get_drain_state(conn, lock=True).enabled:
                return
            # Row locks serialize admission against import deletion.
            imports = conn.execute("SELECT id FROM polymerization_batch.imports WHERE expires_at<now() ORDER BY expires_at FOR UPDATE SKIP LOCKED LIMIT 50").fetchall()
            for row in imports:
                shutil.rmtree(self.root / "imports" / row["id"], ignore_errors=False) if (self.root / "imports" / row["id"]).exists() else None
                conn.execute("DELETE FROM polymerization_batch.imports WHERE id=%s", (row["id"],))
            jobs = conn.execute("""SELECT id FROM polymerization_batch.jobs WHERE expires_at<now() AND status<>'expired'
                AND execution_token IS NULL ORDER BY expires_at FOR UPDATE SKIP LOCKED LIMIT 50""").fetchall()
            for row in jobs:
                path = self.root / "jobs" / row["id"]
                if path.exists():
                    shutil.rmtree(path)
                conn.execute("DELETE FROM polymerization_batch.chunks WHERE job_id=%s", (row["id"],))
                conn.execute("UPDATE polymerization_batch.jobs SET status='expired',stage='finished',artifacts='{}',updated_at=now() WHERE id=%s", (row["id"],))
        # Crash-before-commit artifacts have no published references. Remove only
        # old orphan directories; current admissions are never touched.
        cutoff = time.time() - self.config.import_hours * 3600
        for kind, table in (("imports", "imports"), ("jobs", "jobs")):
            parent = self.root / kind
            if not parent.exists():
                continue
            for path in parent.iterdir():
                if not path.is_dir() or path.stat().st_mtime >= cutoff:
                    continue
                with self.repository.connection() as conn:
                    exists = conn.execute(f"SELECT 1 FROM polymerization_batch.{table} WHERE id=%s", (path.name,)).fetchone()
                if not exists:
                    shutil.rmtree(path)
        # Reclaim old attempts abandoned between filesystem publication and DB
        # commit. Completed chunk references and any current attempt stay intact.
        with self.repository.connection() as conn:
            idle = conn.execute("SELECT id FROM polymerization_batch.jobs WHERE execution_token IS NULL AND status<>'expired'").fetchall()
        for job in idle:
            parent = self.root / "jobs" / job["id"] / "attempts"
            if not parent.exists():
                continue
            references = {resolve_file(self.root, item["artifact"]["path"]).parent
                          for item in self.repository.chunks_for_service(job["id"], completed_only=True)}
            for attempt in parent.iterdir():
                if attempt.is_dir() and attempt not in references and attempt.stat().st_mtime < cutoff:
                    shutil.rmtree(attempt)

    @_service_operation
    def run(self):
        # A compatibility baseline runs with the feature disabled before 0016.
        # It must not load chemistry, require the new schema, or mutate audit data.
        if not self.config.enabled:
            while not self.stopping:
                time.sleep(1)
            return
        self.root.mkdir(parents=True, exist_ok=True)
        while not self.stopping:
            try:
                with self._resource_lease(), psycopg.connect(self.repository.dsn, autocommit=True, row_factory=dict_row, connect_timeout=3) as lock:
                    if not lock.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (WORKER_LOCK,)).fetchone()["acquired"]:
                        time.sleep(2)
                        continue
                    self.lock_connection = lock
                    self.recover()
                    self.engine = engine_fingerprint()
                    self.heartbeat(force=True)
                    last_cleanup = 0.0
                    while not self.stopping:
                        self.heartbeat()
                        if time.monotonic() - last_cleanup > 60:
                            try:
                                self.cleanup()
                            except OSError as exc:
                                logger.warning("batch retention cleanup will retry: error_type=%s", type(exc).__name__)
                            last_cleanup = time.monotonic()
                        claimed = self.repository.claim(self.worker_id, self.engine["fingerprint"]) if self.config.enabled else None
                        if claimed:
                            self.execute(*claimed)
                        else:
                            time.sleep(1)
            except Exception as exc:
                # Chained filesystem errors can contain the private attempt
                # directory/token. Operational logs only need a safe type.
                logger.warning("batch worker unavailable; retrying: error_type=%s", type(exc).__name__)
                try:
                    self.repository.heartbeat_worker(self.worker_id, {}, False, "批量计算 worker 暂不可用。")
                except Exception:
                    pass
                time.sleep(3)
            finally:
                self.lock_connection = None
        self.repository.heartbeat_worker(self.worker_id, self.engine, False, "批量计算 worker 已停止。")
