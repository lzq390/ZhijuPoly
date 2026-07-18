#!/usr/bin/python3 -I
"""Build complete, source-CAS PostgreSQL external-media evidence.

This module is deliberately fail closed.  It discovers the entire reviewed
Docker/backup boundary, copies dormant PostgreSQL media through read-only
mounts, restores backups into a private PostgreSQL 16 scratch cluster, and
emits fresh evidence.  It never starts PostgreSQL against a source volume and
never writes to a source backup or bind mount.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterator, Mapping, Sequence


DOCKER = "/usr/bin/docker"
PSQL = "/usr/bin/psql"
POSTGRES_MAJOR = 16
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_DATABASE_JSON_BYTES = 64 * 1024 * 1024
DEFAULT_RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
DEFAULT_REGISTRY = DEFAULT_RUNTIME_ROOT / "config/postgres-media-registry.json"
DEFAULT_SERVICE_FILE = DEFAULT_RUNTIME_ROOT / "config/pg_service.conf"
DEFAULT_EVIDENCE_ROOT = DEFAULT_RUNTIME_ROOT / "audit/postgres-media"
FORBIDDEN_BACKUP_ROOT = DEFAULT_RUNTIME_ROOT / "backups"
APPROVED_BACKUP_ROOTS = (
    Path(
        "/data/lzq/gith/nexpoly-runtime/legacy-takeover/"
        "preserved-postgres-backups"
    ),
    Path("/data/lzq/recovery/nexpoly-postgres-media"),
)
APPROVED_BACKUP_FORMATS = (
    ("postgres-custom-v1", (".backup", ".dump")),
    ("postgres-tar-v1", (".tar",)),
)
DISCOVERY_METHODS = (
    "all-docker-containers-and-volumes-v1",
    "container-pgdata-mounts-v1",
    "all-volume-pg-version-readonly-recursive-probe-v1",
    "fixed-private-backup-roots-v1",
)
ACTIVE_CONTAINER_STATES = frozenset(
    {"created", "running", "paused", "restarting", "removing"}
)
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
ROLE_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,62}$")
SERVICE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
MEDIA_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,1023}$")
CONTAINER_RE = re.compile(r"^[0-9a-f]{64}$")
PG_SYSTEM_ID_RE = re.compile(r"^[0-9]{1,20}$")
MIGRATION_RE = re.compile(r"^[0-9]{4}_[a-z0-9_]+$")
RFC3339_UTC_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)

LEGACY_0005_ALIAS_VERSION = "0005_polytao_jobs"
LEGACY_0005_ALIAS_CHECKSUM = (
    "b15268a475e8daf8dd58be988a228a0440e59a31dbf11d5d6b52e0974c3daab5"
)
KNOWN_DIRTY_0009_CHECKSUM = (
    "79a6956fc934794d61bc003f02a6b5280e9e8bd77a217b61a28d3dbdb8b7be0b"
)
CANONICAL_0013_CHECKSUM = (
    "ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc"
)
SUPERSEDED_0013_CHECKSUM = (
    "a60cbf66c70981ba6eb7cf545b5bd89df96fa399fff48ef7f6f21d3682c64cab"
)
CANONICAL_MIGRATION_LEDGER = (
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
    (
        "0007_polytao_jobs",
        LEGACY_0005_ALIAS_CHECKSUM,
    ),
    (
        "0008_polytao_backend_runtime",
        "d0d8b2187aad8657269600873d3d2630e30c7d72da2f6662e18ab22031deff90",
    ),
    (
        "0009_monomer_md_job_leases",
        "ef1757a81976f351459e8257bd492aa6267cbf507c4ea85506fefa2d465d2db8",
    ),
    (
        "0010_deployment_control",
        "f7fad29bcf1da1c6903a688a7312a67216bc11002ac558209ff56e25f69cf7cd",
    ),
    (
        "0011_monomer_md_demo_steps",
        "9a03f38329199aa707818c2099b9811d46366bafe0ddaeb39ae53bc20d0a68ed",
    ),
    (
        "0012_drop_polytao_jobs",
        "c59b6f1efe9f926ad135379bd1a7141a7920730fa93c0e802646b1b913511728",
    ),
    (
        "0013_monomer_dft_jobs",
        CANONICAL_0013_CHECKSUM,
    ),
)


class MediaEvidenceError(RuntimeError):
    """External-media discovery or evidence construction failed closed."""


@dataclass(frozen=True, slots=True)
class DiscoveryPolicy:
    backup_roots: tuple[Path, ...] = APPROVED_BACKUP_ROOTS
    backup_formats: tuple[tuple[str, tuple[str, ...]], ...] = (
        APPROVED_BACKUP_FORMATS
    )
    discovery_methods: tuple[str, ...] = DISCOVERY_METHODS

    def document(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "docker_scope": "all-local-containers-volumes-and-pgdata-binds",
            "backup_roots": [str(value) for value in self.backup_roots],
            "backup_formats": [
                {"name": name, "suffixes": list(suffixes)}
                for name, suffixes in self.backup_formats
            ],
            "discovery_methods": list(self.discovery_methods),
        }


@dataclass(frozen=True, slots=True)
class MediaDescriptor:
    media_id: str
    kind: str
    database: str
    database_user: str
    disposition: str
    audit_method: str
    pg_service: str | None

    def document(self) -> dict[str, object]:
        return {
            "media_id": self.media_id,
            "kind": self.kind,
            "database": self.database,
            "database_user": self.database_user,
            "disposition": self.disposition,
            "audit_method": self.audit_method,
            "pg_service": self.pg_service,
        }


@dataclass(frozen=True, slots=True)
class Registry:
    payload: bytes
    digest: str
    audit_image: str
    auditor_sha256: str
    service_file_sha256: str
    descriptors: tuple[MediaDescriptor, ...]
    required_online_databases: tuple[dict[str, str], ...]
    boundary: dict[str, object]


@dataclass(frozen=True, slots=True)
class DiscoveredMedia:
    media_id: str
    kind: str
    locator: str
    data_subpath: str
    attached: tuple[dict[str, object], ...]
    backup_format: str | None = None

    def document(self) -> dict[str, object]:
        return {
            "media_id": self.media_id,
            "kind": self.kind,
            "locator": self.locator,
            "data_subpath": self.data_subpath,
            "attached": [dict(value) for value in self.attached],
            "backup_format": self.backup_format,
        }


@dataclass(frozen=True, slots=True)
class Discovery:
    media: Mapping[str, DiscoveredMedia]
    docker_inventory_sha256: str
    backup_inventory_sha256: str
    scanned_volume_names: tuple[str, ...]
    scanned_container_ids: tuple[str, ...]


@dataclass(slots=True)
class CommandRunner:
    """Small injectable subprocess boundary used by hostile and integration tests."""

    def run(
        self,
        arguments: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        timeout: int = 120,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        if not arguments or not Path(arguments[0]).is_absolute():
            raise MediaEvidenceError("command binary must be an absolute path")
        completed = subprocess.run(
            list(arguments),
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=dict(env) if env is not None else fixed_environment(),
        )
        if check and completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", "replace")[-4096:]
            raise MediaEvidenceError(
                f"command failed ({arguments[0]}): {stderr.strip()}"
            )
        return completed


def fixed_environment(**extra: str) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
        **extra,
    }


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _absolute_parts(path: Path) -> tuple[str, ...]:
    raw = os.fspath(path)
    if not raw.startswith("/") or "\x00" in raw:
        raise MediaEvidenceError(f"path is not absolute: {path}")
    pure = PurePosixPath(raw)
    if ".." in pure.parts or "." in pure.parts:
        raise MediaEvidenceError(f"path is not normalized: {path}")
    return tuple(part for part in pure.parts if part != "/")


def _open_directory_chain(
    path: Path,
    *,
    private_from: Path,
    create_leaf: bool = False,
) -> int:
    """Open a directory without following any component.

    Components at and below ``private_from`` must be deploy-user-owned 0700.
    Earlier ancestors need only be non-group/world-writable.  The returned
    descriptor owns the final directory and must be closed by the caller.
    """

    parts = _absolute_parts(path)
    anchor_parts = _absolute_parts(private_from)
    if parts[: len(anchor_parts)] != anchor_parts:
        raise MediaEvidenceError(f"path escapes its private anchor: {path}")
    descriptor = os.open(
        "/",
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        for index, component in enumerate(parts):
            flags = (
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create_leaf or index != len(parts) - 1:
                    raise
                os.mkdir(component, 0o700, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            metadata = os.fstat(child)
            private = index + 1 >= len(anchor_parts)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or private
                and (
                    metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o700
                )
                or not private
                and metadata.st_mode & 0o022
                and not (
                    metadata.st_uid == 0
                    and metadata.st_mode & stat.S_ISVTX
                )
            ):
                os.close(child)
                raise MediaEvidenceError(f"unsafe directory chain: {path}")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_private_regular(path: Path, *, root: Path) -> int:
    """Open a private regular file using openat/O_NOFOLLOW for every parent."""

    parts = _absolute_parts(path)
    root_parts = _absolute_parts(root)
    if len(parts) <= len(root_parts) or parts[: len(root_parts)] != root_parts:
        raise MediaEvidenceError(f"private file escapes approved root: {path}")
    parent = Path("/").joinpath(*parts[:-1])
    descriptor = _open_directory_chain(parent, private_from=root)
    try:
        file_descriptor = os.open(
            parts[-1],
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=descriptor,
        )
    finally:
        os.close(descriptor)
    metadata = os.fstat(file_descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        os.close(file_descriptor)
        raise MediaEvidenceError(f"private file is unsafe: {path}")
    return file_descriptor


def _read_fd(descriptor: int, maximum: int | None = None) -> bytes:
    chunks: list[bytes] = []
    size = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if maximum is not None and size > maximum:
            raise MediaEvidenceError("private file exceeds its size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise MediaEvidenceError("private file write made no progress")
        view = view[written:]


def _fd_identity(descriptor: int, path: Path, *, include_digest: bool) -> dict[str, object]:
    metadata = os.fstat(descriptor)
    result: dict[str, object] = {
        "path": str(path),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "size_bytes": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
    }
    if include_digest:
        digest = hashlib.sha256()
        os.lseek(descriptor, 0, os.SEEK_SET)
        for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
            digest.update(chunk)
        result["sha256"] = "sha256:" + digest.hexdigest()
    return result


def load_registry(
    path: Path,
    *,
    policy: DiscoveryPolicy = DiscoveryPolicy(),
    private_root: Path | None = None,
) -> Registry:
    root = private_root or path.parent
    descriptor = open_private_regular(path, root=root)
    try:
        before = os.fstat(descriptor)
        payload = _read_fd(descriptor, MAX_REGISTRY_BYTES)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise MediaEvidenceError("media registry changed while being read")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MediaEvidenceError("media registry is not canonical JSON data") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "discovery_boundary",
        "audit_runtime",
        "expected_media",
        "required_online_databases",
    }:
        raise MediaEvidenceError("media registry v2 has an invalid shape")
    if value.get("schema_version") != 2:
        raise MediaEvidenceError("media registry schema is not v2")
    boundary = value.get("discovery_boundary")
    expected_boundary = policy.document()
    if boundary != expected_boundary:
        raise MediaEvidenceError(
            "media registry narrowed or changed the fixed discovery boundary"
        )
    runtime = value.get("audit_runtime")
    if (
        not isinstance(runtime, dict)
        or set(runtime)
        != {
            "postgres_image",
            "postgres_major",
            "auditor_sha256",
            "pg_service_file_sha256",
        }
        or runtime.get("postgres_major") != POSTGRES_MAJOR
        or not isinstance(runtime.get("postgres_image"), str)
        or IMAGE_RE.fullmatch(runtime["postgres_image"]) is None
        or not isinstance(runtime.get("auditor_sha256"), str)
        or DIGEST_RE.fullmatch(runtime["auditor_sha256"]) is None
        or not isinstance(runtime.get("pg_service_file_sha256"), str)
        or DIGEST_RE.fullmatch(runtime["pg_service_file_sha256"]) is None
    ):
        raise MediaEvidenceError("media registry audit runtime is not pinned PG16")
    if runtime["auditor_sha256"] != _auditor_digest():
        raise MediaEvidenceError(
            "media registry does not pin this source-pinned auditor"
        )
    raw_descriptors = value.get("expected_media")
    if not isinstance(raw_descriptors, list) or not raw_descriptors:
        raise MediaEvidenceError("media registry expected-media set is empty")
    descriptors: list[MediaDescriptor] = []
    seen: set[str] = set()
    fields = {
        "media_id",
        "kind",
        "database",
        "database_user",
        "disposition",
        "audit_method",
        "pg_service",
    }
    for raw in raw_descriptors:
        if not isinstance(raw, dict) or set(raw) != fields:
            raise MediaEvidenceError("media registry descriptor is malformed")
        media_id = raw.get("media_id")
        kind = raw.get("kind")
        database = raw.get("database")
        database_user = raw.get("database_user")
        disposition = raw.get("disposition")
        method = raw.get("audit_method")
        service = raw.get("pg_service")
        if (
            not isinstance(media_id, str)
            or MEDIA_ID_RE.fullmatch(media_id) is None
            or media_id in seen
            or kind not in {"docker_volume", "container_bind", "postgres_backup"}
            or not isinstance(database, str)
            or ROLE_RE.fullmatch(database) is None
            or not isinstance(database_user, str)
            or ROLE_RE.fullmatch(database_user) is None
            or disposition
            not in {
                "writable-target",
                "read-only-online",
                "retained-private-isolated",
            }
            or method
            not in {
                "live-read-only",
                "isolated-volume-copy-read-only",
                "isolated-bind-copy-read-only",
                "isolated-backup-restore-read-only",
            }
        ):
            raise MediaEvidenceError("media registry descriptor identity is invalid")
        live = method == "live-read-only"
        if (
            live
            and (
                not isinstance(service, str)
                or SERVICE_RE.fullmatch(service) is None
            )
            or not live
            and service is not None
            or kind == "postgres_backup"
            and method != "isolated-backup-restore-read-only"
            or kind == "docker_volume"
            and method
            not in {"live-read-only", "isolated-volume-copy-read-only"}
            or kind == "container_bind"
            and method
            not in {"live-read-only", "isolated-bind-copy-read-only"}
            or disposition == "retained-private-isolated"
            and live
            or disposition != "retained-private-isolated"
            and not live
        ):
            raise MediaEvidenceError(
                "media registry descriptor audit method conflicts with disposition"
            )
        prefix = {
            "docker_volume": "docker-volume:",
            "container_bind": "container-bind:",
            "postgres_backup": "postgres-backup:",
        }[kind]
        if not media_id.startswith(prefix):
            raise MediaEvidenceError("media registry ID conflicts with its kind")
        seen.add(media_id)
        descriptors.append(
            MediaDescriptor(
                media_id=media_id,
                kind=str(kind),
                database=database,
                database_user=database_user,
                disposition=disposition,
                audit_method=method,
                pg_service=service,
            )
        )
    if [value.media_id for value in descriptors] != sorted(seen):
        raise MediaEvidenceError("media registry descriptors are not canonical")
    writable = [value for value in descriptors if value.disposition == "writable-target"]
    if (
        len(writable) != 1
        or writable[0].database != "nexpoly"
        or writable[0].kind not in {"docker_volume", "container_bind"}
    ):
        raise MediaEvidenceError("media registry lacks one production writable target")
    required_online = value.get("required_online_databases")
    if not isinstance(required_online, list):
        raise MediaEvidenceError("required online database set is invalid")
    expected_stacks = ("nexpoly_dev", "nexpoly_md_health_opt")
    normalized_online: list[dict[str, str]] = []
    descriptor_by_id = {value.media_id: value for value in descriptors}
    for raw in required_online:
        if (
            not isinstance(raw, dict)
            or set(raw) != {"stack", "media_id"}
            or raw.get("stack") not in expected_stacks
            or not isinstance(raw.get("media_id"), str)
            or raw["media_id"] not in descriptor_by_id
            or descriptor_by_id[raw["media_id"]].database != raw["stack"]
            or descriptor_by_id[raw["media_id"]].audit_method != "live-read-only"
        ):
            raise MediaEvidenceError("required online database mapping is invalid")
        normalized_online.append(
            {"stack": raw["stack"], "media_id": raw["media_id"]}
        )
    if [value["stack"] for value in normalized_online] != list(expected_stacks):
        raise MediaEvidenceError(
            "registry must bind both required online databases in fixed order"
        )
    return Registry(
        payload=payload,
        digest=sha256_bytes(payload),
        audit_image=runtime["postgres_image"],
        auditor_sha256=runtime["auditor_sha256"],
        service_file_sha256=runtime["pg_service_file_sha256"],
        descriptors=tuple(descriptors),
        required_online_databases=tuple(normalized_online),
        boundary=expected_boundary,
    )


def _json_command(
    runner: CommandRunner,
    arguments: Sequence[str],
    *,
    timeout: int = 120,
) -> object:
    payload = runner.run(arguments, timeout=timeout).stdout
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MediaEvidenceError("command did not return valid JSON") from exc


def _container_pgdata(container: Mapping[str, object]) -> str | None:
    config = container.get("Config")
    if not isinstance(config, dict):
        return None
    path = container.get("Path")
    args = container.get("Args")
    command: list[str] = []
    if isinstance(path, str):
        command.append(path)
    if isinstance(args, list) and all(isinstance(value, str) for value in args):
        command.extend(args)
    for index, value in enumerate(command):
        if value in {"-D", "--data-directory"} and index + 1 < len(command):
            candidate = command[index + 1]
            if PurePosixPath(candidate).is_absolute():
                return str(PurePosixPath(candidate))
        if value.startswith("--data-directory="):
            candidate = value.split("=", 1)[1]
            if PurePosixPath(candidate).is_absolute():
                return str(PurePosixPath(candidate))
    environment = config.get("Env")
    pgdata: str | None = None
    if isinstance(environment, list):
        for item in environment:
            if isinstance(item, str) and item.startswith("PGDATA="):
                pgdata = item.split("=", 1)[1]
    if pgdata is not None and PurePosixPath(pgdata).is_absolute():
        return str(PurePosixPath(pgdata))
    image = config.get("Image")
    labels = config.get("Labels")
    title = (
        labels.get("org.opencontainers.image.title", "")
        if isinstance(labels, dict)
        else ""
    )
    image_name = image.split("@", 1)[0] if isinstance(image, str) else ""
    image_name = image_name.rsplit("/", 1)[-1].split(":", 1)[0].lower()
    executable = PurePosixPath(path).name.lower() if isinstance(path, str) else ""
    first_argument = (
        args[0].lower()
        if isinstance(args, list)
        and args
        and isinstance(args[0], str)
        else ""
    )
    if (
        re.search(r"(^|[-_.])postgres($|[-_.])", image_name)
        or executable in {"postgres", "docker-entrypoint.sh"}
        and first_argument == "postgres"
        or isinstance(title, str)
        and title.lower() in {"postgres", "postgresql"}
    ):
        return "/var/lib/postgresql/data"
    mounts = container.get("Mounts")
    if isinstance(mounts, list) and any(
        isinstance(mount, dict)
        and mount.get("Destination") == "/var/lib/postgresql/data"
        for mount in mounts
    ):
        return "/var/lib/postgresql/data"
    return None


def _mount_pg_subpath(destination: str, pgdata: str) -> str | None:
    mount = PurePosixPath(destination)
    target = PurePosixPath(pgdata)
    if not mount.is_absolute() or not target.is_absolute():
        return None
    try:
        relative = target.relative_to(mount)
    except ValueError:
        return None
    value = "." if not relative.parts else relative.as_posix()
    if (
        value != "."
        and (
            ".." in PurePosixPath(value).parts
            or re.fullmatch(r"[A-Za-z0-9._/-]{1,512}", value) is None
        )
    ):
        raise MediaEvidenceError("container PGDATA subpath is unsafe")
    return value


def _docker_inventory(
    runner: CommandRunner,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    container_ids = [
        value
        for value in runner.run(
            [DOCKER, "ps", "-aq", "--no-trunc"],
            timeout=60,
        )
        .stdout.decode("ascii", "strict")
        .splitlines()
        if value
    ]
    if any(CONTAINER_RE.fullmatch(value) is None for value in container_ids):
        raise MediaEvidenceError("Docker returned a malformed container ID")
    containers: list[dict[str, object]] = []
    for container_id in sorted(set(container_ids)):
        value = _json_command(
            runner,
            [DOCKER, "container", "inspect", "--", container_id],
        )
        if (
            not isinstance(value, list)
            or len(value) != 1
            or not isinstance(value[0], dict)
            or value[0].get("Id") != container_id
        ):
            raise MediaEvidenceError("Docker container inspect identity differs")
        containers.append(value[0])
    volume_names = [
        value
        for value in runner.run(
            [DOCKER, "volume", "ls", "--format", "{{.Name}}"],
            timeout=60,
        )
        .stdout.decode("utf-8", "strict")
        .splitlines()
        if value
    ]
    if any(VOLUME_RE.fullmatch(value) is None for value in volume_names):
        raise MediaEvidenceError("Docker returned a malformed volume name")
    volumes: list[dict[str, object]] = []
    for volume_name in sorted(set(volume_names)):
        value = _json_command(
            runner,
            [DOCKER, "volume", "inspect", "--", volume_name],
        )
        if (
            not isinstance(value, list)
            or len(value) != 1
            or not isinstance(value[0], dict)
            or value[0].get("Name") != volume_name
        ):
            raise MediaEvidenceError("Docker volume inspect identity differs")
        volumes.append(value[0])
    return containers, volumes


def _container_identity(container: Mapping[str, object]) -> dict[str, object]:
    config = container.get("Config")
    host_config = container.get("HostConfig")
    state = container.get("State")
    status = state.get("Status") if isinstance(state, dict) else None
    started_at = state.get("StartedAt") if isinstance(state, dict) else None
    finished_at = state.get("FinishedAt") if isinstance(state, dict) else None
    container_id = container.get("Id")
    name = container.get("Name")
    image_id = container.get("Image")
    created_at = container.get("Created")
    path = container.get("Path")
    arguments = container.get("Args")
    restart_count = container.get("RestartCount")
    if (
        not isinstance(container_id, str)
        or CONTAINER_RE.fullmatch(container_id) is None
        or not isinstance(name, str)
        or not name.startswith("/")
        or len(name) < 2
        or not isinstance(image_id, str)
        or DIGEST_RE.fullmatch(image_id) is None
        or not isinstance(created_at, str)
        or not created_at
        or not isinstance(path, str)
        or not isinstance(arguments, list)
        or any(not isinstance(value, str) for value in arguments)
        or not isinstance(config, dict)
        or not isinstance(host_config, dict)
        or not isinstance(status, str)
        or not status
        or not isinstance(started_at, str)
        or not started_at
        or not isinstance(finished_at, str)
        or not finished_at
        or isinstance(restart_count, bool)
        or not isinstance(restart_count, int)
        or restart_count < 0
    ):
        raise MediaEvidenceError("Docker container identity is incomplete")
    config_identity = {
        "id": container_id,
        "name": name,
        "image_id": image_id,
        "created_at": created_at,
        "path": path,
        "arguments": arguments,
        "config": config,
        "host_config": host_config,
    }
    return {
        "container_id": container_id,
        "container_name": name,
        "container_image_id": image_id,
        "container_config_sha256": sha256_bytes(
            canonical_json_bytes(config_identity)
        ),
        "container_created_at": created_at,
        "container_started_at": started_at,
        "container_finished_at": finished_at,
        "container_restart_count": restart_count,
        "state": status,
    }


def _attached_record(
    container: Mapping[str, object],
    mount: Mapping[str, object],
) -> dict[str, object]:
    identity = _container_identity(container)
    destination = mount.get("Destination")
    if (
        not isinstance(destination, str)
        or not PurePosixPath(destination).is_absolute()
    ):
        raise MediaEvidenceError("Docker attachment identity is incomplete")
    return {
        **identity,
        "destination": destination,
        "read_only": mount.get("RW") is False,
    }


def _docker_boundary_document(
    containers: Sequence[Mapping[str, object]],
    volumes: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    container_records: list[dict[str, object]] = []
    for container in containers:
        mounts = container.get("Mounts")
        if not isinstance(mounts, list) or any(
            not isinstance(value, dict) for value in mounts
        ):
            raise MediaEvidenceError("Docker container mounts are invalid")
        container_records.append(
            {
                **_container_identity(container),
                "mounts_sha256": sha256_bytes(
                    canonical_json_bytes(
                        sorted(mounts, key=canonical_json_bytes)
                    )
                ),
            }
        )
    volume_records: list[dict[str, object]] = []
    for volume in volumes:
        name = volume.get("Name")
        if not isinstance(name, str) or VOLUME_RE.fullmatch(name) is None:
            raise MediaEvidenceError("Docker volume identity is invalid")
        volume_records.append(
            {
                "name": name,
                "inspect_sha256": sha256_bytes(canonical_json_bytes(volume)),
            }
        )
    return {
        "containers": sorted(
            container_records,
            key=lambda value: str(value["container_id"]),
        ),
        "volumes": sorted(
            volume_records,
            key=lambda value: str(value["name"]),
        ),
    }


def _probe_volume_pgdata(
    runner: CommandRunner,
    image: str,
    volume_name: str,
) -> str | None:
    """Return the unique recursive PGDATA subpath without starting PostgreSQL."""

    completed = runner.run(
        [
            DOCKER,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--mount",
            f"type=volume,src={volume_name},dst=/source,readonly",
            "--entrypoint",
            "/bin/sh",
            image,
            "-ceu",
            (
                "find /source -xdev -type f -name PG_VERSION "
                "-print | LC_ALL=C sort"
            ),
        ],
        timeout=120,
    )
    paths = [
        value
        for value in completed.stdout.decode("utf-8", "strict").splitlines()
        if value
    ]
    if not paths:
        return None
    if len(paths) != 1 or not paths[0].startswith("/source/"):
        raise MediaEvidenceError(
            f"volume has ambiguous PostgreSQL roots: {volume_name}"
        )
    relative = PurePosixPath(paths[0]).parent.relative_to("/source")
    value = "." if not relative.parts else relative.as_posix()
    if (
        value != "."
        and (
            ".." in PurePosixPath(value).parts
            or re.fullmatch(r"[A-Za-z0-9._/-]{1,512}", value) is None
        )
    ):
        raise MediaEvidenceError("volume PGDATA subpath is unsafe")
    return value


def _format_for_backup(path: Path, policy: DiscoveryPolicy, header: bytes) -> str | None:
    for name, suffixes in policy.backup_formats:
        if any(path.name.endswith(suffix) for suffix in suffixes):
            if name == "postgres-custom-v1" and not header.startswith(b"PGDMP"):
                raise MediaEvidenceError(f"custom backup magic differs: {path}")
            if name == "postgres-tar-v1" and (
                len(header) < 265 or header[257:262] != b"ustar"
            ):
                raise MediaEvidenceError(f"tar backup magic differs: {path}")
            return name
    return None


def _walk_backup_root(
    root: Path,
    policy: DiscoveryPolicy,
) -> tuple[list[DiscoveredMedia], list[dict[str, object]]]:
    root_descriptor = _open_directory_chain(root, private_from=root)
    media: list[DiscoveredMedia] = []
    scanned: list[dict[str, object]] = []

    def visit(descriptor: int, relative: tuple[str, ...]) -> None:
        try:
            names = sorted(os.listdir(descriptor))
        except OSError as exc:
            raise MediaEvidenceError(f"cannot enumerate backup root: {root}") from exc
        for name in names:
            if name in {".", ".."} or "/" in name or "\x00" in name:
                raise MediaEvidenceError("backup entry has an invalid name")
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            child = os.open(name, flags, dir_fd=descriptor)
            try:
                metadata = os.fstat(child)
                path = root.joinpath(*relative, name)
                if (
                    metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o077
                ):
                    raise MediaEvidenceError(
                        f"backup private parent chain is unsafe: {path}"
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    visit(child, (*relative, name))
                elif stat.S_ISREG(metadata.st_mode):
                    header = os.read(child, 512)
                    backup_format = _format_for_backup(path, policy, header)
                    scanned.append(
                        {
                            "path": str(path),
                            "device": metadata.st_dev,
                            "inode": metadata.st_ino,
                            "size_bytes": metadata.st_size,
                            "mtime_ns": metadata.st_mtime_ns,
                            "mode": stat.S_IMODE(metadata.st_mode),
                            "recognized_format": backup_format,
                        }
                    )
                    if backup_format is not None:
                        media.append(
                            DiscoveredMedia(
                                media_id=f"postgres-backup:{path}",
                                kind="postgres_backup",
                                locator=str(path),
                                data_subpath=".",
                                attached=(),
                                backup_format=backup_format,
                            )
                        )
                else:
                    raise MediaEvidenceError(
                        f"backup tree contains a symlink or special entry: {path}"
                    )
            finally:
                os.close(child)

    try:
        visit(root_descriptor, ())
    finally:
        os.close(root_descriptor)
    return media, scanned


def discover_media(
    registry: Registry,
    *,
    runner: CommandRunner,
    policy: DiscoveryPolicy = DiscoveryPolicy(),
) -> Discovery:
    containers, volumes = _docker_inventory(runner)
    volume_attachments: dict[str, list[dict[str, object]]] = {}
    pg_mounts: dict[tuple[str, str], tuple[str, list[dict[str, object]]]] = {}
    bind_mounts: dict[str, tuple[str, list[dict[str, object]]]] = {}
    unresolved_pg_candidates: list[str] = []
    for container in containers:
        mounts = container.get("Mounts")
        if not isinstance(mounts, list):
            raise MediaEvidenceError("Docker container mounts are invalid")
        pgdata = _container_pgdata(container)
        matched_pgdata = False
        for raw_mount in mounts:
            if not isinstance(raw_mount, dict):
                raise MediaEvidenceError("Docker mount record is invalid")
            mount_type = raw_mount.get("Type")
            source = raw_mount.get("Source")
            name = raw_mount.get("Name")
            destination = raw_mount.get("Destination")
            if not isinstance(destination, str):
                raise MediaEvidenceError("Docker mount destination is invalid")
            attachment = _attached_record(container, raw_mount)
            if mount_type == "volume" and isinstance(name, str):
                volume_attachments.setdefault(name, []).append(attachment)
            if pgdata is None:
                continue
            subpath = _mount_pg_subpath(destination, pgdata)
            if subpath is None:
                continue
            matched_pgdata = True
            if mount_type == "volume" and isinstance(name, str):
                key = ("docker_volume", name)
                previous = pg_mounts.get(key)
                if previous is not None and previous[0] != subpath:
                    raise MediaEvidenceError("one volume maps to conflicting PGDATA roots")
                pg_mounts[key] = (
                    subpath,
                    [*(previous[1] if previous else []), attachment],
                )
            elif mount_type == "bind" and isinstance(source, str):
                previous = bind_mounts.get(source)
                if previous is not None and previous[0] != subpath:
                    raise MediaEvidenceError("one bind maps to conflicting PGDATA roots")
                bind_mounts[source] = (
                    subpath,
                    [*(previous[1] if previous else []), attachment],
                )
        if pgdata is not None and not matched_pgdata:
            unresolved_pg_candidates.append(str(container.get("Id")))
    if unresolved_pg_candidates:
        raise MediaEvidenceError(
            "PostgreSQL-candidate containers do not expose a reviewable PGDATA mount: "
            + ", ".join(sorted(unresolved_pg_candidates))
        )

    discovered: dict[str, DiscoveredMedia] = {}
    volume_names = {str(value.get("Name")) for value in volumes}
    for value in volumes:
        name = value.get("Name")
        if not isinstance(name, str):
            raise MediaEvidenceError("Docker volume name is invalid")
        attachments = sorted(
            volume_attachments.get(name, []),
            key=lambda item: (str(item["container_id"]), str(item["destination"])),
        )
        known = pg_mounts.get(("docker_volume", name))
        probed_subpath = _probe_volume_pgdata(
            runner,
            registry.audit_image,
            name,
        )
        if known is not None:
            subpath, pg_attachments = known
            if sorted(pg_attachments, key=canonical_json_bytes) != sorted(
                attachments, key=canonical_json_bytes
            ):
                # A PostgreSQL volume mounted a second time outside PGDATA is an
                # ambiguous reader and must be reviewed explicitly.
                raise MediaEvidenceError(
                    f"PostgreSQL volume has an unclassified attachment: {name}"
                )
            if probed_subpath is None or probed_subpath != subpath:
                raise MediaEvidenceError(
                    f"container PGDATA conflicts with volume contents: {name}"
                )
        else:
            if probed_subpath is None:
                continue
            subpath = probed_subpath
            if any(
                str(attachment["state"]) in ACTIVE_CONTAINER_STATES
                for attachment in attachments
            ):
                raise MediaEvidenceError(
                    "an active PostgreSQL volume lacks an exact PGDATA "
                    f"container mapping: {name}"
                )
        media_id = f"docker-volume:{name}"
        discovered[media_id] = DiscoveredMedia(
            media_id=media_id,
            kind="docker_volume",
            locator=name,
            data_subpath=subpath,
            attached=tuple(attachments),
        )
    for source, (subpath, attachments) in sorted(bind_mounts.items()):
        path = Path(source)
        _absolute_parts(path)
        media_id = f"container-bind:{path}"
        discovered[media_id] = DiscoveredMedia(
            media_id=media_id,
            kind="container_bind",
            locator=str(path),
            data_subpath=subpath,
            attached=tuple(
                sorted(
                    attachments,
                    key=lambda item: (
                        str(item["container_id"]),
                        str(item["destination"]),
                    ),
                )
            ),
        )
    backup_scan: list[dict[str, object]] = []
    for root in policy.backup_roots:
        if root == FORBIDDEN_BACKUP_ROOT:
            raise MediaEvidenceError("operation rollback root cannot be scanned as legacy")
        values, scanned = _walk_backup_root(root, policy)
        backup_scan.extend(scanned)
        for value in values:
            if value.media_id in discovered:
                raise MediaEvidenceError("duplicate external media identity")
            discovered[value.media_id] = value

    expected = {value.media_id for value in registry.descriptors}
    if set(discovered) != expected:
        missing = sorted(expected - set(discovered))
        additional = sorted(set(discovered) - expected)
        raise MediaEvidenceError(
            "discovered media differs from registry "
            f"(missing={missing!r}, additional={additional!r})"
        )
    for descriptor in registry.descriptors:
        value = discovered[descriptor.media_id]
        if value.kind != descriptor.kind:
            raise MediaEvidenceError("discovered media kind differs from registry")
        active = [
            attachment
            for attachment in value.attached
            if str(attachment["state"]) in ACTIVE_CONTAINER_STATES
        ]
        if descriptor.audit_method == "live-read-only" and len(active) != 1:
            raise MediaEvidenceError(
                "online media must have exactly one active PostgreSQL reader: "
                f"{value.media_id}"
            )
        if descriptor.audit_method != "live-read-only" and active:
            raise MediaEvidenceError(
                f"isolated media has an active reader: {value.media_id}"
            )
    docker_document = _docker_boundary_document(containers, volumes)
    return Discovery(
        media=discovered,
        docker_inventory_sha256=sha256_bytes(canonical_json_bytes(docker_document)),
        backup_inventory_sha256=sha256_bytes(
            canonical_json_bytes(sorted(backup_scan, key=lambda item: item["path"]))
        ),
        scanned_volume_names=tuple(sorted(volume_names)),
        scanned_container_ids=tuple(
            sorted(str(value["Id"]) for value in containers)
        ),
    )


def analyze_ledger(
    ledger: object,
    *,
    legacy_relation_present: bool,
    isolated: bool,
) -> dict[str, object]:
    if not isinstance(ledger, list):
        raise MediaEvidenceError("migration ledger is not a list")
    raw: list[tuple[str, str]] = []
    for row in ledger:
        if (
            not isinstance(row, dict)
            or set(row) != {"version", "checksum"}
            or not isinstance(row.get("version"), str)
            or MIGRATION_RE.fullmatch(row["version"]) is None
            or not isinstance(row.get("checksum"), str)
            or re.fullmatch(r"[0-9a-f]{64}", row["checksum"]) is None
        ):
            raise MediaEvidenceError("migration ledger row is invalid")
        raw.append((row["version"], row["checksum"]))
    if raw != sorted(raw) or len({version for version, _checksum in raw}) != len(raw):
        raise MediaEvidenceError("migration ledger order or uniqueness is invalid")
    alias_rows = [value for value in raw if value[0] == LEGACY_0005_ALIAS_VERSION]
    if alias_rows and alias_rows != [
        (LEGACY_0005_ALIAS_VERSION, LEGACY_0005_ALIAS_CHECKSUM)
    ]:
        raise MediaEvidenceError("historical 0005 alias identity differs")
    stripped = [value for value in raw if value[0] != LEGACY_0005_ALIAS_VERSION]
    if len(stripped) > len(CANONICAL_MIGRATION_LEDGER):
        raise MediaEvidenceError("migration ledger extends beyond known history")
    mismatches: list[dict[str, str]] = []
    normalized: list[tuple[str, str]] = []
    for index, observed in enumerate(stripped):
        expected = CANONICAL_MIGRATION_LEDGER[index]
        if observed[0] != expected[0]:
            raise MediaEvidenceError("migration ledger is not a contiguous prefix")
        checksum = observed[1]
        if checksum == expected[1]:
            normalized.append(observed)
            continue
        if (
            observed[0] == "0009_monomer_md_job_leases"
            and checksum == KNOWN_DIRTY_0009_CHECKSUM
        ):
            status = "known-isolated-dirty"
        elif (
            observed[0] == "0013_monomer_dft_jobs"
            and checksum == SUPERSEDED_0013_CHECKSUM
        ):
            status = "superseded-requires-0014"
        else:
            raise MediaEvidenceError("migration ledger contains an unknown checksum")
        mismatches.append(
            {
                "version": observed[0],
                "expected_checksum": expected[1],
                "observed_checksum": checksum,
                "status": status,
            }
        )
        normalized.append(observed)
    versions = {version for version, _checksum in stripped}
    if alias_rows and (
        "0007_polytao_jobs" not in versions or "0012_drop_polytao_jobs" in versions
    ):
        raise MediaEvidenceError(
            "historical 0005 alias exists outside its exact pre-0012 epoch"
        )
    expected_relation = (
        "0007_polytao_jobs" in versions
        and "0012_drop_polytao_jobs" not in versions
    )
    if legacy_relation_present is not expected_relation:
        raise MediaEvidenceError(
            "legacy relation presence conflicts with the migration prefix"
        )
    if any(value["status"] == "known-isolated-dirty" for value in mismatches):
        if not isolated:
            raise MediaEvidenceError("known dirty 0009 medium is not isolated")
        status = "known-isolated-dirty"
    elif any(
        value["status"] == "superseded-requires-0014"
        for value in mismatches
    ):
        status = "superseded-requires-0014"
    elif not stripped:
        if not isolated:
            raise MediaEvidenceError("empty migration ledger is not isolated")
        status = "empty-isolated"
    elif alias_rows:
        status = "canonical-with-historical-0005-alias"
    else:
        status = "canonical-prefix"
    migration_0013 = next(
        (checksum for version, checksum in stripped if version == "0013_monomer_dft_jobs"),
        None,
    )
    if migration_0013 is None:
        migration_0013_record = {"state": "absent", "checksum": None}
    elif migration_0013 == CANONICAL_0013_CHECKSUM:
        migration_0013_record = {
            "state": "canonical",
            "checksum": migration_0013,
        }
    else:
        migration_0013_record = {
            "state": "superseded-requires-0014",
            "checksum": migration_0013,
        }
    return {
        "status": status,
        "canonical_prefix_length": len(stripped),
        "historical_0005_alias_present": bool(alias_rows),
        "checksum_mismatches": mismatches,
        "migration_0013": migration_0013_record,
        "requires_0014": migration_0013 == SUPERSEDED_0013_CHECKSUM,
    }


DATABASE_AUDIT_SQL = r"""
\set ON_ERROR_STOP on
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY DEFERRABLE;
SELECT json_build_object(
  'record_type', 'database',
  'database', current_database(),
  'current_user', current_user,
  'transaction_read_only', current_setting('transaction_read_only')::boolean,
  'role_superuser', role.rolsuper,
  'role_create_db', role.rolcreatedb,
  'role_create_role', role.rolcreaterole,
  'system_identifier', control.system_identifier::text,
  'database_oid', database.oid::text,
  'database_owner', pg_get_userbyid(database.datdba),
  'encoding', pg_encoding_to_char(database.encoding),
  'collate', database.datcollate,
  'ctype', database.datctype,
  'server_version_num', current_setting('server_version_num')::integer
)
FROM pg_roles AS role
CROSS JOIN pg_control_system() AS control
JOIN pg_database AS database ON database.datname = current_database()
WHERE role.rolname = current_user;
\set ledger_present false
SELECT (to_regclass('governance.schema_migrations') IS NOT NULL) AS ledger_present \gset
\if :ledger_present
SELECT json_build_object(
  'record_type', 'ledger',
  'rows', COALESCE((
    SELECT json_agg(
      json_build_object('version', version, 'checksum', checksum)
      ORDER BY version, checksum
    )
    FROM governance.schema_migrations
  ), '[]'::json),
  'relation', json_build_object(
    'oid', relation.oid::text,
    'kind', relation.relkind,
    'owner', pg_get_userbyid(relation.relowner),
    'columns', COALESCE((
      SELECT json_agg(
        json_build_object(
          'number', attribute.attnum,
          'name', attribute.attname,
          'type', format_type(attribute.atttypid, attribute.atttypmod),
          'not_null', attribute.attnotnull,
          'identity', attribute.attidentity,
          'generated', attribute.attgenerated,
          'default', pg_get_expr(default_value.adbin, default_value.adrelid)
        ) ORDER BY attribute.attnum
      )
      FROM pg_attribute AS attribute
      LEFT JOIN pg_attrdef AS default_value
        ON default_value.adrelid = attribute.attrelid
       AND default_value.adnum = attribute.attnum
      WHERE attribute.attrelid = relation.oid
        AND attribute.attnum > 0
        AND NOT attribute.attisdropped
    ), '[]'::json),
    'indexes', COALESCE((
      SELECT json_agg(pg_get_indexdef(index_value.indexrelid) ORDER BY index_value.indexrelid)
      FROM pg_index AS index_value
      WHERE index_value.indrelid = relation.oid
    ), '[]'::json),
    'constraints', COALESCE((
      SELECT json_agg(
        json_build_object(
          'name', constraint_value.conname,
          'type', constraint_value.contype,
          'definition', pg_get_constraintdef(constraint_value.oid, true)
        ) ORDER BY constraint_value.conname
      )
      FROM pg_constraint AS constraint_value
      WHERE constraint_value.conrelid = relation.oid
    ), '[]'::json)
  )
)
FROM pg_class AS relation
WHERE relation.oid = 'governance.schema_migrations'::regclass;
\else
SELECT json_build_object(
  'record_type', 'ledger',
  'rows', '[]'::json,
  'relation', null
);
\endif
SELECT (to_regclass('generation.polytao_jobs') IS NOT NULL) AS legacy_present \gset
\if :legacy_present
SELECT json_build_object(
  'record_type', 'legacy_relation',
  'present', true,
  'relation', json_build_object(
    'oid', relation.oid::text,
    'kind', relation.relkind,
    'owner', pg_get_userbyid(relation.relowner),
    'columns', COALESCE((
      SELECT json_agg(
        json_build_object(
          'number', attribute.attnum,
          'name', attribute.attname,
          'type', format_type(attribute.atttypid, attribute.atttypmod),
          'not_null', attribute.attnotnull,
          'identity', attribute.attidentity,
          'generated', attribute.attgenerated,
          'default', pg_get_expr(default_value.adbin, default_value.adrelid)
        ) ORDER BY attribute.attnum
      )
      FROM pg_attribute AS attribute
      LEFT JOIN pg_attrdef AS default_value
        ON default_value.adrelid = attribute.attrelid
       AND default_value.adnum = attribute.attnum
      WHERE attribute.attrelid = relation.oid
        AND attribute.attnum > 0
        AND NOT attribute.attisdropped
    ), '[]'::json),
    'indexes', COALESCE((
      SELECT json_agg(pg_get_indexdef(index_value.indexrelid) ORDER BY index_value.indexrelid)
      FROM pg_index AS index_value
      WHERE index_value.indrelid = relation.oid
    ), '[]'::json),
    'constraints', COALESCE((
      SELECT json_agg(
        json_build_object(
          'name', constraint_value.conname,
          'type', constraint_value.contype,
          'definition', pg_get_constraintdef(constraint_value.oid, true)
        ) ORDER BY constraint_value.conname
      )
      FROM pg_constraint AS constraint_value
      WHERE constraint_value.conrelid = relation.oid
    ), '[]'::json)
  ),
  'rows', COALESCE((
    SELECT json_agg(to_jsonb(value) ORDER BY to_jsonb(value)::text COLLATE "C")
    FROM generation.polytao_jobs AS value
  ), '[]'::json)
)
FROM pg_class AS relation
WHERE relation.oid = 'generation.polytao_jobs'::regclass;
\else
SELECT json_build_object(
  'record_type', 'legacy_relation',
  'present', false,
  'relation', null,
  'rows', '[]'::json
);
\endif
COMMIT;
"""


def _parse_database_audit(
    payload: bytes,
    *,
    expected_database: str,
    expected_user: str,
    isolated: bool,
) -> dict[str, object]:
    if len(payload) > MAX_DATABASE_JSON_BYTES:
        raise MediaEvidenceError("database audit output exceeds its limit")
    values: dict[str, dict[str, object]] = {}
    for raw_line in payload.decode("utf-8", "strict").splitlines():
        if not raw_line.startswith("{"):
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise MediaEvidenceError("database audit emitted malformed JSON") from exc
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("record_type"), str)
            or value["record_type"] in values
        ):
            raise MediaEvidenceError("database audit record identity is invalid")
        values[value["record_type"]] = value
    if set(values) != {"database", "ledger", "legacy_relation"}:
        raise MediaEvidenceError("database audit output is incomplete")
    database = values["database"]
    server_version_num = database.get("server_version_num")
    if (
        database.get("database") != expected_database
        or database.get("current_user") != expected_user
        or database.get("transaction_read_only") is not True
        or database.get("role_superuser") is not isolated
        or not isinstance(database.get("role_create_db"), bool)
        or not isinstance(database.get("role_create_role"), bool)
        or not isinstance(database.get("system_identifier"), str)
        or PG_SYSTEM_ID_RE.fullmatch(database["system_identifier"]) is None
        or isinstance(server_version_num, bool)
        or not isinstance(server_version_num, int)
        or server_version_num // 10000 != POSTGRES_MAJOR
    ):
        raise MediaEvidenceError("database audit identity or read-only role differs")
    if not isolated and (
        database["role_superuser"]
        or database["role_create_db"]
        or database["role_create_role"]
    ):
        raise MediaEvidenceError("online database audit role is privileged")
    ledger_record = values["ledger"]
    ledger = ledger_record.get("rows")
    relation = ledger_record.get("relation")
    legacy = values["legacy_relation"]
    if (
        relation is not None
        and not isinstance(relation, dict)
        or not isinstance(legacy.get("present"), bool)
    ):
        raise MediaEvidenceError("database relation evidence is malformed")
    relation_rows = legacy.get("rows")
    if not isinstance(relation_rows, list):
        raise MediaEvidenceError("legacy relation row evidence is malformed")
    legacy_relation = legacy.get("relation")
    if legacy["present"] != (legacy_relation is not None):
        raise MediaEvidenceError("legacy relation evidence is inconsistent")
    analysis = analyze_ledger(
        ledger,
        legacy_relation_present=legacy["present"],
        isolated=isolated,
    )
    ledger_schema = dict(relation) if isinstance(relation, dict) else None
    legacy_schema = dict(legacy_relation) if isinstance(legacy_relation, dict) else None
    database_identity = {
        key: database[key]
        for key in (
            "database",
            "system_identifier",
            "database_oid",
            "database_owner",
            "encoding",
            "collate",
            "ctype",
            "server_version_num",
        )
    }
    return {
        "database_identity": database_identity,
        "database_identity_sha256": sha256_bytes(
            canonical_json_bytes(database_identity)
        ),
        "current_user": database["current_user"],
        "transaction_read_only": True,
        "role_superuser": database["role_superuser"],
        "role_create_db": database["role_create_db"],
        "role_create_role": database["role_create_role"],
        "ledger": ledger,
        "ledger_sha256": sha256_bytes(canonical_json_bytes(ledger)),
        "ledger_relation": {
            "state": "present" if relation is not None else "absent",
            "row_count": len(ledger) if isinstance(ledger, list) else None,
            "schema_sha256": (
                sha256_bytes(canonical_json_bytes(ledger_schema))
                if relation is not None
                else None
            ),
            "content_sha256": sha256_bytes(canonical_json_bytes(ledger)),
        },
        "ledger_analysis": {
            key: analysis[key]
            for key in (
                "status",
                "canonical_prefix_length",
                "historical_0005_alias_present",
                "checksum_mismatches",
            )
        },
        "legacy_relation_present": legacy["present"],
        "legacy_relation": {
            "state": "present" if legacy["present"] else "absent",
            "row_count": len(relation_rows) if legacy["present"] else None,
            "schema_sha256": (
                sha256_bytes(canonical_json_bytes(legacy_schema))
                if legacy["present"]
                else None
            ),
            "content_sha256": (
                sha256_bytes(canonical_json_bytes(relation_rows))
                if legacy["present"]
                else None
            ),
        },
        "migration_0013": analysis["migration_0013"],
        "requires_0014": analysis["requires_0014"],
    }


def _run_live_audit(
    runner: CommandRunner,
    descriptor: MediaDescriptor,
    service_file: Path,
) -> dict[str, object]:
    completed = runner.run(
        [PSQL, "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1"],
        input_bytes=DATABASE_AUDIT_SQL.encode("utf-8"),
        timeout=300,
        env=fixed_environment(
            PGSERVICEFILE=str(service_file),
            PGSERVICE=str(descriptor.pg_service),
            PGCONNECT_TIMEOUT="10",
        ),
    )
    return _parse_database_audit(
        completed.stdout,
        expected_database=descriptor.database,
        expected_user=descriptor.database_user,
        isolated=False,
    )


def _current_attachments(
    runner: CommandRunner,
    source: DiscoveredMedia,
) -> list[dict[str, object]]:
    current: list[dict[str, object]] = []
    identifiers = [str(value["container_id"]) for value in source.attached]
    if len(set(identifiers)) != len(identifiers):
        raise MediaEvidenceError("source repeats a Docker container attachment")
    expected_by_id = {
        str(value["container_id"]): value for value in source.attached
    }
    for container_id in identifiers:
        value = _json_command(
            runner,
            [DOCKER, "container", "inspect", "--", container_id],
        )
        if (
            not isinstance(value, list)
            or len(value) != 1
            or not isinstance(value[0], dict)
            or value[0].get("Id") != container_id
        ):
            raise MediaEvidenceError("attached Docker container identity changed")
        container = value[0]
        mounts = container.get("Mounts")
        if not isinstance(mounts, list):
            raise MediaEvidenceError("attached Docker container mounts are invalid")
        matched: list[dict[str, object]] = []
        for mount in mounts:
            if not isinstance(mount, dict):
                raise MediaEvidenceError("attached Docker mount is invalid")
            if source.kind == "docker_volume":
                exact_source = (
                    mount.get("Type") == "volume"
                    and mount.get("Name") == source.locator
                )
            elif source.kind == "container_bind":
                exact_source = (
                    mount.get("Type") == "bind"
                    and mount.get("Source") == source.locator
                )
            else:
                raise MediaEvidenceError("backup cannot have Docker attachments")
            if not exact_source:
                continue
            destination = mount.get("Destination")
            if (
                not isinstance(destination, str)
                or destination
                != expected_by_id[container_id].get("destination")
            ):
                raise MediaEvidenceError(
                    "attached Docker mount destination changed"
                )
            matched.append(_attached_record(container, mount))
        if len(matched) != 1:
            raise MediaEvidenceError(
                "attached Docker source no longer has one exact PGDATA mount"
            )
        current.extend(matched)
    return sorted(
        current,
        key=lambda item: (
            str(item["container_id"]),
            str(item["destination"]),
        ),
    )


def _docker_volume_identity(
    runner: CommandRunner,
    source: DiscoveredMedia,
) -> dict[str, object]:
    value = _json_command(
        runner,
        [DOCKER, "volume", "inspect", "--", source.locator],
    )
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise MediaEvidenceError("Docker volume identity is malformed")
    volume = value[0]
    if volume.get("Name") != source.locator:
        raise MediaEvidenceError("Docker volume identity changed")
    labels = volume.get("Labels")
    if labels is None:
        labels = {}
    if not isinstance(labels, dict):
        raise MediaEvidenceError("Docker volume labels are invalid")
    return {
        "name": source.locator,
        "driver": volume.get("Driver"),
        "mountpoint": volume.get("Mountpoint"),
        "labels_sha256": sha256_bytes(canonical_json_bytes(labels)),
        "inspect_sha256": sha256_bytes(canonical_json_bytes(volume)),
        "data_subpath": source.data_subpath,
        "attached": _current_attachments(runner, source),
    }


def _live_source_system_identifier(
    runner: CommandRunner,
    source: DiscoveredMedia,
) -> str:
    attachments = _current_attachments(runner, source)
    active = [
        value
        for value in attachments
        if str(value["state"]) in ACTIVE_CONTAINER_STATES
    ]
    if len(active) != 1:
        raise MediaEvidenceError(
            "online source lacks one exact active PostgreSQL container"
        )
    attachment = active[0]
    destination = PurePosixPath(str(attachment["destination"]))
    pgdata = (
        destination
        if source.data_subpath == "."
        else destination.joinpath(*PurePosixPath(source.data_subpath).parts)
    )
    completed = runner.run(
        [
            DOCKER,
            "exec",
            "--user",
            "postgres",
            str(attachment["container_id"]),
            "pg_controldata",
            "-D",
            str(pgdata),
        ],
        timeout=60,
    )
    match = re.search(
        rb"(?m)^Database system identifier:\s*([0-9]{1,20})\s*$",
        completed.stdout,
    )
    if match is None:
        raise MediaEvidenceError(
            "online container pg_controldata identity is unavailable"
        )
    return match.group(1).decode("ascii")


def _volume_content_digest(
    runner: CommandRunner,
    image: str,
    source: DiscoveredMedia,
) -> str:
    completed = runner.run(
        [
            DOCKER,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--mount",
            f"type=volume,src={source.locator},dst=/source,readonly",
            "--entrypoint",
            "/bin/bash",
            image,
            "-ceu",
            (
                "set -o pipefail; "
                "test -z \"$(find /source -xdev -type l -print -quit)\"; "
                "cd /source; "
                "find . -xdev -mindepth 1 -print0 | LC_ALL=C sort -z | "
                "tar --sort=name --format=posix --numeric-owner "
                "--pax-option=delete=atime,delete=ctime "
                "--null --no-recursion -cf - -T - | sha256sum"
            ),
        ],
        timeout=3600,
    )
    fields = completed.stdout.decode("ascii", "strict").strip().split()
    if len(fields) != 2 or re.fullmatch(r"[0-9a-f]{64}", fields[0]) is None:
        raise MediaEvidenceError("volume content digest output is invalid")
    return "sha256:" + fields[0]


def _temp_name(prefix: str) -> str:
    return f"nexpoly-audit-{prefix}-{secrets.token_hex(12)}"


def _remove_container(runner: CommandRunner, identifier: str) -> None:
    removed = runner.run(
        [DOCKER, "container", "rm", "-f", "--", identifier],
        timeout=120,
        check=False,
    )
    remaining = runner.run(
        [DOCKER, "container", "inspect", "--", identifier],
        timeout=60,
        check=False,
    )
    if remaining.returncode == 0:
        raise MediaEvidenceError("temporary audit container cleanup failed")
    if removed.returncode != 0 and b"No such container" not in removed.stderr:
        raise MediaEvidenceError("temporary audit container removal failed closed")


def _remove_volume(runner: CommandRunner, name: str) -> None:
    removed = runner.run(
        [DOCKER, "volume", "rm", "--", name],
        timeout=120,
        check=False,
    )
    remaining = runner.run(
        [DOCKER, "volume", "inspect", "--", name],
        timeout=60,
        check=False,
    )
    if remaining.returncode == 0:
        raise MediaEvidenceError("temporary audit volume cleanup failed")
    if removed.returncode != 0 and b"no such volume" not in removed.stderr.lower():
        raise MediaEvidenceError("temporary audit volume removal failed closed")


def _cleanup_scratch(
    runner: CommandRunner,
    *,
    container: str | None,
    volume: str,
) -> None:
    errors: list[BaseException] = []
    if container is not None:
        try:
            _remove_container(runner, container)
        except BaseException as exc:
            errors.append(exc)
    try:
        _remove_volume(runner, volume)
    except BaseException as exc:
        errors.append(exc)
    if errors:
        raise MediaEvidenceError(
            "temporary PostgreSQL audit resources were not completely removed"
        ) from errors[0]


def _wait_for_postgres(
    runner: CommandRunner,
    container_id: str,
    *,
    database: str,
    user: str,
) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = runner.run(
            [
                DOCKER,
                "exec",
                "--user",
                "postgres",
                container_id,
                "pg_isready",
                "-h",
                "/var/run/postgresql",
                "-d",
                database,
                "-U",
                user,
            ],
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise MediaEvidenceError("isolated PostgreSQL 16 audit container did not start")


def _isolated_postgres_arguments(
    *,
    pgdata: str,
    hba_file: str | None = None,
) -> list[str]:
    values = [
        "postgres",
        "-c",
        f"data_directory={pgdata}",
        "-c",
        "listen_addresses=",
        "-c",
        "unix_socket_directories=/var/run/postgresql",
        "-c",
        "ssl=off",
        "-c",
        "logging_collector=off",
        "-c",
        "shared_preload_libraries=",
        "-c",
        "session_preload_libraries=",
        "-c",
        "local_preload_libraries=",
        "-c",
        "archive_command=/bin/false",
        "-c",
        "restore_command=/bin/false",
    ]
    if hba_file is not None:
        values.extend(["-c", f"hba_file={hba_file}"])
    return values


def _audit_container_database(
    runner: CommandRunner,
    container_id: str,
    descriptor: MediaDescriptor,
) -> dict[str, object]:
    completed = runner.run(
        [
            DOCKER,
            "exec",
            "--user",
            "postgres",
            "-i",
            container_id,
            "psql",
            "-X",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            "/var/run/postgresql",
            "-U",
            descriptor.database_user,
            "-d",
            descriptor.database,
        ],
        input_bytes=DATABASE_AUDIT_SQL.encode("utf-8"),
        timeout=600,
    )
    return _parse_database_audit(
        completed.stdout,
        expected_database=descriptor.database,
        expected_user=descriptor.database_user,
        isolated=True,
    )


def _isolated_volume_audit(
    runner: CommandRunner,
    registry: Registry,
    descriptor: MediaDescriptor,
    source: DiscoveredMedia,
) -> tuple[
    dict[str, object],
    str,
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    before = _docker_volume_identity(runner, source)
    if any(
        str(value["state"]) in ACTIVE_CONTAINER_STATES
        for value in before["attached"]
    ):
        raise MediaEvidenceError("active volume cannot enter isolated audit")
    before_digest = _volume_content_digest(
        runner,
        registry.audit_image,
        source,
    )
    clone = _temp_name("volume")
    container_id: str | None = None
    container_name: str | None = None
    runner.run(
        [
            DOCKER,
            "volume",
            "create",
            "--label",
            "io.nexpoly.audit=true",
            "--label",
            f"io.nexpoly.source={sha256_bytes(source.media_id.encode())}",
            "--",
            clone,
        ],
        timeout=60,
    )
    try:
        runner.run(
            [
                DOCKER,
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--mount",
                f"type=volume,src={source.locator},dst=/source,readonly",
                "--mount",
                f"type=volume,src={clone},dst=/copy",
                "--entrypoint",
                "/bin/bash",
                registry.audit_image,
                "-ceu",
                (
                    "set -o pipefail; "
                    "tar --format=posix "
                    "--pax-option=delete=atime,delete=ctime "
                    "-C /source -cpf - . | "
                    "tar --format=posix "
                    "--pax-option=delete=atime,delete=ctime "
                    "-C /copy -xpf -"
                ),
            ],
            timeout=3600,
        )
        clone_source = DiscoveredMedia(
            media_id=f"docker-volume:{clone}",
            kind="docker_volume",
            locator=clone,
            data_subpath=source.data_subpath,
            attached=(),
        )
        if (
            _volume_content_digest(
                runner,
                registry.audit_image,
                clone_source,
            )
            != before_digest
        ):
            raise MediaEvidenceError(
                "disposable physical-volume copy digest differs"
            )
        runner.run(
            [
                DOCKER,
                "run",
                "--rm",
                "--network",
                "none",
                "--mount",
                f"type=volume,src={clone},dst=/copy",
                "--entrypoint",
                "/bin/sh",
                registry.audit_image,
                "-ceu",
                "chown -R postgres:postgres /copy",
            ],
            timeout=3600,
        )
        hba_path = (
            "/var/lib/postgresql/data/"
            + ("" if source.data_subpath == "." else source.data_subpath + "/")
            + ".nexpoly-audit-pg_hba.conf"
        )
        data_path = (
            "/var/lib/postgresql/data"
            + ("" if source.data_subpath == "." else f"/{source.data_subpath}")
        )
        runner.run(
            [
                DOCKER,
                "run",
                "--rm",
                "--network",
                "none",
                "--mount",
                f"type=volume,src={clone},dst=/var/lib/postgresql/data",
                "--entrypoint",
                "/bin/sh",
                registry.audit_image,
                "-ceu",
                (
                    f"rm -f '{data_path}/postmaster.pid'; "
                    f"printf '%s\\n' 'local all all trust' > '{hba_path}'; "
                    f"chown postgres:postgres '{hba_path}'; chmod 0600 '{hba_path}'"
                ),
            ],
            timeout=120,
        )
        container_name = _temp_name("postgres")
        pgdata = (
            "/var/lib/postgresql/data"
            + ("" if source.data_subpath == "." else f"/{source.data_subpath}")
        )
        completed = runner.run(
            [
                DOCKER,
                "run",
                "-d",
                "--name",
                container_name,
                "--label",
                "io.nexpoly.audit=true",
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                "/var/run/postgresql:rw,noexec,nosuid,size=16m,uid=999,gid=999,mode=0700",
                "--mount",
                f"type=volume,src={clone},dst=/var/lib/postgresql/data",
                "--env",
                f"PGDATA={pgdata}",
                registry.audit_image,
                *_isolated_postgres_arguments(
                    pgdata=pgdata,
                    hba_file=hba_path,
                ),
            ],
            timeout=120,
        )
        container_id = completed.stdout.decode("ascii", "strict").strip()
        if CONTAINER_RE.fullmatch(container_id) is None:
            raise MediaEvidenceError("isolated audit container ID is invalid")
        _wait_for_postgres(
            runner,
            container_id,
            database=descriptor.database,
            user=descriptor.database_user,
        )
        database = _audit_container_database(runner, container_id, descriptor)
    finally:
        _cleanup_scratch(
            runner,
            container=container_id or container_name,
            volume=clone,
        )
    after = _docker_volume_identity(runner, source)
    after_digest = _volume_content_digest(
        runner,
        registry.audit_image,
        source,
    )
    if before != after or before_digest != after_digest:
        raise MediaEvidenceError("source volume changed during isolated audit")
    isolation = {
        "source_mounted_read_only": True,
        "source_started_as_postgres": False,
        "scratch_network": "none",
        "scratch_destroyed": True,
        "copy_method": "readonly-tar-copy-to-disposable-volume-v1",
    }
    return database, before_digest, before, after, isolation


def _copy_backup_snapshot(
    source: Path,
    *,
    root: Path,
    destination: Path,
) -> tuple[dict[str, object], str]:
    descriptor = open_private_regular(source, root=root)
    try:
        before = _fd_identity(descriptor, source, include_digest=True)
        digest = before["sha256"]
        target_descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        copied_digest = hashlib.sha256()
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                _write_all(target_descriptor, chunk)
                copied_digest.update(chunk)
            os.fsync(target_descriptor)
        finally:
            os.close(target_descriptor)
        after_same_fd = _fd_identity(descriptor, source, include_digest=True)
    finally:
        os.close(descriptor)
    if before != after_same_fd:
        raise MediaEvidenceError("backup changed while creating isolated snapshot")
    if "sha256:" + copied_digest.hexdigest() != digest:
        raise MediaEvidenceError("isolated backup snapshot digest differs")
    return before, str(digest)


def _find_backup_root(path: Path, policy: DiscoveryPolicy) -> Path:
    parts = _absolute_parts(path)
    matches = [
        root
        for root in policy.backup_roots
        if parts[: len(_absolute_parts(root))] == _absolute_parts(root)
    ]
    if len(matches) != 1:
        raise MediaEvidenceError("backup is outside one fixed approved root")
    return matches[0]


def _isolated_backup_audit(
    runner: CommandRunner,
    registry: Registry,
    descriptor: MediaDescriptor,
    source: DiscoveredMedia,
    workspace: Path,
    *,
    policy: DiscoveryPolicy,
) -> tuple[
    dict[str, object],
    str,
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    source_path = Path(source.locator)
    backup_root = _find_backup_root(source_path, policy)
    staged = workspace / "source.backup"
    before, source_digest = _copy_backup_snapshot(
        source_path,
        root=backup_root,
        destination=staged,
    )
    scratch_volume = _temp_name("restore")
    container_id: str | None = None
    container_name: str | None = None
    runner.run(
        [
            DOCKER,
            "volume",
            "create",
            "--label",
            "io.nexpoly.audit=true",
            "--",
            scratch_volume,
        ],
        timeout=60,
    )
    try:
        container_name = _temp_name("postgres")
        pgdata = (
            "/var/lib/postgresql/data"
            + ("" if source.data_subpath == "." else f"/{source.data_subpath}")
        )
        completed = runner.run(
            [
                DOCKER,
                "run",
                "-d",
                "--name",
                container_name,
                "--label",
                "io.nexpoly.audit=true",
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                "/var/run/postgresql:rw,noexec,nosuid,size=16m,uid=999,gid=999,mode=0700",
                "--mount",
                f"type=volume,src={scratch_volume},dst=/var/lib/postgresql/data",
                "--mount",
                f"type=bind,src={workspace},dst=/source-audit,readonly",
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "--env",
                f"POSTGRES_USER={descriptor.database_user}",
                registry.audit_image,
                "postgres",
                "-c",
                "listen_addresses=",
                "-c",
                "unix_socket_directories=/var/run/postgresql",
            ],
            timeout=120,
        )
        container_id = completed.stdout.decode("ascii", "strict").strip()
        if CONTAINER_RE.fullmatch(container_id) is None:
            raise MediaEvidenceError("restore container ID is invalid")
        _wait_for_postgres(
            runner,
            container_id,
            database="postgres",
            user=descriptor.database_user,
        )
        runner.run(
            [
                DOCKER,
                "exec",
                "--user",
                "postgres",
                container_id,
                "createdb",
                "-h",
                "/var/run/postgresql",
                "-U",
                descriptor.database_user,
                "--",
                descriptor.database,
            ],
            timeout=120,
        )
        runner.run(
            [
                DOCKER,
                "exec",
                "--user",
                "root",
                container_id,
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "--strict-names",
                "-h",
                "/var/run/postgresql",
                "-U",
                descriptor.database_user,
                "-d",
                descriptor.database,
                "/source-audit/source.backup",
            ],
            timeout=3600,
        )
        database = _audit_container_database(runner, container_id, descriptor)
    finally:
        _cleanup_scratch(
            runner,
            container=container_id or container_name,
            volume=scratch_volume,
        )
    final_descriptor = open_private_regular(source_path, root=backup_root)
    try:
        after = _fd_identity(final_descriptor, source_path, include_digest=True)
    finally:
        os.close(final_descriptor)
    if before != after or after.get("sha256") != source_digest:
        raise MediaEvidenceError("source backup changed during isolated restore")
    identity = {
        **before,
        "format": source.backup_format,
    }
    isolation = {
        "source_opened_with_openat_no_follow": True,
        "source_passed_to_docker": False,
        "staged_snapshot_mounted_read_only": True,
        "source_started_as_postgres": False,
        "scratch_network": "none",
        "scratch_destroyed": True,
        "restore_method": "pg_restore-no-owner-no-privileges-v1",
    }
    return database, source_digest, identity, {**after, "format": source.backup_format}, isolation


def _bind_tree_snapshot(
    source: Path,
    destination: Path,
) -> tuple[dict[str, object], str]:
    """Copy a private bind tree via openat without following symlinks."""

    source_descriptor = _open_directory_chain(source, private_from=source)
    destination.mkdir(mode=0o700)
    digest = hashlib.sha256()

    def copy_directory(
        input_descriptor: int,
        output: Path,
        relative: tuple[str, ...],
    ) -> None:
        for name in sorted(os.listdir(input_descriptor)):
            child = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=input_descriptor,
            )
            try:
                metadata = os.fstat(child)
                if (
                    metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o077
                ):
                    raise MediaEvidenceError("bind PGDATA is not deploy-user-owned")
                relative_name = "/".join((*relative, name))
                if stat.S_ISDIR(metadata.st_mode):
                    target = output / name
                    target.mkdir(mode=0o700)
                    digest.update(
                        canonical_json_bytes(
                            ["directory", relative_name, stat.S_IMODE(metadata.st_mode)]
                        )
                    )
                    copy_directory(child, target, (*relative, name))
                elif stat.S_ISREG(metadata.st_mode):
                    target = output / name
                    target_descriptor = os.open(
                        target,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                    file_digest = hashlib.sha256()
                    try:
                        while True:
                            chunk = os.read(child, 1024 * 1024)
                            if not chunk:
                                break
                            _write_all(target_descriptor, chunk)
                            file_digest.update(chunk)
                        os.fsync(target_descriptor)
                    finally:
                        os.close(target_descriptor)
                    digest.update(
                        canonical_json_bytes(
                            [
                                "file",
                                relative_name,
                                metadata.st_size,
                                stat.S_IMODE(metadata.st_mode),
                                file_digest.hexdigest(),
                            ]
                        )
                    )
                else:
                    raise MediaEvidenceError(
                        "bind PGDATA contains a symlink or special entry"
                    )
            finally:
                os.close(child)

    try:
        before = os.fstat(source_descriptor)
        copy_directory(source_descriptor, destination, ())
        after = os.fstat(source_descriptor)
    finally:
        os.close(source_descriptor)
    identity = {
        "path": str(source),
        "device": before.st_dev,
        "inode": before.st_ino,
        "mtime_ns": before.st_mtime_ns,
        "mode": stat.S_IMODE(before.st_mode),
        "uid": before.st_uid,
    }
    if (
        before.st_dev,
        before.st_ino,
        before.st_mtime_ns,
        before.st_mode,
        before.st_uid,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mtime_ns,
        after.st_mode,
        after.st_uid,
    ):
        raise MediaEvidenceError("bind PGDATA changed while it was copied")
    return identity, "sha256:" + digest.hexdigest()


def _isolated_bind_audit(
    runner: CommandRunner,
    registry: Registry,
    descriptor: MediaDescriptor,
    source: DiscoveredMedia,
    workspace: Path,
) -> tuple[
    dict[str, object],
    str,
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    before_attachments = _current_attachments(runner, source)
    if any(
        str(value["state"]) in ACTIVE_CONTAINER_STATES
        for value in before_attachments
    ):
        raise MediaEvidenceError("active bind cannot enter isolated audit")
    snapshot = workspace / "pgdata"
    before_tree, source_digest = _bind_tree_snapshot(
        Path(source.locator),
        snapshot,
    )
    before = {
        **before_tree,
        "data_subpath": source.data_subpath,
        "attached": before_attachments,
    }
    # The copied bind is imported into a disposable Docker volume.  The
    # original host path is never passed to Docker.
    clone = _temp_name("bind")
    container_id: str | None = None
    container_name: str | None = None
    runner.run(
        [
            DOCKER,
            "volume",
            "create",
            "--label",
            "io.nexpoly.audit=true",
            "--",
            clone,
        ]
    )
    try:
        runner.run(
            [
                DOCKER,
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--mount",
                f"type=bind,src={snapshot},dst=/source,readonly",
                "--mount",
                f"type=volume,src={clone},dst=/copy",
                "--entrypoint",
                "/bin/bash",
                registry.audit_image,
                "-ceu",
                (
                    "set -o pipefail; "
                    "tar --format=posix "
                    "--pax-option=delete=atime,delete=ctime "
                    "-C /source -cpf - . | "
                    "tar --format=posix "
                    "--pax-option=delete=atime,delete=ctime "
                    "-C /copy -xpf -; "
                    "chown -R postgres:postgres /copy"
                ),
            ],
            timeout=3600,
        )
        # Reuse the isolated-volume start/query primitives without recursively
        # copying the disposable clone.
        hba_path = (
            "/var/lib/postgresql/data/"
            + ("" if source.data_subpath == "." else source.data_subpath + "/")
            + ".nexpoly-audit-pg_hba.conf"
        )
        data_path = (
            "/var/lib/postgresql/data"
            + ("" if source.data_subpath == "." else f"/{source.data_subpath}")
        )
        runner.run(
            [
                DOCKER,
                "run",
                "--rm",
                "--network",
                "none",
                "--mount",
                f"type=volume,src={clone},dst=/var/lib/postgresql/data",
                "--entrypoint",
                "/bin/sh",
                registry.audit_image,
                "-ceu",
                (
                    f"rm -f '{data_path}/postmaster.pid'; "
                    f"printf '%s\\n' 'local all all trust' > '{hba_path}'; "
                    f"chown postgres:postgres '{hba_path}'; chmod 0600 '{hba_path}'"
                ),
            ]
        )
        container_name = _temp_name("postgres")
        completed = runner.run(
            [
                DOCKER,
                "run",
                "-d",
                "--name",
                container_name,
                "--label",
                "io.nexpoly.audit=true",
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                "/var/run/postgresql:rw,noexec,nosuid,size=16m,uid=999,gid=999,mode=0700",
                "--mount",
                f"type=volume,src={clone},dst=/var/lib/postgresql/data",
                "--env",
                f"PGDATA={pgdata}",
                registry.audit_image,
                *_isolated_postgres_arguments(
                    pgdata=pgdata,
                    hba_file=hba_path,
                ),
            ]
        )
        container_id = completed.stdout.decode("ascii", "strict").strip()
        if CONTAINER_RE.fullmatch(container_id) is None:
            raise MediaEvidenceError("isolated bind audit container ID is invalid")
        _wait_for_postgres(
            runner,
            container_id,
            database=descriptor.database,
            user=descriptor.database_user,
        )
        database = _audit_container_database(runner, container_id, descriptor)
    finally:
        _cleanup_scratch(
            runner,
            container=container_id or container_name,
            volume=clone,
        )
    after_workspace = workspace / "pgdata-after"
    after_tree, after_digest = _bind_tree_snapshot(
        Path(source.locator),
        after_workspace,
    )
    shutil.rmtree(after_workspace)
    after = {
        **after_tree,
        "data_subpath": source.data_subpath,
        "attached": _current_attachments(runner, source),
    }
    if before != after or source_digest != after_digest:
        raise MediaEvidenceError("source bind changed during isolated audit")
    isolation = {
        "source_mounted_read_only": False,
        "source_opened_with_openat_no_follow": True,
        "source_started_as_postgres": False,
        "scratch_network": "none",
        "scratch_destroyed": True,
        "copy_method": "private-openat-copy-to-disposable-volume-v1",
    }
    return database, source_digest, before, after, isolation


def _validate_audit_image(runner: CommandRunner, image: str) -> str:
    value = _json_command(runner, [DOCKER, "image", "inspect", "--", image])
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise MediaEvidenceError("pinned PG16 image is not preloaded")
    repo_digests = value[0].get("RepoDigests")

    def normalized_repository(reference: str) -> tuple[str, str] | None:
        if "@" not in reference:
            return None
        repository, digest = reference.rsplit("@", 1)
        if repository.startswith("docker.io/"):
            repository = repository.removeprefix("docker.io/")
        if repository.startswith("library/"):
            repository = repository.removeprefix("library/")
        return repository, digest

    expected_reference = normalized_repository(image)
    if (
        expected_reference is None
        or not isinstance(repo_digests, list)
        or not any(
            isinstance(reference, str)
            and normalized_repository(reference) == expected_reference
            for reference in repo_digests
        )
    ):
        raise MediaEvidenceError("local image does not expose the pinned PG16 digest")
    output = runner.run(
        [
            DOCKER,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--entrypoint",
            "postgres",
            image,
            "--version",
        ],
        timeout=60,
    ).stdout.decode("utf-8", "strict")
    if re.search(r"\b16(?:\.[0-9]+)?\b", output) is None:
        raise MediaEvidenceError("pinned audit image is not PostgreSQL 16")
    runner.run(
        [
            DOCKER,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--entrypoint",
            "/bin/bash",
            image,
            "-ceu",
            (
                "set -o pipefail; "
                "for tool in find tar sha256sum pg_restore createdb "
                "pg_controldata pg_isready psql; do "
                "command -v \"$tool\" >/dev/null; done; "
                "printf ready | sha256sum >/dev/null; "
                "tar --sort=name --format=posix "
                "--pax-option=delete=atime,delete=ctime "
                "-cf /dev/null --files-from /dev/null"
            ),
        ],
        timeout=60,
    )
    image_id = value[0].get("Id")
    if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
        raise MediaEvidenceError("pinned audit image ID is invalid")
    return image_id


def _live_source_identity(
    runner: CommandRunner,
    source: DiscoveredMedia,
) -> dict[str, object]:
    if source.kind == "docker_volume":
        return _docker_volume_identity(runner, source)
    if source.kind == "container_bind":
        path = Path(source.locator)
        descriptor = _open_directory_chain(path, private_from=path)
        try:
            metadata = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        return {
            "path": str(path),
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "mtime_ns": metadata.st_mtime_ns,
            "mode": stat.S_IMODE(metadata.st_mode),
            "uid": metadata.st_uid,
            "data_subpath": source.data_subpath,
            "attached": _current_attachments(runner, source),
        }
    raise MediaEvidenceError("live source must be a Docker volume or bind")


def _auditor_digest() -> str:
    path = Path(__file__)
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        payload = _read_fd(descriptor)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_mode & 0o022
        or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
    ):
        raise MediaEvidenceError("auditor implementation identity is unsafe")
    return sha256_bytes(payload)


def _seal_media_record(record: dict[str, object]) -> dict[str, object]:
    audit = record.get("audit")
    if not isinstance(audit, dict) or "evidence_sha256" in audit:
        raise MediaEvidenceError("internal audit record is malformed")
    digest = sha256_bytes(canonical_json_bytes(record))
    return {
        **record,
        "audit": {
            **audit,
            "evidence_sha256": digest,
        },
    }


def _discovery_state_sha256(discovery: Discovery) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "media": [
                    discovery.media[media_id].document()
                    for media_id in sorted(discovery.media)
                ],
                "docker_inventory_sha256": (
                    discovery.docker_inventory_sha256
                ),
                "backup_inventory_sha256": (
                    discovery.backup_inventory_sha256
                ),
                "scanned_volume_names": list(
                    discovery.scanned_volume_names
                ),
                "scanned_container_ids": list(
                    discovery.scanned_container_ids
                ),
            }
        )
    )


def _scope_database_identity(
    database: dict[str, object],
    scope: str,
) -> dict[str, object]:
    identity = database.get("database_identity")
    if not isinstance(identity, dict):
        raise MediaEvidenceError("internal database identity is malformed")
    scoped = {**identity, "system_identifier_scope": scope}
    return {
        **database,
        "database_identity": scoped,
        "database_identity_sha256": sha256_bytes(
            canonical_json_bytes(scoped)
        ),
    }


def _write_private_atomic(directory: Path, name: str, payload: bytes) -> Path:
    directory_descriptor = _open_directory_chain(
        directory,
        private_from=directory,
        create_leaf=True,
    )
    temporary = f".{name}.tmp-{secrets.token_hex(16)}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            existing = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_descriptor,
            )
            try:
                metadata = os.fstat(existing)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_nlink != 1
                ):
                    raise MediaEvidenceError(
                        f"existing immutable evidence is unsafe: {name}"
                    )
                if _read_fd(existing) != payload:
                    raise MediaEvidenceError(
                        f"conflicting immutable evidence already exists: {name}"
                    )
            finally:
                os.close(existing)
        os.unlink(temporary, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
        os.close(directory_descriptor)
    return directory / name


def build_evidence(
    registry: Registry,
    discovery: Discovery,
    *,
    runner: CommandRunner,
    service_file: Path,
    evidence_root: Path,
    policy: DiscoveryPolicy = DiscoveryPolicy(),
    now: Callable[[], str] = utc_now,
) -> dict[str, object]:
    audit_image_id = _validate_audit_image(runner, registry.audit_image)
    auditor_sha256 = _auditor_digest()
    if auditor_sha256 != registry.auditor_sha256:
        raise MediaEvidenceError("auditor drifted after registry validation")
    service_file_sha256 = _private_service_file_digest(service_file)
    if service_file_sha256 != registry.service_file_sha256:
        raise MediaEvidenceError(
            "private PostgreSQL service file differs from registry authority"
        )
    discovery_state_before = _discovery_state_sha256(discovery)
    media_records: dict[str, dict[str, object]] = {}
    with tempfile.TemporaryDirectory(
        prefix="nexpoly-postgres-media-",
        dir=evidence_root,
    ) as temporary:
        os.chmod(temporary, 0o700)
        temporary_root = Path(temporary)
        for index, descriptor in enumerate(registry.descriptors):
            source = discovery.media[descriptor.media_id]
            workspace = temporary_root / f"medium-{index:04d}"
            workspace.mkdir(mode=0o700)
            audited_at = now()
            if RFC3339_UTC_RE.fullmatch(audited_at) is None:
                raise MediaEvidenceError("audit clock did not return UTC RFC3339")
            if descriptor.audit_method == "live-read-only":
                before = _live_source_identity(runner, source)
                source_system_identifier_before = (
                    _live_source_system_identifier(runner, source)
                )
                database = _run_live_audit(runner, descriptor, service_file)
                database = _scope_database_identity(
                    database,
                    "source-cluster",
                )
                source_system_identifier_after = (
                    _live_source_system_identifier(runner, source)
                )
                after = _live_source_identity(runner, source)
                if before != after:
                    raise MediaEvidenceError("online source identity changed during audit")
                if (
                    source_system_identifier_before
                    != source_system_identifier_after
                    or source_system_identifier_before
                    != database["database_identity"]["system_identifier"]
                ):
                    raise MediaEvidenceError(
                        "online service is not bound to the exact "
                        "Docker PGDATA cluster"
                    )
                source_system_identifier: str | None = (
                    source_system_identifier_before
                )
                source_digest = sha256_bytes(
                    canonical_json_bytes(
                        {
                            "database_identity": database["database_identity"],
                            "ledger": database["ledger"],
                            "ledger_relation": database["ledger_relation"],
                            "legacy_relation": database["legacy_relation"],
                        }
                    )
                )
                algorithm = "logical-database-identity-v2"
                isolation = {
                    "source_mounted_by_auditor": False,
                    "source_started_by_auditor": False,
                    "transaction_read_only": True,
                }
            elif descriptor.audit_method == "isolated-volume-copy-read-only":
                (
                    database,
                    source_digest,
                    before,
                    after,
                    isolation,
                ) = _isolated_volume_audit(
                    runner,
                    registry,
                    descriptor,
                    source,
                )
                database = _scope_database_identity(
                    database,
                    "copied-source-cluster",
                )
                source_system_identifier = str(
                    database["database_identity"]["system_identifier"]
                )
                algorithm = "postgres-data-directory-tar-sha256-v1"
            elif descriptor.audit_method == "isolated-bind-copy-read-only":
                (
                    database,
                    source_digest,
                    before,
                    after,
                    isolation,
                ) = _isolated_bind_audit(
                    runner,
                    registry,
                    descriptor,
                    source,
                    workspace,
                )
                database = _scope_database_identity(
                    database,
                    "copied-source-cluster",
                )
                source_system_identifier = str(
                    database["database_identity"]["system_identifier"]
                )
                algorithm = "postgres-private-tree-sha256-v1"
            else:
                (
                    database,
                    source_digest,
                    before,
                    after,
                    isolation,
                ) = _isolated_backup_audit(
                    runner,
                    registry,
                    descriptor,
                    source,
                    workspace,
                    policy=policy,
                )
                database = _scope_database_identity(
                    database,
                    "isolated-restore-cluster",
                )
                source_system_identifier = None
                algorithm = "sha256-file-v1"
            record: dict[str, object] = {
                "media_id": descriptor.media_id,
                "kind": descriptor.kind,
                "database": descriptor.database,
                "disposition": descriptor.disposition,
                "source_identity_before": before,
                "source_identity_after": after,
                "source_system_identifier": source_system_identifier,
                "source_content_sha256": source_digest,
                "content_identity_algorithm": algorithm,
                "database_identity": database["database_identity"],
                "database_identity_sha256": database[
                    "database_identity_sha256"
                ],
                "current_user": database["current_user"],
                "transaction_read_only": database["transaction_read_only"],
                "role_superuser": database["role_superuser"],
                "role_create_db": database["role_create_db"],
                "role_create_role": database["role_create_role"],
                "ledger": database["ledger"],
                "ledger_sha256": database["ledger_sha256"],
                "ledger_relation": database["ledger_relation"],
                "ledger_analysis": database["ledger_analysis"],
                "legacy_relation_present": database["legacy_relation_present"],
                "legacy_relation": database["legacy_relation"],
                "migration_0013": database["migration_0013"],
                "audit": {
                    "method": descriptor.audit_method,
                    "complete": True,
                    "auditor_sha256": auditor_sha256,
                    "postgres_major": POSTGRES_MAJOR,
                    "postgres_image": registry.audit_image,
                    "postgres_image_id": audit_image_id,
                    "pg_service_file_sha256": service_file_sha256,
                    "audited_at": audited_at,
                    "isolation": isolation,
                },
            }
            sealed = _seal_media_record(record)
            media_records[descriptor.media_id] = sealed
    if _private_service_file_digest(service_file) != service_file_sha256:
        raise MediaEvidenceError(
            "private PostgreSQL service file changed during audit"
        )
    final_discovery = discover_media(
        registry,
        runner=runner,
        policy=policy,
    )
    discovery_state_after = _discovery_state_sha256(final_discovery)
    if discovery_state_after != discovery_state_before:
        raise MediaEvidenceError(
            "external PostgreSQL discovery boundary changed during audit"
        )
    captured_at = now()
    if RFC3339_UTC_RE.fullmatch(captured_at) is None:
        raise MediaEvidenceError("audit clock did not return UTC RFC3339")
    for media_id, sealed in media_records.items():
        name = hashlib.sha256(media_id.encode("utf-8")).hexdigest()
        evidence_suffix = str(
            sealed["audit"]["evidence_sha256"]
        ).removeprefix("sha256:")
        _write_private_atomic(
            evidence_root,
            f"{name}-{evidence_suffix}.json",
            canonical_json_bytes(sealed) + b"\n",
        )
    required_records: list[dict[str, object]] = []
    for mapping in registry.required_online_databases:
        record = media_records[mapping["media_id"]]
        required_records.append(
            {
                "stack": mapping["stack"],
                "media_id": mapping["media_id"],
                "database": record["database"],
                "current_user": record["current_user"],
                "transaction_read_only": record["transaction_read_only"],
                "role_superuser": record["role_superuser"],
                "role_create_db": record["role_create_db"],
                "role_create_role": record["role_create_role"],
                "system_identifier": record["database_identity"]["system_identifier"],
                "database_identity_sha256": record["database_identity_sha256"],
                "ledger": record["ledger"],
                "ledger_sha256": record["ledger_sha256"],
                "legacy_relation_present": record["legacy_relation_present"],
            }
        )
    requires_0014 = any(
        record["migration_0013"]["state"] == "superseded-requires-0014"
        for record in media_records.values()
    )
    envelope = {
        "schema_version": 2,
        "inventory_complete": True,
        "writable_target": {"stack": "production", "database": "nexpoly"},
        "media_registry": {
            "schema_version": 2,
            "sha256": registry.digest,
            "discovery_boundary_sha256": sha256_bytes(
                canonical_json_bytes(registry.boundary)
            ),
            "discovery_state_sha256_before": discovery_state_before,
            "discovery_state_sha256_after": discovery_state_after,
            "captured_at": captured_at,
            "expected_media_ids": sorted(media_records),
            "discovered_media_ids": sorted(discovery.media),
            "docker_inventory_sha256": discovery.docker_inventory_sha256,
            "backup_inventory_sha256": discovery.backup_inventory_sha256,
            "scanned_volume_names": list(discovery.scanned_volume_names),
            "scanned_container_ids": list(discovery.scanned_container_ids),
        },
        "databases": required_records,
        "media": [media_records[name] for name in sorted(media_records)],
        "requires_0014": requires_0014,
    }
    envelope_payload = canonical_json_bytes(envelope) + b"\n"
    envelope_digest = hashlib.sha256(envelope_payload).hexdigest()
    _write_private_atomic(
        evidence_root,
        f"external-database-audit-{envelope_digest}.json",
        envelope_payload,
    )
    return envelope


def _private_service_file_digest(path: Path) -> str:
    descriptor = open_private_regular(path, root=path.parent)
    try:
        before = os.fstat(descriptor)
        payload = _read_fd(descriptor, MAX_REGISTRY_BYTES)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise MediaEvidenceError(
            "private PostgreSQL service file changed while being read"
        )
    return sha256_bytes(payload)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    subparsers = value.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser(
        "build",
        help="discover all fixed media and build fresh isolated evidence",
    )
    build.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    build.add_argument("--service-file", type=Path, default=DEFAULT_SERVICE_FILE)
    build.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    build.add_argument(
        "--expected-registry-sha256",
        required=True,
        help="full sha256:<hex> pinned by the prepared bridge descriptor",
    )
    return value


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.command != "build":
        raise AssertionError("argparse accepted an unknown command")
    if DIGEST_RE.fullmatch(arguments.expected_registry_sha256) is None:
        print(
            "postgres-media-evidence: error: expected registry digest is invalid",
            file=sys.stderr,
        )
        return 2
    try:
        registry = load_registry(arguments.registry)
        if registry.digest != arguments.expected_registry_sha256:
            raise MediaEvidenceError("media registry digest differs from authority")
        if (
            _private_service_file_digest(arguments.service_file)
            != registry.service_file_sha256
        ):
            raise MediaEvidenceError(
                "private PostgreSQL service file differs from registry authority"
            )
        evidence_root = arguments.evidence_root.absolute()
        evidence_descriptor = _open_directory_chain(
            evidence_root,
            private_from=evidence_root,
            create_leaf=True,
        )
        os.close(evidence_descriptor)
        runner = CommandRunner()
        # Validate availability, exact local digest, PG16 and the fixed shell
        # toolchain before any dormant source is mounted for discovery.
        _validate_audit_image(runner, registry.audit_image)
        discovery = discover_media(registry, runner=runner)
        envelope = build_evidence(
            registry,
            discovery,
            runner=runner,
            service_file=arguments.service_file,
            evidence_root=evidence_root,
        )
    except (MediaEvidenceError, OSError, subprocess.SubprocessError) as exc:
        print(f"postgres-media-evidence: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(envelope, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
