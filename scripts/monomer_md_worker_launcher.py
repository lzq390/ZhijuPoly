#!/usr/bin/env python3
"""Stable, fail-closed launcher for the production Monomer-MD Worker."""

from __future__ import annotations

import os
import fcntl
from importlib import import_module
import importlib.util
from pathlib import Path
import stat
import sys
from typing import Callable, Mapping

def _validated_runtime_sibling(name: str) -> Path:
    """Validate an adjacent runtime helper before executing its Python code."""

    launcher = Path(__file__).absolute()
    parent = launcher.parent
    path = parent / name
    try:
        launcher_metadata = launcher.lstat()
        parent_metadata = parent.lstat()
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"stable Worker runtime helper is missing: {name}") from exc
    installed = parent == Path(
        "/data/lzq/gith/nexpoly-runtime/bin"
    )
    if (
        not stat.S_ISREG(launcher_metadata.st_mode)
        or launcher.is_symlink()
        or launcher_metadata.st_uid != os.geteuid()
        or launcher_metadata.st_mode & 0o022
        or (installed and stat.S_IMODE(launcher_metadata.st_mode) != 0o700)
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent.is_symlink()
        or parent_metadata.st_uid != os.geteuid()
        or parent_metadata.st_mode & 0o022
        or (installed and stat.S_IMODE(parent_metadata.st_mode) != 0o700)
        or not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
        or (installed and stat.S_IMODE(metadata.st_mode) != 0o700)
    ):
        raise RuntimeError(f"stable Worker runtime helper is unsafe: {name}")
    return path


if __package__:
    _slot_runtime = import_module(f"{__package__}.worker_slot_runtime")
else:
    _runtime_path = _validated_runtime_sibling("worker_slot_runtime.py")
    _runtime_spec = importlib.util.spec_from_file_location(
        "_nexpoly_worker_slot_runtime",
        _runtime_path,
    )
    if _runtime_spec is None or _runtime_spec.loader is None:
        raise RuntimeError("stable Worker slot runtime cannot be loaded")
    _slot_runtime = importlib.util.module_from_spec(_runtime_spec)
    sys.modules[_runtime_spec.name] = _slot_runtime
    _runtime_spec.loader.exec_module(_slot_runtime)
PRODUCTION_RUNTIME_ROOT = _slot_runtime.PRODUCTION_RUNTIME_ROOT
PRODUCTION_SOURCE_ROOT = _slot_runtime.PRODUCTION_SOURCE_ROOT
WorkerSlotError = _slot_runtime.WorkerSlotError
verify_runtime_binding = _slot_runtime.verify_runtime_binding


SANITIZED_MARKER = "NEXPOLY_MONOMER_MD_ENV_SANITIZED"
PRODUCTION_BYTEFF2_PYTHON = Path(
    "/home/devuser/miniconda3/envs/byteff2-repro/bin/python"
)
PRODUCTION_BYTEFF2_OPENMM_DIR = Path(
    "/home/devuser/miniconda3/envs/byteff2-repro/byteff2_openmm/openmm"
)


class LauncherError(RuntimeError):
    """A public-safe launcher validation failure."""


