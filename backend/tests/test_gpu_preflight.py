from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from app import gpu_preflight
from app.services.conditional_generation_runtime import required_artifact_paths
from app.services.polytao_runtime import REQUIRED_MODEL_FILES


def _configured_settings(tmp_path: Path) -> SimpleNamespace:
    model_root = tmp_path / "model"
    ocsr_root = model_root / "ocsr"
    conditional_root = model_root / "conditional_generation"
    retro_root = model_root / "reactiont5-retrosynthesis"
    polytao_root = model_root / "polytao"
    for root in (ocsr_root, conditional_root, retro_root, polytao_root):
        root.mkdir(parents=True)
    (ocsr_root / "swin_base_char_aux_1m.pth").write_bytes(b"checkpoint")
    for path in required_artifact_paths(conditional_root):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"artifact")
    (retro_root / "config.json").write_text("{}", encoding="utf-8")
    (retro_root / "model.safetensors").write_bytes(b"checkpoint")
    for filename in REQUIRED_MODEL_FILES:
        (polytao_root / filename).write_bytes(b"artifact")
    return SimpleNamespace(
        ocsr_enabled=True,
        ocsr_model_dir_path=ocsr_root,
        gen_model_enabled=True,
        gen_model_dir_path=conditional_root,
        retro_model_enabled=True,
        retro_model_id=str(retro_root),
        polytao_enabled=True,
        polytao_model_dir_path=polytao_root,
        gpu_preload_mode="required",
        gpu_max_concurrent_inferences=1,
        gpu_max_waiting_inferences=8,
        gpu_sync_queue_timeout_seconds=30.0,
        gpu_async_queue_timeout_seconds=600.0,
    )


def _install_fake_torch(monkeypatch, *, capability: tuple[int, int] = (8, 9)) -> None:
    cuda = SimpleNamespace(
        is_available=lambda: True,
        get_device_capability=lambda _index: capability,
        get_device_name=lambda _index: "NVIDIA GeForce RTX 4090",
    )
    torch = SimpleNamespace(
        __version__="2.6.0+cu118",
        version=SimpleNamespace(cuda="11.8"),
        cuda=cuda,
    )
    monkeypatch.setitem(sys.modules, "torch", torch)


def test_configured_preflight_checks_exact_runtime_and_assets(tmp_path, monkeypatch) -> None:
    settings = _configured_settings(tmp_path)
    _install_fake_torch(monkeypatch)
    monkeypatch.setattr(
        gpu_preflight,
        "_distribution_version",
        lambda name: gpu_preflight.EXPECTED_VERSIONS[name],
    )
    monkeypatch.setattr(gpu_preflight, "_import_runtime_dependency", lambda _name: object())

    report = gpu_preflight.inspect_configured_runtime(settings)

    assert report["status"] == "configured"
    assert report["errors"] == []
    assert report["cuda"]["device"]["capability"] == "8.9"
    assert report["scheduler"] == {
        "web_concurrency": "1",
        "max_concurrent_inferences": 1,
        "max_waiting_inferences": 8,
        "sync_queue_timeout_seconds": 30.0,
        "async_queue_timeout_seconds": 600.0,
    }
    assert all(state["enabled"] for state in report["models"].values())


def test_configured_preflight_rejects_version_and_asset_drift(tmp_path, monkeypatch) -> None:
    settings = _configured_settings(tmp_path)
    settings.polytao_model_dir_path.joinpath("pytorch_model.bin").unlink()
    _install_fake_torch(monkeypatch)
    monkeypatch.setattr(
        gpu_preflight,
        "_distribution_version",
        lambda name: "1.9.0" if name == "scikit-learn" else gpu_preflight.EXPECTED_VERSIONS[name],
    )
    monkeypatch.setattr(gpu_preflight, "_import_runtime_dependency", lambda _name: object())

    report = gpu_preflight.inspect_configured_runtime(settings)

    assert report["status"] == "not_configured"
    assert any("scikit-learn must be 1.8.0" in error for error in report["errors"])
    assert any("pytorch_model.bin" in error for error in report["errors"])


