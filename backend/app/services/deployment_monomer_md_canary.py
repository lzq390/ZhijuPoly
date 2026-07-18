from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from app.postgres_database import postgres_connection
from app.services.monomer_md_repository import (
    create_monomer_md_job_postgres,
    mark_monomer_md_job_submitted_postgres,
)
from app.services.monomer_md_worker_client import (
    MonomerMdWorkerClient,
    MonomerMdWorkerError,
    MonomerMdWorkerSubmission,
    MonomerMdWorkerSubmitPayload,
)


EXPECTED_STEPS = 300
ACTIVE_STATUSES = frozenset({"pending", "submitted", "running"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
OPERATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
OWNER_KEY = "_nexpoly_deployment_canary"
OWNER_PURPOSE = "deployment-monomer-md-canary"
MARKER_SCHEMA_VERSION = 1
RESPONSE_SCHEMA_VERSION = 1
CAPACITY_ADVISORY_LOCK_ID = 742128925057001
MAX_MARKER_BYTES = 64 * 1024
MARKER_PHASES = frozenset(
    {"submit-intent", "validated", "cleanup-intent", "cleaned"}
)
MARKER_FIELDS = frozenset(
    {
        "schema_version",
        "operation_id",
        "source_sha",
        "expected_byteff2_commit",
        "job_id",
        "attempt",
        "capability",
        "capability_sha256",
        "phase",
        "validated",
        "validation_sha256",
        "row_sha256",
        "created_at",
        "updated_at",
    }
)


class DeploymentMonomerMdCanaryError(RuntimeError):
    """The deployment-owned Monomer-MD canary cannot be proven safe."""


class DeploymentMonomerMdCanaryBusy(DeploymentMonomerMdCanaryError):
    """The exact operation-owned Worker job is still active."""


ConnectionFactory = Callable[[str], contextlib.AbstractContextManager[Any]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def validate_operation_identity(
    operation_id: str,
    source_sha: str,
    expected_byteff2_commit: str,
) -> tuple[str, str, str]:
    if OPERATION_ID_RE.fullmatch(operation_id) is None:
        raise DeploymentMonomerMdCanaryError(
            "canary operation ID is not a full deployment operation identity"
        )
    if SHA_RE.fullmatch(source_sha) is None:
        raise DeploymentMonomerMdCanaryError(
            "canary source SHA must be a full lowercase Git SHA"
        )
    if SHA_RE.fullmatch(expected_byteff2_commit) is None:
        raise DeploymentMonomerMdCanaryError(
            "canary ByteFF2 commit must be a full lowercase Git SHA"
        )
    return operation_id, source_sha, expected_byteff2_commit


def derive_canary_job_id(operation_id: str, source_sha: str) -> str:
    operation_id, source_sha, _ = validate_operation_identity(
        operation_id,
        source_sha,
        "0" * 40,
    )
    identity = hashlib.sha256(
        f"{OWNER_PURPOSE}\0{operation_id}\0{source_sha}".encode("ascii")
    ).hexdigest()
    return f"deploy-canary-{identity[:40]}"


def _marker_filename(operation_id: str, source_sha: str) -> str:
    identity = hashlib.sha256(
        f"{operation_id}\0{source_sha}".encode("ascii")
    ).hexdigest()
    return f"{identity}.json"


def _require_private_state_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise DeploymentMonomerMdCanaryError(
            "canary state directory must be absolute"
        )
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DeploymentMonomerMdCanaryError(
            "canary state directory is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise DeploymentMonomerMdCanaryError(
            "canary state directory must be owner-private mode 0700"
        )
    return path


@contextlib.contextmanager
def _operation_lock(state_directory: Path) -> Iterator[None]:
    state_directory = _require_private_state_directory(state_directory)
    lock_path = state_directory / ".lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise DeploymentMonomerMdCanaryError(
            "cannot open the private canary operation lock"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise DeploymentMonomerMdCanaryError(
                "canary operation lock is unsafe"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_marker(path: Path, marker: dict[str, Any]) -> None:
    _validate_marker(
        marker,
        operation_id=marker.get("operation_id"),
        source_sha=marker.get("source_sha"),
        expected_byteff2_commit=marker.get("expected_byteff2_commit"),
    )
    payload = _canonical_json_bytes(marker) + b"\n"
    temporary = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_CLOEXEC
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(temporary, flags, 0o600)
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short write while persisting canary marker")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _validate_marker(
    marker: object,
    *,
    operation_id: object,
    source_sha: object,
    expected_byteff2_commit: object,
) -> dict[str, Any]:
    if not isinstance(marker, dict) or set(marker) != MARKER_FIELDS:
        raise DeploymentMonomerMdCanaryError(
            "canary marker has an invalid shape"
        )
    if (
        marker.get("schema_version") != MARKER_SCHEMA_VERSION
        or marker.get("operation_id") != operation_id
        or marker.get("source_sha") != source_sha
        or marker.get("expected_byteff2_commit") != expected_byteff2_commit
        or marker.get("job_id")
        != derive_canary_job_id(str(operation_id), str(source_sha))
        or not isinstance(marker.get("attempt"), int)
        or isinstance(marker.get("attempt"), bool)
        or marker["attempt"] < 1
        or not isinstance(marker.get("capability"), str)
        or TOKEN_RE.fullmatch(marker["capability"]) is None
        or marker.get("capability_sha256")
        != _sha256_bytes(marker["capability"].encode("ascii"))
        or marker.get("phase") not in MARKER_PHASES
        or not isinstance(marker.get("validated"), bool)
        or not isinstance(marker.get("created_at"), str)
        or not marker["created_at"]
        or not isinstance(marker.get("updated_at"), str)
        or not marker["updated_at"]
    ):
        raise DeploymentMonomerMdCanaryError(
            "canary marker identity is invalid"
        )
    validation_sha256 = marker.get("validation_sha256")
    row_sha256 = marker.get("row_sha256")
    if validation_sha256 is not None and (
        not isinstance(validation_sha256, str)
        or DIGEST_RE.fullmatch(validation_sha256) is None
    ):
        raise DeploymentMonomerMdCanaryError(
            "canary validation digest is invalid"
        )
    if row_sha256 is not None and (
        not isinstance(row_sha256, str)
        or DIGEST_RE.fullmatch(row_sha256) is None
    ):
        raise DeploymentMonomerMdCanaryError(
            "canary row digest is invalid"
        )
    if marker["validated"] != (validation_sha256 is not None):
        raise DeploymentMonomerMdCanaryError(
            "canary marker validation state is incomplete"
        )
    if marker["phase"] == "validated" and marker["validated"] is not True:
        raise DeploymentMonomerMdCanaryError(
            "validated canary marker lacks validation evidence"
        )
    if marker["phase"] == "submit-intent" and marker["validated"] is not False:
        raise DeploymentMonomerMdCanaryError(
            "unsubmitted canary marker unexpectedly contains validation evidence"
        )
    if marker["phase"] in {"submit-intent", "validated"} and row_sha256 is not None:
        raise DeploymentMonomerMdCanaryError(
            "canary marker sealed a cleanup row before cleanup intent"
        )
    return marker


def _load_marker(
    path: Path,
    *,
    operation_id: str,
    source_sha: str,
    expected_byteff2_commit: str,
) -> dict[str, Any]:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DeploymentMonomerMdCanaryError(
            "canary marker is unavailable"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > MAX_MARKER_BYTES
        ):
            raise DeploymentMonomerMdCanaryError(
                "canary marker must be owner-private mode 0600"
            )
        chunks: list[bytes] = []
        bytes_read = 0
        while True:
            chunk = os.read(
                descriptor,
                min(64 * 1024, MAX_MARKER_BYTES + 1 - bytes_read),
            )
            if not chunk:
                break
            chunks.append(chunk)
            bytes_read += len(chunk)
            if bytes_read > MAX_MARKER_BYTES:
                raise DeploymentMonomerMdCanaryError(
                    "canary marker exceeds its size limit"
                )
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    try:
        marker = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentMonomerMdCanaryError(
            "canary marker is invalid JSON"
        ) from exc
    return _validate_marker(
        marker,
        operation_id=operation_id,
        source_sha=source_sha,
        expected_byteff2_commit=expected_byteff2_commit,
    )


def _new_marker(
    operation_id: str,
    source_sha: str,
    expected_byteff2_commit: str,
    *,
    attempt: int = 1,
    created_at: str | None = None,
) -> dict[str, Any]:
    timestamp = _utc_now()
    capability = secrets.token_hex(32)
    return {
        "schema_version": MARKER_SCHEMA_VERSION,
        "operation_id": operation_id,
        "source_sha": source_sha,
        "expected_byteff2_commit": expected_byteff2_commit,
        "job_id": derive_canary_job_id(operation_id, source_sha),
        "attempt": attempt,
        "capability": capability,
        "capability_sha256": _sha256_bytes(capability.encode("ascii")),
        "phase": "submit-intent",
        "validated": False,
        "validation_sha256": None,
        "row_sha256": None,
        "created_at": created_at or timestamp,
        "updated_at": timestamp,
    }


def _marker_path(
    state_directory: Path,
    operation_id: str,
    source_sha: str,
) -> Path:
    return state_directory / _marker_filename(operation_id, source_sha)


def _load_or_create_marker(
    state_directory: Path,
    operation_id: str,
    source_sha: str,
    expected_byteff2_commit: str,
) -> tuple[Path, dict[str, Any]]:
    path = _marker_path(state_directory, operation_id, source_sha)
    if path.exists() or path.is_symlink():
        return path, _load_marker(
            path,
            operation_id=operation_id,
            source_sha=source_sha,
            expected_byteff2_commit=expected_byteff2_commit,
        )
    marker = _new_marker(
        operation_id,
        source_sha,
        expected_byteff2_commit,
    )
    _atomic_write_marker(path, marker)
    return path, marker


def read_canary_marker(
    state_directory: Path,
    operation_id: str,
    source_sha: str,
    expected_byteff2_commit: str,
) -> dict[str, Any]:
    operation_id, source_sha, expected_byteff2_commit = (
        validate_operation_identity(
            operation_id,
            source_sha,
            expected_byteff2_commit,
        )
    )
    state_directory = _require_private_state_directory(state_directory)
    with _operation_lock(state_directory):
        return dict(
            _load_marker(
                _marker_path(state_directory, operation_id, source_sha),
                operation_id=operation_id,
                source_sha=source_sha,
                expected_byteff2_commit=expected_byteff2_commit,
            )
        )


def _owner_document(marker: dict[str, Any]) -> dict[str, Any]:
    return {
        OWNER_KEY: {
            "schema_version": 1,
            "purpose": OWNER_PURPOSE,
            "operation_id": marker["operation_id"],
            "source_sha": marker["source_sha"],
            "capability_sha256": marker["capability_sha256"],
            "attempt": marker["attempt"],
        }
    }


def _require_capability(marker: dict[str, Any], capability: str) -> None:
    if (
        not isinstance(capability, str)
        or TOKEN_RE.fullmatch(capability) is None
        or not hmac.compare_digest(marker["capability"], capability)
        or not hmac.compare_digest(
            marker["capability_sha256"],
            _sha256_bytes(capability.encode("ascii")),
        )
    ):
        raise DeploymentMonomerMdCanaryError(
            "canary continuation capability does not match the operation marker"
        )


def _lock_capacity(connection: Any) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(%s)",
        (CAPACITY_ADVISORY_LOCK_ID,),
    )


def _require_no_owned_sequence(connection: Any) -> None:
    rows = connection.execute(
        """
        SELECT sequence_namespace.nspname AS schema_name,
               sequence_relation.relname AS sequence_name
        FROM pg_class AS table_relation
        JOIN pg_namespace AS table_namespace
          ON table_namespace.oid = table_relation.relnamespace
        JOIN pg_depend AS dependency
          ON dependency.refobjid = table_relation.oid
         AND dependency.deptype IN ('a', 'i')
        JOIN pg_class AS sequence_relation
          ON sequence_relation.oid = dependency.objid
         AND sequence_relation.relkind = 'S'
        JOIN pg_namespace AS sequence_namespace
          ON sequence_namespace.oid = sequence_relation.relnamespace
        WHERE table_namespace.nspname = 'md'
          AND table_relation.relname = 'monomer_md_jobs'
        ORDER BY sequence_namespace.nspname, sequence_relation.relname
        """
    ).fetchall()
    if rows:
        raise DeploymentMonomerMdCanaryError(
            "monomer MD canary table unexpectedly owns a sequence"
        )


def _row_document(
    connection: Any,
    job_id: str,
    *,
    lock: bool,
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if lock else ""
    row = connection.execute(
        """
        SELECT to_jsonb(job_row) AS document
        FROM md.monomer_md_jobs AS job_row
        WHERE job_id = %s
        """
        + suffix,
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    document = row["document"]
    if not isinstance(document, dict):
        raise DeploymentMonomerMdCanaryError(
            "canary database row did not serialize as an object"
        )
    return document


def _validate_owned_row(
    row: object,
    marker: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise DeploymentMonomerMdCanaryError(
            "canary database row is invalid"
        )
    expected_identity = {
        "job_id": marker["job_id"],
        "input_smiles": "CCO",
        "canonical_smiles": "CCO",
        "requested_steps": EXPECTED_STEPS,
        "protocol": "DensityDemo",
        "run_mode": "demo",
        "config_json": _owner_document(marker),
        "components": {},
        "engine": "byteff2-density-demo-worker",
    }
    if any(row.get(name) != value for name, value in expected_identity.items()):
        raise DeploymentMonomerMdCanaryError(
            "deterministic canary ID is occupied by an unowned or changed row"
        )
    status_value = row.get("status")
    if status_value not in ACTIVE_STATUSES | TERMINAL_STATUSES:
        raise DeploymentMonomerMdCanaryError(
            "operation-owned canary has an unknown status"
        )
    worker_job_id = row.get("worker_job_id")
    if worker_job_id not in {None, marker["job_id"]}:
        raise DeploymentMonomerMdCanaryError(
            "operation-owned canary points to another Worker job"
        )
    completed_steps = row.get("completed_steps")
    if (
        not isinstance(completed_steps, int)
        or isinstance(completed_steps, bool)
        or completed_steps < 0
        or completed_steps > EXPECTED_STEPS
    ):
        raise DeploymentMonomerMdCanaryError(
            "operation-owned canary has an invalid completed step count"
        )
    artifact_root = row.get("artifact_root")
    if artifact_root is not None:
        if not isinstance(artifact_root, str):
            raise DeploymentMonomerMdCanaryError(
                "operation-owned canary artifact root is invalid"
            )
        parsed_root = PurePosixPath(artifact_root)
        if (
            not parsed_root.is_absolute()
            or ".." in parsed_root.parts
            or parsed_root.name != marker["job_id"]
        ):
            raise DeploymentMonomerMdCanaryError(
                "operation-owned canary artifact root escaped its job directory"
            )
    byteff2_commit = row.get("byteff2_git_sha")
    if byteff2_commit not in {None, marker["expected_byteff2_commit"]}:
        raise DeploymentMonomerMdCanaryError(
            "operation-owned canary reports another ByteFF2 commit"
        )
    return row


def _validate_completed_row(
    row: dict[str, Any],
    marker: dict[str, Any],
) -> dict[str, Any]:
    row = _validate_owned_row(row, marker)
    result = row.get("result_data")
    summary = result.get("summary") if isinstance(result, dict) else None
    artifacts = row.get("artifacts")
    serialized_artifacts = json.dumps(
        artifacts if isinstance(artifacts, dict) else {},
        ensure_ascii=False,
        sort_keys=True,
    )
    if (
        row.get("status") != "completed"
        or row.get("completed_steps") != EXPECTED_STEPS
        or row.get("progress_percent") != 100
        or row.get("byteff2_git_sha") != marker["expected_byteff2_commit"]
        or not isinstance(result, dict)
        or not isinstance(summary, dict)
        or summary.get("n_steps") != EXPECTED_STEPS
        or result.get("not_equilibrated") is not True
        or result.get("physical_density_estimate") is not False
        or not result.get("warnings")
        or any(
            required not in serialized_artifacts
            for required in ("npt_state.csv", "npt.dcd")
        )
    ):
        raise DeploymentMonomerMdCanaryError(
            "operation-owned canary did not satisfy the reviewed 300-step result"
        )
    return row


def _row_sha256(row: dict[str, Any]) -> str:
    return _sha256_bytes(_canonical_json_bytes(row))


def _active_count(connection: Any) -> int:
    row = connection.execute(
        """
        SELECT count(*) AS count
        FROM md.monomer_md_jobs
        WHERE status IN ('pending', 'submitted', 'running')
        """
    ).fetchone()
    if row is None:
        raise DeploymentMonomerMdCanaryError(
            "cannot read monomer MD active capacity"
        )
    return int(row["count"])


def _create_or_load_owned_row(
    dsn: str,
    marker: dict[str, Any],
    *,
    max_active_jobs: int,
    connection_factory: ConnectionFactory,
) -> dict[str, Any]:
    with connection_factory(dsn) as connection:
        _lock_capacity(connection)
        _require_no_owned_sequence(connection)
        existing = _row_document(connection, marker["job_id"], lock=True)
        if existing is None:
            if _active_count(connection) >= max_active_jobs:
                raise DeploymentMonomerMdCanaryBusy(
                    "another monomer MD job owns the deployment canary capacity"
                )
            create_monomer_md_job_postgres(
                connection,
                job_id=marker["job_id"],
                input_smiles="CCO",
                canonical_smiles="CCO",
                requested_steps=EXPECTED_STEPS,
                protocol="DensityDemo",
                run_mode="demo",
                config_json=_owner_document(marker),
                components={},
            )
            existing = _row_document(connection, marker["job_id"], lock=True)
            if existing is None:
                raise DeploymentMonomerMdCanaryError(
                    "operation-owned canary row did not commit"
                )
        return _validate_owned_row(existing, marker)


def _load_owned_row(
    dsn: str,
    marker: dict[str, Any],
    *,
    lock: bool = False,
    connection_factory: ConnectionFactory,
) -> dict[str, Any] | None:
    with connection_factory(dsn) as connection:
        if lock:
            _lock_capacity(connection)
        _require_no_owned_sequence(connection)
        row = _row_document(connection, marker["job_id"], lock=lock)
        return _validate_owned_row(row, marker) if row is not None else None


def _validate_worker_submission(
    submission: MonomerMdWorkerSubmission,
    marker: dict[str, Any],
) -> MonomerMdWorkerSubmission:
    if submission.worker_job_id != marker["job_id"]:
        raise DeploymentMonomerMdCanaryError(
            "Worker accepted the canary under another job identity"
        )
    return submission


def _mark_submitted(
    dsn: str,
    marker: dict[str, Any],
    submission: MonomerMdWorkerSubmission,
    *,
    connection_factory: ConnectionFactory,
) -> dict[str, Any]:
    with connection_factory(dsn) as connection:
        _lock_capacity(connection)
        current = _row_document(connection, marker["job_id"], lock=True)
        if current is None:
            raise DeploymentMonomerMdCanaryError(
                "canary row disappeared after Worker submission"
            )
        _validate_owned_row(current, marker)
        mark_monomer_md_job_submitted_postgres(
            connection,
            job_id=marker["job_id"],
            worker_id=submission.worker_id,
            worker_job_id=submission.worker_job_id,
            worker_version=submission.worker_version,
        )
        current = _row_document(connection, marker["job_id"], lock=True)
        if current is None:
            raise DeploymentMonomerMdCanaryError(
                "canary row disappeared while recording Worker submission"
            )
        return _validate_owned_row(current, marker)


def _validate_worker_ready(
    health: object,
    *,
    source_sha: str,
) -> dict[str, Any]:
    if (
        not isinstance(health, dict)
        or health.get("status") != "ok"
        or health.get("mode") != "real"
        or health.get("source_sha") != source_sha
        or health.get("accepting_jobs") is not True
        or health.get("draining") is not False
        or health.get("db_configured") is not True
        or health.get("runtime_ready") is not True
        or health.get("active_jobs") != 0
        or health.get("max_active_jobs") != 1
        or health.get("default_steps") != EXPECTED_STEPS
        or health.get("max_steps") != EXPECTED_STEPS
        or not isinstance(health.get("job_root"), str)
    ):
        raise DeploymentMonomerMdCanaryBusy(
            "monomer MD Worker is not ready for the deployment canary"
        )
    return health


def _canary_response(
    marker: dict[str, Any],
    status_value: str,
    *,
    include_capability: bool = False,
) -> dict[str, Any]:
    response = {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "operation_id": marker["operation_id"],
        "job_id": marker["job_id"],
        "status": status_value,
        "validated": marker["validated"],
        "attempt": marker["attempt"],
    }
    if include_capability:
        response["capability"] = marker["capability"]
    return response


def submit_canary(
    *,
    dsn: str,
    state_directory: Path,
    operation_id: str,
    source_sha: str,
    expected_byteff2_commit: str,
    max_active_jobs: int,
    worker_client: MonomerMdWorkerClient,
    capability: str | None = None,
    connection_factory: ConnectionFactory = postgres_connection,
) -> dict[str, Any]:
    operation_id, source_sha, expected_byteff2_commit = (
        validate_operation_identity(
            operation_id,
            source_sha,
            expected_byteff2_commit,
        )
    )
    if (
        not isinstance(max_active_jobs, int)
        or isinstance(max_active_jobs, bool)
        or max_active_jobs != 1
    ):
        raise DeploymentMonomerMdCanaryError(
            "deployment canary requires exact Monomer-MD capacity one"
        )
    state_directory = _require_private_state_directory(state_directory)
    with _operation_lock(state_directory):
        path, marker = _load_or_create_marker(
            state_directory,
            operation_id,
            source_sha,
            expected_byteff2_commit,
        )
        if capability is not None:
            _require_capability(marker, capability)
        if marker["phase"] == "cleanup-intent":
            return _canary_response(
                marker,
                "cleanup-intent",
                include_capability=True,
            )
        if marker["phase"] == "cleaned":
            _prove_database_row_absent(
                dsn=dsn,
                marker=marker,
                connection_factory=connection_factory,
            )
            if marker["validated"]:
                return _canary_response(
                    marker,
                    "cleaned",
                    include_capability=True,
                )
            if capability is None:
                return _canary_response(
                    marker,
                    "cleaned",
                    include_capability=True,
                )
            _prove_cleaned_locked(
                dsn=dsn,
                marker=marker,
                worker_client=worker_client,
                connection_factory=connection_factory,
            )
            marker = _new_marker(
                operation_id,
                source_sha,
                expected_byteff2_commit,
                attempt=marker["attempt"] + 1,
                created_at=marker["created_at"],
            )
            _atomic_write_marker(path, marker)
        row = _create_or_load_owned_row(
            dsn,
            marker,
            max_active_jobs=max_active_jobs,
            connection_factory=connection_factory,
        )
        status_value = str(row["status"])
        if status_value != "pending":
            return _canary_response(
                marker,
                status_value,
                include_capability=True,
            )
        try:
            worker_health = worker_client.get_health()
        except MonomerMdWorkerError as exc:
            raise DeploymentMonomerMdCanaryBusy(
                "monomer MD Worker is unavailable for the deployment canary"
            ) from exc
        _validate_worker_ready(worker_health, source_sha=source_sha)
        try:
            submission = _validate_worker_submission(
                worker_client.submit_job(
                    MonomerMdWorkerSubmitPayload(
                        job_id=marker["job_id"],
                        smiles="CCO",
                        canonical_smiles="CCO",
                        steps=EXPECTED_STEPS,
                        protocol="DensityDemo",
                        run_mode="demo",
                        config_json=_owner_document(marker),
                    )
                ),
                marker,
            )
        except MonomerMdWorkerError as exc:
            reconciled = _load_owned_row(
                dsn,
                marker,
                connection_factory=connection_factory,
            )
            if reconciled is not None and reconciled.get("status") != "pending":
                return _canary_response(
                    marker,
                    str(reconciled["status"]),
                    include_capability=True,
                )
            raise DeploymentMonomerMdCanaryBusy(
                "Worker submission did not commit an operation-owned canary"
            ) from exc
        row = _mark_submitted(
            dsn,
            marker,
            submission,
            connection_factory=connection_factory,
        )
        return _canary_response(
            marker,
            str(row["status"]),
            include_capability=True,
        )


def validate_completed_canary(
    *,
    dsn: str,
    state_directory: Path,
    operation_id: str,
    source_sha: str,
    expected_byteff2_commit: str,
    capability: str,
    connection_factory: ConnectionFactory = postgres_connection,
) -> dict[str, Any]:
    operation_id, source_sha, expected_byteff2_commit = (
        validate_operation_identity(
            operation_id,
            source_sha,
            expected_byteff2_commit,
        )
    )
    state_directory = _require_private_state_directory(state_directory)
    with _operation_lock(state_directory):
        path = _marker_path(state_directory, operation_id, source_sha)
        marker = _load_marker(
            path,
            operation_id=operation_id,
            source_sha=source_sha,
            expected_byteff2_commit=expected_byteff2_commit,
        )
        _require_capability(marker, capability)
        if marker["phase"] in {"cleanup-intent", "cleaned"}:
            if marker["validated"]:
                return _canary_response(marker, marker["phase"])
            raise DeploymentMonomerMdCanaryError(
                "cleaned canary has no successful validation evidence"
            )
        with connection_factory(dsn) as connection:
            _lock_capacity(connection)
            _require_no_owned_sequence(connection)
            row = _row_document(connection, marker["job_id"], lock=True)
            if row is None:
                raise DeploymentMonomerMdCanaryError(
                    "canary row is absent before validation"
                )
            row = _validate_completed_row(row, marker)
            validation_sha256 = _row_sha256(row)
        if marker["validated"] and marker["validation_sha256"] != validation_sha256:
            raise DeploymentMonomerMdCanaryError(
                "validated canary row changed before cleanup"
            )
        marker = {
            **marker,
            "phase": "validated",
            "validated": True,
            "validation_sha256": validation_sha256,
            "updated_at": _utc_now(),
        }
        _atomic_write_marker(path, marker)
        return _canary_response(marker, "validated")


def _worker_artifact_root(
    worker_client: MonomerMdWorkerClient,
    marker: dict[str, Any],
) -> str:
    try:
        health = worker_client.get_health()
    except MonomerMdWorkerError as exc:
        raise DeploymentMonomerMdCanaryBusy(
            "cannot prove the canary Worker artifact root"
        ) from exc
    root_value = health.get("job_root") if isinstance(health, dict) else None
    if (
        not isinstance(health, dict)
        or health.get("mode") != "real"
        or health.get("source_sha") != marker["source_sha"]
        or not isinstance(root_value, str)
    ):
        raise DeploymentMonomerMdCanaryError(
            "Worker identity or artifact root differs from the canary operation"
        )
    root = PurePosixPath(root_value)
    if not root.is_absolute() or ".." in root.parts:
        raise DeploymentMonomerMdCanaryError(
            "Worker reported an unsafe artifact root"
        )
    return str(root / marker["job_id"])


def _delete_worker_artifacts_once(
    worker_client: MonomerMdWorkerClient,
    marker: dict[str, Any],
    *,
    expected_root: str,
) -> dict[str, Any]:
    try:
        response = worker_client.delete_artifacts(marker["job_id"])
    except MonomerMdWorkerError as exc:
        if "active" in str(exc).lower():
            raise DeploymentMonomerMdCanaryBusy(
                "operation-owned canary is still active on the Worker"
            ) from exc
        raise DeploymentMonomerMdCanaryBusy(
            "cannot clean the operation-owned Worker artifacts"
        ) from exc
    if (
        not isinstance(response, dict)
        or response.get("job_id") != marker["job_id"]
        or response.get("artifact_root") != expected_root
        or not isinstance(response.get("deleted"), bool)
        or not isinstance(response.get("message"), str)
        or not response["message"]
    ):
        raise DeploymentMonomerMdCanaryError(
            "Worker artifact cleanup returned mismatched evidence"
        )
    return response


def _prove_worker_artifacts_absent(
    worker_client: MonomerMdWorkerClient,
    marker: dict[str, Any],
    *,
    database_row: dict[str, Any] | None = None,
) -> None:
    expected_root = _worker_artifact_root(worker_client, marker)
    if (
        database_row is not None
        and database_row.get("artifact_root") is not None
        and database_row["artifact_root"] != expected_root
    ):
        raise DeploymentMonomerMdCanaryError(
            "canary database row points outside the exact Worker job root"
        )
    first = _delete_worker_artifacts_once(
        worker_client,
        marker,
        expected_root=expected_root,
    )
    second = _delete_worker_artifacts_once(
        worker_client,
        marker,
        expected_root=expected_root,
    )
    if second["deleted"] is not False:
        raise DeploymentMonomerMdCanaryError(
            "Worker artifact cleanup did not converge to an absent directory"
        )
    if first["deleted"] not in {True, False}:  # pragma: no cover - bool checked above
        raise DeploymentMonomerMdCanaryError(
            "Worker artifact cleanup evidence is invalid"
        )


def _preflight_cleanup_row(
    dsn: str,
    marker: dict[str, Any],
    *,
    connection_factory: ConnectionFactory,
) -> dict[str, Any] | None:
    with connection_factory(dsn) as connection:
        _lock_capacity(connection)
        _require_no_owned_sequence(connection)
        row = _row_document(connection, marker["job_id"], lock=True)
        if row is None:
            if marker["phase"] == "validated":
                raise DeploymentMonomerMdCanaryError(
                    "validated canary row disappeared before cleanup intent"
                )
            return None
        if marker["phase"] == "cleaned":
            raise DeploymentMonomerMdCanaryError(
                "a cleaned canary operation unexpectedly regained a database row"
            )
        row = _validate_owned_row(row, marker)
        row_sha256 = _row_sha256(row)
        if (
            marker["phase"] == "validated"
            and marker["validation_sha256"] != row_sha256
        ):
            raise DeploymentMonomerMdCanaryError(
                "validated canary row changed before cleanup"
            )
        if (
            marker["phase"] == "cleanup-intent"
            and marker["row_sha256"] != row_sha256
        ):
            raise DeploymentMonomerMdCanaryError(
                "canary row changed after durable cleanup intent"
            )
        return row


def _delete_owned_row_with_intent(
    *,
    dsn: str,
    marker_path: Path,
    marker: dict[str, Any],
    connection_factory: ConnectionFactory,
) -> dict[str, Any]:
    with connection_factory(dsn) as connection:
        _lock_capacity(connection)
        _require_no_owned_sequence(connection)
        row = _row_document(connection, marker["job_id"], lock=True)
        row_sha256: str | None = None
        if row is not None:
            row = _validate_owned_row(row, marker)
            row_sha256 = _row_sha256(row)
        if (
            marker["phase"] == "validated"
            and marker["validation_sha256"] != row_sha256
        ):
            raise DeploymentMonomerMdCanaryError(
                "validated canary row changed before durable cleanup intent"
            )
        if marker["phase"] == "cleanup-intent":
            if marker["row_sha256"] != row_sha256 and row is not None:
                raise DeploymentMonomerMdCanaryError(
                    "canary row changed after durable cleanup intent"
                )
        else:
            marker = {
                **marker,
                "phase": "cleanup-intent",
                "row_sha256": row_sha256,
                "updated_at": _utc_now(),
            }
            _atomic_write_marker(marker_path, marker)
        if row is not None:
            cursor = connection.execute(
                """
                DELETE FROM md.monomer_md_jobs
                WHERE job_id = %s
                  AND config_json = %s::jsonb
                """,
                (
                    marker["job_id"],
                    json.dumps(
                        _owner_document(marker),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
            if int(cursor.rowcount or 0) != 1:
                raise DeploymentMonomerMdCanaryError(
                    "canary row CAS deletion did not remove exactly one owned row"
                )
        _require_no_owned_sequence(connection)
    return marker


def _prove_database_row_absent(
    dsn: str,
    marker: dict[str, Any],
    *,
    connection_factory: ConnectionFactory,
) -> None:
    with connection_factory(dsn) as connection:
        _lock_capacity(connection)
        _require_no_owned_sequence(connection)
        if _row_document(connection, marker["job_id"], lock=True) is not None:
            raise DeploymentMonomerMdCanaryError(
                "canary database row remains after cleanup"
            )


def _prove_cleaned_locked(
    *,
    dsn: str,
    marker: dict[str, Any],
    worker_client: MonomerMdWorkerClient,
    connection_factory: ConnectionFactory,
) -> None:
    row = _preflight_cleanup_row(
        dsn,
        marker,
        connection_factory=connection_factory,
    )
    _prove_worker_artifacts_absent(
        worker_client,
        marker,
        database_row=row,
    )
    _prove_database_row_absent(
        dsn,
        marker,
        connection_factory=connection_factory,
    )


def _cleanup_locked(
    *,
    dsn: str,
    marker_path: Path,
    marker: dict[str, Any],
    worker_client: MonomerMdWorkerClient,
    connection_factory: ConnectionFactory,
) -> dict[str, Any]:
    row = _preflight_cleanup_row(
        dsn,
        marker,
        connection_factory=connection_factory,
    )
    _prove_worker_artifacts_absent(
        worker_client,
        marker,
        database_row=row,
    )
    marker = _delete_owned_row_with_intent(
        dsn=dsn,
        marker_path=marker_path,
        marker=marker,
        connection_factory=connection_factory,
    )
    _prove_worker_artifacts_absent(worker_client, marker)
    _prove_database_row_absent(
        dsn,
        marker,
        connection_factory=connection_factory,
    )
    marker = {
        **marker,
        "phase": "cleaned",
        "updated_at": _utc_now(),
    }
    _atomic_write_marker(marker_path, marker)
    return marker


def cleanup_canary(
    *,
    dsn: str,
    state_directory: Path,
    operation_id: str,
    source_sha: str,
    expected_byteff2_commit: str,
    capability: str,
    worker_client: MonomerMdWorkerClient,
    connection_factory: ConnectionFactory = postgres_connection,
) -> dict[str, Any]:
    operation_id, source_sha, expected_byteff2_commit = (
        validate_operation_identity(
            operation_id,
            source_sha,
            expected_byteff2_commit,
        )
    )
    state_directory = _require_private_state_directory(state_directory)
    with _operation_lock(state_directory):
        path = _marker_path(state_directory, operation_id, source_sha)
        marker = _load_marker(
            path,
            operation_id=operation_id,
            source_sha=source_sha,
            expected_byteff2_commit=expected_byteff2_commit,
        )
        _require_capability(marker, capability)
        if marker["phase"] == "cleaned":
            _prove_cleaned_locked(
                dsn=dsn,
                marker=marker,
                worker_client=worker_client,
                connection_factory=connection_factory,
            )
        else:
            marker = _cleanup_locked(
                dsn=dsn,
                marker_path=path,
                marker=marker,
                worker_client=worker_client,
                connection_factory=connection_factory,
            )
        return _canary_response(marker, "cleaned")