def _require_directory(path: Path, *, mode: int = 0o700) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise LauncherError(f"required runtime directory is missing: {path}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise LauncherError(f"required runtime directory is unsafe: {path}")


def runtime_paths(runtime_root: Path) -> tuple[Path, Path, Path, Path]:
    state = runtime_root / "state"
    return (
        state / "current-assets/byteff2",
        state / "monomer-md-worker-runs",
        state / "monomer-md-worker-socket/worker.sock",
        state / "gpu-resource",
    )


def expected_environment(
    *,
    source_root: Path = PRODUCTION_SOURCE_ROOT,
    runtime_root: Path = PRODUCTION_RUNTIME_ROOT,
) -> dict[str, str]:
    asset_root, job_root, socket, gpu_state_root = runtime_paths(runtime_root)
    return {
        "BYTEFF2_ROOT": str(asset_root),
        "BYTEFF2_PYTHON": str(PRODUCTION_BYTEFF2_PYTHON),
        "BYTEFF2_OPENMM_DIR": str(PRODUCTION_BYTEFF2_OPENMM_DIR),
        "MONOMER_MD_JOB_ROOT": str(job_root),
        "MONOMER_MD_WORKER_UDS": str(socket),
        "MONOMER_MD_WORKER_MODE": "real",
        "MONOMER_MD_WORKER_ID": "monomer-md-production-worker",
        "MONOMER_MD_MAX_ACTIVE_JOBS": "1",
        "MONOMER_MD_MAX_CONCURRENT_JOBS": "1",
        "MONOMER_MD_DEFAULT_STEPS": "300",
        "MONOMER_MD_MAX_STEPS": "300",
        "MONOMER_MD_TRANSPORT_CUDA_SMOKE_ENABLED": "true",
        "NEXPOLY_GPU_DEVICE": "2",
        "MONOMER_MD_GPU_BROKER_ENABLED": "false",
        "MONOMER_MD_GPU_BROKER_ENVIRONMENT": "prod",
        "MONOMER_MD_GPU_BROKER_SOCKET_PATH": str(gpu_state_root / "broker.sock"),
        "MONOMER_MD_GPU_MPS_PIPE_ROOT": str(gpu_state_root),
        "PYTHONPATH": (
            f"{source_root}:{asset_root}:"
            f"{asset_root / 'submodules' / 'bytemol'}"
        ),
    }


def validate_environment(
    environment: Mapping[str, str],
    *,
    source_root: Path = PRODUCTION_SOURCE_ROOT,
    runtime_root: Path = PRODUCTION_RUNTIME_ROOT,
) -> None:
    if environment.get(SANITIZED_MARKER) != "1":
        raise LauncherError("Worker environment was not produced by the stable sanitizer")
    for key, expected in expected_environment(
        source_root=source_root,
        runtime_root=runtime_root,
    ).items():
        if environment.get(key) != expected:
            raise LauncherError(f"production Worker setting {key} is not pinned")
    if environment.get("MONOMER_MD_PYTHON"):
        raise LauncherError("MONOMER_MD_PYTHON must not select a production A/B slot")
    if environment.get("MONOMER_MD_CUDA_VISIBLE_DEVICES") not in {None, "2"}:
        raise LauncherError(
            "production Worker setting MONOMER_MD_CUDA_VISIBLE_DEVICES is not pinned"
        )
    if environment.get("PYTHONNOUSERSITE") != "1":
        raise LauncherError("production Worker must disable the user site")
    if environment.get("PYTHONDONTWRITEBYTECODE") != "1":
        raise LauncherError("production Worker must disable bytecode writes")


def prepare_socket(socket: Path) -> None:
    parent = socket.parent
    _require_directory(parent)
    try:
        metadata = socket.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LauncherError("Worker socket path cannot be inspected") from exc
    if socket.is_symlink() or not stat.S_ISSOCK(metadata.st_mode):
        raise LauncherError("Worker socket path exists but is not a Unix socket")
    if metadata.st_uid != os.geteuid():
        raise LauncherError("stale Worker socket belongs to a different user")
    try:
        socket.unlink()
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise LauncherError("stale Worker socket could not be removed safely") from exc


def acquire_worker_singleton(runtime_root: Path) -> int:
    """Acquire the owner-only Worker singleton and retain it across exec."""

    state = runtime_root / "state"
    _require_directory(state)
    path = state / "monomer-md-worker.lock"
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise LauncherError("Worker singleton lock is missing or unsafe") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise LauncherError("Worker singleton lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LauncherError("another production Worker instance is already running") from exc
        # Python-created descriptors are non-inheritable by default.  The raw
        # descriptor intentionally survives exec so the uvicorn process owns
        # the singleton for its complete lifetime.
        os.set_inheritable(descriptor, True)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def worker_argv(python: Path, socket: Path) -> list[str]:
    return [
        str(python),
        "-B",
        "-P",
        "-m",
        "uvicorn",
        "workers.monomer_md_worker.app.main:app",
        "--uds",
        str(socket),
    ]


def launch(
    environment: Mapping[str, str] | None = None,
    *,
    source_root: Path = PRODUCTION_SOURCE_ROOT,
    runtime_root: Path = PRODUCTION_RUNTIME_ROOT,
    execute: Callable[[str, list[str], dict[str, str]], object] = os.execve,
    change_directory: Callable[[Path], object] = os.chdir,
) -> None:
    child_environment = dict(os.environ if environment is None else environment)
    validate_environment(
        child_environment,
        source_root=source_root,
        runtime_root=runtime_root,
    )
    asset_root, job_root, socket, _gpu_state_root = runtime_paths(runtime_root)
    _require_directory(runtime_root)
    _require_directory(runtime_root / "state")
    _require_directory(job_root)
    if not asset_root.is_dir():
        raise LauncherError("pinned ByteFF2 asset root is missing")
    try:
        _checkout, _selection, python = verify_runtime_binding(
            source_root=source_root,
            runtime_root=runtime_root,
        )
    except WorkerSlotError as exc:
        raise LauncherError(str(exc)) from exc
    singleton = acquire_worker_singleton(runtime_root)
    try:
        prepare_socket(socket)
        change_directory(source_root)
        argv = worker_argv(python, socket)
        execute(str(python), argv, child_environment)
    except OSError as exc:
        raise LauncherError("production Worker could not be executed") from exc
    finally:
        # This branch is test-only or an exec failure.  A successful os.execve
        # replaces the process and the inherited descriptor remains open.
        try:
            os.close(singleton)
        except OSError:
            pass


def main() -> int:
    try:
        launch()
    except LauncherError as exc:
        print(f"monomer-md-worker-launcher: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
