#!/usr/bin/env python3
"""Immutable router for content-addressed production control releases.

Only this module and the tiny stable wrappers live in ``runtime/bin``.  Every
controller, maintenance helper, and Worker launcher is loaded from an
immutable, manifest-sealed release below ``runtime/control-releases``.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from typing import Any, Iterable, Mapping


PROTOCOL_VERSION = 1
SOURCE_MANIFEST_SCHEMA_VERSION = 1
CONTROL_MANIFEST_SCHEMA_VERSION = 1
CONTROL_CANDIDATE_SCHEMA_VERSION = 1
ACTIVE_CONTROL_SCHEMA_VERSION = 1
PRODUCTION_RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
SOURCE_MANIFEST_RELATIVE_PATH = "scripts/control-release.json"
CONTROL_MANIFEST_NAME = "CONTROL-MANIFEST.json"
BOOTSTRAP_AUTHORITY_NAME = "bootstrap-control.json"
ADOPTED_DEPLOYMENT_NAME = "adopted-deployment.json"
ADOPTION_AUTHORITY_KIND = "manual-runtime-adoption"
BOOTSTRAP_ROUTER_INTENT_NAME = "bootstrap-router-successor-intent.json"
BOOTSTRAP_ROUTER_AUTHORITY_NAME = "bootstrap-router-successor.json"
BOOTSTRAP_ROUTER_AUTHORITY_KIND = (
    "manual-runtime-adoption-bootstrap-router-successor"
)
BOOTSTRAP_ROUTER_POLICY = "nexpoly-bootstrap-router-successor-v1"
BOOTSTRAP_ROUTER_ROOT_NAME = "bootstrap-router-successors"
PRODUCTION_GIT_SNAPSHOT_AUTHORITY_NAME = "production-git-snapshot.json"
SOURCE_SUCCESSOR_AUTHORITY_NAME = (
    "adopted-git-permission-source-successor.json"
)
UNIT_PERMISSION_AUTHORITY_NAME = "adopted-unit-permissions.json"
BOOTSTRAP_IMMUTABLE_FILES = {
    "control_runtime_selector.py",
    "nexpoly-pull-deploy",
    "nexpoly-postgres-media-evidence",
    "nexpoly-production-readiness",
    "nexpoly-pull-contract-0012",
    "nexpoly-reconcile-production-0005-polytao-alias",
}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
RELEASE_ID_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
SAFE_ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
OPERATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
SAFE_CONFIG_RE = re.compile(r"^config/[a-z][a-z0-9_.-]{0,127}$")
CONTROL_DATA_SOURCES = {
    "ops/config/postgres-media-authority-rules.json": (
        "postgres-media-authority-rules.json"
    ),
    "ops/config/postgres-media-audit-role.sql.example": (
        "postgres-media-audit-role.sql.example"
    ),
}
ALIAS_MARKER_RELATIVE = Path("state/maintenance/0005-polytao-alias/operation.json")
ALIAS_AUDIT_ROOT_RELATIVE = Path("audit/maintenance/0005-polytao-alias")
ALIAS_BACKUP_ROOT_RELATIVE = Path("backups/maintenance/0005-polytao-alias")
ALIAS_ACTION = "reconcile-production-0005-polytao-alias"
ALIAS_VERSION = "0005_polytao_jobs"
ALIAS_CHECKSUM = (
    "b15268a475e8daf8dd58be988a228a0440e59a31dbf11d5d6b52e0974c3daab5"
)
ALIAS_APPLIED_AT = "2026-07-08T03:44:05.662979Z"
ALIAS_RESTORE_IMAGE = (
    "postgres:16-alpine@sha256:"
    "57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
)
ALIAS_SYSTEM_IDENTIFIER = "7659245354718314530"
ALIAS_DATABASE_ENDPOINT = {
    "host_sha256": "12ca17b49af2289436f303e0166030a21e525d266e209267433801a8fd4071a0",
    "port": 55432,
    "database": "nexpoly",
    "user": "polyprop",
    "sslmode": "disable",
}
ALIAS_CANONICAL_LEDGER = [
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
    ("0007_polytao_jobs", ALIAS_CHECKSUM),
    (
        "0008_polytao_backend_runtime",
        "d0d8b2187aad8657269600873d3d2630e30c7d72da2f6662e18ab22031deff90",
    ),
]
ALIAS_PRE_LEDGER = sorted(
    [*ALIAS_CANONICAL_LEDGER, (ALIAS_VERSION, ALIAS_CHECKSUM)]
)
ALIAS_POST_LEDGER = sorted(ALIAS_CANONICAL_LEDGER)
ADOPTED_POST_0013_LEDGER = sorted(
    [
        *ALIAS_CANONICAL_LEDGER,
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
            "ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc",
        ),
    ]
)
ADOPTED_DFT_GPU_UUID = "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe"
MONOMER_DFT_GPU_INDEX = "2"
MONOMER_DFT_GUARD_STATE = (
    PRODUCTION_RUNTIME_ROOT / "state/gpu2-guard.json"
)
MONOMER_DFT_UNIT_TARGET = Path(
    "/home/devuser/.config/systemd/user/nexpoly-monomer-dft-worker.service"
)
MONOMER_DFT_ENV_KEYS = frozenset(
    {
        "MONOMER_DFT_RELEASE_SHA",
        "MONOMER_DFT_RUNTIME_CONTRACT_SHA256",
        "MONOMER_DFT_RUNTIME_INVENTORY_SHA256",
        "MONOMER_DFT_PYTHON",
        "AIMNET_CACHE_DIR",
        "WARP_CACHE_PATH",
        "NEXPOLY_DFT_GPU_GUARD_MODE",
    }
)
MONOMER_DFT_MODEL_FILES = frozenset(
    {
        "aimnet2-pd_0.pt",
        "aimnet2_2025_b973c_d3_0.pt",
        "aimnet2_b973c_d3_0.pt",
        "aimnet2_rxn_0.pt",
        "aimnet2_wb97m_d3_0.pt",
        "aimnet2nse_wb97m_0.pt",
    }
)
ADOPTED_DFT_RUNTIME_SYMLINKS = {
    "venv/bin/python": "/usr/bin/python3.12",
    "venv/bin/python3": "python",
    "venv/bin/python3.12": "python",
    "venv/lib64": "lib",
}
ALIAS_EXPECTED_SCHEMA_SHA256 = (
    "8594868c661024af0766627a2d48280fc6967b8efe445878fc2a252a4520000c"
)
ALIAS_EXPECTED_STRUCTURE_COUNTS = {
    "columns": 23,
    "indexes": 3,
    "constraints": 6,
    "triggers": 0,
}
ALIAS_EXPECTED_LEDGER_SCHEMA_SHA256 = (
    "db77ff078329ed4ec8b00f70172be743b9f3e67924d27716fba26277466ecfdd"
)
ALIAS_EXPECTED_LEDGER_STRUCTURE_COUNTS = {
    "columns": 3,
    "indexes": 1,
    "constraints": 1,
    "triggers": 0,
}
ALIAS_AUDIT_NAMES = {
    "pg-restore.list",
    "isolated-postgres16-restore.json",
    "database-after.json",
    "external-database-alias-transition.json",
    "AUDIT-MANIFEST.json",
}
ALIAS_BACKUP_NAMES = {
    "nexpoly-before.dump",
    "nexpoly-before.dump.sha256",
}
REQUIRED_COMPATIBILITY = {
    "handoff_protocol_versions": 1,
    "descriptor_schema_versions": 2,
    "current_state_schema_versions": 2,
    "marker_schema_versions": 2,
    "worker_slot_schema_versions": 2,
    "prepare_abort_abi_versions": 1,
}
PINNED_PYTHON_BOOTSTRAP = r"""
import hashlib
import json
import os
import stat
import sys


def fail(message):
    raise SystemExit("sealed control bootstrap: " + message)


def read_regular(path, expected, maximum):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > maximum:
                fail("release file is oversized")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or identity
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        or stat.S_IMODE(after.st_mode) != expected["mode"]
        or len(payload) != expected["size"]
        or "sha256:" + hashlib.sha256(payload).hexdigest()
        != expected["sha256"]
    ):
        fail("release file identity differs")
    return bytes(payload)


entry_descriptor = int(sys.argv[1])
entry_path = os.path.abspath(sys.argv[2])
release_root = os.path.abspath(sys.argv[3])
try:
    expected_manifest = json.loads(sys.argv[4])
    expected_files = json.loads(sys.argv[5])
except (TypeError, ValueError):
    fail("file manifest is invalid")
arguments = sys.argv[6:]
if (
    os.path.dirname(entry_path) != release_root
    or not isinstance(expected_manifest, dict)
    or set(expected_manifest) != {"sha256", "size", "mode"}
    or not isinstance(expected_files, dict)
    or not expected_files
    or any(
        not isinstance(name, str)
        or not name
        or name in {".", "..", "CONTROL-MANIFEST.json"}
        or "/" in name
        or "\x00" in name
        or not isinstance(record, dict)
        or set(record) != {"sha256", "size", "mode"}
        for name, record in expected_files.items()
    )
):
    fail("release identity is invalid")
try:
    root_metadata = os.lstat(release_root)
    actual_names = set(os.listdir(release_root))
except OSError:
    fail("release directory is unavailable")
if (
    not stat.S_ISDIR(root_metadata.st_mode)
    or stat.S_ISLNK(root_metadata.st_mode)
    or root_metadata.st_uid != os.geteuid()
    or stat.S_IMODE(root_metadata.st_mode) != 0o700
    or actual_names != set(expected_files) | {"CONTROL-MANIFEST.json"}
):
    fail("release directory identity differs")
for name, expected in sorted(expected_files.items()):
    read_regular(os.path.join(release_root, name), expected, 16 * 1024 * 1024)
manifest_path = os.path.join(release_root, "CONTROL-MANIFEST.json")
manifest_payload = read_regular(
    manifest_path,
    expected_manifest,
    16 * 1024 * 1024,
)
try:
    manifest = json.loads(manifest_payload)
except (TypeError, ValueError):
    fail("release manifest payload is invalid")
if (
    not isinstance(manifest, dict)
    or manifest.get("files") != expected_files
    or manifest.get("release_id") != os.path.basename(release_root)
):
    fail("release manifest binding differs")
entry_name = os.path.basename(entry_path)
entry_expected = expected_files.get(entry_name)
if not isinstance(entry_expected, dict):
    fail("entrypoint is absent from release")
os.lseek(entry_descriptor, 0, os.SEEK_SET)
before = os.fstat(entry_descriptor)
payload = bytearray()
while True:
    chunk = os.read(entry_descriptor, 1024 * 1024)
    if not chunk:
        break
    payload.extend(chunk)
    if len(payload) > 16 * 1024 * 1024:
        fail("entrypoint is oversized")
after = os.fstat(entry_descriptor)
if (
    not stat.S_ISREG(before.st_mode)
    or before.st_uid != os.geteuid()
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
    or stat.S_IMODE(after.st_mode) != entry_expected["mode"]
    or len(payload) != entry_expected["size"]
    or "sha256:" + hashlib.sha256(payload).hexdigest()
    != entry_expected["sha256"]
):
    fail("pinned entrypoint identity differs")
os.lseek(entry_descriptor, 0, os.SEEK_SET)
sys.argv = [entry_path, *arguments]
if not sys.path or sys.path[0] != release_root:
    sys.path.insert(0, release_root)
