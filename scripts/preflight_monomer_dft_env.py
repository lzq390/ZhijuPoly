#!/usr/bin/env python3
"""Fail-closed validation for the isolated monomer DFT worker runtime."""

from __future__ import annotations

import hashlib
import importlib.metadata
import base64
import csv
import io
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import urllib.parse
import zipfile
from typing import Any


EXPECTED_GPU_NAME = "NVIDIA GeForce RTX 4090"
EXPECTED_GPU_UUIDS = {
    "1": "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
    "3": "GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5",
}
PRODUCTION_REPO_ROOT = pathlib.Path("/data/lzq/gith/nexpoly")
EXPECTED_CUDA_RUNTIME = "12.8"
EXPECTED_UV_VERSION = "0.11.21"
EXPECTED_DIRECT_VERSIONS = {
    "torch": "2.9.1+cu128",
    "numpy": "2.4.4",
    "warp-lang": "1.11.0",
    "nvalchemi-toolkit-ops": "0.3.1",
    "requests": "2.33.1",
    "click": "8.3.1",
    "pyyaml": "6.0.3",
    "jinja2": "3.1.6",
    "h5py": "3.15.1",
    "ase": "3.27.0",
    "rdkit": "2026.3.3",
    "fastapi": "0.115.0",
    "uvicorn": "0.32.0",
}
REQUIRED_ENV_KEYS = {
    "MONOMER_DFT_PYTHON",
    "MONOMER_DFT_WORKER_UDS",
    "MONOMER_DFT_JOB_ROOT",
    "MONOMER_DFT_MAX_CONCURRENT_JOBS",
    "NEXPOLY_DFT_GPU_DEVICE",
    "NEXPOLY_DFT_OVERFLOW_GPU_DEVICES",
    "MONOMER_DFT_DEPLOYMENT",
    "MONOMER_DFT_GPU_BUDGET_MIB",
    "MONOMER_DFT_GPU_ACTIVE_THREAD_PERCENTAGE",
    "MONOMER_DFT_GPU_BROKER_ENABLED",
    "MONOMER_DFT_STANDALONE_GPU_SMOKE",
    "MONOMER_DFT_GPU_BROKER_UDS",
    "MONOMER_DFT_GPU_MPS_PIPE_ROOT",
    "MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS",
    "MONOMER_DFT_DOWNLOAD_SPOOL_ROOT",
    "AIMNET_CACHE_DIR",
    "WARP_CACHE_PATH",
    "UV_CACHE_DIR",
    "AIMNET_SOURCE_DIR",
    "AIMNET_MODEL_SOURCE_DIR",
    "AIMNET_SOURCE_LOCK",
    "PYTHONNOUSERSITE",
    "PYTHONDONTWRITEBYTECODE",
    "CUDA_DEVICE_ORDER",
}


