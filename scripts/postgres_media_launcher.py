#!/usr/bin/python3 -I
"""Manifest-pinned launcher for PostgreSQL external-media evidence."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RELEASE_ID_RE = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_NAME = "CONTROL-MANIFEST.json"
AUTHORITY_NAME = "postgres-media-authority-rules.json"
ROLE_SQL_NAME = "postgres-media-audit-role.sql.example"
IMPLEMENTATION_NAME = "postgres_media_evidence.py"
LAUNCHER_NAME = "postgres_media_launcher.py"
AUTHORITY_ENV = (
    "NEXPOLY_CONTRACT_0012_MEDIA_AUTHORITY_RULES_SHA256"
)
ROLE_SQL_ENV = "NEXPOLY_CONTRACT_0012_AUDIT_ROLE_SQL_SHA256"
LAUNCHER_ENV = "NEXPOLY_MEDIA_LAUNCHER_SHA256"
IMPLEMENTATION_ENV = "NEXPOLY_MEDIA_IMPLEMENTATION_SHA256"
DATABASE_CREDENTIALS_FD_ENV = (
    "NEXPOLY_MEDIA_DATABASE_CREDENTIALS_FD"
)
DATABASE_CREDENTIALS_DIGEST_ENV = (
    "NEXPOLY_MEDIA_DATABASE_CREDENTIALS_SHA256"
)
DATABASE_CREDENTIALS_NAME = "postgres-media-credentials.json"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_DATABASE_CREDENTIALS_BYTES = 1024 * 1024


def fail(message: str) -> "NoReturn":
    raise SystemExit(message)


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def read_fd(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            fail("PostgreSQL media control file exceeds its size limit")


def stable_file(
    parent_descriptor: int,
    name: str,
    *,
    expected: dict[str, object],
) -> tuple[int, str]:
    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    try:
        before = os.fstat(descriptor)
        payload = read_fd(descriptor, MAX_FILE_BYTES)
        after = os.fstat(descriptor)
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o700
            or before.st_nlink != 1
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            or expected
            != {
                "sha256": digest,
                "size": len(payload),
                "mode": 0o700,
            }
        ):
            fail(f"PostgreSQL media control file differs: {name}")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor, digest
    except BaseException:
        os.close(descriptor)
        raise


def stable_private_runtime_file(path: Path) -> tuple[int, str]:
    """Open one fixed runtime secret without accepting caller path authority."""

    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        fail(
            "PostgreSQL media database credential envelope is unavailable"
        )
        raise AssertionError from exc
    try:
        before = os.fstat(descriptor)
        payload = read_fd(descriptor, MAX_DATABASE_CREDENTIALS_BYTES)
        after = os.fstat(descriptor)
        if (
            not payload
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        ):
            fail(
                "PostgreSQL media database credential envelope is unsafe"
            )
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor, digest
    except BaseException:
        os.close(descriptor)
        raise


if not sys.flags.isolated:
    fail("postgres-media launcher requires isolated Python")

release = Path(__file__).resolve().parent
runtime_authority = (
    release.parent.parent
    / "config"
    / "postgres-media-authority-rules.json"
)
runtime_database_credentials = (
    release.parent.parent
    / "config"
    / DATABASE_CREDENTIALS_NAME
)
selected_root = os.environ.get("NEXPOLY_ACTIVE_CONTROL_ROOT")
release_id = os.environ.get("NEXPOLY_ACTIVE_CONTROL_RELEASE_ID")
if (
    selected_root != str(release)
    or not isinstance(release_id, str)
    or RELEASE_ID_RE.fullmatch(release_id) is None
    or release.name != release_id
):
    fail("PostgreSQL media launcher lacks selector authority")

parent_descriptor = os.open(
    release,
    os.O_RDONLY
    | os.O_DIRECTORY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0),
)
implementation_descriptor: int | None = None
role_descriptor: int | None = None
database_credentials_descriptor: int | None = None
try:
    parent_metadata = os.fstat(parent_descriptor)
    if (
        parent_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
    ):
        fail("PostgreSQL media control release is unsafe")
    manifest_descriptor = os.open(
        MANIFEST_NAME,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    try:
        manifest_metadata = os.fstat(manifest_descriptor)
        manifest_payload = read_fd(manifest_descriptor, MAX_FILE_BYTES)
    finally:
        os.close(manifest_descriptor)
    try:
        manifest = json.loads(manifest_payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        fail("PostgreSQL media control manifest is invalid")
    if (
        not stat.S_ISREG(manifest_metadata.st_mode)
        or manifest_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(manifest_metadata.st_mode) != 0o600
        or manifest_metadata.st_nlink != 1
        or not isinstance(manifest, dict)
        or manifest.get("release_id") != release_id
        or "files" not in manifest
        or not isinstance(manifest["files"], dict)
        or (
            hashlib.sha256(
                canonical_json_bytes(
                    {
                        key: value
                        for key, value in manifest.items()
                        if key != "release_id"
                    }
                )
            ).hexdigest()
            != release_id
        )
    ):
        fail("PostgreSQL media control manifest differs")
    required = {
        name: manifest["files"].get(name)
        for name in (
            IMPLEMENTATION_NAME,
            AUTHORITY_NAME,
            ROLE_SQL_NAME,
        )
    }
    if any(not isinstance(value, dict) for value in required.values()):
        fail("PostgreSQL media control closure is incomplete")
    implementation_descriptor, implementation_digest = stable_file(
        parent_descriptor,
        IMPLEMENTATION_NAME,
        expected=required[IMPLEMENTATION_NAME],
    )
    authority_descriptor, authority_digest = stable_file(
        parent_descriptor,
        AUTHORITY_NAME,
        expected=required[AUTHORITY_NAME],
    )
    os.close(authority_descriptor)
    role_descriptor, role_sql_digest = stable_file(
        parent_descriptor,
        ROLE_SQL_NAME,
        expected=required[ROLE_SQL_NAME],
    )
finally:
    os.close(parent_descriptor)

expected_authority = os.environ.get(AUTHORITY_ENV, "")
expected_role_sql = os.environ.get(ROLE_SQL_ENV, "")
if (
    expected_authority != authority_digest
    or expected_role_sql != role_sql_digest
    or os.environ.get(IMPLEMENTATION_ENV) != implementation_digest
    or os.environ.get(LAUNCHER_ENV)
    != manifest["files"].get(LAUNCHER_NAME, {}).get("sha256")
):
    fail("PostgreSQL media authority differs from the F control release")
runtime_descriptor = os.open(
    runtime_authority,
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0),
)
try:
    runtime_metadata = os.fstat(runtime_descriptor)
    runtime_payload = read_fd(runtime_descriptor, MAX_FILE_BYTES)
finally:
    os.close(runtime_descriptor)
if (
    not stat.S_ISREG(runtime_metadata.st_mode)
    or runtime_metadata.st_uid != os.geteuid()
    or stat.S_IMODE(runtime_metadata.st_mode) != 0o600
    or runtime_metadata.st_nlink != 1
    or "sha256:" + hashlib.sha256(runtime_payload).hexdigest()
    != authority_digest
):
    fail("runtime PostgreSQL media authority differs from exact F")

database_credentials_descriptor, database_credentials_digest = (
    stable_private_runtime_file(runtime_database_credentials)
)
assert implementation_descriptor is not None
assert role_descriptor is not None
assert database_credentials_descriptor is not None
os.set_inheritable(implementation_descriptor, True)
os.set_inheritable(role_descriptor, True)
os.set_inheritable(database_credentials_descriptor, True)
implementation_path = f"/proc/self/fd/{implementation_descriptor}"
os.execve(
    "/usr/bin/python3",
    [
        "/usr/bin/python3",
        "-I",
        "-B",
        implementation_path,
        *sys.argv[1:],
    ],
    {
        "HOME": "/nonexistent",
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
        AUTHORITY_ENV: authority_digest,
        "NEXPOLY_MEDIA_AUDITOR_SHA256": implementation_digest,
        "NEXPOLY_ACTIVE_CONTROL_ROOT": str(release),
        "NEXPOLY_ACTIVE_CONTROL_RELEASE_ID": release_id,
        "NEXPOLY_MEDIA_AUDIT_ROLE_SQL_SHA256": role_sql_digest,
        "NEXPOLY_MEDIA_AUDIT_ROLE_SQL_FD": str(role_descriptor),
        DATABASE_CREDENTIALS_DIGEST_ENV: database_credentials_digest,
        DATABASE_CREDENTIALS_FD_ENV: str(
            database_credentials_descriptor
        ),
    },
)
