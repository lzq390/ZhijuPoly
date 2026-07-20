from __future__ import annotations

import math
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from gpu_resource.authority import (
    load_formal_gpu_authority,
    materialize_formal_gpu_authority,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_ROOT = REPO_ROOT / ".runtime"
PRODUCTION_REPO_ROOT = Path("/data/lzq/gith/nexpoly")
FORBIDDEN_SOURCE_ROOTS = (
    Path("/data/cgy").resolve(),
    Path("/data/lzq/gith/aimnetcentral").resolve(),
)
GPU_UUID_BY_INDEX = {
    "1": "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
    "3": "GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5",
}


def _absolute_path(value: str | os.PathLike[str], *, base: Path = REPO_ROOT) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return Path(os.path.abspath(os.path.normpath(path)))


def _is_below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _reject_production_path(name: str, path: Path) -> None:
    production_root = PRODUCTION_REPO_ROOT.resolve(strict=False)
    candidate = path.resolve(strict=False)
    if candidate == production_root or production_root in candidate.parents:
        raise ValueError(f"{name} must not reference the production repository")


def validate_private_dev_runtime_root(path: Path) -> Path:
    """Return an owner-private development runtime root or fail closed."""

    root = _absolute_path(path)
    _reject_production_path("MONOMER_DFT_DEV_RUNTIME_ROOT", root)
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise ValueError(
            f"development runtime root must already exist: {root}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"development runtime root must be a real directory: {root}")
    resolved = root.resolve(strict=True)
    if resolved != root:
        raise ValueError(
            f"development runtime root must not contain symlink components: {root}"
        )
    if metadata.st_uid != os.geteuid():
        raise ValueError(
            f"development runtime root must be owned by uid {os.geteuid()}: {root}"
        )
    mode = stat.S_IMODE(metadata.st_mode)
    if mode != 0o700:
        raise ValueError(
            f"development runtime root must have mode 0700, found {mode:04o}: {root}"
        )
    return resolved


def validate_dev_runtime_path(
    name: str,
    path: Path,
    *,
    runtime_root: Path,
    leaf_kind: Literal["directory", "file", "socket", "any"] = "any",
    allow_leaf_symlink: bool = False,
) -> Path:
    """Validate lexical and existing-component containment below a dev root."""

    root = validate_private_dev_runtime_root(runtime_root)
    candidate = _absolute_path(path)
    authority = load_formal_gpu_authority(
        expected_reservations_file=(
            REPO_ROOT / "ops/config/gpu-external-reservations.json"
        ),
        expected_root=REPO_ROOT / ".runtime/gpu-resource",
    )
    if authority is not None and name in {
        "MONOMER_DFT_GPU_BROKER_UDS",
        "MONOMER_DFT_GPU_MPS_PIPE_ROOT",
        "MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS",
    }:
        expected = {
            "MONOMER_DFT_GPU_BROKER_UDS": (
                authority.root / "broker.sock"
            ),
            "MONOMER_DFT_GPU_MPS_PIPE_ROOT": authority.root,
            "MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS": (
                authority.reservations
            ),
        }[name]
        if candidate != expected:
            raise ValueError(
                f"{name} differs from formal GPU descriptor authority"
            )
        return candidate
    _reject_production_path(name, candidate)
    if candidate == root or not _is_below(candidate, root):
        raise ValueError(f"{name} must be located below {root}")

    relative = candidate.relative_to(root)
    current = root
    parts = relative.parts
    for index, component in enumerate(parts):
        current = current / component
        is_leaf = index == len(parts) - 1
        if current.is_symlink():
            if is_leaf and allow_leaf_symlink:
                continue
            raise ValueError(f"{name} contains a symlink component: {current}")
        if not current.exists():
            continue
        if not is_leaf and not current.is_dir():
            raise ValueError(f"{name} contains a non-directory component: {current}")
        if not is_leaf:
            continue
        if leaf_kind == "directory" and not current.is_dir():
            raise ValueError(f"{name} must be a directory: {current}")
        if leaf_kind == "file" and not current.is_file():
            raise ValueError(f"{name} must be a regular file: {current}")
        if leaf_kind == "socket" and not stat.S_ISSOCK(current.stat().st_mode):
            raise ValueError(f"{name} must be a Unix socket: {current}")
    return candidate


def _configured_runtime_root() -> Path:
    configured = os.getenv("MONOMER_DFT_DEV_RUNTIME_ROOT")
    value = (configured if configured is not None else str(RUNTIME_ROOT)).strip()
    if not value:
        raise ValueError("MONOMER_DFT_DEV_RUNTIME_ROOT must not be empty")
    root = _absolute_path(value)
    if configured is None and not root.exists() and not root.is_symlink():
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            pass
    return validate_private_dev_runtime_root(root)


def _runtime_path(name: str, default: str, *, runtime_root: Path | None = None) -> Path:
    value = os.getenv(name, default).strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    root = runtime_root or RUNTIME_ROOT
    # Keep the venv's ``bin/python`` symlink inside the runtime namespace.  A
    # normal Path.resolve() would follow it to /usr/bin/python3.12 and falsely
    # report that the configured interpreter escaped .runtime/.
    path = _absolute_path(value)
    authority = load_formal_gpu_authority(
        expected_reservations_file=(
            REPO_ROOT / "ops/config/gpu-external-reservations.json"
        ),
        expected_root=REPO_ROOT / ".runtime/gpu-resource",
    )
    if authority is not None and name in {
        "MONOMER_DFT_GPU_BROKER_UDS",
        "MONOMER_DFT_GPU_MPS_PIPE_ROOT",
        "MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS",
    }:
        expected = {
            "MONOMER_DFT_GPU_BROKER_UDS": (
                authority.root / "broker.sock"
            ),
            "MONOMER_DFT_GPU_MPS_PIPE_ROOT": authority.root,
            "MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS": (
                authority.reservations
            ),
        }[name]
        if path != expected:
            raise ValueError(
                f"{name} differs from formal GPU descriptor authority"
            )
        return path
    if path == root or not _is_below(path, root):
        raise ValueError(f"{name} must be located below {root}")
    _reject_production_path(name, path)
    return path


def _socket_path(name: str, default: str, *, runtime_root: Path) -> Path:
    path = _runtime_path(name, default, runtime_root=runtime_root)
    if len(os.fsencode(path)) > 107:
        raise ValueError(f"{name} exceeds the Linux Unix-socket path limit")
    return path


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
    if value != "1":
        raise ValueError(f"{name} must be physical GPU 1 in the dev-only release")
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
    if any(value != "3" for value in values):
        raise ValueError("dev overflow GPUs must be exactly 3")
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
    deployment: Literal["dev"] = "dev"
    overflow_gpu_devices: tuple[str, ...] = ("3",)
    broker_enabled: bool = False
    standalone_gpu_smoke: bool = False
    broker_uds: Path | None = None
    mps_pipe_root: Path | None = None
    mps_pipe_directories: tuple[tuple[int, Path], ...] = ()
    gpu_residency_budget_mib: int = 4096
    gpu_active_thread_percentage: int = 50
    dev_runtime_root: Path = field(default_factory=lambda: RUNTIME_ROOT)
    gpu_external_reservations: Path | None = None
    download_spool_root: Path | None = None
    executor_process: bool = False

    def __post_init__(self) -> None:
        code_root = REPO_ROOT.resolve(strict=False)
        _reject_production_path("Worker code root", code_root)
        runtime_root = validate_private_dev_runtime_root(self.dev_runtime_root)
        formal_gpu_authority = load_formal_gpu_authority(
            expected_reservations_file=(
                REPO_ROOT / "ops/config/gpu-external-reservations.json"
            ),
            expected_root=REPO_ROOT / ".runtime/gpu-resource",
        )
        object.__setattr__(self, "dev_runtime_root", runtime_root)

        if self.deployment != "dev":
            raise ValueError("MONOMER_DFT_DEPLOYMENT must be dev; production is hard-off")
        if self.physical_gpu not in GPU_UUID_BY_INDEX:
            raise ValueError("physical GPU must be 1 or 3; GPU 0 and GPU 2 are forbidden")
        if not self.executor_process and self.physical_gpu != "1":
            raise ValueError("dev supervisor primary GPU must be physical GPU 1")
        if self.overflow_gpu_devices != ("3",):
            raise ValueError("dev overflow GPUs must be exactly physical GPU 3")
        if self.logical_device != "cuda:0":
            raise ValueError("executor logical device must remain cuda:0")

        mps_pipe_root = self.mps_pipe_root or runtime_root / "gpu-resource"
        external_reservations = (
            self.gpu_external_reservations
            or runtime_root / "gpu-resource/external-reservations.json"
        )
        download_spool_root = (
            self.download_spool_root or runtime_root / "monomer-dft-downloads"
        )
        object.__setattr__(
            self,
            "uds",
            validate_dev_runtime_path(
                "MONOMER_DFT_WORKER_UDS",
                self.uds,
                runtime_root=runtime_root,
                leaf_kind="socket",
            ),
        )
        object.__setattr__(
            self,
            "job_root",
            validate_dev_runtime_path(
                "MONOMER_DFT_JOB_ROOT",
                self.job_root,
                runtime_root=runtime_root,
                leaf_kind="directory",
            ),
        )
        object.__setattr__(
            self,
            "aimnet_cache_dir",
            validate_dev_runtime_path(
                "AIMNET_CACHE_DIR",
                self.aimnet_cache_dir,
                runtime_root=runtime_root,
                leaf_kind="directory",
            ),
        )
        object.__setattr__(
            self,
            "warp_cache_path",
            validate_dev_runtime_path(
                "WARP_CACHE_PATH",
                self.warp_cache_path,
                runtime_root=runtime_root,
                leaf_kind="directory",
            ),
        )
        if self.broker_uds is not None:
            object.__setattr__(
                self,
                "broker_uds",
                validate_dev_runtime_path(
                    "MONOMER_DFT_GPU_BROKER_UDS",
                    self.broker_uds,
                    runtime_root=runtime_root,
                    leaf_kind="socket",
                ),
            )
        object.__setattr__(
            self,
            "mps_pipe_root",
            validate_dev_runtime_path(
                "MONOMER_DFT_GPU_MPS_PIPE_ROOT",
                mps_pipe_root,
                runtime_root=runtime_root,
                leaf_kind="directory",
            ),
        )
        expected_pipe_directories = (
            formal_gpu_authority.pipe_directories
            if formal_gpu_authority is not None
            else ()
        )
        if self.mps_pipe_directories != expected_pipe_directories:
            raise ValueError(
                "formal MPS pipe descriptor authority is inconsistent"
            )
        object.__setattr__(
            self,
            "gpu_external_reservations",
            validate_dev_runtime_path(
                "MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS",
                external_reservations,
                runtime_root=runtime_root,
                leaf_kind="file",
            ),
        )
        object.__setattr__(
            self,
            "download_spool_root",
            validate_dev_runtime_path(
                "MONOMER_DFT_DOWNLOAD_SPOOL_ROOT",
                download_spool_root,
                runtime_root=runtime_root,
                leaf_kind="directory",
            ),
        )


def load_settings() -> WorkerSettings:
    _validate_pythonpath()

    code_root = REPO_ROOT.resolve(strict=False)
    _reject_production_path("Worker code root", code_root)
    formal_gpu_authority = materialize_formal_gpu_authority(
        expected_reservations_file=(
            REPO_ROOT / "ops/config/gpu-external-reservations.json"
        ),
        expected_root=REPO_ROOT / ".runtime/gpu-resource",
    )
    if formal_gpu_authority is not None:
        os.environ.update(
            {
                "MONOMER_DFT_GPU_BROKER_UDS": str(
                    formal_gpu_authority.root / "broker.sock"
                ),
                "MONOMER_DFT_GPU_MPS_PIPE_ROOT": str(
                    formal_gpu_authority.root
                ),
                "MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS": str(
                    formal_gpu_authority.reservations
                ),
            }
        )
    runtime_root = _configured_runtime_root()
    deployment = os.getenv("MONOMER_DFT_DEPLOYMENT", "dev").strip().lower()
    if deployment != "dev":
        raise ValueError("MONOMER_DFT_DEPLOYMENT must be dev; production is hard-off")
    physical_gpu = _gpu_index("NEXPOLY_DFT_GPU_DEVICE", "1")
    overflow_gpu_devices = _gpu_list(
        "NEXPOLY_DFT_OVERFLOW_GPU_DEVICES",
        "3",
        primary=physical_gpu,
    )
    expected_overflow = ("3",)
    if overflow_gpu_devices != expected_overflow:
        raise ValueError("dev overflow GPUs must be exactly 3")

    # The HTTP supervisor is deliberately CUDA-blind. Only the executor child
    # receives CUDA_VISIBLE_DEVICES, before importing Torch/Warp/AIMNet.
    visible_devices = os.getenv("CUDA_VISIBLE_DEVICES")
    executor_process = os.getenv("MONOMER_DFT_EXECUTOR_PROCESS") == "1"
    if executor_process:
        executor_gpu = os.getenv("NEXPOLY_DFT_EXECUTOR_GPU_DEVICE", "").strip()
        if executor_gpu not in GPU_UUID_BY_INDEX:
            raise ValueError(
                "executor GPU must be physical GPU 1 or 3; GPU 0 and GPU 2 are forbidden"
            )
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
        str(runtime_root / "monomer-dft-worker-socket/worker.sock"),
        runtime_root=runtime_root,
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
    default_gpu_root = runtime_root / "gpu-resource"
    broker_uds = _socket_path(
        "MONOMER_DFT_GPU_BROKER_UDS",
        str(default_gpu_root / "broker.sock"),
        runtime_root=runtime_root,
    )
    mps_pipe_root = _runtime_path(
        "MONOMER_DFT_GPU_MPS_PIPE_ROOT",
        str(default_gpu_root),
        runtime_root=runtime_root,
    )
    external_reservations = _runtime_path(
        "MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS",
        str(default_gpu_root / "external-reservations.json"),
        runtime_root=runtime_root,
    )
    download_spool_root = _runtime_path(
        "MONOMER_DFT_DOWNLOAD_SPOOL_ROOT",
        str(runtime_root / "monomer-dft-downloads"),
        runtime_root=runtime_root,
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
            str(runtime_root / "venvs/monomer-dft-worker/bin/python"),
            runtime_root=runtime_root,
        ),
        uds=uds,
        job_root=_runtime_path(
            "MONOMER_DFT_JOB_ROOT",
            str(runtime_root / "monomer-dft-worker-runs"),
            runtime_root=runtime_root,
        ),
        max_concurrent_jobs=max_concurrent_jobs,
        physical_gpu=physical_gpu,
        logical_device="cuda:0",
        aimnet_cache_dir=_runtime_path(
            "AIMNET_CACHE_DIR",
            str(runtime_root / "aimnet-cache"),
            runtime_root=runtime_root,
        ),
        warp_cache_path=_runtime_path(
            "WARP_CACHE_PATH",
            str(runtime_root / "warp-cache"),
            runtime_root=runtime_root,
        ),
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
        deployment="dev",
        overflow_gpu_devices=overflow_gpu_devices,
        broker_enabled=broker_enabled,
        standalone_gpu_smoke=standalone_gpu_smoke,
        broker_uds=broker_uds,
        mps_pipe_root=mps_pipe_root,
        mps_pipe_directories=(
            formal_gpu_authority.pipe_directories
            if formal_gpu_authority is not None
            else ()
        ),
        gpu_residency_budget_mib=residency_budget,
        gpu_active_thread_percentage=active_threads,
        dev_runtime_root=runtime_root,
        gpu_external_reservations=external_reservations,
        download_spool_root=download_spool_root,
        executor_process=executor_process,
    )