class PreflightError(RuntimeError):
    """A preflight invariant was not satisfied."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PreflightError(message)


def effective_environment(
    repo_root: pathlib.Path,
    values: dict[str, str],
) -> tuple[dict[str, str], Any | None]:
    """Overlay only descriptor-bound GPU paths for formal acceptance."""

    authority_enabled = (
        os.environ.get("NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY") == "1"
    )
    authority_names_present = any(
        os.environ.get(name, "")
        for name in (
            "NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY",
            "NEXPOLY_DFT_GPU_AUTHORITY_PID",
            "NEXPOLY_DFT_GPU_AUTHORITY_START_TICKS",
            "NEXPOLY_DFT_GPU_AUTHORITY_ROOT",
            "NEXPOLY_DFT_GPU_AUTHORITY_ROOT_IDENTITY",
            "NEXPOLY_DFT_GPU_RESERVATIONS_AUTHORITY",
            "NEXPOLY_DFT_GPU_RESERVATIONS_IDENTITY",
            "NEXPOLY_DFT_GPU_RESERVATIONS_SHA256",
            "NEXPOLY_DFT_GPU1_MPS_PIPE_AUTHORITY",
            "NEXPOLY_DFT_GPU1_MPS_PIPE_IDENTITY",
            "NEXPOLY_DFT_GPU3_MPS_PIPE_AUTHORITY",
            "NEXPOLY_DFT_GPU3_MPS_PIPE_IDENTITY",
        )
    )
    if not authority_enabled:
        require(
            not authority_names_present,
            "partial GPU descriptor authority is forbidden",
        )
        return dict(values), None
    try:
        sys.path.insert(0, str(repo_root))
        from gpu_resource.authority import (  # noqa: PLC0415
            FormalGpuAuthorityError,
            load_formal_gpu_authority,
        )

        authority = load_formal_gpu_authority(
            expected_reservations_file=(
                repo_root / "ops/config/gpu-external-reservations.json"
            ),
            expected_root=repo_root / ".runtime/gpu-resource",
            require=True,
        )
    except (ImportError, FormalGpuAuthorityError, OSError) as exc:
        raise PreflightError(
            f"formal GPU descriptor authority is invalid: {exc}"
        ) from exc
    finally:
        if sys.path and sys.path[0] == str(repo_root):
            del sys.path[0]
    assert authority is not None
    effective = dict(values)
    effective.update(
        {
            "MONOMER_DFT_GPU_BROKER_UDS": str(
                authority.root / "broker.sock"
            ),
            "MONOMER_DFT_GPU_MPS_PIPE_ROOT": str(authority.root),
            "MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS": str(
                authority.reservations
            ),
        }
    )
    return effective, authority


def run(*args: str) -> str:
    completed = subprocess.run(
        args,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise PreflightError(f"command failed ({' '.join(args)}): {detail}")
    return completed.stdout.strip()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def directory_digest(root: pathlib.Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        root.rglob("*"),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            kind = b"d"
            content = b""
        elif stat.S_ISREG(metadata.st_mode):
            kind = b"f"
            content = path.read_bytes()
        else:
            raise PreflightError(f"unsafe clean-archive entry: {relative}")
        digest.update(kind)
        digest.update(b"\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{stat.S_IMODE(metadata.st_mode):04o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()


def load_env_file(path: pathlib.Path) -> dict[str, str]:
    require(path.is_file() and not path.is_symlink(), f"environment file must be a regular non-symlink: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    require(mode == 0o600, f"environment file mode must be 0600, found {mode:04o}")

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        require("=" in line, f"invalid environment line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        require(key and key.replace("_", "").isalnum(), f"invalid environment key on line {line_number}")
        require(key not in values, f"duplicate environment key: {key}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value

    missing = sorted(REQUIRED_ENV_KEYS - values.keys())
    require(not missing, f"missing environment keys: {', '.join(missing)}")
    require(not values.get("PYTHONPATH"), "PYTHONPATH must be empty")
    return values


def resolve_path(repo_root: pathlib.Path, value: str) -> pathlib.Path:
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve(strict=False)


def lexical_path(repo_root: pathlib.Path, value: str) -> pathlib.Path:
    """Normalize a configured path without following its final symlink."""
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return pathlib.Path(os.path.abspath(os.path.normpath(path)))


def require_development_repo_root(repo_root: pathlib.Path) -> pathlib.Path:
    normalized = pathlib.Path(os.path.abspath(os.path.normpath(repo_root)))
    require(
        normalized != PRODUCTION_REPO_ROOT,
        "development DFT preflight is forbidden in the production repository",
    )
    return normalized


def require_not_production_path(path: pathlib.Path, label: str) -> None:
    require(
        path != PRODUCTION_REPO_ROOT
        and not path.is_relative_to(PRODUCTION_REPO_ROOT),
        f"{label} must not reference the production repository",
    )


def canonical_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def validate_complete_lock(repo_root: pathlib.Path, venv: pathlib.Path) -> dict[str, str]:
    lock_path = repo_root / "workers/monomer_dft_worker/requirements.lock"
    require(lock_path.is_file(), f"dependency lock is missing: {lock_path}")
    pattern = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)")
    locked: dict[str, str] = {}
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            name = canonical_distribution_name(match.group(1))
            require(name not in locked, f"duplicate package in dependency lock: {name}")
            locked[name] = match.group(2)
    require(locked, "dependency lock contains no packages")

    installed: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        location = pathlib.Path(distribution.locate_file("")).resolve()
        if not location.is_relative_to(venv):
            continue
        name = canonical_distribution_name(distribution.metadata["Name"])
        installed[name] = distribution.version

    expected_names = set(locked) | {"aimnet"}
    require(set(installed) == expected_names, f"installed/locked package set differs: expected {sorted(expected_names)}, found {sorted(installed)}")
    for name, expected in locked.items():
        require(installed[name] == expected, f"locked version mismatch for {name}: expected {expected}, found {installed[name]}")
    return locked


def validate_aimnet_wheel_record(repo_root: pathlib.Path, distribution: importlib.metadata.Distribution) -> dict[str, str]:
    direct_url_text = distribution.read_text("direct_url.json")
    require(direct_url_text is not None, "AIMNet wheel provenance is missing")
    direct_url = json.loads(direct_url_text)
    require(not direct_url.get("dir_info", {}).get("editable", False), "editable AIMNet installation is forbidden")
    require(str(direct_url.get("url", "")).endswith(".whl"), "AIMNet was not installed from a wheel")

    wheel_path = pathlib.Path(urllib.parse.unquote(urllib.parse.urlparse(direct_url["url"]).path)).resolve()
    wheelhouse = (repo_root / ".runtime/wheelhouse").resolve()
    require(wheel_path.is_relative_to(wheelhouse) and wheel_path.is_file(), "AIMNet wheel is outside the isolated wheelhouse")
    wheel_sha = sha256_file(wheel_path)
    sha_record = wheelhouse / "aimnet-wheel.sha256"
    fields = sha_record.read_text(encoding="utf-8").split()
    require(len(fields) == 2 and fields[1] == wheel_path.name, "invalid AIMNet wheel checksum record")
    require(fields[0] == wheel_sha, "AIMNet wheel checksum record mismatch")
    manifest_path = wheelhouse / "aimnet-wheel-manifest.json"
    require(
        manifest_path.is_file() and not manifest_path.is_symlink(),
        "AIMNet wheel manifest is missing or unsafe",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_lock = json.loads(
        (
            repo_root
            / "workers/monomer_dft_worker/aimnet-source.lock.json"
        ).read_text(encoding="utf-8")
    )
    source = source_lock["source"]
    wheel_lock = source_lock["wheel"]
    require(manifest.get("schema_version") == 1, "unsupported AIMNet wheel manifest")
    require(manifest.get("source_commit") == source.get("commit"), "wheel source commit mismatch")
    require(manifest.get("source_tree") == source.get("tree"), "wheel source tree mismatch")
    require(
        manifest.get("source_date_epoch") == source.get("source_date_epoch"),
        "wheel SOURCE_DATE_EPOCH mismatch",
    )
    require(manifest.get("wheel_file") == wheel_path.name, "wheel manifest filename mismatch")
    require(manifest.get("wheel_sha256") == wheel_sha, "wheel manifest checksum mismatch")
    require(wheel_lock.get("filename") == wheel_path.name, "wheel filename lock mismatch")
    require(wheel_lock.get("sha256") == wheel_sha, "wheel digest lock mismatch")
    with zipfile.ZipFile(wheel_path) as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        actual_files = [
            {
                "path": info.filename,
                "size": info.file_size,
                "sha256": hashlib.sha256(archive.read(info.filename)).hexdigest(),
            }
            for info in sorted(infos, key=lambda item: item.filename)
        ]
        record_names = [
            info.filename
            for info in infos
            if info.filename.endswith(".dist-info/RECORD")
        ]
        require(len(record_names) == 1, "AIMNet wheel must contain one RECORD")
        record_bytes = archive.read(record_names[0])
    inventory_sha256 = hashlib.sha256(
        json.dumps(actual_files, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    require(manifest.get("files") == actual_files, "wheel file inventory mismatch")
    require(manifest.get("file_count") == len(actual_files), "wheel file count mismatch")
    require(
        manifest.get("inventory_sha256") == inventory_sha256,
        "wheel manifest inventory checksum mismatch",
    )
    require(wheel_lock.get("file_count") == len(actual_files), "wheel file-count lock mismatch")
    require(
        wheel_lock.get("inventory_sha256") == inventory_sha256,
        "wheel inventory lock mismatch",
    )
    require(manifest.get("record_path") == record_names[0], "wheel RECORD path mismatch")
    require(
        manifest.get("record_sha256") == hashlib.sha256(record_bytes).hexdigest(),
        "wheel RECORD checksum mismatch",
    )
    require(wheel_lock.get("record_path") == record_names[0], "wheel RECORD path lock mismatch")
    require(
        wheel_lock.get("record_sha256") == hashlib.sha256(record_bytes).hexdigest(),
        "wheel RECORD checksum lock mismatch",
    )
    archive_hash = direct_url.get("archive_info", {}).get("hashes", {}).get("sha256")
    if archive_hash is None:
        legacy_hash = direct_url.get("archive_info", {}).get("hash", "")
        archive_hash = legacy_hash.removeprefix("sha256=") if legacy_hash.startswith("sha256=") else None
    require(archive_hash in (None, wheel_sha), "installed AIMNet archive provenance checksum mismatch")

    record_text = distribution.read_text("RECORD")
    require(record_text is not None, "installed AIMNet RECORD is missing")
    checked_files = 0
    for relative_name, hash_spec, _size in csv.reader(io.StringIO(record_text)):
        if not hash_spec:
            continue
        algorithm, encoded_digest = hash_spec.split("=", 1)
        require(algorithm == "sha256", f"unsupported AIMNet RECORD algorithm: {algorithm}")
        installed_file = pathlib.Path(distribution.locate_file(relative_name)).resolve()
        require(installed_file.is_file(), f"AIMNet RECORD file is missing: {relative_name}")
        actual = base64.urlsafe_b64encode(hashlib.sha256(installed_file.read_bytes()).digest()).rstrip(b"=").decode()
        require(actual == encoded_digest, f"AIMNet RECORD checksum mismatch: {relative_name}")
        checked_files += 1
    require(checked_files > 0, "AIMNet RECORD contains no verifiable files")
    return {"wheel": str(wheel_path), "wheel_sha256": wheel_sha, "record_files_verified": str(checked_files)}


def validate_environment(
    repo_root: pathlib.Path,
    values: dict[str, str],
    formal_gpu_authority: Any | None = None,
) -> dict[str, str]:
    repo_root = require_development_repo_root(repo_root)
    deployment = values["MONOMER_DFT_DEPLOYMENT"]
    require(
        deployment == "dev",
        "MONOMER_DFT_DEPLOYMENT must be exactly dev; production mode is forbidden",
    )
    runtime_path = repo_root / ".runtime"
    require(
        runtime_path.is_dir() and not runtime_path.is_symlink(),
        f"development runtime must be a real directory: {runtime_path}",
    )
    runtime = runtime_path.resolve(strict=True)
    require(runtime == runtime_path, f"development runtime resolved unexpectedly: {runtime}")
    expected_paths = {
        "MONOMER_DFT_PYTHON": runtime / "venvs/monomer-dft-worker/bin/python",
        "MONOMER_DFT_WORKER_UDS": runtime / "monomer-dft-worker-socket/worker.sock",
        "MONOMER_DFT_JOB_ROOT": runtime / "monomer-dft-worker-runs",
        "AIMNET_CACHE_DIR": runtime / "aimnet-cache",
        "WARP_CACHE_PATH": runtime / "warp-cache",
        "UV_CACHE_DIR": runtime / "uv-cache",
        "AIMNET_SOURCE_DIR": runtime / "aimnet-source-archive",
        "AIMNET_SOURCE_LOCK": repo_root / "workers/monomer_dft_worker/aimnet-source.lock.json",
    }
    resolved: dict[str, str] = {}
    for key, expected in expected_paths.items():
        actual_lexical = lexical_path(repo_root, values[key])
        expected_lexical = lexical_path(repo_root, str(expected))
        require_not_production_path(actual_lexical, key)
        require(
            actual_lexical == expected_lexical,
            f"{key} must resolve to {expected}, found {actual_lexical}",
        )
        actual = (
            actual_lexical
            if key == "MONOMER_DFT_PYTHON"
            else actual_lexical.resolve(strict=False)
        )
        resolved[key] = str(actual)

    for key in (
        "MONOMER_DFT_PYTHON",
        "MONOMER_DFT_JOB_ROOT",
        "AIMNET_CACHE_DIR",
        "WARP_CACHE_PATH",
        "UV_CACHE_DIR",
    ):
        actual = lexical_path(repo_root, values[key]) if key == "MONOMER_DFT_PYTHON" else resolve_path(repo_root, values[key])
        require(actual.is_relative_to(runtime), f"{key} escapes .runtime")
    require(resolve_path(repo_root, values["MONOMER_DFT_WORKER_UDS"]).parent.is_relative_to(runtime), "UDS escapes .runtime")
    dev_gpu_root = runtime / "gpu-resource"
    expected_broker_socket = dev_gpu_root / "broker.sock"
    expected_mps_pipe_root = dev_gpu_root
    expected_external_reservations = dev_gpu_root / "external-reservations.json"
    if formal_gpu_authority is not None:
        expected_broker_socket = formal_gpu_authority.root / "broker.sock"
        expected_mps_pipe_root = formal_gpu_authority.root
        expected_external_reservations = formal_gpu_authority.reservations
    broker_socket_lexical = lexical_path(
        repo_root, values["MONOMER_DFT_GPU_BROKER_UDS"]
    )
    require_not_production_path(
        broker_socket_lexical, "MONOMER_DFT_GPU_BROKER_UDS"
    )
    require(
        broker_socket_lexical == expected_broker_socket,
        f"unexpected development GPU Broker socket: {broker_socket_lexical}",
    )
    broker_socket = (
        broker_socket_lexical
        if formal_gpu_authority is not None
        else broker_socket_lexical.resolve(strict=False)
    )
    require(broker_socket == expected_broker_socket, f"unexpected Host GPU Broker socket: {broker_socket}")
    resolved["MONOMER_DFT_GPU_BROKER_UDS"] = str(broker_socket)
    mps_pipe_root_lexical = lexical_path(
        repo_root, values["MONOMER_DFT_GPU_MPS_PIPE_ROOT"]
    )
    require_not_production_path(
        mps_pipe_root_lexical, "MONOMER_DFT_GPU_MPS_PIPE_ROOT"
    )
    require(
        mps_pipe_root_lexical == expected_mps_pipe_root,
        f"unexpected development MPS state root: {mps_pipe_root_lexical}",
    )
    mps_pipe_root = (
        mps_pipe_root_lexical
        if formal_gpu_authority is not None
        else mps_pipe_root_lexical.resolve(strict=False)
    )
    require(
        mps_pipe_root == expected_mps_pipe_root,
        f"unexpected Host MPS state root: {mps_pipe_root}",
    )
    resolved["MONOMER_DFT_GPU_MPS_PIPE_ROOT"] = str(mps_pipe_root)
    external_reservations_lexical = lexical_path(
        repo_root, values["MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS"]
    )
    require_not_production_path(
        external_reservations_lexical,
        "MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS",
    )
    require(
        external_reservations_lexical == expected_external_reservations,
        "GPU external reservations must use the current development worktree",
    )
    external_reservations = (
        external_reservations_lexical
        if formal_gpu_authority is not None
        else external_reservations_lexical.resolve(strict=False)
    )
    require(
        external_reservations == expected_external_reservations,
        f"unexpected GPU external reservation manifest: {external_reservations}",
    )
    resolved["MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS"] = str(
        external_reservations
    )

    require(values["MONOMER_DFT_MAX_CONCURRENT_JOBS"] == "1", "worker concurrency must be exactly 1")
    require(
        values["NEXPOLY_DFT_GPU_DEVICE"] == "1",
        "development primary GPU must be physical GPU 1; GPUs 0 and 2 are forbidden",
    )
    require(
        values["NEXPOLY_DFT_OVERFLOW_GPU_DEVICES"] == "3",
        "development overflow GPU must be physical GPU 3 only; GPUs 0 and 2 are forbidden",
    )
    require(values["MONOMER_DFT_GPU_BUDGET_MIB"] == "4096", "DFT GPU budget must be 4096 MiB")
    require(values["MONOMER_DFT_GPU_ACTIVE_THREAD_PERCENTAGE"] == "50", "DFT MPS active-thread limit must be 50 percent")
    require(
        values["MONOMER_DFT_DOWNLOAD_SPOOL_ROOT"]
        == "/app/.runtime/monomer-dft-download-spool",
        "download spool must use the fixed worktree-backed development mount",
    )
    require(values["MONOMER_DFT_GPU_BROKER_ENABLED"] in {"0", "1"}, "GPU Broker flag must be 0 or 1")
    require(values["MONOMER_DFT_STANDALONE_GPU_SMOKE"] in {"0", "1"}, "standalone GPU smoke flag must be 0 or 1")
    if values["MONOMER_DFT_GPU_BROKER_ENABLED"] == "0":
        require(values["MONOMER_DFT_STANDALONE_GPU_SMOKE"] == "1", "Broker-disabled mode requires explicit standalone GPU smoke authorization")
    else:
        require(values["MONOMER_DFT_STANDALONE_GPU_SMOKE"] == "0", "Broker mode cannot be marked standalone")
    require(values["PYTHONNOUSERSITE"] == "1", "PYTHONNOUSERSITE must be 1")
    require(values["PYTHONDONTWRITEBYTECODE"] == "1", "PYTHONDONTWRITEBYTECODE must be 1")
    require(values["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID", "CUDA_DEVICE_ORDER must be PCI_BUS_ID")

    python_path = pathlib.Path(resolved["MONOMER_DFT_PYTHON"])
    require(python_path.is_file(), f"isolated Python does not exist: {python_path}")
    require(pathlib.Path(os.path.abspath(sys.executable)) == python_path, f"run preflight with {python_path}")
    require(pathlib.Path(sys.prefix).resolve() == python_path.parents[1].resolve(), "Python is not running inside the isolated venv")
    require(sys.prefix != sys.base_prefix, "system Python is forbidden; activate the isolated venv interpreter")
    require(pathlib.Path(resolved["MONOMER_DFT_JOB_ROOT"]).is_dir(), "job root is missing")
    require(pathlib.Path(resolved["AIMNET_CACHE_DIR"]).is_dir(), "AIMNet cache is missing")
    require(pathlib.Path(resolved["WARP_CACHE_PATH"]).is_dir(), "Warp cache is missing")
    require(pathlib.Path(resolved["UV_CACHE_DIR"]).is_dir(), "uv cache is missing")
    require(pathlib.Path(resolved["MONOMER_DFT_WORKER_UDS"]).parent.is_dir(), "worker socket directory is missing")

    source_dir = resolve_path(repo_root, values["AIMNET_SOURCE_DIR"])
    model_source_lexical = lexical_path(
        repo_root, values["AIMNET_MODEL_SOURCE_DIR"]
    )
    require_not_production_path(model_source_lexical, "AIMNET_MODEL_SOURCE_DIR")
    model_source_dir = model_source_lexical.resolve(strict=False)
    require(source_dir.is_dir(), f"AIMNet source input is missing: {source_dir}")
    require(model_source_dir.is_dir(), f"shared model source is missing: {model_source_dir}")
    resolved["AIMNET_SOURCE_DIR"] = str(source_dir)
    resolved["AIMNET_MODEL_SOURCE_DIR"] = str(model_source_dir)
    return resolved


def validate_git(repo_root: pathlib.Path) -> dict[str, str]:
    branch = run("git", "-C", str(repo_root), "branch", "--show-current")
    expected_branch = os.getenv("MONOMER_DFT_EXPECTED_BRANCH", "").strip()
    if expected_branch:
        require(branch == expected_branch, f"unexpected branch: {branch or 'detached HEAD'}")
    ignored_runtime = subprocess.run(
        ("git", "-C", str(repo_root), "check-ignore", "-q", ".runtime/probe"), check=False
    ).returncode
    ignored_env = subprocess.run(
        ("git", "-C", str(repo_root), "check-ignore", "-q", ".env.monomer-dft.dev"), check=False
    ).returncode
    require(ignored_runtime == 0 and ignored_env == 0, "runtime paths are not ignored by Git")
    return {"branch": branch, "head": run("git", "-C", str(repo_root), "rev-parse", "HEAD")}


def validate_physical_gpu(gpu_index: str) -> dict[str, Any]:
    require(
        gpu_index in EXPECTED_GPU_UUIDS,
        "only physical GPUs 1 and 3 belong to development DFT; GPUs 0 and 2 are forbidden",
    )
    rows = run(
        "nvidia-smi",
        f"--id={gpu_index}",
        "--query-gpu=index,uuid,name",
        "--format=csv,noheader,nounits",
    ).splitlines()
    gpu: dict[str, str] | None = None
    for row in rows:
        fields = [field.strip() for field in row.split(",", 2)]
        if len(fields) == 3 and fields[0] == gpu_index:
            gpu = {"physical_index": fields[0], "uuid": fields[1], "name": fields[2]}
            break
    require(gpu is not None, f"physical GPU {gpu_index} is unavailable")
    require(gpu["name"] == EXPECTED_GPU_NAME, f"GPU {gpu_index} is {gpu['name']}, expected {EXPECTED_GPU_NAME}")
    require(gpu["uuid"] == EXPECTED_GPU_UUIDS[gpu_index], f"GPU {gpu_index} UUID drifted: {gpu['uuid']}")

    processes = run(
        "nvidia-smi",
        f"--id={gpu_index}",
        "--query-compute-apps=gpu_uuid,pid,process_name",
        "--format=csv,noheader,nounits",
    )
    conflicts: list[dict[str, str]] = []
    for row in processes.splitlines():
        fields = [field.strip() for field in row.split(",", 2)]
        if len(fields) == 3 and fields[0] == gpu["uuid"] and fields[1] != str(os.getpid()):
            conflicts.append({"pid": fields[1], "process_name": fields[2]})
    # Shared MPS clients are expected. Capacity and unregistered-client checks
    # belong to the host Broker rather than this package/provenance preflight.
    gpu["compute_processes_before_import"] = conflicts
    return gpu


def validate_standalone_gpu_claims(
    gpu_index: str,
    external_reservations_path: pathlib.Path,
) -> dict[str, Any]:
    from workers.monomer_dft_worker.app.gpu_broker_client import (
        audit_isolated_gpu_availability,
    )

    blocked = audit_isolated_gpu_availability((gpu_index,))
    require(not blocked, f"standalone GPU {gpu_index} has a live process or Docker claim")
    rows = run(
        "nvidia-smi",
        "--query-gpu=index,compute_mode",
        "--format=csv,noheader,nounits",
    ).splitlines()
    compute_mode = next(
        (
            fields[1]
            for row in rows
            if len(fields := [item.strip() for item in row.split(",", 1)]) == 2
            and fields[0] == gpu_index
        ),
        None,
    )
    require(
        compute_mode in {"Exclusive_Process", "Exclusive Process"},
        f"standalone GPU {gpu_index} must use EXCLUSIVE_PROCESS compute mode",
    )
    require(
        external_reservations_path.is_file()
        and not external_reservations_path.is_symlink(),
        "GPU external reservation manifest is unavailable or unsafe",
    )
    manifest = json.loads(external_reservations_path.read_text(encoding="utf-8"))
    require(manifest.get("schema_version") == 1, "unsupported GPU reservation manifest")
    target_uuid = EXPECTED_GPU_UUIDS[gpu_index]
    declared: list[str] = []
    blocked_uuids = manifest.get("blocked_gpu_uuids", {})
    if isinstance(blocked_uuids, dict) and target_uuid in blocked_uuids:
        declared.append("blocked_gpu_uuids")
    for section in ("managed_docker_claims", "managed_systemd_claims"):
        claims = manifest.get(section, {})
        require(isinstance(claims, dict), f"invalid {section} declaration")
        for name, claim in claims.items():
            if isinstance(claim, dict) and target_uuid in claim.get("gpu_uuids", []):
                declared.append(f"{section}:{name}")
    require(
        not declared,
        f"standalone GPU {gpu_index} remains externally declared: {declared}",
    )
    return {"compute_mode": compute_mode, "external_claims": declared}


def validate_source_lock(repo_root: pathlib.Path, resolved: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    lock_path = pathlib.Path(resolved["AIMNET_SOURCE_LOCK"])
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    require(lock.get("schema_version") == 1, "unsupported AIMNet lock schema")
    source = lock.get("source", {})
    wheel = lock.get("wheel", {})
    registry = lock.get("registry", {})
    require(source.get("package_name") == "aimnet", "unexpected package name in AIMNet lock")
    require(source.get("wheel_install_mode") == "non-editable", "AIMNet must be installed non-editably")
    require(
        isinstance(wheel, dict)
        and isinstance(wheel.get("filename"), str)
        and isinstance(wheel.get("sha256"), str)
        and wheel.get("file_count") == 47,
        "AIMNet deterministic wheel lock is incomplete",
    )
    require(source.get("python_minor") == "3.12", "AIMNet Python version lock mismatch")
    require(source.get("uv_version") == EXPECTED_UV_VERSION, "AIMNet uv version lock mismatch")
    require(
        source.get("source_date_epoch") == 1782945961,
        "AIMNet SOURCE_DATE_EPOCH lock mismatch",
    )
    build_lock = repo_root / "workers/monomer_dft_worker/build-requirements.lock"
    require(
        source.get("build_requirements_sha256") == sha256_file(build_lock),
        "AIMNet build dependency lock checksum mismatch",
    )

    source_dir = pathlib.Path(resolved["AIMNET_SOURCE_DIR"])
    require(source_dir.is_dir() and not source_dir.is_symlink(), "AIMNet clean archive is missing")
    evidence_path = repo_root / ".runtime/aimnet-source-archive.json"
    require(
        evidence_path.is_file() and not evidence_path.is_symlink(),
        "AIMNet clean-archive evidence is missing or unsafe",
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    require(evidence.get("schema_version") == 1, "unsupported AIMNet archive evidence")
    require(evidence.get("commit") == source.get("commit"), "AIMNet archive commit mismatch")
    require(evidence.get("tree") == source.get("tree"), "AIMNet archive tree mismatch")
    require(
        evidence.get("source_date_epoch") == source.get("source_date_epoch"),
        "AIMNet archive SOURCE_DATE_EPOCH mismatch",
    )
    inventory_digest = directory_digest(source_dir)
    require(
        evidence.get("archive_inventory_sha256") == inventory_digest,
        "AIMNet clean archive changed after extraction",
    )
    require(
        source.get("archive_inventory_sha256") == inventory_digest,
        "AIMNet clean archive differs from the fixed source lock",
    )
    registry_source = (source_dir / str(registry.get("path", ""))).resolve()
    require(
        registry_source.is_relative_to(source_dir),
        "pinned AIMNet registry escapes the clean archive",
    )
    require(registry_source.is_file(), "pinned AIMNet registry is missing")
    require(sha256_file(registry_source) == registry.get("sha256"), "source registry checksum mismatch")

    models = lock.get("models")
    require(isinstance(models, list) and len(models) == 6, "AIMNet lock must contain six models")
    return lock, {
        "commit": source["commit"],
        "tree": source["tree"],
        "archive_inventory_sha256": inventory_digest,
        "package_version": source["package_version"],
    }


def validate_python_and_models(
    repo_root: pathlib.Path,
    resolved: dict[str, str],
    lock: dict[str, Any],
    gpu_index: str,
    *,
    initialize_cuda: bool = True,
) -> dict[str, Any]:
    require(sys.version_info[:2] == (3, 12), f"expected Python 3.12, found {sys.version.split()[0]}")

    runtime_uv = repo_root / ".runtime/tools/uv"
    try:
        uv_metadata = runtime_uv.lstat()
    except OSError as exc:
        raise PreflightError(
            f"private uv runtime tool is unavailable: {runtime_uv}"
        ) from exc
    require(
        stat.S_ISREG(uv_metadata.st_mode)
        and uv_metadata.st_uid == os.geteuid()
        and uv_metadata.st_gid == os.getegid()
        and uv_metadata.st_nlink == 1
        and stat.S_IMODE(uv_metadata.st_mode) == 0o500,
        "private uv runtime tool is unsafe",
    )
    uv_version = run(str(runtime_uv), "--version").split()
    require(len(uv_version) >= 2 and uv_version[1] == EXPECTED_UV_VERSION, "uv version mismatch")

    versions = {name: importlib.metadata.version(name) for name in EXPECTED_DIRECT_VERSIONS}
    for name, expected in EXPECTED_DIRECT_VERSIONS.items():
        require(versions[name] == expected, f"{name} version mismatch: expected {expected}, found {versions[name]}")
    expected_aimnet = lock["source"]["package_version"]
    versions["aimnet"] = importlib.metadata.version("aimnet")
    require(versions["aimnet"] == expected_aimnet, "installed AIMNet version does not match the source lock")

    venv = (repo_root / ".runtime/venvs/monomer-dft-worker").resolve()
    locked_versions = validate_complete_lock(repo_root, venv)
    distribution = importlib.metadata.distribution("aimnet")
    aimnet_path = pathlib.Path(
        distribution.locate_file("aimnet/__init__.py")
    ).resolve()
    require(aimnet_path.is_relative_to(venv), f"AIMNet distribution escapes the isolated venv: {aimnet_path}")
    require("site-packages" in aimnet_path.parts, f"AIMNet distribution is not in site-packages: {aimnet_path}")
    wheel_result = validate_aimnet_wheel_record(repo_root, distribution)

    torch: Any | None = None
    if initialize_cuda:
        # This path is reserved for an explicitly authorized, exclusively
        # audited standalone development smoke. Broker preflight must not
        # import any CUDA-bearing package.
        import aimnet
        import ase
        import nvalchemiops
        import rdkit
        import torch as imported_torch
        import warp

        torch = imported_torch
        imported_aimnet_path = pathlib.Path(aimnet.__file__).resolve()
        require(imported_aimnet_path == aimnet_path, "AIMNet import differs from distribution provenance")
        for module in (ase, nvalchemiops, rdkit, warp):
            module_path = pathlib.Path(module.__file__).resolve()
            require(module_path.is_relative_to(venv), f"dependency import escapes the isolated venv: {module_path}")

    registry_rel = pathlib.Path(lock["registry"]["path"])
    require(registry_rel.parts and registry_rel.parts[0] == "aimnet", "unsafe registry path in lock")
    installed_registry = aimnet_path.parent.joinpath(*registry_rel.parts[1:])
    require(installed_registry.is_file(), "installed AIMNet registry is missing")
    require(sha256_file(installed_registry) == lock["registry"]["sha256"], "installed registry checksum mismatch")

    cache_dir = pathlib.Path(resolved["AIMNET_CACHE_DIR"])
    require(cache_dir.stat().st_mode & 0o222 == 0, "AIMNet cache directory must be read-only")
    model_results: list[dict[str, str]] = []
    seen_files: set[str] = set()
    for model in lock["models"]:
        require(model.get("ensemble_member") == 0, f"non-member-0 model in lock: {model.get('alias')}")
        model_file = str(model.get("file", ""))
        require(model_file and pathlib.Path(model_file).name == model_file, "unsafe model filename")
        require(model_file not in seen_files, f"duplicate model file: {model_file}")
        seen_files.add(model_file)
        expected_sha = model.get("sha256") or model.get("cache_sha256")
        require(expected_sha == model.get("registry_sha256") == model.get("cache_sha256"), "model hash audit fields disagree")
        path = cache_dir / model_file
        require(path.is_file() and not path.is_symlink(), f"isolated model is missing: {model_file}")
        require(path.stat().st_mode & 0o222 == 0, f"isolated model must be read-only: {model_file}")
        actual_sha = sha256_file(path)
        require(actual_sha == expected_sha, f"isolated model checksum mismatch: {model_file}")
        model_results.append({"alias": model["alias"], "file": model_file, "sha256": actual_sha})
    actual_cache_entries = {entry.name for entry in cache_dir.iterdir()}
    require(actual_cache_entries == seen_files, f"isolated model cache contains unexpected entries: {sorted(actual_cache_entries - seen_files)}")

    torch_cuda_runtime = EXPECTED_CUDA_RUNTIME
    visible_name: str | None = None
    visible_uuid: str | None = None
    physical_uuid = EXPECTED_GPU_UUIDS[gpu_index]
    visible_device_count = 0
    if initialize_cuda:
        assert torch is not None
        require(torch.version.cuda == EXPECTED_CUDA_RUNTIME, f"expected CUDA runtime {EXPECTED_CUDA_RUNTIME}, found {torch.version.cuda}")
        torch_cuda_runtime = str(torch.version.cuda)
        require(torch.cuda.is_available(), "CUDA is unavailable")
        visible_device_count = torch.cuda.device_count()
        require(visible_device_count == 1, f"worker must see exactly one GPU, found {visible_device_count}")
        visible_name = torch.cuda.get_device_name(0)
        require(visible_name == EXPECTED_GPU_NAME, f"visible GPU is {visible_name}, expected {EXPECTED_GPU_NAME}")
        visible_uuid = f"GPU-{str(torch.cuda.get_device_properties(0).uuid)}"
        require(
            visible_uuid.lower() == physical_uuid.lower(),
            f"visible cuda:0 UUID {visible_uuid} is not physical GPU {gpu_index} ({physical_uuid})",
        )
        torch.cuda.synchronize()

    return {
        "python": sys.version.split()[0],
        "uv": EXPECTED_UV_VERSION,
        "versions": versions,
        "locked_package_count": len(locked_versions),
        "aimnet_import": str(aimnet_path),
        "aimnet_wheel": wheel_result,
        "installed_registry": str(installed_registry),
        "models": model_results,
        "cuda_validation": (
            "direct_development_probe"
            if initialize_cuda
            else "deferred_to_registered_residency_executor"
        ),
        "visible_cuda_devices": visible_device_count,
        "visible_gpu_name": visible_name,
        "visible_gpu_uuid": visible_uuid,
        "physical_gpu_uuid": physical_uuid,
        "torch_cuda_runtime": torch_cuda_runtime,
    }


def main() -> int:
    repo_root = require_development_repo_root(
        pathlib.Path(__file__).resolve().parents[1]
    )
    inherited_pythonpath = os.environ.get("PYTHONPATH", "")
    require(not inherited_pythonpath, "inherited PYTHONPATH must be unset or empty")

    env_file = repo_root / ".env.monomer-dft.dev"
    values, formal_gpu_authority = effective_environment(
        repo_root,
        load_env_file(env_file),
    )
    resolved = validate_environment(
        repo_root,
        values,
        formal_gpu_authority,
    )
    git_result = validate_git(repo_root)

    broker_enabled = values["MONOMER_DFT_GPU_BROKER_ENABLED"] == "1"
    inherited_visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    if broker_enabled:
        require(
            inherited_visible in (None, ""),
            "Broker-enabled preflight must remain CPU-only and CUDA-blind",
        )
    else:
        require(
            inherited_visible in (None, "", values["NEXPOLY_DFT_GPU_DEVICE"]),
            f"CUDA_VISIBLE_DEVICES conflicts with physical GPU {values['NEXPOLY_DFT_GPU_DEVICE']}",
        )
    inherited_order = os.environ.get("CUDA_DEVICE_ORDER")
    require(inherited_order in (None, "", "PCI_BUS_ID"), "CUDA_DEVICE_ORDER conflicts with PCI_BUS_ID")
    for key, value in values.items():
        if key != "PYTHONPATH":
            os.environ[key] = value
    for key, value in resolved.items():
        if key in values:
            os.environ[key] = value
    if broker_enabled:
        os.environ.pop("CUDA_VISIBLE_DEVICES", None)
    else:
        os.environ["CUDA_VISIBLE_DEVICES"] = values["NEXPOLY_DFT_GPU_DEVICE"]
    os.environ.pop("PYTHONPATH", None)
    sys.dont_write_bytecode = True

    gpu_result = validate_physical_gpu(values["NEXPOLY_DFT_GPU_DEVICE"])
    if not broker_enabled:
        gpu_result["standalone_audit"] = validate_standalone_gpu_claims(
            values["NEXPOLY_DFT_GPU_DEVICE"],
            pathlib.Path(resolved["MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS"]),
        )
    lock, source_result = validate_source_lock(repo_root, resolved)
    python_result = validate_python_and_models(
        repo_root,
        resolved,
        lock,
        values["NEXPOLY_DFT_GPU_DEVICE"],
        initialize_cuda=not broker_enabled,
    )

    report = {
        "status": "ok",
        "worktree": str(repo_root),
        "git": git_result,
        "source": source_result,
        "gpu": gpu_result,
        "runtime": python_result,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PreflightError, KeyError, json.JSONDecodeError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        raise SystemExit(2) from None
