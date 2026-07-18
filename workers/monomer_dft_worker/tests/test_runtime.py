from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from workers.monomer_dft_worker.app import runtime as runtime_module
from workers.monomer_dft_worker.app.config import GPU_UUID_BY_INDEX, WorkerSettings
from workers.monomer_dft_worker.app.runtime import (
    AimnetRuntime,
    load_model_spec,
    load_model_specs,
)


MODEL_SPEC = load_model_spec("aimnet2")


def _settings(tmp_path: Path) -> WorkerSettings:
    return WorkerSettings(
        python=tmp_path / "venv/bin/python",
        uds=tmp_path / "socket/worker.sock",
        job_root=tmp_path / "runs",
        max_concurrent_jobs=1,
        physical_gpu="3",
        logical_device="cuda:0",
        aimnet_cache_dir=tmp_path / "models",
        warp_cache_path=tmp_path / "warp",
        model_name="aimnet2",
        worker_version="test",
        dev_runtime_root=tmp_path,
        executor_process=True,
    )


class FakeCuda:
    def __init__(self, count: int = 1):
        self.count = count
        self.set_devices: list[int] = []
        self.synchronized: list[int] = []
        self.empty_cache_calls = 0

    @staticmethod
    def is_available() -> bool:
        return True

    def device_count(self) -> int:
        return self.count

    def set_device(self, device: int) -> None:
        self.set_devices.append(device)

    def synchronize(self, device: int) -> None:
        self.synchronized.append(device)

    @staticmethod
    def get_device_name(device: int) -> str:
        assert device == 0
        return "Fake RTX 4090"

    @staticmethod
    def get_device_properties(device: int) -> Any:
        assert device == 0
        return SimpleNamespace(
            uuid=bytes.fromhex("0818ca6bd9b6af6a71bfafe3777ee3a5")
        )

    def empty_cache(self) -> None:
        self.empty_cache_calls += 1


def _fake_torch(count: int = 1) -> Any:
    return SimpleNamespace(
        __version__="2.9.1+cu128",
        version=SimpleNamespace(cuda="12.8"),
        cuda=FakeCuda(count),
    )


def test_missing_model_fails_before_importing_torch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    runtime = AimnetRuntime(settings)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", GPU_UUID_BY_INDEX["3"])
    monkeypatch.setattr(runtime, "_validate_isolated_runtime", lambda: None)
    monkeypatch.setattr(
        runtime,
        "_import_torch",
        lambda: pytest.fail("torch must not be imported when the local model is missing"),
    )

    with pytest.raises(FileNotFoundError, match="never downloads"):
        runtime.load()

    probe = runtime.probe()
    assert probe.ready is False
    assert probe.model_loaded is False
    assert "FileNotFoundError" in (probe.error or "")


def test_symlinked_model_is_rejected_before_importing_torch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    settings.aimnet_cache_dir.mkdir(parents=True)
    external_model = tmp_path / "shared-model.pt"
    external_model.write_bytes(b"shared checkpoint")
    (settings.aimnet_cache_dir / MODEL_SPEC.file).symlink_to(external_model)
    runtime = AimnetRuntime(settings)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "3")
    monkeypatch.setattr(runtime, "_validate_isolated_runtime", lambda: None)
    monkeypatch.setattr(
        runtime,
        "_import_torch",
        lambda: pytest.fail("torch must not be imported for a symlinked model"),
    )

    with pytest.raises(RuntimeError, match="not a symlink"):
        runtime.load()


def test_load_uses_local_checkpoint_and_logical_cuda_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    settings.aimnet_cache_dir.mkdir(parents=True)
    model_path = settings.aimnet_cache_dir / MODEL_SPEC.file
    model_path.write_bytes(b"test checkpoint")
    fake_torch = _fake_torch()
    calculator_calls: list[tuple[str, str]] = []

    class FakeCalculator:
        def __init__(self, model: str, *, device: str):
            calculator_calls.append((model, device))

    runtime = AimnetRuntime(settings)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "3")
    monkeypatch.setattr(runtime, "_validate_isolated_runtime", lambda: None)
    monkeypatch.setattr(runtime, "_sha256", lambda _: MODEL_SPEC.sha256)
    monkeypatch.setattr(runtime, "_import_torch", lambda: fake_torch)
    monkeypatch.setattr(
        runtime,
        "_import_aimnet_calculator",
        lambda: (Path("/isolated/site-packages/aimnet/__init__.py"), FakeCalculator),
    )

    runtime.load()

    assert calculator_calls == [(str(model_path), "cuda:0")]
    assert fake_torch.cuda.set_devices == [0]
    assert fake_torch.cuda.synchronized == [0]
    assert runtime.calculator is not None
    probe = runtime.probe()
    assert probe.ready is True
    assert probe.visible_gpu_count == 1
    assert probe.logical_device == "cuda:0"
    assert probe.model_sha256 == MODEL_SPEC.sha256
    assert probe.gpu_name == "Fake RTX 4090"
    assert probe.gpu_uuid == GPU_UUID_BY_INDEX["3"]

    runtime.close()
    assert fake_torch.cuda.empty_cache_calls == 1
    assert runtime.probe().model_loaded is False


