from __future__ import annotations

import builtins
import importlib.util
import tempfile
from pathlib import Path

import pytest

from workers.monomer_dft_worker.app import config


_PREFLIGHT_SPEC = importlib.util.spec_from_file_location(
    "monomer_dft_preflight_for_test",
    Path(__file__).resolve().parents[3] / "scripts/preflight_monomer_dft_env.py",
)
assert _PREFLIGHT_SPEC is not None and _PREFLIGHT_SPEC.loader is not None
preflight = importlib.util.module_from_spec(_PREFLIGHT_SPEC)
_PREFLIGHT_SPEC.loader.exec_module(preflight)


ENV_NAMES = (
    "MONOMER_DFT_PYTHON",
    "MONOMER_DFT_WORKER_UDS",
    "MONOMER_DFT_JOB_ROOT",
    "MONOMER_DFT_DEV_RUNTIME_ROOT",
    "MONOMER_DFT_MAX_CONCURRENT_JOBS",
    "MONOMER_DFT_MAX_QUEUED_JOBS",
    "MONOMER_DFT_SINGLE_POINT_TIMEOUT_SECONDS",
    "MONOMER_DFT_OPTIMIZATION_TIMEOUT_SECONDS",
    "NEXPOLY_DFT_GPU_DEVICE",
    "NEXPOLY_DFT_OVERFLOW_GPU_DEVICES",
    "MONOMER_DFT_DEPLOYMENT",
    "MONOMER_DFT_GPU_BROKER_ENABLED",
    "MONOMER_DFT_STANDALONE_GPU_SMOKE",
    "MONOMER_DFT_GPU_BROKER_UDS",
    "MONOMER_DFT_GPU_MPS_PIPE_ROOT",
    "MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS",
    "MONOMER_DFT_DOWNLOAD_SPOOL_ROOT",
    "MONOMER_DFT_GPU_BUDGET_MIB",
    "MONOMER_DFT_GPU_ACTIVE_THREAD_PERCENTAGE",
    "MONOMER_DFT_EXECUTOR_PROCESS",
    "NEXPOLY_DFT_EXECUTOR_GPU_DEVICE",
    "NEXPOLY_DFT_EXECUTOR_GPU_UUID",
    "AIMNET_CACHE_DIR",
    "WARP_CACHE_PATH",
    "CUDA_VISIBLE_DEVICES",
)


@pytest.fixture(autouse=True)
def _private_dev_runtime_root(
    monkeypatch: pytest.MonkeyPatch,
):
    with tempfile.TemporaryDirectory(prefix="dftcfg-", dir="/tmp") as temp_dir:
        repo_root = Path(temp_dir)
        runtime_root = repo_root / ".runtime"
        runtime_root.mkdir(mode=0o700)
        runtime_root.chmod(0o700)
        monkeypatch.setattr(config, "REPO_ROOT", repo_root)
        monkeypatch.setattr(config, "RUNTIME_ROOT", runtime_root)
        yield


def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_load_settings_uses_isolated_runtime_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_environment(monkeypatch)

    settings = config.load_settings()

    assert settings.python == config.RUNTIME_ROOT / "venvs/monomer-dft-worker/bin/python"
    assert settings.uds == config.RUNTIME_ROOT / "monomer-dft-worker-socket/worker.sock"
    assert settings.job_root == config.RUNTIME_ROOT / "monomer-dft-worker-runs"
    assert settings.aimnet_cache_dir == config.RUNTIME_ROOT / "aimnet-cache"
    assert settings.warp_cache_path == config.RUNTIME_ROOT / "warp-cache"
    assert settings.physical_gpu == "1"
    assert settings.overflow_gpu_devices == ("3",)
    assert settings.mps_pipe_root == config.REPO_ROOT / ".runtime/gpu-resource"
    assert settings.broker_uds == config.REPO_ROOT / ".runtime/gpu-resource/broker.sock"
    assert settings.gpu_external_reservations == (
        config.REPO_ROOT / ".runtime/gpu-resource/external-reservations.json"
    )
    assert settings.download_spool_root == (
        config.REPO_ROOT / ".runtime/monomer-dft-downloads"
    )
    assert settings.dev_runtime_root == config.RUNTIME_ROOT
    assert "2" not in (settings.physical_gpu, *settings.overflow_gpu_devices)
    assert settings.deployment == "dev"
    assert settings.logical_device == "cuda:0"
    assert settings.max_concurrent_jobs == 1
    assert settings.max_queued_jobs == 8
    assert settings.single_point_timeout_seconds == 600
    assert settings.optimization_timeout_seconds == 1800
    assert settings.model_name == "aimnet2"


