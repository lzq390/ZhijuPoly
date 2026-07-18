from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_ROOT = REPO_ROOT / ".runtime"
FORBIDDEN_SOURCE_ROOTS = (
    Path("/data/cgy").resolve(),
    Path("/data/lzq/gith/aimnetcentral").resolve(),
)
GPU_UUID_BY_INDEX = {
    "1": "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
    "2": "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe",
    "3": "GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5",
}


def _runtime_path(name: str, default: str) -> Path:
    value = os.getenv(name, default).strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    # Keep the venv's ``bin/python`` symlink inside the runtime namespace.  A
    # normal Path.resolve() would follow it to /usr/bin/python3.12 and falsely
    # report that the configured interpreter escaped .runtime/.
    path = Path(os.path.abspath(os.path.normpath(path)))
    try:
        path.relative_to(RUNTIME_ROOT)
    except ValueError as exc:
        raise ValueError(f"{name} must be located below {RUNTIME_ROOT}") from exc
    return path


def _host_socket_path(name: str, default: str) -> Path:
    value = os.getenv(name, default).strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    path = Path(os.path.abspath(os.path.normpath(path)))
    if len(os.fsencode(path)) > 107:
        raise ValueError(f"{name} exceeds the Linux Unix-socket path limit")
    return path


def _host_path(name: str, default: str) -> Path:
    value = os.getenv(name, default).strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return Path(os.path.abspath(os.path.normpath(path)))


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be >= 1")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be > 0")
    return value


