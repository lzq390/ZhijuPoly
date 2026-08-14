#!/usr/bin/python3 -I
"""Plan and run the pre-maintenance PostgreSQL 16 migration rehearsal.

The rehearsal takes a fresh logical backup from the live PostgreSQL container,
restores it into a network-isolated, tmpfs-backed PostgreSQL 16 container, runs
the exact prepared Backend image's expand migrations, and seals bounded timing
and query-plan evidence.  It never connects the rehearsal container to a
Docker network and never writes to the production database.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid
from typing import Any, Iterator


PRODUCTION_ROOT = Path("/data/lzq/gith/nexpoly")
RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
EXPECTED_DATABASE = "nexpoly"
EXPECTED_PROPERTY_RECORDS = 615_159
BACKUP_RESTORE_LIMIT_SECONDS = 30 * 60
MIGRATION_LIMIT_SECONDS = 10 * 60
OPERATION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
SYSTEM_ID_RE = re.compile(r"^[0-9]{10,30}$")
MIGRATION_VERSION_RE = re.compile(r"^[0-9]{4}_[a-z0-9_]+$")
REPORT_MAX_AGE_SECONDS = 24 * 60 * 60
PG_DUMP_CLEANUP_SECONDS = 30
MIGRATION_MANIFEST_FIELDS = frozenset(
    {"version", "kind", "epoch", "checksum", "requires_contracts"}
)


def _load_controller() -> Any:
    path = Path(__file__).with_name("pull_deploy_controller.py")
    name = "nexpoly_production_rehearsal_controller"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RehearsalError("target controller validator is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:
        sys.modules.pop(name, None)
        raise RehearsalError("target controller validator cannot be loaded") from exc
    return module


class RehearsalError(RuntimeError):
    """The rehearsal authority or one of its assertions is invalid."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RehearsalError(message)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _digest(value: object) -> str:
    return _digest_bytes(_canonical_bytes(value))


def _database_ledger_projection(manifest: object) -> list[dict[str, str]]:
    """Project the full migration authority onto PostgreSQL's two columns."""

    _require(
        isinstance(manifest, list) and bool(manifest),
        "descriptor migration manifest is invalid",
    )
    projected: list[dict[str, str]] = []
    seen: set[str] = set()
    for record in manifest:
        _require(
            isinstance(record, dict)
            and set(record) == MIGRATION_MANIFEST_FIELDS
            and MIGRATION_VERSION_RE.fullmatch(str(record.get("version", "")))
            is not None
            and record.get("version") not in seen
            and record.get("kind") in {"baseline", "expand", "contract"}
            and isinstance(record.get("epoch"), int)
            and not isinstance(record.get("epoch"), bool)
            and record["epoch"] > 0
            and re.fullmatch(r"[0-9a-f]{64}", str(record.get("checksum", "")))
            is not None
            and isinstance(record.get("requires_contracts"), list),
            "descriptor migration manifest is invalid",
        )
        for requirement in record["requires_contracts"]:
            _require(
                isinstance(requirement, dict)
                and set(requirement) == {"version", "checksum"}
                and MIGRATION_VERSION_RE.fullmatch(
                    str(requirement.get("version", ""))
                )
                is not None
                and re.fullmatch(
                    r"[0-9a-f]{64}", str(requirement.get("checksum", ""))
                )
                is not None,
                "descriptor migration manifest is invalid",
            )
        version = str(record["version"])
        seen.add(version)
        projected.append(
            {"version": version, "checksum": str(record["checksum"])}
        )
    return projected


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise RehearsalError(f"cannot hash regular file: {path}") from exc
    try:
        before = os.fstat(descriptor)
        _require(stat.S_ISREG(before.st_mode), f"hash target is not regular: {path}")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            hasher.update(block)
        after = os.fstat(descriptor)
        path_metadata = path.lstat()
        _require(
            before.st_dev == after.st_dev == path_metadata.st_dev
            and before.st_ino == after.st_ino == path_metadata.st_ino
            and before.st_size == after.st_size,
            f"hash target changed while it was read: {path}",
        )
        return "sha256:" + hasher.hexdigest()
    except OSError as exc:
        raise RehearsalError(f"cannot hash stable regular file: {path}") from exc
    finally:
        os.close(descriptor)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object, label: str) -> dt.datetime:
    _require(isinstance(value, str) and value.endswith("Z"), f"{label} is invalid")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RehearsalError(f"{label} is invalid") from exc
    _require(
        parsed.tzinfo is not None and parsed.utcoffset() == dt.timedelta(0),
        f"{label} is invalid",
    )
    return parsed