def test_broker_preflight_provenance_path_imports_no_cuda_stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path
    venv = repo / ".runtime/venvs/monomer-dft-worker"
    aimnet_package = venv / "lib/python3.12/site-packages/aimnet"
    aimnet_package.mkdir(parents=True)
    (aimnet_package / "__init__.py").write_text("", encoding="utf-8")
    (aimnet_package / "models.yaml").write_text("registry", encoding="utf-8")
    cache = repo / ".runtime/aimnet-cache"
    runtime_uv = repo / ".runtime/tools/uv"
    runtime_uv.parent.mkdir(parents=True)
    runtime_uv.write_bytes(b"test uv")
    runtime_uv.chmod(0o500)
    cache.mkdir(parents=True)
    models = []
    for index in range(6):
        filename = f"model-{index}.jpt"
        path = cache / filename
        path.write_bytes(b"model")
        path.chmod(0o444)
        models.append(
            {
                "alias": f"model-{index}",
                "file": filename,
                "ensemble_member": 0,
                "sha256": "a" * 64,
                "registry_sha256": "a" * 64,
                "cache_sha256": "a" * 64,
            }
        )
    cache.chmod(0o555)
    lock = {
        "source": {"package_version": "0.0.test"},
        "registry": {"path": "aimnet/models.yaml", "sha256": "b" * 64},
        "models": models,
    }

    class Distribution:
        def locate_file(self, relative):
            return venv / "lib/python3.12/site-packages" / relative

    versions = dict(preflight.EXPECTED_DIRECT_VERSIONS)
    versions["aimnet"] = "0.0.test"
    monkeypatch.setattr(
        preflight,
        "run",
        lambda *args: (
            "uv 0.11.21"
            if Path(args[0]).name == "uv"
            else ""
        ),
    )
    monkeypatch.setattr(preflight.importlib.metadata, "version", lambda name: versions[name])
    monkeypatch.setattr(preflight.importlib.metadata, "distribution", lambda _name: Distribution())
    monkeypatch.setattr(preflight, "validate_complete_lock", lambda *_args: {})
    monkeypatch.setattr(preflight, "validate_aimnet_wheel_record", lambda *_args: {"ok": True})
    monkeypatch.setattr(
        preflight,
        "sha256_file",
        lambda path: "b" * 64 if Path(path).name == "models.yaml" else "a" * 64,
    )
    original_import = builtins.__import__

    def forbid_cuda_import(name, *args, **kwargs):
        if name.split(".", 1)[0] in {
            "aimnet",
            "torch",
            "warp",
            "nvalchemiops",
            "ase",
            "rdkit",
        }:
            raise AssertionError(f"Broker preflight imported CUDA/scientific stack: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", forbid_cuda_import)
    result = preflight.validate_python_and_models(
        repo,
        {"AIMNET_CACHE_DIR": str(cache)},
        lock,
        "1",
        initialize_cuda=False,
    )

    assert result["cuda_validation"] == "deferred_to_registered_residency_executor"
    assert result["visible_cuda_devices"] == 0


@pytest.mark.parametrize(
    "pythonpath",
    (
        "/data/cgy/AIMNet/model/aimnetcentral",
        "/safe/path:/data/lzq/gith/aimnetcentral",
        "/data/lzq/gith/aimnetcentral/running_code",
    ),
)
def test_load_settings_rejects_forbidden_pythonpath(
    monkeypatch: pytest.MonkeyPatch,
    pythonpath: str,
) -> None:
    _clean_environment(monkeypatch)
    monkeypatch.setenv("PYTHONPATH", pythonpath)

    with pytest.raises(ValueError, match="PYTHONPATH"):
        config.load_settings()


