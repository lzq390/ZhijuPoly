from __future__ import annotations

import json
import math
import re
import secrets
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from uuid import uuid4

from app.postgres_database import postgres_connection

from .monomer_dft_models import MAX_ARTIFACT_BYTES, validate_portable_artifact_filename
from .monomer_dft_protocol import PreparedMonomerDftRequest
from .monomer_dft_schema import probe_monomer_dft_schema


ACTIVE_STATUSES = frozenset({"pending", "queued", "running", "cancel_requested"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
ALL_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES
CAPACITY_ADVISORY_LOCK_ID = 742_128_925_057_013
RECONCILER_ADVISORY_LOCK_ID = 742_128_925_057_014
RETENTION_ADVISORY_LOCK_ID = 742_128_925_057_016
_SAFE_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PATH_KEYS = ("path", "directory", "root", "socket", "uds", "python", "traceback")
PUBLIC_TIMING_KEYS = (
    "queue_wait_ms",
    "structure_prepare_ms",
    "gpu_wait_ms",
    "model_load_ms",
    "model_compute_ms",
    "optimization_ms",
    "hessian_ms",
    "frequency_ms",
    "artifact_ms",
    "total_ms",
)
MAX_PAGE = 10_000
WORKER_STAGES = frozenset(
    {
        "queued",
        "validating",
        "conformer",
        "single_point",
        "optimization",
        "hessian",
        "frequency",
        "artifacts",
    }
)


class MonomerDftRepositoryError(RuntimeError):
    pass


class MonomerDftCapacityError(MonomerDftRepositoryError):
    pass


class MonomerDftIdempotencyConflict(MonomerDftRepositoryError):
    pass


class MonomerDftJobNotFound(MonomerDftRepositoryError):
    pass


class MonomerDftJobStateConflict(MonomerDftRepositoryError):
    pass


class MonomerDftStaleAttempt(MonomerDftRepositoryError):
    pass


class MonomerDftArtifactNotFound(MonomerDftRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class CreateJobResult:
    job: dict[str, Any]
    created: bool


@dataclass(frozen=True, slots=True)
class JobPage:
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int


def _jsonb(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    return dict(value)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, list) else []
    return list(value)


def sanitize_public_text(value: Any, *, fallback: str = "operation failed", limit: int = 1_000) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    if not text:
        return fallback
    text = re.sub(r"(?i)(?:file|unix)://[^\s]+", "[redacted]", text)
    text = re.sub(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|/)[^\s'\";,)}\]]+", "[redacted]", text)
    text = re.sub(r"(?<![A-Za-z0-9])\\[^\s'\";,)}\]]+", "[redacted]", text)
    return text[:limit]


def _validated_result_canonical_smiles(value: Any) -> str | None:
    """Return only the already-model-validated, path-safe SMILES representation.

    This is intentionally scoped to ``sanitize_result``.  Generic error/detail
    JSON must never gain a ``canonical_smiles`` escape hatch around path
    redaction merely by choosing that key name.
    """

    if not isinstance(value, dict):
        return None
    raw_input = value.get("input")
    raw = raw_input.get("canonical_smiles") if isinstance(raw_input, dict) else None
    if not isinstance(raw, str) or not 1 <= len(raw) <= 2_048:
        return None
    if not raw.isascii() or any(
        character.isspace() or ord(character) < 0x20 for character in raw
    ):
        return None
    if re.match(r"(?i)^(?:file|unix)://", raw):
        return None
    if re.match(r"^(?:[A-Za-z]:[\\/]|[\\/])", raw):
        return None
    # V2 binds this field to a Backend-prepared request, but legacy V1 results
    # are intentionally still readable and historically only length-checked
    # it.  Require a real single-component molecule before restoring the value
    # removed by generic path redaction.  This rejects relative-path payloads
    # such as ``../secret/model.pt`` while preserving stereo bond markers such
    # as ``F/C=C(\\F)C``.
    try:
        from rdkit import Chem, rdBase

        with rdBase.BlockLogs():
            molecule = Chem.MolFromSmiles(raw)
        if molecule is None or len(Chem.GetMolFrags(molecule)) != 1:
            return None
    except Exception:
        return None
    return raw


def _safe_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 12:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return sanitize_public_text(value, fallback="", limit=10_000)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)[:128]
            lowered = key.lower()
            if lowered != "execution_path" and any(part in lowered for part in _PATH_KEYS):
                continue
            result[key] = _safe_json(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [
            _safe_json(item, depth=depth + 1)
            for item in list(value)[:10_000]
        ]
    return sanitize_public_text(value, fallback="", limit=1_000)


def sanitize_public_json(value: Any) -> dict[str, Any]:
    """Project untrusted Worker details onto path-free JSON safe for persistence."""
    safe = _safe_json(value if isinstance(value, dict) else {})
    return safe if isinstance(safe, dict) else {}


def sanitize_result(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    canonical_smiles = _validated_result_canonical_smiles(value)
    result = _safe_json(value)
    if not isinstance(result, dict):
        return None

    result_input = result.get("input")
    if isinstance(result_input, dict):
        if canonical_smiles is None:
            result_input.pop("canonical_smiles", None)
        else:
            result_input["canonical_smiles"] = canonical_smiles

    provenance = result.get("provenance")
    if isinstance(provenance, dict):
        for internal_alias in (
            "gpu_lease_id",
            "gpu_fencing_token",
            "gpu_broker_instance_id",
        ):
            provenance.pop(internal_alias, None)

    properties = result.get("properties")
    if isinstance(properties, dict):
        hessian = properties.get("hessian")
        if isinstance(hessian, dict):
            for key in list(hessian):
                lowered = key.lower()
                if any(part in lowered for part in ("values", "matrix", "tensor", "eigenvector", "raw")):
                    hessian.pop(key, None)

    optimization = result.get("optimization")
    if isinstance(optimization, dict):
        trace = optimization.get("trace")
        if isinstance(trace, list):
            projected_trace: list[dict[str, int | float]] = []
            for raw_point in trace[:100]:
                if not isinstance(raw_point, dict):
                    continue
                step = raw_point.get("step")
                energy = raw_point.get("energy_eV")
                fmax = raw_point.get("fmax_eV_per_A")
                if (
                    not isinstance(step, int)
                    or isinstance(step, bool)
                    or step < 0
                    or isinstance(energy, bool)
                    or not isinstance(energy, (int, float))
                    or not math.isfinite(float(energy))
                    or isinstance(fmax, bool)
                    or not isinstance(fmax, (int, float))
                    or not math.isfinite(float(fmax))
                    or float(fmax) < 0.0
                ):
                    continue
                projected_trace.append(
                    {
                        "step": step,
                        "energy_eV": float(energy),
                        "fmax_eV_per_A": float(fmax),
                    }
                )
            optimization["trace"] = projected_trace
    return result


def sanitize_timings(value: Any) -> dict[str, float]:
    timings = {key: 0.0 for key in PUBLIC_TIMING_KEYS}
    if not isinstance(value, dict):
        return timings
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if key not in timings:
            continue
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            continue
        number = float(raw_value)
        if math.isfinite(number) and 0.0 <= number <= 1_000_000_000.0:
            timings[key] = number
    return timings


def sanitize_error(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    raw_code = str(value.get("code") or "worker_error")
    code = raw_code if _SAFE_ERROR_CODE.fullmatch(raw_code) else "worker_error"
    details = _safe_json(value.get("details") if isinstance(value.get("details"), dict) else {})
    return {
        "code": code,
        "message": sanitize_public_text(value.get("message"), fallback="DFT worker operation failed"),
        "retryable": value.get("retryable") is True,
        "details": details if isinstance(details, dict) else {},
    }


def normalize_artifacts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_names: set[str] = set()
    for raw in value[:1_000]:
        if not isinstance(raw, dict):
            continue
        artifact_id = str(raw.get("artifact_id") or "")
        sha256 = str(raw.get("sha256") or "").lower()
        name = raw.get("name")
        media_type = str(raw.get("media_type") or "application/octet-stream")
        size_bytes = raw.get("size_bytes")
        try:
            safe_name = validate_portable_artifact_filename(name)
        except ValueError:
            continue
        folded_name = safe_name.casefold()
        if (
            artifact_id in seen
            or folded_name in seen_names
            or _SAFE_ARTIFACT_ID.fullmatch(artifact_id) is None
            or _SAFE_SHA256.fullmatch(sha256) is None
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
            or size_bytes > MAX_ARTIFACT_BYTES
            or not media_type
            or len(media_type) > 255
            or "\n" in media_type
            or "\r" in media_type
        ):
            continue
        seen.add(artifact_id)
        seen_names.add(folded_name)
        metadata = _safe_json(raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {})
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "name": safe_name,
                "media_type": media_type,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "metadata": metadata if isinstance(metadata, dict) else {},
            }
        )
    return artifacts


class MonomerDftRepository:
    def __init__(
        self,
        dsn: str,
        *,
        connection_factory: Callable[[str], AbstractContextManager[Any]] = postgres_connection,
    ) -> None:
        self._dsn = dsn
        self._connection_factory = connection_factory

    def schema_ready(self) -> bool:
        """Return true only for the exact governed DFT schema.

        This probe deliberately avoids touching any ``monomer_dft`` relation
        until PostgreSQL has proved both the relation inventory and the exact
        migration ledger entry.  It is therefore safe during the 0012 -> 0013
        deployment boundary.
        """

        with self._connection_factory(self._dsn) as connection:
            return probe_monomer_dft_schema(connection).ready

    @contextmanager
    def reconciliation_leader(self):
        """Hold a PostgreSQL session advisory lock for one reconciliation cycle."""
        with self._connection_factory(self._dsn) as connection:
            row = connection.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired",
                (RECONCILER_ADVISORY_LOCK_ID,),
            ).fetchone()
            acquired = bool(row and row["acquired"])
            try:
                yield acquired
            finally:
                if acquired:
                    connection.execute(
                        "SELECT pg_advisory_unlock(%s)",
                        (RECONCILER_ADVISORY_LOCK_ID,),
                    )

    @contextmanager
    def retention_leader(self):
        """Hold the DFT retention session lock, isolated from reconciliation."""
        with self._connection_factory(self._dsn) as connection:
            row = connection.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired",
                (RETENTION_ADVISORY_LOCK_ID,),
            ).fetchone()
            acquired = bool(row and row["acquired"])
            try:
                yield acquired
            finally:
                if acquired:
                    connection.execute(
                        "SELECT pg_advisory_unlock(%s)",
                        (RETENTION_ADVISORY_LOCK_ID,),
                    )

    def create_job(
        self,
        prepared: PreparedMonomerDftRequest,
        *,
        idempotency_key: str,
        max_active_jobs: int,
    ) -> CreateJobResult:
        with self._connection_factory(self._dsn) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (CAPACITY_ADVISORY_LOCK_ID,))
            existing = connection.execute(
                "SELECT job_id, request_sha256 FROM monomer_dft.jobs WHERE idempotency_key = %s FOR UPDATE",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["request_sha256"]) != prepared.request_sha256:
                    raise MonomerDftIdempotencyConflict(
                        "Idempotency-Key was already used for a different DFT request"
                    )
                job = self._get_job(connection, str(existing["job_id"]))
                if job is None:  # pragma: no cover - protected by the selected row
                    raise MonomerDftJobNotFound("DFT job not found")
                return CreateJobResult(job=job, created=False)

            count_row = connection.execute(
                "SELECT count(*) AS count FROM monomer_dft.jobs WHERE status = ANY(%s)",
                (list(ACTIVE_STATUSES),),
            ).fetchone()
            active_jobs = int(count_row["count"] if count_row is not None else 0)
            if active_jobs >= max_active_jobs:
                raise MonomerDftCapacityError("monomer DFT job capacity is full")

            job_id = str(uuid4())
            attempt_token = secrets.token_hex(32)
            request = prepared.public_request
            connection.execute(
                """
                INSERT INTO monomer_dft.jobs (
                  job_id, idempotency_key, request_sha256, request_json, request_warnings,
                  calculation_type, model_name, input_smiles, canonical_smiles,
                  effective_charge, multiplicity, status, current_attempt, attempt_token
                ) VALUES (
                  %s::uuid, %s, %s, %s::jsonb, %s::jsonb,
                  %s, %s, %s, %s, %s, %s, 'pending', 1, %s
                )
                """,
                (
                    job_id,
                    idempotency_key,
                    prepared.request_sha256,
                    _jsonb(request),
                    _jsonb(list(prepared.warnings)),
                    request["calculation_type"],
                    request["model"],
                    request["input"]["smiles"],
                    prepared.canonical_smiles,
                    prepared.effective_charge,
                    request["input"]["multiplicity"],
                    attempt_token,
                ),
            )
            connection.execute(
                """
                INSERT INTO monomer_dft.job_attempts (
                  job_id, attempt, attempt_token, request_sha256, status
                ) VALUES (%s::uuid, 1, %s, %s, 'pending')
                """,
                (job_id, attempt_token, prepared.request_sha256),
            )
            job = self._get_job(connection, job_id)
            if job is None:  # pragma: no cover - insert and select share one transaction
                raise MonomerDftJobNotFound("DFT job not found")
            return CreateJobResult(job=job, created=True)

    def count_active_jobs(self) -> int:
        with self._connection_factory(self._dsn) as connection:
            row = connection.execute(
                "SELECT count(*) AS count FROM monomer_dft.jobs WHERE status = ANY(%s)",
                (list(ACTIVE_STATUSES),),
            ).fetchone()
            return int(row["count"] if row is not None else 0)

    def find_idempotent_job(
        self,
        *,
        idempotency_key: str,
        request_sha256: str,
    ) -> dict[str, Any] | None:
        with self._connection_factory(self._dsn) as connection:
            row = connection.execute(
                "SELECT job_id, request_sha256 FROM monomer_dft.jobs WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                return None
            if str(row["request_sha256"]) != request_sha256:
                raise MonomerDftIdempotencyConflict(
                    "Idempotency-Key was already used for a different DFT request"
                )
            return self._get_job(connection, str(row["job_id"]))

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connection_factory(self._dsn) as connection:
            return self._get_job(connection, job_id)

    def list_expired_jobs(
        self,
        *,
        retention_days: int,
        limit: int,
        after_terminal_at: datetime | None = None,
        after_job_id: str | None = None,
    ) -> list[dict[str, Any]]:
        cursor_sql = ""
        params: list[Any] = [
            list(TERMINAL_STATUSES),
            retention_days,
            retention_days,
        ]
        if after_terminal_at is not None and after_job_id is not None:
            cursor_sql = """
              AND (COALESCE(finished_at, updated_at), job_id) >
                  (%s, %s::uuid)
            """
            params.extend((after_terminal_at, after_job_id))
        params.append(limit)
        with self._connection_factory(self._dsn) as connection:
            rows = connection.execute(
                f"""
                {self._job_select_sql()}
                WHERE status = ANY(%s)
                  AND created_at <= now() - (%s * interval '1 day')
                  AND COALESCE(finished_at, updated_at) <=
                      now() - (%s * interval '1 day')
                  {cursor_sql}
                ORDER BY COALESCE(finished_at, updated_at), job_id
                LIMIT %s
                """,
                tuple(params),
            ).fetchall()
            return [self._row_to_job(row, []) for row in rows]

    def delete_job_cas(self, expected: dict[str, Any]) -> bool:
        with self._connection_factory(self._dsn) as connection:
            row = connection.execute(
                """
                DELETE FROM monomer_dft.jobs
                WHERE job_id = %s::uuid
                  AND status = %s
                  AND status = ANY(%s)
                  AND attempt_token = %s
                  AND request_sha256 = %s
                  AND enqueue_sequence = %s
                  AND finished_at IS NOT DISTINCT FROM %s
                  AND updated_at IS NOT DISTINCT FROM %s
                RETURNING job_id
                """,
                (
                    expected["job_id"],
                    expected["status"],
                    list(TERMINAL_STATUSES),
                    expected["_attempt_token"],
                    expected["request_sha256"],
                    expected["_enqueue_sequence"],
                    expected["finished_at"],
                    expected["updated_at"],
                ),
            ).fetchone()
            return row is not None

    def list_jobs(
        self,
        *,
        page: int,
        page_size: int,
        status: str | None = None,
        calculation_type: str | None = None,
    ) -> JobPage:
        if not 1 <= page <= MAX_PAGE:
            raise ValueError(f"page must be between 1 and {MAX_PAGE}")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if calculation_type is not None:
            clauses.append("calculation_type = %s")
            params.append(calculation_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        offset = (page - 1) * page_size
        with self._connection_factory(self._dsn) as connection:
            total_row = connection.execute(
                f"SELECT count(*) AS count FROM monomer_dft.jobs {where}",
                tuple(params),
            ).fetchone()
            total = int(total_row["count"] if total_row is not None else 0)
            if offset >= total:
                rows = []
            else:
                rows = connection.execute(
                    f"""
                    {self._job_select_sql()}
                    {where}
                    ORDER BY created_at DESC, job_id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (*params, page_size, offset),
                ).fetchall()
            job_ids = [str(row["job_id"]) for row in rows]
            artifacts = self._artifacts_for_jobs(connection, job_ids)
            items = [self._row_to_job(row, artifacts.get(str(row["job_id"]), [])) for row in rows]
        return JobPage(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
        )

    def list_reconcilable_jobs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection_factory(self._dsn) as connection:
            rows = connection.execute(
                f"""
                {self._job_select_sql()}
                WHERE status = ANY(%s)
                ORDER BY enqueue_sequence ASC
                LIMIT %s
                """,
                (list(ACTIVE_STATUSES), limit),
            ).fetchall()
            return [self._row_to_job(row, []) for row in rows]

    def claim_pending_dispatch(self, *, job_id: str, attempt_token: str) -> bool:
        """Fence the first outbound submit before leaving PostgreSQL.

        ``submitted_at`` is deliberately written before the UDS request.  A
        concurrent cancellation may complete locally only while this durable
        marker is still absent; once it is present the Worker might have
        accepted the request even if the Backend never received a response.
        Repeated claims for the same pending attempt are allowed because the
        Worker submit protocol is independently idempotent.
        """

        with self._connection_factory(self._dsn) as connection:
            row = connection.execute(
                """
                SELECT status, attempt_token
                FROM monomer_dft.jobs
                WHERE job_id = %s::uuid
                FOR UPDATE
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise MonomerDftJobNotFound("DFT job not found")
            if str(row["attempt_token"]) != attempt_token:
                raise MonomerDftStaleAttempt("DFT attempt token is stale")
            if str(row["status"]) != "pending":
                return False
            connection.execute(
                """
                UPDATE monomer_dft.jobs
                SET submitted_at = COALESCE(submitted_at, now()), updated_at = now()
                WHERE job_id = %s::uuid AND attempt_token = %s AND status = 'pending'
                """,
                (job_id, attempt_token),
            )
            connection.execute(
                """
                UPDATE monomer_dft.job_attempts
                SET submitted_at = COALESCE(submitted_at, now())
                WHERE job_id = %s::uuid AND attempt_token = %s AND status = 'pending'
                """,
                (job_id, attempt_token),
            )
            return True

    def record_dispatch_error(
        self,
        *,
        job_id: str,
        attempt_token: str,
        code: str,
        message: str,
        retryable: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        safe_code = code if _SAFE_ERROR_CODE.fullmatch(code) else "worker_unavailable"
        safe_message = sanitize_public_text(message, fallback="DFT worker is unavailable")
        safe_details = sanitize_public_json(details)
        with self._connection_factory(self._dsn) as connection:
            connection.execute(
                """
                UPDATE monomer_dft.jobs
                SET status = CASE WHEN %s THEN status ELSE 'failed' END,
                    error_code = %s, error_message = %s, error_retryable = %s,
                    error_details = %s::jsonb,
                    finished_at = CASE WHEN %s THEN finished_at ELSE COALESCE(finished_at, now()) END,
                    updated_at = now()
                WHERE job_id = %s::uuid AND attempt_token = %s AND status = ANY(%s)
                """,
                (
                    retryable,
                    safe_code,
                    safe_message,
                    retryable,
                    _jsonb(safe_details),
                    retryable,
                    job_id,
                    attempt_token,
                    list(ACTIVE_STATUSES),
                ),
            )
            if not retryable:
                connection.execute(
                    """
                    UPDATE monomer_dft.job_attempts
                    SET status = 'failed', error_code = %s, error_message = %s,
                        outcome = %s::jsonb, heartbeat_at = now(), lease_expires_at = now(),
                        finished_at = COALESCE(finished_at, now())
                    WHERE job_id = %s::uuid AND attempt_token = %s
                      AND status = ANY(%s)
                    """,
                    (
                        safe_code,
                        safe_message,
                        _jsonb(
                            {
                                "status": "failed",
                                "retryable": False,
                                "error_details": safe_details,
                            }
                        ),
                        job_id,
                        attempt_token,
                        list(ACTIVE_STATUSES),
                    ),
                )

    def apply_worker_snapshot(
        self,
        *,
        job_id: str,
        attempt_token: str,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(snapshot.get("job_id"), str) or snapshot["job_id"] != job_id:
            raise MonomerDftStaleAttempt("worker snapshot job id does not match")
        if not isinstance(snapshot.get("attempt_token"), str) or snapshot["attempt_token"] != attempt_token:
            raise MonomerDftStaleAttempt("worker snapshot attempt token does not match")

        incoming_status = str(snapshot.get("status") or "")
        if incoming_status not in ALL_STATUSES:
            raise MonomerDftRepositoryError("worker returned an unsupported job status")
        worker_error = sanitize_error(snapshot.get("error"))
        timings = sanitize_timings(snapshot.get("timings"))
        result = sanitize_result(snapshot.get("result"))
        if incoming_status == "completed" and (result is None or worker_error is not None):
            raise MonomerDftRepositoryError("completed worker snapshot is missing a valid result")
        if incoming_status == "failed" and (result is not None or worker_error is None):
            raise MonomerDftRepositoryError("failed worker snapshot is missing a structured error")
        if incoming_status == "cancelled" and (result is not None or worker_error is not None):
            raise MonomerDftRepositoryError("cancelled worker snapshot contains a terminal payload")
        if incoming_status in ACTIVE_STATUSES and (result is not None or worker_error is not None):
            raise MonomerDftRepositoryError("active worker snapshot contains a terminal payload")
        provenance = _safe_json(result.get("provenance", {}) if result else {})
        artifacts = normalize_artifacts(snapshot.get("artifacts"))

        with self._connection_factory(self._dsn) as connection:
            current = connection.execute(
                """
                SELECT status, current_attempt, attempt_token, request_sha256,
                       enqueue_sequence, created_at,
                       artifacts_delete_requested_at, artifacts_deleted_at
                FROM monomer_dft.jobs
                WHERE job_id = %s::uuid
                FOR UPDATE
                """,
                (job_id,),
            ).fetchone()
            if current is None:
                raise MonomerDftJobNotFound("DFT job not found")
            if str(current["attempt_token"]) != attempt_token:
                raise MonomerDftStaleAttempt("worker snapshot belongs to a stale job attempt")
            snapshot_sha = snapshot.get("request_sha256")
            if not isinstance(snapshot_sha, str) or snapshot_sha != str(current["request_sha256"]):
                raise MonomerDftStaleAttempt("worker snapshot request digest does not match")
            snapshot_sequence = snapshot.get("enqueue_sequence")
            if (
                not isinstance(snapshot_sequence, int)
                or isinstance(snapshot_sequence, bool)
                or snapshot_sequence != int(current["enqueue_sequence"])
            ):
                raise MonomerDftStaleAttempt("worker snapshot enqueue sequence does not match")
            current_status = str(current["status"])
            if current_status in TERMINAL_STATUSES:
                job = self._get_job(connection, job_id)
                if job is None:  # pragma: no cover
                    raise MonomerDftJobNotFound("DFT job not found")
                return job

            status = self._non_regressing_status(current_status, incoming_status)
            queue_position = snapshot.get("queue_position")
            if not isinstance(queue_position, int) or isinstance(queue_position, bool) or queue_position < 1:
                queue_position = None
            progress = snapshot.get("progress_percent")
            if isinstance(progress, bool) or not isinstance(progress, (int, float)) or not math.isfinite(float(progress)):
                progress = 0.0
            progress = max(0.0, min(100.0, float(progress)))
            stage = str(snapshot.get("stage") or "")
            if stage not in WORKER_STAGES:
                raise MonomerDftRepositoryError("worker returned an unsupported job stage")
            scientific_status = None
            if result and isinstance(result.get("scientific_status"), dict):
                raw_assessment = result["scientific_status"].get("minimum_assessment")
                scientific_status = sanitize_public_text(raw_assessment, fallback="", limit=128) or None
            error_code = worker_error["code"] if worker_error else None
            error_message = worker_error["message"] if worker_error else None
            error_retryable = bool(worker_error and worker_error["retryable"])
            error_details = worker_error["details"] if worker_error else {}
            worker_id = self._optional_identifier(snapshot.get("worker_id"))
            worker_instance_id = self._optional_identifier(snapshot.get("worker_instance_id"))
            worker_job_id = self._optional_identifier(snapshot.get("worker_job_id")) or job_id
            canonical_smiles, effective_charge = self._result_identity(result)

            updated = connection.execute(
                """
                UPDATE monomer_dft.jobs
                SET status = %s, worker_job_id = %s, worker_id = COALESCE(%s, worker_id),
                    worker_instance_id = COALESCE(%s, worker_instance_id), queue_position = %s,
                    stage = %s, progress_percent = %s, scientific_status = %s,
                    canonical_smiles = COALESCE(%s, canonical_smiles),
                    effective_charge = COALESCE(%s, effective_charge),
                    result_json = COALESCE(%s::jsonb, result_json), timings = %s::jsonb,
                    provenance = %s::jsonb, error_code = %s, error_message = %s,
                    error_retryable = %s, error_details = %s::jsonb,
                    submitted_at = CASE WHEN %s IN ('queued','running','cancel_requested','completed','failed','cancelled')
                                        THEN COALESCE(submitted_at, now()) ELSE submitted_at END,
                    started_at = CASE WHEN %s IN ('running','completed','failed','cancelled')
                                     THEN COALESCE(started_at, now()) ELSE started_at END,
                    finished_at = CASE WHEN %s IN ('completed','failed','cancelled')
                                      THEN COALESCE(finished_at, now()) ELSE finished_at END,
                    last_reconciled_at = now(), updated_at = now()
                WHERE job_id = %s::uuid AND attempt_token = %s AND current_attempt = %s
                  AND status = ANY(%s)
                RETURNING job_id
                """,
                (
                    status,
                    worker_job_id,
                    worker_id,
                    worker_instance_id,
                    queue_position,
                    stage,
                    progress,
                    scientific_status,
                    canonical_smiles,
                    effective_charge,
                    _jsonb(result) if result is not None else None,
                    _jsonb(timings),
                    _jsonb(provenance if isinstance(provenance, dict) else {}),
                    error_code,
                    error_message,
                    error_retryable,
                    _jsonb(error_details),
                    status,
                    status,
                    status,
                    job_id,
                    attempt_token,
                    int(current["current_attempt"]),
                    list(ACTIVE_STATUSES),
                ),
            ).fetchone()
            if updated is None:
                raise MonomerDftStaleAttempt("worker snapshot lost the attempt fence")

            connection.execute(
                """
                UPDATE monomer_dft.job_attempts
                SET status = %s, worker_job_id = %s, worker_id = COALESCE(%s, worker_id),
                    worker_instance_id = COALESCE(%s, worker_instance_id),
                    heartbeat_at = now(),
                    lease_expires_at = CASE WHEN %s IN ('pending','queued','running','cancel_requested')
                                            THEN now() + interval '30 seconds' ELSE now() END,
                    outcome = %s::jsonb,
                    error_code = %s, error_message = %s,
                    submitted_at = CASE WHEN %s <> 'pending' THEN COALESCE(submitted_at, now()) ELSE submitted_at END,
                    started_at = CASE WHEN %s IN ('running','completed','failed','cancelled')
                                     THEN COALESCE(started_at, now()) ELSE started_at END,
                    finished_at = CASE WHEN %s IN ('completed','failed','cancelled')
                                      THEN COALESCE(finished_at, now()) ELSE finished_at END
                WHERE job_id = %s::uuid AND attempt = %s AND attempt_token = %s
                """,
                (
                    status,
                    worker_job_id,
                    worker_id,
                    worker_instance_id,
                    status,
                    _jsonb({"status": status, "timings": timings}),
                    error_code,
                    error_message,
                    status,
                    status,
                    status,
                    job_id,
                    int(current["current_attempt"]),
                    attempt_token,
                ),
            )
            if (
                current["artifacts_delete_requested_at"] is None
                and current["artifacts_deleted_at"] is None
            ):
                self._upsert_artifacts(connection, job_id, artifacts)
            job = self._get_job(connection, job_id)
            if job is None:  # pragma: no cover
                raise MonomerDftJobNotFound("DFT job not found")
            return job

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        with self._connection_factory(self._dsn) as connection:
            row = connection.execute(
                """
                SELECT status, submitted_at
                FROM monomer_dft.jobs
                WHERE job_id = %s::uuid
                FOR UPDATE
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise MonomerDftJobNotFound("DFT job not found")
            status = str(row["status"])
            if status == "pending" and row["submitted_at"] is None:
                # The dispatch claim and this row lock are the proof that no
                # Worker can know this attempt.  This is the only safe local
                # cancellation path.
                connection.execute(
                    """
                    UPDATE monomer_dft.jobs
                    SET status = 'cancelled',
                        cancel_requested_at = COALESCE(cancel_requested_at, now()),
                        finished_at = COALESCE(finished_at, now()),
                        updated_at = now()
                    WHERE job_id = %s::uuid AND status = 'pending' AND submitted_at IS NULL
                    """,
                    (job_id,),
                )
                connection.execute(
                    """
                    UPDATE monomer_dft.job_attempts
                    SET status = 'cancelled', finished_at = COALESCE(finished_at, now())
                    WHERE job_id = %s::uuid
                      AND attempt = (SELECT current_attempt FROM monomer_dft.jobs WHERE job_id = %s::uuid)
                      AND status = 'pending'
                    """,
                    (job_id, job_id),
                )
            elif status not in TERMINAL_STATUSES:
                connection.execute(
                    """
                    UPDATE monomer_dft.jobs
                    SET status = 'cancel_requested',
                        cancel_requested_at = COALESCE(cancel_requested_at, now()), updated_at = now()
                    WHERE job_id = %s::uuid AND status = ANY(%s)
                    """,
                    (job_id, list(ACTIVE_STATUSES)),
                )
                connection.execute(
                    """
                    UPDATE monomer_dft.job_attempts
                    SET status = 'cancel_requested'
                    WHERE job_id = %s::uuid
                      AND attempt = (SELECT current_attempt FROM monomer_dft.jobs WHERE job_id = %s::uuid)
                    """,
                    (job_id, job_id),
                )
            job = self._get_job(connection, job_id)
            if job is None:  # pragma: no cover
                raise MonomerDftJobNotFound("DFT job not found")
            return job

    def get_artifact(self, *, job_id: str, artifact_id: str) -> dict[str, Any]:
        with self._connection_factory(self._dsn) as connection:
            row = connection.execute(
                """
                SELECT a.artifact_id, a.name, a.media_type, a.size_bytes,
                       a.sha256, a.metadata, a.available
                FROM monomer_dft.artifacts a
                JOIN monomer_dft.jobs j ON j.job_id = a.job_id
                WHERE a.job_id = %s::uuid AND a.artifact_id = %s
                  AND a.available = true
                  AND j.artifacts_delete_requested_at IS NULL
                  AND j.artifacts_deleted_at IS NULL
                """,
                (job_id, artifact_id),
            ).fetchone()
            if row is None:
                raise MonomerDftArtifactNotFound("DFT artifact not found")
            return self._artifact_row(row)

    def mark_artifacts_deleted(self, job_id: str) -> dict[str, Any]:
        with self._connection_factory(self._dsn) as connection:
            row = connection.execute(
                """
                SELECT status, artifacts_delete_requested_at, artifacts_deleted_at
                FROM monomer_dft.jobs
                WHERE job_id = %s::uuid
                FOR UPDATE
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise MonomerDftJobNotFound("DFT job not found")
            if str(row["status"]) not in TERMINAL_STATUSES:
                raise MonomerDftJobStateConflict("artifacts can be deleted only after a DFT job reaches a terminal state")
            if row["artifacts_delete_requested_at"] is None:
                raise MonomerDftJobStateConflict("artifact deletion has not been requested")
            if row["artifacts_deleted_at"] is None:
                connection.execute(
                    """
                    UPDATE monomer_dft.artifacts
                    SET available = false, deleted_at = COALESCE(deleted_at, now()), updated_at = now()
                    WHERE job_id = %s::uuid AND deleted_at IS NULL
                    """,
                    (job_id,),
                )
                connection.execute(
                    "UPDATE monomer_dft.jobs SET artifacts_deleted_at = now(), updated_at = now() WHERE job_id = %s::uuid",
                    (job_id,),
                )
            job = self._get_job(connection, job_id)
            if job is None:  # pragma: no cover
                raise MonomerDftJobNotFound("DFT job not found")
            return job

    def request_artifact_deletion(self, job_id: str) -> dict[str, Any]:
        with self._connection_factory(self._dsn) as connection:
            row = connection.execute(
                """
                SELECT status, artifacts_delete_requested_at, artifacts_deleted_at
                FROM monomer_dft.jobs
                WHERE job_id = %s::uuid
                FOR UPDATE
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise MonomerDftJobNotFound("DFT job not found")
            if str(row["status"]) not in TERMINAL_STATUSES:
                raise MonomerDftJobStateConflict(
                    "artifacts can be deleted only after a DFT job reaches a terminal state"
                )
            available = connection.execute(
                """
                SELECT EXISTS (
                  SELECT 1 FROM monomer_dft.artifacts
                  WHERE job_id = %s::uuid AND available = true
                ) AS available
                """,
                (job_id,),
            ).fetchone()
            has_available_artifacts = bool(available and available["available"])
            if (
                has_available_artifacts
                and row["artifacts_deleted_at"] is None
                and row["artifacts_delete_requested_at"] is None
            ):
                connection.execute(
                    """
                    UPDATE monomer_dft.jobs
                    SET artifacts_delete_requested_at = now(), updated_at = now()
                    WHERE job_id = %s::uuid
                    """,
                    (job_id,),
                )
                connection.execute(
                    """
                    UPDATE monomer_dft.artifacts
                    SET available = false, updated_at = now()
                    WHERE job_id = %s::uuid AND available = true
                    """,
                    (job_id,),
                )
            job = self._get_job(connection, job_id)
            if job is None:  # pragma: no cover
                raise MonomerDftJobNotFound("DFT job not found")
            return job

    def list_pending_artifact_deletions(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection_factory(self._dsn) as connection:
            rows = connection.execute(
                f"""
                {self._job_select_sql()}
                WHERE status = ANY(%s)
                  AND artifacts_delete_requested_at IS NOT NULL
                  AND artifacts_deleted_at IS NULL
                ORDER BY artifacts_delete_requested_at ASC, job_id ASC
                LIMIT %s
                """,
                (list(TERMINAL_STATUSES), limit),
            ).fetchall()
            return [self._row_to_job(row, []) for row in rows]

    def list_expired_artifact_jobs(self, *, retention_days: int = 30, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection_factory(self._dsn) as connection:
            rows = connection.execute(
                f"""
                {self._job_select_sql()}
                WHERE status = ANY(%s)
                  AND artifacts_delete_requested_at IS NULL
                  AND artifacts_deleted_at IS NULL
                  AND COALESCE(finished_at, updated_at) < now() - (%s * interval '1 day')
                  AND EXISTS (
                    SELECT 1 FROM monomer_dft.artifacts a
                    WHERE a.job_id = monomer_dft.jobs.job_id AND a.available = true
                  )
                ORDER BY COALESCE(finished_at, updated_at) ASC
                LIMIT %s
                """,
                (list(TERMINAL_STATUSES), retention_days, limit),
            ).fetchall()
            return [self._row_to_job(row, []) for row in rows]

    @staticmethod
    def public_job(job: dict[str, Any], *, idempotent_replay: bool = False) -> dict[str, Any]:
        payload = {key: value for key, value in job.items() if not key.startswith("_")}
        payload["idempotent_replay"] = idempotent_replay
        return payload

    @staticmethod
    def _non_regressing_status(current: str, incoming: str) -> str:
        if current == "cancel_requested" and incoming in {"pending", "queued", "running", "cancel_requested"}:
            return "cancel_requested"
        if current == "running" and incoming in {"pending", "queued"}:
            return "running"
        if current == "queued" and incoming == "pending":
            return "queued"
        return incoming

    @staticmethod
    def _optional_identifier(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text or len(text) > 128 or "/" in text or "\\" in text:
            return None
        return text

    @staticmethod
    def _result_identity(result: dict[str, Any] | None) -> tuple[str | None, int | None]:
        if not result or not isinstance(result.get("input"), dict):
            return None, None
        result_input = result["input"]
        canonical = result_input.get("canonical_smiles")
        canonical_smiles = str(canonical)[:512] if isinstance(canonical, str) and canonical else None
        charge = result_input.get("net_charge")
        effective_charge = charge if isinstance(charge, int) and not isinstance(charge, bool) and -5 <= charge <= 5 else None
        return canonical_smiles, effective_charge

    def _get_job(self, connection: Any, job_id: str) -> dict[str, Any] | None:
        row = connection.execute(
            f"{self._job_select_sql()} WHERE job_id = %s::uuid",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        artifacts = self._artifacts_for_jobs(connection, [job_id]).get(job_id, [])
        return self._row_to_job(row, artifacts)

    @staticmethod
    def _job_select_sql() -> str:
        return """
            SELECT job_id, idempotency_key, request_sha256, request_json, request_warnings,
                   enqueue_sequence,
                   calculation_type, model_name, input_smiles, canonical_smiles, effective_charge,
                   multiplicity, status, current_attempt, attempt_token, worker_job_id,
                   worker_id, worker_instance_id, queue_position, stage, progress_percent,
                   scientific_status, result_json, timings, provenance, error_code,
                   error_message, error_retryable, error_details,
                   artifacts_delete_requested_at, artifacts_deleted_at,
                   cancel_requested_at, created_at, updated_at, submitted_at, started_at,
                   finished_at, last_reconciled_at
            FROM monomer_dft.jobs
        """

    def _row_to_job(self, row: Any, artifacts: list[dict[str, Any]]) -> dict[str, Any]:
        request = _as_dict(row["request_json"])
        result = _as_dict(row["result_json"]) or None
        error = None
        if row["error_code"] and row["error_message"]:
            error = {
                "code": str(row["error_code"]),
                "message": str(row["error_message"]),
                "retryable": bool(row["error_retryable"]),
                "details": _as_dict(row["error_details"]),
            }
        raw_timings = _as_dict(row["timings"])
        timings = sanitize_timings(raw_timings)
        created_at = row["created_at"]
        started_at = row["started_at"]
        finished_at = row["finished_at"]
        allow_database_timing_fallback = str(row["status"]) in ACTIVE_STATUSES | {"failed"}
        if (
            allow_database_timing_fallback
            and timings["queue_wait_ms"] <= 0.0
            and isinstance(created_at, datetime)
            and isinstance(started_at, datetime)
        ):
            timings["queue_wait_ms"] = max(0.0, (started_at - created_at).total_seconds() * 1_000.0)
        if (
            allow_database_timing_fallback
            and timings["total_ms"] <= 0.0
            and isinstance(created_at, datetime)
            and isinstance(finished_at, datetime)
        ):
            timings["total_ms"] = max(0.0, (finished_at - created_at).total_seconds() * 1_000.0)
        end_at = finished_at if isinstance(finished_at, datetime) else datetime.now(timezone.utc)
        if isinstance(created_at, datetime):
            timings["end_to_end_ms"] = max(
                0.0,
                (end_at - created_at).total_seconds() * 1_000.0,
            )
        else:  # pragma: no cover - PostgreSQL always returns a timestamp
            timings["end_to_end_ms"] = 0.0
        warnings = [str(item) for item in _as_list(row["request_warnings"]) if isinstance(item, str)]
        if result and isinstance(result.get("scientific_status"), dict):
            for item in result["scientific_status"].get("warnings", []):
                if isinstance(item, str) and item not in warnings:
                    warnings.append(sanitize_public_text(item, fallback="", limit=1_000))
        if result and isinstance(result.get("warnings"), list):
            for item in result["warnings"]:
                if isinstance(item, str):
                    message = item
                elif isinstance(item, dict) and isinstance(item.get("message"), str):
                    message = item["message"]
                else:
                    continue
                safe_message = sanitize_public_text(message, fallback="", limit=1_000)
                if safe_message and safe_message not in warnings:
                    warnings.append(safe_message)
        has_available_artifacts = any(item.get("available") is True for item in artifacts)
        if row["artifacts_deleted_at"] is not None:
            artifacts_state = "deleted"
        elif row["artifacts_delete_requested_at"] is not None:
            artifacts_state = "delete_requested"
        elif has_available_artifacts:
            artifacts_state = "available"
        else:
            artifacts_state = "none"
        return {
            "job_id": str(row["job_id"]),
            "calculation_type": str(row["calculation_type"]),
            "status": str(row["status"]),
            "request": request,
            "request_sha256": str(row["request_sha256"]),
            "attempt": int(row["current_attempt"]),
            "queue_position": int(row["queue_position"]) if row["queue_position"] is not None else None,
            "stage": str(row["stage"]),
            "progress_percent": float(row["progress_percent"]),
            "scientific_status": row["scientific_status"],
            "warnings": warnings,
            "result": result,
            "timings": timings,
            "provenance": _as_dict(row["provenance"]),
            "error": error,
            "artifacts": artifacts,
            "artifacts_state": artifacts_state,
            "artifacts_deleted": artifacts_state == "deleted",
            "cancel_requested": row["cancel_requested_at"] is not None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "_idempotency_key": str(row["idempotency_key"]),
            "_attempt_token": str(row["attempt_token"]),
            "_enqueue_sequence": int(row["enqueue_sequence"]),
            "_worker_job_id": str(row["worker_job_id"] or row["job_id"]),
            "_worker_id": row["worker_id"],
            "_worker_instance_id": row["worker_instance_id"],
            "_dispatch_started": row["submitted_at"] is not None,
        }

    def _artifacts_for_jobs(self, connection: Any, job_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        ids = list(job_ids)
        if not ids:
            return {}
        rows = connection.execute(
            """
            SELECT job_id, artifact_id, name, media_type, size_bytes, sha256, metadata, available
            FROM monomer_dft.artifacts
            WHERE job_id = ANY(%s::uuid[])
            ORDER BY name, artifact_id
            """,
            (ids,),
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row["job_id"]), []).append(self._artifact_row(row))
        return grouped

    @staticmethod
    def _artifact_row(row: Any) -> dict[str, Any]:
        return {
            "artifact_id": str(row["artifact_id"]),
            "name": str(row["name"]),
            "media_type": str(row["media_type"]),
            "size_bytes": int(row["size_bytes"]),
            "sha256": str(row["sha256"]),
            "available": bool(row["available"]),
        }

    @staticmethod
    def _upsert_artifacts(connection: Any, job_id: str, artifacts: list[dict[str, Any]]) -> None:
        for artifact in artifacts:
            connection.execute(
                """
                INSERT INTO monomer_dft.artifacts (
                  job_id, artifact_id, name, relative_location, media_type,
                  size_bytes, sha256, metadata, available
                ) VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s::jsonb, true)
                ON CONFLICT (job_id, artifact_id) DO UPDATE
                SET name = EXCLUDED.name,
                    relative_location = EXCLUDED.relative_location,
                    media_type = EXCLUDED.media_type,
                    size_bytes = EXCLUDED.size_bytes, sha256 = EXCLUDED.sha256,
                    metadata = EXCLUDED.metadata, available = true,
                    deleted_at = NULL, updated_at = now()
                """,
                (
                    job_id,
                    artifact["artifact_id"],
                    artifact["name"],
                    f"artifacts/{artifact['name']}",
                    artifact["media_type"],
                    artifact["size_bytes"],
                    artifact["sha256"],
                    _jsonb(artifact["metadata"]),
                ),
            )