def _gpu_index(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    if value not in {"1", "2", "3"}:
        raise ValueError(f"{name} must be one of physical GPU 1, 2, or 3")
    return value


def _gpu_list(name: str, default: str, *, primary: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not values:
        return ()
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must not contain duplicate devices")
    if primary in values:
        raise ValueError(f"{name} must not contain the primary GPU")
    if any(value not in {"1", "2", "3"} for value in values):
        raise ValueError(f"{name} may contain only physical GPU 1, 2, or 3")
    return values


def _validate_pythonpath() -> None:
    raw_pythonpath = os.getenv("PYTHONPATH", "")
    if not raw_pythonpath:
        return
    for raw_entry in raw_pythonpath.split(os.pathsep):
        if not raw_entry.strip():
            continue
        entry = Path(raw_entry).expanduser().resolve(strict=False)
        for forbidden_root in FORBIDDEN_SOURCE_ROOTS:
            if entry == forbidden_root or forbidden_root in entry.parents:
                raise ValueError(
                    "PYTHONPATH must not reference the original AIMNet environment "
                    f"or source clone: {entry}"
                )
    raise ValueError("PYTHONPATH must be empty for the isolated monomer DFT worker")


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    python: Path
    uds: Path
    job_root: Path
    max_concurrent_jobs: int
    physical_gpu: str
    logical_device: str
    aimnet_cache_dir: Path
    warp_cache_path: Path
    model_name: str
    worker_version: str
    max_queued_jobs: int = 8
    preload_all_models: bool = False
    warmup_models: bool = False
    single_point_timeout_seconds: float = 600.0
    optimization_timeout_seconds: float = 1800.0
    deployment: Literal["dev", "prod"] = "dev"
    overflow_gpu_devices: tuple[str, ...] = ("3",)
    broker_enabled: bool = False
    standalone_gpu_smoke: bool = False
    broker_uds: Path | None = None
    mps_pipe_root: Path = Path("/data/lzq/gith/nexpoly/ops/state/gpu-resource")
    gpu_residency_budget_mib: int = 4096
    gpu_active_thread_percentage: int = 50


def load_settings() -> WorkerSettings:
    _validate_pythonpath()

    deployment = os.getenv("MONOMER_DFT_DEPLOYMENT", "dev").strip().lower()
    if deployment not in {"dev", "prod"}:
        raise ValueError("MONOMER_DFT_DEPLOYMENT must be dev or prod")
    default_primary = "1" if deployment == "dev" else "2"
    physical_gpu = _gpu_index("NEXPOLY_DFT_GPU_DEVICE", default_primary)
    expected_primary = "1" if deployment == "dev" else "2"
    if physical_gpu != expected_primary:
        raise ValueError(
            f"{deployment} monomer DFT primary GPU must be physical GPU "
            f"{expected_primary}"
        )
    overflow_gpu_devices = _gpu_list(
        "NEXPOLY_DFT_OVERFLOW_GPU_DEVICES",
        "3" if deployment == "dev" else "3,1",
        primary=physical_gpu,
    )
    expected_overflow = ("3",) if deployment == "dev" else ("3", "1")
    if overflow_gpu_devices != expected_overflow:
        raise ValueError(
            f"{deployment} monomer DFT overflow GPUs must be exactly "
            + ",".join(expected_overflow)
        )

    # The HTTP supervisor is deliberately CUDA-blind. Only the executor child
    # receives CUDA_VISIBLE_DEVICES, before importing Torch/Warp/AIMNet.
    visible_devices = os.getenv("CUDA_VISIBLE_DEVICES")
    if os.getenv("MONOMER_DFT_EXECUTOR_PROCESS") == "1":
        executor_gpu = os.getenv("NEXPOLY_DFT_EXECUTOR_GPU_DEVICE", "").strip()
        if executor_gpu not in {"1", "2", "3"}:
            raise ValueError("executor GPU identity is missing or invalid")
        expected_uuid = GPU_UUID_BY_INDEX[executor_gpu]
        executor_uuid = os.getenv("NEXPOLY_DFT_EXECUTOR_GPU_UUID", "").strip()
        if executor_uuid and executor_uuid != expected_uuid:
            raise ValueError("executor GPU index-to-UUID identity is invalid")
        expected_visible = executor_uuid or executor_gpu
        if visible_devices is None or visible_devices.strip() != expected_visible:
            raise ValueError(
                "executor CUDA_VISIBLE_DEVICES must contain exactly its leased GPU index or UUID"
            )
    elif visible_devices not in {None, ""}:
        raise ValueError("CUDA_VISIBLE_DEVICES must be unset for the CPU-only supervisor")

    max_concurrent_jobs = _positive_int("MONOMER_DFT_MAX_CONCURRENT_JOBS", 1)
    if max_concurrent_jobs != 1:
        raise ValueError("MONOMER_DFT_MAX_CONCURRENT_JOBS must be 1 in the isolated worker")

    max_queued_jobs = _positive_int("MONOMER_DFT_MAX_QUEUED_JOBS", 8)
    if max_queued_jobs != 8:
        raise ValueError("MONOMER_DFT_MAX_QUEUED_JOBS must be 8 in the isolated worker")

    uds = _runtime_path(
        "MONOMER_DFT_WORKER_UDS",
        ".runtime/monomer-dft-worker-socket/worker.sock",
    )
    if len(os.fsencode(uds)) > 107:
        raise ValueError("MONOMER_DFT_WORKER_UDS exceeds the Linux Unix-socket path limit")

    broker_raw = os.getenv("MONOMER_DFT_GPU_BROKER_ENABLED", "1").strip()
    if broker_raw not in {"0", "1"}:
        raise ValueError("MONOMER_DFT_GPU_BROKER_ENABLED must be 0 or 1")
    broker_enabled = broker_raw == "1"
    standalone_raw = os.getenv("MONOMER_DFT_STANDALONE_GPU_SMOKE", "0").strip()
    if standalone_raw not in {"0", "1"}:
        raise ValueError("MONOMER_DFT_STANDALONE_GPU_SMOKE must be 0 or 1")
    standalone_gpu_smoke = standalone_raw == "1"
    if not broker_enabled and not standalone_gpu_smoke:
        raise ValueError(
            "Broker-disabled execution is allowed only for an explicitly audited standalone GPU smoke"
        )
    if broker_enabled and standalone_gpu_smoke:
        raise ValueError("standalone GPU smoke mode cannot be combined with the Broker")
    if deployment == "prod" and not broker_enabled:
        raise ValueError("production monomer DFT Worker requires the Host GPU Broker")
    default_gpu_root = (
        REPO_ROOT / ".runtime" / "gpu-resource"
        if deployment == "dev"
        else Path("/data/lzq/gith/nexpoly/ops/state/gpu-resource")
    )
    broker_uds = _host_socket_path(
        "MONOMER_DFT_GPU_BROKER_UDS",
        str(default_gpu_root / "broker.sock"),
    )
    mps_pipe_root = _host_path(
        "MONOMER_DFT_GPU_MPS_PIPE_ROOT",
        str(default_gpu_root),
    )
    if deployment == "dev":
        expected_broker_uds = default_gpu_root / "broker.sock"
        if broker_uds != expected_broker_uds:
            raise ValueError(
                "dev MONOMER_DFT_GPU_BROKER_UDS must stay inside this "
                f"worktree at {expected_broker_uds}"
            )
        if mps_pipe_root != default_gpu_root:
            raise ValueError(
                "dev MONOMER_DFT_GPU_MPS_PIPE_ROOT must stay inside this "
                f"worktree at {default_gpu_root}"
            )
        if (
            default_gpu_root.is_symlink()
            or default_gpu_root.resolve(strict=False) != default_gpu_root
            or broker_uds.is_symlink()
            or broker_uds.parent.resolve(strict=False) != default_gpu_root
            or mps_pipe_root.is_symlink()
            or mps_pipe_root.resolve(strict=False) != default_gpu_root
        ):
            raise ValueError(
                "dev GPU Broker/MPS paths must use the real, non-symlinked "
                f"worktree namespace at {default_gpu_root}"
            )
    residency_budget = _positive_int("MONOMER_DFT_GPU_BUDGET_MIB", 4096)
    if residency_budget != 4096:
        raise ValueError("MONOMER_DFT_GPU_BUDGET_MIB must be 4096 until benchmarks approve a new cap")
    active_threads = _positive_int("MONOMER_DFT_GPU_ACTIVE_THREAD_PERCENTAGE", 50)
    if active_threads != 50:
        raise ValueError("MONOMER_DFT_GPU_ACTIVE_THREAD_PERCENTAGE must be 50")

    return WorkerSettings(
        python=_runtime_path(
            "MONOMER_DFT_PYTHON",
            ".runtime/venvs/monomer-dft-worker/bin/python",
        ),
        uds=uds,
        job_root=_runtime_path(
            "MONOMER_DFT_JOB_ROOT",
            ".runtime/monomer-dft-worker-runs",
        ),
        max_concurrent_jobs=max_concurrent_jobs,
        physical_gpu=physical_gpu,
        logical_device="cuda:0",
        aimnet_cache_dir=_runtime_path("AIMNET_CACHE_DIR", ".runtime/aimnet-cache"),
        warp_cache_path=_runtime_path("WARP_CACHE_PATH", ".runtime/warp-cache"),
        model_name="aimnet2",
        worker_version=os.getenv("MONOMER_DFT_WORKER_VERSION", "0.1.0").strip()
        or "0.1.0",
        max_queued_jobs=max_queued_jobs,
        preload_all_models=True,
        warmup_models=True,
        single_point_timeout_seconds=_positive_float(
            "MONOMER_DFT_SINGLE_POINT_TIMEOUT_SECONDS", 600.0
        ),
        optimization_timeout_seconds=_positive_float(
            "MONOMER_DFT_OPTIMIZATION_TIMEOUT_SECONDS", 1800.0
        ),
        deployment=deployment,  # type: ignore[arg-type]
        overflow_gpu_devices=overflow_gpu_devices,
        broker_enabled=broker_enabled,
        standalone_gpu_smoke=standalone_gpu_smoke,
        broker_uds=broker_uds,
        mps_pipe_root=mps_pipe_root,
        gpu_residency_budget_mib=residency_budget,
        gpu_active_thread_percentage=active_threads,
    )
