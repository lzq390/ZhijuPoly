"""Durable exclusion for native processes outside the business job journal.

Callers hold the Worker RootLock while claiming, releasing or checking this
fence. A marker outlives both the CLI and its cleanup guardian. A Worker may
remove it only after the exact bound journal confirms full process cleanup.
This module and its journal dependency use only the standard library.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat

from .journal import read_json

MARKER = ".external-execution.json"
PURPOSE = "polymer_md_external_execution_fence"


def _path(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeError("external execution paths must be absolute and contained")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise RuntimeError("external execution paths must not contain symbolic links")
    return path


def _private(path: Path, *, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("external execution metadata must be private and owned")
    return info


def _root(job_root: Path) -> Path:
    root = _path(job_root)
    _private(root, directory=True)
    return root


def _sync(root: Path):
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_marker(root: Path):
    path = root / MARKER
    if not path.exists() and not path.is_symlink():
        return None
    info = _private(path)
    try:
        value = read_json(path)
        if (not isinstance(value, dict) or value.get("version") != 1 or value.get("purpose") != PURPOSE
                or value.get("job_root") != str(root)):
            raise ValueError("external marker identity differs")
    except (ValueError, TypeError) as error:
        raise RuntimeError("external execution marker is invalid; explicit recovery is required") from error
    return value, info


def _journal(root: Path, marker: dict):
    try:
        evidence = _path(Path(marker["evidence_dir"]))
        runtime = _path(Path(marker["runtime_root"]))
        _private(evidence, directory=True)
        if not evidence.is_relative_to(runtime) or evidence == runtime:
            raise ValueError("external evidence is outside its runtime")
        path = evidence / "journal.json"
        _private(path)
        value = read_json(path)
        identity = value.get("scope", {})
        if (value.get("job_root") != str(root) or value.get("runtime_root") != str(runtime)
                or identity.get("nonce") != marker["nonce"] or identity.get("unit") != marker["unit"]
                or not isinstance(marker["nonce"], str) or not marker["nonce"]
                or not isinstance(marker["unit"], str) or not marker["unit"].endswith(".scope")):
            raise ValueError("external journal identity differs")
        return value
    except (KeyError, TypeError, ValueError, FileNotFoundError) as error:
        raise RuntimeError("external execution journal is invalid; explicit recovery is required") from error


def _remove(root: Path, info):
    path = root / MARKER
    current = path.lstat()
    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
        raise RuntimeError("external execution marker changed during verification")
    path.unlink()
    _sync(root)


def assert_no_external_execution(job_root: Path) -> None:
    """Reject admission even if no original CLI, guardian or lock survives."""
    root = _root(job_root)
    found = _read_marker(root)
    if found is None:
        return
    marker, info = found
    fact = _journal(root, marker)
    if fact.get("cleanup_confirmed") is not True:
        raise RuntimeError("external native execution cleanup is unconfirmed; recover its exact evidence directory")
    _remove(root, info)


def claim_execution(job_root: Path, evidence: Path, runtime: Path, nonce: str, unit: str) -> dict:
    """Persist after the journal, before any guardian or scientific spawn."""
    root = _root(job_root)
    marker = {"version": 1, "purpose": PURPOSE, "job_root": str(root),
              "evidence_dir": str(_path(evidence)), "runtime_root": str(_path(runtime)),
              "nonce": nonce, "unit": unit, "claimed_at": datetime.now(timezone.utc).isoformat()}
    fact = _journal(root, marker)
    if fact.get("cleanup_confirmed") is not False:
        raise RuntimeError("external execution claim requires an uncompleted cleanup journal")
    found = _read_marker(root)
    if found is not None:
        existing, _ = found
        if all(existing.get(key) == marker[key] for key in marker if key != "claimed_at"):
            return existing
        assert_no_external_execution(root)
    path = root / MARKER
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        # A torn claim remains an invalid marker and fails closed on restart.
        with os.fdopen(descriptor, "w") as handle:
            json.dump(marker, handle, sort_keys=True, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        _sync(root)
    except BaseException:
        # Never erase an incomplete marker while launch ownership is uncertain.
        raise
    return marker


def release_execution(job_root: Path, evidence: Path, nonce: str, unit: str) -> bool:
    """Release only the same durable execution, after its cleanup is confirmed."""
    root = _root(job_root)
    found = _read_marker(root)
    if found is None:
        return False
    marker, info = found
    if (marker.get("evidence_dir") != str(_path(evidence))
            or marker.get("nonce") != nonce or marker.get("unit") != unit):
        raise RuntimeError("cannot release a different external execution")
    if _journal(root, marker).get("cleanup_confirmed") is not True:
        raise RuntimeError("cannot release external execution before confirmed process cleanup")
    _remove(root, info)
    return True
