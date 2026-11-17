"""PostgreSQL authority for jobs, immutable request identities and quota receipts."""
from contextlib import contextmanager
import json
from uuid import uuid4
from fastapi import HTTPException
from psycopg.types.json import Jsonb
from app.auth.context import service_context, SYSTEM_USER_ID
from app.postgres_database import postgres_connection
from app.task_control import authorize_start
from task_usage import native_entitlements as entitlement

MODULE = "retrosynthesis"
TERMINAL = frozenset({"completed", "failed", "timed_out", "interrupted"})
MAX_RESULT_BYTES = 1024 * 1024
MAX_OWNER_ROWS = 1000
MAX_OWNER_BYTES = 128 * 1024 * 1024
PUBLIC_FIELDS = ("job_id", "status", "stage", "created_at", "updated_at", "started_at", "finished_at", "error_code")


def public_job(row):
    if row is None:
        raise HTTPException(404, detail={"code": "retrosynthesis_job_not_found"})
    result = {key: str(row[key]) if key == "job_id" else row[key] for key in PUBLIC_FIELDS}
    result["result"] = row["result_json"]
    return result


class RetrosynthesisRepository:
    def __init__(self, dsn, *, connection_factory=postgres_connection):
        self.dsn, self.connection_factory = dsn, connection_factory

    @contextmanager
    def connection(self, *, trusted=False):
        if trusted:
            with service_context(), self.connection_factory(self.dsn) as connection:
                yield connection
        else:
            with self.connection_factory(self.dsn) as connection:
                yield connection

    def ready(self):
        with self.connection() as c:
            return c.execute("SELECT to_regclass('retrosynthesis.jobs') IS NOT NULL AS ready").fetchone()["ready"]

    def get(self, owner, *, job_id=None, key=None):
        with self.connection() as c:
            column, value = ("job_id", job_id) if job_id is not None else ("idempotency_key", key)
            return c.execute(f"SELECT * FROM retrosynthesis.jobs WHERE owner_user_id=%s AND {column}=%s", (owner, value)).fetchone()

    def create(self, owner, body, key):
        fingerprint = entitlement.request_fingerprint(body)
        with self.connection(trusted=True) as c:
            # Serialize per-owner capacity and idempotency even when quota policy is off.
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("retrosynthesis-owner:"+owner,))
            old = c.execute("SELECT * FROM retrosynthesis.jobs WHERE owner_user_id=%s AND idempotency_key=%s", (owner,key)).fetchone()
            if old:
                if old["request_sha256"] != fingerprint:
                    raise HTTPException(409, detail={"code": "retrosynthesis_idempotency_conflict"})
                return old, False
            # Hold the account authorization through durable admission, even
            # when success allowances and usage metering are disabled.
            user = c.execute("SELECT status,is_system FROM auth.users WHERE user_id=%s FOR UPDATE",(owner,)).fetchone()
            system = user is not None and user["is_system"] and owner == SYSTEM_USER_ID
            if user is None or (user["status"] != "active" and not system):
                raise HTTPException(403,detail={"code":"retrosynthesis_account_disabled"})
            if not system:
                from task_usage.entitlements import resolve_access_tier
                if resolve_access_tier(c,owner_user_id=owner) != "member":
                    raise HTTPException(403,detail={"code":"retrosynthesis_membership_required"})
            capacity = c.execute("SELECT count(*) AS rows,coalesce(sum(storage_bytes),0) AS bytes FROM retrosynthesis.jobs WHERE owner_user_id=%s", (owner,)).fetchone()
            if capacity["rows"] >= MAX_OWNER_ROWS or capacity["bytes"] + MAX_RESULT_BYTES > MAX_OWNER_BYTES:
                raise HTTPException(429, detail={"code": "retrosynthesis_storage_capacity"})
            job_id, claim = uuid4(), uuid4()
            entitlement.reserve_native(c, owner_user_id=owner,module=MODULE,idempotency_key=key,
                request_fingerprint=fingerprint,native_task_id=job_id,
                spec={key:body[key] for key in ("num_beams","num_return_sequences","max_new_tokens")})
            row = c.execute("""INSERT INTO retrosynthesis.jobs(job_id,owner_user_id,idempotency_key,request_sha256,request_json,claim_token,storage_bytes)
                VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *""", (job_id,owner,key,fingerprint,Jsonb(body),claim,MAX_RESULT_BYTES)).fetchone()
            return row, True

    def queued(self, row, executor):
        with self.connection(trusted=True) as c:
            locked = c.execute("SELECT * FROM retrosynthesis.jobs WHERE job_id=%s AND owner_user_id=%s FOR UPDATE", (row["job_id"],row["owner_user_id"])).fetchone()
            if locked is None or locked["status"] != "pending" or locked["claim_token"] != row["claim_token"]:
                return False
            permit = entitlement.begin_native_dispatch(c,owner_user_id=str(row["owner_user_id"]),module=MODULE,native_task_id=row["job_id"],executor_id=executor)
            if not permit["allowed"]:
                return False
            c.execute("UPDATE retrosynthesis.jobs SET status='queued',stage='waiting_gpu',executor_id=%s,updated_at=now() WHERE job_id=%s", (executor,row["job_id"]))
            return True

    def running(self, row, executor):
        with self.connection(trusted=True) as c:
            # Account row -> job row lock order, matching native start authorization.
            if not authorize_start(c,owner_user_id=str(row["owner_user_id"]),table="retrosynthesis.jobs",key_column="job_id",key=str(row["job_id"])):
                return False
            locked = c.execute("SELECT * FROM retrosynthesis.jobs WHERE job_id=%s FOR UPDATE", (row["job_id"],)).fetchone()
            if locked["status"] != "queued" or locked["claim_token"] != row["claim_token"] or locked["executor_id"] != executor:
                return False
            permit = entitlement.claim_native_running(c,owner_user_id=str(row["owner_user_id"]),module=MODULE,native_task_id=row["job_id"],executor_id=executor)
            if not permit["allowed"]:
                return False
            c.execute("UPDATE retrosynthesis.jobs SET status='running',stage='predicting',started_at=now(),updated_at=now() WHERE job_id=%s", (row["job_id"],))
            return True

    def finish(self, row, executor, *, status, result=None, error_code=None, facts=None):
        if status not in TERMINAL:
            raise ValueError("terminal proof required")
        size = len(json.dumps(row["request_json"],ensure_ascii=True).encode()) + (len(json.dumps(result,ensure_ascii=True).encode()) if result is not None else 0)
        if size > MAX_RESULT_BYTES:
            status,result,error_code,facts,size = "failed",None,"result_too_large",None,1024
        with self.connection(trusted=True) as c:
            current = c.execute("SELECT * FROM retrosynthesis.jobs WHERE job_id=%s AND owner_user_id=%s FOR UPDATE", (row["job_id"],row["owner_user_id"])).fetchone()
            if current is None or current["status"] in TERMINAL or current["claim_token"] != row["claim_token"] or (current["executor_id"] and current["executor_id"] != executor):
                return False
            if current["status"] == "unknown" and current["executor_id"] != executor:
                return False
            c.execute("""UPDATE retrosynthesis.jobs SET status=%s,stage=%s,result_json=%s,error_code=%s,storage_bytes=%s,updated_at=now(),finished_at=now() WHERE job_id=%s""",
                (status,status,Jsonb(result) if result is not None else None,error_code,max(1,size),row["job_id"]))
            if result is not None and status == "completed":
                entitlement.settle_native(c,owner_user_id=str(row["owner_user_id"]),module=MODULE,native_task_id=row["job_id"],status=status,facts=facts,
                    result_ref={"module":MODULE,"native_task_id":str(row["job_id"])})
            else:
                entitlement.release_native(c,owner_user_id=str(row["owner_user_id"]),module=MODULE,native_task_id=row["job_id"],status=status,
                    source="retrosynthesis_executor",never_dispatched=current["status"] == "pending")
            return True

    def mark_unknown(self,row,executor):
        with self.connection(trusted=True) as c:
            current=c.execute("SELECT * FROM retrosynthesis.jobs WHERE job_id=%s FOR UPDATE",(row["job_id"],)).fetchone()
            if current is None or current["status"] in TERMINAL or current["claim_token"] != row["claim_token"] or current["executor_id"] != executor:
                return False
            c.execute("UPDATE retrosynthesis.jobs SET status='unknown',stage='awaiting_verification',error_code='executor_outcome_unknown',updated_at=now() WHERE job_id=%s",(row["job_id"],))
            entitlement.mark_native_unknown(c,owner_user_id=str(row["owner_user_id"]),module=MODULE,native_task_id=row["job_id"],reason="executor_outcome_unknown")
            return True

    def recover(self):
        """Called only while holding the single-manager lock. Never redispatch."""
        unresolved = False
        with self.connection(trusted=True) as c:
            rows = c.execute("SELECT * FROM retrosynthesis.jobs WHERE status IN ('pending','queued','running','unknown') ORDER BY created_at FOR UPDATE").fetchall()
            for row in rows:
                owner = str(row["owner_user_id"])
                if row["status"] in {"running","unknown"}:
                    unresolved = True
                    c.execute("UPDATE retrosynthesis.jobs SET status='unknown',stage='awaiting_verification',error_code='executor_interrupted',updated_at=now() WHERE job_id=%s", (row["job_id"],))
                    entitlement.mark_native_unknown(c,owner_user_id=owner,module=MODULE,native_task_id=row["job_id"],reason="executor_interrupted")
                else:
                    c.execute("UPDATE retrosynthesis.jobs SET status='interrupted',stage='interrupted',error_code='not_started_before_restart',finished_at=now(),updated_at=now(),storage_bytes=1024 WHERE job_id=%s", (row["job_id"],))
                    entitlement.release_native(c,owner_user_id=owner,module=MODULE,native_task_id=row["job_id"],status="interrupted",source="retrosynthesis_preexecution_cas_fenced",never_dispatched=row["status"] == "pending")
        return not unresolved
