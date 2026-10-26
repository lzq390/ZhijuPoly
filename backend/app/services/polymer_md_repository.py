"""Owner-scoped durable tasks; all execution transitions use the service identity."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import re
import secrets
from typing import Any
from uuid import uuid4

from app.auth.context import service_context
from app.postgres_database import postgres_connection
from polymer_md.contracts import (ACTIVE_STATUSES, TERMINAL_STATUSES, ScientificRequest,
                                  SubmitRequest, WorkerSnapshot, StartAuthorizationRequest,
                                  request_sha256)
from .polymer_md_schema import schema_installed, validate_polymer_md_schema
from .monomer_dft_repository import sanitize_public_json, sanitize_public_text
from task_usage import entitlements
from task_usage.ledger import metering_enabled, record_event

CAPACITY_LOCK = 742128925057020
RECONCILE_LOCK = 742128925057021


class PolymerMdError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


def _require_new_computation(connection, owner_user_id):
    """Caller holds auth.users FOR UPDATE, shared with disable/tier changes."""
    if entitlements.resolve_access_tier(connection, owner_user_id=owner_user_id) != "member":
        raise PolymerMdError("membership_required", "该计算需要会员账号。", 403)
    if entitlements.enabled():
        raise PolymerMdError("entitlements_unsupported", "当前额度策略下暂不能提交聚合物 MD 计算。", 503)


def _record_usage(connection, job):
    if not metering_enabled():
        return
    status = {"pending": "queued", "queued": "queued"}.get(job["status"], job["status"])
    if status == "cancel_requested":
        return  # Intent never proves a terminal result or resource cleanup.
    if status in TERMINAL_STATUSES and not job["cleanup_confirmed"]:
        return
    submitted = job["created_at"]
    # The database authorization is the logical start, not measured CPU time.
    started = job.get("start_authorized_at") if status != "queued" else None
    finished = job.get("finished_at") if status in TERMINAL_STATUSES else None
    occurred = submitted if status == "queued" else started if status == "running" else finished
    if occurred is None:
        return
    metrics = {}
    if started is not None:
        metrics["queue_ms"] = int((started - submitted).total_seconds() * 1000)
    if finished is not None:
        metrics["observed_wall_ms"] = int((finished - submitted).total_seconds() * 1000)
    task = "polymer_md:" + str(job["job_id"])
    record_event(connection, owner_user_id=str(job["owner_user_id"]), module="polymer_md",
        task_id=task, attempt_id="1", event_key=task + ":1:" + status, status=status,
        occurred_at=occurred, submitted_at=submitted, started_at=started,
        finished_at=finished, metrics=metrics)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def safe_scientific_json(value):
    """Strip internal file locations without silently truncating measured series."""
    if isinstance(value,dict):
        return {key:safe_scientific_json(item) for key,item in value.items()
                if key not in {"trajectory","checkpoint","path","directory","work_dir","traceback","socket","uds","storage_ref","attempt_token"}
                and not key.endswith(("_file","_path","_dir","_root"))}
    if isinstance(value,list):
        return [safe_scientific_json(item) for item in value]
    if isinstance(value,str) and value.startswith(("/","file://","unix://")):
        return "[private]"
    return value


def public_job(job: dict) -> dict:
    forbidden = {"owner_user_id", "idempotency_key", "input_sha256", "request_sha256", "attempt_token", "attempt_fingerprint", "worker_instance_id", "dispatch_started_at", "enqueue_sequence"}
    result = {k: v for k, v in job.items() if k not in forbidden and not k.startswith("_")}
    result["artifacts"] = [{k:v for k,v in artifact.items() if k not in {"storage_ref","attempt_token"}} for artifact in job.get("artifacts",[])]
    # The caller has already verified ownership. Publish a stable evidence
    # identity without exposing the credential used for execution fencing.
    job_id, token = job.get("job_id"), job.get("attempt_token")
    if (isinstance(job_id, str) and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", job_id)
            and isinstance(token, str) and re.fullmatch(r"[0-9a-f]{64}", token)):
        result["attempt_fingerprint"] = hashlib.sha256(
            ("polymer-md-attempt-v1:" + job_id + ":" + token).encode("utf-8")).hexdigest()
    return result


def submit_request(job: dict) -> SubmitRequest:
    return SubmitRequest(**{k: str(job[k]) if k in {"job_id", "owner_user_id"} else job[k]
                            for k in ("job_id", "owner_user_id", "attempt_token", "request_sha256", "enqueue_sequence", "scientific_request")})


def validate_science(request: ScientificRequest) -> ScientificRequest:
    # RDKit is the existing API chemistry validator, never a QM/MD engine.
    from rdkit import Chem, rdBase
    allowed = {0, 1, 6, 7, 8, 9, 14, 15, 16, 17, 35, 53}

    def validated_molecule(smiles):
        with rdBase.BlockLogs():
            mol = Chem.MolFromSmiles(smiles)
        if mol is None or len(Chem.GetMolFrags(mol)) != 1:
            raise PolymerMdError("invalid_smiles", "重复单元 SMILES 无法解析。", 422)
        linkers = [a for a in mol.GetAtoms() if a.GetAtomicNum() == 0]
        if (len(linkers) != 2 or any(a.GetDegree() != 1 for a in linkers)
            or mol.GetNumHeavyAtoms() > 100 or Chem.GetFormalCharge(mol) != 0
            or any(a.GetFormalCharge() or a.GetNumRadicalElectrons() or a.GetIsotope()
                   or a.GetAtomicNum() not in allowed for a in mol.GetAtoms())):
            raise PolymerMdError("unsupported_chemistry", "各重复单元须为中性闭壳层、两个连接点且元素受支持的结构。", 422)
        if any(a.GetNeighbors()[0].GetAtomicNum() == 0 or a.GetBonds()[0].GetBondTypeAsDouble() != 1 for a in linkers):
            raise PolymerMdError("unsupported_connection", "连接点必须通过单键连接真实原子。", 422)
        return mol

    if request.model_configuration is None:
        mol = validated_molecule(request.smiles)
        return request.model_copy(update={"smiles": Chem.MolToSmiles(mol, canonical=True)})

    def statistics(mol):
        atoms = [atom for atom in Chem.AddHs(mol).GetAtoms() if atom.GetAtomicNum() != 0]
        return {"atoms": len(atoms), "mass": sum(atom.GetMass() for atom in atoms)}

    components = []
    stats = {}
    for component in request.model_configuration.components:
        mol = validated_molecule(component.smiles)
        components.append(component.model_copy(update={"smiles": Chem.MolToSmiles(mol, canonical=True)}))
        stats[component.id] = statistics(mol)
    model = request.model_configuration.model_copy(update={"components": components})
    normalized = request.model_copy(update={"smiles": components[0].smiles, "model_configuration": model})
    terminal_smiles = {"CH3": "*C", "H": "*[H]", "OH": "*O"}
    with rdBase.BlockLogs():
        terminal_stats = [statistics(Chem.MolFromSmiles(terminal_smiles[name]))
                          for name in (request.terminal_head, request.terminal_tail)]
    from polymer_md.model_configuration import estimate_model_atoms
    try:
        estimated = estimate_model_atoms(normalized.model_dump(mode="json"), stats, terminal_stats)
    except ValueError as exc:
        raise PolymerMdError("model_target_too_small", "目标链长无法形成至少一个有效重复单元，请增大链长。", 422) from exc
    if estimated > 20000 or request.calculation == "basic_tc" and estimated * request.tc_rep > 60000:
        raise PolymerMdError("model_atom_budget", "按重复单元、端基和组成核算后的体系超过原子数上限；建链后仍会复核实际规模。", 422)
    return normalized


class PolymerMdRepository:
    def __init__(self, dsn: str, *, connection_factory=postgres_connection):
        self.dsn, self.connection_factory = dsn, connection_factory

    @contextmanager
    def service_connection(self):
        with service_context(), self.connection_factory(self.dsn) as c:
            yield c

    def schema_ready(self) -> bool:
        try:
            with self.service_connection() as c:
                if not schema_installed(c):
                    return False
                from app.auth.isolation_ledger import validate_application_migration_ledger
                validate_application_migration_ledger(c, require_usage=True, require_entitlements=True)
                validate_polymer_md_schema(c)
                from app.auth.schema import validate_runtime_role
                validate_runtime_role(c,"nexpoly_service")
                return True
        except Exception:
            return False

    @contextmanager
    def reconciliation_leader(self):
        with self.service_connection() as c:
            acquired = c.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (RECONCILE_LOCK,)).fetchone()["acquired"]
            try:
                yield acquired
            finally:
                if acquired:
                    c.execute("SELECT pg_advisory_unlock(%s)", (RECONCILE_LOCK,))

    def _get(self, c, job_id: str, *, owner: str | None = None, lock=False):
        query = "SELECT * FROM polymer_md.jobs WHERE job_id=%s::uuid"
        args: tuple = (job_id,)
        if owner is not None:
            query += " AND owner_user_id=%s::uuid"
            args += (owner,)
        if lock:
            query += " FOR UPDATE"
        row = c.execute(query, args).fetchone()
        if row is None:
            raise PolymerMdError("job_not_found", "任务不存在。", 404)
        job = dict(row)
        job["job_id"], job["owner_user_id"] = str(job["job_id"]), str(job["owner_user_id"])
        job["stage_runs"] = [dict(r) for r in c.execute("SELECT stage_run_id,stage,attempt,status,quality_status,results,quality,errors,started_at,finished_at FROM polymer_md.stage_runs WHERE job_id=%s ORDER BY started_at NULLS FIRST,stage_run_id", (job_id,))]
        job["artifacts"] = [dict(r) for r in c.execute("SELECT artifact_id,stage_run_id,kind,filename,media_type,size_bytes,sha256,storage_ref,attempt_token FROM polymer_md.artifacts WHERE job_id=%s AND deleted_at IS NULL ORDER BY stage_run_id,artifact_id", (job_id,))]
        return job

    def get_job(self, job_id: str, *, owner_user_id: str):
        with self.connection_factory(self.dsn) as c:
            return self._get(c, job_id, owner=owner_user_id)

    def get_service_job(self, job_id: str):
        with self.service_connection() as c:
            return self._get(c, job_id)

    def list_jobs(self, *, owner_user_id: str, page=1, page_size=20):
        with self.connection_factory(self.dsn) as c:
            total = c.execute("SELECT count(*) AS n FROM polymer_md.jobs WHERE owner_user_id=%s::uuid", (owner_user_id,)).fetchone()["n"]
            ids = c.execute("SELECT job_id FROM polymer_md.jobs WHERE owner_user_id=%s::uuid ORDER BY enqueue_sequence DESC LIMIT %s OFFSET %s", (owner_user_id, page_size, (page-1)*page_size)).fetchall()
            return {"items": [public_job(self._get(c, str(r["job_id"]), owner=owner_user_id)) for r in ids], "total": total, "page": page, "page_size": page_size}

    def find_idempotent_job(self, request: ScientificRequest, *, owner_user_id: str, idempotency_key: str):
        """An accepted request can be replayed while execution transport is down."""
        input_hash=request_sha256(validate_science(request))
        with self.connection_factory(self.dsn) as c:
            existing=c.execute("SELECT job_id,input_sha256 FROM polymer_md.jobs WHERE owner_user_id=%s::uuid AND idempotency_key=%s",(owner_user_id,idempotency_key)).fetchone()
            if existing is None:
                return None
            if existing["input_sha256"]!=input_hash:
                raise PolymerMdError("idempotency_conflict", "幂等键已用于其他参数。")
            return self._get(c,str(existing["job_id"]),owner=owner_user_id)

    def create_job(self, request: ScientificRequest, *, owner_user_id: str, idempotency_key: str):
        normalized = validate_science(request)
        input_hash = request_sha256(normalized)
        with self.service_connection() as c:
            user = c.execute("SELECT status,is_system FROM auth.users WHERE user_id=%s::uuid FOR UPDATE", (owner_user_id,)).fetchone()
            if not user or user["status"] != "active":
                raise PolymerMdError("account_disabled", "账号当前无法提交计算。", 403)
            c.execute("SELECT pg_advisory_xact_lock(%s)", (CAPACITY_LOCK,))
            existing = c.execute("SELECT job_id,input_sha256 FROM polymer_md.jobs WHERE owner_user_id=%s::uuid AND idempotency_key=%s FOR UPDATE", (owner_user_id, idempotency_key)).fetchone()
            if existing:
                if existing["input_sha256"] != input_hash:
                    raise PolymerMdError("idempotency_conflict", "幂等键已用于其他参数。")
                return self._get(c, str(existing["job_id"])), False
            _require_new_computation(c, owner_user_id)
            own = c.execute("SELECT count(*) AS n FROM polymer_md.jobs WHERE owner_user_id=%s::uuid AND status=ANY(%s)", (owner_user_id, list(ACTIVE_STATUSES))).fetchone()["n"]
            total = c.execute("SELECT count(*) AS n FROM polymer_md.jobs WHERE status=ANY(%s)", (list(ACTIVE_STATUSES),)).fetchone()["n"]
            if own or total >= 3:
                raise PolymerMdError("capacity_full", "本人或全局活动任务名额已满。", 429)
            effective = normalized.model_copy(update={"seed": normalized.seed or secrets.randbelow(2147483646)+1})
            job_id, token = str(uuid4()), secrets.token_hex(32)
            sha = request_sha256(effective)
            c.execute("""INSERT INTO polymer_md.jobs(job_id,owner_user_id,idempotency_key,input_sha256,request_sha256,scientific_request,attempt_token)
                VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s)""", (job_id, owner_user_id, idempotency_key, input_hash, sha, _json(effective.model_dump()), token))
            c.execute("INSERT INTO polymer_md.job_attempts(job_id,attempt,attempt_token,request_sha256) VALUES(%s,1,%s,%s)", (job_id, token, sha))
            job = self._get(c, job_id)
            _record_usage(c, job)
            return job, True

    def claim_dispatch(self, job_id: str, token: str):
        with self.service_connection() as c:
            row = c.execute("""UPDATE polymer_md.jobs SET status='queued',dispatch_started_at=now(),updated_at=now()
                WHERE job_id=%s AND attempt_token=%s AND status='pending' RETURNING job_id""", (job_id, token)).fetchone()
            return bool(row)

    def list_active(self):
        with self.service_connection() as c:
            ids = c.execute("SELECT job_id FROM polymer_md.jobs WHERE status=ANY(%s) ORDER BY enqueue_sequence", (list(ACTIVE_STATUSES),)).fetchall()
            return [self._get(c, str(r["job_id"])) for r in ids]

    def request_cancel(self, job_id: str, *, owner_user_id: str):
        with self.service_connection() as c:
            job = self._get(c, job_id, owner=owner_user_id, lock=True)
            if job["status"] in TERMINAL_STATUSES:
                return job
            # Even an undispatched pending cancel is sent as a durable worker
            # tombstone; an unknown/late submit can never start afterwards.
            c.execute("UPDATE polymer_md.jobs SET status='cancel_requested',cancel_requested_at=now(),updated_at=now() WHERE job_id=%s", (job_id,))
            return self._get(c, job_id)

    def authorize_start(self, request: StartAuthorizationRequest):
        with self.service_connection() as c:
            owner = c.execute("SELECT owner_user_id FROM polymer_md.jobs WHERE job_id=%s", (request.job_id,)).fetchone()
            if owner is None or str(owner["owner_user_id"]) != request.owner_user_id:
                raise PolymerMdError("attempt_rejected", "Execution identity rejected", 409)
            user = c.execute("SELECT status FROM auth.users WHERE user_id=%s FOR UPDATE", (owner["owner_user_id"],)).fetchone()
            # Match admission's lock order. This database guard is shared by
            # every Worker/job root; a filesystem RootLock cannot enforce the
            # deployment-wide execution limit or durable FIFO ordering.
            c.execute("SELECT pg_advisory_xact_lock(%s)",(CAPACITY_LOCK,))
            job = self._get(c, request.job_id, lock=True)
            self._fence(job, request)
            if job["status"] not in {"queued", "running"}:
                raise PolymerMdError("start_rejected", "Execution is not startable", 409)
            if job["start_authorized_at"] is None:
                rejection = "account_disabled" if not user or user["status"] != "active" else None
                if rejection is None:
                    try:
                        _require_new_computation(c, request.owner_user_id)
                    except PolymerMdError as exc:
                        if exc.code not in {"membership_required", "entitlements_unsupported"}:
                            raise
                        rejection = exc.code
                if rejection:
                    c.execute("UPDATE polymer_md.jobs SET status='cancel_requested',cancel_requested_at=now(),error_code=%s,updated_at=now() WHERE job_id=%s", (rejection, request.job_id))
                    return None
            instance = job["worker_instance_id"]
            if instance is not None and instance != request.worker_instance_id:
                if request.replaces_worker_instance_id != instance or not request.prior_execution_cleanup_confirmed:
                    raise PolymerMdError("worker_instance_conflict", "Worker identity changed without confirmed recovery", 409)
            # Do not lock other jobs: snapshot/cancel paths lock their own job
            # and must remain able to commit confirmed cleanup. Unknown
            # responses and cancel_requested holders retain execution rights.
            holder=c.execute("""SELECT job_id FROM polymer_md.jobs
                WHERE job_id<>%s AND start_authorized_at IS NOT NULL
                  AND NOT(status=ANY(%s) AND cleanup_confirmed) LIMIT 1""",
                (request.job_id,list(TERMINAL_STATUSES))).fetchone()
            if holder is not None:
                raise PolymerMdError("execution_slot_busy","前一执行尚未确认清理，当前任务继续排队。",409)
            predecessor=c.execute("""SELECT job_id FROM polymer_md.jobs
                WHERE job_id<>%s AND enqueue_sequence<%s AND status=ANY(%s)
                ORDER BY enqueue_sequence LIMIT 1""",
                (request.job_id,job['enqueue_sequence'],list(ACTIVE_STATUSES))).fetchone()
            if predecessor is not None:
                raise PolymerMdError("fifo_predecessor_pending","更早的活动任务尚未完成，当前任务继续排队。",409)
            row = c.execute("""UPDATE polymer_md.jobs SET worker_instance_id=%s,start_authorized_at=COALESCE(start_authorized_at,now()),updated_at=now()
                WHERE job_id=%s RETURNING start_authorized_at""", (request.worker_instance_id, request.job_id)).fetchone()
            c.execute("UPDATE polymer_md.job_attempts SET worker_instance_id=%s,start_authorized_at=%s,updated_at=now() WHERE job_id=%s AND attempt_token=%s", (request.worker_instance_id, row["start_authorized_at"], request.job_id, request.attempt_token))
            return row["start_authorized_at"]

    @staticmethod
    def _fence(job, request):
        if any(str(job[k]) != str(getattr(request, k)) for k in ("job_id", "attempt_token", "request_sha256", "enqueue_sequence")):
            raise PolymerMdError("stale_attempt", "Execution attempt no longer matches", 409)

    def apply_snapshot(self, snapshot: WorkerSnapshot):
        with self.service_connection() as c:
            job = self._get(c, snapshot.job_id, lock=True)
            self._fence(job, snapshot)
            if snapshot.revision <= job["snapshot_revision"]:
                return job
            if job["worker_instance_id"] is not None and job["worker_instance_id"] != snapshot.worker_instance_id:
                raise PolymerMdError("worker_instance_conflict", "Worker identity changed", 409)
            if job["status"] in TERMINAL_STATUSES:
                return job
            if snapshot.status in {"running", "completed"} and job["start_authorized_at"] is None:
                raise PolymerMdError("start_not_authorized", "Worker has not obtained durable start authorization", 409)
            if job["status"] == "running" and snapshot.status in {"pending", "queued"}:
                raise PolymerMdError("snapshot_regression", "Worker snapshot regressed", 409)
            target = snapshot.status
            if target == "pending":
                raise PolymerMdError("invalid_worker_state", "Worker cannot return pending", 409)
            if job["status"] == "cancel_requested" and target not in TERMINAL_STATUSES:
                target = "cancel_requested"
            for stage in snapshot.stage_runs:
                old = c.execute("SELECT status,results FROM polymer_md.stage_runs WHERE job_id=%s AND stage_run_id=%s", (job["job_id"], stage.stage_run_id)).fetchone()
                if old and old["status"] == "completed" and (stage.status != "completed" or old["results"] != safe_scientific_json(stage.results)):
                    raise PolymerMdError("stage_regression", "Completed stage cannot be overwritten", 409)
                c.execute("""INSERT INTO polymer_md.stage_runs(job_id,stage_run_id,attempt_token,stage,attempt,status,quality_status,results,quality,errors,started_at,finished_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s)
                    ON CONFLICT(job_id,stage_run_id) DO UPDATE SET status=excluded.status,quality_status=excluded.quality_status,
                    results=excluded.results,quality=excluded.quality,errors=excluded.errors,started_at=excluded.started_at,finished_at=excluded.finished_at""",
                    (job["job_id"], stage.stage_run_id, snapshot.attempt_token, stage.stage, stage.attempt, stage.status, stage.quality_status,
                     _json(safe_scientific_json(stage.results)), _json(safe_scientific_json(stage.quality)), _json([sanitize_public_json(e) for e in stage.errors]), stage.started_at, stage.finished_at))
            for artifact in snapshot.artifacts:
                old = c.execute("SELECT sha256,size_bytes,stage_run_id,filename,storage_ref FROM polymer_md.artifacts WHERE job_id=%s AND artifact_id=%s", (job["job_id"], artifact.artifact_id)).fetchone()
                if old and any(old[k] != getattr(artifact,k) for k in ("sha256", "size_bytes", "stage_run_id", "filename", "storage_ref")):
                    raise PolymerMdError("artifact_identity_conflict", "Artifact identity changed", 409)
                c.execute("""INSERT INTO polymer_md.artifacts(job_id,artifact_id,stage_run_id,attempt_token,kind,filename,media_type,size_bytes,sha256,storage_ref)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(job_id,artifact_id) DO NOTHING""",
                    (job["job_id"], artifact.artifact_id, artifact.stage_run_id, snapshot.attempt_token, artifact.kind, artifact.filename, artifact.media_type, artifact.size_bytes, artifact.sha256,artifact.storage_ref))
            error = snapshot.errors[-1] if snapshot.errors else {}
            c.execute("""UPDATE polymer_md.jobs SET status=%s,quality_status=%s,stage=%s,results=%s::jsonb,worker_instance_id=%s,snapshot_revision=%s,
                cleanup_confirmed=%s,started_at=COALESCE(started_at,%s),
                finished_at=CASE WHEN %s THEN COALESCE(finished_at,now()) ELSE NULL END,
                error_code=%s,error_message=%s,updated_at=now() WHERE job_id=%s""",
                (target, snapshot.quality_status, snapshot.stage, _json(safe_scientific_json(snapshot.results)), snapshot.worker_instance_id,snapshot.revision,
                 snapshot.cleanup_confirmed, snapshot.started_at, target in TERMINAL_STATUSES, error.get("code"), sanitize_public_text(error.get("message"), fallback="") or None, job["job_id"]))
            c.execute("UPDATE polymer_md.job_attempts SET status=%s,worker_instance_id=%s,snapshot=%s::jsonb,updated_at=now() WHERE job_id=%s AND attempt_token=%s", (target, snapshot.worker_instance_id, _json(snapshot.model_dump(mode="json")), job["job_id"], snapshot.attempt_token))
            current = self._get(c, job["job_id"])
            _record_usage(c, current)
            return current

    def record_error(self, job_id, token, code, message):
        with self.service_connection() as c:
            c.execute("UPDATE polymer_md.jobs SET error_code=%s,error_message=%s,updated_at=now() WHERE job_id=%s AND attempt_token=%s AND status=ANY(%s)", (code, sanitize_public_text(message), job_id, token, list(ACTIVE_STATUSES)))

    def request_deletion(self, job_id, *, owner_user_id, delete_job=False):
        with self.service_connection() as c:
            job = self._get(c, job_id, owner=owner_user_id, lock=True)
            if job["status"] not in TERMINAL_STATUSES or not job["cleanup_confirmed"]:
                raise PolymerMdError("cleanup_not_confirmed", "请先取消任务并等待执行资源回收。")
            c.execute("UPDATE polymer_md.jobs SET artifact_state='delete_requested',delete_job_requested=delete_job_requested OR %s,updated_at=now() WHERE job_id=%s", (delete_job,job_id))
            return self._get(c,job_id)

    def list_deletions(self):
        with self.service_connection() as c:
            rows=c.execute("SELECT job_id FROM polymer_md.jobs WHERE artifact_state='delete_requested' AND cleanup_confirmed AND status=ANY(%s) ORDER BY enqueue_sequence",(list(TERMINAL_STATUSES),)).fetchall()
            return [self._get(c,str(r["job_id"])) for r in rows]

    def mark_expired(self, *, now: datetime | None = None):
        """Persist retention intent; files/checkpoints survive until Worker ACK.

        Only this service method can inject a clock (for deterministic tests).
        Public requests cannot choose expiry timestamps or retention periods.
        """
        if now is not None and now.tzinfo is None:
            raise ValueError("retention clock must include a timezone")
        with self.service_connection() as c:
            rows=c.execute("""WITH eligible AS (
                SELECT j.job_id FROM polymer_md.jobs j
                WHERE j.status=ANY(%s) AND j.cleanup_confirmed AND j.finished_at IS NOT NULL
                AND NOT EXISTS(SELECT FROM polymer_md.job_attempts a WHERE a.job_id=j.job_id AND a.status=ANY(%s))
                AND NOT EXISTS(SELECT FROM polymer_md.stage_runs s WHERE s.job_id=j.job_id AND s.status IN ('pending','running'))
                AND ((j.artifact_state='available' AND j.finished_at<=COALESCE(%s::timestamptz,now())-interval '7 days')
                  OR (NOT j.delete_job_requested AND j.finished_at<=COALESCE(%s::timestamptz,now())-interval '30 days'))
                FOR UPDATE OF j SKIP LOCKED
            ) UPDATE polymer_md.jobs j SET artifact_state='delete_requested',
                delete_job_requested=j.delete_job_requested OR j.finished_at<=COALESCE(%s::timestamptz,now())-interval '30 days',
                updated_at=now() FROM eligible e WHERE j.job_id=e.job_id RETURNING j.job_id""",
                (list(TERMINAL_STATUSES),list(ACTIVE_STATUSES),now,now,now)).fetchall()
            return [str(row['job_id']) for row in rows]

    def complete_deletion(self, job_id, token, *, delete_job=False):
        with self.service_connection() as c:
            try:
                job = self._get(c, job_id, lock=True)
            except PolymerMdError as exc:
                if delete_job and exc.status==404:
                    return
                raise
            if not delete_job and job["attempt_token"]==token and job["artifact_state"]=="deleted":
                return
            if job["attempt_token"] != token or job["status"] not in TERMINAL_STATUSES or not job["cleanup_confirmed"] or job["artifact_state"] != "delete_requested":
                raise PolymerMdError("deletion_conflict", "Cleanup identity changed")
            if delete_job:
                c.execute("DELETE FROM polymer_md.jobs WHERE job_id=%s", (job_id,))
            else:
                c.execute("UPDATE polymer_md.artifacts SET deleted_at=now() WHERE job_id=%s", (job_id,))
                c.execute("UPDATE polymer_md.jobs SET artifact_state='deleted',updated_at=now() WHERE job_id=%s", (job_id,))
