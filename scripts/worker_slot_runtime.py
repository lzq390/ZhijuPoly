#!/usr/bin/env python3
"""Strict runtime identity helpers for production Worker A/B slots.

This module deliberately uses only the Python standard library.  It is shared
by the pull-deploy controller, the stable host launcher, and the Worker health
identity check so all three paths interpret the A/B selection records in the
same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any, Mapping, NoReturn


PRODUCTION_SOURCE_ROOT = Path("/data/lzq/gith/nexpoly")
PRODUCTION_RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
WORKER_LOCK_RELATIVE_PATH = Path("workers/monomer_md_worker/requirements.lock")

ACTIVE_RECORD_RELATIVE_PATH = Path("state/monomer-md-active-slot.json")
SLOT_RECORD_DIRECTORY = Path("state/worker-slots")
WORKER_VENV_DIRECTORY = Path("worker-venvs")

COMPONENT = "monomer-md"
SLOTS = frozenset({"a", "b"})
MAX_PRIVATE_JSON_BYTES = 64 * 1024
ACTIVE_RECORD_SCHEMA_VERSION = 1
SLOT_RECORD_SCHEMA_VERSION = 2

SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
OPERATION_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{7,127}\Z")

ACTIVE_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "component",
        "slot",
        "source_sha",
        "source_tree",
        "worker_lock_sha256",
        "slot_record_sha256",
        "operation_id",
        "activated_at",
    }
)
SLOT_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "component",
        "status",
        "slot",
        "source_sha",
        "source_tree",
        "worker_lock_sha256",
        "requirements_sha256",
        "wheel_cache_key",
        "wheel_inventory_sha256",
        "venv_prefix",
        "venv_inventory_sha256",
        "base_python_configured_path",
        "base_python_identity_sha256",
        "prepared_operation_id",
        "prepared_at",
    }
)


class WorkerSlotError(RuntimeError):
    """A fail-closed production Worker slot or source identity error."""


@dataclass(frozen=True)
class ActiveSlotRecord:
    schema_version: int
    component: str
    slot: str
    source_sha: str
    source_tree: str
    worker_lock_sha256: str
    slot_record_sha256: str
    operation_id: str
    activated_at: str


@dataclass(frozen=True)
class SlotRecord:
    schema_version: int
    component: str
    status: str
    slot: str
    source_sha: str
    source_tree: str
    worker_lock_sha256: str
    requirements_sha256: str
    wheel_cache_key: str
    wheel_inventory_sha256: str
    venv_prefix: str
    venv_inventory_sha256: str
    base_python_configured_path: str
    base_python_identity_sha256: str
    prepared_operation_id: str
    prepared_at: str


@dataclass(frozen=True)
class RuntimeSelection:
    active: ActiveSlotRecord
    slot: SlotRecord
    active_path: Path
    slot_path: Path


@dataclass(frozen=True)
class GitCheckoutIdentity:
    source_root: Path
    source_sha: str
    source_tree: str


WORKER_BASE_IDENTITY_FIELDS = frozenset(
    {
        "schema_version",
        "configured_path",
        "resolved_path",
        "executable_sha256",
        "executable_size",
        "implementation",
        "python_version",
        "python_abi",
        "prefix",
        "base_prefix",
        "distribution_count",
        "distribution_metadata_sha256",
        "conda_package_count",
        "conda_metadata_sha256",
    }
)

# This program and the surrounding material intentionally preserve the
# original release-controller ``worker-base-identity`` digest contract.  The
# pull controller and the live launcher must never derive a second, subtly
# incompatible identity for the same frozen Python environment.
WORKER_BASE_IDENTITY_PROGRAM = r'''
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys


def digest_bytes(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def canonical_name(value):
    return re.sub(r"[-_.]+", "-", value).lower()


distributions = []
for distribution in importlib.metadata.distributions():
    name = distribution.metadata.get("Name")
    if not name:
        continue
    distributions.append(
        {
            "name": canonical_name(name),
            "version": distribution.version,
            "metadata_sha256": digest_bytes(
                (distribution.read_text("METADATA") or "").encode("utf-8", "surrogateescape")
            ),
            "record_sha256": digest_bytes(
                (distribution.read_text("RECORD") or "").encode("utf-8", "surrogateescape")
            ),
            "direct_url_sha256": digest_bytes(
                (distribution.read_text("direct_url.json") or "").encode("utf-8", "surrogateescape")
            ),
        }
    )
distributions.sort(key=lambda item: tuple(item.values()))
distribution_bytes = json.dumps(
    distributions, sort_keys=True, separators=(",", ":")
).encode("utf-8")

prefix = Path(sys.prefix).resolve()
conda_records = []
conda_meta = prefix / "conda-meta"
if conda_meta.is_dir():
    for path in sorted(conda_meta.glob("*.json")):
        if path.is_file() and not path.is_symlink():
            conda_records.append([path.name, digest_bytes(path.read_bytes())])
conda_bytes = json.dumps(conda_records, separators=(",", ":")).encode("utf-8")

print(
    json.dumps(
        {
            "implementation": sys.implementation.name,
            "python_version": sys.version,
            "python_abi": sys.implementation.cache_tag,
            "reported_executable": os.path.realpath(sys.executable),
            "prefix": str(prefix),
            "base_prefix": str(Path(sys.base_prefix).resolve()),
            "distribution_count": len(distributions),
            "distribution_metadata_sha256": digest_bytes(distribution_bytes),
            "conda_package_count": len(conda_records),
            "conda_metadata_sha256": digest_bytes(conda_bytes),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
)
'''


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_json_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        metadata_before = path.lstat()
    except OSError as exc:
        raise WorkerSlotError(f"required file is unavailable: {path}") from exc
    if not stat.S_ISREG(metadata_before.st_mode) or path.is_symlink():
        raise WorkerSlotError(f"required file must be a regular non-symlink: {path}")
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        metadata_after = path.lstat()
    except OSError as exc:
        raise WorkerSlotError(f"required file cannot be hashed: {path}") from exc
    stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(
        getattr(metadata_before, field) != getattr(metadata_after, field)
        for field in stable
    ):
        raise WorkerSlotError(f"required file changed while it was hashed: {path}")
    return "sha256:" + digest.hexdigest()


def validate_worker_base_identity(value: object) -> dict[str, Any]:
    """Validate the legacy-compatible frozen base-Python identity document."""

    if not isinstance(value, dict) or set(value) != WORKER_BASE_IDENTITY_FIELDS | {
        "identity_sha256"
    }:
        raise WorkerSlotError("Worker base Python identity record is missing or invalid")
    if value.get("schema_version") != 1:
        raise WorkerSlotError("Worker base Python identity schema is unsupported")
    for key in ("configured_path", "resolved_path", "prefix", "base_prefix"):
        item = value.get(key)
        if (
            not isinstance(item, str)
            or not Path(item).is_absolute()
            or ".." in Path(item).parts
        ):
            raise WorkerSlotError(
                f"Worker base Python identity contains an invalid {key}"
            )
    for key in (
        "executable_sha256",
        "distribution_metadata_sha256",
        "conda_metadata_sha256",
    ):
        if not isinstance(value.get(key), str) or DIGEST_RE.fullmatch(value[key]) is None:
            raise WorkerSlotError(
                f"Worker base Python identity contains an invalid {key}"
            )
    for key in ("implementation", "python_version", "python_abi"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise WorkerSlotError(
                f"Worker base Python identity contains an invalid {key}"
            )
    for key in ("executable_size", "distribution_count", "conda_package_count"):
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise WorkerSlotError(
                f"Worker base Python identity contains an invalid {key}"
            )
    material = {key: value[key] for key in WORKER_BASE_IDENTITY_FIELDS}
    if value.get("identity_sha256") != canonical_json_digest(material):
        raise WorkerSlotError(
            "Worker base Python identity fingerprint does not match its record"
        )
    return dict(value)


def inspect_base_python_identity(
    configured: Path,
    *,
    expected_identity: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Recompute the established frozen base-Python identity.

    Normal Conda ``bin/python`` symlinks are accepted, but their resolved
    executable and the complete distribution/Conda metadata fingerprint are
    sealed.  This is byte-for-byte compatible with the historical
    ``worker-base-identity`` command.
    """

    if not configured.is_absolute() or ".." in configured.parts:
        raise WorkerSlotError(
            "configured Worker base Python must be an absolute safe path"
        )
    try:
        configured_metadata = configured.lstat()
        resolved = configured.resolve(strict=True)
        before = resolved.stat()
    except OSError as exc:
        raise WorkerSlotError("configured Worker base Python cannot be resolved safely") from exc
    if not (
        stat.S_ISREG(configured_metadata.st_mode)
        or stat.S_ISLNK(configured_metadata.st_mode)
    ):
        raise WorkerSlotError(
            "configured Worker base Python must name a file or file symlink"
        )
    if not stat.S_ISREG(before.st_mode) or not os.access(resolved, os.X_OK):
        raise WorkerSlotError(
            "configured Worker base Python must resolve to an executable regular file"
        )
    if before.st_mode & 0o022:
        raise WorkerSlotError("frozen Worker base Python must not be group/world writable")

    executable_digest = sha256_file(resolved)
    clean_environment = dict(os.environ if environment is None else environment)
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        clean_environment.pop(key, None)
    clean_environment["PYTHONNOUSERSITE"] = "1"
    try:
        result = subprocess.run(
            [str(resolved), "-I", "-c", WORKER_BASE_IDENTITY_PROGRAM],
            cwd="/",
            env=clean_environment,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkerSlotError("Worker base Python identity cannot be recomputed") from exc
    try:
        runtime = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise WorkerSlotError(
            "Worker base Python did not return a valid identity document"
        ) from exc
    runtime_fields = {
        "implementation",
        "python_version",
        "python_abi",
        "reported_executable",
        "prefix",
        "base_prefix",
        "distribution_count",
        "distribution_metadata_sha256",
        "conda_package_count",
        "conda_metadata_sha256",
    }
    if not isinstance(runtime, dict) or set(runtime) != runtime_fields:
        raise WorkerSlotError(
            "Worker base Python returned an incomplete identity document"
        )
    if runtime.get("reported_executable") != str(resolved):
        raise WorkerSlotError(
            "Worker base Python reported a different executable identity"
        )
    try:
        after = resolved.stat()
        resolved_after = configured.resolve(strict=True)
    except OSError as exc:
        raise WorkerSlotError(
            "Worker base Python changed while it was fingerprinted"
        ) from exc
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if resolved_after != resolved or any(
        getattr(before, key) != getattr(after, key) for key in stable_fields
    ):
        raise WorkerSlotError("Worker base Python changed while it was fingerprinted")

    material = {
        "schema_version": 1,
        "configured_path": str(configured),
        "resolved_path": str(resolved),
        "executable_sha256": executable_digest,
        "executable_size": before.st_size,
        **{key: runtime[key] for key in runtime_fields if key != "reported_executable"},
    }
    identity = validate_worker_base_identity(
        {**material, "identity_sha256": canonical_json_digest(material)}
    )
    if expected_identity is not None:
        if DIGEST_RE.fullmatch(expected_identity) is None:
            raise WorkerSlotError("expected Worker base Python identity is invalid")
        if identity["identity_sha256"] != expected_identity:
            raise WorkerSlotError(
                "frozen Worker base Python identity differs from its READY record"
            )
    return identity


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkerSlotError("private JSON record contains a duplicate key")
        result[key] = value
    return result


def load_private_json(path: Path) -> dict[str, Any]:
    """Read one owner-only record without following the final path component."""

    if not path.is_absolute() or ".." in path.parts:
        raise WorkerSlotError("private JSON path must be absolute and safe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WorkerSlotError(f"private JSON record is missing or unsafe: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise WorkerSlotError(f"private JSON record must be regular: {path}")
        if before.st_uid != os.geteuid():
            raise WorkerSlotError(
                f"private JSON record must be owned by uid {os.geteuid()}: {path}"
            )
        if stat.S_IMODE(before.st_mode) != 0o600:
            raise WorkerSlotError(f"private JSON record must have mode 0600: {path}")
        if before.st_size > MAX_PRIVATE_JSON_BYTES:
            raise WorkerSlotError(f"private JSON record is oversized: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(MAX_PRIVATE_JSON_BYTES + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_PRIVATE_JSON_BYTES:
        raise WorkerSlotError(f"private JSON record is oversized: {path}")
    stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable):
        raise WorkerSlotError(f"private JSON record changed while it was read: {path}")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    except WorkerSlotError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerSlotError(f"private JSON record is invalid: {path}") from exc
    if not isinstance(value, dict):
        raise WorkerSlotError(f"private JSON record must contain an object: {path}")
    return value


def require_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise WorkerSlotError(f"private runtime directory is missing or unsafe: {path}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or resolved != path
    ):
        raise WorkerSlotError(f"private runtime directory must be owner-only mode 0700: {path}")


def _require_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise WorkerSlotError(f"{label} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise WorkerSlotError(f"{label} must be an RFC3339 UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise WorkerSlotError(f"{label} must be an RFC3339 UTC timestamp")
    return value


def _require_operation_id(value: object, label: str) -> str:
    if not isinstance(value, str) or OPERATION_ID_RE.fullmatch(value) is None:
        raise WorkerSlotError(f"{label} is invalid")
    return value


def _require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise WorkerSlotError(f"{label} must be a full lowercase Git SHA")
    return value


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise WorkerSlotError(f"{label} must be a sha256 digest")
    return value


def _require_absolute_path(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise WorkerSlotError(f"{label} must be an absolute safe path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise WorkerSlotError(f"{label} must be an absolute safe path")
    return value


def _require_slot(value: object) -> str:
    if not isinstance(value, str) or value not in SLOTS:
        raise WorkerSlotError("Worker slot must be exactly 'a' or 'b'")
    return value


def validate_active_record(value: object) -> ActiveSlotRecord:
    if not isinstance(value, dict) or set(value) != ACTIVE_RECORD_FIELDS:
        raise WorkerSlotError("active Worker slot record has an invalid shape")
    if (
        value.get("schema_version") != ACTIVE_RECORD_SCHEMA_VERSION
        or value.get("component") != COMPONENT
    ):
        raise WorkerSlotError("active Worker slot record has an unsupported identity")
    return ActiveSlotRecord(
        schema_version=ACTIVE_RECORD_SCHEMA_VERSION,
        component=COMPONENT,
        slot=_require_slot(value.get("slot")),
        source_sha=_require_sha(value.get("source_sha"), "active source SHA"),
        source_tree=_require_sha(value.get("source_tree"), "active source tree"),
        worker_lock_sha256=_require_digest(
            value.get("worker_lock_sha256"), "active Worker lock digest"
        ),
        slot_record_sha256=_require_digest(
            value.get("slot_record_sha256"), "active slot record digest"
        ),
        operation_id=_require_operation_id(value.get("operation_id"), "active operation ID"),
        activated_at=_require_timestamp(value.get("activated_at"), "activated_at"),
    )


def slot_root(runtime_root: Path, slot: str) -> Path:
    _require_slot(slot)
    return runtime_root / WORKER_VENV_DIRECTORY / f"md-{slot}"


def slot_venv_prefix(runtime_root: Path, slot: str) -> Path:
    return slot_root(runtime_root, slot) / "venv"


def slot_record_path(runtime_root: Path, slot: str) -> Path:
    _require_slot(slot)
    return runtime_root / SLOT_RECORD_DIRECTORY / f"md-{slot}.json"


def validate_slot_record(
    value: object,
    *,
    runtime_root: Path,
    expected_slot: str | None = None,
) -> SlotRecord:
    if not isinstance(value, dict) or set(value) != SLOT_RECORD_FIELDS:
        raise WorkerSlotError("Worker slot READY record has an invalid shape")
    if (
        value.get("schema_version") != SLOT_RECORD_SCHEMA_VERSION
        or value.get("component") != COMPONENT
        or value.get("status") != "ready"
    ):
        raise WorkerSlotError("Worker slot READY record has an unsupported identity")
    slot = _require_slot(value.get("slot"))
    if expected_slot is not None and slot != expected_slot:
        raise WorkerSlotError("Worker slot READY record names a different slot")
    expected_prefix = slot_venv_prefix(runtime_root, slot)
    if value.get("venv_prefix") != str(expected_prefix):
        raise WorkerSlotError("Worker slot READY record has an unexpected venv prefix")
    return SlotRecord(
        schema_version=SLOT_RECORD_SCHEMA_VERSION,
        component=COMPONENT,
        status="ready",
        slot=slot,
        source_sha=_require_sha(value.get("source_sha"), "slot source SHA"),
        source_tree=_require_sha(value.get("source_tree"), "slot source tree"),
        worker_lock_sha256=_require_digest(
            value.get("worker_lock_sha256"), "slot Worker lock digest"
        ),
        requirements_sha256=_require_digest(
            value.get("requirements_sha256"), "slot requirements digest"
        ),
        wheel_cache_key=_require_digest(
            value.get("wheel_cache_key"), "slot wheel cache key"
        ),
        wheel_inventory_sha256=_require_digest(
            value.get("wheel_inventory_sha256"), "slot wheel inventory digest"
        ),
        venv_prefix=str(expected_prefix),
        venv_inventory_sha256=_require_digest(
            value.get("venv_inventory_sha256"), "slot venv inventory digest"
        ),
        base_python_configured_path=_require_absolute_path(
            value.get("base_python_configured_path"),
            "slot configured base Python path",
        ),
        base_python_identity_sha256=_require_digest(
            value.get("base_python_identity_sha256"), "slot base Python identity"
        ),
        prepared_operation_id=_require_operation_id(
            value.get("prepared_operation_id"), "slot preparation operation ID"
        ),
        prepared_at=_require_timestamp(value.get("prepared_at"), "prepared_at"),
    )


def slot_record_document(record: SlotRecord) -> dict[str, Any]:
    return {field: getattr(record, field) for field in SLOT_RECORD_FIELDS}


def load_runtime_selection(
    runtime_root: Path = PRODUCTION_RUNTIME_ROOT,
) -> RuntimeSelection:
    if not runtime_root.is_absolute() or ".." in runtime_root.parts:
        raise WorkerSlotError("production runtime root must be absolute and safe")
    for directory in (
        runtime_root,
        runtime_root / "state",
        runtime_root / SLOT_RECORD_DIRECTORY,
        runtime_root / WORKER_VENV_DIRECTORY,
    ):
        require_private_directory(directory)
    active_path = runtime_root / ACTIVE_RECORD_RELATIVE_PATH
    active = validate_active_record(load_private_json(active_path))
    ready_path = slot_record_path(runtime_root, active.slot)
    ready_document = load_private_json(ready_path)
    if canonical_json_digest(ready_document) != active.slot_record_sha256:
        raise WorkerSlotError("active Worker slot record digest does not match READY record")
    ready = validate_slot_record(
        ready_document,
        runtime_root=runtime_root,
        expected_slot=active.slot,
    )
    for field in ("source_sha", "source_tree", "worker_lock_sha256"):
        if getattr(active, field) != getattr(ready, field):
            raise WorkerSlotError(f"active Worker selection disagrees with slot {field}")
    return RuntimeSelection(active, ready, active_path, ready_path)


def _run_git(source_root: Path, *arguments: str) -> str:
    environment = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    try:
        completed = subprocess.run(
            ["/usr/bin/git", "-C", str(source_root), *arguments],
            cwd="/",
            env=environment,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkerSlotError("production source Git identity cannot be verified") from exc
    return completed.stdout.strip()


def inspect_git_checkout(
    source_root: Path = PRODUCTION_SOURCE_ROOT,
) -> GitCheckoutIdentity:
    if not source_root.is_absolute() or ".." in source_root.parts:
        raise WorkerSlotError("production source root must be absolute and safe")
    try:
        root_metadata = source_root.lstat()
        resolved_root = source_root.resolve(strict=True)
    except OSError as exc:
        raise WorkerSlotError("production source root is missing or unsafe") from exc
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or source_root.is_symlink()
        or root_metadata.st_uid != os.geteuid()
        or root_metadata.st_mode & 0o022
        or resolved_root != source_root
    ):
        raise WorkerSlotError("production source root must be owner-controlled and non-symlink")

    git_directory = source_root / ".git"
    try:
        git_metadata = git_directory.lstat()
    except OSError as exc:
        raise WorkerSlotError("production source .git directory is missing") from exc
    if (
        not stat.S_ISDIR(git_metadata.st_mode)
        or git_directory.is_symlink()
        or git_metadata.st_uid != os.geteuid()
        or git_metadata.st_mode & 0o022
    ):
        raise WorkerSlotError("production source .git directory is unsafe")

    if _run_git(source_root, "rev-parse", "--show-toplevel") != str(source_root):
        raise WorkerSlotError("production source Git top-level differs from the fixed root")
    source_sha = _run_git(source_root, "rev-parse", "--verify", "HEAD")
    source_tree = _run_git(source_root, "rev-parse", "--verify", "HEAD^{tree}")
    if SHA_RE.fullmatch(source_sha) is None or SHA_RE.fullmatch(source_tree) is None:
        raise WorkerSlotError("production source returned an invalid Git identity")
    status_output = _run_git(
        source_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    if status_output:
        raise WorkerSlotError("production source checkout is not clean")
    if (
        _run_git(source_root, "rev-parse", "--verify", "HEAD") != source_sha
        or _run_git(source_root, "rev-parse", "--verify", "HEAD^{tree}") != source_tree
    ):
        raise WorkerSlotError("production source changed while its identity was verified")
    return GitCheckoutIdentity(source_root, source_sha, source_tree)


def _inventory_failure(message: str) -> NoReturn:
    raise WorkerSlotError(message)


def directory_inventory_digest(root: Path) -> str:
    """Hash an owner-controlled tree without following symlinks."""

    try:
        root_metadata = root.lstat()
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise WorkerSlotError(f"inventory root is missing or unsafe: {root}") from exc
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root.is_symlink()
        or root_metadata.st_uid != os.geteuid()
        or root_metadata.st_mode & 0o022
        or resolved_root != root
    ):
        raise WorkerSlotError(f"inventory root is unsafe: {root}")

    records: list[dict[str, Any]] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in sorted(directory_names + file_names):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise WorkerSlotError("inventory entry changed while scanning") from exc
            if metadata.st_uid != os.geteuid() or (
                not stat.S_ISLNK(metadata.st_mode) and metadata.st_mode & 0o022
            ):
                _inventory_failure(f"inventory entry is not owner-controlled: {relative}")
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISDIR(metadata.st_mode):
                records.append({"path": relative, "type": "directory", "mode": mode})
            elif stat.S_ISREG(metadata.st_mode):
                records.append(
                    {
                        "path": relative,
                        "type": "file",
                        "mode": mode,
                        "size": metadata.st_size,
                        "sha256": sha256_file(path),
                    }
                )
            elif stat.S_ISLNK(metadata.st_mode):
                try:
                    target = os.readlink(path)
                except OSError as exc:
                    raise WorkerSlotError("inventory symlink changed while scanning") from exc
                records.append(
                    {"path": relative, "type": "symlink", "mode": mode, "target": target}
                )
                if name in directory_names:
                    directory_names.remove(name)
            else:
                _inventory_failure(f"inventory contains an unsupported entry: {relative}")
    records.sort(key=lambda item: item["path"])
    return canonical_json_digest(records)


def _load_pyvenv_configuration(path: Path) -> dict[str, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WorkerSlotError("selected Worker venv configuration is missing or unsafe") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_mode & 0o022
            or before.st_size > MAX_PRIVATE_JSON_BYTES
        ):
            raise WorkerSlotError(
                "selected Worker venv configuration is missing or unsafe"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(MAX_PRIVATE_JSON_BYTES + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if len(raw) > MAX_PRIVATE_JSON_BYTES or any(
        getattr(before, key) != getattr(after, key) for key in stable_fields
    ):
        raise WorkerSlotError("selected Worker venv configuration changed while read")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkerSlotError("selected Worker venv configuration is not UTF-8") from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        if "=" not in line:
            raise WorkerSlotError("selected Worker venv configuration is malformed")
        key, value = (item.strip() for item in line.split("=", 1))
        normalized = key.lower()
        if not normalized or normalized in values or not value:
            raise WorkerSlotError("selected Worker venv configuration is malformed")
        values[normalized] = value
    return values


def validate_selected_venv(
    selection: RuntimeSelection,
    runtime_root: Path = PRODUCTION_RUNTIME_ROOT,
) -> Path:
    expected_prefix = slot_venv_prefix(runtime_root, selection.active.slot)
    require_private_directory(slot_root(runtime_root, selection.active.slot))
    if selection.slot.venv_prefix != str(expected_prefix):
        raise WorkerSlotError("selected Worker venv prefix differs from its slot")
    try:
        prefix_metadata = expected_prefix.lstat()
        resolved_prefix = expected_prefix.resolve(strict=True)
    except OSError as exc:
        raise WorkerSlotError("selected Worker venv is missing or unsafe") from exc
    if (
        not stat.S_ISDIR(prefix_metadata.st_mode)
        or expected_prefix.is_symlink()
        or prefix_metadata.st_uid != os.geteuid()
        or prefix_metadata.st_mode & 0o022
        or resolved_prefix != expected_prefix
    ):
        raise WorkerSlotError("selected Worker venv is unsafe")
    python = expected_prefix / "bin/python"
    try:
        python_metadata = python.lstat()
        resolved_python = python.resolve(strict=True)
    except OSError as exc:
        raise WorkerSlotError("selected Worker venv Python is missing or unsafe") from exc
    if (
        not (stat.S_ISREG(python_metadata.st_mode) or stat.S_ISLNK(python_metadata.st_mode))
        or not resolved_python.is_file()
        or not os.access(python, os.X_OK)
    ):
        raise WorkerSlotError("selected Worker venv Python is missing or unsafe")
    configuration = expected_prefix / "pyvenv.cfg"
    pyvenv = _load_pyvenv_configuration(configuration)
    if directory_inventory_digest(expected_prefix) != selection.slot.venv_inventory_sha256:
        raise WorkerSlotError("selected Worker venv inventory differs from its READY record")
    configured_base = Path(selection.slot.base_python_configured_path)
    pyvenv_executable = Path(pyvenv.get("executable", ""))
    if not pyvenv_executable.is_absolute() or ".." in pyvenv_executable.parts:
        raise WorkerSlotError(
            "selected Worker venv does not identify its absolute base executable"
        )
    try:
        resolved_base = configured_base.resolve(strict=True)
        resolved_pyvenv_base = pyvenv_executable.resolve(strict=True)
    except OSError as exc:
        raise WorkerSlotError("selected Worker venv base executable is unavailable") from exc
    if resolved_python != resolved_base or resolved_pyvenv_base != resolved_base:
        raise WorkerSlotError(
            "selected Worker venv base executable differs from its READY record"
        )
    inspect_base_python_identity(
        configured_base,
        expected_identity=selection.slot.base_python_identity_sha256,
    )
    return python


def verify_runtime_binding(
    *,
    source_root: Path = PRODUCTION_SOURCE_ROOT,
    runtime_root: Path = PRODUCTION_RUNTIME_ROOT,
) -> tuple[GitCheckoutIdentity, RuntimeSelection, Path]:
    checkout = inspect_git_checkout(source_root)
    selection = load_runtime_selection(runtime_root)
    if selection.active.source_sha != checkout.source_sha:
        raise WorkerSlotError("active Worker source SHA differs from the live checkout")
    if selection.active.source_tree != checkout.source_tree:
        raise WorkerSlotError("active Worker source tree differs from the live checkout")
    lock_digest = sha256_file(source_root / WORKER_LOCK_RELATIVE_PATH)
    if lock_digest != selection.active.worker_lock_sha256:
        raise WorkerSlotError("active Worker lock digest differs from the live checkout")
    python = validate_selected_venv(selection, runtime_root)
    return checkout, selection, python
