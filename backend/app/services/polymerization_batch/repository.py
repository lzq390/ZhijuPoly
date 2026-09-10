from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from psycopg.types.json import Jsonb

from app.postgres_database import postgres_connection
from app.services.deployment_control import get_drain_state
from .models import BatchError, BatchSettings, TERMINAL_STATUSES
from .storage import check_id


WORKER_LOCK = 785391204617
CAPACITY_LOCK = 785391204618


class BatchRepository:
    def __init__(self, dsn: str, config: BatchSettings):
        self.dsn, self.config = dsn, config

    def connection(self):
        return postgres_connection(self.dsn)

    def ready(self) -> bool:
        with self.connection() as conn:
            row = conn.execute("SELECT to_regclass('polymerization_batch.chunks') AS table_name").fetchone()
            return row["table_name"] is not None

    def worker_status(self) -> dict | None:
        with self.connection() as conn:
            return conn.execute("""SELECT available, message, engine,
                heartbeat_at > now() - interval '30 seconds' AS fresh
                FROM polymerization_batch.worker_status WHERE singleton""").fetchone()

    def heartbeat_worker(self, worker_id: str, engine: dict, available: bool, message: str) -> None:
        with self.connection() as conn:
            if get_drain_state(conn, lock=True).enabled:
                return
            conn.execute("""INSERT INTO polymerization_batch.worker_status
                (singleton, worker_id, available, message, engine) VALUES (true,%s,%s,%s,%s)
                ON CONFLICT (singleton) DO UPDATE SET worker_id=EXCLUDED.worker_id,
                available=EXCLUDED.available, message=EXCLUDED.message, engine=EXCLUDED.engine, heartbeat_at=now()""",
                (worker_id, available, message, Jsonb(engine)))

    def create_import(self, import_id: str, files: dict) -> None:
        with self.connection() as conn:
            conn.execute("INSERT INTO polymerization_batch.imports(id,files,expires_at) VALUES (%s,%s,%s)",
                         (import_id, Jsonb(files), datetime.now(timezone.utc) + timedelta(hours=self.config.import_hours)))

    def get_import(self, import_id: str, conn=None, *, lock=False) -> dict:
        check_id(import_id)
        if conn is None:
            with self.connection() as connection:
                return self.get_import(import_id, connection, lock=lock)
        row = conn.execute("SELECT * FROM polymerization_batch.imports WHERE id=%s" + (" FOR UPDATE" if lock else ""), (import_id,)).fetchone()
        if row is None:
            raise BatchError("导入记录不存在或已过期，请重新上传。", "import_expired", 410)
        if row["expires_at"] <= datetime.now(timezone.utc):
            raise BatchError("导入文件已过期，请重新上传。", "import_expired", 410)
        return row

    def save_preview(self, import_id: str, revision: str, preview: dict) -> None:
        with self.connection() as conn:
            self.get_import(import_id, conn, lock=True)
            updated = conn.execute("UPDATE polymerization_batch.imports SET preview=%s WHERE id=%s AND preview_revision=%s RETURNING id",
                         (Jsonb(preview), import_id, revision)).fetchone()
            if not updated:
                raise BatchError("已有更新的预检请求，请使用最新结果。", "stale_preview", 409)

    def begin_preview(self, import_id: str, revision: str) -> None:
        with self.connection() as conn:
            self.get_import(import_id, conn, lock=True)
            conn.execute("UPDATE polymerization_batch.imports SET preview_revision=%s,preview=NULL WHERE id=%s", (revision, import_id))

    def get_job(self, job_id: str, conn=None, *, lock=False) -> dict:
        check_id(job_id)
        if conn is None:
            with self.connection() as connection:
                return self.get_job(job_id, connection, lock=lock)
        row = conn.execute("SELECT * FROM polymerization_batch.jobs WHERE id=%s" + (" FOR UPDATE" if lock else ""), (job_id,)).fetchone()
        if row is None:
            raise BatchError("任务不存在。", "not_found", 404)
        return row

    def existing_job(self, key: str, request_hash: str, conn) -> dict | None:
        row = conn.execute("SELECT * FROM polymerization_batch.jobs WHERE idempotency_key=%s", (key,)).fetchone()
        if row and row["request_hash"] != request_hash:
            raise BatchError("此提交标识已用于不同参数。", "idempotency_conflict", 409)
        return row

    def admit(self, conn, key: str, request_hash: str) -> dict | None:
        draining = get_drain_state(conn, lock=True).enabled
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (CAPACITY_LOCK,))
        existing = self.existing_job(key, request_hash, conn)
        if existing:
            return existing
        if draining:
            raise BatchError("服务正在维护，请稍后提交。", "draining", 503)
        count = conn.execute("SELECT count(*) AS n FROM polymerization_batch.jobs WHERE status IN ('queued','running','cancelling')").fetchone()["n"]
        if count >= self.config.queue_capacity + 1:
            raise BatchError("批量任务队列已满，请稍后重试。", "queue_full", 429)

    def insert_job(self, conn, job_id: str, options: dict, request_hash: str, key: str, engine: dict, summary: dict, chunks: list[dict]) -> dict:
        row = conn.execute("""INSERT INTO polymerization_batch.jobs
            (id,import_id,idempotency_key,request_hash,options,engine,summary)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (job_id, options["import_id"], key, request_hash, Jsonb(options), Jsonb(engine), Jsonb(summary))).fetchone()
        with conn.cursor() as cursor:
            cursor.executemany("""INSERT INTO polymerization_batch.chunks
                (job_id,chunk_id,phase,ordinal,kind,payload) VALUES (%s,%s,%s,%s,%s,%s)""",
                [(job_id, chunk["chunk_id"], chunk["phase"], index, chunk["kind"], Jsonb(chunk["payload"])) for index, chunk in enumerate(chunks)])
        return row

    def chunks(self, job_id: str, *, completed_only=False) -> list[dict]:
        with self.connection() as conn:
            return conn.execute("SELECT * FROM polymerization_batch.chunks WHERE job_id=%s" +
                                (" AND status='completed'" if completed_only else "") + " ORDER BY phase,ordinal", (job_id,)).fetchall()

    def cancel(self, job_id: str) -> dict:
        with self.connection() as conn:
            job = self.get_job(job_id, conn, lock=True)
            if job["status"] not in TERMINAL_STATUSES:
                conn.execute("""UPDATE polymerization_batch.jobs SET terminal_intent='cancelled',status='cancelling',
                    updated_at=now() WHERE id=%s AND terminal_intent IS NULL""", (job_id,))
            return self.get_job(job_id, conn)

    def recover(self) -> None:
        # Only called while holding the session-level singleton worker lock.
        # Attempt fencing makes files from a previous execution unpublishable.
        with self.connection() as conn:
            draining = get_drain_state(conn, lock=True).enabled
            # During drain, only stale executions (which still block drain)
            # may be fenced. Do not mutate idle attempts after active_total=0.
            conn.execute("""UPDATE polymerization_batch.chunks SET attempt_token=NULL,lease_expires_at=NULL
                WHERE status='pending' AND attempt_token IS NOT NULL
                AND (%s OR job_id IN (SELECT id FROM polymerization_batch.jobs WHERE execution_token IS NOT NULL))""",
                (not draining,))
            conn.execute("""UPDATE polymerization_batch.jobs SET
                consumed_seconds=consumed_seconds+GREATEST(0, EXTRACT(EPOCH FROM
                    COALESCE(heartbeat_at,execution_started_at)-execution_started_at)),
                execution_token=NULL, execution_started_at=NULL, worker_id=NULL,
                heartbeat_at=NULL,updated_at=now() WHERE execution_token IS NOT NULL""")

    def claim(self, worker_id: str, fingerprint: str) -> tuple[dict, dict] | None:
        with self.connection() as conn:
            if get_drain_state(conn, lock=True).enabled:
                return None
            job = conn.execute("""SELECT * FROM polymerization_batch.jobs
                WHERE status IN ('queued','running','cancelling') AND execution_token IS NULL
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
            if job is None:
                return None
            if job["engine"].get("fingerprint") != fingerprint:
                conn.execute("""UPDATE polymerization_batch.jobs SET status='failed',stage='finished',
                    error_code='engine_version_mismatch',message='计算版本已变化，请重新提交任务。',
                    finished_at=now(),expires_at=now()+%s,updated_at=now() WHERE id=%s""",
                    (timedelta(days=self.config.retention_days), job["id"]))
                return None
            if job["consumed_seconds"] >= self.config.job_seconds and not job["terminal_intent"]:
                conn.execute("""UPDATE polymerization_batch.jobs SET terminal_intent='failed',
                    error_code='job_timeout',message='累计计算时间超过限额。' WHERE id=%s""", (job["id"],))
                job["terminal_intent"] = "failed"
            if job["terminal_intent"]:
                conn.execute("UPDATE polymerization_batch.chunks SET status='skipped' WHERE job_id=%s AND kind<>'export' AND status='pending'", (job["id"],))
            chunk = conn.execute("SELECT * FROM polymerization_batch.chunks WHERE job_id=%s AND status='pending' ORDER BY phase,ordinal LIMIT 1", (job["id"],)).fetchone()
            if chunk is None:
                raise RuntimeError("active batch job has no pending execution unit")
            token = uuid4().hex
            chunk = conn.execute("""UPDATE polymerization_batch.chunks SET attempt=attempt+1,
                attempt_token=%s,lease_expires_at=now()+%s WHERE job_id=%s AND chunk_id=%s RETURNING *""",
                (token, timedelta(seconds=self.config.lease_seconds), job["id"], chunk["chunk_id"])).fetchone()
            job = conn.execute("""UPDATE polymerization_batch.jobs SET execution_token=%s,worker_id=%s,
                execution_started_at=now(),heartbeat_at=now(),status=CASE WHEN terminal_intent='cancelled' THEN 'cancelling' ELSE 'running' END,
                stage=%s,updated_at=now() WHERE id=%s RETURNING *""",
                (token, worker_id, chunk["kind"], job["id"])).fetchone()
            return job, chunk

    def heartbeat_execution(self, job_id: str, token: str) -> dict:
        with self.connection() as conn:
            row = conn.execute("UPDATE polymerization_batch.jobs SET heartbeat_at=now() WHERE id=%s AND execution_token=%s RETURNING terminal_intent", (job_id, token)).fetchone()
            if row is None:
                raise RuntimeError("batch execution was fenced")
            conn.execute("""UPDATE polymerization_batch.chunks SET lease_expires_at=now()+%s
                WHERE job_id=%s AND attempt_token=%s AND status='pending'""",
                (timedelta(seconds=self.config.lease_seconds), job_id, token))
            return row

    def finish_unit(self, job: dict, chunk: dict, artifact: dict, elapsed: float, counts: dict | None = None, export: dict | None = None) -> bool:
        with self.connection() as conn:
            current = self.get_job(job["id"], conn, lock=True)
            if current["execution_token"] != job["execution_token"]:
                return False
            # Cancellation arriving during export cannot publish a 'completed'
            # workbook; run finalization again with the new terminal intent.
            if export is not None and current["terminal_intent"] != job["terminal_intent"]:
                conn.execute("UPDATE polymerization_batch.jobs SET execution_token=NULL,worker_id=NULL,consumed_seconds=consumed_seconds+%s WHERE id=%s", (elapsed, job["id"]))
                return False
            conn.execute("""UPDATE polymerization_batch.chunks SET status='completed',artifact=%s,
                counts=%s,lease_expires_at=NULL,completed_at=now() WHERE job_id=%s AND chunk_id=%s""",
                         (Jsonb(artifact), Jsonb(counts or {}), job["id"], chunk["chunk_id"]))
            summary = dict(current["summary"])
            for key, value in (counts or {}).items():
                summary[key] = summary.get(key, 0) + value
            if export is None:
                conn.execute("""UPDATE polymerization_batch.jobs SET execution_token=NULL,worker_id=NULL,
                    consumed_seconds=consumed_seconds+%s,summary=%s,updated_at=now() WHERE id=%s""",
                    (elapsed, Jsonb(summary), job["id"]))
            else:
                conn.execute("""UPDATE polymerization_batch.jobs SET execution_token=NULL,worker_id=NULL,
                    consumed_seconds=consumed_seconds+%s,status=%s,stage='finished',summary=%s,artifacts=%s,
                    finished_at=now(),expires_at=now()+%s,updated_at=now() WHERE id=%s""",
                    (elapsed, export["status"], Jsonb(export["summary"]), Jsonb(export["artifacts"]),
                     timedelta(days=self.config.retention_days), job["id"]))
            return True

    def stop_execution(self, job: dict, *, elapsed: float, code: str, message: str, finalize: bool) -> None:
        with self.connection() as conn:
            conn.execute("""UPDATE polymerization_batch.chunks SET attempt_token=NULL,lease_expires_at=NULL
                WHERE job_id=%s AND attempt_token=%s AND status='pending'""", (job["id"], job["execution_token"]))
            conn.execute("""UPDATE polymerization_batch.jobs SET execution_token=NULL,worker_id=NULL,
                terminal_intent=COALESCE(terminal_intent,'failed'),error_code=%s,message=%s,
                consumed_seconds=consumed_seconds+%s,status=CASE WHEN %s THEN status ELSE 'failed' END,
                stage=CASE WHEN %s THEN stage ELSE 'finished' END,
                finished_at=CASE WHEN %s THEN finished_at ELSE now() END,
                expires_at=CASE WHEN %s THEN expires_at ELSE now()+%s END,updated_at=now()
                WHERE id=%s AND execution_token=%s""",
                (code, message[:500], elapsed, finalize, finalize, finalize, finalize,
                 timedelta(days=self.config.retention_days), job["id"], job["execution_token"]))

    def release_execution(self, job: dict, elapsed: float) -> None:
        with self.connection() as conn:
            conn.execute("""UPDATE polymerization_batch.chunks SET attempt_token=NULL,lease_expires_at=NULL
                WHERE job_id=%s AND attempt_token=%s AND status='pending'""", (job["id"], job["execution_token"]))
            conn.execute("""UPDATE polymerization_batch.jobs SET execution_token=NULL,execution_started_at=NULL,worker_id=NULL,
                consumed_seconds=consumed_seconds+%s,updated_at=now() WHERE id=%s AND execution_token=%s""",
                (elapsed, job["id"], job["execution_token"]))