def test_configured_preflight_uses_runtime_ocsr_checkpoint_resolver(tmp_path, monkeypatch) -> None:
    settings = _configured_settings(tmp_path)
    default_checkpoint = settings.ocsr_model_dir_path / "swin_base_char_aux_1m.pth"
    default_checkpoint.unlink()
    nested_checkpoint = settings.ocsr_model_dir_path / "release" / "model.ckpt"
    nested_checkpoint.parent.mkdir()
    nested_checkpoint.write_bytes(b"checkpoint")
    _install_fake_torch(monkeypatch)
    monkeypatch.setattr(
        gpu_preflight,
        "_distribution_version",
        lambda name: gpu_preflight.EXPECTED_VERSIONS[name],
    )
    monkeypatch.setattr(gpu_preflight, "_import_runtime_dependency", lambda _name: object())

    report = gpu_preflight.inspect_configured_runtime(settings)

    assert report["status"] == "configured"
    assert report["errors"] == []


def test_configured_preflight_rejects_required_concurrency_above_one(tmp_path, monkeypatch) -> None:
    settings = _configured_settings(tmp_path)
    settings.gpu_max_concurrent_inferences = 2
    _install_fake_torch(monkeypatch)
    monkeypatch.setattr(
        gpu_preflight,
        "_distribution_version",
        lambda name: gpu_preflight.EXPECTED_VERSIONS[name],
    )
    monkeypatch.setattr(gpu_preflight, "_import_runtime_dependency", lambda _name: object())

    report = gpu_preflight.inspect_configured_runtime(settings)

    assert report["status"] == "not_configured"
    assert any("must be 1" in error for error in report["errors"])


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("WEB_CONCURRENCY", "WEB_CONCURRENCY must be 1"),
        ("UVICORN_WORKERS", "UVICORN_WORKERS must be 1"),
    ],
)
def test_configured_preflight_rejects_multiple_backend_processes(
    tmp_path,
    monkeypatch,
    name,
    message,
) -> None:
    settings = _configured_settings(tmp_path)
    _install_fake_torch(monkeypatch)
    monkeypatch.setenv(name, "2")
    monkeypatch.setattr(
        gpu_preflight,
        "_distribution_version",
        lambda distribution: gpu_preflight.EXPECTED_VERSIONS[distribution],
    )
    monkeypatch.setattr(gpu_preflight, "_import_runtime_dependency", lambda _name: object())

    report = gpu_preflight.inspect_configured_runtime(settings)

    assert report["status"] == "not_configured"
    assert any(message in error for error in report["errors"])


def test_ready_preflight_requires_ready_enabled_models(monkeypatch) -> None:
    payload = {
        "status": "ready",
        "accepting_inferences": True,
        "max_concurrent_inferences": 1,
        "models": {
            "polytao": {"enabled": True, "ready": True},
            "disabled": {"enabled": False, "ready": False},
        },
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return json.dumps(payload).encode()

    monkeypatch.setattr(gpu_preflight, "urlopen", lambda *_args, **_kwargs: Response())

    assert gpu_preflight.inspect_ready_runtime("http://status")["status"] == "ready"


def test_ready_preflight_rejects_scheduler_that_is_not_accepting(monkeypatch) -> None:
    payload = {
        "status": "ready",
        "accepting_inferences": False,
        "max_concurrent_inferences": 1,
        "models": {"polytao": {"enabled": True, "ready": True}},
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return json.dumps(payload).encode()

    monkeypatch.setattr(gpu_preflight, "urlopen", lambda *_args, **_kwargs: Response())

    with pytest.raises(gpu_preflight.PreflightError, match="not accepting"):
        gpu_preflight.inspect_ready_runtime("http://status")
