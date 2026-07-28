#!/usr/bin/env python3
"""Fail-closed startup validation for the production DFT A slot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any


PRODUCTION_REPO_ROOT = Path("/data/lzq/gith/nexpoly")
PRODUCTION_RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
SLOT_RELATIVE = Path("worker-venvs/dft-a")
GPU_UUID = "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe"


class PreflightError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def private_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or path.resolve(strict=True) != path
    ):
        raise PreflightError(f"unsafe owner-private directory: {path}")


def private_file(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise PreflightError(f"unsafe owner-private file: {path}")


def run(*command: str) -> str:
    try:
        return subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PreflightError(f"startup command failed: {command[0]}") from exc


def validate(
    *,
    repo_root: Path = PRODUCTION_REPO_ROOT,
    runtime_root: Path = PRODUCTION_RUNTIME_ROOT,
) -> dict[str, Any]:
    if repo_root.resolve(strict=True) != repo_root:
        raise PreflightError("production source root is not canonical")
    private_directory(runtime_root)
    head = run("git", "-C", os.fspath(repo_root), "rev-parse", "HEAD")
    if run(
        "git",
        "-C",
        os.fspath(repo_root),
        "status",
        "--porcelain=v1",
        "--ignored",
    ):
        raise PreflightError("production source is not clean or contains ignored paths")

    slot = runtime_root / SLOT_RELATIVE
    private_directory(slot)
    slot_manifest = slot / "slot.json"
    private_file(slot_manifest)
    try:
        slot_payload = json.loads(slot_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError("DFT slot manifest is invalid") from exc
    if slot_payload.get("release") != head:
        raise PreflightError("DFT slot release differs from production source")
    if slot_payload.get("python") != "3.12" or slot_payload.get("uv") != "0.11.21":
        raise PreflightError("DFT slot toolchain differs from the locked release")

    python = slot / "venv/bin/python"
    if not python.exists() or not os.access(python, os.X_OK):
        raise PreflightError("DFT slot Python is unavailable")
    if run(os.fspath(python), "-I", "-c", "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')") != "3.12":
        raise PreflightError("DFT slot Python is not 3.12")

    lock_path = repo_root / "workers/monomer_dft_worker/aimnet-source.lock.json"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError("AIMNet source lock is invalid") from exc
    models = lock.get("models")
    if not isinstance(models, list) or len(models) != 6:
        raise PreflightError("AIMNet source lock must contain six models")
    model_root = slot / "aimnet-cache"
    private_directory(model_root)
    expected_files: set[str] = set()
    model_digests: dict[str, str] = {}
    for model in models:
        if not isinstance(model, dict):
            raise PreflightError("AIMNet model lock entry is invalid")
        filename = model.get("file")
        expected = model.get("sha256")
        alias = model.get("alias")
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not isinstance(expected, str)
            or len(expected) != 64
            or not isinstance(alias, str)
        ):
            raise PreflightError("AIMNet model lock metadata is invalid")
        path = model_root / filename
        private_file(path)
        actual = sha256_file(path)
        if actual != expected:
            raise PreflightError(f"AIMNet model SHA mismatch: {alias}")
        expected_files.add(filename)
        model_digests[alias] = actual
    actual_files = {path.name for path in model_root.iterdir()}
    if actual_files != expected_files:
        raise PreflightError("AIMNet production cache inventory differs from the lock")

    for relative in (
        "state/monomer-dft-worker-socket",
        "state/monomer-dft-worker-runs",
        "state/monomer-dft-download-spool",
    ):
        private_directory(runtime_root / relative)
    observed_uuid = run(
        "nvidia-smi",
        "--query-gpu=uuid",
        "--format=csv,noheader",
        "-i",
        "2",
    )
    if observed_uuid != GPU_UUID:
        raise PreflightError("physical GPU2 UUID differs from production policy")
    return {
        "status": "ready",
        "release": head,
        "gpu_uuid": observed_uuid,
        "models": model_digests,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    print(json.dumps(validate(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
