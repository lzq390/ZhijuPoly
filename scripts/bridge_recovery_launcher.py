#!/usr/bin/python3 -I -B
"""Validate and enter one content-addressed B bridge recovery capsule."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
CAPSULES_ROOT = (
    RUNTIME_ROOT / "legacy-takeover/runtime/bridge-recovery-capsules"
)
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
OPERATION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")


class LauncherError(RuntimeError):
    """The requested B capsule is not safe to execute."""


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise LauncherError(f"private directory is unavailable: {path}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise LauncherError(f"private directory is unsafe: {path}")


def private_json(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
        payload = path.read_bytes()
    except OSError as exc:
        raise LauncherError("capsule metadata is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or len(payload) > 1024 * 1024
    ):
        raise LauncherError("capsule metadata is unsafe")
    try:
        document = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LauncherError("capsule metadata is invalid") from exc
    if not isinstance(document, dict):
        raise LauncherError("capsule metadata is not an object")
    return document


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--capsule-sha256", required=True)
    value.add_argument("--authority-sha", required=True)
    value.add_argument("--target-sha", required=True)
    value.add_argument("--operation-id", required=True)
    value.add_argument("--descriptor-sha256", required=True)
    value.add_argument("--restored-terminal-sha256", required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if (
        DIGEST_RE.fullmatch(arguments.capsule_sha256) is None
        or DIGEST_RE.fullmatch(arguments.descriptor_sha256) is None
        or DIGEST_RE.fullmatch(arguments.restored_terminal_sha256) is None
        or SHA_RE.fullmatch(arguments.authority_sha) is None
        or SHA_RE.fullmatch(arguments.target_sha) is None
        or OPERATION_RE.fullmatch(arguments.operation_id) is None
    ):
        raise LauncherError("bridge recovery content address is invalid")
    for directory in (
        RUNTIME_ROOT,
        RUNTIME_ROOT / "legacy-takeover",
        RUNTIME_ROOT / "legacy-takeover/runtime",
        CAPSULES_ROOT,
    ):
        private_directory(directory)
    root = CAPSULES_ROOT / arguments.capsule_sha256.removeprefix("sha256:")
    private_directory(root)
    metadata = private_json(root / "capsule.json")
    identity = {
        key: value
        for key, value in metadata.items()
        if key != "capsule_sha256"
    }
    files = metadata.get("files")
    if (
        metadata.get("schema_version") != 1
        or metadata.get("capsule_sha256") != arguments.capsule_sha256
        or sha256_bytes(canonical_json_bytes(identity))
        != arguments.capsule_sha256
        or metadata.get("operation_id") != arguments.operation_id
        or metadata.get("authority_sha") != arguments.authority_sha
        or metadata.get("target_sha") != arguments.target_sha
        or metadata.get("descriptor_sha256")
        != arguments.descriptor_sha256
        or not isinstance(files, dict)
        or not isinstance(files.get("bridge_recovery_capsule.py"), dict)
    ):
        raise LauncherError("bridge recovery capsule identity differs")
    control = root / "control"
    private_directory(control)
    entry = control / "bridge_recovery_capsule.py"
    try:
        entry_metadata = entry.lstat()
    except OSError as exc:
        raise LauncherError("B recovery entry is unavailable") from exc
    entry_record = files["bridge_recovery_capsule.py"]
    if (
        set(entry_record) != {"sha256", "mode"}
        or entry_record.get("mode") != "0700"
        or DIGEST_RE.fullmatch(str(entry_record.get("sha256"))) is None
        or not stat.S_ISREG(entry_metadata.st_mode)
        or entry.is_symlink()
        or entry_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(entry_metadata.st_mode) != 0o700
        or sha256_file(entry) != entry_record["sha256"]
    ):
        raise LauncherError("B recovery entry changed")
    environment = {
        "HOME": "/home/devuser",
        "USER": "devuser",
        "LOGNAME": "devuser",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    os.execve(
        "/usr/bin/python3",
        [
            "/usr/bin/python3",
            "-I",
            "-B",
            str(entry),
            *(argv if argv is not None else sys.argv[1:]),
        ],
        environment,
    )
    raise AssertionError("execve returned unexpectedly")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LauncherError, OSError) as exc:
        print(f"bridge-recover-launcher: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