def _remaining(deadline: float, label: str) -> float:
    remaining = deadline - time.monotonic()
    _require(remaining > 0, f"{label} exceeded its total deadline")
    return remaining


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    text: bool = True,
    stdin: Any = None,
    stdout: Any = subprocess.PIPE,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[Any]:
    environment = {
        "GIT_OPTIONAL_LOCKS": "0",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            check=check,
            text=text,
            stdin=stdin,
            stdout=stdout,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        detail = str(stderr or "").strip()[-1000:]
        raise RehearsalError(
            f"controlled command failed ({command[0]}): {detail}"
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RehearsalError(
            f"controlled command did not complete: {command[0]}"
        ) from exc


def _private_directory(path: Path, *, create: bool) -> None:
    _require(path.is_absolute(), f"private path is not absolute: {path}")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RehearsalError(f"private directory is unavailable: {path}") from exc
    _require(
        stat.S_ISDIR(metadata.st_mode)
        and not path.is_symlink()
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700,
        f"private directory is unsafe: {path}",
    )


def _private_file(path: Path, *, maximum_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RehearsalError(f"private file is unavailable: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        _require(
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and stat.S_IMODE(metadata.st_mode) == 0o600
            and 0 < metadata.st_size <= maximum_bytes,
            f"private file is unsafe: {path}",
        )
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        path_metadata = path.lstat()
        _require(
            0 < len(payload) <= maximum_bytes
            and metadata.st_dev == after.st_dev == path_metadata.st_dev
            and metadata.st_ino == after.st_ino == path_metadata.st_ino
            and metadata.st_size == after.st_size == len(payload),
            f"private file changed while it was read: {path}",
        )
        return payload
    except OSError as exc:
        raise RehearsalError(f"private file changed while it was read: {path}") from exc
    finally:
        os.close(descriptor)


def _load_private_json(path: Path) -> dict[str, Any]:
    payload = _private_file(path, maximum_bytes=32 * 1024 * 1024)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RehearsalError(f"private JSON is invalid: {path}") from exc
    _require(isinstance(value, dict), f"private JSON is not an object: {path}")
    return value


def _load_private_json_with_sha256(path: Path) -> tuple[dict[str, Any], str]:
    payload = _private_file(path, maximum_bytes=32 * 1024 * 1024)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RehearsalError(f"private JSON is invalid: {path}") from exc
    _require(isinstance(value, dict), f"private JSON is not an object: {path}")
    return value, _digest_bytes(payload)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_exclusive(path: Path, payload: bytes, mode: int = 0o600) -> None:
    """Atomically publish immutable evidence without a truncated-final window."""

    _require(payload, f"refusing to publish empty evidence: {path}")
    temporary = path.with_name(
        f".{path.name}.{hashlib.sha256(payload).hexdigest()}.{uuid.uuid4().hex}.tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temporary, flags, mode)
    except OSError as exc:
        raise RehearsalError(f"cannot stage immutable evidence: {path}") from exc
    try:
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, path, follow_symlinks=False)
        _fsync_directory(path.parent)
    except FileExistsError as exc:
        raise RehearsalError(f"refusing to overwrite evidence: {path}") from exc
    except OSError as exc:
        raise RehearsalError(f"cannot publish immutable evidence: {path}") from exc
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def _claim_json(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    """Create an immutable journal record, or claim an identical prior record."""

    payload = _canonical_bytes(value) + b"\n"
    if path.exists() or path.is_symlink():
        existing = _private_file(path, maximum_bytes=32 * 1024 * 1024)
        _require(existing == payload, f"journal record differs: {path.name}")
        return value
    try:
        _write_exclusive(path, payload)
    except RehearsalError:
        if path.exists() and not path.is_symlink():
            existing = _private_file(path, maximum_bytes=32 * 1024 * 1024)
            _require(existing == payload, f"journal record differs: {path.name}")
        else:
            raise
    return value


def _unlink_owned_private_file(path: Path) -> None:
    """Remove an operation-owned file without unlinking a raced replacement."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RehearsalError(f"operation-owned file is unavailable: {path}") from exc
    quarantine = path.with_name(f".{path.name}.{uuid.uuid4().hex}.quarantine")
    try:
        metadata = os.fstat(descriptor)
        _require(
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and metadata.st_nlink == 1
            and stat.S_IMODE(metadata.st_mode) == 0o600,
            f"operation-owned file is unsafe: {path}",
        )
        os.rename(path, quarantine)
        moved = quarantine.lstat()
        if moved.st_dev != metadata.st_dev or moved.st_ino != metadata.st_ino:
            with contextlib.suppress(OSError):
                os.rename(quarantine, path)
            raise RehearsalError(
                f"operation-owned file changed before quarantine: {path}"
            )
        quarantine.unlink()
        _fsync_directory(path.parent)
    except OSError as exc:
        raise RehearsalError(f"cannot safely remove operation-owned file: {path}") from exc
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _deploy_lock(runtime_root: Path) -> Iterator[None]:
    path = runtime_root / "state/deploy.lock"
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
    except OSError as exc:
        raise RehearsalError("the governed deploy lock is unavailable") from exc
    try:
        _require(
            stat.S_ISREG(metadata.st_mode)
            and not path.is_symlink()
            and metadata.st_uid == os.geteuid()
            and stat.S_IMODE(metadata.st_mode) == 0o600,
            "the governed deploy lock is unsafe",
        )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        after = os.fstat(descriptor)
        path_metadata = path.lstat()
        _require(
            metadata.st_dev == after.st_dev == path_metadata.st_dev
            and metadata.st_ino == after.st_ino == path_metadata.st_ino,
            "the governed deploy lock changed while acquiring it",
        )
        yield
    finally:
        os.close(descriptor)


def _parse_literal_env(path: Path) -> dict[str, str]:
    payload = _private_file(path, maximum_bytes=1024 * 1024)
    values: dict[str, str] = {}
    for raw_line in payload.decode("utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        _require("=" in line and not line.startswith("export "), "deploy.env is not literal")
        key, value = line.split("=", 1)
        _require(
            re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is not None
            and key not in values
            and "\x00" not in value,
            "deploy.env contains an invalid entry",
        )
        values[key] = value
    return values


def _descriptor_authority(
    runtime_root: Path,
    operation_id: str,
    target_sha: str,
) -> tuple[dict[str, Any], Path, str, str]:
    prepared = runtime_root / "state/prepared" / operation_id
    _private_directory(prepared, create=False)
    descriptor_path = prepared / "descriptor.json"
    ready_path = prepared / "ready.json"
    raw_descriptor, descriptor_sha256 = _load_private_json_with_sha256(
        descriptor_path
    )
    controller = _load_controller()
    try:
        descriptor = controller.validate_descriptor(raw_descriptor)
    except Exception as exc:
        raise RehearsalError("prepared descriptor failed the complete V4 contract") from exc
    ready, ready_sha256 = _load_private_json_with_sha256(ready_path)
    repository = descriptor.get("repository")
    images = descriptor.get("images")
    postgres = descriptor.get("postgres_restore_image")
    _require(
        descriptor.get("schema_version") == controller.DESCRIPTOR_SCHEMA_VERSION == 4,
        "rehearsal requires descriptor V4",
    )
    _require(descriptor.get("operation_id") == operation_id, "descriptor operation differs")
    _require(
        isinstance(repository, dict)
        and repository.get("target_sha") == target_sha
        and SHA_RE.fullmatch(str(repository.get("target_tree", ""))) is not None,
        "descriptor target identity differs",
    )
    _require(
        isinstance(images, dict)
        and isinstance(images.get("backend"), dict)
        and DIGEST_RE.fullmatch(str(images["backend"].get("image_id", ""))) is not None
        and "@sha256:" in str(images["backend"].get("digest_ref", "")),
        "descriptor Backend image is invalid",
    )
    _require(
        isinstance(postgres, dict)
        and DIGEST_RE.fullmatch(str(postgres.get("image_id", ""))) is not None
        and "@sha256:" in str(postgres.get("digest_ref", "")),
        "descriptor PostgreSQL image is invalid",
    )
    _require(
        ready.get("schema_version") == 1
        and ready.get("status") == "ready"
        and ready.get("operation_id") == operation_id
        and ready.get("source_sha") == target_sha
        and ready.get("descriptor_sha256") == descriptor_sha256,
        "prepared ready authority differs from descriptor",
    )
    # The descriptor validator pins the exact PG16 digest.  Re-state it here so
    # this script fails closed if an older validator is ever selected.
    _require(
        postgres["digest_ref"] == controller.POSTGRES16_IMAGE,
        "descriptor PostgreSQL restore image is not the fixed PG16 authority",
    )
    return descriptor, descriptor_path, descriptor_sha256, ready_sha256


def _live_postgres_container() -> tuple[str, dict[str, Any]]:
    completed = _run(
        [
            "docker",
            "ps",
            "--filter",
            "label=com.docker.compose.project=nexpoly",
            "--filter",
            "label=com.docker.compose.service=lab-postgres",
            "--format",
            "{{.ID}}",
        ]
    )
    identifiers = [line.strip() for line in str(completed.stdout).splitlines() if line.strip()]
    _require(len(identifiers) == 1, "production PostgreSQL container is ambiguous")
    inspected = _run(["docker", "container", "inspect", identifiers[0]])
    try:
        records = json.loads(str(inspected.stdout))
    except json.JSONDecodeError as exc:
        raise RehearsalError("production PostgreSQL inspect is invalid") from exc
    _require(isinstance(records, list) and len(records) == 1, "PostgreSQL inspect differs")
    record = records[0]
    container_id = str(record.get("Id", ""))
    state = record.get("State")
    labels = (record.get("Config") or {}).get("Labels") or {}
    _require(CONTAINER_ID_RE.fullmatch(container_id) is not None, "PostgreSQL ID is invalid")
    _require(
        isinstance(state, dict)
        and state.get("Running") is True
        and labels.get("com.docker.compose.project") == "nexpoly"
        and labels.get("com.docker.compose.service") == "lab-postgres",
        "production PostgreSQL runtime identity is invalid",
    )
    return container_id, record


def _psql_json(
    container_id: str,
    user: str,
    database: str,
    sql: str,
    *,
    postgres_options: str | None = None,
) -> Any:
    command = ["docker", "exec"]
    if postgres_options is not None:
        _require(
            postgres_options == "-c enable_seqscan=off",
            "unsupported PostgreSQL diagnostic option",
        )
        command.extend(["--env", f"PGOPTIONS={postgres_options}"])
    command.extend(
        [
            container_id,
            "psql",
            "-X",
            "--quiet",
            "--set",
            "ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            "--username",
            user,
            "--dbname",
            database,
            "--command",
            sql,
        ]
    )
    completed = _run(
        command,
        timeout=300,
    )
    output = str(completed.stdout).strip()
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise RehearsalError("PostgreSQL evidence is invalid JSON") from exc


def _pg_dump_process_absent(
    container_id: str,
    application_name: str,
    *,
    signal: str = "probe",
) -> bool:
    """Inspect (and optionally signal) only the operation-tagged pg_dump."""

    _require(
        re.fullmatch(r"nexpoly_rehearsal_[0-9a-f]{16}", application_name)
        is not None,
        "pg_dump application identity is invalid",
    )
    _require(signal in {"probe", "TERM", "KILL"}, "pg_dump signal is invalid")
    script = r'''
marker=$1
action=$2
found=0
for command_file in /proc/[0-9]*/cmdline; do
    [ -r "$command_file" ] || continue
    pid=${command_file#/proc/}
    pid=${pid%%/*}
    [ "$pid" = "$$" ] && continue
    command=$(tr '\000' '\n' < "$command_file" 2>/dev/null || true)
    case "$command" in
        *"$marker"*)
            found=1
            if [ "$action" != "probe" ]; then
                kill -"$action" "$pid" 2>/dev/null || true
            fi
            ;;
    esac
done
[ "$found" -eq 0 ]
'''
    completed = _run(
        [
            "docker",
            "exec",
            container_id,
            "/bin/sh",
            "-c",
            script,
            "nexpoly-pg-dump-process-fence",
            application_name,
            signal,
        ],
        check=False,
        timeout=10,
    )
    _require(
        completed.returncode in {0, 1},
        "cannot inspect the production container for a residual pg_dump",
    )
    return completed.returncode == 0


def _pg_dump_backend_count(
    container_id: str,
    user: str,
    database: str,
    application_name: str,
    *,
    terminate: bool,
) -> int:
    _require(
        re.fullmatch(r"nexpoly_rehearsal_[0-9a-f]{16}", application_name)
        is not None,
        "pg_dump application identity is invalid",
    )
    action = (
        "coalesce(bool_and(pg_terminate_backend(pid)), true)"
        if terminate
        else "true"
    )
    result = _psql_json(
        container_id,
        user,
        database,
        f"""
        SELECT json_build_object(
          'count', count(*),
          'action_ok', {action}
        )::text
        FROM pg_stat_activity
        WHERE application_name = '{application_name}'
          AND pid <> pg_backend_pid();
        """,
    )
    _require(
        isinstance(result, dict)
        and isinstance(result.get("count"), int)
        and not isinstance(result.get("count"), bool)
        and result["count"] >= 0
        and isinstance(result.get("action_ok"), bool),
        "pg_dump server-backend evidence is invalid",
    )
    return int(result["count"])


def _terminate_and_prove_pg_dump_absent(
    container_id: str,
    user: str,
    database: str,
    application_name: str,
) -> None:
    """Converge both server and in-container sides of one pg_dump to zero."""

    _pg_dump_backend_count(
        container_id,
        user,
        database,
        application_name,
        terminate=True,
    )
    _pg_dump_process_absent(
        container_id, application_name, signal="TERM"
    )
    deadline = time.monotonic() + PG_DUMP_CLEANUP_SECONDS
    sent_kill = False
    while True:
        backend_count = _pg_dump_backend_count(
            container_id,
            user,
            database,
            application_name,
            terminate=False,
        )
        process_absent = _pg_dump_process_absent(
            container_id, application_name
        )
        if backend_count == 0 and process_absent:
            return
        if time.monotonic() >= deadline:
            raise RehearsalError(
                "cannot prove timed-out pg_dump server/process cleanup"
            )
        if not sent_kill and deadline - time.monotonic() <= (
            PG_DUMP_CLEANUP_SECONDS / 2
        ):
            _pg_dump_backend_count(
                container_id,
                user,
                database,
                application_name,
                terminate=True,
            )
            _pg_dump_process_absent(
                container_id, application_name, signal="KILL"
            )
            sent_kill = True
        time.sleep(0.2)


def _run_pg_dump_with_cleanup(
    *,
    container_id: str,
    user: str,
    database: str,
    operation_id: str,
    output: Any,
    timeout: float,
) -> None:
    """Run a bounded pg_dump and always prove the remote work has ended."""

    application_name = "nexpoly_rehearsal_" + hashlib.sha256(
        operation_id.encode("ascii")
    ).hexdigest()[:16]
    inner_seconds = max(1, int(timeout))
    failure: BaseException | None = None
    try:
        _run(
            [
                "docker",
                "exec",
                "--env",
                f"PGAPPNAME={application_name}",
                container_id,
                "/usr/bin/timeout",
                "-s",
                "TERM",
                "-k",
                f"{PG_DUMP_CLEANUP_SECONDS}s",
                f"{inner_seconds}s",
                "pg_dump",
                "--format=custom",
                "--username",
                user,
                "--dbname",
                (
                    f"postgresql:///{database}"
                    f"?application_name={application_name}"
                ),
            ],
            text=False,
            stdout=output,
            timeout=timeout + PG_DUMP_CLEANUP_SECONDS + 5,
        )
    except BaseException as exc:
        failure = exc
    try:
        _terminate_and_prove_pg_dump_absent(
            container_id,
            user,
            database,
            application_name,
        )
    except BaseException as cleanup_exc:
        raise RehearsalError(
            "pg_dump did not leave a provably clean server/process boundary"
        ) from (failure or cleanup_exc)
    if failure is not None:
        raise failure


def _source_database_evidence(
    container_id: str,
    record: dict[str, Any],
    user: str,
    database: str,
) -> dict[str, Any]:
    document = _psql_json(
        container_id,
        user,
        database,
        """
        BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
        SELECT json_build_object(
          'system_identifier', (SELECT system_identifier FROM pg_control_system()),
          'server_version_num', current_setting('server_version_num'),
          'property_records', (SELECT count(*) FROM core.polymer_property_filter_records),
          'ledger', (SELECT coalesce(json_agg(json_build_object('version', version, 'checksum', checksum) ORDER BY version), '[]'::json) FROM governance.schema_migrations)
        )::text;
        COMMIT;
        """,
    )
    _require(isinstance(document, dict), "source database evidence is invalid")
    system_identifier = str(document.get("system_identifier", ""))
    version = str(document.get("server_version_num", ""))
    property_records = document.get("property_records")
    ledger = document.get("ledger")
    _require(SYSTEM_ID_RE.fullmatch(system_identifier) is not None, "source system ID is invalid")
    _require(version.startswith("16") and version.isdigit(), "source PostgreSQL is not major 16")
    _require(
        isinstance(property_records, int)
        and not isinstance(property_records, bool)
        and property_records > 0,
        "source property count is invalid",
    )
    _require(isinstance(ledger, list) and ledger, "source migration ledger is invalid")
    state = record.get("State") or {}
    host = record.get("HostConfig") or {}
    config = record.get("Config") or {}
    mounts = record.get("Mounts") or []
    restart_count = record.get("RestartCount")
    runtime_projection = {
        "container_id": container_id,
        "image_id": record.get("Image"),
        "config_image": config.get("Image"),
        "name": record.get("Name"),
        "pid": state.get("Pid"),
        "started_at": state.get("StartedAt"),
        "restart_count": restart_count,
        "network_mode": host.get("NetworkMode"),
        "binds": sorted(host.get("Binds") or []),
        "port_bindings": host.get("PortBindings") or {},
        "mounts": sorted(
            [
                {
                    "type": item.get("Type"),
                    "name": item.get("Name"),
                    "source": item.get("Source"),
                    "destination": item.get("Destination"),
                    "rw": item.get("RW"),
                }
                for item in mounts
                if isinstance(item, dict)
            ],
            key=lambda item: str(item.get("destination")),
        ),
    }
    _require(
        DIGEST_RE.fullmatch(str(runtime_projection["image_id"])) is not None
        and isinstance(runtime_projection["pid"], int)
        and not isinstance(runtime_projection["pid"], bool)
        and runtime_projection["pid"] > 0
        and isinstance(runtime_projection["started_at"], str)
        and runtime_projection["started_at"]
        and isinstance(restart_count, int)
        and not isinstance(restart_count, bool)
        and restart_count >= 0,
        "source PostgreSQL runtime evidence is invalid",
    )
    return {
        "container_id": container_id,
        "image_id": str(record.get("Image", "")),
        "restart_count": restart_count,
        "runtime_sha256": _digest(runtime_projection),
        "system_identifier": system_identifier,
        "server_version_num": version,
        "database": database,
        "user": user,
        "property_records": property_records,
        "ledger": ledger,
        "ledger_sha256": _digest(ledger),
    }


def _validate_local_image(image: dict[str, Any], label: str) -> dict[str, Any]:
    digest_ref = str(image.get("digest_ref", ""))
    expected_id = str(image.get("image_id", ""))
    repository, manifest_digest = digest_ref.rsplit("@", 1)
    final_component = repository.rsplit("/", 1)[-1]
    if ":" in final_component:
        repository = repository.rsplit(":", 1)[0]
    canonical_repo_digest = f"{repository}@{manifest_digest}"
    completed = _run(["docker", "image", "inspect", digest_ref])
    try:
        records = json.loads(str(completed.stdout))
    except json.JSONDecodeError as exc:
        raise RehearsalError(f"{label} image inspect is invalid") from exc
    _require(isinstance(records, list) and len(records) == 1, f"{label} image is ambiguous")
    record = records[0]
    repo_digests = record.get("RepoDigests")
    _require(
        record.get("Id") == expected_id
        and isinstance(repo_digests, list)
        and canonical_repo_digest in repo_digests,
        f"{label} digest reference does not resolve to its sealed image ID",
    )
    return dict(image)


def build_plan(
    *,
    production_root: Path,
    runtime_root: Path,
    operation_id: str,
    target_sha: str,
) -> dict[str, Any]:
    _require(OPERATION_RE.fullmatch(operation_id) is not None, "invalid operation ID")
    _require(SHA_RE.fullmatch(target_sha) is not None, "invalid target SHA")
    _private_directory(runtime_root, create=False)
    _private_directory(runtime_root / "state", create=False)
    descriptor, descriptor_path, descriptor_sha256, ready_sha256 = _descriptor_authority(
        runtime_root, operation_id, target_sha
    )
    repository = descriptor["repository"]
    head = str(_run(["git", "rev-parse", "HEAD"], cwd=production_root).stdout).strip()
    tree = str(_run(["git", "rev-parse", "HEAD^{tree}"], cwd=production_root).stdout).strip()
    status = str(_run(["git", "status", "--porcelain=v1"], cwd=production_root).stdout)
    _require(
        head == repository["previous_sha"]
        and tree == repository["previous_tree"]
        and status == "",
        "production repository changed after prepare",
    )
    values = _parse_literal_env(runtime_root / "config/deploy.env")
    user = values.get("NEXPOLY_POSTGRES_USER", "")
    database = values.get("NEXPOLY_POSTGRES_DB", "")
    capacity_value = values.get("NEXPOLY_POSTGRES_RESTORE_TMPFS_BYTES", "")
    _require(re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", user) is not None, "PostgreSQL user is invalid")
    _require(database == EXPECTED_DATABASE, "production database must be nexpoly")
    _require(capacity_value.isdigit(), "restore tmpfs capacity is invalid")
    capacity = int(capacity_value)
    _require(8 * 1024**3 <= capacity <= 256 * 1024**3, "restore tmpfs capacity is out of bounds")
    container_id, inspect = _live_postgres_container()
    source = _source_database_evidence(container_id, inspect, user, database)
    migration_manifest = descriptor.get("migrations", {}).get("records")
    expected_ledger = _database_ledger_projection(migration_manifest)
    _require(
        len(expected_ledger) >= 2
        and [record.get("version") for record in expected_ledger[-2:]]
        == [
            "0014_monomer_md_task_queue_cancel",
            "0015_property_filter_performance",
        ]
        and source["ledger"] == expected_ledger[:-2]
        and source["ledger"][-1].get("version") == "0013_monomer_dft_jobs",
        "production source ledger is not the exact post-0013 predecessor",
    )
    _require(
        source["property_records"] == EXPECTED_PROPERTY_RECORDS,
        "production property record count differs from the reviewed 615159 baseline",
    )
    backend_image = _validate_local_image(descriptor["images"]["backend"], "Backend")
    postgres_image = _validate_local_image(descriptor["postgres_restore_image"], "PostgreSQL 16")
    plan = {
        "schema_version": 1,
        "action": "production-postgres-rehearsal",
        "apply": False,
        "operation_id": operation_id,
        "target_sha": target_sha,
        "target_tree": repository["target_tree"],
        "descriptor_path": str(descriptor_path),
        "descriptor_sha256": descriptor_sha256,
        "ready_sha256": ready_sha256,
        "backend_image": backend_image,
        "postgres_image": postgres_image,
        "source": source,
        "expected_target_ledger_sha256": _digest(expected_ledger),
        "expected_property_records": EXPECTED_PROPERTY_RECORDS,
        "restore_tmpfs_bytes": capacity,
        "limits_seconds": {
            "backup_restore": BACKUP_RESTORE_LIMIT_SECONDS,
            "migrations": MIGRATION_LIMIT_SECONDS,
        },
    }
    plan["confirmations"] = {
        "descriptor_sha256": descriptor_sha256,
        "source_system_identifier": source["system_identifier"],
        "source_ledger_sha256": source["ledger_sha256"],
        "source_property_records": source["property_records"],
        "plan_sha256": _digest(plan),
    }
    return plan


def _wait_postgres(container_id: str, *, deadline: float) -> None:
    readiness_deadline = min(deadline, time.monotonic() + 120)
    while True:
        completed = _run(
            ["docker", "exec", container_id, "pg_isready", "-U", "postgres"],
            check=False,
        )
        if completed.returncode == 0:
            return
        _require(completed.returncode in {1, 2}, "isolated PostgreSQL readiness failed")
        if time.monotonic() >= readiness_deadline:
            raise RehearsalError("isolated PostgreSQL did not become ready")
        time.sleep(1)


def _owned_rehearsal_container(
    name: str,
    operation_id: str,
    descriptor_sha256: str,
    postgres_image: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    completed = _run(["docker", "container", "inspect", name], check=False)
    if completed.returncode == 1 and "No such" in str(completed.stderr):
        return None
    _require(completed.returncode == 0, "cannot prove rehearsal container state")
    try:
        records = json.loads(str(completed.stdout))
    except json.JSONDecodeError as exc:
        raise RehearsalError("rehearsal container inspect is invalid") from exc
    _require(isinstance(records, list) and len(records) == 1, "rehearsal container is ambiguous")
    record = records[0]
    identifier = str(record.get("Id", ""))
    labels = (record.get("Config") or {}).get("Labels") or {}
    host = record.get("HostConfig") or {}
    _require(
        CONTAINER_ID_RE.fullmatch(identifier) is not None
        and labels.get("com.nexpoly.rehearsal-operation") == operation_id
        and labels.get("com.nexpoly.rehearsal-descriptor") == descriptor_sha256
        and record.get("Image") == postgres_image["image_id"]
        and host.get("NetworkMode") == "none"
        and host.get("ReadonlyRootfs") is True
        and host.get("Privileged") is False
        and not host.get("Binds")
        and not (host.get("PortBindings") or {})
        and (host.get("RestartPolicy") or {}).get("Name") in {"", "no"}
        and (host.get("Tmpfs") or {}).get("/var/lib/postgresql/data")
        and (host.get("Tmpfs") or {}).get("/var/run/postgresql")
        and (host.get("Tmpfs") or {}).get("/tmp"),
        "existing rehearsal container is not owned by this operation",
    )
    return identifier, record


def _remove_owned_container(
    name: str,
    identifier: str,
    operation_id: str,
    descriptor_sha256: str,
    postgres_image: dict[str, Any],
) -> None:
    owned = _owned_rehearsal_container(
        name, operation_id, descriptor_sha256, postgres_image
    )
    if owned is None:
        return
    _require(owned[0] == identifier, "rehearsal container changed before cleanup")
    _run(["docker", "container", "rm", "--force", identifier])
    _require(
        _owned_rehearsal_container(
            name, operation_id, descriptor_sha256, postgres_image
        )
        is None,
        "rehearsal container remained after cleanup",
    )


def _owned_migration_container(
    name: str,
    operation_id: str,
    descriptor_sha256: str,
    backend_image: dict[str, Any],
    postgres_container_id: str,
) -> tuple[str, dict[str, Any]] | None:
    completed = _run(["docker", "container", "inspect", name], check=False)
    if completed.returncode == 1 and "No such" in str(completed.stderr):
        return None
    _require(completed.returncode == 0, "cannot prove migration container state")
    try:
        records = json.loads(str(completed.stdout))
    except json.JSONDecodeError as exc:
        raise RehearsalError("migration container inspect is invalid") from exc
    _require(isinstance(records, list) and len(records) == 1, "migration container is ambiguous")
    record = records[0]
    identifier = str(record.get("Id", ""))
    config = record.get("Config") or {}
    host = record.get("HostConfig") or {}
    labels = config.get("Labels") or {}
    environment = config.get("Env") or []
    command = config.get("Cmd") or []
    rehearsal_dsn = "postgresql://postgres@127.0.0.1:5432/nexpoly_restore"
    governed_environment = {
        f"APP_POSTGRES_DSN={rehearsal_dsn}",
        f"PI_POSTGRES_DSN={rehearsal_dsn}",
        f"LAB_DATA_POSTGRES_DSN={rehearsal_dsn}",
    }
    _require(
        CONTAINER_ID_RE.fullmatch(identifier) is not None
        and record.get("Image") == backend_image["image_id"]
        and labels.get("com.nexpoly.rehearsal-operation") == operation_id
        and labels.get("com.nexpoly.rehearsal-descriptor") == descriptor_sha256
        and host.get("NetworkMode") in {
            f"container:{postgres_container_id}",
            postgres_container_id,
        }
        and not host.get("Binds")
        and not (host.get("PortBindings") or {})
        and governed_environment | {"PYTHONDONTWRITEBYTECODE=1"}
        == {
            entry
            for entry in environment
            if entry.startswith(
                (
                    "APP_POSTGRES_DSN=",
                    "PI_POSTGRES_DSN=",
                    "LAB_DATA_POSTGRES_DSN=",
                    "PYTHONDONTWRITEBYTECODE=",
                )
            )
        }
        and config.get("Entrypoint") == ["python"]
        and command
        == [
            "-m",
            "app.postgres_migrations",
            "--mode",
            "expand",
            "--dsn",
            rehearsal_dsn,
        ]
        and host.get("ReadonlyRootfs") is True
        and host.get("Privileged") is False
        and set(host.get("CapDrop") or []) == {"ALL"}
        and bool(
            {"no-new-privileges", "no-new-privileges:true"}
            & set(host.get("SecurityOpt") or [])
        )
        and (host.get("Tmpfs") or {}).get("/tmp")
        and (host.get("RestartPolicy") or {}).get("Name") in {"", "no"}
        and not any(
            entry.startswith(
                (
                    "ONLINE_KNOWLEDGE_API_KEY=",
                    "ASSISTANT_API_KEY=",
                    "NEXPOLY_POSTGRES_PASSWORD=",
                )
            )
            for entry in environment
        ),
        "migration container is not the exact secret-free isolated authority",
    )
    return identifier, record


def _remove_owned_migration_container(
    name: str,
    identifier: str,
    operation_id: str,
    descriptor_sha256: str,
    backend_image: dict[str, Any],
    postgres_container_id: str,
) -> None:
    owned = _owned_migration_container(
        name,
        operation_id,
        descriptor_sha256,
        backend_image,
        postgres_container_id,
    )
    if owned is None:
        return
    _require(owned[0] == identifier, "migration container changed before cleanup")
    _run(["docker", "container", "rm", "--force", identifier])
    _require(
        _owned_migration_container(
            name,
            operation_id,
            descriptor_sha256,
            backend_image,
            postgres_container_id,
        )
        is None,
        "migration container remained after cleanup",
    )


def _container_absent(name: str, label: str) -> None:
    completed = _run(["docker", "container", "inspect", name], check=False)
    _require(
        completed.returncode == 1 and "No such" in str(completed.stderr),
        f"{label} container name is still present or cannot be proved absent",
    )


def _migration_network_target(name: str) -> str | None:
    completed = _run(["docker", "container", "inspect", name], check=False)
    if completed.returncode == 1 and "No such" in str(completed.stderr):
        return None
    _require(completed.returncode == 0, "cannot inspect stale migration container")
    try:
        records = json.loads(str(completed.stdout))
    except json.JSONDecodeError as exc:
        raise RehearsalError("stale migration container inspect is invalid") from exc
    _require(isinstance(records, list) and len(records) == 1, "stale migration container is ambiguous")
    network_mode = str((records[0].get("HostConfig") or {}).get("NetworkMode", ""))
    target = network_mode.removeprefix("container:")
    _require(CONTAINER_ID_RE.fullmatch(target) is not None, "stale migration network target is invalid")
    return target


def _cleanup_owned_attempt(
    *,
    postgres_name: str,
    migration_name: str,
    operation_id: str,
    descriptor_sha256: str,
    postgres_image: dict[str, Any],
    backend_image: dict[str, Any],
) -> None:
    postgres = _owned_rehearsal_container(
        postgres_name, operation_id, descriptor_sha256, postgres_image
    )
    network_target = postgres[0] if postgres is not None else _migration_network_target(migration_name)
    if network_target is not None:
        migration = _owned_migration_container(
            migration_name,
            operation_id,
            descriptor_sha256,
            backend_image,
            network_target,
        )
        if migration is not None:
            _remove_owned_migration_container(
                migration_name,
                migration[0],
                operation_id,
                descriptor_sha256,
                backend_image,
                network_target,
            )
    if postgres is not None:
        _remove_owned_container(
            postgres_name,
            postgres[0],
            operation_id,
            descriptor_sha256,
            postgres_image,
        )
    _container_absent(migration_name, "migration")
    _container_absent(postgres_name, "PostgreSQL rehearsal")


def _journal_record(
    path: Path,
    *,
    phase: str,
    previous_sha256: str | None,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    record = {
        "schema_version": 1,
        "phase": phase,
        "previous_sha256": previous_sha256,
        "payload": payload,
    }
    sealed = {"record": record, "record_sha256": _digest(record)}
    _claim_json(path, sealed)
    return sealed, sealed["record_sha256"]


def _load_journal_record(
    path: Path,
    *,
    phase: str,
    previous_sha256: str | None,
) -> tuple[dict[str, Any], str]:
    sealed = _load_private_json(path)
    _require(
        set(sealed) == {"record", "record_sha256"}
        and isinstance(sealed.get("record"), dict)
        and set(sealed["record"])
        == {"schema_version", "phase", "previous_sha256", "payload"}
        and sealed["record"].get("schema_version") == 1
        and sealed["record"].get("phase") == phase
        and sealed["record"].get("previous_sha256") == previous_sha256
        and isinstance(sealed["record"].get("payload"), dict)
        and sealed.get("record_sha256") == _digest(sealed["record"]),
        f"{phase} journal record is invalid",
    )
    return sealed, sealed["record_sha256"]


def _parse_migration_records(
    output: str,
    manifest: list[dict[str, Any]],
    *,
    existing_count: int,
) -> list[dict[str, str]]:
    lines = output.splitlines()
    _require(lines and all(line for line in lines), "migration output contains an empty record")
    _require(len(lines) == len(manifest), "migration output does not cover the exact target manifest")
    result: list[dict[str, str]] = []
    for index, (line, expected) in enumerate(zip(lines, manifest, strict=True)):
        fields = line.split("\t")
        _require(
            len(fields) == 3
            and MIGRATION_VERSION_RE.fullmatch(fields[0]) is not None
            and fields[1] in {"applied", "skipped"}
            and re.fullmatch(r"[0-9a-f]{64}", fields[2]) is not None
            and fields[0] == expected.get("version")
            and fields[2] == expected.get("checksum"),
            "migration output differs from the canonical target manifest",
        )
        expected_status = "skipped" if index < existing_count else "applied"
        _require(
            fields[1] == expected_status,
            "migration output did not apply exactly 0014 then 0015",
        )
        result.append(
            {"version": fields[0], "status": fields[1], "checksum": fields[2]}
        )
    _require(
        [record["version"] for record in result[-2:]]
        == [
            "0014_monomer_md_task_queue_cancel",
            "0015_property_filter_performance",
        ],
        "migration output tail is not exact 0014/0015",
    )
    return result


def _plan_index_names(value: object) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        index_name = value.get("Index Name")
        if isinstance(index_name, str):
            names.add(index_name)
        for child in value.values():
            names.update(_plan_index_names(child))
    elif isinstance(value, list):
        for child in value:
            names.update(_plan_index_names(child))
    return names


def _records_index_names(value: object, *, records_scope: bool = False) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        current_scope = records_scope or value.get("Alias") == "records"
        name = value.get("Index Name")
        node_type = value.get("Node Type")
        if (
            current_scope
            and isinstance(name, str)
            and isinstance(node_type, str)
            and "Index" in node_type
            and isinstance(value.get("Index Cond"), str)
            and value["Index Cond"]
        ):
            names.add(name)
        for child in value.values():
            names.update(
                _records_index_names(child, records_scope=current_scope)
            )
    elif isinstance(value, list):
        for child in value:
            names.update(
                _records_index_names(child, records_scope=records_scope)
            )
    return names


def _post_migration_evidence(container_id: str, expected_records: int) -> dict[str, Any]:
    summary = _psql_json(
        container_id,
        "postgres",
        "nexpoly_restore",
        """
        BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
        SELECT json_build_object(
          'system_identifier', (SELECT system_identifier FROM pg_control_system()),
          'ledger', (SELECT coalesce(json_agg(json_build_object('version', version, 'checksum', checksum) ORDER BY version), '[]'::json) FROM governance.schema_migrations),
          'property_records', (SELECT count(*) FROM core.polymer_property_filter_records),
          'snapshot_count', (SELECT count(*) FROM governance.property_filter_options_snapshots),
          'snapshot', (SELECT json_build_object('snapshot_key', snapshot_key, 'schema_version', schema_version, 'generation', generation, 'total_records', total_records, 'mapped_records', mapped_records, 'raw_records', raw_records, 'option_count', jsonb_array_length(options)) FROM governance.property_filter_options_snapshots WHERE snapshot_key='current'),
          'indexes', (SELECT coalesce(json_agg(indexname ORDER BY indexname), '[]'::json) FROM pg_indexes WHERE schemaname='core' AND indexname IN ('idx_core_filter_records_standardized_unit_value','idx_core_filter_records_raw_unit_value_v2')),
          'statistics', (SELECT coalesce(json_agg(statistics_name ORDER BY statistics_name), '[]'::json) FROM pg_stats_ext WHERE schemaname='core' AND statistics_name IN ('stats_core_filter_records_standardized_unit','stats_core_filter_records_raw_unit'))
        )::text;
        COMMIT;
        """,
    )
    _require(isinstance(summary, dict), "post-migration summary is invalid")
    snapshot = summary.get("snapshot")
    _require(summary.get("property_records") == expected_records, "property record count changed during migrations")
    _require(summary.get("snapshot_count") == 1 and isinstance(snapshot, dict), "0015 did not create exactly one snapshot")
    _require(
        snapshot.get("snapshot_key") == "current"
        and snapshot.get("schema_version") == 1
        and snapshot.get("generation") == 1
        and snapshot.get("total_records") == expected_records
        and snapshot.get("mapped_records", 0) + snapshot.get("raw_records", 0) == expected_records
        and isinstance(snapshot.get("option_count"), int)
        and snapshot["option_count"] > 0,
        "0015 snapshot contents are invalid",
    )
    _require(
        set(summary.get("indexes", []))
        == {
            "idx_core_filter_records_standardized_unit_value",
            "idx_core_filter_records_raw_unit_value_v2",
        },
        "0015 filter indexes are incomplete",
    )
    _require(
        set(summary.get("statistics", []))
        == {
            "stats_core_filter_records_standardized_unit",
            "stats_core_filter_records_raw_unit",
        },
        "0015 extended statistics are incomplete",
    )
    plans: dict[str, Any] = {}
    queries = {
        "standardized": """
          EXPLAIN (FORMAT JSON)
          WITH target AS MATERIALIZED (
            SELECT property_key, COALESCE(canonical_unit, '') AS unit_value,
                   canonical_value AS target_value
            FROM core.polymer_property_filter_records
            WHERE property_key IS NOT NULL AND canonical_value IS NOT NULL
            ORDER BY filter_record_id LIMIT 1
          )
          SELECT records.filter_record_id
          FROM core.polymer_property_filter_records AS records, target
          WHERE records.property_key=target.property_key
            AND COALESCE(records.canonical_unit, '')=target.unit_value
            AND records.canonical_value=target.target_value
          LIMIT 100;
        """,
        "raw": """
          EXPLAIN (FORMAT JSON)
          WITH target AS MATERIALIZED (
            SELECT property_name, COALESCE(property_unit_clean, '') AS unit_value,
                   property_value_num AS target_value
            FROM core.polymer_property_filter_records
            WHERE property_key IS NULL AND property_value_num IS NOT NULL
            ORDER BY filter_record_id LIMIT 1
          )
          SELECT records.filter_record_id
          FROM core.polymer_property_filter_records AS records, target
          WHERE records.property_key IS NULL
            AND records.property_name=target.property_name
            AND COALESCE(records.property_unit_clean, '')=target.unit_value
            AND records.property_value_num=target.target_value
          LIMIT 100;
        """,
    }
    expected_indexes = {
        "standardized": "idx_core_filter_records_standardized_unit_value",
        "raw": "idx_core_filter_records_raw_unit_value_v2",
    }
    for label, query in queries.items():
        default_plan = _psql_json(
            container_id, "postgres", "nexpoly_restore", query
        )
        diagnostic_plan = _psql_json(
            container_id,
            "postgres",
            "nexpoly_restore",
            query,
            postgres_options="-c enable_seqscan=off",
        )
        default_names = sorted(_records_index_names(default_plan))
        diagnostic_names = sorted(_records_index_names(diagnostic_plan))
        _require(
            expected_indexes[label] in default_names,
            f"{label} default query plan did not use the 0015 records index",
        )
        _require(
            expected_indexes[label] in diagnostic_names,
            f"{label} diagnostic query plan did not use the 0015 records index",
        )
        plans[label] = {
            "default_records_index_names": default_names,
            "default_plan_sha256": _digest(default_plan),
            "diagnostic_records_index_names": diagnostic_names,
            "diagnostic_plan_sha256": _digest(diagnostic_plan),
        }
    summary["query_plans"] = plans
    return summary


def validate_rehearsal_report(
    sealed: object,
    *,
    descriptor: dict[str, Any],
    descriptor_sha256: str,
    ready_sha256: str,
    runtime_root: Path | str,
    now: dt.datetime | None = None,
    verify_runtime: bool = True,
) -> dict[str, Any]:
    """Validate the complete immutable rehearsal contract for controller apply."""

    _require(
        isinstance(sealed, dict)
        and set(sealed) == {"report", "report_sha256"}
        and isinstance(sealed.get("report"), dict)
        and sealed.get("report_sha256") == _digest(sealed["report"]),
        "rehearsal report seal differs",
    )
    report = sealed["report"]
    report_fields = {
        "schema_version",
        "status",
        "operation_id",
        "target_sha",
        "target_tree",
        "descriptor_sha256",
        "ready_sha256",
        "plan_sha256",
        "backend_image",
        "postgres_image",
        "source_before",
        "source_after",
        "dump",
        "restored_before",
        "migrations",
        "after",
        "timings",
        "cleanup",
        "journal_head_sha256",
        "started_at",
        "completed_at",
    }
    repository = descriptor.get("repository") or {}
    operation_id = descriptor.get("operation_id")
    _require(
        set(report) == report_fields
        and report.get("schema_version") == 1
        and report.get("status") == "passed"
        and report.get("operation_id") == operation_id
        and report.get("target_sha") == repository.get("target_sha")
        and report.get("target_tree") == repository.get("target_tree")
        and report.get("descriptor_sha256") == descriptor_sha256
        and report.get("ready_sha256") == ready_sha256
        and DIGEST_RE.fullmatch(str(report.get("plan_sha256", ""))) is not None
        and report.get("backend_image") == (descriptor.get("images") or {}).get("backend")
        and report.get("postgres_image") == descriptor.get("postgres_restore_image"),
        "rehearsal belongs to another prepared deployment",
    )

    target_manifest = (descriptor.get("migrations") or {}).get("records")
    target_ledger = _database_ledger_projection(target_manifest)
    _require(
        isinstance(target_ledger, list)
        and len(target_ledger) >= 2
        and [entry.get("version") for entry in target_ledger[-2:]]
        == [
            "0014_monomer_md_task_queue_cancel",
            "0015_property_filter_performance",
        ],
        "prepared ledger is not the exact reviewed 0014/0015 target",
    )
    source_ledger = target_ledger[:-2]
    source_fields = {
        "container_id",
        "image_id",
        "restart_count",
        "runtime_sha256",
        "system_identifier",
        "server_version_num",
        "database",
        "user",
        "property_records",
        "ledger",
        "ledger_sha256",
    }
    source_before = report.get("source_before")
    source_after = report.get("source_after")
    restored_before = report.get("restored_before")
    for label, source in (
        ("source-before", source_before),
        ("source-after", source_after),
        ("restored-before", restored_before),
    ):
        _require(
            isinstance(source, dict)
            and set(source) == source_fields
            and CONTAINER_ID_RE.fullmatch(str(source.get("container_id", "")))
            is not None
            and DIGEST_RE.fullmatch(str(source.get("image_id", ""))) is not None
            and DIGEST_RE.fullmatch(str(source.get("runtime_sha256", ""))) is not None
            and isinstance(source.get("restart_count"), int)
            and not isinstance(source.get("restart_count"), bool)
            and source["restart_count"] >= 0
            and SYSTEM_ID_RE.fullmatch(str(source.get("system_identifier", "")))
            is not None
            and re.fullmatch(r"16[0-9]+", str(source.get("server_version_num", "")))
            is not None
            and source.get("property_records") == EXPECTED_PROPERTY_RECORDS
            and source.get("ledger") == source_ledger
            and source.get("ledger_sha256") == _digest(source_ledger),
            f"rehearsal {label} evidence is invalid",
        )
    _require(
        source_before == source_after
        and source_before["database"] == EXPECTED_DATABASE
        and restored_before["database"] == "nexpoly_restore"
        and restored_before["user"] == "postgres"
        and restored_before["system_identifier"] != source_before["system_identifier"]
        and restored_before["image_id"]
        == descriptor["postgres_restore_image"]["image_id"],
        "production changed during rehearsal or restore was not isolated",
    )

    root = Path(runtime_root)
    dump_path = (
        root / "backups" / str(operation_id) / "preflight-rehearsal" / "database.dump"
    )
    dump = report.get("dump")
    _require(
        isinstance(dump, dict)
        and set(dump) == {"path", "sha256", "bytes"}
        and dump.get("path") == str(dump_path)
        and DIGEST_RE.fullmatch(str(dump.get("sha256", ""))) is not None
        and isinstance(dump.get("bytes"), int)
        and not isinstance(dump.get("bytes"), bool)
        and dump["bytes"] > 0,
        "rehearsal dump identity is invalid",
    )
    if verify_runtime:
        try:
            dump_metadata = dump_path.lstat()
        except OSError as exc:
            raise RehearsalError("rehearsal dump is unavailable") from exc
        _require(
            stat.S_ISREG(dump_metadata.st_mode)
            and not dump_path.is_symlink()
            and dump_metadata.st_uid == os.geteuid()
            and dump_metadata.st_nlink == 1
            and stat.S_IMODE(dump_metadata.st_mode) == 0o600
            and dump_metadata.st_size == dump["bytes"]
            and _sha256_file(dump_path) == dump["sha256"],
            "rehearsal dump changed after seal",
        )

    migration = report.get("migrations")
    _require(
        isinstance(migration, dict)
        and set(migration)
        == {
            "duration_seconds",
            "output_sha256",
            "lock_timeout",
            "statement_timeout",
            "records",
        }
        and isinstance(migration.get("duration_seconds"), (int, float))
        and not isinstance(migration.get("duration_seconds"), bool)
        and 0 <= migration["duration_seconds"] <= MIGRATION_LIMIT_SECONDS
        and migration.get("lock_timeout") == "30s"
        and migration.get("statement_timeout") == "15min"
        and DIGEST_RE.fullmatch(str(migration.get("output_sha256", ""))) is not None
        and isinstance(migration.get("records"), list),
        "rehearsal migration evidence is invalid",
    )
    expected_records = [
        {
            "version": entry["version"],
            "status": "skipped" if index < len(source_ledger) else "applied",
            "checksum": entry["checksum"],
        }
        for index, entry in enumerate(target_manifest)
    ]
    _require(
        migration["records"] == expected_records
        and migration["output_sha256"] == _digest(expected_records),
        "rehearsal did not apply exact ordered 0014/0015 migrations",
    )

    timings = report.get("timings")
    _require(
        isinstance(timings, dict)
        and set(timings)
        == {
            "backup_restore_seconds",
            "backup_restore_limit_seconds",
            "migration_limit_seconds",
        }
        and timings.get("backup_restore_limit_seconds")
        == BACKUP_RESTORE_LIMIT_SECONDS
        and timings.get("migration_limit_seconds") == MIGRATION_LIMIT_SECONDS
        and isinstance(timings.get("backup_restore_seconds"), (int, float))
        and not isinstance(timings.get("backup_restore_seconds"), bool)
        and 0 <= timings["backup_restore_seconds"] <= BACKUP_RESTORE_LIMIT_SECONDS,
        "rehearsal timing evidence is invalid",
    )

    after = report.get("after")
    expected_indexes = {
        "standardized": "idx_core_filter_records_standardized_unit_value",
        "raw": "idx_core_filter_records_raw_unit_value_v2",
    }
    _require(
        isinstance(after, dict)
        and set(after)
        == {
            "system_identifier",
            "ledger",
            "property_records",
            "snapshot_count",
            "snapshot",
            "indexes",
            "statistics",
            "query_plans",
        }
        and after.get("system_identifier") == restored_before["system_identifier"]
        and after.get("ledger") == target_ledger
        and after.get("property_records") == EXPECTED_PROPERTY_RECORDS
        and after.get("snapshot_count") == 1,
        "rehearsal post-migration state is invalid",
    )
    snapshot = after.get("snapshot")
    _require(
        isinstance(snapshot, dict)
        and set(snapshot)
        == {
            "snapshot_key",
            "schema_version",
            "generation",
            "total_records",
            "mapped_records",
            "raw_records",
            "option_count",
        }
        and snapshot.get("snapshot_key") == "current"
        and snapshot.get("schema_version") == 1
        and snapshot.get("generation") == 1
        and snapshot.get("total_records") == EXPECTED_PROPERTY_RECORDS
        and isinstance(snapshot.get("mapped_records"), int)
        and not isinstance(snapshot.get("mapped_records"), bool)
        and isinstance(snapshot.get("raw_records"), int)
        and not isinstance(snapshot.get("raw_records"), bool)
        and snapshot["mapped_records"] + snapshot["raw_records"]
        == EXPECTED_PROPERTY_RECORDS
        and isinstance(snapshot.get("option_count"), int)
        and not isinstance(snapshot.get("option_count"), bool)
        and snapshot["option_count"] > 0
        and isinstance(after.get("indexes"), list)
        and all(isinstance(value, str) for value in after["indexes"])
        and set(after["indexes"]) == set(expected_indexes.values())
        and isinstance(after.get("statistics"), list)
        and all(isinstance(value, str) for value in after["statistics"])
        and set(after["statistics"])
        == {
            "stats_core_filter_records_standardized_unit",
            "stats_core_filter_records_raw_unit",
        },
        "rehearsal snapshot/index/statistics evidence is invalid",
    )
    plans = after.get("query_plans")
    _require(isinstance(plans, dict) and set(plans) == set(expected_indexes), "query-plan evidence is invalid")
    for label, expected_index in expected_indexes.items():
        plan = plans[label]
        _require(
            isinstance(plan, dict)
            and set(plan)
            == {
                "default_records_index_names",
                "default_plan_sha256",
                "diagnostic_records_index_names",
                "diagnostic_plan_sha256",
            }
            and isinstance(plan.get("default_records_index_names"), list)
            and all(
                isinstance(value, str)
                for value in plan["default_records_index_names"]
            )
            and isinstance(plan.get("diagnostic_records_index_names"), list)
            and all(
                isinstance(value, str)
                for value in plan["diagnostic_records_index_names"]
            )
            and expected_index in (plan.get("default_records_index_names") or [])
            and expected_index in (plan.get("diagnostic_records_index_names") or [])
            and DIGEST_RE.fullmatch(str(plan.get("default_plan_sha256", "")))
            is not None
            and DIGEST_RE.fullmatch(str(plan.get("diagnostic_plan_sha256", "")))
            is not None,
            f"{label} query plan did not prove the 0015 records index",
        )

    cleanup = report.get("cleanup")
    expected_postgres_name = "nexpoly-rehearsal-" + hashlib.sha256(
        str(operation_id).encode()
    ).hexdigest()[:16]
    expected_migration_name = expected_postgres_name + "-migrate"
    _require(
        isinstance(cleanup, dict)
        and set(cleanup)
        == {
            "postgres_container_name",
            "migration_container_name",
            "postgres_absent",
            "migration_absent",
            "proved_at",
        }
        and cleanup.get("postgres_container_name") == expected_postgres_name
        and cleanup.get("migration_container_name") == expected_migration_name
        and cleanup.get("postgres_absent") is True
        and cleanup.get("migration_absent") is True,
        "rehearsal cleanup evidence is invalid",
    )
    if verify_runtime:
        _container_absent(expected_migration_name, "migration")
        _container_absent(expected_postgres_name, "PostgreSQL rehearsal")

    journal_head = report.get("journal_head_sha256")
    _require(DIGEST_RE.fullmatch(str(journal_head or "")) is not None, "journal head is invalid")
    if verify_runtime:
        journal_root = (
            root / "audit" / "deployment-rehearsals" / str(operation_id) / "journal"
        )
        _private_directory(journal_root, create=False)
        intent_sealed, intent_sha = _load_journal_record(
            journal_root / "intent.json",
            phase="intent",
            previous_sha256=None,
        )
        intent_payload = intent_sealed["record"]["payload"]
        intent_authority = intent_payload.get("authority")
        intent_confirmations = (
            intent_authority.get("confirmations")
            if isinstance(intent_authority, dict)
            else None
        )
        _require(
            set(intent_payload) == {"authority", "started_at"}
            and isinstance(intent_authority, dict)
            and set(intent_authority)
            == {
                "operation_id",
                "target_sha",
                "target_tree",
                "descriptor_sha256",
                "ready_sha256",
                "plan_sha256",
                "confirmations",
                "source",
            }
            and intent_authority.get("operation_id") == operation_id
            and intent_authority.get("target_sha") == report["target_sha"]
            and intent_authority.get("target_tree") == report["target_tree"]
            and intent_authority.get("descriptor_sha256") == descriptor_sha256
            and intent_authority.get("ready_sha256") == ready_sha256
            and intent_authority.get("plan_sha256") == report["plan_sha256"]
            and intent_authority.get("source") == source_before
            and isinstance(intent_confirmations, dict)
            and intent_confirmations
            == {
                "descriptor_sha256": descriptor_sha256,
                "source_system_identifier": source_before["system_identifier"],
                "source_ledger_sha256": source_before["ledger_sha256"],
                "source_property_records": source_before["property_records"],
                "plan_sha256": report["plan_sha256"],
            }
            and intent_payload.get("started_at") == report["started_at"],
            "intent journal does not bind the report",
        )
        dump_sealed, dump_sha = _load_journal_record(
            journal_root / "dump-sealed.json",
            phase="dump-sealed",
            previous_sha256=intent_sha,
        )
        dump_payload = dump_sealed["record"]["payload"]
        _require(
            set(dump_payload) == {"dump", "backup_seconds"}
            and dump_payload.get("dump") == dump
            and isinstance(dump_payload.get("backup_seconds"), (int, float))
            and not isinstance(dump_payload.get("backup_seconds"), bool)
            and 0 <= dump_payload["backup_seconds"]
            <= timings["backup_restore_seconds"],
            "dump journal does not bind the report",
        )
        cleanup_sealed, cleanup_record_sha = _load_journal_record(
            journal_root / "cleanup-proved.json",
            phase="cleanup-proved",
            previous_sha256=dump_sha,
        )
        _require(
            cleanup_record_sha == journal_head
            and cleanup_sealed["record"].get("payload", {}).get("report_core")
            == {key: value for key, value in report.items() if key != "journal_head_sha256"}
            ,
            "cleanup journal does not bind the report",
        )

    prepared_at = _parse_utc(descriptor.get("prepared_at"), "descriptor prepared_at")
    started_at = _parse_utc(report.get("started_at"), "rehearsal started_at")
    completed_at = _parse_utc(report.get("completed_at"), "rehearsal completed_at")
    proved_at = _parse_utc(cleanup.get("proved_at"), "cleanup proved_at")
    current = now or dt.datetime.now(dt.timezone.utc)
    _require(
        started_at >= prepared_at
        and completed_at >= started_at
        and proved_at == completed_at
        and completed_at <= current
        and (current - completed_at).total_seconds() <= REPORT_MAX_AGE_SECONDS,
        "rehearsal report is stale",
    )
    return {
        "report_sha256": sealed["report_sha256"],
        "completed_at": report["completed_at"],
        "dump_sha256": dump["sha256"],
        "journal_head_sha256": journal_head,
    }


def run_rehearsal(
    *,
    production_root: Path,
    runtime_root: Path,
    operation_id: str,
    target_sha: str,
    confirmations: dict[str, Any],
) -> dict[str, Any]:
    plan = build_plan(
        production_root=production_root,
        runtime_root=runtime_root,
        operation_id=operation_id,
        target_sha=target_sha,
    )
    _require(confirmations == plan["confirmations"], "rehearsal confirmations differ from plan")
    descriptor, descriptor_path, descriptor_sha256, ready_sha256 = _descriptor_authority(
        runtime_root, operation_id, target_sha
    )
    _require(
        descriptor_sha256 == plan["descriptor_sha256"]
        and ready_sha256 == plan["ready_sha256"]
        and descriptor_path == Path(plan["descriptor_path"]),
        "prepared authority changed after rehearsal plan",
    )
    report_directory = runtime_root / "audit/deployment-rehearsals" / operation_id
    backup_directory = runtime_root / "backups" / operation_id / "preflight-rehearsal"
    journal_directory = report_directory / "journal"
    _private_directory(runtime_root / "audit", create=False)
    _private_directory(runtime_root / "backups", create=False)
    _private_directory(report_directory, create=True)
    _private_directory(journal_directory, create=True)
    _private_directory(runtime_root / "backups" / operation_id, create=True)
    _private_directory(backup_directory, create=True)
    report_path = report_directory / "report.json"
    if report_path.exists() or report_path.is_symlink():
        sealed = _load_private_json(report_path)
        validate_rehearsal_report(
            sealed,
            descriptor=descriptor,
            descriptor_sha256=descriptor_sha256,
            ready_sha256=ready_sha256,
            runtime_root=runtime_root,
        )
        return sealed

    intent_path = journal_directory / "intent.json"
    intent_authority = {
        "operation_id": operation_id,
        "target_sha": target_sha,
        "target_tree": plan["target_tree"],
        "descriptor_sha256": descriptor_sha256,
        "ready_sha256": ready_sha256,
        "plan_sha256": plan["confirmations"]["plan_sha256"],
        "confirmations": confirmations,
        "source": plan["source"],
    }
    if intent_path.exists() or intent_path.is_symlink():
        intent_sealed, intent_sha256 = _load_journal_record(
            intent_path, phase="intent", previous_sha256=None
        )
        intent_payload = intent_sealed["record"]["payload"]
        _require(
            set(intent_payload) == {"authority", "started_at"}
            and intent_payload.get("authority") == intent_authority,
            "rehearsal intent differs from the current exact plan",
        )
        _parse_utc(intent_payload.get("started_at"), "rehearsal intent started_at")
        started_at = intent_payload["started_at"]
    else:
        started_at = _utc_now()
        _intent, intent_sha256 = _journal_record(
            intent_path,
            phase="intent",
            previous_sha256=None,
            payload={"authority": intent_authority, "started_at": started_at},
        )

    dump = backup_directory / "database.dump"
    temporary = backup_directory / "database.dump.tmp"
    dump_journal_path = journal_directory / "dump-sealed.json"
    if temporary.exists() or temporary.is_symlink():
        try:
            partial_metadata = temporary.lstat()
        except OSError as exc:
            raise RehearsalError("interrupted dump cannot be inspected") from exc
        _require(
            stat.S_ISREG(partial_metadata.st_mode)
            and not temporary.is_symlink()
            and partial_metadata.st_uid == os.geteuid()
            and partial_metadata.st_nlink == 1
            and stat.S_IMODE(partial_metadata.st_mode) == 0o600,
            "interrupted dump is not an operation-owned private file",
        )
        _unlink_owned_private_file(temporary)

    if dump_journal_path.exists() or dump_journal_path.is_symlink():
        dump_sealed, dump_journal_sha256 = _load_journal_record(
            dump_journal_path,
            phase="dump-sealed",
            previous_sha256=intent_sha256,
        )
        dump_payload = dump_sealed["record"]["payload"]
        _require(
            set(dump_payload) == {"dump", "backup_seconds"}
            and isinstance(dump_payload.get("dump"), dict)
            and dump_payload["dump"].get("path") == str(dump)
            and isinstance(dump_payload.get("backup_seconds"), (int, float))
            and not isinstance(dump_payload.get("backup_seconds"), bool)
            and 0 <= dump_payload["backup_seconds"] <= BACKUP_RESTORE_LIMIT_SECONDS,
            "dump journal is invalid",
        )
        dump_identity = dump_payload["dump"]
        _require(
            dump.exists()
            and not dump.is_symlink()
            and dump.stat().st_size == dump_identity.get("bytes")
            and _sha256_file(dump) == dump_identity.get("sha256"),
            "sealed rehearsal dump changed after interruption",
        )
        backup_seconds = float(dump_payload["backup_seconds"])
    else:
        _require(
            not dump.exists() and not dump.is_symlink(),
            "dump exists without its immutable journal record",
        )
        backup_started = time.monotonic()
        backup_deadline = backup_started + BACKUP_RESTORE_LIMIT_SECONDS
        output_descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(output_descriptor, "wb") as output:
                _run_pg_dump_with_cleanup(
                    container_id=plan["source"]["container_id"],
                    user=plan["source"]["user"],
                    database=EXPECTED_DATABASE,
                    operation_id=operation_id,
                    output=output,
                    timeout=_remaining(
                        backup_deadline, "backup plus restore"
                    ),
                )
                output.flush()
                os.fsync(output.fileno())
            _run(
                ["pg_restore", "--list", str(temporary)],
                timeout=_remaining(backup_deadline, "backup plus restore"),
            )
            os.rename(temporary, dump)
            _fsync_directory(backup_directory)
        except BaseException:
            if temporary.exists() and not temporary.is_symlink():
                with contextlib.suppress(RehearsalError):
                    _unlink_owned_private_file(temporary)
            raise
        backup_seconds = time.monotonic() - backup_started
        dump_identity = {
            "path": str(dump),
            "sha256": _sha256_file(dump),
            "bytes": dump.stat().st_size,
        }
        _dump_record, dump_journal_sha256 = _journal_record(
            dump_journal_path,
            phase="dump-sealed",
            previous_sha256=intent_sha256,
            payload={
                "dump": dump_identity,
                "backup_seconds": backup_seconds,
            },
        )
    dump_digest = str(dump_identity["sha256"])
    minimum_capacity = max(dump.stat().st_size * 8, 8 * 1024**3)
    _require(plan["restore_tmpfs_bytes"] >= minimum_capacity, "restore tmpfs is too small for the dump")
    container_name = "nexpoly-rehearsal-" + hashlib.sha256(operation_id.encode()).hexdigest()[:16]
    migration_name = container_name + "-migrate"
    postgres_image = plan["postgres_image"]
    backend_image = plan["backend_image"]
    cleanup_journal_path = journal_directory / "cleanup-proved.json"
    if cleanup_journal_path.exists() or cleanup_journal_path.is_symlink():
        cleanup_sealed, cleanup_sha256 = _load_journal_record(
            cleanup_journal_path,
            phase="cleanup-proved",
            previous_sha256=dump_journal_sha256,
        )
        report_core = cleanup_sealed["record"]["payload"].get("report_core")
        _require(isinstance(report_core, dict), "cleanup journal omits report evidence")
        sealed = {
            "report": {**report_core, "journal_head_sha256": cleanup_sha256},
        }
        sealed["report_sha256"] = _digest(sealed["report"])
        validate_rehearsal_report(
            sealed,
            descriptor=descriptor,
            descriptor_sha256=descriptor_sha256,
            ready_sha256=ready_sha256,
            runtime_root=runtime_root,
        )
        _write_exclusive(report_path, _canonical_bytes(sealed) + b"\n")
        return sealed

    _cleanup_owned_attempt(
        postgres_name=container_name,
        migration_name=migration_name,
        operation_id=operation_id,
        descriptor_sha256=descriptor_sha256,
        postgres_image=postgres_image,
        backend_image=backend_image,
    )
    remaining_restore_budget = BACKUP_RESTORE_LIMIT_SECONDS - backup_seconds
    _require(remaining_restore_budget > 0, "backup exhausted the 30 minute rehearsal budget")
    restore_started = time.monotonic()
    backup_restore_deadline = restore_started + remaining_restore_budget
    container_id = ""
    migration_id = ""
    restored_before: dict[str, Any] | None = None
    migration_seconds = 0.0
    migration_records: list[dict[str, str]] | None = None
    after: dict[str, Any] | None = None
    try:
        _run(
            [
                "docker",
                "run",
                "--detach",
                "--pull=never",
                "--name",
                container_name,
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                f"/var/lib/postgresql/data:rw,nosuid,nodev,size={plan['restore_tmpfs_bytes']}",
                "--tmpfs",
                "/var/run/postgresql:rw,nosuid,nodev,size=64m",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,noexec,size=256m",
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "--label",
                f"com.nexpoly.rehearsal-operation={operation_id}",
                "--label",
                f"com.nexpoly.rehearsal-descriptor={descriptor_sha256}",
                postgres_image["digest_ref"],
            ],
            timeout=_remaining(backup_restore_deadline, "backup plus restore"),
        )
        owned = _owned_rehearsal_container(
            container_name, operation_id, descriptor_sha256, postgres_image
        )
        _require(owned is not None, "cannot prove rehearsal container startup")
        container_id, postgres_record = owned
        _wait_postgres(container_id, deadline=backup_restore_deadline)
        _run(
            ["docker", "exec", container_id, "createdb", "-U", "postgres", "nexpoly_restore"],
            timeout=_remaining(backup_restore_deadline, "backup plus restore"),
        )
        dump_descriptor = os.open(
            dump, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            dump_before = os.fstat(dump_descriptor)
            _require(
                stat.S_ISREG(dump_before.st_mode)
                and dump_before.st_size == dump_identity["bytes"],
                "sealed dump identity changed before restore",
            )
            with os.fdopen(dump_descriptor, "rb", closefd=False) as source:
                _run(
                    [
                        "docker",
                        "exec",
                        "-i",
                        container_id,
                        "pg_restore",
                        "--exit-on-error",
                        "--no-owner",
                        "--no-acl",
                        "--username",
                        "postgres",
                        "--dbname",
                        "nexpoly_restore",
                    ],
                    text=False,
                    stdin=source,
                    timeout=_remaining(backup_restore_deadline, "backup plus restore"),
                )
            dump_after = os.fstat(dump_descriptor)
            _require(
                dump_before.st_dev == dump_after.st_dev
                and dump_before.st_ino == dump_after.st_ino
                and dump_before.st_size == dump_after.st_size
                and dump_before.st_mtime_ns == dump_after.st_mtime_ns
                and dump_before.st_ctime_ns == dump_after.st_ctime_ns
                and _sha256_file(dump) == dump_identity["sha256"],
                "sealed dump changed during restore",
            )
        finally:
            os.close(dump_descriptor)
        backup_restore_seconds = backup_seconds + (time.monotonic() - restore_started)
        _require(
            backup_restore_seconds <= BACKUP_RESTORE_LIMIT_SECONDS,
            "backup plus isolated restore exceeded 30 minutes",
        )
        restored_before = _source_database_evidence(
            container_id,
            postgres_record,
            "postgres",
            "nexpoly_restore",
        )
        _require(
            restored_before["ledger"] == plan["source"]["ledger"]
            and restored_before["property_records"] == plan["source"]["property_records"],
            "isolated restore differs from the fresh production dump",
        )
        rehearsal_dsn = "postgresql://postgres@127.0.0.1:5432/nexpoly_restore"
        migration_started = time.monotonic()
        migration_deadline = migration_started + MIGRATION_LIMIT_SECONDS
        _run(
            [
                "docker",
                "create",
                "--pull=never",
                "--name",
                migration_name,
                "--network",
                f"container:{container_id}",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,noexec,size=256m",
                "--label",
                f"com.nexpoly.rehearsal-operation={operation_id}",
                "--label",
                f"com.nexpoly.rehearsal-descriptor={descriptor_sha256}",
                "--env",
                f"APP_POSTGRES_DSN={rehearsal_dsn}",
                "--env",
                f"PI_POSTGRES_DSN={rehearsal_dsn}",
                "--env",
                f"LAB_DATA_POSTGRES_DSN={rehearsal_dsn}",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--entrypoint",
                "python",
                backend_image["digest_ref"],
                "-m",
                "app.postgres_migrations",
                "--mode",
                "expand",
                "--dsn",
                rehearsal_dsn,
            ],
            timeout=_remaining(migration_deadline, "0014/0015 migrations"),
        )
        owned_migration = _owned_migration_container(
            migration_name,
            operation_id,
            descriptor_sha256,
            backend_image,
            container_id,
        )
        _require(owned_migration is not None, "cannot prove migration container creation")
        migration_id = owned_migration[0]
        migration = _run(
            ["docker", "start", "--attach", migration_id],
            timeout=_remaining(migration_deadline, "0014/0015 migrations"),
        )
        migration_seconds = time.monotonic() - migration_started
        _require(migration_seconds <= MIGRATION_LIMIT_SECONDS, "0014/0015 exceeded 10 minutes")
        migrated_container = _owned_migration_container(
            migration_name,
            operation_id,
            descriptor_sha256,
            backend_image,
            container_id,
        )
        _require(
            migrated_container is not None
            and migrated_container[0] == migration_id
            and (migrated_container[1].get("State") or {}).get("Running") is False
            and (migrated_container[1].get("State") or {}).get("ExitCode") == 0,
            "migration container did not stop successfully under owned identity",
        )
        migration_records = _parse_migration_records(
            str(migration.stdout),
            descriptor["migrations"]["records"],
            existing_count=len(plan["source"]["ledger"]),
        )
        after = _post_migration_evidence(container_id, plan["source"]["property_records"])
        expected_ledger = _database_ledger_projection(
            descriptor["migrations"]["records"]
        )
        _require(after["ledger"] == expected_ledger, "rehearsed ledger does not equal target 0015")
        _require(
            after["property_records"] == EXPECTED_PROPERTY_RECORDS,
            "production property record count differs from the reviewed baseline",
        )
    finally:
        _cleanup_owned_attempt(
            postgres_name=container_name,
            migration_name=migration_name,
            operation_id=operation_id,
            descriptor_sha256=descriptor_sha256,
            postgres_image=postgres_image,
            backend_image=backend_image,
        )

    _require(
        restored_before is not None
        and migration_records is not None
        and after is not None,
        "rehearsal completed without the full isolated evidence set",
    )
    source_container_id, source_inspect = _live_postgres_container()
    source_after = _source_database_evidence(
        source_container_id,
        source_inspect,
        plan["source"]["user"],
        EXPECTED_DATABASE,
    )
    _require(source_after == plan["source"], "production PostgreSQL changed during rehearsal")
    current_descriptor, current_path, current_descriptor_sha, current_ready_sha = (
        _descriptor_authority(runtime_root, operation_id, target_sha)
    )
    _require(
        current_descriptor == descriptor
        and current_path == descriptor_path
        and current_descriptor_sha == descriptor_sha256
        and current_ready_sha == ready_sha256,
        "prepared authority changed during rehearsal",
    )
    _validate_local_image(descriptor["images"]["backend"], "Backend")
    _validate_local_image(descriptor["postgres_restore_image"], "PostgreSQL 16")
    completed_at = _utc_now()
    cleanup = {
        "postgres_container_name": container_name,
        "migration_container_name": migration_name,
        "postgres_absent": True,
        "migration_absent": True,
        "proved_at": completed_at,
    }
    report_core = {
        "schema_version": 1,
        "status": "passed",
        "operation_id": operation_id,
        "target_sha": target_sha,
        "target_tree": plan["target_tree"],
        "descriptor_sha256": descriptor_sha256,
        "ready_sha256": ready_sha256,
        "plan_sha256": plan["confirmations"]["plan_sha256"],
        "backend_image": backend_image,
        "postgres_image": postgres_image,
        "source_before": plan["source"],
        "source_after": source_after,
        "dump": {
            "path": str(dump),
            "sha256": dump_digest,
            "bytes": dump.stat().st_size,
        },
        "restored_before": restored_before,
        "migrations": {
            "duration_seconds": round(migration_seconds, 3),
            "output_sha256": _digest(migration_records),
            "lock_timeout": "30s",
            "statement_timeout": "15min",
            "records": migration_records,
        },
        "after": after,
        "timings": {
            "backup_restore_seconds": round(backup_restore_seconds, 3),
            "backup_restore_limit_seconds": BACKUP_RESTORE_LIMIT_SECONDS,
            "migration_limit_seconds": MIGRATION_LIMIT_SECONDS,
        },
        "cleanup": cleanup,
        "started_at": started_at,
        "completed_at": completed_at,
    }
    _cleanup_record, cleanup_sha256 = _journal_record(
        cleanup_journal_path,
        phase="cleanup-proved",
        previous_sha256=dump_journal_sha256,
        payload={"report_core": report_core},
    )
    report = {**report_core, "journal_head_sha256": cleanup_sha256}
    sealed = {"report": report, "report_sha256": _digest(report)}
    validate_rehearsal_report(
        sealed,
        descriptor=descriptor,
        descriptor_sha256=descriptor_sha256,
        ready_sha256=ready_sha256,
        runtime_root=runtime_root,
    )
    _write_exclusive(report_path, _canonical_bytes(sealed) + b"\n")
    return sealed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--operation-id", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan", action="store_true")
    action.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-descriptor-sha256")
    parser.add_argument("--confirm-source-system-identifier")
    parser.add_argument("--confirm-source-ledger-sha256")
    parser.add_argument("--confirm-source-property-records", type=int)
    parser.add_argument("--confirm-plan-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    if not sys.flags.isolated:
        print("production-postgres-rehearsal: isolated Python startup is required", file=sys.stderr)
        return 2
    args = _parser().parse_args(argv)
    try:
        with _deploy_lock(RUNTIME_ROOT):
            if args.plan:
                _require(
                    all(
                        value is None
                        for value in (
                            args.confirm_descriptor_sha256,
                            args.confirm_source_system_identifier,
                            args.confirm_source_ledger_sha256,
                            args.confirm_source_property_records,
                            args.confirm_plan_sha256,
                        )
                    ),
                    "read-only plan does not accept confirmations",
                )
                result = build_plan(
                    production_root=PRODUCTION_ROOT,
                    runtime_root=RUNTIME_ROOT,
                    operation_id=args.operation_id,
                    target_sha=args.sha,
                )
            else:
                confirmations = {
                    "descriptor_sha256": args.confirm_descriptor_sha256,
                    "source_system_identifier": args.confirm_source_system_identifier,
                    "source_ledger_sha256": args.confirm_source_ledger_sha256,
                    "source_property_records": args.confirm_source_property_records,
                    "plan_sha256": args.confirm_plan_sha256,
                }
                _require(
                    DIGEST_RE.fullmatch(str(args.confirm_descriptor_sha256 or "")) is not None
                    and SYSTEM_ID_RE.fullmatch(str(args.confirm_source_system_identifier or "")) is not None
                    and DIGEST_RE.fullmatch(str(args.confirm_source_ledger_sha256 or "")) is not None
                    and isinstance(args.confirm_source_property_records, int)
                    and not isinstance(args.confirm_source_property_records, bool)
                    and DIGEST_RE.fullmatch(str(args.confirm_plan_sha256 or "")) is not None,
                    "apply requires every exact plan confirmation",
                )
                result = run_rehearsal(
                    production_root=PRODUCTION_ROOT,
                    runtime_root=RUNTIME_ROOT,
                    operation_id=args.operation_id,
                    target_sha=args.sha,
                    confirmations=confirmations,
                )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (RehearsalError, OSError, UnicodeError) as exc:
        print(f"production-postgres-rehearsal: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
