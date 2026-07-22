from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
import warnings

from app.config import Settings
from app.services.conditional_generation_runtime import required_artifact_paths
from app.services.image_recognition import resolve_molscribe_checkpoint
from app.services.polytao_runtime import REQUIRED_MODEL_FILES


EXPECTED_VERSIONS = {
    "torch": "2.6.0+cu124",
    "torchvision": "0.21.0+cu124",
    "transformers": "4.57.6",
    "scikit-learn": "1.8.0",
}
EXPECTED_CUDA_RUNTIME = "12.4"
EXPECTED_CAPABILITY = (8, 9)
DEFAULT_STATUS_URL = "http://127.0.0.1:8000/internal/gpu/status"


class PreflightError(RuntimeError):
    pass


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise PreflightError(f"required distribution is not installed: {name}") from exc


def _import_runtime_dependency(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except Exception as exc:
        raise PreflightError(f"required runtime module cannot be imported: {name}: {exc}") from exc


def _required_file(path: Path, errors: list[str]) -> None:
    if not path.is_file() or path.is_symlink():
        errors.append(f"required model asset is missing or is not a regular file: {path}")


def _required_directory(path: Path, errors: list[str]) -> None:
    if not path.is_dir() or path.is_symlink():
        errors.append(f"required model asset directory is missing or invalid: {path}")


def inspect_configured_runtime(
    settings: Settings,
    *,
    require_cuda: bool = True,
) -> dict[str, Any]:
    errors: list[str] = []
    process_concurrency = os.getenv("WEB_CONCURRENCY", "1")
    uvicorn_workers = os.getenv("UVICORN_WORKERS", "1")
    if process_concurrency != "1":
        errors.append(
            f"WEB_CONCURRENCY must be 1 for the process-local GPU scheduler, found {process_concurrency}"
        )
    if uvicorn_workers != "1":
        errors.append(
            f"UVICORN_WORKERS must be 1 for the process-local GPU scheduler, found {uvicorn_workers}"
        )
    if (
        settings.gpu_preload_mode == "required"
        and settings.gpu_max_concurrent_inferences != 1
    ):
        errors.append(
            "GPU_MAX_CONCURRENT_INFERENCES must be 1 when GPU_PRELOAD_MODE=required"
        )
    versions: dict[str, str] = {}
    for distribution, expected in EXPECTED_VERSIONS.items():
        try:
            actual = _distribution_version(distribution)
        except PreflightError as exc:
            errors.append(str(exc))
            continue
        versions[distribution] = actual
        if actual != expected:
            errors.append(f"{distribution} must be {expected}, found {actual}")
    broker_managed = bool(getattr(settings, "gpu_broker_enabled", False))
    if not broker_managed:
        for module_name in ("torchvision", "transformers", "sklearn"):
            try:
                _import_runtime_dependency(module_name)
            except PreflightError as exc:
                errors.append(str(exc))

    cuda: dict[str, Any] = {
        "available": False,
        "runtime": None,
        "device": None,
        "managed_by_broker": broker_managed,
    }
    if broker_managed:
        # This command is used by Docker health checks in a process separate
        # from the lease-owning Backend.  Importing torch/torchvision or
        # calling any CUDA API here would create an unleased MPS client.  CUDA
        # readiness is therefore proven only by the main-process status API.
        cuda["inspection"] = "deferred_to_lease_owner"
    elif require_cuda:
        try:
            import torch

            cuda["runtime"] = torch.version.cuda
            cuda["available"] = bool(torch.cuda.is_available())
            if torch.__version__ != EXPECTED_VERSIONS["torch"]:
                errors.append(
                    f"imported torch must be {EXPECTED_VERSIONS['torch']}, found {torch.__version__}"
                )
            if torch.version.cuda != EXPECTED_CUDA_RUNTIME:
                errors.append(
                    f"CUDA runtime must be {EXPECTED_CUDA_RUNTIME}, found {torch.version.cuda or 'none'}"
                )
            if not torch.cuda.is_available():
                errors.append("CUDA is not available")
            else:
                capability = tuple(int(value) for value in torch.cuda.get_device_capability(0))
                name = torch.cuda.get_device_name(0)
                cuda["device"] = {
                    "index": 0,
                    "name": name,
                    "capability": f"{capability[0]}.{capability[1]}",
                }
                if capability != EXPECTED_CAPABILITY:
                    errors.append(
                        "GPU capability must be "
                        f"{EXPECTED_CAPABILITY[0]}.{EXPECTED_CAPABILITY[1]}, found "
                        f"{capability[0]}.{capability[1]}"
                    )
                if "RTX 4090" not in name.upper():
                    errors.append(f"GPU must be an RTX 4090, found {name}")
        except Exception as exc:
            errors.append(f"CUDA runtime inspection failed: {exc}")
    else:
        cuda["inspection"] = "disabled_by_policy"

    models = {
        "ocsr": settings.ocsr_enabled,
        "conditional_generation": settings.gen_model_enabled,
        "retrosynthesis": settings.retro_model_enabled,
        "polytao": settings.polytao_enabled,
    }
    if settings.ocsr_enabled:
        try:
            resolve_molscribe_checkpoint(settings.ocsr_model_dir_path)
        except Exception as exc:
            errors.append(str(exc))
    if settings.gen_model_enabled:
        for path in required_artifact_paths(settings.gen_model_dir_path):
            _required_file(path, errors)
    if settings.retro_model_enabled:
        retro_root = Path(settings.retro_model_id)
        _required_directory(retro_root, errors)
        if retro_root.is_dir():
            _required_file(retro_root / "config.json", errors)
            if not any(
                candidate.is_file()
                for pattern in ("*.safetensors", "pytorch_model*.bin")
                for candidate in retro_root.glob(pattern)
            ):
                errors.append(f"retrosynthesis checkpoint is missing from {retro_root}")
    if settings.polytao_enabled:
        for filename in REQUIRED_MODEL_FILES:
            _required_file(settings.polytao_model_dir_path / filename, errors)

    return {
        "status": "configured" if not errors else "not_configured",
        "build_revision": os.getenv("BUILD_REVISION", "unknown"),
        "versions": versions,
        "cuda": cuda,
        "scheduler": {
            "web_concurrency": process_concurrency,
            "max_concurrent_inferences": settings.gpu_max_concurrent_inferences,
            "max_waiting_inferences": settings.gpu_max_waiting_inferences,
            "sync_queue_timeout_seconds": settings.gpu_sync_queue_timeout_seconds,
            "async_queue_timeout_seconds": settings.gpu_async_queue_timeout_seconds,
        },
        "models": {name: {"enabled": enabled} for name, enabled in models.items()},
        "errors": errors,
    }


def inspect_disabled_runtime(settings: Settings) -> dict[str, Any]:
    report = inspect_configured_runtime(settings, require_cuda=False)
    errors = report["errors"]
    enabled_models = [
        name
        for name, state in report["models"].items()
        if state.get("enabled")
    ]
    if bool(getattr(settings, "model_enabled", False)):
        enabled_models.append("property_prediction")
    if enabled_models:
        errors.append(
            "CPU-only runtime requires all model entry points disabled: "
            + ", ".join(sorted(enabled_models))
        )
    if bool(getattr(settings, "gpu_broker_enabled", False)):
        errors.append("CPU-only runtime requires GPU Broker disabled")
    if settings.gpu_preload_mode != "lazy":
        errors.append("CPU-only runtime requires GPU_PRELOAD_MODE=lazy")
    report["status"] = "disabled" if not errors else "not_disabled"
    return report


def verify_serialized_assets(settings: Settings) -> dict[str, Any]:
    import joblib
    import numpy as np
    from sklearn.exceptions import InconsistentVersionWarning

    from app.services.predictor import PROPERTY_MODELS

    checked: list[str] = []
    with warnings.catch_warnings():
        warnings.simplefilter("error", InconsistentVersionWarning)
        for filename in PROPERTY_MODELS.values():
            path = settings.model_dir_path / filename
            model = joblib.load(path)
            feature_count = int(getattr(model, "n_features_in_", 2048))
            prediction = model.predict(np.zeros((1, feature_count), dtype=np.float32))
            value = float(np.asarray(prediction).reshape(-1)[0])
            if not math.isfinite(value):
                raise PreflightError(f"RF model produced a non-finite prediction: {path}")
            checked.append(str(path))

        scaler_path = settings.gen_model_dir_path / "tg_scaler.pkl"
        scaler = joblib.load(scaler_path)
        feature_count = int(getattr(scaler, "n_features_in_", 10))
        transformed = np.asarray(
            scaler.transform(np.zeros((1, feature_count), dtype=np.float32)),
            dtype=float,
        )
        if not np.isfinite(transformed).all():
            raise PreflightError(f"conditional-generation scaler produced non-finite values: {scaler_path}")
        checked.append(str(scaler_path))
    return {"checked": checked, "count": len(checked)}


def inspect_ready_runtime(
    status_url: str,
    *,
    require_broker: bool = False,
) -> dict[str, Any]:
    request = Request(status_url, headers={"Cache-Control": "no-store"})
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except Exception as exc:
        raise PreflightError(f"GPU status endpoint is unavailable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("status") != "ready":
        actual = payload.get("status") if isinstance(payload, dict) else "invalid-response"
        raise PreflightError(f"GPU registry is not ready: {actual}")
    if payload.get("accepting_inferences") is not True:
        raise PreflightError("GPU scheduler is not accepting inference work")
    if payload.get("max_concurrent_inferences") != 1:
        raise PreflightError("production GPU concurrency must be exactly 1")
    models = payload.get("models")
    if not isinstance(models, dict):
        raise PreflightError("GPU status response does not contain model states")
    not_ready = [
        name
        for name, state in models.items()
        if isinstance(state, dict) and state.get("enabled") and not state.get("ready")
    ]
    if not_ready:
        raise PreflightError("enabled GPU runtimes are not ready: " + ", ".join(sorted(not_ready)))
    if require_broker:
        broker = payload.get("resource_broker")
        lease = broker.get("lease") if isinstance(broker, dict) else None
        if (
            not isinstance(broker, dict)
            or broker.get("enabled") is not True
            or broker.get("connectivity") != "healthy"
            or not isinstance(lease, dict)
            or lease.get("status") != "active"
        ):
            raise PreflightError(
                "GPU registry is not backed by a healthy active residency lease"
            )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the unified backend GPU runtime.")
    parser.add_argument(
        "--mode",
        choices=("disabled", "configured", "ready"),
        default="configured",
    )
    parser.add_argument("--status-url", default=DEFAULT_STATUS_URL)
    parser.add_argument("--verify-serialized-assets", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings()
    report = (
        inspect_disabled_runtime(settings)
        if args.mode == "disabled"
        else inspect_configured_runtime(settings)
    )
    try:
        if args.verify_serialized_assets:
            report["serialized_assets"] = verify_serialized_assets(settings)
        if args.mode == "ready" and not report["errors"]:
            report["registry"] = inspect_ready_runtime(
                args.status_url,
                require_broker=bool(getattr(settings, "gpu_broker_enabled", False)),
            )
    except Exception as exc:
        report["errors"].append(str(exc))
    if args.mode == "ready" and "registry" in report:
        report["status"] = "ready"
    elif report["errors"]:
        report["status"] = {
            "disabled": "not_disabled",
            "configured": "not_configured",
            "ready": "not_ready",
        }[args.mode]
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
