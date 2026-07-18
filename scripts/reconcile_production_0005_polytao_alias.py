#!/usr/bin/python3 -I
"""Remove one audited duplicate production migration-ledger alias.

This is intentionally not a general migration or ledger editor.  Its sole
write is a compare-and-swap delete of the historical ``0005_polytao_jobs``
alias after the exact production database, archive, backup and isolated restore
have been proven.  All operator-controlled selectors other than an operation ID
are forbidden.
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
import secrets
import select
import stat
import subprocess
import sys
import time
from typing import Any, BinaryIO, Callable, Mapping, Protocol
import urllib.parse

sys.dont_write_bytecode = True


def _load_git_source_trust() -> Any:
    module_name = "nexpoly_alias_git_source_trust"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = Path(__file__).with_name("git_source_trust.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Git source trust policy cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


GIT_SOURCE_TRUST = _load_git_source_trust()

PRODUCTION_ROOT = Path("/data/lzq/gith/nexpoly")
RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
STATE_ROOT_RELATIVE = Path("state/maintenance/0005-polytao-alias")
AUDIT_ROOT_RELATIVE = Path("audit/maintenance/0005-polytao-alias")
BACKUP_ROOT_RELATIVE = Path("backups/maintenance/0005-polytao-alias")
DEPLOY_LOCK_RELATIVE = Path("state/deploy.lock")
DEPLOY_MARKER_RELATIVE = Path("state/deploy-in-progress.json")
CONTRACT_MARKER_RELATIVE = Path("state/contract-0012-in-progress.json")
CONTROL_MANIFEST_NAME = "CONTROL-MANIFEST.json"

DATABASE_NAME = "nexpoly"
DATABASE_OWNER = "polyprop"
DATABASE_USER = "polyprop"
DATABASE_HOST = "127.0.0.1"
DATABASE_PORT = 55432
DATABASE_SSLMODE = "disable"
SYSTEM_IDENTIFIER = "7659245354718314530"
LEGACY_SOURCE_SHA = "b875829c3f008b5ee733d8ffced3093e4cbb07c5"
LEGACY_SOURCE_TREE = "4f68c10a39c6943f7ff13af33d547ebb8f5d7a00"

ALIAS_VERSION = "0005_polytao_jobs"
ALIAS_CHECKSUM = (
    "b15268a475e8daf8dd58be988a228a0440e59a31dbf11d5d6b52e0974c3daab5"
)
ALIAS_APPLIED_AT = "2026-07-08T03:44:05.662979Z"
CANONICAL_VERSION = "0007_polytao_jobs"

CANONICAL_LEDGER = [
    (
        "0001_app_data_governance",
        "d5fc9f3d063f1cba476834f3530519b7970cd54f3c3711d05aba1f1cb2fd34f9",
    ),
    (
        "0002_lab_identity_defaults",
        "580ed6dc7c34970aabd662bc47765e9d02446c28aea1c4fa8fb2a99f05b1ac2f",
    ),
    (
        "0003_runtime_postgres_cutover",
        "0888ac9abd1b6b642f0addd42274b5408981a26c27f1140b7b656ff34ad73ce3",
    ),
    (
        "0004_monomer_md_jobs",
        "b3ad64728f399f42b2bf9edb47ad035ac70f09fce6ced48e7b422ea74d5a7e8e",
    ),
    (
        "0005_byteff2_formal_monomer_md",
        "c9ec808c50915b82a696ab482ed676c62bc75f00a9af21baf9e7f66b185bacb5",
    ),
    (
        "0006_property_filter_records",
        "57b103dc656334cf5e52bdc9512576a303ae0044ec5fb64eb7cba802021eceaa",
    ),
    (CANONICAL_VERSION, ALIAS_CHECKSUM),
    (
        "0008_polytao_backend_runtime",
        "d0d8b2187aad8657269600873d3d2630e30c7d72da2f6662e18ab22031deff90",
    ),
]
PRE_LEDGER = sorted([*CANONICAL_LEDGER, (ALIAS_VERSION, ALIAS_CHECKSUM)])
POST_LEDGER = sorted(CANONICAL_LEDGER)

EXPECTED_SCHEMA_SHA256 = (
    "8594868c661024af0766627a2d48280fc6967b8efe445878fc2a252a4520000c"
)
EXPECTED_LEDGER_SCHEMA_SHA256 = (
    "db77ff078329ed4ec8b00f70172be743b9f3e67924d27716fba26277466ecfdd"
)
EXPECTED_STRUCTURE_COUNTS = {
    "columns": 23,
    "indexes": 3,
    "constraints": 6,
    "triggers": 0,
}
EXPECTED_LEDGER_STRUCTURE_COUNTS = {
    "columns": 3,
    "indexes": 1,
    "constraints": 1,
    "triggers": 0,
}

POSTGRES16_IMAGE = (
    "postgres:16-alpine@sha256:"
    "57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
)
POSTGRES16_IMAGE_ENV = [
    "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "GOSU_VERSION=1.19",
    "LANG=en_US.utf8",
    "PG_MAJOR=16",
    "PG_VERSION=16.14",
    "PG_SHA256=f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471",
    "DOCKER_PG_LLVM_DEPS=llvm21-dev \t\tclang21",
    "PGDATA=/var/lib/postgresql/data",
]
RESTORE_TMPFS = {
    "/tmp": "rw,nosuid,nodev,noexec,mode=1777",
    "/var/lib/postgresql/data": "rw,nosuid,nodev,mode=0700",
    "/var/run/postgresql": "rw,nosuid,nodev,mode=0770",
}
DOCKER = Path("/usr/bin/docker")
GIT = Path("/usr/bin/git")
SYSTEMCTL = Path("/usr/bin/systemctl")
PG_BIN = Path("/usr/lib/postgresql/16/bin")
BINARY_SHA256 = {
    str(DOCKER): "4fd41ecac3ec0f8a6067f8da79cbce516ed0572d1081e4767db764c84d32257b",
    str(GIT): "2a8c18fbf43da9f692d75474c72bea9dfd796c260b0f3dfe456376abc3bbd668",
    str(SYSTEMCTL): "7ba82b5ba146759c710e1b80fadaa3fdbc0f9b85c8fb2c8c3196b7b1a0037ef8",
    str(PG_BIN / "psql"): "6d593ef8e95e5275691fcc28927cc540282db141ca1ec5e3806e7db5523613cb",
    str(PG_BIN / "pg_dump"): "864ecb96b747c5e47c2b232376fee9b2768810e5d552db9b6f50c0ad75a5977a",
    str(PG_BIN / "pg_restore"): "13fc1bc1743ac1e45bd6b5d0178e73c65e99958cddf61e38ba265d481bf45595",
}
ADVISORY_LOCK_KEY = 5_977_005_007
OPERATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_ID_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_PSQL_BLOCK_BYTES = MAX_JSON_BYTES + 64 * 1024
PSQL_BLOCK_TIMEOUT_SECONDS = 31 * 60
CONTROL_ENVIRONMENT = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/home/devuser",
    "USER": "devuser",
    "LOGNAME": "devuser",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "XDG_RUNTIME_DIR": "/run/user/1001",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1001/bus",
}
MARKER_PHASES = (
    "directory-intent",
    "planned",
    "runtime-fenced",
    "locked-preverified",
    "backup-started",
    "backup-complete",
    "restore-started",
    "restore-verified",
    "mutation-intent",
    "mutation-commit-started",
    "mutation-committed",
    "completed",
)
MARKER_PHASE_INDEX = {phase: index for index, phase in enumerate(MARKER_PHASES)}
AUDIT_EVIDENCE_NAMES = {
    "pg-restore.list",
    "isolated-postgres16-restore.json",
    "database-after.json",
    "AUDIT-MANIFEST.json",
}
BACKUP_EVIDENCE_NAMES = {
    "nexpoly-before.dump",
    "nexpoly-before.dump.sha256",
}


def _load_pull_deploy_controller() -> Any:
    """Load the exact manifest-sealed sibling without ambient import paths."""

    module_name = "nexpoly_alias_pull_deploy_controller"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    current = Path(__file__).absolute()
    parent = current.parent
    path = parent / "pull_deploy_controller.py"
    try:
        current_metadata = current.lstat()
        parent_metadata = parent.lstat()
        metadata = path.lstat()
    except OSError as exc:
        raise ReconcileError(
            "prepared bridge controller sibling is unavailable"
        ) from exc
    if (
        not stat.S_ISREG(current_metadata.st_mode)
        or current.is_symlink()
        or current_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(current_metadata.st_mode) != 0o700
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent.is_symlink()
        or parent_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
        or not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ReconcileError(
            "prepared bridge controller sibling is unsafe"
        )
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ReconcileError("cannot load prepared bridge controller")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


class ReconcileError(RuntimeError):
    """The one-purpose maintenance contract failed closed."""


class CommandRunner(Protocol):
    def run(
        self,
        command: list[str],
        *,
        env: Mapping[str, str] | None = None,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | int | None = subprocess.PIPE,
        text: bool = True,
        timeout: float | None = None,
        check: bool = True,
        umask: int = -1,
    ) -> subprocess.CompletedProcess[Any]: ...


class SystemRunner:
    def run(
        self,
        command: list[str],
        *,
        env: Mapping[str, str] | None = None,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | int | None = subprocess.PIPE,
        text: bool = True,
        timeout: float | None = None,
        check: bool = True,
        umask: int = -1,
    ) -> subprocess.CompletedProcess[Any]:
        return subprocess.run(
            command,
            env=None if env is None else dict(env),
            stdin=stdin,
            stdout=stdout,
            stderr=subprocess.PIPE,
            text=text,
            timeout=timeout,
            check=check,
            umask=umask,
        )


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def require_private_directory(path: Path, *, create: bool = False) -> None:
    if create:
        try:
            path.mkdir(mode=0o700)
            fsync_directory(path.parent)
        except FileExistsError:
            pass
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ReconcileError(f"private directory is missing: {path}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ReconcileError(f"private directory identity is unsafe: {path}")


def require_private_file(path: Path, *, mode: int = 0o600) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ReconcileError(f"private file is missing: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise ReconcileError(f"private file identity is unsafe: {path}")
    return metadata


def atomic_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    require_private_directory(path.parent)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        fsync_directory(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def atomic_json(path: Path, document: dict[str, Any]) -> None:
    atomic_bytes(path, canonical_json_bytes(document) + b"\n")


def load_private_json(path: Path) -> dict[str, Any]:
    metadata = require_private_file(path)
    if metadata.st_size > MAX_JSON_BYTES:
        raise ReconcileError(f"private JSON file is too large: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReconcileError(f"private JSON file is invalid: {path}") from exc
    if not isinstance(document, dict):
        raise ReconcileError(f"private JSON file is not an object: {path}")
    return document


def unlink_private(path: Path) -> None:
    require_private_file(path)
    path.unlink()
    fsync_directory(path.parent)


def safe_operation_id(value: str) -> str:
    if OPERATION_ID_RE.fullmatch(value) is None:
        raise ReconcileError("operation ID must be 8-128 lowercase safe characters")
    return value


def _regular_root_binary(path: Path, expected_sha256: str) -> dict[str, object]:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ReconcileError(f"reviewed maintenance binary is missing: {path}") from exc
    if (
        path != resolved
        or not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != 0
        or metadata.st_mode & 0o022
    ):
        raise ReconcileError(f"reviewed maintenance binary is unsafe: {path}")
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise ReconcileError(f"reviewed maintenance binary hash differs: {path}")
    return {
        "path": str(path),
        "uid": metadata.st_uid,
        "mode": stat.S_IMODE(metadata.st_mode),
        "size": metadata.st_size,
        "sha256": actual,
    }


def binary_inventory(runner: CommandRunner) -> dict[str, dict[str, object]]:
    result = {
        path: _regular_root_binary(Path(path), expected)
        for path, expected in sorted(BINARY_SHA256.items())
    }
    for name in ("psql", "pg_dump", "pg_restore"):
        path = str(PG_BIN / name)
        try:
            completed = runner.run(
                [path, "--version"], env=CONTROL_ENVIRONMENT, timeout=10
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReconcileError(f"cannot execute reviewed PostgreSQL client: {name}") from exc
        version = str(completed.stdout).strip()
        if "PostgreSQL) 16." not in version:
            raise ReconcileError(f"reviewed PostgreSQL client is not major 16: {name}")
        result[path]["version"] = version
    return result


def _parse_dsn(value: str) -> tuple[dict[str, str], dict[str, object]]:
    if (
        not value
        or len(value) > 8192
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise ReconcileError("production PostgreSQL DSN is unavailable or malformed")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ReconcileError("production PostgreSQL DSN is malformed") from exc
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not parsed.hostname
        or parsed.username is None
        or parsed.password is None
        or parsed.fragment
        or parsed.path != f"/{DATABASE_NAME}"
    ):
        raise ReconcileError("production PostgreSQL DSN identity is not allowed")
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if set(query) != {"sslmode"} or any(
        len(values) != 1 for values in query.values()
    ):
        raise ReconcileError("production PostgreSQL DSN options are not allowed")
    sslmode = query["sslmode"][0]
    username = urllib.parse.unquote(parsed.username)
    password = urllib.parse.unquote(parsed.password)
    host = parsed.hostname
    if (
        username != DATABASE_USER
        or host != DATABASE_HOST
        or port != DATABASE_PORT
        or sslmode != DATABASE_SSLMODE
        or not password
        or any(character in username + password + host for character in ("\x00", "\r", "\n"))
    ):
        raise ReconcileError("production PostgreSQL DSN credentials are malformed")
    environment = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/home/devuser",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PGHOST": host,
        "PGPORT": str(DATABASE_PORT),
        "PGDATABASE": DATABASE_NAME,
        "PGUSER": username,
        "PGPASSWORD": password,
        "PGSSLMODE": sslmode,
        "PGCONNECT_TIMEOUT": "10",
        "PGAPPNAME": "nexpoly-reconcile-production-0005-alias",
        "PGOPTIONS": "-c search_path=pg_catalog",
    }
    public = {
        "host_sha256": sha256_bytes(host.encode("utf-8")),
        "port": DATABASE_PORT,
        "database": DATABASE_NAME,
        "user": username,
        "sslmode": sslmode,
    }
    return environment, public


def _run_checked(
    runner: CommandRunner,
    command: list[str],
    *,
    label: str,
    env: Mapping[str, str] | None = None,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | int | None = subprocess.PIPE,
    text: bool = True,
    timeout: float | None = None,
    umask: int = -1,
) -> subprocess.CompletedProcess[Any]:
    try:
        options: dict[str, Any] = {
            "env": env,
            "stdin": stdin,
            "stdout": stdout,
            "text": text,
            "timeout": timeout,
        }
        if umask != -1:
            options["umask"] = umask
        return runner.run(command, **options)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReconcileError(f"{label} failed") from exc


def _require_local_docker_endpoint(runner: CommandRunner) -> None:
    completed = _run_checked(
        runner,
        [
            str(DOCKER),
            "context",
            "inspect",
            "--format",
            "{{json .Endpoints.docker.Host}}",
        ],
        label="local Docker endpoint identity",
        env=CONTROL_ENVIRONMENT,
        timeout=30,
    )
    lines = [line for line in str(completed.stdout).splitlines() if line.strip()]
    try:
        endpoint = json.loads(lines[0]) if len(lines) == 1 else None
    except json.JSONDecodeError as exc:
        raise ReconcileError("local Docker endpoint identity is malformed") from exc
    if endpoint != "unix:///var/run/docker.sock":
        raise ReconcileError("maintenance requires the local Docker Unix socket")


def _psql_json(
    runner: CommandRunner,
    environment: Mapping[str, str],
    sql: str,
    *,
    read_only: bool = True,
) -> dict[str, Any]:
    command = [
        str(PG_BIN / "psql"),
        "-X",
        "--no-psqlrc",
        "--quiet",
        "--no-align",
        "--tuples-only",
        "--set",
        "ON_ERROR_STOP=1",
        "--command",
        ("BEGIN READ ONLY; " if read_only else "")
        + sql.rstrip().rstrip(";")
        + ("; COMMIT;" if read_only else ";"),
    ]
    completed = _run_checked(
        runner,
        command,
        label="production PostgreSQL inventory",
        env=environment,
        timeout=60,
    )
    lines = [line for line in str(completed.stdout).splitlines() if line.strip()]
    if len(lines) != 1 or len(lines[0].encode("utf-8")) > MAX_JSON_BYTES:
        raise ReconcileError("production PostgreSQL inventory output is malformed")
    try:
        document = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise ReconcileError("production PostgreSQL inventory is not JSON") from exc
    if not isinstance(document, dict):
        raise ReconcileError("production PostgreSQL inventory is not an object")
    return document


INVENTORY_SQL = r"""
SELECT json_build_object(
  'database', current_database(),
  'current_user', current_user,
  'database_owner', (
    SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()
  ),
  'server_version_num', current_setting('server_version_num')::integer,
  'in_recovery', pg_is_in_recovery(),
  'system_identifier', (SELECT system_identifier::text FROM pg_control_system()),
  'transaction_read_only', current_setting('transaction_read_only'),
  'ledger', COALESCE((
    SELECT json_agg(json_build_object(
      'version', version,
      'checksum', checksum,
      'applied_at', to_char(applied_at AT TIME ZONE 'UTC',
        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
    ) ORDER BY version, checksum)
    FROM governance.schema_migrations
  ), '[]'::json),
  'rows', COALESCE((
    SELECT json_agg(to_jsonb(jobs) ORDER BY job_id::text)
    FROM generation.polytao_jobs AS jobs
  ), '[]'::json),
  'status_counts', COALESCE((
    SELECT json_object_agg(status, count)
    FROM (
      SELECT status, COUNT(*) AS count
      FROM generation.polytao_jobs GROUP BY status ORDER BY status
    ) AS counts
  ), '{}'::json),
  'columns', COALESCE((
    SELECT json_agg(row_to_json(records) ORDER BY ordinal_position)
    FROM (
      SELECT column_name, ordinal_position, data_type, udt_schema, udt_name,
             is_nullable, column_default
      FROM information_schema.columns
      WHERE table_schema='generation' AND table_name='polytao_jobs'
    ) AS records
  ), '[]'::json),
  'indexes', COALESCE((
    SELECT json_agg(row_to_json(records) ORDER BY indexname)
    FROM (
      SELECT indexname, indexdef FROM pg_indexes
      WHERE schemaname='generation' AND tablename='polytao_jobs'
    ) AS records
  ), '[]'::json),
  'constraints', COALESCE((
    SELECT json_agg(row_to_json(records) ORDER BY name)
    FROM (
      SELECT c.conname AS name, c.contype AS type,
             c.condeferrable AS deferrable,
             c.condeferred AS initially_deferred,
             c.convalidated AS validated,
             pg_get_constraintdef(c.oid, true) AS definition
      FROM pg_constraint c
      JOIN pg_class r ON r.oid=c.conrelid
      JOIN pg_namespace n ON n.oid=r.relnamespace
      WHERE n.nspname='generation' AND r.relname='polytao_jobs'
    ) AS records
  ), '[]'::json),
  'triggers', COALESCE((
    SELECT json_agg(row_to_json(records) ORDER BY name)
    FROM (
      SELECT t.tgname AS name, t.tgenabled AS enabled,
             pg_get_triggerdef(t.oid, true) AS definition
      FROM pg_trigger t
      JOIN pg_class r ON r.oid=t.tgrelid
      JOIN pg_namespace n ON n.oid=r.relnamespace
      WHERE n.nspname='generation' AND r.relname='polytao_jobs'
        AND NOT t.tgisinternal
    ) AS records
  ), '[]'::json),
  'ledger_columns', COALESCE((
    SELECT json_agg(row_to_json(records) ORDER BY ordinal_position)
    FROM (
      SELECT column_name, ordinal_position, data_type, udt_schema, udt_name,
             is_nullable, column_default
      FROM information_schema.columns
      WHERE table_schema='governance' AND table_name='schema_migrations'
    ) AS records
  ), '[]'::json),
  'ledger_indexes', COALESCE((
    SELECT json_agg(row_to_json(records) ORDER BY indexname)
    FROM (
      SELECT indexname, indexdef FROM pg_indexes
      WHERE schemaname='governance' AND tablename='schema_migrations'
    ) AS records
  ), '[]'::json),
  'ledger_constraints', COALESCE((
    SELECT json_agg(row_to_json(records) ORDER BY name)
    FROM (
      SELECT c.conname AS name, c.contype AS type,
             c.condeferrable AS deferrable,
             c.condeferred AS initially_deferred,
             c.convalidated AS validated,
             pg_get_constraintdef(c.oid, true) AS definition
      FROM pg_constraint c
      JOIN pg_class r ON r.oid=c.conrelid
      JOIN pg_namespace n ON n.oid=r.relnamespace
      WHERE n.nspname='governance' AND r.relname='schema_migrations'
    ) AS records
  ), '[]'::json),
  'ledger_triggers', COALESCE((
    SELECT json_agg(row_to_json(records) ORDER BY name)
    FROM (
      SELECT t.tgname AS name, t.tgenabled AS enabled,
             pg_get_triggerdef(t.oid, true) AS definition
      FROM pg_trigger t
      JOIN pg_class r ON r.oid=t.tgrelid
      JOIN pg_namespace n ON n.oid=r.relnamespace
      WHERE n.nspname='governance' AND r.relname='schema_migrations'
        AND NOT t.tgisinternal
    ) AS records
  ), '[]'::json),
  'polytao_relation', (
    SELECT json_build_object(
      'kind', r.relkind,
      'persistence', r.relpersistence,
      'is_partition', r.relispartition,
      'row_security', r.relrowsecurity,
      'force_row_security', r.relforcerowsecurity,
      'owner', pg_get_userbyid(r.relowner),
      'parents', (SELECT COUNT(*) FROM pg_inherits WHERE inhrelid=r.oid),
      'children', (SELECT COUNT(*) FROM pg_inherits WHERE inhparent=r.oid)
    )
    FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace
    WHERE n.nspname='generation' AND r.relname='polytao_jobs'
  ),
  'ledger_relation', (
    SELECT json_build_object(
      'kind', r.relkind,
      'persistence', r.relpersistence,
      'is_partition', r.relispartition,
      'row_security', r.relrowsecurity,
      'force_row_security', r.relforcerowsecurity,
      'owner', pg_get_userbyid(r.relowner),
      'parents', (SELECT COUNT(*) FROM pg_inherits WHERE inhrelid=r.oid),
      'children', (SELECT COUNT(*) FROM pg_inherits WHERE inhparent=r.oid)
    )
    FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace
    WHERE n.nspname='governance' AND r.relname='schema_migrations'
  )
)::text
"""


def _structure(document: Mapping[str, Any], *, prefix: str = "") -> dict[str, Any]:
    return {
        "columns": document[f"{prefix}columns"],
        "indexes": document[f"{prefix}indexes"],
        "constraints": document[f"{prefix}constraints"],
        "triggers": document[f"{prefix}triggers"],
    }


def _public_inventory(document: Mapping[str, Any]) -> dict[str, Any]:
    structure = _structure(document)
    ledger_structure = _structure(document, prefix="ledger_")
    ledger = [
        {
            "version": row["version"],
            "checksum": row["checksum"],
            "applied_at": row["applied_at"],
        }
        for row in document["ledger"]
    ]
    return {
        "database": document["database"],
        "current_user": document["current_user"],
        "database_owner": document["database_owner"],
        "server_version_num": document["server_version_num"],
        "in_recovery": document["in_recovery"],
        "system_identifier": document["system_identifier"],
        "ledger": ledger,
        "archive": {
            "row_count": len(document["rows"]),
            "status_counts": document["status_counts"],
            "rows_sha256": sha256_bytes(canonical_json_bytes(document["rows"])),
            "schema_sha256": sha256_bytes(canonical_json_bytes(structure)),
            "structure_counts": {key: len(value) for key, value in structure.items()},
        },
        "ledger_schema_sha256": sha256_bytes(canonical_json_bytes(ledger_structure)),
        "ledger_structure_counts": {
            key: len(value) for key, value in ledger_structure.items()
        },
        "polytao_relation": document["polytao_relation"],
        "ledger_relation": document["ledger_relation"],
    }


def _validate_dynamic_archive(archive: object) -> dict[str, Any]:
    if not isinstance(archive, dict) or set(archive) != {
        "row_count",
        "status_counts",
        "rows_sha256",
        "schema_sha256",
        "structure_counts",
    }:
        raise ReconcileError("production PolyTAO archive shape is invalid")
    row_count = archive.get("row_count")
    status_counts = archive.get("status_counts")
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count < 0
        or not isinstance(status_counts, dict)
        or any(
            not isinstance(status, str)
            or not status
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            for status, count in status_counts.items()
        )
        or sum(status_counts.values()) != row_count
        or not isinstance(archive.get("rows_sha256"), str)
        or HEX_DIGEST_RE.fullmatch(archive["rows_sha256"]) is None
    ):
        raise ReconcileError("production PolyTAO business snapshot is invalid")
    if (
        archive.get("schema_sha256") != EXPECTED_SCHEMA_SHA256
        or archive.get("structure_counts") != EXPECTED_STRUCTURE_COUNTS
    ):
        raise ReconcileError("production PolyTAO schema identity differs")
    return dict(archive)


def validate_inventory(
    document: Mapping[str, Any],
    *,
    expected_phase: str,
    restored: bool = False,
) -> dict[str, Any]:
    try:
        public = _public_inventory(document)
    except (KeyError, TypeError, ValueError) as exc:
        raise ReconcileError("database inventory shape is invalid") from exc
    if expected_phase not in {"pre", "post"}:
        raise ReconcileError("internal ledger phase is invalid")
    expected_ledger = PRE_LEDGER if expected_phase == "pre" else POST_LEDGER
    observed_ledger = [
        (str(row["version"]), str(row["checksum"])) for row in public["ledger"]
    ]
    if observed_ledger != expected_ledger:
        raise ReconcileError(f"database ledger is not the exact {expected_phase} state")
    if expected_phase == "pre":
        aliases = [row for row in public["ledger"] if row["version"] == ALIAS_VERSION]
        if len(aliases) != 1 or aliases[0]["applied_at"] != ALIAS_APPLIED_AT:
            raise ReconcileError("production alias tuple differs from reviewed evidence")
    public["archive"] = _validate_dynamic_archive(public["archive"])
    if (
        public["ledger_schema_sha256"] != EXPECTED_LEDGER_SCHEMA_SHA256
        or public["ledger_structure_counts"] != EXPECTED_LEDGER_STRUCTURE_COUNTS
    ):
        raise ReconcileError("production migration-ledger schema differs")
    expected_relation = {
        "kind": "r",
        "persistence": "p",
        "is_partition": False,
        "row_security": False,
        "force_row_security": False,
        "parents": 0,
        "children": 0,
    }
    for label in ("polytao_relation", "ledger_relation"):
        relation = public[label]
        if not isinstance(relation, dict) or any(
            relation.get(key) != value for key, value in expected_relation.items()
        ):
            raise ReconcileError(f"database {label} identity differs")
        expected_owner = "postgres" if restored else DATABASE_OWNER
        if relation.get("owner") != expected_owner:
            raise ReconcileError(f"database {label} owner differs")
    if not restored:
        if (
            public["database"] != DATABASE_NAME
            or public["current_user"] != DATABASE_USER
            or public["database_owner"] != DATABASE_OWNER
            or str(public["system_identifier"]) != SYSTEM_IDENTIFIER
            or public["in_recovery"] is not False
            or not 160000 <= int(public["server_version_num"]) < 170000
        ):
            raise ReconcileError("production PostgreSQL cluster identity differs")
    else:
        if (
            public["database"] != "nexpoly_alias_restore"
            or public["current_user"] != "postgres"
            or public["database_owner"] != "postgres"
            or public["in_recovery"] is not False
            or not 160000 <= int(public["server_version_num"]) < 170000
        ):
            raise ReconcileError("isolated PostgreSQL restore identity differs")
    return public


def _require_post_matches_before(
    before: object, after: object
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ReconcileError("database before/after evidence is malformed")
    before_ledger = before.get("ledger")
    expected_after_ledger = (
        [
            row
            for row in before_ledger
            if isinstance(row, dict) and row.get("version") != ALIAS_VERSION
        ]
        if isinstance(before_ledger, list)
        else None
    )
    if (
        expected_after_ledger is None
        or len(expected_after_ledger) + 1 != len(before_ledger)
        or after.get("ledger") != expected_after_ledger
        or set(before) != set(after)
        or any(
            before.get(key) != after.get(key) for key in before if key != "ledger"
        )
    ):
        raise ReconcileError(
            "post-alias database differs from the locked business snapshot"
        )
    return before, after


def _require_restore_matches_before(
    before: object, restored: object
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(before, dict) or not isinstance(restored, dict):
        raise ReconcileError("isolated restore database evidence is malformed")
    if (
        restored.get("database") != "nexpoly_alias_restore"
        or restored.get("current_user") != "postgres"
        or restored.get("database_owner") != "postgres"
        or restored.get("in_recovery") is not False
        or isinstance(restored.get("server_version_num"), bool)
        or not isinstance(restored.get("server_version_num"), int)
        or not 160000 <= restored["server_version_num"] < 170000
        or not isinstance(restored.get("system_identifier"), str)
        or not restored["system_identifier"].isdigit()
        or restored.get("ledger") != before.get("ledger")
        or restored.get("archive") != before.get("archive")
        or restored.get("ledger_schema_sha256")
        != before.get("ledger_schema_sha256")
        or restored.get("ledger_structure_counts")
        != before.get("ledger_structure_counts")
    ):
        raise ReconcileError("isolated restore differs from the locked database snapshot")
    for relation_name in ("polytao_relation", "ledger_relation"):
        source_relation = before.get(relation_name)
        restored_relation = restored.get(relation_name)
        if (
            not isinstance(source_relation, dict)
            or not isinstance(restored_relation, dict)
            or restored_relation != {**source_relation, "owner": "postgres"}
        ):
            raise ReconcileError(
                "isolated restore relation differs from the locked database snapshot"
            )
    return before, restored


def _source_identity(runner: CommandRunner) -> dict[str, Any]:
    try:
        permission_takeover = (
            GIT_SOURCE_TRUST.verify_repository_permission_takeover(
                PRODUCTION_ROOT,
                GIT_SOURCE_TRUST.permission_takeover_marker_path(
                    RUNTIME_ROOT
                ),
                verify_content=True,
            )
        )
    except Exception as exc:
        raise ReconcileError(
            "legacy production Git permission takeover is unavailable"
        ) from exc
    try:
        metadata = PRODUCTION_ROOT.lstat()
    except OSError as exc:
        raise ReconcileError("legacy production checkout is missing") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or PRODUCTION_ROOT.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        raise ReconcileError("legacy production checkout permissions are unsafe")
    try:
        preflight = GIT_SOURCE_TRUST.repository_preflight_evidence(
            PRODUCTION_ROOT,
            ambient=os.environ,
        )
        git_environment = GIT_SOURCE_TRUST.safe_git_environment(
            PRODUCTION_ROOT,
            ambient=os.environ,
        )
    except Exception as exc:
        raise ReconcileError("legacy production Git trust preflight failed") from exc
    completed = _run_checked(
        runner,
        GIT_SOURCE_TRUST.safe_git_command(
            PRODUCTION_ROOT,
            "rev-parse",
            "HEAD",
            "HEAD^{tree}",
            executable=str(GIT),
        ),
        label="legacy source identity",
        env=git_environment,
        timeout=30,
        umask=0o077,
    )
    lines = str(completed.stdout).splitlines()
    if lines != [LEGACY_SOURCE_SHA, LEGACY_SOURCE_TREE]:
        raise ReconcileError("legacy production checkout identity differs")
    status = _run_checked(
        runner,
        GIT_SOURCE_TRUST.safe_git_command(
            PRODUCTION_ROOT,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            executable=str(GIT),
        ),
        label="legacy source cleanliness",
        env=git_environment,
        timeout=30,
        umask=0o077,
    )
    if str(status.stdout):
        raise ReconcileError("legacy production checkout is not clean")
    for path, expected_mode in (
        (PRODUCTION_ROOT / ".git", 0o700),
        (PRODUCTION_ROOT / ".git/config", 0o600),
    ):
        try:
            child = path.lstat()
        except OSError as exc:
            raise ReconcileError("legacy production Git identity is incomplete") from exc
        expected_kind = stat.S_ISDIR if path.name == ".git" else stat.S_ISREG
        if (
            not expected_kind(child.st_mode)
            or path.is_symlink()
            or child.st_uid != os.geteuid()
            or stat.S_IMODE(child.st_mode) != expected_mode
        ):
            raise ReconcileError("legacy production Git permissions are unsafe")
    try:
        evidence = GIT_SOURCE_TRUST.repository_trust_evidence(
            PRODUCTION_ROOT,
            source_sha=LEGACY_SOURCE_SHA,
            source_tree=LEGACY_SOURCE_TREE,
            branch="refs/heads/main",
            origin=None,
            ambient=os.environ,
        )
        GIT_SOURCE_TRUST.require_stable_trust_surface(preflight, evidence)
    except Exception as exc:
        raise ReconcileError(
            "legacy production Git trust evidence changed"
        ) from exc
    return {
        "sha": LEGACY_SOURCE_SHA,
        "tree": LEGACY_SOURCE_TREE,
        "trust": evidence,
        "permission_takeover": permission_takeover,
    }


def _control_identity(environment: Mapping[str, str]) -> dict[str, Any]:
    root_value = environment.get("NEXPOLY_ACTIVE_CONTROL_ROOT")
    release_id = environment.get("NEXPOLY_ACTIVE_CONTROL_RELEASE_ID")
    if (
        not isinstance(root_value, str)
        or not isinstance(release_id, str)
        or RELEASE_ID_RE.fullmatch(release_id) is None
    ):
        raise ReconcileError("active content-addressed control identity is unavailable")
    root = Path(root_value)
    expected_root = RUNTIME_ROOT / "control-releases" / release_id
    if root != expected_root:
        raise ReconcileError("active content-addressed control root differs")
    require_private_directory(root)
    manifest_path = root / CONTROL_MANIFEST_NAME
    manifest = load_private_json(manifest_path)
    if manifest.get("release_id") != release_id:
        raise ReconcileError("active control release ID differs")
    source_sha = manifest.get("source_sha")
    source_tree = manifest.get("source_tree")
    if (
        not isinstance(source_sha, str)
        or SHA_RE.fullmatch(source_sha) is None
        or not isinstance(source_tree, str)
        or SHA_RE.fullmatch(source_tree) is None
    ):
        raise ReconcileError("active control source identity is malformed")
    script_path = Path(__file__).resolve()
    if script_path.parent != root or script_path.name != Path(__file__).name:
        raise ReconcileError("maintenance script is outside the active control release")
    files = manifest.get("files")
    record = files.get(script_path.name) if isinstance(files, dict) else None
    if (
        not isinstance(record, dict)
        or record.get("sha256") != "sha256:" + sha256_file(script_path)
        or record.get("mode") != 0o700
    ):
        raise ReconcileError("maintenance script is not sealed by active control")
    return {
        "release_id": release_id,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "manifest_sha256": sha256_file(manifest_path),
        "script_sha256": sha256_file(script_path),
    }


class PsqlSession:
    """A single psql session that keeps advisory/table locks across backup."""

    def __init__(self, environment: Mapping[str, str]) -> None:
        self.environment = dict(environment)
        self.process: subprocess.Popen[bytes] | None = None
        self._read_buffer = bytearray()

    def __enter__(self) -> PsqlSession:
        try:
            self.process = subprocess.Popen(
                [
                    str(PG_BIN / "psql"),
                    "-X",
                    "--no-psqlrc",
                    "--quiet",
                    "--no-align",
                    "--tuples-only",
                    "--set",
                    "ON_ERROR_STOP=1",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # Keep the interactive protocol on one continuously-drained pipe.
                # A separate stderr PIPE can fill while stdout.readline() waits and
                # deadlock a maintenance session.
                stderr=subprocess.STDOUT,
                text=False,
                bufsize=0,
                env=self.environment,
            )
        except OSError as exc:
            raise ReconcileError("cannot start locked PostgreSQL maintenance session") from exc
        return self

    def _block(self, sql: str) -> list[str]:
        process = self.process
        if process is None or process.stdin is None or process.stdout is None:
            raise ReconcileError("PostgreSQL maintenance session is unavailable")
        token = "nexpoly_" + secrets.token_hex(16)
        payload = (
            f"\\echo {token}_begin\n"
            + sql.rstrip().rstrip(";")
            + f";\n\\echo {token}_end\n"
        ).encode("utf-8")
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            raise ReconcileError("PostgreSQL maintenance session write failed") from exc
        lines: list[str] = []
        started = False
        observed_bytes = 0
        deadline = time.monotonic() + PSQL_BLOCK_TIMEOUT_SECONDS
        while True:
            line = self._readline(deadline)
            observed_bytes += len(line)
            if observed_bytes > MAX_PSQL_BLOCK_BYTES:
                raise ReconcileError("PostgreSQL maintenance response is too large")
            try:
                value = line.rstrip(b"\r\n").decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ReconcileError(
                    "PostgreSQL maintenance response encoding is invalid"
                ) from exc
            if value == f"{token}_begin":
                started = True
            elif value == f"{token}_end":
                if not started:
                    raise ReconcileError("PostgreSQL maintenance protocol is malformed")
                return [item for item in lines if item]
            elif started:
                lines.append(value)

    def _readline(self, deadline: float) -> bytes:
        process = self.process
        if process is None or process.stdout is None:
            raise ReconcileError("PostgreSQL maintenance session is unavailable")
        while True:
            newline = self._read_buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._read_buffer[: newline + 1])
                del self._read_buffer[: newline + 1]
                return line
            if len(self._read_buffer) > MAX_PSQL_BLOCK_BYTES:
                raise ReconcileError("PostgreSQL maintenance response is too large")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReconcileError("PostgreSQL maintenance response timed out")
            try:
                readable, _, _ = select.select(
                    [process.stdout.fileno()], [], [], remaining
                )
            except (OSError, ValueError) as exc:
                raise ReconcileError(
                    "PostgreSQL maintenance response wait failed"
                ) from exc
            if not readable:
                raise ReconcileError("PostgreSQL maintenance response timed out")
            try:
                chunk = os.read(process.stdout.fileno(), 64 * 1024)
            except OSError as exc:
                raise ReconcileError(
                    "PostgreSQL maintenance response read failed"
                ) from exc
            if not chunk:
                raise ReconcileError(
                    "PostgreSQL maintenance session ended unexpectedly"
                )
            self._read_buffer.extend(chunk)

    def scalar(self, sql: str) -> str:
        values = self._block(sql)
        if len(values) != 1:
            raise ReconcileError("PostgreSQL maintenance scalar response is malformed")
        return values[0]

    def json(self, sql: str) -> dict[str, Any]:
        value = self.scalar(sql)
        if len(value.encode("utf-8")) > MAX_JSON_BYTES:
            raise ReconcileError("PostgreSQL maintenance JSON response is too large")
        try:
            document = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ReconcileError("PostgreSQL maintenance JSON response is invalid") from exc
        if not isinstance(document, dict):
            raise ReconcileError("PostgreSQL maintenance JSON is not an object")
        return document

    def command(self, sql: str) -> None:
        if self.scalar(sql.rstrip().rstrip(";") + "; SELECT 'ok'") != "ok":
            raise ReconcileError("PostgreSQL maintenance command was not acknowledged")

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        process = self.process
        if process is None:
            return
        try:
            if process.stdin is not None:
                with contextlib.suppress(OSError, BrokenPipeError):
                    process.stdin.write(b"\\q\n")
                    process.stdin.flush()
                    process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    with contextlib.suppress(OSError):
                        stream.close()
            self.process = None


def _marker_identity(
    *,
    operation_id: str,
    control: Mapping[str, Any],
    source: Mapping[str, str],
    binaries: Mapping[str, Any],
    database_endpoint: Mapping[str, Any],
    restore_image: Mapping[str, str],
    bridge_authority: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "control": dict(control),
        "legacy_source": dict(source),
        "binaries_sha256": {
            path: record["sha256"] for path, record in sorted(binaries.items())
        },
        "database_endpoint": dict(database_endpoint),
        "database_system_identifier": SYSTEM_IDENTIFIER,
        "restore_image": _validate_restore_image_identity(restore_image),
        "bridge_authority": dict(bridge_authority),
        "alias": {
            "version": ALIAS_VERSION,
            "checksum": ALIAS_CHECKSUM,
            "applied_at": ALIAS_APPLIED_AT,
        },
    }


def _validate_restore_image_identity(value: object) -> dict[str, str]:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"digest_ref", "image_id"}
        or value.get("digest_ref") != POSTGRES16_IMAGE
        or not isinstance(value.get("image_id"), str)
        or DIGEST_RE.fullmatch(value["image_id"]) is None
    ):
        raise ReconcileError("pinned PostgreSQL 16 restore image identity is malformed")
    return {
        "digest_ref": POSTGRES16_IMAGE,
        "image_id": value["image_id"],
    }


class Reconciliation:
    def __init__(
        self,
        *,
        operation_id: str,
        environment: Mapping[str, str],
        runner: CommandRunner | None = None,
        session_factory: Callable[[Mapping[str, str]], Any] = PsqlSession,
        bridge_controller_loader: Callable[[], Any] | None = None,
    ) -> None:
        self.operation_id = safe_operation_id(operation_id)
        self.environment = dict(environment)
        self.runner = runner or SystemRunner()
        self.session_factory = session_factory
        self.bridge_controller_loader = (
            bridge_controller_loader or _load_pull_deploy_controller
        )
        self.state_root = RUNTIME_ROOT / STATE_ROOT_RELATIVE
        self.audit_root = RUNTIME_ROOT / AUDIT_ROOT_RELATIVE
        self.backup_root = RUNTIME_ROOT / BACKUP_ROOT_RELATIVE
        self.marker_path = self.state_root / "operation.json"
        self.audit_dir = self.audit_root / self.operation_id
        self.backup_dir = self.backup_root / self.operation_id
        self.dump_path = self.backup_dir / "nexpoly-before.dump"
        self.dump_sha_path = self.backup_dir / "nexpoly-before.dump.sha256"
        self.restore_list_path = self.audit_dir / "pg-restore.list"
        self._operation_restore_image: dict[str, str] | None = None
        self._pg_environment, self.database_endpoint = _parse_dsn(
            self.environment.get("NEXPOLY_PRODUCTION_POSTGRES_DSN", "")
        )

    def identities(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        control = _control_identity(self.environment)
        source = _source_identity(self.runner)
        binaries = binary_inventory(self.runner)
        return control, source, binaries

    def _bridge_authority(
        self,
        control: Mapping[str, Any],
        source: Mapping[str, str],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            module = self.bridge_controller_loader()
            controller = module.PullDeployController(
                PRODUCTION_ROOT,
                RUNTIME_ROOT,
                apply=False,
            )
            authority, takeover_runtime = (
                controller.prepared_alias_bridge_authority(
                    control=control,
                    legacy_source=source,
                )
            )
        except Exception as exc:
            raise ReconcileError(
                "exact prepared F-to-B bridge authority is unavailable"
            ) from exc
        if not isinstance(authority, dict) or not isinstance(
            takeover_runtime, dict
        ):
            raise ReconcileError(
                "prepared bridge authority returned malformed evidence"
            )
        return dict(authority), dict(takeover_runtime)

    def _database_inventory(self, *, phase: str) -> dict[str, Any]:
        document = _psql_json(self.runner, self._pg_environment, INVENTORY_SQL)
        return validate_inventory(document, expected_phase=phase)

    def plan(self) -> dict[str, Any]:
        self._conflicting_markers()
        self._prepare_roots()
        marker_phase: str | None = None
        existing_marker: dict[str, Any] | None = None
        if self.marker_path.exists() or self.marker_path.is_symlink():
            existing_marker = load_private_json(self.marker_path)
            phase = self._validate_marker_shape(existing_marker)
            if existing_marker["identity"]["operation_id"] != self.operation_id:
                raise ReconcileError("ledger-alias marker belongs to another operation")
            marker_phase = phase
        control, source, binaries = self.identities()
        bridge_authority, takeover_runtime = self._bridge_authority(
            control, source
        )
        runtime_stop_fence = self._runtime_stop_fence()
        self._require_takeover_runtime_match(
            takeover_runtime,
            runtime_stop_fence,
        )
        inventory = self._database_inventory(phase="pre")
        image = self._image_identity()
        expected_identity = _marker_identity(
            operation_id=self.operation_id,
            control=control,
            source=source,
            binaries=binaries,
            database_endpoint=self.database_endpoint,
            restore_image=image,
            bridge_authority=bridge_authority,
        )
        if (
            existing_marker is not None
            and existing_marker.get("identity") != expected_identity
        ):
            raise ReconcileError(
                "ledger-alias marker belongs to another execution identity"
            )
        return {
            "schema_version": 1,
            "action": "reconcile-production-0005-polytao-alias",
            "apply": False,
            "operation_id": self.operation_id,
            "control": control,
            "legacy_source": source,
            "bridge_authority": bridge_authority,
            "runtime_stop_fence": runtime_stop_fence,
            "database_endpoint": self.database_endpoint,
            "database": inventory,
            "binaries": binaries,
            "restore_image": image,
            "planned_audit_dir": str(self.audit_dir),
            "planned_backup_dir": str(self.backup_dir),
            "existing_operation_phase": marker_phase,
            "only_database_mutation": (
                "CAS delete exact 0005_polytao_jobs version/checksum/applied_at"
            ),
        }

    def _image_identity(self) -> dict[str, str]:
        _require_local_docker_endpoint(self.runner)
        completed = _run_checked(
            self.runner,
            [
                str(DOCKER),
                "image",
                "inspect",
                POSTGRES16_IMAGE,
                "--format",
                "{{json .Id}}",
            ],
            label="pinned PostgreSQL 16 restore image identity",
            env=CONTROL_ENVIRONMENT,
            timeout=30,
        )
        lines = [line for line in str(completed.stdout).splitlines() if line.strip()]
        try:
            image_id = json.loads(lines[0]) if len(lines) == 1 else None
        except json.JSONDecodeError as exc:
            raise ReconcileError(
                "pinned PostgreSQL 16 restore image ID is malformed"
            ) from exc
        return _validate_restore_image_identity(
            {"digest_ref": POSTGRES16_IMAGE, "image_id": image_id}
        )

    def _bind_restore_image(self, value: object) -> dict[str, str]:
        image = _validate_restore_image_identity(value)
        if (
            self._operation_restore_image is not None
            and self._operation_restore_image != image
        ):
            raise ReconcileError("operation restore image identity changed")
        self._operation_restore_image = image
        return dict(image)

    def _bound_restore_image(self) -> dict[str, str]:
        if self._operation_restore_image is None:
            raise ReconcileError("operation restore image identity is unavailable")
        return dict(self._operation_restore_image)

    @contextlib.contextmanager
    def _deployment_lock(self):
        path = RUNTIME_ROOT / DEPLOY_LOCK_RELATIVE
        require_private_file(path)
        descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ReconcileError("another deployment or maintenance holds the lock") from exc
            yield
        finally:
            os.close(descriptor)

    def _conflicting_markers(self) -> None:
        for relative in (DEPLOY_MARKER_RELATIVE, CONTRACT_MARKER_RELATIVE):
            path = RUNTIME_ROOT / relative
            if path.exists() or path.is_symlink():
                raise ReconcileError("another deployment or contract marker is present")

    def _prepare_roots(self) -> None:
        for root in (self.state_root, self.audit_root, self.backup_root):
            require_private_directory(root)

    def _validate_marker_shape(self, marker: Mapping[str, Any]) -> str:
        phase = marker.get("phase")
        identity = marker.get("identity")
        directories = marker.get("operation_directories")
        if (
            marker.get("schema_version") != 1
            or marker.get("action") != "reconcile-production-0005-polytao-alias"
            or not isinstance(phase, str)
            or phase not in MARKER_PHASE_INDEX
            or not isinstance(identity, dict)
            or not isinstance(identity.get("operation_id"), str)
            or OPERATION_ID_RE.fullmatch(identity["operation_id"]) is None
            or not isinstance(identity.get("bridge_authority"), dict)
            or directories
            != {"audit": str(self.audit_dir), "backup": str(self.backup_dir)}
            or not isinstance(marker.get("started_at"), str)
            or not isinstance(marker.get("updated_at"), str)
        ):
            raise ReconcileError("ledger-alias operation marker is malformed")
        _validate_restore_image_identity(identity.get("restore_image"))
        required_by_phase: tuple[tuple[str, str], ...] = (
            ("runtime-fenced", "runtime_stop_fence"),
            ("locked-preverified", "before"),
            ("backup-complete", "database_backup"),
            ("restore-started", "restore_container"),
            ("restore-verified", "isolated_restore"),
            ("mutation-intent", "mutation_intent"),
            ("mutation-commit-started", "after"),
            ("mutation-committed", "after"),
            ("completed", "audit_manifest_sha256"),
            ("completed", "completed_at"),
        )
        current = MARKER_PHASE_INDEX[phase]
        for required_phase, field in required_by_phase:
            if current >= MARKER_PHASE_INDEX[required_phase] and field not in marker:
                raise ReconcileError(
                    f"ledger-alias marker phase {phase} lacks {field} evidence"
                )
        for field in (
            "runtime_stop_fence",
            "before",
            "database_backup",
            "restore_container",
            "isolated_restore",
            "mutation_intent",
            "after",
        ):
            if field in marker and not isinstance(marker[field], dict):
                raise ReconcileError(f"ledger-alias marker {field} evidence is malformed")
        if marker.get("runtime_stop_fence_history") not in (None, []):
            raise ReconcileError(
                "ledger-alias runtime fence adoption history is forbidden"
            )
        return phase

    def _new_marker(self, identity: Mapping[str, Any]) -> dict[str, Any]:
        self._bind_restore_image(identity.get("restore_image"))
        if self.marker_path.exists() or self.marker_path.is_symlink():
            marker = load_private_json(self.marker_path)
            self._validate_marker_shape(marker)
            if marker.get("identity") != identity:
                raise ReconcileError("ledger-alias operation marker belongs to another identity")
            self._ensure_operation_directories(marker)
            return marker
        if self.audit_dir.exists() or self.audit_dir.is_symlink():
            raise ReconcileError("unowned ledger-alias audit directory already exists")
        if self.backup_dir.exists() or self.backup_dir.is_symlink():
            raise ReconcileError("unowned ledger-alias backup directory already exists")
        marker = {
            "schema_version": 1,
            "action": "reconcile-production-0005-polytao-alias",
            "phase": "directory-intent",
            "identity": dict(identity),
            "operation_directories": {
                "audit": str(self.audit_dir),
                "backup": str(self.backup_dir),
            },
            "started_at": utc_now(),
            "updated_at": utc_now(),
        }
        self._validate_marker_shape(marker)
        atomic_json(self.marker_path, marker)
        self._ensure_operation_directories(marker)
        return marker

    def _ensure_operation_directories(self, marker: dict[str, Any]) -> None:
        expected = {
            "audit": str(self.audit_dir),
            "backup": str(self.backup_dir),
        }
        if marker.get("operation_directories") != expected:
            raise ReconcileError("ledger-alias operation directories differ")
        creating = marker.get("phase") == "directory-intent"
        for path in (self.audit_dir, self.backup_dir):
            require_private_directory(path, create=creating)
        if creating:
            self._write_marker(marker, "planned")

    def _write_marker(self, marker: dict[str, Any], phase: str, **values: Any) -> None:
        candidate = {**marker, **values, "phase": phase, "updated_at": utc_now()}
        self._validate_marker_shape(candidate)
        atomic_json(self.marker_path, candidate)
        marker.clear()
        marker.update(candidate)

    def _create_file(self, path: Path) -> BinaryIO:
        require_private_directory(path.parent)
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        return os.fdopen(descriptor, "wb")

    def _archive_from_files(self) -> dict[str, Any]:
        dump_metadata = require_private_file(self.dump_path)
        require_private_file(self.dump_sha_path)
        require_private_file(self.restore_list_path)
        if dump_metadata.st_size <= 0:
            raise ReconcileError("full production PostgreSQL backup is empty")
        dump_sha = sha256_file(self.dump_path)
        recorded = self.dump_sha_path.read_text(encoding="ascii").strip()
        if recorded != dump_sha:
            raise ReconcileError("existing database backup hash differs")
        listing = self.restore_list_path.read_text(encoding="utf-8", errors="strict")
        if (
            "TABLE DATA generation polytao_jobs" not in listing
            or "TABLE DATA governance schema_migrations" not in listing
        ):
            raise ReconcileError("PostgreSQL backup catalog lacks governed tables")
        return {
            "dump_path": str(self.dump_path),
            "dump_sha256": dump_sha,
            "dump_size": dump_metadata.st_size,
            "restore_list_sha256": sha256_file(self.restore_list_path),
        }

    def _archive(self, marker: dict[str, Any], inventory: Mapping[str, Any]) -> dict[str, Any]:
        existing = [
            path
            for path in (self.dump_path, self.dump_sha_path, self.restore_list_path)
            if path.exists() or path.is_symlink()
        ]
        if existing and len(existing) != 3:
            if marker.get("phase") not in {"backup-started", "locked-preverified"}:
                raise ReconcileError("partial database backup lacks recoverable ownership")
            for path in existing:
                require_private_file(path)
                path.unlink()
                fsync_directory(path.parent)
            existing = []
        if existing:
            archive = self._archive_from_files()
            recorded = marker.get("database_backup")
            if recorded is not None and recorded != archive:
                raise ReconcileError("existing database backup evidence differs")
            if recorded is None:
                self._write_marker(
                    marker, "backup-complete", database_backup=archive
                )
            return archive
        self._write_marker(marker, "backup-started", before=dict(inventory))
        with self._create_file(self.dump_path) as stream:
            _run_checked(
                self.runner,
                [
                    str(PG_BIN / "pg_dump"),
                    "--format=custom",
                ],
                label="full production PostgreSQL backup",
                env=self._pg_environment,
                stdout=stream,
                text=False,
                timeout=1800,
            )
            stream.flush()
            os.fsync(stream.fileno())
        fsync_directory(self.dump_path.parent)
        require_private_file(self.dump_path)
        if self.dump_path.stat().st_size <= 0:
            raise ReconcileError("full production PostgreSQL backup is empty")
        dump_sha = sha256_file(self.dump_path)
        atomic_bytes(self.dump_sha_path, (dump_sha + "\n").encode("ascii"))
        with self._create_file(self.restore_list_path) as stream:
            _run_checked(
                self.runner,
                [str(PG_BIN / "pg_restore"), "--list", str(self.dump_path)],
                label="PostgreSQL backup catalog",
                stdout=stream,
                text=False,
                timeout=120,
            )
            stream.flush()
            os.fsync(stream.fileno())
        fsync_directory(self.restore_list_path.parent)
        archive = self._archive_from_files()
        self._write_marker(marker, "backup-complete", database_backup=archive)
        return archive

    def _container_name(self) -> str:
        suffix = sha256_bytes(self.operation_id.encode("ascii"))[:16]
        return f"nexpoly-alias-restore-{suffix}"

    def _inspect_container(self, name: str) -> dict[str, Any] | None:
        listed = _run_checked(
            self.runner,
            [
                str(DOCKER),
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--filter",
                f"name=^/{name}$",
                "--format",
                "{{.Names}}",
            ],
            label="restore-container existence proof",
            env=CONTROL_ENVIRONMENT,
            timeout=30,
        )
        names = [line for line in str(listed.stdout).splitlines() if line]
        if not names:
            return None
        if names != [name]:
            raise ReconcileError("restore-container name inventory is ambiguous")
        completed = _run_checked(
            self.runner,
            [str(DOCKER), "container", "inspect", name],
            label="restore-container identity",
            env=CONTROL_ENVIRONMENT,
            timeout=30,
        )
        try:
            records = json.loads(str(completed.stdout))
        except json.JSONDecodeError as exc:
            raise ReconcileError("restore-container identity is malformed") from exc
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            raise ReconcileError("restore-container identity is malformed")
        return records[0]

    def _owned_container(
        self, record: Mapping[str, Any], *, archive: Mapping[str, Any]
    ) -> bool:
        restore_image = self._bound_restore_image()
        config = record.get("Config")
        host = record.get("HostConfig")
        labels = config.get("Labels") if isinstance(config, dict) else None
        mounts = record.get("Mounts")
        state = record.get("State")
        network = record.get("NetworkSettings")
        expected_labels = {
            "io.nexpoly.operation-id": self.operation_id,
            "io.nexpoly.dump-sha256": archive["dump_sha256"],
        }
        expected_environment = [
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "POSTGRES_DB=nexpoly_alias_restore",
            *POSTGRES16_IMAGE_ENV,
        ]
        expected_bind = f"{self.backup_dir}:/archive:ro"
        return (
            isinstance(config, dict)
            and isinstance(host, dict)
            and isinstance(state, dict)
            and isinstance(network, dict)
            and record.get("Image") == restore_image["image_id"]
            and record.get("Name") == f"/{self._container_name()}"
            and record.get("RestartCount") == 0
            and config.get("Image") == restore_image["digest_ref"]
            and labels == expected_labels
            and config.get("Env") == expected_environment
            and config.get("Cmd") == ["postgres"]
            and config.get("Entrypoint") == ["docker-entrypoint.sh"]
            and config.get("User") == ""
            and config.get("WorkingDir") == "/"
            and host.get("NetworkMode") == "none"
            and host.get("Binds") == [expected_bind]
            and host.get("ReadonlyRootfs") is True
            and host.get("Privileged") is False
            and host.get("PublishAllPorts") is False
            and host.get("AutoRemove") is False
            and host.get("RestartPolicy")
            == {"Name": "no", "MaximumRetryCount": 0}
            and host.get("Tmpfs") == RESTORE_TMPFS
            and host.get("CapAdd") is None
            and host.get("CapDrop") is None
            and host.get("Devices") == []
            and host.get("DeviceRequests") is None
            and host.get("SecurityOpt") is None
            and host.get("PidMode") == ""
            and host.get("IpcMode") == "private"
            and isinstance(mounts, list)
            and len(mounts) == 1
            and isinstance(mounts[0], dict)
            and mounts[0].get("Type") == "bind"
            and mounts[0].get("Source") == str(self.backup_dir)
            and mounts[0].get("Destination") == "/archive"
            and mounts[0].get("Mode") == "ro"
            and mounts[0].get("RW") is False
            and network.get("Ports") == {}
            and isinstance(network.get("Networks"), dict)
            and set(network["Networks"]) == {"none"}
        )

    def _running_owned_container(
        self, record: Mapping[str, Any], *, archive: Mapping[str, Any]
    ) -> bool:
        state = record.get("State")
        return bool(
            self._owned_container(record, archive=archive)
            and isinstance(state, dict)
            and state.get("Running") is True
            and state.get("Paused") is False
            and state.get("Restarting") is False
            and state.get("OOMKilled") is False
            and state.get("Dead") is False
        )

    def _remove_owned_container(self, name: str, archive: Mapping[str, Any]) -> None:
        record = self._inspect_container(name)
        if record is None:
            return
        if not self._owned_container(record, archive=archive):
            raise ReconcileError("pre-existing restore container is not operation-owned")
        _run_checked(
            self.runner,
            [str(DOCKER), "rm", "--force", name],
            label="owned restore-container cleanup",
            env=CONTROL_ENVIRONMENT,
            timeout=60,
        )
        if self._inspect_container(name) is not None:
            raise ReconcileError("owned restore container still exists after cleanup")

    def _restore_proof(
        self, marker: dict[str, Any], archive: Mapping[str, Any]
    ) -> dict[str, Any]:
        existing = marker.get("isolated_restore")
        phase = self._validate_marker_shape(marker)
        if MARKER_PHASE_INDEX[phase] >= MARKER_PHASE_INDEX["restore-verified"]:
            if not isinstance(existing, dict) or existing.get("dump_sha256") != archive["dump_sha256"]:
                raise ReconcileError("isolated restore evidence is missing or differs")
            evidence_path = self.audit_dir / "isolated-postgres16-restore.json"
            if load_private_json(evidence_path) != existing:
                raise ReconcileError("isolated restore evidence file differs")
            self._remove_owned_container(self._container_name(), archive)
            return dict(existing)
        name = self._container_name()
        self._write_marker(
            marker,
            "restore-started",
            restore_container={
                "name": name,
                "image": POSTGRES16_IMAGE,
                "dump_sha256": archive["dump_sha256"],
            },
        )
        self._remove_owned_container(name, archive)
        try:
            _run_checked(
                self.runner,
                [
                    str(DOCKER),
                    "run",
                    "--detach",
                    "--pull",
                    "never",
                    "--name",
                    name,
                    "--network",
                    "none",
                    "--read-only",
                    "--tmpfs",
                    "/var/lib/postgresql/data:rw,nosuid,nodev,mode=0700",
                    "--tmpfs",
                    "/var/run/postgresql:rw,nosuid,nodev,mode=0770",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,noexec,mode=1777",
                    "--env",
                    "POSTGRES_HOST_AUTH_METHOD=trust",
                    "--env",
                    "POSTGRES_DB=nexpoly_alias_restore",
                    "--label",
                    f"io.nexpoly.operation-id={self.operation_id}",
                    "--label",
                    f"io.nexpoly.dump-sha256={archive['dump_sha256']}",
                    "--volume",
                    f"{self.backup_dir}:/archive:ro",
                    POSTGRES16_IMAGE,
                ],
                label="isolated PostgreSQL 16 restore container creation",
                env=CONTROL_ENVIRONMENT,
                timeout=120,
            )
            created = self._inspect_container(name)
            if created is None or not self._running_owned_container(
                created, archive=archive
            ):
                raise ReconcileError(
                    "created restore container differs from sealed runtime spec"
                )
            ready = False
            for _ in range(60):
                probe = self.runner.run(
                    [
                        str(DOCKER),
                        "exec",
                        name,
                        "pg_isready",
                        "--username",
                        "postgres",
                        "--dbname",
                        "nexpoly_alias_restore",
                    ],
                    env=CONTROL_ENVIRONMENT,
                    check=False,
                    timeout=10,
                )
                if probe.returncode == 0:
                    ready = True
                    break
                time.sleep(1)
            if not ready:
                raise ReconcileError("isolated PostgreSQL 16 did not become ready")
            _run_checked(
                self.runner,
                [
                    str(DOCKER),
                    "exec",
                    name,
                    "pg_restore",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-privileges",
                    "--username",
                    "postgres",
                    "--dbname",
                    "nexpoly_alias_restore",
                    "/archive/nexpoly-before.dump",
                ],
                label="isolated PostgreSQL 16 full restore",
                env=CONTROL_ENVIRONMENT,
                timeout=1800,
            )
            completed = _run_checked(
                self.runner,
                [
                    str(DOCKER),
                    "exec",
                    name,
                    "psql",
                    "-X",
                    "--no-psqlrc",
                    "--quiet",
                    "--no-align",
                    "--tuples-only",
                    "--set",
                    "ON_ERROR_STOP=1",
                    "--username",
                    "postgres",
                    "--dbname",
                    "nexpoly_alias_restore",
                    "--command",
                    "BEGIN READ ONLY; " + INVENTORY_SQL.rstrip() + "; COMMIT;",
                ],
                label="isolated PostgreSQL 16 restore verification",
                env=CONTROL_ENVIRONMENT,
                timeout=120,
            )
            lines = [line for line in str(completed.stdout).splitlines() if line.strip()]
            if len(lines) != 1:
                raise ReconcileError("isolated PostgreSQL restore inventory is malformed")
            try:
                restored_document = json.loads(lines[0])
            except json.JSONDecodeError as exc:
                raise ReconcileError("isolated PostgreSQL restore inventory is invalid") from exc
            restored = validate_inventory(
                restored_document, expected_phase="pre", restored=True
            )
        finally:
            self._remove_owned_container(name, archive)
        proof = {
            "image": self._bound_restore_image(),
            "container_name": name,
            "network_mode": "none",
            "dump_sha256": archive["dump_sha256"],
            "archive": restored["archive"],
            "ledger_schema_sha256": restored["ledger_schema_sha256"],
            "database_inventory": restored,
            "verified_at": utc_now(),
        }
        atomic_json(self.audit_dir / "isolated-postgres16-restore.json", proof)
        self._write_marker(marker, "restore-verified", isolated_restore=proof)
        return proof

    @staticmethod
    def _directory_names(path: Path) -> set[str]:
        require_private_directory(path)
        names: set[str] = set()
        for child in path.iterdir():
            if child.name in names:
                raise ReconcileError("maintenance evidence directory is ambiguous")
            require_private_file(child)
            names.add(child.name)
        return names

    def _validate_mandatory_evidence(
        self, marker: Mapping[str, Any], *, require_intent: bool
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self._validate_marker_shape(marker)
        backup_names = self._directory_names(self.backup_dir)
        if backup_names != BACKUP_EVIDENCE_NAMES:
            raise ReconcileError("database backup evidence set is incomplete or extra")
        audit_names = self._directory_names(self.audit_dir)
        required_audit = {"pg-restore.list", "isolated-postgres16-restore.json"}
        if not required_audit.issubset(audit_names) or not audit_names.issubset(
            AUDIT_EVIDENCE_NAMES
        ):
            raise ReconcileError("database audit evidence set is incomplete or extra")
        archive = self._archive_from_files()
        if marker.get("database_backup") != archive:
            raise ReconcileError("database backup marker evidence differs")
        before = marker.get("before")
        restore = marker.get("isolated_restore")
        if not isinstance(before, dict) or not isinstance(restore, dict):
            raise ReconcileError("database backup/restore evidence is malformed")
        restore_path = self.audit_dir / "isolated-postgres16-restore.json"
        if load_private_json(restore_path) != restore:
            raise ReconcileError("isolated restore evidence file differs")
        identity = marker.get("identity")
        recorded_image = (
            identity.get("restore_image") if isinstance(identity, dict) else None
        )
        if (
            restore.get("image")
            != _validate_restore_image_identity(recorded_image)
            or restore.get("container_name") != self._container_name()
            or restore.get("network_mode") != "none"
            or restore.get("dump_sha256") != archive["dump_sha256"]
            or restore.get("archive") != before.get("archive")
            or restore.get("ledger_schema_sha256")
            != before.get("ledger_schema_sha256")
            or not isinstance(restore.get("database_inventory"), dict)
            or not isinstance(restore.get("verified_at"), str)
        ):
            raise ReconcileError("isolated restore evidence identity differs")
        _require_restore_matches_before(before, restore["database_inventory"])
        if self._inspect_container(self._container_name()) is not None:
            raise ReconcileError("isolated restore container remains present")
        if require_intent:
            intent = marker.get("mutation_intent")
            identity = marker.get("identity")
            if not isinstance(intent, dict) or not isinstance(identity, dict):
                raise ReconcileError("ledger mutation intent is missing")
            if intent != {
                "database_system_identifier": SYSTEM_IDENTIFIER,
                "alias": identity.get("alias"),
                "pre_ledger": before.get("ledger"),
                "archive": before.get("archive"),
                "dump_sha256": archive["dump_sha256"],
                "restore_dump_sha256": restore["dump_sha256"],
            }:
                raise ReconcileError("ledger mutation intent differs from evidence")
        after_path = self.audit_dir / "database-after.json"
        if after_path.exists() or after_path.is_symlink():
            after = marker.get("after")
            if not isinstance(after, dict) or load_private_json(after_path) != after:
                raise ReconcileError("post-mutation database evidence differs")
            _require_post_matches_before(before, after)
        return archive, restore

    def _production_container_fence(self) -> list[dict[str, Any]]:
        listed = _run_checked(
            self.runner,
            [
                str(DOCKER),
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--filter",
                "label=com.docker.compose.project=nexpoly",
                "--format",
                "{{.ID}}",
            ],
            label="production container inventory",
            env=CONTROL_ENVIRONMENT,
            timeout=30,
        )
        identifiers = [line for line in str(listed.stdout).splitlines() if line]
        if len(identifiers) != len(set(identifiers)) or any(
            re.fullmatch(r"[0-9a-f]{64}", value) is None for value in identifiers
        ):
            raise ReconcileError("production container inventory is malformed")
        if not identifiers:
            raise ReconcileError("production PostgreSQL container is missing")
        inspected = _run_checked(
            self.runner,
            [str(DOCKER), "container", "inspect", *sorted(identifiers)],
            label="production container stop fence",
            env=CONTROL_ENVIRONMENT,
            timeout=30,
        )
        try:
            records = json.loads(str(inspected.stdout))
        except json.JSONDecodeError as exc:
            raise ReconcileError("production container stop fence is invalid") from exc
        if not isinstance(records, list) or len(records) != len(identifiers):
            raise ReconcileError("production container stop fence is incomplete")
        allowed_services = {"backend", "nginx", "postgres-init", "lab-postgres"}
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                raise ReconcileError("production container stop fence is malformed")
            config = record.get("Config")
            state = record.get("State")
            host = record.get("HostConfig")
            mounts = record.get("Mounts")
            labels = config.get("Labels") if isinstance(config, dict) else None
            if (
                not isinstance(config, dict)
                or not isinstance(state, dict)
                or not isinstance(host, dict)
                or not isinstance(mounts, list)
                or not isinstance(labels, dict)
                or labels.get("com.docker.compose.project") != "nexpoly"
            ):
                raise ReconcileError("production container compose identity differs")
            service = labels.get("com.docker.compose.service")
            identifier = record.get("Id")
            name = record.get("Name")
            if (
                service not in allowed_services
                or service in seen
                or not isinstance(identifier, str)
                or identifier not in identifiers
                or name != f"/nexpoly-{service}-1"
            ):
                raise ReconcileError("production container service inventory differs")
            seen.add(service)
            running = state.get("Running") is True
            image_id = record.get("Image")
            configured_image = config.get("Image")
            config_environment = config.get("Env")
            if (
                not isinstance(image_id, str)
                or DIGEST_RE.fullmatch(image_id) is None
                or not isinstance(configured_image, str)
                or not configured_image
                or not isinstance(config_environment, list)
                or any(
                    not isinstance(value, str)
                    for value in config_environment
                )
            ):
                raise ReconcileError(
                    "production container image identity is malformed"
                )
            data_volume: dict[str, Any] | None = None
            if service == "lab-postgres":
                bindings = host.get("PortBindings")
                data_mounts = [
                    mount
                    for mount in mounts
                    if isinstance(mount, dict)
                    and mount.get("Destination")
                    == "/var/lib/postgresql/data"
                ]
                if (
                    not running
                    or state.get("Paused") is not False
                    or state.get("Restarting") is not False
                    or state.get("OOMKilled") is not False
                    or state.get("Dead") is not False
                    or not isinstance(bindings, dict)
                    or bindings.get("5432/tcp")
                    != [{"HostIp": "", "HostPort": str(DATABASE_PORT)}]
                    or len(data_mounts) != 1
                ):
                    raise ReconcileError("production PostgreSQL runtime identity differs")
                mount = data_mounts[0]
                if (
                    mount.get("Type") != "volume"
                    or not isinstance(mount.get("Name"), str)
                    or not mount["Name"]
                    or not isinstance(mount.get("Source"), str)
                    or not Path(mount["Source"]).is_absolute()
                    or mount.get("Destination")
                    != "/var/lib/postgresql/data"
                    or not isinstance(mount.get("Driver"), str)
                    or not mount["Driver"]
                    or mount.get("RW") is not True
                ):
                    raise ReconcileError(
                        "production PostgreSQL data-volume identity differs"
                    )
                data_volume = {
                    "type": "volume",
                    "name": mount["Name"],
                    "source": mount["Source"],
                    "destination": mount["Destination"],
                    "driver": mount["Driver"],
                    "read_write": True,
                }
            elif running or state.get("Status") not in {"created", "exited", "dead"}:
                raise ReconcileError(
                    "production Backend/Web/init containers must remain stopped"
                )
            result.append(
                {
                    "id": identifier,
                    "name": name,
                    "service": service,
                    "image": image_id,
                    "config_image": configured_image,
                    "config_env_sha256": sha256_bytes(
                        canonical_json_bytes(config_environment)
                    ),
                    "labels_sha256": sha256_bytes(
                        canonical_json_bytes(labels)
                    ),
                    "data_volume": data_volume,
                    "status": state.get("Status"),
                    "running": running,
                    "finished_at": (
                        None if service == "lab-postgres" else state.get("FinishedAt")
                    ),
                    "restart_count": record.get("RestartCount"),
                    "restart_policy": host.get("RestartPolicy"),
                }
            )
        if "lab-postgres" not in seen:
            raise ReconcileError("production PostgreSQL container is missing")
        return sorted(result, key=lambda item: str(item["service"]))

    def _worker_unit_fence(self, unit: str, *, required: bool) -> dict[str, Any]:
        properties = (
            "LoadState,ActiveState,SubState,MainPID,InvocationID,FragmentPath,"
            "DropInPaths,UnitFileState"
        )
        completed = _run_checked(
            self.runner,
            [
                str(SYSTEMCTL),
                "--user",
                "show",
                unit,
                f"--property={properties}",
            ],
            label=f"{unit} stop fence",
            env=CONTROL_ENVIRONMENT,
            timeout=30,
        )
        values: dict[str, str] = {}
        for line in str(completed.stdout).splitlines():
            key, separator, value = line.partition("=")
            if not separator or key in values:
                raise ReconcileError(f"{unit} stop fence is malformed")
            values[key] = value
        expected_keys = set(properties.split(","))
        if set(values) != expected_keys:
            raise ReconcileError(f"{unit} stop fence is incomplete")
        if required and values["LoadState"] != "loaded":
            raise ReconcileError(f"{unit} must remain installed during maintenance")
        if not required and values["LoadState"] not in {"loaded", "not-found"}:
            raise ReconcileError(f"{unit} load state is invalid")
        if (
            values["ActiveState"] not in {"inactive", "failed"}
            or values["SubState"] not in {"dead", "failed"}
            or values["MainPID"] != "0"
        ):
            raise ReconcileError(f"{unit} must remain stopped during maintenance")
        fragment_sha256: str | None = None
        if values["LoadState"] == "loaded":
            fragment = Path(values["FragmentPath"])
            try:
                metadata = fragment.lstat()
            except OSError as exc:
                raise ReconcileError(
                    f"{unit} installed fragment is unavailable"
                ) from exc
            if (
                not fragment.is_absolute()
                or fragment.is_symlink()
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o022
            ):
                raise ReconcileError(
                    f"{unit} installed fragment is unsafe"
                )
            fragment_sha256 = sha256_file(fragment)
        elif values["FragmentPath"]:
            raise ReconcileError(
                f"{unit} absent fragment path is inconsistent"
            )
        values["FragmentSHA256"] = fragment_sha256
        return values

    def _runtime_stop_fence(self) -> dict[str, Any]:
        return {
            "database_system_identifier": SYSTEM_IDENTIFIER,
            "containers": self._production_container_fence(),
            "monomer_md_unit": self._worker_unit_fence(
                "nexpoly-monomer-md-worker.service", required=True
            ),
            "monomer_dft_unit": self._worker_unit_fence(
                "nexpoly-monomer-dft-worker.service", required=False
            ),
        }

    def _require_takeover_runtime_match(
        self,
        takeover: Mapping[str, Any],
        current: Mapping[str, Any],
    ) -> None:
        containers = current.get("containers")
        if not isinstance(containers, list):
            raise ReconcileError(
                "production stopped-runtime container fence is malformed"
            )
        by_service = {
            record.get("service"): record
            for record in containers
            if isinstance(record, dict)
            and isinstance(record.get("service"), str)
        }
        postgres = by_service.get("lab-postgres")
        backend = by_service.get("backend")
        web = by_service.get("nginx")
        md_unit = current.get("monomer_md_unit")
        if (
            len(by_service) != len(containers)
            or not isinstance(postgres, dict)
            or not isinstance(backend, dict)
            or not isinstance(web, dict)
            or not isinstance(md_unit, dict)
            or current.get("database_system_identifier")
            != SYSTEM_IDENTIFIER
            or takeover.get("readers_stopped") is not True
            or takeover.get("postgres_running_untouched") is not True
            or takeover.get("postgres_system_identifier")
            != SYSTEM_IDENTIFIER
            or takeover.get("postgres_container_id")
            != postgres.get("id")
            or takeover.get("postgres_image_id")
            != postgres.get("image")
            or not isinstance(postgres.get("data_volume"), dict)
            or takeover.get("postgres_data_volume")
            != postgres["data_volume"].get("name")
            or takeover.get("backend_container_id")
            != backend.get("id")
            or takeover.get("backend_image_id")
            != backend.get("image")
            or takeover.get("web_container_id") != web.get("id")
            or takeover.get("web_image_id") != web.get("image")
            or takeover.get("worker_unit_name")
            != "nexpoly-monomer-md-worker.service"
            or takeover.get("worker_unit_sha256")
            != md_unit.get("FragmentSHA256")
        ):
            raise ReconcileError(
                "current stopped readers or PostgreSQL differ from legacy "
                "takeover fence"
            )

    def _establish_runtime_stop_fence(
        self,
        marker: dict[str, Any],
        takeover_runtime: Mapping[str, Any],
    ) -> dict[str, Any]:
        current = self._runtime_stop_fence()
        self._require_takeover_runtime_match(
            takeover_runtime,
            current,
        )
        existing = marker.get("runtime_stop_fence")
        if existing is None:
            self._write_marker(marker, "runtime-fenced", runtime_stop_fence=current)
            return current
        if existing == current:
            return current
        raise ReconcileError(
            "production stopped-runtime fence changed; the operation cannot "
            "adopt replacement readers or PostgreSQL"
        )

    def _revalidate_runtime_stop_fence(self, marker: Mapping[str, Any]) -> None:
        if marker.get("runtime_stop_fence") != self._runtime_stop_fence():
            raise ReconcileError("production stopped-runtime fence changed")

    @staticmethod
    def _other_clients(session: Any) -> int:
        value = session.scalar(
            "SELECT COUNT(*)::text FROM pg_stat_activity "
            "WHERE datname=current_database() AND pid<>pg_backend_pid() "
            "AND backend_type='client backend'"
        )
        if not value.isdigit():
            raise ReconcileError("production client-session count is malformed")
        return int(value)

    def _begin_locked(self, session: Any) -> dict[str, Any]:
        if session.scalar(f"SELECT pg_try_advisory_lock({ADVISORY_LOCK_KEY})::text") != "true":
            raise ReconcileError("production database maintenance lock is unavailable")
        session.command(
            "BEGIN ISOLATION LEVEL SERIALIZABLE; "
            "SET LOCAL lock_timeout='10s'; "
            "SET LOCAL statement_timeout='30min'; "
            "SET LOCAL idle_in_transaction_session_timeout=0; "
            "SET LOCAL synchronous_commit=on; "
            "LOCK TABLE governance.schema_migrations IN EXCLUSIVE MODE; "
            "LOCK TABLE generation.polytao_jobs IN SHARE MODE"
        )
        if self._other_clients(session) != 0:
            raise ReconcileError("production database still has another client session")
        return validate_inventory(session.json(INVENTORY_SQL), expected_phase="pre")

    def _begin_recovery_locked(
        self, session: Any
    ) -> tuple[str, dict[str, Any]]:
        if session.scalar(f"SELECT pg_try_advisory_lock({ADVISORY_LOCK_KEY})::text") != "true":
            raise ReconcileError("production database maintenance lock is unavailable")
        session.command(
            "BEGIN ISOLATION LEVEL SERIALIZABLE; "
            "SET LOCAL lock_timeout='10s'; "
            "SET LOCAL statement_timeout='30min'; "
            "SET LOCAL idle_in_transaction_session_timeout=0; "
            "SET LOCAL synchronous_commit=on; "
            "LOCK TABLE governance.schema_migrations IN EXCLUSIVE MODE; "
            "LOCK TABLE generation.polytao_jobs IN SHARE MODE"
        )
        if self._other_clients(session) != 0:
            raise ReconcileError("production database still has another client session")
        document = session.json(INVENTORY_SQL)
        try:
            return "post", validate_inventory(document, expected_phase="post")
        except ReconcileError:
            return "pre", validate_inventory(document, expected_phase="pre")

    def _begin_post_locked(
        self, session: Any, *, advisory_already_held: bool
    ) -> dict[str, Any]:
        if not advisory_already_held and (
            session.scalar(f"SELECT pg_try_advisory_lock({ADVISORY_LOCK_KEY})::text")
            != "true"
        ):
            raise ReconcileError("production database maintenance lock is unavailable")
        session.command(
            "BEGIN ISOLATION LEVEL SERIALIZABLE; "
            "SET LOCAL lock_timeout='10s'; "
            "SET LOCAL statement_timeout='30min'; "
            "SET LOCAL idle_in_transaction_session_timeout=0; "
            "SET LOCAL synchronous_commit=on; "
            "LOCK TABLE governance.schema_migrations IN EXCLUSIVE MODE; "
            "LOCK TABLE generation.polytao_jobs IN SHARE MODE"
        )
        if self._other_clients(session) != 0:
            raise ReconcileError("production database still has another client session")
        return validate_inventory(session.json(INVENTORY_SQL), expected_phase="post")

    def _require_execution_identity(
        self, marker: Mapping[str, Any], binaries: Mapping[str, Any]
    ) -> None:
        control, source, current_binaries = self.identities()
        bridge_authority, takeover_runtime = self._bridge_authority(
            control, source
        )
        expected = _marker_identity(
            operation_id=self.operation_id,
            control=control,
            source=source,
            binaries=current_binaries,
            database_endpoint=self.database_endpoint,
            restore_image=self._image_identity(),
            bridge_authority=bridge_authority,
        )
        self._bind_restore_image(expected["restore_image"])
        if marker.get("identity") != expected or current_binaries != binaries:
            raise ReconcileError("maintenance execution identity changed")
        runtime_stop_fence = marker.get("runtime_stop_fence")
        if runtime_stop_fence is not None:
            if not isinstance(runtime_stop_fence, dict):
                raise ReconcileError(
                    "maintenance stopped-runtime identity is malformed"
                )
            self._require_takeover_runtime_match(
                takeover_runtime,
                runtime_stop_fence,
            )

    def _evidence_file_inventory(self) -> dict[str, dict[str, object]]:
        paths = {
            "audit/pg-restore.list": self.restore_list_path,
            "audit/isolated-postgres16-restore.json": (
                self.audit_dir / "isolated-postgres16-restore.json"
            ),
            "audit/database-after.json": self.audit_dir / "database-after.json",
            "backup/nexpoly-before.dump": self.dump_path,
            "backup/nexpoly-before.dump.sha256": self.dump_sha_path,
        }
        result: dict[str, dict[str, object]] = {}
        for logical, path in paths.items():
            metadata = require_private_file(path)
            result[logical] = {
                "size": metadata.st_size,
                "sha256": sha256_file(path),
                "mode": stat.S_IMODE(metadata.st_mode),
            }
        return result

    def _finalize(
        self,
        marker: dict[str, Any],
        *,
        after: Mapping[str, Any],
        fresh: Mapping[str, Any],
        binaries: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._require_execution_identity(marker, binaries)
        self._revalidate_runtime_stop_fence(marker)
        self._validate_mandatory_evidence(marker, require_intent=True)
        if dict(fresh) != dict(after):
            raise ReconcileError("locked post-commit database inventory differs")
        after_path = self.audit_dir / "database-after.json"
        if after_path.exists() or after_path.is_symlink():
            if load_private_json(after_path) != dict(fresh):
                raise ReconcileError("post-commit database evidence differs")
        else:
            atomic_json(after_path, dict(fresh))
        files = self._evidence_file_inventory()
        audit_path = self.audit_dir / "AUDIT-MANIFEST.json"
        existing_audit: dict[str, Any] | None = None
        if audit_path.exists() or audit_path.is_symlink():
            existing_audit = load_private_json(audit_path)
        completed_at = (
            existing_audit.get("completed_at")
            if isinstance(existing_audit, dict)
            else utc_now()
        )
        if not isinstance(completed_at, str):
            raise ReconcileError("completed audit timestamp is malformed")
        audit = {
            "schema_version": 1,
            "operation_id": self.operation_id,
            "outcome": "completed",
            "identity": marker["identity"],
            "database_before": marker["before"],
            "database_after": fresh,
            "database_backup": marker["database_backup"],
            "isolated_restore": marker["isolated_restore"],
            "runtime_stop_fence": marker["runtime_stop_fence"],
            "runtime_stop_fence_sha256": sha256_bytes(
                canonical_json_bytes(marker["runtime_stop_fence"])
            ),
            "binaries": dict(binaries),
            "files": files,
            "completed_at": completed_at,
        }
        if existing_audit is not None:
            if existing_audit != audit:
                raise ReconcileError("existing completed audit manifest differs")
        else:
            atomic_json(audit_path, audit)
        self._write_marker(
            marker,
            "completed",
            after=fresh,
            audit_manifest_sha256=sha256_file(audit_path),
            completed_at=completed_at,
        )
        return {
            "schema_version": 1,
            "action": "reconcile-production-0005-polytao-alias",
            "apply": True,
            "operation_id": self.operation_id,
            "status": "completed",
            "deleted": {
                "version": ALIAS_VERSION,
                "checksum": ALIAS_CHECKSUM,
                "applied_at": ALIAS_APPLIED_AT,
            },
            "ledger": fresh["ledger"],
            "archive": fresh["archive"],
            "audit_manifest": str(audit_path),
        }

    def _completed_result(
        self,
        marker: Mapping[str, Any],
        *,
        binaries: Mapping[str, Any],
        after: Mapping[str, Any],
        current_runtime_fence: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._require_execution_identity(marker, binaries)
        if self._runtime_stop_fence() != current_runtime_fence:
            raise ReconcileError("production stopped-runtime fence changed")
        self._validate_mandatory_evidence(marker, require_intent=True)
        if marker.get("after") != after:
            raise ReconcileError("completed ledger-alias database evidence differs")
        audit_path = self.audit_dir / "AUDIT-MANIFEST.json"
        audit = load_private_json(audit_path)
        if (
            audit.get("operation_id") != self.operation_id
            or audit.get("outcome") != "completed"
            or audit.get("identity") != marker.get("identity")
            or audit.get("database_after") != after
            or audit.get("database_before") != marker.get("before")
            or audit.get("database_backup") != marker.get("database_backup")
            or audit.get("isolated_restore") != marker.get("isolated_restore")
            or audit.get("runtime_stop_fence")
            != marker.get("runtime_stop_fence")
            or audit.get("runtime_stop_fence_sha256")
            != sha256_bytes(
                canonical_json_bytes(marker.get("runtime_stop_fence"))
            )
            or audit.get("binaries") != binaries
            or marker.get("audit_manifest_sha256") != sha256_file(audit_path)
        ):
            raise ReconcileError("completed ledger-alias audit identity differs")
        files = audit.get("files")
        if not isinstance(files, dict):
            raise ReconcileError("completed ledger-alias audit inventory is malformed")
        if files != self._evidence_file_inventory():
            raise ReconcileError("completed ledger-alias file evidence differs")
        if self._directory_names(self.backup_dir) != BACKUP_EVIDENCE_NAMES or (
            self._directory_names(self.audit_dir) != AUDIT_EVIDENCE_NAMES
        ):
            raise ReconcileError("completed ledger-alias evidence inventory differs")
        return {
            "schema_version": 1,
            "action": "reconcile-production-0005-polytao-alias",
            "apply": True,
            "operation_id": self.operation_id,
            "status": "completed",
            "idempotent_replay": True,
            "deleted": {
                "version": ALIAS_VERSION,
                "checksum": ALIAS_CHECKSUM,
                "applied_at": ALIAS_APPLIED_AT,
            },
            "ledger": after["ledger"],
            "archive": after["archive"],
            "audit_manifest": str(audit_path),
        }

    def apply(self) -> dict[str, Any]:
        with self._deployment_lock():
            self._conflicting_markers()
            self._prepare_roots()
            control, source, binaries = self.identities()
            bridge_authority, takeover_runtime = self._bridge_authority(
                control, source
            )
            image = self._image_identity()
            identity = _marker_identity(
                operation_id=self.operation_id,
                control=control,
                source=source,
                binaries=binaries,
                database_endpoint=self.database_endpoint,
                restore_image=image,
                bridge_authority=bridge_authority,
            )
            marker = self._new_marker(identity)
            current_runtime_fence = self._establish_runtime_stop_fence(
                marker,
                takeover_runtime,
            )
            phase = self._validate_marker_shape(marker)
            if MARKER_PHASE_INDEX[phase] >= MARKER_PHASE_INDEX["mutation-intent"]:
                with self.session_factory(self._pg_environment) as session:
                    try:
                        database_phase, locked = self._begin_recovery_locked(session)
                        self._require_execution_identity(marker, binaries)
                        if database_phase == "post":
                            if MARKER_PHASE_INDEX[phase] < MARKER_PHASE_INDEX[
                                "mutation-intent"
                            ]:
                                raise ReconcileError(
                                    "post-mutation database lacks durable mutation intent"
                                )
                            if phase == "completed":
                                result = self._completed_result(
                                    marker,
                                    binaries=binaries,
                                    after=locked,
                                    current_runtime_fence=current_runtime_fence,
                                )
                                session.command("COMMIT")
                                return result
                            self._revalidate_runtime_stop_fence(marker)
                            _require_post_matches_before(marker.get("before"), locked)
                            archive = marker.get("database_backup")
                            if not isinstance(archive, dict):
                                raise ReconcileError(
                                    "database backup evidence is unavailable during recovery"
                                )
                            self._remove_owned_container(
                                self._container_name(), archive
                            )
                            self._validate_mandatory_evidence(
                                marker, require_intent=True
                            )
                            recorded_after = marker.get("after")
                            if recorded_after is not None and recorded_after != locked:
                                raise ReconcileError(
                                    "recovered post-commit inventory differs"
                                )
                            self._write_marker(
                                marker,
                                "mutation-committed",
                                after=locked,
                                recovered=True,
                            )
                            result = self._finalize(
                                marker,
                                after=locked,
                                fresh=locked,
                                binaries=binaries,
                            )
                            session.command("COMMIT")
                            return result
                        if phase in {"mutation-committed", "completed"}:
                            raise ReconcileError(
                                "committed ledger-alias marker has pre-mutation database"
                            )
                        session.command("ROLLBACK")
                    except BaseException:
                        with contextlib.suppress(BaseException):
                            session.command("ROLLBACK")
                        raise
            with self.session_factory(self._pg_environment) as session:
                try:
                    before = self._begin_locked(session)
                    recorded_before = marker.get("before")
                    if recorded_before is not None and recorded_before != before:
                        raise ReconcileError("locked production inventory changed")
                    self._write_marker(marker, "locked-preverified", before=before)
                    archive = self._archive(marker, before)
                    restore = self._restore_proof(marker, archive)
                    del restore, image
                    self._require_execution_identity(marker, binaries)
                    self._revalidate_runtime_stop_fence(marker)
                    self._validate_mandatory_evidence(
                        marker, require_intent=False
                    )
                    if self._other_clients(session) != 0:
                        raise ReconcileError(
                            "production database gained another client session"
                        )
                    locked_before = validate_inventory(
                        session.json(INVENTORY_SQL), expected_phase="pre"
                    )
                    if locked_before != before:
                        raise ReconcileError("locked production inventory changed")
                    intent = {
                        "database_system_identifier": SYSTEM_IDENTIFIER,
                        "alias": identity["alias"],
                        "pre_ledger": locked_before["ledger"],
                        "archive": locked_before["archive"],
                        "dump_sha256": archive["dump_sha256"],
                        "restore_dump_sha256": marker["isolated_restore"]["dump_sha256"],
                    }
                    self._write_marker(marker, "mutation-intent", mutation_intent=intent)
                    self._validate_mandatory_evidence(marker, require_intent=True)
                    self._require_execution_identity(marker, binaries)
                    self._revalidate_runtime_stop_fence(marker)
                    deleted = session.json(
                        "WITH deleted AS ("
                        "DELETE FROM governance.schema_migrations "
                        f"WHERE version='{ALIAS_VERSION}' "
                        f"AND checksum='{ALIAS_CHECKSUM}' "
                        f"AND applied_at='{ALIAS_APPLIED_AT}'::timestamptz "
                        "RETURNING version, checksum, "
                        "to_char(applied_at AT TIME ZONE 'UTC', "
                        "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') AS applied_at"
                        ") SELECT json_build_object('rows', COALESCE("
                        "(SELECT json_agg(row_to_json(deleted)) FROM deleted), "
                        "'[]'::json))::text"
                    )
                    if deleted.get("rows") != [
                        {
                            "version": ALIAS_VERSION,
                            "checksum": ALIAS_CHECKSUM,
                            "applied_at": ALIAS_APPLIED_AT,
                        }
                    ]:
                        raise ReconcileError("alias compare-and-swap did not delete one row")
                    after = validate_inventory(
                        session.json(INVENTORY_SQL), expected_phase="post"
                    )
                    _require_post_matches_before(before, after)
                    self._write_marker(marker, "mutation-commit-started", after=after)
                    session.command("COMMIT")
                    self._write_marker(marker, "mutation-committed", after=after)
                    fresh = self._begin_post_locked(
                        session, advisory_already_held=True
                    )
                    result = self._finalize(
                        marker,
                        after=after,
                        fresh=fresh,
                        binaries=binaries,
                    )
                    session.command("COMMIT")
                    return result
                except BaseException:
                    with contextlib.suppress(BaseException):
                        session.command("ROLLBACK")
                    raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexpoly-reconcile-production-0005-polytao-alias",
        description="Reconcile the one audited duplicate production ledger alias.",
    )
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-database")
    return parser


def main(argv: list[str] | None = None) -> int:
    if not sys.flags.isolated:
        print(
            "reconcile-production-0005-alias: error: isolated Python startup is required",
            file=sys.stderr,
        )
        return 2
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.apply and arguments.confirm_database != DATABASE_NAME:
        print(
            "reconcile-production-0005-alias: error: --apply requires "
            "--confirm-database nexpoly",
            file=sys.stderr,
        )
        return 2
    if not arguments.apply and arguments.confirm_database is not None:
        print(
            "reconcile-production-0005-alias: error: confirmation is valid only with --apply",
            file=sys.stderr,
        )
        return 2
    previous_umask = os.umask(0o077)
    try:
        operation = Reconciliation(
            operation_id=arguments.operation_id,
            environment=os.environ,
        )
        result = operation.apply() if arguments.apply else operation.plan()
    except ReconcileError as exc:
        print(f"reconcile-production-0005-alias: error: {exc}", file=sys.stderr)
        return 2
    except BaseException:
        print(
            "reconcile-production-0005-alias: error: maintenance failed safely; "
            "reuse the same operation ID for recovery",
            file=sys.stderr,
        )
        return 2
    finally:
        os.umask(previous_umask)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