def test_resident_dev_executor_preloads_all_six_pinned_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(_settings(tmp_path), preload_all_models=True, warmup_models=False)
    settings.aimnet_cache_dir.mkdir(parents=True)
    specs = load_model_specs()
    checksums = {}
    for spec in specs:
        path = settings.aimnet_cache_dir / spec.file
        path.write_bytes(spec.alias.encode())
        path.chmod(0o444)
        checksums[path] = spec.sha256
    calculator_calls: list[tuple[str, str]] = []

    class FakeCalculator:
        def __init__(self, model: str, *, device: str):
            calculator_calls.append((model, device))

    runtime = AimnetRuntime(settings)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "3")
    monkeypatch.setattr(runtime, "_validate_isolated_runtime", lambda: None)
    monkeypatch.setattr(runtime, "_sha256", lambda path: checksums[path])
    monkeypatch.setattr(runtime, "_import_torch", lambda: _fake_torch())
    monkeypatch.setattr(
        runtime,
        "_import_aimnet_calculator",
        lambda: (Path("/isolated/site-packages/aimnet/__init__.py"), FakeCalculator),
    )

    runtime.load()

    assert len(calculator_calls) == 6
    assert {Path(path).name for path, _ in calculator_calls} == {spec.file for spec in specs}
    assert all(device == "cuda:0" for _, device in calculator_calls)
    assert set(runtime.probe().models) == {spec.alias for spec in specs}
    assert runtime.calculator_for("aimnet2-rxn") is not None


def test_load_rejects_multiple_logical_cuda_devices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    settings.aimnet_cache_dir.mkdir(parents=True)
    (settings.aimnet_cache_dir / MODEL_SPEC.file).write_bytes(b"test checkpoint")
    runtime = AimnetRuntime(settings)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "3")
    monkeypatch.setattr(runtime, "_validate_isolated_runtime", lambda: None)
    monkeypatch.setattr(runtime, "_sha256", lambda _: MODEL_SPEC.sha256)
    monkeypatch.setattr(runtime, "_import_torch", lambda: _fake_torch(count=2))
    monkeypatch.setattr(
        runtime,
        "_import_aimnet_calculator",
        lambda: pytest.fail("AIMNet must not load when two GPUs are visible"),
    )

    with pytest.raises(RuntimeError, match="exactly one CUDA device"):
        runtime.load()


def test_calculator_is_unavailable_before_lifespan_load(tmp_path: Path) -> None:
    runtime = AimnetRuntime(_settings(tmp_path))

    with pytest.raises(RuntimeError, match="has not been loaded"):
        _ = runtime.calculator


def test_runtime_path_validation_rejects_intermediate_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / ".runtime"
    runtime_root.mkdir(mode=0o700)
    runtime_root.chmod(0o700)
    settings = _settings(runtime_root)
    settings.python.parent.mkdir(parents=True)
    settings.python.write_text("#!/bin/sh\n", encoding="utf-8")
    settings.python.chmod(0o700)
    settings.uds.parent.mkdir(parents=True)
    settings.job_root.mkdir()
    settings.aimnet_cache_dir.mkdir()
    settings.warp_cache_path.mkdir()
    assert settings.mps_pipe_root is not None
    settings.mps_pipe_root.mkdir()
    assert settings.download_spool_root is not None
    settings.download_spool_root.mkdir()
    monkeypatch.setattr(runtime_module.sys, "executable", str(settings.python))
    runtime = AimnetRuntime(settings)

    runtime._validate_isolated_runtime()

    settings.job_root.rmdir()
    outside = tmp_path / "shared-runs"
    outside.mkdir()
    settings.job_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="MONOMER_DFT_JOB_ROOT must be a real directory"):
        runtime._validate_isolated_runtime()


def test_aimnet_import_must_belong_to_configured_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    settings.python.parent.mkdir(parents=True)
    outside_origin = tmp_path / "other/site-packages/aimnet/__init__.py"
    outside_origin.parent.mkdir(parents=True)
    outside_origin.write_text("", encoding="utf-8")
    runtime = AimnetRuntime(settings)

    def fake_import(name: str) -> Any:
        if name == "aimnet":
            return SimpleNamespace(__file__=str(outside_origin))
        return pytest.fail(f"unexpected import after origin rejection: {name}")

    monkeypatch.setattr(runtime_module.importlib, "import_module", fake_import)

    with pytest.raises(RuntimeError, match="outside the configured isolated venv"):
        runtime._import_aimnet_calculator()