namespace = {
    "__name__": "__main__",
    "__file__": entry_path,
    "__package__": None,
    "__cached__": None,
    "__spec__": None,
}
exec(compile(bytes(payload), entry_path, "exec"), namespace, namespace)
"""


class ControlRuntimeError(RuntimeError):
    """Fail-closed control release validation error."""


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def canonical_json_digest(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def adopted_dft_runtime_inventory(root: Path) -> str:
    """Recompute the immutable portion of an adopted legacy DFT runtime."""

    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise ControlRuntimeError("adopted monomer DFT runtime is unavailable") from exc
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root.is_symlink()
        or root_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
    ):
        raise ControlRuntimeError("adopted monomer DFT runtime root is unsafe")

    def immutable_paths(directory: Path) -> Iterable[Path]:
        try:
            with os.scandir(directory) as stream:
                entries = sorted(stream, key=lambda entry: entry.name)
        except OSError as exc:
            raise ControlRuntimeError(
                "adopted monomer DFT runtime changed during inventory"
            ) from exc
        for entry in entries:
            path = directory / entry.name
            yield path
            relative = path.relative_to(root).as_posix()
            if relative == "warp-cache":
                continue
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                raise ControlRuntimeError(
                    "adopted monomer DFT runtime changed during inventory"
                ) from exc
            if is_directory:
                yield from immutable_paths(path)

    records: list[dict[str, object]] = [
        {
            "path": ".",
            "kind": "directory",
            "uid": root_metadata.st_uid,
            "mode": stat.S_IMODE(root_metadata.st_mode),
            "nlink": root_metadata.st_nlink,
        }
    ]
    observed_links: set[str] = set()
    warp_root_seen = False
    for path in immutable_paths(root):
        relative = path.relative_to(root).as_posix()
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ControlRuntimeError(
                "adopted monomer DFT runtime changed during inventory"
            ) from exc
        mode = stat.S_IMODE(metadata.st_mode)
        if metadata.st_uid != os.geteuid() or metadata.st_nlink < 1:
            raise ControlRuntimeError(
                "adopted monomer DFT runtime ownership is unsafe"
            )
        if relative == "warp-cache":
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or path.is_symlink()
                or mode != 0o700
            ):
                raise ControlRuntimeError(
                    "adopted monomer DFT mutable Warp cache root is unsafe"
                )
            warp_root_seen = True
            records.append(
                {
                    "path": relative,
                    "kind": "mutable-directory",
                    "uid": metadata.st_uid,
                    "mode": mode,
                }
            )
            continue
        if stat.S_ISLNK(metadata.st_mode):
            try:
                target = os.readlink(path)
                after = path.lstat()
            except OSError as exc:
                raise ControlRuntimeError(
                    "adopted monomer DFT runtime symlink changed"
                ) from exc
            if (
                ADOPTED_DFT_RUNTIME_SYMLINKS.get(relative) != target
                or (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_uid,
                    metadata.st_nlink,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                )
                != (
                    after.st_dev,
                    after.st_ino,
                    after.st_mode,
                    after.st_uid,
                    after.st_nlink,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
            ):
                raise ControlRuntimeError(
                    "adopted monomer DFT runtime contains an unknown symlink"
                )
            observed_links.add(relative)
            records.append(
                {
                    "path": relative,
                    "kind": "symlink",
                    "uid": metadata.st_uid,
                    "mode": mode,
                    "nlink": metadata.st_nlink,
                    "target": target,
                }
            )
            continue
        if stat.S_ISDIR(metadata.st_mode):
            if mode & 0o022:
                raise ControlRuntimeError("adopted monomer DFT runtime mode is unsafe")
            records.append(
                {
                    "path": relative,
                    "kind": "directory",
                    "uid": metadata.st_uid,
                    "mode": mode,
                    "nlink": metadata.st_nlink,
                }
            )
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ControlRuntimeError(
                "adopted monomer DFT runtime contains a special file"
            )
        if (relative != "venv/.lock" and mode & 0o022) or (
            relative == "venv/.lock" and mode != 0o666
        ):
            raise ControlRuntimeError("adopted monomer DFT runtime mode is unsafe")
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as exc:
            raise ControlRuntimeError(
                "adopted monomer DFT runtime file is unavailable"
            ) from exc
        try:
            before = os.fstat(descriptor)
            file_digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                file_digest.update(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or identity
            != (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_uid,
                metadata.st_nlink,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
            or identity
            != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_uid,
                after.st_nlink,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        ):
            raise ControlRuntimeError(
                "adopted monomer DFT runtime file changed during inventory"
            )
        records.append(
            {
                "path": relative,
                "kind": "file",
                "uid": before.st_uid,
                "mode": stat.S_IMODE(before.st_mode),
                "nlink": before.st_nlink,
                "size": before.st_size,
                "sha256": "sha256:" + file_digest.hexdigest(),
            }
        )
    if observed_links != set(ADOPTED_DFT_RUNTIME_SYMLINKS) or not warp_root_seen:
        raise ControlRuntimeError("adopted monomer DFT runtime layout is incomplete")
    return canonical_json_digest(records)


def governed_dft_runtime_inventory(root: Path) -> str:
    """Recompute a controller-built immutable DFT release inventory."""

    allowed_links = ADOPTED_DFT_RUNTIME_SYMLINKS
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative in {"READY.json", ".preparing.json"}:
            continue
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if metadata.st_uid != os.geteuid():
            raise ControlRuntimeError("governed monomer DFT runtime owner differs")
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(path)
            if allowed_links.get(relative) != target:
                raise ControlRuntimeError("governed monomer DFT runtime link differs")
            records.append(
                {
                    "path": relative,
                    "kind": "symlink",
                    "uid": metadata.st_uid,
                    "mode": mode,
                    "target": target,
                }
            )
        elif stat.S_ISDIR(metadata.st_mode):
            if mode & 0o022:
                raise ControlRuntimeError("governed monomer DFT runtime mode differs")
            records.append(
                {
                    "path": relative,
                    "kind": "directory",
                    "uid": metadata.st_uid,
                    "mode": mode,
                }
            )
        elif stat.S_ISREG(metadata.st_mode):
            if mode & 0o022 or metadata.st_nlink != 1:
                raise ControlRuntimeError("governed monomer DFT runtime file differs")
            records.append(
                {
                    "path": relative,
                    "kind": "file",
                    "uid": metadata.st_uid,
                    "mode": mode,
                    "size": metadata.st_size,
                    "sha256": sha256_file(path),
                }
            )
        else:
            raise ControlRuntimeError(
                "governed monomer DFT runtime contains a special file"
            )
    return canonical_json_digest(records)


def _open_verified_control_file(
    release: Path,
    manifest: Mapping[str, Any],
    name: str,
) -> int:
    """Pin one manifest-authenticated control inode across exec."""

    expected = manifest.get("files", {}).get(name)
    if not isinstance(expected, dict):
        raise ControlRuntimeError(
            f"control release lacks executable file: {name}"
        )
    descriptor = os.open(
        release / name,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            if size > 16 * 1024 * 1024:
                raise ControlRuntimeError(
                    f"control release file is oversized: {name}"
                )
        after = os.fstat(descriptor)
        observed = {
            "sha256": "sha256:" + digest.hexdigest(),
            "size": size,
            "mode": stat.S_IMODE(after.st_mode),
        }
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
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
            or observed != expected
        ):
            raise ControlRuntimeError(
                f"control release file changed before exec: {name}"
            )
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.set_inheritable(descriptor, True)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _control_manifest_identity(release: Path) -> dict[str, int | str]:
    """Seal the manifest bytes passed to the pinned-source bootstrap."""

    path = release / CONTROL_MANIFEST_NAME
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            if size > 16 * 1024 * 1024:
                raise ControlRuntimeError("control release manifest is oversized")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != 0o600
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
        raise ControlRuntimeError("control release manifest changed before exec")
    return {
        "sha256": "sha256:" + digest.hexdigest(),
        "size": size,
        "mode": 0o600,
    }


def release_identity(document_without_release_id: Mapping[str, Any]) -> str:
    return canonical_json_digest(document_without_release_id).removeprefix("sha256:")


def _require_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ControlRuntimeError(f"control directory is unavailable: {path}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ControlRuntimeError(f"control directory is unsafe: {path}")


def _load_private_json(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        payload = path.read_bytes()
    except OSError as exc:
        raise ControlRuntimeError(f"control record is unavailable: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or len(payload) > 1024 * 1024
    ):
        raise ControlRuntimeError(f"control record is unsafe: {path}")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ControlRuntimeError(f"control record is invalid: {path}") from exc
    if not isinstance(value, dict):
        raise ControlRuntimeError(f"control record is invalid: {path}")
    return value


def _load_private_canonical_json(
    path: Path,
    *,
    maximum: int = 4 * 1024 * 1024,
) -> tuple[dict[str, Any], str]:
    """Load one stable, owner-private canonical authority and its raw digest."""

    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise ControlRuntimeError(f"control authority is unavailable: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or not 1 <= before.st_size <= maximum
        ):
            raise ControlRuntimeError(f"control authority is unsafe: {path}")
        payload = bytearray()
        while len(payload) < before.st_size:
            block = os.read(
                descriptor,
                min(1024 * 1024, before.st_size - len(payload)),
            )
            if not block:
                raise ControlRuntimeError(
                    f"control authority changed while reading: {path}"
                )
            payload.extend(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ControlRuntimeError(f"control authority changed while reading: {path}")
    raw = bytes(payload)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ControlRuntimeError(f"control authority is invalid: {path}") from exc
    if (
        not isinstance(document, dict)
        or raw != canonical_json_bytes(document) + b"\n"
    ):
        raise ControlRuntimeError(f"control authority is not canonical: {path}")
    return document, sha256_bytes(raw)


def _private_file_identity(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ControlRuntimeError(f"alias evidence is unavailable: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ControlRuntimeError(f"alias evidence is unsafe: {path}")
    return {
        "size": metadata.st_size,
        "sha256": sha256_file(path).removeprefix("sha256:"),
        "mode": 0o600,
    }


def _alias_evidence_files(
    audit_dir: Path, backup_dir: Path
) -> dict[str, dict[str, Any]]:
    paths = {
        "audit/pg-restore.list": audit_dir / "pg-restore.list",
        "audit/isolated-postgres16-restore.json": (
            audit_dir / "isolated-postgres16-restore.json"
        ),
        "audit/database-after.json": audit_dir / "database-after.json",
        "audit/external-database-alias-transition.json": (
            audit_dir / "external-database-alias-transition.json"
        ),
        "backup/nexpoly-before.dump": backup_dir / "nexpoly-before.dump",
        "backup/nexpoly-before.dump.sha256": (
            backup_dir / "nexpoly-before.dump.sha256"
        ),
    }
    return {name: _private_file_identity(path) for name, path in paths.items()}


def _alias_ledger_pairs(value: object) -> list[tuple[str, str]] | None:
    if not isinstance(value, list):
        return None
    pairs: list[tuple[str, str]] = []
    for row in value:
        if (
            not isinstance(row, dict)
            or set(row) != {"version", "checksum", "applied_at"}
            or not isinstance(row.get("version"), str)
            or not isinstance(row.get("checksum"), str)
            or not isinstance(row.get("applied_at"), str)
            or not row["applied_at"]
        ):
            return None
        pairs.append((row["version"], row["checksum"]))
    return pairs


def _alias_archive_is_valid(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "row_count",
        "status_counts",
        "rows_sha256",
        "schema_sha256",
        "structure_counts",
    }:
        return False
    row_count = value.get("row_count")
    status_counts = value.get("status_counts")
    return bool(
        not isinstance(row_count, bool)
        and isinstance(row_count, int)
        and row_count >= 0
        and isinstance(status_counts, dict)
        and all(
            isinstance(status, str)
            and bool(status)
            and not isinstance(count, bool)
            and isinstance(count, int)
            and count >= 0
            for status, count in status_counts.items()
        )
        and sum(status_counts.values()) == row_count
        and isinstance(value.get("rows_sha256"), str)
        and HEX_DIGEST_RE.fullmatch(value["rows_sha256"]) is not None
        and value.get("schema_sha256") == ALIAS_EXPECTED_SCHEMA_SHA256
        and value.get("structure_counts") == ALIAS_EXPECTED_STRUCTURE_COUNTS
    )


def _alias_restore_image_is_valid(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == {"digest_ref", "image_id"}
        and value.get("digest_ref") == ALIAS_RESTORE_IMAGE
        and isinstance(value.get("image_id"), str)
        and DIGEST_RE.fullmatch(value["image_id"]) is not None
    )


def _alias_relation_is_valid(value: object, *, owner: str) -> bool:
    return value == {
        "kind": "r",
        "persistence": "p",
        "is_partition": False,
        "row_security": False,
        "force_row_security": False,
        "owner": owner,
        "parents": 0,
        "children": 0,
    }


def _alias_live_inventory_is_valid(
    value: object,
    *,
    ledger: list[tuple[str, str]],
    archive: object | None = None,
) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "database",
        "current_user",
        "database_owner",
        "server_version_num",
        "in_recovery",
        "system_identifier",
        "ledger",
        "archive",
        "ledger_schema_sha256",
        "ledger_structure_counts",
        "polytao_relation",
        "ledger_relation",
    }:
        return False
    rows = value.get("ledger")
    alias_rows = (
        [row for row in rows if row.get("version") == ALIAS_VERSION]
        if isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
        else []
    )
    return bool(
        value.get("database") == ALIAS_DATABASE_ENDPOINT["database"]
        and value.get("current_user") == ALIAS_DATABASE_ENDPOINT["user"]
        and value.get("database_owner") == "polyprop"
        and not isinstance(value.get("server_version_num"), bool)
        and isinstance(value.get("server_version_num"), int)
        and 160000 <= value["server_version_num"] < 170000
        and value.get("in_recovery") is False
        and str(value.get("system_identifier")) == ALIAS_SYSTEM_IDENTIFIER
        and _alias_ledger_pairs(rows) == ledger
        and (
            not any(pair[0] == ALIAS_VERSION for pair in ledger)
            or len(alias_rows) == 1
            and alias_rows[0].get("applied_at") == ALIAS_APPLIED_AT
        )
        and (
            _alias_archive_is_valid(value.get("archive"))
            if archive is None
            else value.get("archive") == archive
        )
        and value.get("ledger_schema_sha256")
        == ALIAS_EXPECTED_LEDGER_SCHEMA_SHA256
        and value.get("ledger_structure_counts")
        == ALIAS_EXPECTED_LEDGER_STRUCTURE_COUNTS
        and _alias_relation_is_valid(value.get("polytao_relation"), owner="polyprop")
        and _alias_relation_is_valid(value.get("ledger_relation"), owner="polyprop")
    )


def _alias_restore_inventory_matches(before: object, restored: object) -> bool:
    if not isinstance(before, dict) or not isinstance(restored, dict):
        return False
    return bool(
        set(restored) == set(before)
        and restored.get("database") == "nexpoly_alias_restore"
        and restored.get("current_user") == "postgres"
        and restored.get("database_owner") == "postgres"
        and restored.get("in_recovery") is False
        and not isinstance(restored.get("server_version_num"), bool)
        and isinstance(restored.get("server_version_num"), int)
        and 160000 <= restored["server_version_num"] < 170000
        and isinstance(restored.get("system_identifier"), str)
        and restored["system_identifier"].isdigit()
        and restored.get("ledger") == before.get("ledger")
        and restored.get("archive") == before.get("archive")
        and restored.get("ledger_schema_sha256")
        == before.get("ledger_schema_sha256")
        and restored.get("ledger_structure_counts")
        == before.get("ledger_structure_counts")
        and _alias_relation_is_valid(restored.get("polytao_relation"), owner="postgres")
        and _alias_relation_is_valid(restored.get("ledger_relation"), owner="postgres")
    )


def _alias_bridge_authority_is_valid(
    runtime_root: Path,
    identity: object,
) -> bool:
    if not isinstance(identity, dict):
        return False
    authority = identity.get("bridge_authority")
    fields = {
        "schema_version",
        "operation_id",
        "descriptor",
        "ready",
        "authority",
        "target",
        "repository_previous",
        "policy",
        "token",
        "takeover",
        "prefetch",
        "external_database_audit_sha256",
        "identity_sha256",
    }
    if not isinstance(authority, dict) or set(authority) != fields:
        return False
    operation_id = authority.get("operation_id")
    if (
        authority.get("schema_version") != 1
        or not isinstance(operation_id, str)
        or OPERATION_ID_RE.fullmatch(operation_id) is None
    ):
        return False
    operation_root = runtime_root / "state" / "prepared" / operation_id
    descriptor_path = operation_root / "descriptor.json"
    ready_path = operation_root / "ready.json"
    try:
        _require_private_directory(runtime_root / "state")
        _require_private_directory(runtime_root / "state" / "prepared")
        _require_private_directory(operation_root)
        descriptor = _load_private_json(descriptor_path)
        ready = _load_private_json(ready_path)
        bridge = descriptor["bridge"]
        takeover = descriptor["legacy_takeover"]
        prefetch = descriptor["prefetch"]
        previous_control = descriptor["controller"][
            "previous_active_control"
        ]
        expected: dict[str, Any] = {
            "schema_version": 1,
            "operation_id": descriptor["operation_id"],
            "descriptor": {
                "path": str(descriptor_path),
                "sha256": sha256_file(descriptor_path),
            },
            "ready": {
                "path": str(ready_path),
                "sha256": sha256_file(ready_path),
            },
            "authority": {
                "sha": bridge["authority"]["sha"],
                "tree": bridge["authority"]["tree"],
                "control_release_id": bridge["authority"][
                    "control_release_id"
                ],
            },
            "target": {
                "sha": bridge["target"]["sha"],
                "tree": bridge["target"]["tree"],
                "control_release_id": bridge["target"][
                    "control_release_id"
                ],
            },
            "repository_previous": {
                "sha": descriptor["repository"]["previous_sha"],
                "tree": descriptor["repository"]["previous_tree"],
            },
            "policy": {
                "id": bridge["policy"]["policy_id"],
                "sha256": bridge["policy_sha256"],
            },
            "token": dict(bridge["token"]),
            "takeover": {
                key: takeover[key]
                for key in (
                    "operation_id",
                    "runtime_identity_sha256",
                    "pre_stopped_fence_sha256",
                    "applied_record_sha256",
                    "binding_sha256",
                )
            },
            "prefetch": {
                key: prefetch[key]
                for key in (
                    "operation_id",
                    "ready_sha256",
                    "identity_sha256",
                    "binding_sha256",
                )
            },
            "external_database_audit_sha256": descriptor[
                "external_database_audit"
            ]["identity_sha256"],
        }
        expected["identity_sha256"] = canonical_json_digest(expected)
    except (ControlRuntimeError, KeyError, OSError, TypeError, ValueError):
        return False
    control = identity.get("control")
    legacy_source = identity.get("legacy_source")
    ready_fields = {
        "schema_version",
        "status",
        "operation_id",
        "source_sha",
        "descriptor_sha256",
        "executor_control",
        "executor_control_sha256",
        "slot_record_sha256",
        "prepared_at",
    }
    return bool(
        authority == expected
        and descriptor.get("schema_version") == 3
        and descriptor.get("operation_id") == operation_id
        and set(ready) == ready_fields
        and ready.get("schema_version") == 1
        and ready.get("status") == "ready"
        and ready.get("operation_id") == operation_id
        and ready.get("source_sha")
        == descriptor["repository"].get("target_sha")
        and ready.get("descriptor_sha256")
        == sha256_file(descriptor_path)
        and isinstance(control, dict)
        and previous_control.get("release_id") == control.get("release_id")
        and previous_control.get("source_sha") == control.get("source_sha")
        and previous_control.get("source_tree") == control.get("source_tree")
        and previous_control.get("manifest_sha256")
        == "sha256:" + str(control.get("manifest_sha256"))
        and expected["authority"]["control_release_id"]
        == control.get("release_id")
        and expected["authority"]["sha"] == control.get("source_sha")
        and expected["authority"]["tree"] == control.get("source_tree")
        and expected["repository_previous"] == legacy_source
        and all(
            isinstance(value, str) and DIGEST_RE.fullmatch(value) is not None
            for value in (
                expected["descriptor"]["sha256"],
                expected["ready"]["sha256"],
                expected["policy"]["id"],
                expected["policy"]["sha256"],
                expected["token"]["token_id"],
                expected["token"]["token_sha256"],
                expected["external_database_audit_sha256"],
                expected["identity_sha256"],
            )
        )
    )


def _alias_runtime_stop_fence_is_valid(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "database_system_identifier",
        "containers",
        "monomer_md_unit",
        "monomer_dft_unit",
    }:
        return False
    containers = value.get("containers")
    if not isinstance(containers, list) or len(containers) not in {3, 4}:
        return False
    by_service = {
        record.get("service"): record
        for record in containers
        if isinstance(record, dict)
        and isinstance(record.get("service"), str)
    }
    postgres = by_service.get("lab-postgres")
    if (
        len(by_service) != len(containers)
        or not {"backend", "nginx", "lab-postgres"}.issubset(by_service)
        or not set(by_service).issubset(
            {"backend", "nginx", "postgres-init", "lab-postgres"}
        )
        or value.get("database_system_identifier") != ALIAS_SYSTEM_IDENTIFIER
        or not isinstance(postgres, dict)
        or postgres.get("running") is not True
        or not isinstance(postgres.get("data_volume"), dict)
        or postgres["data_volume"].get("type") != "volume"
        or postgres["data_volume"].get("read_write") is not True
    ):
        return False
    container_fields = {
        "id",
        "name",
        "service",
        "image",
        "config_image",
        "config_env_sha256",
        "labels_sha256",
        "data_volume",
        "status",
        "running",
        "finished_at",
        "restart_count",
        "restart_policy",
    }
    for service, record in by_service.items():
        if (
            set(record) != container_fields
            or not isinstance(record.get("id"), str)
            or re.fullmatch(r"[0-9a-f]{64}", record["id"]) is None
            or not isinstance(record.get("name"), str)
            or not record["name"]
            or not isinstance(record.get("image"), str)
            or DIGEST_RE.fullmatch(record["image"]) is None
            or not isinstance(record.get("config_image"), str)
            or not record["config_image"]
            or not isinstance(record.get("config_env_sha256"), str)
            or HEX_DIGEST_RE.fullmatch(record["config_env_sha256"]) is None
            or not isinstance(record.get("labels_sha256"), str)
            or HEX_DIGEST_RE.fullmatch(record["labels_sha256"]) is None
            or (
                service != "lab-postgres"
                and record.get("running") is not False
            )
            or (
                service != "lab-postgres"
                and record.get("data_volume") is not None
            )
        ):
            return False
    unit_fields = {
        "LoadState",
        "ActiveState",
        "SubState",
        "MainPID",
        "InvocationID",
        "FragmentPath",
        "DropInPaths",
        "UnitFileState",
        "FragmentSHA256",
    }
    for name, required in (
        ("monomer_md_unit", True),
        ("monomer_dft_unit", False),
    ):
        unit = value.get(name)
        if (
            not isinstance(unit, dict)
            or set(unit) != unit_fields
            or unit.get("MainPID") != "0"
            or unit.get("ActiveState") not in {"inactive", "failed"}
            or unit.get("SubState") not in {"dead", "failed"}
            or (
                required
                and (
                    unit.get("LoadState") != "loaded"
                    or not isinstance(unit.get("FragmentSHA256"), str)
                    or HEX_DIGEST_RE.fullmatch(unit["FragmentSHA256"]) is None
                )
            )
            or (
                not required
                and unit.get("LoadState") not in {"loaded", "not-found"}
            )
            or (
                unit.get("LoadState") == "loaded"
                and (
                    not isinstance(unit.get("FragmentPath"), str)
                    or not unit["FragmentPath"]
                    or not isinstance(unit.get("FragmentSHA256"), str)
                    or HEX_DIGEST_RE.fullmatch(unit["FragmentSHA256"])
                    is None
                )
            )
            or (
                unit.get("LoadState") == "not-found"
                and (
                    unit.get("FragmentPath") != ""
                    or unit.get("FragmentSHA256") is not None
                )
            )
        ):
            return False
    return True


def load_production_0005_alias_gate(
    runtime_root: Path, *, require_completed: bool
) -> dict[str, Any] | None:
    """Validate the durable one-purpose alias repair gate and all evidence."""

    marker_path = runtime_root / ALIAS_MARKER_RELATIVE
    if not (marker_path.exists() or marker_path.is_symlink()):
        bootstrap_path = runtime_root / "state" / BOOTSTRAP_AUTHORITY_NAME
        if bootstrap_path.exists() or bootstrap_path.is_symlink():
            bootstrap = _validate_bootstrap_authority(runtime_root)
            if (
                bootstrap.get("schema_version") == 3
                and bootstrap.get("authority_kind") == ADOPTION_AUTHORITY_KIND
            ):
                adoption = bootstrap["adoption"]
                maintenance = adoption["maintenance"]
                return {
                    "schema_version": 1,
                    "action": "adopted-maintenance-provenance",
                    "phase": "completed",
                    "authority_kind": ADOPTION_AUTHORITY_KIND,
                    "operation_id": adoption["operation_id"],
                    "ledger_sha256": maintenance["ledger_sha256"],
                    "adoption_evidence_sha256": bootstrap[
                        "adoption_evidence_sha256"
                    ],
                }
        if require_completed:
            raise ControlRuntimeError(
                "production 0005 ledger-alias reconciliation is required"
            )
        return None
    marker = _load_private_json(marker_path)
    identity = marker.get("identity")
    operation_id = identity.get("operation_id") if isinstance(identity, dict) else None
    directories = marker.get("operation_directories")
    if (
        marker.get("schema_version") != 1
        or marker.get("action") != ALIAS_ACTION
        or marker.get("phase") not in {
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
        }
        or not isinstance(operation_id, str)
        or OPERATION_ID_RE.fullmatch(operation_id) is None
        or directories
        != {
            "audit": str(runtime_root / ALIAS_AUDIT_ROOT_RELATIVE / operation_id),
            "backup": str(runtime_root / ALIAS_BACKUP_ROOT_RELATIVE / operation_id),
        }
    ):
        raise ControlRuntimeError("production 0005 alias marker is invalid")
    if marker["phase"] != "completed":
        if require_completed:
            raise ControlRuntimeError(
                "production 0005 ledger-alias reconciliation is incomplete"
            )
        return marker
    expected_alias = {
        "version": ALIAS_VERSION,
        "checksum": ALIAS_CHECKSUM,
        "applied_at": ALIAS_APPLIED_AT,
    }
    audit_dir = runtime_root / ALIAS_AUDIT_ROOT_RELATIVE / operation_id
    backup_dir = runtime_root / ALIAS_BACKUP_ROOT_RELATIVE / operation_id
    _require_private_directory(audit_dir)
    _require_private_directory(backup_dir)
    if {entry.name for entry in audit_dir.iterdir()} != ALIAS_AUDIT_NAMES or {
        entry.name for entry in backup_dir.iterdir()
    } != ALIAS_BACKUP_NAMES:
        raise ControlRuntimeError("production 0005 alias evidence inventory differs")
    if identity.get("alias") != expected_alias:
        raise ControlRuntimeError("production 0005 alias identity differs")
    manifest_path = audit_dir / "AUDIT-MANIFEST.json"
    manifest = _load_private_json(manifest_path)
    manifest_sha = sha256_file(manifest_path).removeprefix("sha256:")
    after = _load_private_json(audit_dir / "database-after.json")
    restore = _load_private_json(audit_dir / "isolated-postgres16-restore.json")
    backup = marker.get("database_backup")
    before = marker.get("before")
    mutation_intent = marker.get("mutation_intent")
    runtime_stop_fence = marker.get("runtime_stop_fence")
    external_transition = marker.get(
        "external_database_alias_transition"
    )
    external_transition_path = (
        audit_dir / "external-database-alias-transition.json"
    )
    dump_path = backup_dir / "nexpoly-before.dump"
    sidecar_path = backup_dir / "nexpoly-before.dump.sha256"
    _private_file_identity(dump_path)
    _private_file_identity(sidecar_path)
    try:
        sidecar = sidecar_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise ControlRuntimeError("production 0005 alias dump hash is invalid") from exc
    actual_dump_sha = sha256_file(dump_path).removeprefix("sha256:")
    files = _alias_evidence_files(audit_dir, backup_dir)
    binary_hashes = identity.get("binaries_sha256")
    audit_binaries = manifest.get("binaries")
    restore_image = identity.get("restore_image")
    if (
        not isinstance(backup, dict)
        or not isinstance(before, dict)
        or not isinstance(mutation_intent, dict)
        or not isinstance(runtime_stop_fence, dict)
        or not isinstance(external_transition, dict)
        or set(external_transition)
        != {
            "path",
            "sha256",
            "identity_sha256",
            "before_state_sha256",
            "after_state_sha256",
            "descriptor_sha256",
            "operation_id",
            "kind",
        }
        or external_transition.get("path")
        != str(external_transition_path)
        or external_transition.get("sha256")
        != sha256_file(external_transition_path)
        or external_transition.get("operation_id") != operation_id
        or external_transition.get("kind")
        != "alias-0005-reconciliation"
        or any(
            not isinstance(external_transition.get(name), str)
            or DIGEST_RE.fullmatch(external_transition[name]) is None
            for name in (
                "sha256",
                "identity_sha256",
                "before_state_sha256",
                "after_state_sha256",
                "descriptor_sha256",
            )
        )
        or (
            runtime_root == PRODUCTION_RUNTIME_ROOT
            and not _alias_bridge_authority_is_valid(runtime_root, identity)
        )
        or (
            runtime_root == PRODUCTION_RUNTIME_ROOT
            and not _alias_runtime_stop_fence_is_valid(runtime_stop_fence)
        )
        or identity.get("database_endpoint") != ALIAS_DATABASE_ENDPOINT
        or identity.get("database_system_identifier") != ALIAS_SYSTEM_IDENTIFIER
        or not _alias_live_inventory_is_valid(before, ledger=ALIAS_PRE_LEDGER)
        or not _alias_live_inventory_is_valid(
            after,
            ledger=ALIAS_POST_LEDGER,
            archive=before.get("archive"),
        )
        or any(
            before.get(key) != after.get(key)
            for key in before
            if key != "ledger"
        )
        or after.get("ledger")
        != [
            row
            for row in before.get("ledger", [])
            if isinstance(row, dict) and row.get("version") != ALIAS_VERSION
        ]
        or backup.get("dump_path") != str(dump_path)
        or backup.get("dump_sha256") != actual_dump_sha
        or backup.get("dump_size") != dump_path.stat().st_size
        or backup.get("restore_list_sha256")
        != files["audit/pg-restore.list"]["sha256"]
        or sidecar != actual_dump_sha
        or restore != marker.get("isolated_restore")
        or restore.get("dump_sha256") != actual_dump_sha
        or not _alias_restore_image_is_valid(restore_image)
        or restore.get("image") != restore_image
        or restore.get("archive") != before.get("archive")
        or restore.get("ledger_schema_sha256")
        != before.get("ledger_schema_sha256")
        or not _alias_restore_inventory_matches(
            before, restore.get("database_inventory")
        )
        or mutation_intent
        != {
            "database_system_identifier": identity.get(
                "database_system_identifier"
            ),
            "alias": expected_alias,
            "pre_ledger": before.get("ledger"),
            "archive": before.get("archive"),
            "dump_sha256": actual_dump_sha,
            "restore_dump_sha256": actual_dump_sha,
        }
        or after != marker.get("after")
        or marker.get("audit_manifest_sha256") != manifest_sha
        or manifest.get("schema_version") != 1
        or manifest.get("operation_id") != operation_id
        or manifest.get("outcome") != "completed"
        or manifest.get("identity") != identity
        or manifest.get("database_before") != marker.get("before")
        or manifest.get("database_after") != after
        or manifest.get("database_backup") != backup
        or manifest.get("isolated_restore") != restore
        or manifest.get("runtime_stop_fence") != runtime_stop_fence
        or manifest.get("runtime_stop_fence_sha256")
        != canonical_json_digest(runtime_stop_fence).removeprefix("sha256:")
        or manifest.get("external_database_alias_transition")
        != external_transition
        or manifest.get("files") != files
        or manifest.get("completed_at") != marker.get("completed_at")
        or set(manifest)
        != {
            "schema_version",
            "operation_id",
            "outcome",
            "identity",
            "database_before",
            "database_after",
            "database_backup",
            "isolated_restore",
            "runtime_stop_fence",
            "runtime_stop_fence_sha256",
            "external_database_alias_transition",
            "binaries",
            "files",
            "completed_at",
        }
        or not isinstance(binary_hashes, dict)
        or not isinstance(audit_binaries, dict)
        or {
            path: record.get("sha256")
            for path, record in audit_binaries.items()
            if isinstance(path, str) and isinstance(record, dict)
        }
        != binary_hashes
        or not isinstance(marker.get("completed_at"), str)
        or HEX_DIGEST_RE.fullmatch(str(manifest_sha)) is None
    ):
        raise ControlRuntimeError("production 0005 alias completion evidence differs")
    control = identity.get("control")
    if not isinstance(control, dict):
        raise ControlRuntimeError("production 0005 alias control identity is invalid")
    release_id = control.get("release_id")
    if not isinstance(release_id, str) or RELEASE_ID_RE.fullmatch(release_id) is None:
        raise ControlRuntimeError("production 0005 alias control release is invalid")
    control_manifest, control_root = load_control_release(runtime_root, release_id)
    entrypoint = control_manifest["entrypoints"].get(
        "reconcile-production-0005-alias"
    )
    if (
        control.get("source_sha") != control_manifest["source_sha"]
        or control.get("source_tree") != control_manifest["source_tree"]
        or control.get("manifest_sha256")
        != sha256_file(control_root / CONTROL_MANIFEST_NAME).removeprefix("sha256:")
        or not isinstance(entrypoint, dict)
        or entrypoint.get("kind") != "python"
        or control.get("script_sha256")
        != sha256_file(control_root / str(entrypoint.get("file"))).removeprefix(
            "sha256:"
        )
    ):
        raise ControlRuntimeError("production 0005 alias control evidence differs")
    return marker


def _validate_compatibility(value: object) -> dict[str, Any]:
    fields = {
        "handoff_protocol_versions",
        "descriptor_schema_versions",
        "current_state_schema_versions",
        "marker_schema_versions",
        "worker_slot_schema_versions",
        "prepare_abort_abi_versions",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ControlRuntimeError("control compatibility declaration is invalid")
    result: dict[str, list[int]] = {}
    for name in sorted(fields):
        versions = value[name]
        if (
            not isinstance(versions, list)
            or not versions
            or len(versions) > 16
            or any(
                not isinstance(item, int)
                or isinstance(item, bool)
                or not 1 <= item <= 1024
                for item in versions
            )
            or versions != sorted(set(versions))
        ):
            raise ControlRuntimeError("control compatibility versions are invalid")
        result[name] = list(versions)
    if PROTOCOL_VERSION not in result["handoff_protocol_versions"]:
        raise ControlRuntimeError("control release does not support this handoff protocol")
    for field, required in REQUIRED_COMPATIBILITY.items():
        if required not in result[field]:
            raise ControlRuntimeError(
                f"control release lacks required {field} version {required}"
            )
    return result


def _validate_entrypoints(value: object, file_names: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or not 1 <= len(value) <= 32:
        raise ControlRuntimeError("control entrypoint map is invalid")
    result: dict[str, dict[str, Any]] = {}
    for role, record in value.items():
        if not isinstance(role, str) or SAFE_ROLE_RE.fullmatch(role) is None:
            raise ControlRuntimeError("control role is unsafe")
        if not isinstance(record, dict) or record.get("kind") not in {
            "python",
            "worker",
        }:
            raise ControlRuntimeError("control entrypoint is invalid")
        if record["kind"] == "python":
            if set(record) != {"kind", "file"} or record.get("file") not in file_names:
                raise ControlRuntimeError("Python control entrypoint is invalid")
        else:
            if (
                set(record)
                != {"kind", "environment_loader", "launcher", "config_relative"}
                or record.get("environment_loader") not in file_names
                or record.get("launcher") not in file_names
                or not isinstance(record.get("config_relative"), str)
                or SAFE_CONFIG_RE.fullmatch(record["config_relative"]) is None
            ):
                raise ControlRuntimeError("Worker control entrypoint is invalid")
        result[role] = dict(record)
    if "deploy" not in result or result["deploy"].get("kind") != "python":
        raise ControlRuntimeError("control release lacks the deploy entrypoint")
    return result


def parse_source_manifest(payload: bytes) -> dict[str, Any]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ControlRuntimeError("control source manifest is invalid JSON") from exc
    fields = {
        "schema_version",
        "protocol_version",
        "compatibility",
        "entrypoints",
        "files",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != SOURCE_MANIFEST_SCHEMA_VERSION
        or document.get("protocol_version") != PROTOCOL_VERSION
        or not isinstance(document.get("files"), list)
        or not 1 <= len(document["files"]) <= 64
    ):
        raise ControlRuntimeError("control source manifest has an invalid shape")
    files: list[dict[str, Any]] = []
    names: set[str] = set()
    for record in document["files"]:
        if not isinstance(record, dict) or set(record) != {"name", "source", "mode"}:
            raise ControlRuntimeError("control source file record is invalid")
        name = record.get("name")
        source = record.get("source")
        mode = record.get("mode")
        pure = PurePosixPath(source) if isinstance(source, str) else PurePosixPath(".")
        source_is_safe = isinstance(source, str) and (
            (
                source.startswith("scripts/")
                and pure.name == name
            )
            or CONTROL_DATA_SOURCES.get(source) == name
        )
        if (
            not isinstance(name, str)
            or SAFE_NAME_RE.fullmatch(name) is None
            or name in names
            or not isinstance(source, str)
            or pure.is_absolute()
            or ".." in pure.parts
            or not source_is_safe
            or mode != 0o700
        ):
            raise ControlRuntimeError("control source file record is unsafe")
        names.add(name)
        files.append({"name": name, "source": source, "mode": mode})
    compatibility = _validate_compatibility(document["compatibility"])
    entrypoints = _validate_entrypoints(document["entrypoints"], names)
    return {
        "schema_version": SOURCE_MANIFEST_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "compatibility": compatibility,
        "entrypoints": entrypoints,
        "files": files,
    }


def validate_control_manifest(document: object) -> dict[str, Any]:
    fields = {
        "schema_version",
        "protocol_version",
        "release_id",
        "source_sha",
        "source_tree",
        "compatibility",
        "entrypoints",
        "files",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != CONTROL_MANIFEST_SCHEMA_VERSION
        or document.get("protocol_version") != PROTOCOL_VERSION
        or not isinstance(document.get("release_id"), str)
        or RELEASE_ID_RE.fullmatch(document["release_id"]) is None
        or not isinstance(document.get("source_sha"), str)
        or SHA_RE.fullmatch(document["source_sha"]) is None
        or not isinstance(document.get("source_tree"), str)
        or SHA_RE.fullmatch(document["source_tree"]) is None
        or not isinstance(document.get("files"), dict)
        or not 1 <= len(document["files"]) <= 64
    ):
        raise ControlRuntimeError("control release manifest has an invalid shape")
    files: dict[str, dict[str, Any]] = {}
    for name, record in document["files"].items():
        if (
            not isinstance(name, str)
            or SAFE_NAME_RE.fullmatch(name) is None
            or not isinstance(record, dict)
            or set(record) != {"sha256", "size", "mode"}
            or not isinstance(record.get("sha256"), str)
            or DIGEST_RE.fullmatch(record["sha256"]) is None
            or not isinstance(record.get("size"), int)
            or isinstance(record.get("size"), bool)
            or not 1 <= record["size"] <= 16 * 1024 * 1024
            or record.get("mode") != 0o700
        ):
            raise ControlRuntimeError("control release file identity is invalid")
        files[name] = dict(record)
    compatibility = _validate_compatibility(document["compatibility"])
    entrypoints = _validate_entrypoints(document["entrypoints"], set(files))
    normalized = {
        **document,
        "compatibility": compatibility,
        "entrypoints": entrypoints,
        "files": files,
    }
    identity_payload = {key: value for key, value in normalized.items() if key != "release_id"}
    if release_identity(identity_payload) != normalized["release_id"]:
        raise ControlRuntimeError("control release identity differs from its manifest")
    return normalized


def validate_candidate_record(document: object) -> dict[str, Any]:
    fields = {
        "schema_version",
        "protocol_version",
        "component",
        "release_id",
        "source_sha",
        "source_tree",
        "manifest_sha256",
        "operation_id",
        "prepared_at",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != CONTROL_CANDIDATE_SCHEMA_VERSION
        or document.get("protocol_version") != PROTOCOL_VERSION
        or document.get("component") != "deployment-controls"
        or not isinstance(document.get("release_id"), str)
        or RELEASE_ID_RE.fullmatch(document["release_id"]) is None
        or not isinstance(document.get("source_sha"), str)
        or SHA_RE.fullmatch(document["source_sha"]) is None
        or not isinstance(document.get("source_tree"), str)
        or SHA_RE.fullmatch(document["source_tree"]) is None
        or not isinstance(document.get("manifest_sha256"), str)
        or DIGEST_RE.fullmatch(document["manifest_sha256"]) is None
        or not isinstance(document.get("operation_id"), str)
        or OPERATION_ID_RE.fullmatch(document["operation_id"]) is None
        or not isinstance(document.get("prepared_at"), str)
        or not document["prepared_at"]
    ):
        raise ControlRuntimeError("candidate control record has an invalid shape")
    return dict(document)


def validate_active_control_record(document: object) -> dict[str, Any]:
    fields = {
        "schema_version",
        "protocol_version",
        "component",
        "generation",
        "release_id",
        "source_sha",
        "source_tree",
        "manifest_sha256",
        "operation_id",
        "previous_release_id",
        "activated_at",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != ACTIVE_CONTROL_SCHEMA_VERSION
        or document.get("protocol_version") != PROTOCOL_VERSION
        or document.get("component") != "deployment-controls"
        or not isinstance(document.get("generation"), int)
        or isinstance(document.get("generation"), bool)
        or document["generation"] < 1
        or not isinstance(document.get("release_id"), str)
        or RELEASE_ID_RE.fullmatch(document["release_id"]) is None
        or not isinstance(document.get("source_sha"), str)
        or SHA_RE.fullmatch(document["source_sha"]) is None
        or not isinstance(document.get("source_tree"), str)
        or SHA_RE.fullmatch(document["source_tree"]) is None
        or not isinstance(document.get("manifest_sha256"), str)
        or DIGEST_RE.fullmatch(document["manifest_sha256"]) is None
        or not isinstance(document.get("operation_id"), str)
        or OPERATION_ID_RE.fullmatch(document["operation_id"]) is None
        or (
            document.get("previous_release_id") is not None
            and (
                not isinstance(document["previous_release_id"], str)
                or RELEASE_ID_RE.fullmatch(document["previous_release_id"]) is None
            )
        )
        or not isinstance(document.get("activated_at"), str)
        or not document["activated_at"]
    ):
        raise ControlRuntimeError("active control record has an invalid shape")
    return dict(document)


def control_release_root(runtime_root: Path, release_id: str) -> Path:
    if RELEASE_ID_RE.fullmatch(release_id) is None:
        raise ControlRuntimeError("control release identity is invalid")
    return runtime_root / "control-releases" / release_id


def active_control_record_path(runtime_root: Path) -> Path:
    return runtime_root / "state/active-control.json"


def _adoption_file_record_is_valid(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == {"path", "sha256", "size", "mode"}
        and isinstance(value.get("path"), str)
        and Path(value["path"]).is_absolute()
        and isinstance(value.get("sha256"), str)
        and DIGEST_RE.fullmatch(value["sha256"]) is not None
        and isinstance(value.get("size"), int)
        and not isinstance(value.get("size"), bool)
        and value["size"] > 0
        and isinstance(value.get("mode"), str)
        and re.fullmatch(r"0[0-7]{3}", value["mode"]) is not None
    )


def _validate_adoption_evidence(
    value: object,
    *,
    operation_id: str,
    bootstrap_source_sha: str,
    bootstrap_source_tree: str,
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "authority_kind",
        "operation_id",
        "bootstrap_source",
        "live_repository",
        "production_config",
        "images",
        "database",
        "asset_identity",
        "migrations",
        "maintenance",
        "monomer_md",
        "monomer_dft",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("schema_version") != 1
        or value.get("authority_kind") != ADOPTION_AUTHORITY_KIND
        or value.get("operation_id") != operation_id
        or value.get("bootstrap_source")
        != {"sha": bootstrap_source_sha, "tree": bootstrap_source_tree}
    ):
        raise ControlRuntimeError("manual runtime adoption evidence is invalid")
    repository = value.get("live_repository")
    if (
        not isinstance(repository, dict)
        or set(repository)
        != {
            "branch",
            "origin",
            "head",
            "tree",
            "target",
            "fast_forward",
            "ignored_entries",
        }
        or repository.get("branch") != "main"
        or repository.get("origin") != "git@github.com:lzq390/ZhijuPoly.git"
        or SHA_RE.fullmatch(str(repository.get("head", ""))) is None
        or SHA_RE.fullmatch(str(repository.get("tree", ""))) is None
        or repository.get("target") != bootstrap_source_sha
        or repository.get("fast_forward") is not True
        or repository.get("ignored_entries") != 0
    ):
        raise ControlRuntimeError("adopted production repository evidence is invalid")
    production_config = value.get("production_config")
    if (
        not isinstance(production_config, dict)
        or set(production_config) != {"deploy_env", "app_env"}
        or not all(
            _adoption_file_record_is_valid(production_config.get(name))
            for name in ("deploy_env", "app_env")
        )
    ):
        raise ControlRuntimeError("adopted production configuration is invalid")
    migrations = value.get("migrations")
    if not isinstance(migrations, list) or len(migrations) != 13:
        raise ControlRuntimeError("adopted migration evidence is invalid")
    ledger: list[tuple[str, str]] = []
    for record in migrations:
        if (
            not isinstance(record, dict)
            or set(record)
            != {"version", "kind", "epoch", "checksum", "requires_contracts"}
            or not isinstance(record.get("version"), str)
            or not isinstance(record.get("checksum"), str)
            or HEX_DIGEST_RE.fullmatch(record["checksum"]) is None
            or not isinstance(record.get("kind"), str)
            or not isinstance(record.get("epoch"), int)
            or isinstance(record.get("epoch"), bool)
            or not isinstance(record.get("requires_contracts"), list)
        ):
            raise ControlRuntimeError("adopted migration record is invalid")
        ledger.append((record["version"], record["checksum"]))
    if ledger != ADOPTED_POST_0013_LEDGER:
        raise ControlRuntimeError("adopted migration ledger is not exact post-0013")
    database = value.get("database")
    database_ledger = database.get("ledger") if isinstance(database, dict) else None
    if (
        not isinstance(database, dict)
        or database.get("postgres_major") != 16
        or not isinstance(database.get("system_identifier"), str)
        or not database["system_identifier"].isdigit()
        or not isinstance(database.get("runtime"), dict)
        or database_ledger
        != [
            {"version": version, "checksum": checksum}
            for version, checksum in ADOPTED_POST_0013_LEDGER
        ]
    ):
        raise ControlRuntimeError("adopted PostgreSQL evidence is invalid")
    maintenance = value.get("maintenance")
    if (
        not isinstance(maintenance, dict)
        or set(maintenance)
        != {
            "kind",
            "alias_0005",
            "contract_0012",
            "ledger_endpoint",
            "ledger_sha256",
            "manual_evidence",
        }
        or maintenance.get("kind") != "adopted-maintenance-provenance"
        or maintenance.get("alias_0005") != "completed-without-formal-marker"
        or maintenance.get("contract_0012") != "completed-without-formal-marker"
        or maintenance.get("ledger_endpoint") != "post-0013"
        or maintenance.get("ledger_sha256")
        != canonical_json_digest(database_ledger)
        or not isinstance(maintenance.get("manual_evidence"), dict)
        or set(maintenance["manual_evidence"])
        != {
            "manual_deployment_report",
            "post_0013_database",
            "isolated_restore",
            "formal_dft_release",
        }
        or not all(
            _adoption_file_record_is_valid(record)
            for record in maintenance["manual_evidence"].values()
        )
    ):
        raise ControlRuntimeError("adopted maintenance provenance is invalid")
    asset = value.get("asset_identity")
    if (
        not isinstance(asset, dict)
        or set(asset) != {"pointer", "root", "manifest_sha256"}
        or not isinstance(asset.get("pointer"), str)
        or not Path(asset["pointer"]).is_absolute()
        or not isinstance(asset.get("root"), str)
        or not Path(asset["root"]).is_absolute()
        or not isinstance(asset.get("manifest_sha256"), str)
        or DIGEST_RE.fullmatch(asset["manifest_sha256"]) is None
    ):
        raise ControlRuntimeError("adopted asset identity is invalid")
    monomer_md = value.get("monomer_md")
    md_unit = monomer_md.get("systemd_unit") if isinstance(monomer_md, dict) else None
    if (
        not isinstance(monomer_md, dict)
        or not isinstance(monomer_md.get("active_slot"), dict)
        or monomer_md["active_slot"].get("source_sha") != repository["head"]
        or monomer_md["active_slot"].get("source_tree") != repository["tree"]
        or not isinstance(monomer_md.get("slot_record"), dict)
        or monomer_md["slot_record"].get("source_sha") != repository["head"]
        or monomer_md["slot_record"].get("source_tree") != repository["tree"]
        or not isinstance(md_unit, dict)
        or md_unit.get("control_release_id") is not None
        or not isinstance(md_unit.get("target_path"), str)
        or not Path(md_unit["target_path"]).is_absolute()
        or not isinstance(md_unit.get("sha256"), str)
        or DIGEST_RE.fullmatch(md_unit["sha256"]) is None
        or not isinstance(md_unit.get("launcher_path"), str)
        or not Path(md_unit["launcher_path"]).is_absolute()
        or not isinstance(md_unit.get("launcher_sha256"), str)
        or DIGEST_RE.fullmatch(md_unit["launcher_sha256"]) is None
    ):
        raise ControlRuntimeError("adopted monomer MD identity is invalid")
    monomer_dft = value.get("monomer_dft")
    runtime = monomer_dft.get("runtime") if isinstance(monomer_dft, dict) else None
    runtime_env = (
        monomer_dft.get("runtime_env") if isinstance(monomer_dft, dict) else None
    )
    dft_unit = (
        monomer_dft.get("systemd_unit") if isinstance(monomer_dft, dict) else None
    )
    gpu = monomer_dft.get("gpu") if isinstance(monomer_dft, dict) else None
    health = monomer_dft.get("health") if isinstance(monomer_dft, dict) else None
    model_names = {
        "aimnet2-pd_0.pt",
        "aimnet2_2025_b973c_d3_0.pt",
        "aimnet2_b973c_d3_0.pt",
        "aimnet2_rxn_0.pt",
        "aimnet2_wb97m_d3_0.pt",
        "aimnet2nse_wb97m_0.pt",
    }
    if (
        not isinstance(runtime, dict)
        or runtime.get("release_sha") != repository["head"]
        or runtime.get("source_tree") != repository["tree"]
        or not isinstance(runtime.get("models"), dict)
        or set(runtime["models"]) != model_names
        or any(
            not isinstance(checksum, str) or DIGEST_RE.fullmatch(checksum) is None
            for checksum in runtime["models"].values()
        )
        or any(
            not isinstance(runtime.get(name), str)
            or DIGEST_RE.fullmatch(runtime[name]) is None
            for name in (
                "runtime_manifest_sha256",
                "requirements_lock_sha256",
                "aimnet_source_lock_sha256",
                "runtime_inventory_sha256",
            )
        )
        or not isinstance(runtime_env, dict)
        or not isinstance(runtime_env.get("sha256"), str)
        or DIGEST_RE.fullmatch(runtime_env["sha256"]) is None
        or not isinstance(dft_unit, dict)
        or dft_unit.get("control_release_id") is not None
        or not isinstance(dft_unit.get("sha256"), str)
        or DIGEST_RE.fullmatch(dft_unit["sha256"]) is None
        or not isinstance(dft_unit.get("launcher_path"), str)
        or not Path(dft_unit["launcher_path"]).is_absolute()
        or not isinstance(dft_unit.get("launcher_sha256"), str)
        or DIGEST_RE.fullmatch(dft_unit["launcher_sha256"]) is None
        or not isinstance(gpu, dict)
        or gpu.get("index") != "2"
        or gpu.get("guard_mode") != "enforce"
        or gpu.get("guard_status") not in {"ready", "quarantined"}
        or gpu.get("contention_observed")
        != (gpu.get("guard_status") == "quarantined")
        or gpu.get("uuid") != ADOPTED_DFT_GPU_UUID
        or not isinstance(health, dict)
        or health.get("release_sha") != repository["head"]
        or health.get("active_jobs") != 0
        or health.get("queued_jobs") != 0
        or health.get("gpu_guard_status") != gpu.get("guard_status")
        or health.get("gpu_contention_observed")
        != gpu.get("contention_observed")
        or (
            gpu.get("guard_status") == "ready"
            and (
                health.get("status") != "ok"
                or health.get("runtime_ready") is not True
                or health.get("degradation_reason") is not None
            )
        )
        or (
            gpu.get("guard_status") == "quarantined"
            and (
                health.get("status") != "degraded"
                or health.get("runtime_ready") is not False
                or health.get("degradation_reason") != "gpu-guard-quarantined"
            )
        )
    ):
        raise ControlRuntimeError("adopted monomer DFT identity is invalid")
    return dict(value)


def _validate_adopted_deployment(
    value: object,
    *,
    adoption: Mapping[str, Any],
    adoption_sha256: str,
    active: Mapping[str, Any],
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "status",
        "authority_kind",
        "operation_id",
        "source_sha",
        "source_tree",
        "bootstrap_source_sha",
        "bootstrap_source_tree",
        "active_control",
        "adoption_evidence",
        "adoption_evidence_sha256",
        "images",
        "production_config",
        "asset_identity",
        "migrations",
        "database",
        "maintenance",
        "monomer_md",
        "monomer_dft",
        "adopted_at",
    }
    repository = adoption["live_repository"]
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("schema_version") != 1
        or value.get("status") != "adopted"
        or value.get("authority_kind") != ADOPTION_AUTHORITY_KIND
        or value.get("operation_id") != adoption["operation_id"]
        or value.get("source_sha") != repository["head"]
        or value.get("source_tree") != repository["tree"]
        or value.get("bootstrap_source_sha") != active["source_sha"]
        or value.get("bootstrap_source_tree") != active["source_tree"]
        or value.get("active_control") != active
        or value.get("adoption_evidence") != adoption
        or value.get("adoption_evidence_sha256") != adoption_sha256
        or any(
            value.get(name) != adoption.get(name)
            for name in (
                "images",
                "production_config",
                "asset_identity",
                "migrations",
                "database",
                "maintenance",
                "monomer_md",
                "monomer_dft",
            )
        )
        or not isinstance(value.get("adopted_at"), str)
        or not value["adopted_at"]
    ):
        raise ControlRuntimeError("adopted deployment authority is invalid")
    return dict(value)


def _validate_bootstrap_router_file(
    runtime_root: Path,
    operation_id: str,
    name: str,
    value: object,
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"relative_path", "sha256", "size", "mode"}
        or value.get("relative_path")
        != f"{BOOTSTRAP_ROUTER_ROOT_NAME}/{operation_id}/{name}"
        or not isinstance(value.get("sha256"), str)
        or DIGEST_RE.fullmatch(value["sha256"]) is None
        or type(value.get("size")) is not int
        or not 1 <= value["size"] <= 16 * 1024 * 1024
        or value.get("mode") != 0o700
    ):
        raise ControlRuntimeError("bootstrap-router file authority is invalid")
    path = runtime_root / value["relative_path"]
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ControlRuntimeError("bootstrap-router file is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != value["mode"]
        or metadata.st_size != value["size"]
        or sha256_file(path) != value["sha256"]
    ):
        raise ControlRuntimeError("bootstrap-router file differs from authority")
    return dict(value)


def _validate_bootstrap_router_intent(
    runtime_root: Path,
    document: object,
    *,
    bootstrap_digest: str,
    predecessor_selector_digest: str,
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "status",
        "authority_kind",
        "policy",
        "operation_id",
        "target_source_sha",
        "target_source_tree",
        "bootstrap_control_sha256",
        "predecessor_selector_sha256",
        "successor_selector_sha256",
        "snapshot_authority_sha256",
        "source_successor_authority_sha256",
        "unit_permission_authority_sha256",
        "target_control_release",
        "router_files",
        "delivery_gate_sha256",
        "plan_sha256",
        "created_at",
    }
    operation_id = document.get("operation_id") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != 1
        or document.get("status") != "selector-swap-intent"
        or document.get("authority_kind") != BOOTSTRAP_ROUTER_AUTHORITY_KIND
        or document.get("policy") != BOOTSTRAP_ROUTER_POLICY
        or not isinstance(operation_id, str)
        or re.fullmatch(r"adopt-router-[a-z0-9][a-z0-9._-]{7,95}", operation_id)
        is None
        or not isinstance(document.get("target_source_sha"), str)
        or SHA_RE.fullmatch(document["target_source_sha"]) is None
        or not isinstance(document.get("target_source_tree"), str)
        or SHA_RE.fullmatch(document["target_source_tree"]) is None
        or document.get("bootstrap_control_sha256") != bootstrap_digest
        or document.get("predecessor_selector_sha256")
        != predecessor_selector_digest
    ):
        raise ControlRuntimeError("bootstrap-router successor intent is invalid")
    for field in (
        "successor_selector_sha256",
        "snapshot_authority_sha256",
        "source_successor_authority_sha256",
        "unit_permission_authority_sha256",
        "delivery_gate_sha256",
        "plan_sha256",
    ):
        value = document.get(field)
        if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
            raise ControlRuntimeError("bootstrap-router successor digest is invalid")
    created_at = document.get("created_at")
    if (
        not isinstance(created_at, str)
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:"
            r"[0-9]{2}:[0-9]{2}\.[0-9]{6}Z",
            created_at,
        )
        is None
    ):
        raise ControlRuntimeError("bootstrap-router successor timestamp is invalid")
    target_control = document.get("target_control_release")
    if (
        not isinstance(target_control, dict)
        or set(target_control)
        != {
            "release_id",
            "source_sha",
            "source_tree",
            "manifest_sha256",
            "deploy_sha256",
        }
        or not isinstance(target_control.get("release_id"), str)
        or RELEASE_ID_RE.fullmatch(target_control["release_id"]) is None
        or target_control.get("source_sha") != document["target_source_sha"]
        or target_control.get("source_tree") != document["target_source_tree"]
        or any(
            not isinstance(target_control.get(field), str)
            or DIGEST_RE.fullmatch(target_control[field]) is None
            for field in ("manifest_sha256", "deploy_sha256")
        )
    ):
        raise ControlRuntimeError("bootstrap-router target control is invalid")
    router_files = document.get("router_files")
    expected_names = {
        "production_git_snapshot.py",
        "restore_production_git_snapshot.py",
        "reviewed-selector.py",
        "predecessor-selector.py",
    }
    if not isinstance(router_files, dict) or set(router_files) != expected_names:
        raise ControlRuntimeError("bootstrap-router file inventory is invalid")
    normalized_files = {
        name: _validate_bootstrap_router_file(
            runtime_root,
            operation_id,
            name,
            router_files[name],
        )
        for name in sorted(expected_names)
    }
    if (
        normalized_files["reviewed-selector.py"]["sha256"]
        != document["successor_selector_sha256"]
        or normalized_files["predecessor-selector.py"]["sha256"]
        != document["predecessor_selector_sha256"]
    ):
        raise ControlRuntimeError("bootstrap-router selector copies differ")
    authority_bindings = (
        (
            PRODUCTION_GIT_SNAPSHOT_AUTHORITY_NAME,
            "snapshot_authority_sha256",
            "manual-runtime-adoption-production-git-snapshot",
        ),
        (
            SOURCE_SUCCESSOR_AUTHORITY_NAME,
            "source_successor_authority_sha256",
            "manual-runtime-adoption-git-permission-source-successor",
        ),
        (
            UNIT_PERMISSION_AUTHORITY_NAME,
            "unit_permission_authority_sha256",
            "manual-runtime-adoption-unit-permission-hardening",
        ),
    )
    for name, digest_field, authority_kind in authority_bindings:
        authority, digest = _load_private_canonical_json(runtime_root / "state" / name)
        if (
            digest != document[digest_field]
            or authority.get("status") != "completed"
            or authority.get("authority_kind") != authority_kind
        ):
            raise ControlRuntimeError(
                "bootstrap-router predecessor authority differs"
            )
        if name == PRODUCTION_GIT_SNAPSHOT_AUTHORITY_NAME:
            if (
                authority.get("schema_version") != 1
                or authority.get("target_source_sha")
                != document["target_source_sha"]
                or authority.get("target_source_tree")
                != document["target_source_tree"]
            ):
                raise ControlRuntimeError(
                    "bootstrap-router snapshot target differs"
                )
        if name == SOURCE_SUCCESSOR_AUTHORITY_NAME:
            if (
                authority.get("schema_version") != 2
                or authority.get("source_sha")
                != document["target_source_sha"]
                or authority.get("source_tree")
                != document["target_source_tree"]
                or authority.get("snapshot_authority_sha256")
                != document["snapshot_authority_sha256"]
                or authority.get("bootstrap_control_sha256")
                != document["bootstrap_control_sha256"]
            ):
                raise ControlRuntimeError(
                    "bootstrap-router source target differs"
                )
        if name == UNIT_PERMISSION_AUTHORITY_NAME:
            unit_plan = authority.get("plan")
            unit_successor = (
                unit_plan.get("git_permission_successor")
                if isinstance(unit_plan, dict)
                else None
            )
            source_binding = (
                unit_successor.get("source_successor_authority")
                if isinstance(unit_successor, dict)
                else None
            )
            if (
                authority.get("schema_version") != 2
                or authority.get("source_sha")
                != document["target_source_sha"]
                or authority.get("source_tree")
                != document["target_source_tree"]
                or authority.get("bootstrap_control_sha256")
                != document["bootstrap_control_sha256"]
                or authority.get(
                    "adopted_git_permission_source_successor_sha256"
                )
                != document["source_successor_authority_sha256"]
                or not isinstance(source_binding, dict)
                or source_binding.get("snapshot_authority_sha256")
                != document["snapshot_authority_sha256"]
                or source_binding.get("authority_file_sha256")
                != document["source_successor_authority_sha256"]
            ):
                raise ControlRuntimeError(
                    "bootstrap-router unit target differs"
                )
    manifest, release_root = load_control_release(
        runtime_root,
        target_control["release_id"],
    )
    deploy = manifest["entrypoints"].get("deploy")
    if (
        manifest["source_sha"] != document["target_source_sha"]
        or manifest["source_tree"] != document["target_source_tree"]
        or sha256_file(release_root / CONTROL_MANIFEST_NAME)
        != target_control["manifest_sha256"]
        or not isinstance(deploy, dict)
        or deploy.get("kind") != "python"
        or sha256_file(release_root / str(deploy.get("file")))
        != target_control["deploy_sha256"]
    ):
        raise ControlRuntimeError("bootstrap-router target release differs")
    normalized = dict(document)
    normalized["target_control_release"] = dict(target_control)
    normalized["router_files"] = normalized_files
    return normalized


def _validate_bootstrap_router_authority(
    document: object,
    *,
    intent: Mapping[str, Any],
    intent_digest: str,
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "status",
        "authority_kind",
        "policy",
        "operation_id",
        "intent_sha256",
        "intent",
        "completed_at",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != 1
        or document.get("status") != "completed"
        or document.get("authority_kind") != BOOTSTRAP_ROUTER_AUTHORITY_KIND
        or document.get("policy") != BOOTSTRAP_ROUTER_POLICY
        or document.get("operation_id") != intent["operation_id"]
        or document.get("intent_sha256") != intent_digest
        or document.get("intent") != intent
        or not isinstance(document.get("completed_at"), str)
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:"
            r"[0-9]{2}:[0-9]{2}\.[0-9]{6}Z",
            document["completed_at"],
        )
        is None
        or document["completed_at"] < intent["created_at"]
    ):
        raise ControlRuntimeError("bootstrap-router successor authority is invalid")
    return dict(document)


def _bootstrap_router_transition(
    runtime_root: Path,
    *,
    bootstrap_digest: str,
    predecessor_selector_digest: str,
    observed_selector_digest: str,
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    intent_path = runtime_root / "state" / BOOTSTRAP_ROUTER_INTENT_NAME
    authority_path = runtime_root / "state" / BOOTSTRAP_ROUTER_AUTHORITY_NAME
    intent_present = intent_path.exists() or intent_path.is_symlink()
    authority_present = authority_path.exists() or authority_path.is_symlink()
    if not intent_present:
        if authority_present:
            raise ControlRuntimeError(
                "bootstrap-router authority exists without swap intent"
            )
        if observed_selector_digest != predecessor_selector_digest:
            raise ControlRuntimeError(
                "bootstrap selector changed without successor intent"
            )
        return None
    raw_intent, intent_digest = _load_private_canonical_json(intent_path)
    intent = _validate_bootstrap_router_intent(
        runtime_root,
        raw_intent,
        bootstrap_digest=bootstrap_digest,
        predecessor_selector_digest=predecessor_selector_digest,
    )
    if observed_selector_digest not in {
        predecessor_selector_digest,
        intent["successor_selector_sha256"],
    }:
        raise ControlRuntimeError("bootstrap selector is outside its successor CAS")
    authority: dict[str, Any] | None = None
    if authority_present:
        raw_authority, _authority_digest = _load_private_canonical_json(
            authority_path
        )
        authority = _validate_bootstrap_router_authority(
            raw_authority,
            intent=intent,
            intent_digest=intent_digest,
        )
        if observed_selector_digest != intent["successor_selector_sha256"]:
            raise ControlRuntimeError(
                "completed bootstrap-router authority lacks its successor selector"
            )
    return intent, authority


def _validate_bootstrap_bin(
    runtime_root: Path,
    immutable: Mapping[str, Any],
    *,
    bootstrap_digest: str,
) -> None:
    bin_root = runtime_root / "bin"
    _require_private_directory(bin_root)
    if {entry.name for entry in bin_root.iterdir()} != BOOTSTRAP_IMMUTABLE_FILES:
        raise ControlRuntimeError("immutable bootstrap router inventory differs")
    for name, expected_digest in immutable.items():
        path = bin_root / name
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ControlRuntimeError(
                f"immutable bootstrap router is unavailable: {name}"
            ) from exc
        observed_digest = sha256_file(path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or (
                name != "control_runtime_selector.py"
                and observed_digest != expected_digest
            )
        ):
            raise ControlRuntimeError(f"immutable bootstrap router differs: {name}")
        if name == "control_runtime_selector.py":
            _bootstrap_router_transition(
                runtime_root,
                bootstrap_digest=bootstrap_digest,
                predecessor_selector_digest=expected_digest,
                observed_selector_digest=observed_digest,
            )


def _validate_adoption_bootstrap_authority(
    runtime_root: Path,
    record: dict[str, Any],
    *,
    bootstrap_digest: str,
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "status",
        "authority_kind",
        "operation_id",
        "source_sha",
        "source_tree",
        "source_readiness",
        "source_readiness_sha256",
        "delivery_gate",
        "production_repository",
        "adoption",
        "adoption_evidence_sha256",
        "adopted_deployment",
        "adopted_deployment_sha256",
        "immutable_files",
        "candidate_control",
        "active_control",
    }
    operation_id = record.get("operation_id")
    if (
        set(record) != fields
        or record.get("schema_version") != 3
        or record.get("status") != "completed"
        or record.get("authority_kind") != ADOPTION_AUTHORITY_KIND
        or not isinstance(operation_id, str)
        or OPERATION_ID_RE.fullmatch(operation_id) is None
        or SHA_RE.fullmatch(str(record.get("source_sha", ""))) is None
        or SHA_RE.fullmatch(str(record.get("source_tree", ""))) is None
    ):
        raise ControlRuntimeError("completed adoption bootstrap authority is invalid")
    readiness = record.get("source_readiness")
    if (
        not isinstance(readiness, dict)
        or readiness.get("ready") is not True
        or readiness.get("source_sha") != record["source_sha"]
        or readiness.get("source_tree") != record["source_tree"]
        or readiness.get("branch") != "main"
        or readiness.get("origin") != "git@github.com:lzq390/ZhijuPoly.git"
        or readiness.get("origin_main_sha") != record["source_sha"]
        or readiness.get("dirty_entries") != 0
        or readiness.get("ignored_entries") != 0
        or record.get("source_readiness_sha256")
        != canonical_json_digest(readiness)
    ):
        raise ControlRuntimeError("adoption bootstrap source readiness is invalid")
    candidate = validate_candidate_record(record.get("candidate_control"))
    active = validate_active_control_record(record.get("active_control"))
    if (
        any(
            candidate.get(name) != active.get(name)
            for name in (
                "protocol_version",
                "release_id",
                "source_sha",
                "source_tree",
                "manifest_sha256",
                "operation_id",
            )
        )
        or active["source_sha"] != record["source_sha"]
        or active["source_tree"] != record["source_tree"]
    ):
        raise ControlRuntimeError("adoption initial control authority is invalid")
    adoption = _validate_adoption_evidence(
        record.get("adoption"),
        operation_id=operation_id,
        bootstrap_source_sha=record["source_sha"],
        bootstrap_source_tree=record["source_tree"],
    )
    adoption_sha256 = canonical_json_digest(adoption)
    if (
        record.get("adoption_evidence_sha256") != adoption_sha256
        or record.get("production_repository") != adoption["live_repository"]
    ):
        raise ControlRuntimeError("adoption evidence binding differs")
    adopted = _validate_adopted_deployment(
        record.get("adopted_deployment"),
        adoption=adoption,
        adoption_sha256=adoption_sha256,
        active=active,
    )
    if record.get("adopted_deployment_sha256") != canonical_json_digest(adopted):
        raise ControlRuntimeError("adopted deployment digest differs")
    adopted_path = runtime_root / "state" / ADOPTED_DEPLOYMENT_NAME
    if _load_private_json(adopted_path) != adopted:
        raise ControlRuntimeError("durable adopted deployment authority differs")
    immutable = record.get("immutable_files")
    if (
        not isinstance(immutable, dict)
        or set(immutable) != BOOTSTRAP_IMMUTABLE_FILES
        or any(
            not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None
            for value in immutable.values()
        )
    ):
        raise ControlRuntimeError("adoption immutable controls are invalid")
    _validate_bootstrap_bin(
        runtime_root,
        immutable,
        bootstrap_digest=bootstrap_digest,
    )
    return record


def _validate_bootstrap_authority(runtime_root: Path) -> dict[str, Any]:
    authority_path = runtime_root / "state" / BOOTSTRAP_AUTHORITY_NAME
    record, bootstrap_digest = _load_private_canonical_json(authority_path)
    if record.get("schema_version") == 3:
        return _validate_adoption_bootstrap_authority(
            runtime_root,
            record,
            bootstrap_digest=bootstrap_digest,
        )
    expected_fields = {
        "schema_version",
        "status",
        "source_sha",
        "source_tree",
        "source_readiness",
        "source_readiness_sha256",
        "legacy_takeover",
        "delivery_gate",
        "production_repository",
        "immutable_files",
        "worker_unit_takeover",
        "candidate_control",
        "active_control",
    }
    immutable = record.get("immutable_files")
    readiness = record.get("source_readiness")
    candidate = validate_candidate_record(record.get("candidate_control"))
    initial_active = validate_active_control_record(record.get("active_control"))
    if (
        set(record) != expected_fields
        or record.get("schema_version") != 2
        or record.get("status") != "completed"
        or SHA_RE.fullmatch(str(record.get("source_sha", ""))) is None
        or SHA_RE.fullmatch(str(record.get("source_tree", ""))) is None
        or not isinstance(readiness, dict)
        or set(readiness)
        != {
            "schema_version",
            "ready",
            "source_root",
            "source_sha",
            "source_tree",
            "branch",
            "origin",
            "remote_names",
            "origin_fetch_urls",
            "origin_push_urls",
            "origin_main_sha",
            "standalone_object_database",
            "shallow",
            "dirty_entries",
            "ignored_entries",
            "unreachable_objects",
            "replace_refs",
            "special_index_entries",
            "sparse_index",
            "owner_private",
            "group_or_world_writable",
        }
        or readiness.get("schema_version") != 2
        or readiness.get("ready") is not True
        or not isinstance(readiness.get("source_root"), str)
        or not Path(readiness["source_root"]).is_absolute()
        or readiness.get("source_sha") != record.get("source_sha")
        or readiness.get("source_tree") != record.get("source_tree")
        or readiness.get("branch") != "main"
        or readiness.get("origin") != "git@github.com:lzq390/ZhijuPoly.git"
        or readiness.get("remote_names") != ["origin"]
        or readiness.get("origin_fetch_urls")
        != ["git@github.com:lzq390/ZhijuPoly.git"]
        or readiness.get("origin_push_urls")
        != ["git@github.com:lzq390/ZhijuPoly.git"]
        or readiness.get("origin_main_sha") != record.get("source_sha")
        or readiness.get("standalone_object_database") is not True
        or readiness.get("shallow") is not False
        or readiness.get("dirty_entries") != 0
        or readiness.get("ignored_entries") != 0
        or readiness.get("unreachable_objects") != 0
        or readiness.get("replace_refs") != 0
        or readiness.get("special_index_entries") != 0
        or readiness.get("sparse_index") is not False
        or readiness.get("owner_private") is not True
        or readiness.get("group_or_world_writable") is not False
        or record.get("source_readiness_sha256")
        != canonical_json_digest(readiness)
        or not isinstance(record.get("delivery_gate"), dict)
        or not isinstance(record.get("production_repository"), dict)
        or not isinstance(record.get("worker_unit_takeover"), dict)
        or not isinstance(immutable, dict)
        or set(immutable) != BOOTSTRAP_IMMUTABLE_FILES
        or any(
            not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None
            for value in immutable.values()
        )
        or any(
            candidate.get(field) != initial_active.get(field)
            for field in (
                "protocol_version",
                "release_id",
                "source_sha",
                "source_tree",
                "manifest_sha256",
                "operation_id",
            )
        )
        or record["source_sha"] != candidate["source_sha"]
        or record["source_tree"] != candidate["source_tree"]
    ):
        raise ControlRuntimeError("completed bootstrap authority is invalid")
    takeover = record.get("legacy_takeover")
    takeover_fields = {
        "schema_version",
        "operation_id",
        "authority_sha",
        "authority_tree",
        "install_manifest_sha256",
        "classification_sha256",
        "runtime_identity_sha256",
        "git_identity",
        "pre_stopped_fence_sha256",
        "control_layout_sha256",
        "checkout_permissions_sha256",
        "applied_record_sha256",
        "binding_sha256",
    }
    if (
        not isinstance(takeover, dict)
        or set(takeover) != takeover_fields
        or takeover.get("schema_version") != 1
        or not isinstance(takeover.get("operation_id"), str)
        or OPERATION_ID_RE.fullmatch(takeover["operation_id"]) is None
        or takeover.get("authority_sha") != record["source_sha"]
        or takeover.get("authority_tree") != record["source_tree"]
        or any(
            not isinstance(takeover.get(name), str)
            or DIGEST_RE.fullmatch(takeover[name]) is None
            for name in (
                "install_manifest_sha256",
                "classification_sha256",
                "runtime_identity_sha256",
                "pre_stopped_fence_sha256",
                "control_layout_sha256",
                "checkout_permissions_sha256",
                "applied_record_sha256",
                "binding_sha256",
            )
        )
        or not isinstance(takeover.get("git_identity"), dict)
        or set(takeover["git_identity"])
        != {"branch", "head_sha", "head_tree", "local_main_sha"}
        or takeover["git_identity"].get("branch") != "refs/heads/main"
        or takeover["git_identity"].get("head_sha")
        != takeover["git_identity"].get("local_main_sha")
        or any(
            not isinstance(takeover["git_identity"].get(name), str)
            or SHA_RE.fullmatch(takeover["git_identity"][name]) is None
            for name in ("head_sha", "head_tree", "local_main_sha")
        )
        or takeover["binding_sha256"]
        != canonical_json_digest(
            {
                key: value
                for key, value in takeover.items()
                if key != "binding_sha256"
            }
        )
    ):
        raise ControlRuntimeError(
            "completed bootstrap legacy takeover authority is invalid"
        )
    _validate_bootstrap_bin(
        runtime_root,
        immutable,
        bootstrap_digest=bootstrap_digest,
    )
    return record


def load_control_release(
    runtime_root: Path, release_id: str
) -> tuple[dict[str, Any], Path]:
    _require_private_directory(runtime_root)
    parent = runtime_root / "control-releases"
    _require_private_directory(parent)
    root = control_release_root(runtime_root, release_id)
    _require_private_directory(root)
    manifest_path = root / CONTROL_MANIFEST_NAME
    manifest = validate_control_manifest(_load_private_json(manifest_path))
    if manifest["release_id"] != release_id:
        raise ControlRuntimeError("control release directory differs from its identity")
    expected_names = set(manifest["files"]) | {CONTROL_MANIFEST_NAME}
    actual_names = {entry.name for entry in root.iterdir()}
    if actual_names != expected_names:
        raise ControlRuntimeError("control release inventory contains extra or missing files")
    for name, identity in manifest["files"].items():
        path = root / name
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ControlRuntimeError(f"control release file is unavailable: {name}") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != identity["mode"]
            or metadata.st_size != identity["size"]
            or sha256_file(path) != identity["sha256"]
        ):
            raise ControlRuntimeError(f"control release file differs: {name}")
    return manifest, root


def load_candidate_control(
    runtime_root: Path, candidate: object
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    record = validate_candidate_record(candidate)
    manifest, root = load_control_release(runtime_root, record["release_id"])
    if (
        sha256_file(root / CONTROL_MANIFEST_NAME) != record["manifest_sha256"]
        or any(
            manifest[key] != record[key]
            for key in ("source_sha", "source_tree", "release_id")
        )
    ):
        raise ControlRuntimeError("candidate control record differs from its release")
    return record, manifest, root


def load_active_control(
    runtime_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    _validate_bootstrap_authority(runtime_root)
    active = validate_active_control_record(
        _load_private_json(active_control_record_path(runtime_root))
    )
    manifest, root = load_control_release(runtime_root, active["release_id"])
    if (
        sha256_file(root / CONTROL_MANIFEST_NAME) != active["manifest_sha256"]
        or any(
            active[key] != manifest[key]
            for key in ("source_sha", "source_tree", "release_id")
        )
    ):
        raise ControlRuntimeError("active control record differs from its release")
    return active, manifest, root


def _active_matches_candidate(
    active: Mapping[str, Any], candidate: Mapping[str, Any]
) -> bool:
    return all(
        active.get(field) == candidate.get(field)
        for field in (
            "protocol_version",
            "release_id",
            "source_sha",
            "source_tree",
            "manifest_sha256",
            "operation_id",
        )
    )


def _require_deploy_lock_held(runtime_root: Path) -> None:
    path = runtime_root / "state/deploy.lock"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise ControlRuntimeError("deployment transition lock is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ControlRuntimeError("deployment transition lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        raise ControlRuntimeError("deployment transition marker is not lock-owned")
    finally:
        os.close(descriptor)


def _worker_projection_matches(
    active: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    source_sha: object,
    source_tree: object,
    unit: object,
    slot: object,
    operation_id: object,
) -> bool:
    entrypoint = manifest.get("entrypoints", {}).get("monomer-md")
    files = manifest.get("files")
    if (
        not isinstance(entrypoint, dict)
        or entrypoint.get("kind") != "worker"
        or not isinstance(files, dict)
        or source_sha != active.get("source_sha")
        or source_tree != active.get("source_tree")
        or not isinstance(unit, dict)
        or not isinstance(slot, dict)
    ):
        return False
    launcher = files.get(entrypoint.get("launcher"))
    return bool(
        isinstance(launcher, dict)
        and unit.get("control_release_id") == active.get("release_id")
        and unit.get("launcher_sha256") == launcher.get("sha256")
        and slot.get("source_sha") == source_sha
        and slot.get("source_tree") == source_tree
        and slot.get("operation_id") == operation_id
        and active.get("operation_id") == operation_id
    )


def _asset_pointer_matches(runtime_root: Path, asset: object) -> bool:
    if not isinstance(asset, dict) or not isinstance(asset.get("root"), str):
        return False
    root = Path(asset["root"])
    pointer = runtime_root / "state/current-assets"
    try:
        metadata = pointer.lstat()
        target = Path(os.readlink(pointer))
    except OSError:
        return False
    if not target.is_absolute():
        target = pointer.parent / target
    return bool(
        stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and target.absolute() == root.absolute()
    )


def _adopted_worker_route_matches(
    runtime_root: Path,
    active: Mapping[str, Any],
) -> bool:
    try:
        bootstrap = _validate_bootstrap_authority(runtime_root)
    except (ControlRuntimeError, OSError, ValueError):
        return False
    if bootstrap.get("schema_version") != 3:
        return False
    adopted = bootstrap.get("adopted_deployment")
    if (
        not isinstance(adopted, dict)
        or adopted.get("schema_version") != 1
        or adopted.get("active_control") != active
    ):
        return False
    monomer = adopted.get("monomer_md")
    if not isinstance(monomer, dict):
        return False
    unit = monomer.get("systemd_unit")
    active_slot = monomer.get("active_slot")
    slot_record = monomer.get("slot_record")
    if (
        not isinstance(unit, dict)
        or not isinstance(active_slot, dict)
        or not isinstance(slot_record, dict)
        or active_slot.get("source_sha") != adopted.get("source_sha")
        or active_slot.get("source_tree") != adopted.get("source_tree")
        or slot_record.get("source_sha") != adopted.get("source_sha")
        or slot_record.get("source_tree") != adopted.get("source_tree")
    ):
        return False
    paths = (
        (unit.get("target_path"), unit.get("sha256")),
        (unit.get("launcher_path"), unit.get("launcher_sha256")),
        (
            monomer.get("worker_env", {}).get("path")
            if isinstance(monomer.get("worker_env"), dict)
            else None,
            monomer.get("worker_env", {}).get("sha256")
            if isinstance(monomer.get("worker_env"), dict)
            else None,
        ),
        (
            monomer.get("active_slot_path"),
            monomer.get("active_slot_file_sha256"),
        ),
        (
            monomer.get("slot_record_path"),
            monomer.get("slot_record_file_sha256"),
        ),
    )
    for raw_path, expected_digest in paths:
        if (
            not isinstance(raw_path, str)
            or not Path(raw_path).is_absolute()
            or not isinstance(expected_digest, str)
            or DIGEST_RE.fullmatch(expected_digest) is None
        ):
            return False
        path = Path(raw_path)
        try:
            metadata = path.lstat()
        except OSError:
            return False
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or sha256_file(path) != expected_digest
        ):
            return False
    monomer_dft = adopted.get("monomer_dft")
    if not isinstance(monomer_dft, dict):
        return False
    dft_unit = monomer_dft.get("systemd_unit")
    dft_env = monomer_dft.get("runtime_env")
    dft_runtime = monomer_dft.get("runtime")
    dft_gpu = monomer_dft.get("gpu")
    if (
        not isinstance(dft_unit, dict)
        or not isinstance(dft_env, dict)
        or not isinstance(dft_runtime, dict)
        or not isinstance(dft_gpu, dict)
        or dft_runtime.get("release_sha") != adopted.get("source_sha")
        or dft_runtime.get("source_tree") != adopted.get("source_tree")
        or dft_gpu.get("index") != "2"
        or dft_gpu.get("guard_mode") != "enforce"
        or dft_gpu.get("uuid") != ADOPTED_DFT_GPU_UUID
    ):
        return False
    dft_paths = (
        (dft_unit.get("target_path"), dft_unit.get("sha256")),
        (dft_unit.get("launcher_path"), dft_unit.get("launcher_sha256")),
        (dft_env.get("path"), dft_env.get("sha256")),
        (
            dft_runtime.get("runtime_manifest_path"),
            dft_runtime.get("runtime_manifest_sha256"),
        ),
    )
    for raw_path, expected_digest in dft_paths:
        if (
            not isinstance(raw_path, str)
            or not Path(raw_path).is_absolute()
            or not isinstance(expected_digest, str)
            or DIGEST_RE.fullmatch(expected_digest) is None
        ):
            return False
        path = Path(raw_path)
        try:
            metadata = path.lstat()
        except OSError:
            return False
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or sha256_file(path) != expected_digest
        ):
            return False
    release_root_raw = dft_runtime.get("root")
    models = dft_runtime.get("models")
    if (
        not isinstance(release_root_raw, str)
        or not Path(release_root_raw).is_absolute()
        or not isinstance(models, dict)
    ):
        return False
    release_root = Path(release_root_raw)
    try:
        if adopted_dft_runtime_inventory(release_root) != dft_runtime.get(
            "runtime_inventory_sha256"
        ):
            return False
    except (ControlRuntimeError, OSError, ValueError):
        return False
    for name, expected_digest in models.items():
        if (
            not isinstance(name, str)
            or SAFE_NAME_RE.fullmatch(name) is None
            or not isinstance(expected_digest, str)
            or DIGEST_RE.fullmatch(expected_digest) is None
        ):
            return False
        path = release_root / "aimnet-cache" / name
        try:
            metadata = path.lstat()
        except OSError:
            return False
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or sha256_file(path) != expected_digest
        ):
            return False
    return _asset_pointer_matches(runtime_root, adopted.get("asset_identity"))


def _dft_projection_matches(
    runtime_root: Path,
    active: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    source_sha: object,
    source_tree: object,
    projection: object,
) -> bool:
    if (
        not isinstance(source_sha, str)
        or not isinstance(source_tree, str)
        or not isinstance(projection, dict)
        or active.get("source_sha") != source_sha
        or active.get("source_tree") != source_tree
        or manifest.get("source_sha") != source_sha
        or manifest.get("source_tree") != source_tree
    ):
        return False
    runtime = projection.get("runtime")
    env_transition = projection.get("runtime_env")
    unit = projection.get("systemd_unit")
    gpu = projection.get("gpu")
    if (
        not isinstance(runtime, dict)
        or not isinstance(env_transition, dict)
        or not isinstance(unit, dict)
        or not isinstance(gpu, dict)
    ):
        return False
    runtime_env = env_transition.get("target", env_transition)
    if (
        not isinstance(runtime_env, dict)
        or set(runtime_env) != {"path", "sha256", "values"}
        or not isinstance(runtime_env.get("values"), dict)
        or set(runtime_env["values"]) != MONOMER_DFT_ENV_KEYS
        or any(
            not isinstance(value, str) or not value
            for value in runtime_env["values"].values()
        )
        or set(gpu)
        != {
            "index",
            "uuid",
            "guard_mode",
            "guard_state_path",
            "guard_schema_version",
        }
    ):
        return False
    values = runtime_env["values"]
    release_root = runtime_root / "worker-venvs/dft" / source_sha
    manifest_path = release_root / "runtime.json"
    env_path = runtime_root / "config/monomer-dft-runtime.env"
    aimnet_cache = release_root / "aimnet-cache"
    python_path = release_root / "venv/bin/python"
    warp_cache = runtime_root / "state/monomer-dft-warp-cache" / source_sha
    role = manifest.get("entrypoints", {}).get("monomer-dft")
    launcher_name = role.get("launcher") if isinstance(role, dict) else None
    launcher = manifest.get("files", {}).get(launcher_name)
    expected_launcher_path = (
        runtime_root / "control-releases" / str(active.get("release_id")) / str(launcher_name)
    )
    models = runtime.get("models")
    if (
        runtime.get("release_sha") != source_sha
        or runtime.get("source_tree") != source_tree
        or runtime.get("root") != str(release_root)
        or runtime.get("runtime_manifest_path") != str(manifest_path)
        or runtime.get("python") != str(python_path)
        or any(
            not isinstance(runtime.get(name), str)
            or DIGEST_RE.fullmatch(runtime[name]) is None
            for name in (
                "runtime_manifest_sha256",
                "runtime_inventory_sha256",
                "requirements_lock_sha256",
                "aimnet_source_lock_sha256",
            )
        )
        or not isinstance(models, dict)
        or set(models) != MONOMER_DFT_MODEL_FILES
        or any(
            not isinstance(checksum, str)
            or DIGEST_RE.fullmatch(checksum) is None
            for checksum in models.values()
        )
        or values["MONOMER_DFT_RELEASE_SHA"] != source_sha
        or values["MONOMER_DFT_RUNTIME_CONTRACT_SHA256"]
        != runtime["runtime_manifest_sha256"]
        or values["MONOMER_DFT_RUNTIME_INVENTORY_SHA256"]
        != runtime["runtime_inventory_sha256"]
        or values["MONOMER_DFT_PYTHON"] != str(python_path)
        or values["AIMNET_CACHE_DIR"] != str(aimnet_cache)
        or values["WARP_CACHE_PATH"] != str(warp_cache)
        or values["NEXPOLY_DFT_GPU_GUARD_MODE"] != gpu.get("guard_mode")
        or runtime_env.get("path") != str(env_path)
        or gpu.get("index") != MONOMER_DFT_GPU_INDEX
        or gpu.get("uuid") != ADOPTED_DFT_GPU_UUID
        or gpu.get("guard_mode") not in {"enforce", "observe"}
        or gpu.get("guard_state_path") != str(MONOMER_DFT_GUARD_STATE)
        or gpu.get("guard_schema_version") != 1
        or unit.get("target_path") != str(MONOMER_DFT_UNIT_TARGET)
        or unit.get("control_release_id") != active.get("release_id")
        or not isinstance(role, dict)
        or role.get("kind") != "worker"
        or not isinstance(launcher, dict)
        or unit.get("launcher_path") != str(expected_launcher_path)
        or unit.get("launcher_sha256") != launcher.get("sha256")
    ):
        return False
    preparing_path = release_root / ".preparing.json"
    ready_path = release_root / "READY.json"
    if preparing_path.exists() or preparing_path.is_symlink():
        return False
    try:
        ready_metadata = ready_path.lstat()
        ready = _load_private_json(ready_path)
    except (ControlRuntimeError, OSError, ValueError):
        return False
    if (
        not stat.S_ISREG(ready_metadata.st_mode)
        or ready_path.is_symlink()
        or ready_metadata.st_uid != os.geteuid()
        or ready_metadata.st_nlink != 1
        or stat.S_IMODE(ready_metadata.st_mode) != 0o600
        or set(ready)
        != {
            "schema_version",
            "status",
            "release_sha",
            "source_tree",
            "requirements_lock_sha256",
            "aimnet_source_lock_sha256",
            "runtime",
            "ready_at",
        }
        or ready.get("schema_version") != 1
        or ready.get("status") != "ready"
        or ready.get("release_sha") != source_sha
        or ready.get("source_tree") != source_tree
        or ready.get("requirements_lock_sha256")
        != runtime["requirements_lock_sha256"]
        or ready.get("aimnet_source_lock_sha256")
        != runtime["aimnet_source_lock_sha256"]
        or ready.get("runtime") != runtime
        or not isinstance(ready.get("ready_at"), str)
        or not ready["ready_at"]
    ):
        return False
    paths = (
        (manifest_path, runtime["runtime_manifest_sha256"], 0o600),
        (env_path, runtime_env.get("sha256"), 0o600),
        (MONOMER_DFT_UNIT_TARGET, unit.get("sha256"), 0o600),
        (expected_launcher_path, launcher.get("sha256"), 0o700),
    )
    for path, expected_digest, expected_mode in paths:
        if (
            not path.is_absolute()
            or not isinstance(expected_digest, str)
            or DIGEST_RE.fullmatch(expected_digest) is None
        ):
            return False
        try:
            metadata = path.lstat()
        except OSError:
            return False
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != expected_mode
            or sha256_file(path) != expected_digest
        ):
            return False
    for name, expected_digest in models.items():
        path = aimnet_cache / name
        try:
            metadata = path.lstat()
        except OSError:
            return False
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or sha256_file(path) != expected_digest
        ):
            return False
    try:
        return (
            governed_dft_runtime_inventory(release_root)
            == runtime["runtime_inventory_sha256"]
        )
    except (ControlRuntimeError, OSError, ValueError):
        return False


def _validate_worker_route_authority(
    runtime_root: Path,
    active: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    role: str,
) -> None:
    current_path = runtime_root / "state/current-deployment.json"
    marker_path = runtime_root / "state/deploy-in-progress.json"
    marker_present = marker_path.exists() or marker_path.is_symlink()
    if (
        not marker_present
        and not (current_path.exists() or current_path.is_symlink())
        and _adopted_worker_route_matches(runtime_root, active)
    ):
        return
    if not marker_present and (current_path.exists() or current_path.is_symlink()):
        current = _load_private_json(current_path)
        compatibility = manifest["compatibility"]["current_state_schema_versions"]
        projection_matches = (
            _dft_projection_matches(
                runtime_root,
                active,
                manifest,
                source_sha=current.get("source_sha"),
                source_tree=current.get("source_tree"),
                projection=current.get("monomer_dft"),
            )
            if role == "monomer-dft"
            else _worker_projection_matches(
                active,
                manifest,
                source_sha=current.get("source_sha"),
                source_tree=current.get("source_tree"),
                unit=current.get("monomer_md_systemd_unit"),
                slot=current.get("active_monomer_md_slot"),
                operation_id=current.get("operation_id"),
            )
        )
        if (
            current.get("schema_version") in compatibility
            and current.get("active_control") == active
            and projection_matches
            and _asset_pointer_matches(runtime_root, current.get("asset_identity"))
        ):
            return

    if not marker_present:
        raise ControlRuntimeError(
            "active Worker controls differ from governed deployment authority"
        )
    marker = _load_private_json(marker_path)
    operation_id = marker.get("operation_id")
    descriptor_digest = marker.get("descriptor_sha256")
    if (
        marker.get("schema_version")
        not in manifest["compatibility"]["marker_schema_versions"]
        or marker.get("action") not in {"deploy", "explicit-rollback"}
        or not isinstance(operation_id, str)
        or OPERATION_ID_RE.fullmatch(operation_id) is None
        or not isinstance(descriptor_digest, str)
        or DIGEST_RE.fullmatch(descriptor_digest) is None
    ):
        raise ControlRuntimeError("Worker transition marker is invalid")
    _require_deploy_lock_held(runtime_root)
    descriptor_path = runtime_root / "state/prepared" / operation_id / "descriptor.json"
    descriptor = _load_private_json(descriptor_path)
    if sha256_file(descriptor_path) != descriptor_digest:
        raise ControlRuntimeError("Worker transition descriptor differs from marker")
    controller = descriptor.get("controller")
    repository = descriptor.get("repository")
    monomer = descriptor.get("monomer_md")
    previous = descriptor.get("previous_deployment")
    if not isinstance(controller, dict) or not isinstance(repository, dict):
        raise ControlRuntimeError("Worker transition descriptor is incomplete")
    candidate = controller.get("executor_control")
    candidate_digest = controller.get("executor_control_sha256")
    if (
        not isinstance(candidate, dict)
        or canonical_json_digest(candidate) != candidate_digest
        or marker.get("executor_control") != candidate
        or marker.get("executor_control_sha256") != candidate_digest
    ):
        raise ControlRuntimeError("Worker transition control authority differs")
    common_effects = [
        "source_switched",
        "slot_switched",
        "unit_switched",
        "control_switched",
        "asset_switched",
    ]
    if role == "monomer-dft" and marker.get("schema_version") == 3:
        common_effects.extend(
            ("worker_env_switched", "dft_runtime_switched", "dft_unit_switched")
        )
    switched = all(
        marker.get(field) is True
        for field in common_effects
    )
    restored = (
        marker.get("runtime_stopped") is True
        and all(
            marker.get(field) is False
            for field in common_effects
        )
    )
    pre_stop_previous = (
        marker.get("action") == "deploy"
        and marker.get("runtime_stopped") is False
        and marker.get("database_change_started") is False
        and marker.get("phase") in {"prepared", "drain-started", "drained", "failed"}
        and (
            marker.get("phase") != "failed"
            or marker.get("failed_phase") in {"prepared", "drain-started", "drained"}
        )
        and all(
            marker.get(field) is False
            for field in common_effects
        )
    )
    if switched and _active_matches_candidate(active, candidate):
        candidate_projection_matches = (
            _dft_projection_matches(
                runtime_root,
                active,
                manifest,
                source_sha=repository.get("target_sha"),
                source_tree=repository.get("target_tree"),
                projection=descriptor.get("monomer_dft"),
            )
            if role == "monomer-dft"
            else isinstance(monomer, dict)
            and _worker_projection_matches(
                active,
                manifest,
                source_sha=repository.get("target_sha"),
                source_tree=repository.get("target_tree"),
                unit=monomer.get("systemd_unit"),
                slot=monomer.get("slot_record"),
                operation_id=operation_id,
            )
        )
        if not candidate_projection_matches:
            raise ControlRuntimeError("candidate Worker transition identity differs")
        release_input = descriptor.get("release_input")
        if not isinstance(release_input, dict) or not _asset_pointer_matches(
            runtime_root, release_input.get("asset")
        ):
            raise ControlRuntimeError("candidate Worker asset identity differs")
        return
    previous_control = controller.get("previous_active_control")
    adopted = descriptor.get("adopted_deployment")
    if (
        (restored or pre_stop_previous)
        and previous is None
        and isinstance(adopted, dict)
        and active == previous_control
        and previous_control == adopted.get("active_control")
        and _adopted_worker_route_matches(runtime_root, active)
    ):
        return
    if (
        (restored or pre_stop_previous)
        and isinstance(previous, dict)
        and active == previous_control
        and previous_control == previous.get("active_control")
    ):
        if pre_stop_previous:
            if not (current_path.exists() or current_path.is_symlink()):
                raise ControlRuntimeError(
                    "unchanged pre-stop Worker authority has no current state"
                )
            current = _load_private_json(current_path)
            if current != previous:
                raise ControlRuntimeError(
                    "unchanged pre-stop Worker state differs from previous deployment"
                )
        previous_projection_matches = (
            _dft_projection_matches(
                runtime_root,
                active,
                manifest,
                source_sha=previous.get("source_sha"),
                source_tree=previous.get("source_tree"),
                projection=previous.get("monomer_dft"),
            )
            if role == "monomer-dft"
            else _worker_projection_matches(
                active,
                manifest,
                source_sha=previous.get("source_sha"),
                source_tree=previous.get("source_tree"),
                unit=previous.get("monomer_md_systemd_unit"),
                slot=previous.get("active_monomer_md_slot"),
                operation_id=previous.get("operation_id"),
            )
        )
        if not previous_projection_matches:
            raise ControlRuntimeError("restored Worker transition identity differs")
        if not _asset_pointer_matches(runtime_root, previous.get("asset_identity")):
            raise ControlRuntimeError("restored Worker asset identity differs")
        return
    raise ControlRuntimeError(
        "active Worker controls differ from governed deployment authority"
    )


def _bootstrap_router_deploy_release(
    runtime_root: Path,
    arguments: list[str],
) -> tuple[dict[str, Any], Path] | None:
    """Gate and route the one-time first deployment before active-control moves."""

    bootstrap = _validate_bootstrap_authority(runtime_root)
    immutable = bootstrap.get("immutable_files")
    if not isinstance(immutable, dict):
        raise ControlRuntimeError("bootstrap immutable authority is unavailable")
    bootstrap_path = runtime_root / "state" / BOOTSTRAP_AUTHORITY_NAME
    observed_bootstrap, bootstrap_digest = _load_private_canonical_json(
        bootstrap_path
    )
    if observed_bootstrap != bootstrap:
        raise ControlRuntimeError(
            "bootstrap authority changed during router validation"
        )
    predecessor_selector_digest = immutable.get("control_runtime_selector.py")
    if (
        not isinstance(predecessor_selector_digest, str)
        or DIGEST_RE.fullmatch(predecessor_selector_digest) is None
    ):
        raise ControlRuntimeError("bootstrap selector authority is invalid")
    selector_path = runtime_root / "bin/control_runtime_selector.py"
    selector_digest = sha256_file(selector_path)
    transition = _bootstrap_router_transition(
        runtime_root,
        bootstrap_digest=bootstrap_digest,
        predecessor_selector_digest=predecessor_selector_digest,
        observed_selector_digest=selector_digest,
    )
    if transition is None:
        return None
    intent, authority = transition
    if selector_digest == predecessor_selector_digest:
        # Worker routes may remain on the predecessor while the reviewed
        # selector is staged, but deploy must not fall through to A once any
        # successor intent is durable. The publisher's independent fence is a
        # second guard, not a substitute for this router-level fail closure.
        raise ControlRuntimeError(
            "bootstrap-router successor is in progress; deployment is blocked"
        )
    if authority is None:
        raise ControlRuntimeError(
            "bootstrap-router successor is incomplete; deployment is blocked"
        )
    current_path = runtime_root / "state/current-deployment.json"
    if current_path.exists() or current_path.is_symlink():
        current, _current_digest = _load_private_canonical_json(
            current_path,
            maximum=32 * 1024 * 1024,
        )
        lineage = current.get("adoption_successor_lineage")
        lineage_fields = {
            "schema_version",
            "source_successor_authority_sha256",
            "source_successor_completed_journal_sha256",
            "unit_permission_authority_sha256",
            "unit_permission_completed_journal_sha256",
            "unit_permission_transaction_inventory_sha256",
            "production_git_snapshot_authority_sha256",
            "bootstrap_router_intent_sha256",
            "bootstrap_router_authority_sha256",
        }
        active, active_manifest, _active_root = load_active_control(runtime_root)
        target = intent["target_control_release"]
        authority_digest = sha256_bytes(
            canonical_json_bytes(authority) + b"\n"
        )
        if (
            current.get("schema_version") != 3
            or current.get("status") != "success"
            or current.get("source_sha") != intent["target_source_sha"]
            or current.get("source_tree") != intent["target_source_tree"]
            or current.get("active_control") != active
            or not isinstance(lineage, dict)
            or set(lineage) != lineage_fields
            or lineage.get("schema_version") != 3
            or any(
                not isinstance(lineage.get(field), str)
                or DIGEST_RE.fullmatch(lineage[field]) is None
                for field in lineage_fields - {"schema_version"}
            )
            or lineage.get("source_successor_authority_sha256")
            != intent["source_successor_authority_sha256"]
            or lineage.get("unit_permission_authority_sha256")
            != intent["unit_permission_authority_sha256"]
            or lineage.get("production_git_snapshot_authority_sha256")
            != intent["snapshot_authority_sha256"]
            or lineage.get("bootstrap_router_intent_sha256")
            != authority["intent_sha256"]
            or lineage.get("bootstrap_router_authority_sha256")
            != authority_digest
            or active.get("release_id") != target["release_id"]
            or active.get("source_sha") != target["source_sha"]
            or active.get("source_tree") != target["source_tree"]
            or active.get("manifest_sha256") != target["manifest_sha256"]
            or active_manifest.get("release_id") != target["release_id"]
        ):
            raise ControlRuntimeError(
                "current-state cannot retire the bootstrap-router successor"
            )
        # The one-time route retires only after a target-bound current-state
        # and the exact target control release are both durable.
        return None
    command = arguments[0] if arguments else None
    if command == "plan":
        raise ControlRuntimeError(
            "first deployment plan must run read-only from the exact private target clone"
        )
    target_commands = {
        "prepare",
        "bridge-plan",
        "bridge-prepare",
    }
    sha_count = arguments.count("--sha")
    if sha_count > 1 or command in target_commands and sha_count != 1:
        raise ControlRuntimeError("first deployment target SHA is ambiguous")
    if sha_count == 1:
        try:
            target_sha = arguments[arguments.index("--sha") + 1]
        except IndexError:
            raise ControlRuntimeError("first deployment target SHA is missing") from None
        if target_sha != intent["target_source_sha"]:
            raise ControlRuntimeError(
                "first deployment target differs from router successor"
            )
    verifier = intent["router_files"]["production_git_snapshot.py"]
    verifier_path = runtime_root / verifier["relative_path"]
    environment = {
        "USER": "devuser",
        "LOGNAME": "devuser",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        completed = subprocess.run(
            [
                "/usr/bin/python3",
                "-I",
                "-B",
                str(verifier_path),
                "verify-integrity",
            ],
            cwd=verifier_path.parent,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=900,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ControlRuntimeError(
            "production Git snapshot integrity gate failed"
        ) from exc
    if len(completed.stdout) > 4 * 1024 * 1024 or len(completed.stderr) > 1024 * 1024:
        raise ControlRuntimeError("production Git snapshot gate output is oversized")
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ControlRuntimeError("production Git snapshot gate output is invalid") from exc
    if (
        not isinstance(result, dict)
        or set(result) != {"action", "verified", "authority", "authority_sha256"}
        or completed.stdout != canonical_json_bytes(result) + b"\n"
        or result.get("action")
        != "production-git-snapshot-verify-integrity"
        or result.get("verified") is not True
        or result.get("authority_sha256") != intent["snapshot_authority_sha256"]
        or not isinstance(result.get("authority"), dict)
        or result["authority"].get("target_source_sha")
        != intent["target_source_sha"]
    ):
        raise ControlRuntimeError("production Git snapshot gate evidence differs")
    if current_path.exists() or current_path.is_symlink():
        raise ControlRuntimeError("current-state changed during first deployment gate")
    after = _bootstrap_router_transition(
        runtime_root,
        bootstrap_digest=_load_private_canonical_json(bootstrap_path)[1],
        predecessor_selector_digest=predecessor_selector_digest,
        observed_selector_digest=sha256_file(selector_path),
    )
    if after != transition:
        raise ControlRuntimeError("bootstrap-router authority changed during gate")
    target = intent["target_control_release"]
    manifest, release_root = load_control_release(runtime_root, target["release_id"])
    return manifest, release_root


def _selected_release(
    runtime_root: Path, role: str, arguments: list[str]
) -> tuple[dict[str, Any], Path]:
    """Route recovery/apply to a sealed candidate; all other calls use active."""

    router_release = (
        _bootstrap_router_deploy_release(runtime_root, arguments)
        if role == "deploy"
        else None
    )
    alias_marker = None
    deploy_command = arguments[0] if role == "deploy" and arguments else None
    deploy_preparation = deploy_command in {
        "plan",
        "prepare",
        "bridge-plan",
        "bridge-prepare",
        "prepare-abort",
    }
    if role in {
        "deploy",
        "contract-0012",
        "reconcile-production-0005-alias",
    }:
        alias_marker = load_production_0005_alias_gate(
            runtime_root,
            require_completed=(
                role == "contract-0012"
                or (role == "deploy" and not deploy_preparation)
            ),
        )
    if (
        role == "deploy"
        and deploy_preparation
        and alias_marker is not None
        and alias_marker.get("phase") != "completed"
    ):
        raise ControlRuntimeError(
            "interrupted alias reconciliation must recover before deployment preparation"
        )
    if role in {"contract-0012", "reconcile-production-0005-alias"}:
        deploy_marker = runtime_root / "state/deploy-in-progress.json"
        if deploy_marker.exists() or deploy_marker.is_symlink():
            raise ControlRuntimeError(
                "database maintenance is blocked by an interrupted code deployment"
            )
    if role == "reconcile-production-0005-alias":
        contract_marker = runtime_root / "state/contract-0012-in-progress.json"
        if contract_marker.exists() or contract_marker.is_symlink():
            raise ControlRuntimeError(
                "ledger-alias maintenance is blocked by interrupted 0012 maintenance"
            )
    if (
        role == "reconcile-production-0005-alias"
        and alias_marker is not None
    ):
        if alias_marker.get("authority_kind") == ADOPTION_AUTHORITY_KIND:
            raise ControlRuntimeError(
                "production 0005 maintenance is already sealed by manual adoption"
            )
        _validate_bootstrap_authority(runtime_root)
        identity = alias_marker.get("identity")
        control = identity.get("control") if isinstance(identity, dict) else None
        release_id = control.get("release_id") if isinstance(control, dict) else None
        if not isinstance(release_id, str) or RELEASE_ID_RE.fullmatch(release_id) is None:
            raise ControlRuntimeError(
                "recorded alias reconciliation lacks sealed control authority"
            )
        recovery_manifest, recovery_root = load_control_release(
            runtime_root, release_id
        )
        entrypoint = recovery_manifest["entrypoints"].get(
            "reconcile-production-0005-alias"
        )
        if (
            not isinstance(entrypoint, dict)
            or entrypoint.get("kind") != "python"
            or control.get("source_sha") != recovery_manifest["source_sha"]
            or control.get("source_tree") != recovery_manifest["source_tree"]
            or control.get("manifest_sha256")
            != sha256_file(recovery_root / CONTROL_MANIFEST_NAME).removeprefix(
                "sha256:"
            )
            or control.get("script_sha256")
            != sha256_file(recovery_root / str(entrypoint.get("file"))).removeprefix(
                "sha256:"
            )
        ):
            raise ControlRuntimeError(
                "recorded alias reconciliation control authority differs"
            )
        return recovery_manifest, recovery_root
    if router_release is not None:
        return router_release
    active, manifest, root = load_active_control(runtime_root)
    if role in {"monomer-md", "monomer-dft"}:
        _validate_worker_route_authority(
            runtime_root, active, manifest, role=role
        )
    if role != "deploy" or not arguments:
        return manifest, root
    command = arguments[0]
    operation_id: str | None = None
    if command in {"apply", "bridge-apply", "rollback"}:
        if arguments.count("--operation-id") != 1:
            raise ControlRuntimeError("deploy operation ID must occur exactly once")
        try:
            index = arguments.index("--operation-id")
            operation_id = arguments[index + 1]
        except IndexError:
            raise ControlRuntimeError("deploy operation ID is missing") from None
        if OPERATION_ID_RE.fullmatch(operation_id) is None:
            raise ControlRuntimeError("deploy operation ID is invalid")
    marker_path = runtime_root / "state/deploy-in-progress.json"
    if marker_path.exists() or marker_path.is_symlink():
        marker = _load_private_json(marker_path)
        if (
            not isinstance(marker.get("schema_version"), int)
            or isinstance(marker.get("schema_version"), bool)
            or marker.get("action") not in {"deploy", "explicit-rollback"}
            or not isinstance(marker.get("operation_id"), str)
            or OPERATION_ID_RE.fullmatch(marker["operation_id"]) is None
            or not isinstance(marker.get("executor_control"), dict)
            or not isinstance(marker.get("executor_control_sha256"), str)
        ):
            raise ControlRuntimeError("deployment marker lacks sealed control authority")
        candidate = marker["executor_control"]
        if (
            canonical_json_digest(candidate) != marker["executor_control_sha256"]
            or candidate.get("operation_id") != marker["operation_id"]
            or operation_id is not None
            and operation_id != marker["operation_id"]
        ):
            raise ControlRuntimeError("deployment marker control identity differs")
        _record, candidate_manifest, candidate_root = load_candidate_control(
            runtime_root, candidate
        )
        if (
            marker["schema_version"]
            not in candidate_manifest["compatibility"]["marker_schema_versions"]
        ):
            raise ControlRuntimeError(
                "deployment marker schema is unsupported by its executor"
            )
        return candidate_manifest, candidate_root
    if operation_id is not None:
        ready_path = runtime_root / "state/prepared" / operation_id / "ready.json"
        ready = _load_private_json(ready_path)
        if (
            ready.get("schema_version") != 1
            or ready.get("status") != "ready"
            or ready.get("operation_id") != operation_id
        ):
            raise ControlRuntimeError("prepared control operation identity differs")
        candidate = ready.get("executor_control")
        if (
            not isinstance(candidate, dict)
            or candidate.get("operation_id") != operation_id
            or canonical_json_digest(candidate)
            != ready.get("executor_control_sha256")
        ):
            raise ControlRuntimeError("prepared control identity differs")
        _record, candidate_manifest, candidate_root = load_candidate_control(
            runtime_root, candidate
        )
        return candidate_manifest, candidate_root
    return manifest, root


def _exec_role(
    role: str,
    arguments: list[str],
    environment: Mapping[str, str],
    *,
    runtime_root: Path = PRODUCTION_RUNTIME_ROOT,
) -> None:
    if SAFE_ROLE_RE.fullmatch(role) is None:
        raise ControlRuntimeError("control selector role is invalid")
    runtime_root = runtime_root.absolute()
    manifest, release = _selected_release(runtime_root, role, arguments)
    entrypoint = manifest["entrypoints"].get(role)
    if entrypoint is None:
        raise ControlRuntimeError("active control release does not provide this role")
    allowed = {
        "HOME",
        "USER",
        "LOGNAME",
        "PATH",
        "LANG",
        "LC_ALL",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
    }
    clean_environment = {
        key: value for key, value in environment.items() if key in allowed
    }
    clean_environment.update(
        {
            "HOME": "/home/devuser",
            "USER": "devuser",
            "LOGNAME": "devuser",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "XDG_RUNTIME_DIR": "/run/user/1001",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    clean_environment["NEXPOLY_ACTIVE_CONTROL_ROOT"] = str(release)
    clean_environment["NEXPOLY_ACTIVE_CONTROL_RELEASE_ID"] = manifest["release_id"]
    if role == "reconcile-production-0005-alias":
        dsn = environment.get("NEXPOLY_PRODUCTION_POSTGRES_DSN")
        if (
            not isinstance(dsn, str)
            or not dsn
            or len(dsn) > 8192
            or any(character in dsn for character in ("\x00", "\r", "\n"))
        ):
            raise ControlRuntimeError(
                "production PostgreSQL DSN is unavailable or malformed"
            )
        clean_environment["NEXPOLY_PRODUCTION_POSTGRES_DSN"] = dsn
    if role == "postgres-media-evidence":
        authority_digest = environment.get(
            "NEXPOLY_CONTRACT_0012_MEDIA_AUTHORITY_RULES_SHA256"
        )
        role_sql_digest = environment.get(
            "NEXPOLY_CONTRACT_0012_AUDIT_ROLE_SQL_SHA256"
        )
        launcher_digest = environment.get(
            "NEXPOLY_MEDIA_LAUNCHER_SHA256"
        )
        implementation_digest = environment.get(
            "NEXPOLY_MEDIA_IMPLEMENTATION_SHA256"
        )
        if (
            not isinstance(authority_digest, str)
            or DIGEST_RE.fullmatch(authority_digest) is None
            or not isinstance(role_sql_digest, str)
            or DIGEST_RE.fullmatch(role_sql_digest) is None
            or not isinstance(launcher_digest, str)
            or DIGEST_RE.fullmatch(launcher_digest) is None
            or not isinstance(implementation_digest, str)
            or DIGEST_RE.fullmatch(implementation_digest) is None
            or manifest["files"].get(entrypoint["file"], {}).get(
                "sha256"
            )
            != launcher_digest
            or manifest["files"].get(
                "postgres_media_evidence.py",
                {},
            ).get("sha256")
            != implementation_digest
        ):
            raise ControlRuntimeError(
                "PostgreSQL media authority digests are unavailable"
            )
        clean_environment[
            "NEXPOLY_CONTRACT_0012_MEDIA_AUTHORITY_RULES_SHA256"
        ] = authority_digest
        clean_environment[
            "NEXPOLY_CONTRACT_0012_AUDIT_ROLE_SQL_SHA256"
        ] = role_sql_digest
        clean_environment[
            "NEXPOLY_MEDIA_LAUNCHER_SHA256"
        ] = launcher_digest
        clean_environment[
            "NEXPOLY_MEDIA_IMPLEMENTATION_SHA256"
        ] = implementation_digest
    python = "/usr/bin/python3"
    manifest_identity = canonical_json_bytes(
        _control_manifest_identity(release)
    ).decode("ascii")
    file_identities = canonical_json_bytes(manifest["files"]).decode("ascii")

    def pinned_python(
        descriptor: int,
        logical_path: Path,
        values: list[str],
    ) -> list[str]:
        return [
            python,
            "-I",
            "-B",
            "-c",
            PINNED_PYTHON_BOOTSTRAP,
            str(descriptor),
            str(logical_path),
            str(release),
            manifest_identity,
            file_identities,
            *values,
        ]

    if entrypoint["kind"] == "python":
        target_descriptor = _open_verified_control_file(
            release,
            manifest,
            entrypoint["file"],
        )
        target = release / entrypoint["file"]
        argv = pinned_python(target_descriptor, target, arguments)
    else:
        environment_descriptor = _open_verified_control_file(
            release,
            manifest,
            entrypoint["environment_loader"],
        )
        launcher_descriptor = _open_verified_control_file(
            release,
            manifest,
            entrypoint["launcher"],
        )
        environment_loader = release / entrypoint["environment_loader"]
        launcher = release / entrypoint["launcher"]
        config = runtime_root / entrypoint["config_relative"]
        launcher_argv = pinned_python(
            launcher_descriptor,
            launcher,
            arguments,
        )
        argv = pinned_python(
            environment_descriptor,
            environment_loader,
            [
                "exec",
                str(config),
                "--",
                *launcher_argv,
            ],
        )
    os.execve(python, argv, clean_environment)


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) < 2 or values[0] != "run":
        print("control-runtime-selector: usage: run <role> [arguments...]", file=sys.stderr)
        return 2
    try:
        selector_path = Path(__file__).resolve()
        runtime_root = selector_path.parent.parent
        if selector_path.parent.name != "bin":
            raise ControlRuntimeError(
                "control selector is not running from the immutable bin root"
            )
        _exec_role(
            values[1],
            values[2:],
            os.environ,
            runtime_root=runtime_root,
        )
    except (ControlRuntimeError, OSError, ValueError) as exc:
        print(f"control-runtime-selector: error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