def test_load_settings_rejects_any_external_pythonpath(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_environment(monkeypatch)
    monkeypatch.setenv("PYTHONPATH", "/otherwise/safe/path")

    with pytest.raises(ValueError, match="must be empty"):
        config.load_settings()


def test_load_settings_rejects_more_than_one_visible_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_environment(monkeypatch)
    monkeypatch.setenv("NEXPOLY_DFT_GPU_DEVICE", "1")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1,3")

    with pytest.raises(ValueError, match="CUDA_VISIBLE_DEVICES"):
        config.load_settings()


@pytest.mark.parametrize("gpu", ("0", "2", "3"))
def test_load_settings_rejects_a_different_physical_gpu(
    monkeypatch: pytest.MonkeyPatch,
    gpu: str,
) -> None:
    _clean_environment(monkeypatch)
    monkeypatch.setenv("NEXPOLY_DFT_GPU_DEVICE", gpu)

    with pytest.raises(ValueError, match="must be physical GPU 1"):
        config.load_settings()


@pytest.mark.parametrize("overflow", ("", "2", "3,2", "2,3"))
def test_dev_load_settings_requires_gpu3_as_the_only_overflow(
    monkeypatch: pytest.MonkeyPatch,
    overflow: str,
) -> None:
    _clean_environment(monkeypatch)
    monkeypatch.setenv("NEXPOLY_DFT_OVERFLOW_GPU_DEVICES", overflow)

    with pytest.raises(ValueError, match="overflow GPUs must be exactly 3"):
        config.load_settings()


@pytest.mark.parametrize(
    ("name", "value"),
    (
        (
            "MONOMER_DFT_WORKER_UDS",
            "/data/lzq/gith/nexpoly/ops/state/worker.sock",
        ),
        (
            "MONOMER_DFT_JOB_ROOT",
            "/data/lzq/gith/nexpoly/ops/state/monomer-dft-runs",
        ),
        (
            "AIMNET_CACHE_DIR",
            "/data/lzq/gith/nexpoly/ops/state/aimnet-cache",
        ),
        (
            "WARP_CACHE_PATH",
            "/data/lzq/gith/nexpoly/ops/state/warp-cache",
        ),
        (
            "MONOMER_DFT_GPU_BROKER_UDS",
            "/data/lzq/gith/nexpoly/ops/state/gpu-resource/broker.sock",
        ),
        (
            "MONOMER_DFT_GPU_MPS_PIPE_ROOT",
            "/data/lzq/gith/nexpoly/ops/state/gpu-resource",
        ),
        (
            "MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS",
            "/data/lzq/gith/nexpoly/ops/state/gpu-resource/external-reservations.json",
        ),
        (
            "MONOMER_DFT_DOWNLOAD_SPOOL_ROOT",
            "/data/lzq/gith/nexpoly/ops/state/monomer-dft-downloads",
        ),
    ),
)
def test_dev_load_settings_rejects_production_gpu_resource_paths(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    _clean_environment(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match="must be located below"):
        config.load_settings()


def test_dev_load_settings_rejects_symlinked_gpu_resource_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_environment(monkeypatch)
    # Keep the root short enough that both test UDS paths remain below Linux's
    # byte limit; pytest's normal generated path is intentionally much longer.
    with tempfile.TemporaryDirectory(prefix="dftcfg-", dir="/tmp") as temp_dir:
        temp_root = Path(temp_dir)
        repo_root = temp_root / "worktree"
        runtime_root = repo_root / ".runtime"
        external_gpu_root = temp_root / "shared-gpu-resource"
        runtime_root.mkdir(parents=True)
        runtime_root.chmod(0o700)
        external_gpu_root.mkdir()
        (runtime_root / "gpu-resource").symlink_to(
            external_gpu_root,
            target_is_directory=True,
        )
        monkeypatch.setattr(config, "REPO_ROOT", repo_root)
        monkeypatch.setattr(config, "RUNTIME_ROOT", runtime_root)

        with pytest.raises(ValueError, match="symlink component"):
            config.load_settings()


def test_load_settings_rejects_production_even_with_broker_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_environment(monkeypatch)
    monkeypatch.setenv("MONOMER_DFT_DEPLOYMENT", "prod")
    monkeypatch.setenv("MONOMER_DFT_GPU_BROKER_ENABLED", "1")

    with pytest.raises(ValueError, match="production is hard-off"):
        config.load_settings()


def test_broker_disabled_requires_explicit_standalone_smoke_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_environment(monkeypatch)
    monkeypatch.setenv("MONOMER_DFT_GPU_BROKER_ENABLED", "0")
    with pytest.raises(ValueError, match="standalone GPU smoke"):
        config.load_settings()

    monkeypatch.setenv("MONOMER_DFT_STANDALONE_GPU_SMOKE", "1")
    settings = config.load_settings()
    assert settings.broker_enabled is False
    assert settings.standalone_gpu_smoke is True


def test_executor_process_may_see_only_its_preselected_overflow_gpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_environment(monkeypatch)
    monkeypatch.setenv("MONOMER_DFT_EXECUTOR_PROCESS", "1")
    monkeypatch.setenv("NEXPOLY_DFT_EXECUTOR_GPU_DEVICE", "3")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "3")

    settings = config.load_settings()

    assert settings.physical_gpu == "1"
    assert settings.executor_process is True


def test_executor_process_accepts_the_leased_gpu_uuid_from_mps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_environment(monkeypatch)
    gpu_uuid = config.GPU_UUID_BY_INDEX["3"]
    monkeypatch.setenv("MONOMER_DFT_EXECUTOR_PROCESS", "1")
    monkeypatch.setenv("NEXPOLY_DFT_EXECUTOR_GPU_DEVICE", "3")
    monkeypatch.setenv("NEXPOLY_DFT_EXECUTOR_GPU_UUID", gpu_uuid)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", gpu_uuid)

    settings = config.load_settings()

    assert settings.physical_gpu == "1"


def test_executor_process_rejects_gpu_selection_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_environment(monkeypatch)
    monkeypatch.setenv("MONOMER_DFT_EXECUTOR_PROCESS", "1")
    monkeypatch.setenv("NEXPOLY_DFT_EXECUTOR_GPU_DEVICE", "3")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1")

    with pytest.raises(ValueError, match="leased GPU"):
        config.load_settings()


@pytest.mark.parametrize("gpu", ("0", "2"))
def test_executor_process_explicitly_rejects_gpu0_and_gpu2(
    monkeypatch: pytest.MonkeyPatch,
    gpu: str,
) -> None:
    _clean_environment(monkeypatch)
    monkeypatch.setenv("MONOMER_DFT_EXECUTOR_PROCESS", "1")
    monkeypatch.setenv("NEXPOLY_DFT_EXECUTOR_GPU_DEVICE", gpu)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", gpu)

    with pytest.raises(ValueError, match="GPU 0 and GPU 2 are forbidden"):
        config.load_settings()


def test_load_settings_rejects_parallel_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_environment(monkeypatch)
    monkeypatch.setenv("MONOMER_DFT_MAX_CONCURRENT_JOBS", "2")

    with pytest.raises(ValueError, match="must be 1"):
        config.load_settings()


def test_load_settings_rejects_runtime_path_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_environment(monkeypatch)
    monkeypatch.setenv("AIMNET_CACHE_DIR", "/tmp/shared-aimnet-cache")

    with pytest.raises(ValueError, match="must be located below"):
        config.load_settings()


def test_runtime_path_keeps_venv_python_symlink_lexically_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / ".runtime"
    python_path = runtime_root / "venvs/worker/bin/python"
    python_path.parent.mkdir(parents=True)
    python_path.symlink_to("/usr/bin/python3")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setenv("MONOMER_DFT_PYTHON", str(python_path))

    assert config._runtime_path("MONOMER_DFT_PYTHON", "unused") == python_path


def test_load_settings_rejects_non_private_runtime_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_environment(monkeypatch)
    runtime_root = tmp_path / "world-readable-runtime"
    runtime_root.mkdir(mode=0o755)
    runtime_root.chmod(0o755)
    monkeypatch.setenv("MONOMER_DFT_DEV_RUNTIME_ROOT", str(runtime_root))

    with pytest.raises(ValueError, match="mode 0700"):
        config.load_settings()


def test_direct_worker_settings_reject_prod_gpu2_and_external_paths(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "direct-runtime"
    runtime_root.mkdir(mode=0o700)
    runtime_root.chmod(0o700)
    common = {
        "python": Path("/usr/bin/python3.12"),
        "uds": runtime_root / "socket/worker.sock",
        "job_root": runtime_root / "runs",
        "max_concurrent_jobs": 1,
        "physical_gpu": "1",
        "logical_device": "cuda:0",
        "aimnet_cache_dir": runtime_root / "models",
        "warp_cache_path": runtime_root / "warp",
        "model_name": "aimnet2",
        "worker_version": "test",
        "dev_runtime_root": runtime_root,
    }
    with pytest.raises(ValueError, match="production is hard-off"):
        config.WorkerSettings(**common, deployment="prod")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="GPU 0 and GPU 2 are forbidden"):
        config.WorkerSettings(**{**common, "physical_gpu": "2"})
    with pytest.raises(ValueError, match="must be located below|production repository"):
        config.WorkerSettings(
            **{**common, "job_root": Path("/data/lzq/gith/nexpoly/ops/state/runs")}
        )
