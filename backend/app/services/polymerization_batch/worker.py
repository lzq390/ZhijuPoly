from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from .chemistry import engine_fingerprint
from .execution import ExecutionStopped, classify_isolated, generate_isolated, run_isolated
from .models import BatchError, BatchSettings
from .repository import BatchRepository, WORKER_LOCK
from .storage import digest, file_manifest, read_json, resolve_file, write_json


logger = logging.getLogger(__name__)


class BatchWorker:
    def __init__(self, dsn: str, config: BatchSettings):
        self.config, self.root = config, config.storage_root
        self.repository = BatchRepository(dsn, config)
        self.worker_id = uuid4().hex
        self.stopping = False
        self.engine = {}
        self.lock_connection = None
        self.last_heartbeat = 0.0

    def heartbeat(self, *, force=False):
        if self.lock_connection is not None:
            self.lock_connection.execute("SELECT 1")
        if force or time.monotonic() - self.last_heartbeat > 5:
            self.repository.heartbeat_worker(self.worker_id, self.engine, self.config.enabled, "批量计算 worker 已就绪。" if self.config.enabled else "批量聚合当前未启用。")
            self.last_heartbeat = time.monotonic()

    def execute(self, job: dict, chunk: dict) -> None:
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
            completed = self.repository.chunks(job["id"], completed_only=True)
            classification = [item["artifact"] for item in completed if item["kind"] == "classify"]
            def call(payload):
                guard()
                return run_isolated({**payload, "engine": job["engine"], "classification": classification,
                                     "target": job["options"]["target_class"]}, self.config, scratch, guard=guard)
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
                                        self.config, scratch, guard=guard, timeout=max(600, self.config.subprocess_seconds))
                path = directory / "manifest.json"
                write_json(path, exported)
            guard()
            artifact = file_manifest(self.root, path, "application/json")
            publication_uncertain = True
            published = self.repository.finish_unit(job, chunk, artifact, time.monotonic() - started, counts, exported)
            publication_uncertain = False
        except ExecutionStopped:
            # Shutdown checkpoints by releasing this uncommitted attempt. A
            # cancellation already stored in jobs is finalized on the next claim.
            self.repository.release_execution(job, time.monotonic() - started)
        except Exception as exc:
            logger.exception("batch execution failed: job=%s chunk=%s", job["id"], chunk["chunk_id"])
            self.repository.stop_execution(job, elapsed=time.monotonic() - started,
                                           code=getattr(exc, "code", "execution_error"), message=str(exc),
                                           finalize=chunk["kind"] != "export")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            # A lost commit acknowledgement must never delete a committed
            # shard. Recovery reads its DB reference; cleanup reaps true orphans.
            if not published and not publication_uncertain:
                shutil.rmtree(directory, ignore_errors=True)

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
                          for item in self.repository.chunks(job["id"], completed_only=True)}
            for attempt in parent.iterdir():
                if attempt.is_dir() and attempt not in references and attempt.stat().st_mtime < cutoff:
                    shutil.rmtree(attempt)

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
                with psycopg.connect(self.repository.dsn, autocommit=True, row_factory=dict_row, connect_timeout=3) as lock:
                    if not lock.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (WORKER_LOCK,)).fetchone()["acquired"]:
                        time.sleep(2)
                        continue
                    self.lock_connection = lock
                    # A previous disconnected owner checks its session every 0.8s
                    # and kills its child before publishing. Fence all old tokens.
                    self.repository.recover()
                    time.sleep(2)
                    self.engine = engine_fingerprint()
                    self.heartbeat(force=True)
                    last_cleanup = 0.0
                    while not self.stopping:
                        self.heartbeat()
                        if time.monotonic() - last_cleanup > 60:
                            try:
                                self.cleanup()
                            except OSError:
                                logger.exception("batch retention cleanup will retry")
                            last_cleanup = time.monotonic()
                        claimed = self.repository.claim(self.worker_id, self.engine["fingerprint"]) if self.config.enabled else None
                        if claimed:
                            self.execute(*claimed)
                        else:
                            time.sleep(1)
            except Exception:
                logger.exception("batch worker unavailable; retrying")
                try:
                    self.repository.heartbeat_worker(self.worker_id, {}, False, "批量计算 worker 暂不可用。")
                except Exception:
                    pass
                time.sleep(3)
            finally:
                self.lock_connection = None
        self.repository.heartbeat_worker(self.worker_id, self.engine, False, "批量计算 worker 已停止。")
