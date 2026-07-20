from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import stat
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import (
    FORBIDDEN_SOURCE_ROOTS,
    GPU_UUID_BY_INDEX,
    WorkerSettings,
    validate_dev_runtime_path,
    validate_private_dev_runtime_root,
)


SOURCE_LOCK_PATH = Path(__file__).resolve().parents[1] / "aimnet-source.lock.json"


@dataclass(frozen=True, slots=True)
class ModelSpec:
    alias: str
    file: str
    sha256: str
    registry_key: str = ""
    family: str = ""


def _load_source_lock(lock_path: Path) -> dict[str, Any]:
    if lock_path.is_symlink() or not lock_path.is_file():
        raise RuntimeError(f"AIMNet source lock must be a tracked regular file: {lock_path}")
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"unable to read AIMNet source lock: {lock_path}") from exc
    if payload.get("schema_version") != 1 or not isinstance(payload.get("models"), list):
        raise RuntimeError(f"unsupported AIMNet source lock schema: {lock_path}")
    return payload


def load_model_spec(model_name: str, lock_path: Path = SOURCE_LOCK_PATH) -> ModelSpec:
    payload = _load_source_lock(lock_path)

    matches = [
        item
        for item in payload["models"]
        if isinstance(item, dict) and item.get("alias") == model_name
    ]
    if len(matches) != 1:
        raise RuntimeError(f"AIMNet source lock must contain exactly one {model_name!r} model")
    item = matches[0]
    filename = item.get("file")
    sha256 = item.get("cache_sha256")
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise RuntimeError(f"unsafe model filename in AIMNet source lock: {filename!r}")
    if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
        raise RuntimeError(f"invalid model SHA-256 in AIMNet source lock for {model_name!r}")
    registry_key = item.get("registry_key")
    family = item.get("family")
    if not isinstance(registry_key, str) or not registry_key:
        raise RuntimeError(f"missing registry key in AIMNet source lock for {model_name!r}")
    if not isinstance(family, str) or not family:
        raise RuntimeError(f"missing family in AIMNet source lock for {model_name!r}")
    return ModelSpec(
        alias=model_name,
        file=filename,
        sha256=sha256,
        registry_key=registry_key,
        family=family,
    )


def load_model_specs(lock_path: Path = SOURCE_LOCK_PATH) -> tuple[ModelSpec, ...]:
    payload = _load_source_lock(lock_path)
    aliases = [item.get("alias") for item in payload["models"] if isinstance(item, dict)]
    if len(aliases) != 6 or len(set(aliases)) != 6 or not all(isinstance(alias, str) for alias in aliases):
        raise RuntimeError("AIMNet source lock must contain exactly six unique model aliases")
    return tuple(load_model_spec(str(alias), lock_path) for alias in aliases)


@dataclass(frozen=True, slots=True)
class RuntimeProbe:
    ready: bool
    model_loaded: bool
    model_name: str
    model_file: str
    model_sha256: str | None
    aimnet_origin: str | None
    torch_version: str | None
    cuda_runtime: str | None
    gpu_name: str | None
    visible_gpu_count: int
    logical_device: str
    loaded_at_unix: float | None
    error: str | None
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    aimnet_version: str | None = None
    aimnet_commit: str | None = None
    aimnet_wheel_sha256: str | None = None
    warp_version: str | None = None
    gpu_uuid: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AimnetRuntime:
    """Owns the single long-lived AIMNet2 calculator loaded during lifespan."""

    def __init__(self, settings: WorkerSettings):
        self.settings = settings
        self._calculator: Any | None = None
        self._calculators: dict[str, Any] = {}
        self._torch: Any | None = None
        self._model_sha256: str | None = None
        self._model_path: Path | None = None
        self._aimnet_origin: str | None = None
        self._torch_version: str | None = None
        self._cuda_runtime: str | None = None
        self._gpu_name: str | None = None
        self._gpu_uuid: str | None = None
        self._visible_gpu_count = 0
        self._loaded_at_unix: float | None = None
        self._error: str | None = None
        self._model_details: dict[str, dict[str, Any]] = {}
        self._aimnet_version: str | None = None
        self._aimnet_commit: str | None = None
        self._aimnet_wheel_sha256: str | None = None
        self._warp_version: str | None = None
        self._lock = threading.Lock()

    @property
    def calculator(self) -> Any:
        if self._calculator is None:
            raise RuntimeError("AIMNet2 runtime has not been loaded")
        return self._calculator

    def calculator_for(self, model_name: str) -> Any:
        calculator = self._calculators.get(model_name)
        if calculator is None:
            raise RuntimeError(f"AIMNet2 model is not preloaded: {model_name}")
        return calculator

    def load(self) -> None:
        with self._lock:
            if self._calculators:
                return
            self._error = None
            try:
                self._load()
            except Exception as exc:
                self._error = f"{type(exc).__name__}: {exc}"
                raise

    def _load(self) -> None:
        self._validate_isolated_runtime()
        visible_device = os.getenv("CUDA_VISIBLE_DEVICES", "").strip()
        if visible_device not in {
            self.settings.physical_gpu,
            GPU_UUID_BY_INDEX[self.settings.physical_gpu],
        }:
            raise RuntimeError(
                "executor must be launched with exactly its leased physical GPU "
                "index or UUID in CUDA_VISIBLE_DEVICES"
            )

        model_specs = (
            load_model_specs()
            if self.settings.preload_all_models
            else (load_model_spec(self.settings.model_name),)
        )
        verified_models: list[tuple[ModelSpec, Path, str]] = []
        for model_spec in model_specs:
            model_path = self.settings.aimnet_cache_dir / model_spec.file
            if not model_path.is_file():
                raise FileNotFoundError(
                    f"required local AIMNet2 model is missing: {model_path}; "
                    "the worker never downloads model assets"
                )
            if model_path.is_symlink():
                raise RuntimeError(
                    "AIMNet2 model must be an isolated regular file, not a symlink: "
                    f"{model_path}"
                )
            if self.settings.preload_all_models and model_path.stat().st_mode & (
                stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
            ):
                raise RuntimeError(f"AIMNet2 model cache file must be read-only: {model_path}")
            model_sha256 = self._sha256(model_path)
            if model_sha256 != model_spec.sha256:
                raise RuntimeError(
                    f"AIMNet2 model checksum mismatch for {model_path}: "
                    f"expected {model_spec.sha256}, got {model_sha256}"
                )
            verified_models.append((model_spec, model_path, model_sha256))

        torch = self._import_torch()
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is not available to the monomer DFT worker")
        visible_gpu_count = int(torch.cuda.device_count())
        if visible_gpu_count != 1:
            raise RuntimeError(
                "monomer DFT worker must see exactly one CUDA device; "
                f"found {visible_gpu_count}"
            )

        torch.cuda.set_device(0)
        gpu_uuid = self._cuda_device_uuid(torch)
        expected_gpu_uuid = GPU_UUID_BY_INDEX[self.settings.physical_gpu]
        if gpu_uuid != expected_gpu_uuid:
            raise RuntimeError(
                "visible cuda:0 UUID does not match the leased physical GPU: "
                f"expected {expected_gpu_uuid}, found {gpu_uuid}"
            )
        aimnet_origin, calculator_type = self._import_aimnet_calculator()
        calculators: dict[str, Any] = {}
        model_details: dict[str, dict[str, Any]] = {}
        for model_spec, model_path, model_sha256 in verified_models:
            calculator = calculator_type(str(model_path), device=self.settings.logical_device)
            if self.settings.warmup_models:
                self._warmup_calculator(calculator, torch)
            calculators[model_spec.alias] = calculator
            model_details[model_spec.alias] = {
                "alias": model_spec.alias,
                "registry_key": model_spec.registry_key,
                "family": model_spec.family,
                "file": model_spec.file,
                "sha256": model_sha256,
                "loaded": True,
                "warmed_up": self.settings.warmup_models,
            }
        torch.cuda.synchronize(0)

        self._calculators = calculators
        self._calculator = calculators[self.settings.model_name]
        self._torch = torch
        default_spec, default_path, default_sha256 = next(
            item for item in verified_models if item[0].alias == self.settings.model_name
        )
        self._model_sha256 = default_sha256
        self._model_path = default_path
        self._model_details = model_details
        source = _load_source_lock(SOURCE_LOCK_PATH).get("source", {})
        self._aimnet_version = str(source.get("package_version") or "") or None
        self._aimnet_commit = str(source.get("commit") or "") or None
        wheel_record = (
            self.settings.dev_runtime_root
            / "wheelhouse"
            / "aimnet-wheel.sha256"
        )
        if wheel_record.is_file() and not wheel_record.is_symlink():
            fields = wheel_record.read_text(encoding="utf-8").strip().split()
            if fields and re.fullmatch(r"[0-9a-f]{64}", fields[0]):
                self._aimnet_wheel_sha256 = fields[0]
        try:
            from importlib.metadata import version

            self._warp_version = version("warp-lang")
        except Exception:
            self._warp_version = None
        self._aimnet_origin = str(aimnet_origin)
        self._torch_version = str(torch.__version__)
        self._cuda_runtime = str(torch.version.cuda)
        self._gpu_name = str(torch.cuda.get_device_name(0))
        self._gpu_uuid = gpu_uuid
        self._visible_gpu_count = visible_gpu_count
        self._loaded_at_unix = time.time()

    def _warmup_calculator(self, calculator: Any, torch: Any) -> None:
        coordinates = torch.as_tensor(
            [[0.0, 0.0, 0.0], [0.9572, 0.0, 0.0], [-0.2390, 0.9270, 0.0]],
            dtype=torch.float32,
            device=self.settings.logical_device,
        )
        numbers = torch.as_tensor(
            [8, 1, 1],
            dtype=torch.long,
            device=self.settings.logical_device,
        )
        calculator(
            {
                "coord": coordinates,
                "numbers": numbers,
                "charge": torch.as_tensor(0.0, dtype=torch.float32, device=self.settings.logical_device),
                "mult": torch.as_tensor(1.0, dtype=torch.float32, device=self.settings.logical_device),
            },
            forces=True,
            validate_species=True,
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _import_torch() -> Any:
        return importlib.import_module("torch")

    @staticmethod
    def _cuda_device_uuid(torch: Any) -> str:
        raw_uuid = getattr(torch.cuda.get_device_properties(0), "uuid", None)
        if isinstance(raw_uuid, str):
            normalized = raw_uuid.strip()
            if normalized.lower().startswith("gpu-"):
                normalized = normalized[4:]
            if re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                normalized,
            ) is None:
                raise RuntimeError("CUDA reported an invalid device UUID")
            try:
                return f"GPU-{uuid.UUID(normalized)}"
            except ValueError as exc:
                raise RuntimeError("CUDA reported an invalid device UUID") from exc
        try:
            # PyTorch 2.9 exposes cudaDeviceProp.uuid as torch._C._CUuuid.
            # Its public ``bytes`` property is a vector<uint8_t>, represented
            # in Python as a 16-item list; the object itself has no buffer
            # protocol and therefore cannot be passed to bytes().
            byte_source = getattr(raw_uuid, "bytes", raw_uuid)
            if isinstance(byte_source, (bytes, bytearray, memoryview)):
                raw_bytes = bytes(byte_source)
            elif (
                isinstance(byte_source, (list, tuple))
                and len(byte_source) == 16
                and all(
                    not isinstance(value, bool)
                    and isinstance(value, int)
                    and 0 <= value <= 255
                    for value in byte_source
                )
            ):
                raw_bytes = bytes(byte_source)
            else:
                raise TypeError
            if len(raw_bytes) != 16:
                raise ValueError
            return f"GPU-{uuid.UUID(bytes=raw_bytes)}"
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError("CUDA did not expose a valid device UUID") from exc

    def _import_aimnet_calculator(self) -> tuple[Path, Any]:
        aimnet = importlib.import_module("aimnet")
        origin_raw = getattr(aimnet, "__file__", None)
        if not origin_raw:
            raise RuntimeError("unable to determine installed AIMNet package origin")
        origin = Path(origin_raw).resolve()
        for forbidden_root in FORBIDDEN_SOURCE_ROOTS:
            if origin == forbidden_root or forbidden_root in origin.parents:
                raise RuntimeError(f"AIMNet must not be imported from {forbidden_root}")
        if "site-packages" not in origin.parts and "dist-packages" not in origin.parts:
            raise RuntimeError(f"AIMNet must be installed in the isolated venv: {origin}")
        venv_root = self.settings.python.parent.parent.resolve(strict=True)
        try:
            origin.relative_to(venv_root)
        except ValueError as exc:
            raise RuntimeError(
                f"AIMNet package is outside the configured isolated venv: {origin}"
            ) from exc

        calculators = importlib.import_module("aimnet.calculators")
        return origin, calculators.AIMNet2Calculator

    def _validate_isolated_runtime(self) -> None:
        try:
            runtime_root = validate_private_dev_runtime_root(
                self.settings.dev_runtime_root
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

        expected_python = Path(os.path.abspath(self.settings.python))
        running_python = Path(os.path.abspath(sys.executable))
        if running_python != expected_python:
            raise RuntimeError(
                "worker interpreter does not match MONOMER_DFT_PYTHON: "
                f"expected {expected_python}, running {running_python}"
            )
        if not self.settings.python.is_file() or not os.access(self.settings.python, os.X_OK):
            raise RuntimeError(f"MONOMER_DFT_PYTHON is not executable: {self.settings.python}")

        paths = {
            "MONOMER_DFT_PYTHON parent": self.settings.python.parent,
            "MONOMER_DFT_WORKER_UDS parent": self.settings.uds.parent,
            "MONOMER_DFT_JOB_ROOT": self.settings.job_root,
            "AIMNET_CACHE_DIR": self.settings.aimnet_cache_dir,
            "WARP_CACHE_PATH": self.settings.warp_cache_path,
            "MONOMER_DFT_GPU_MPS_PIPE_ROOT": self.settings.mps_pipe_root,
            "MONOMER_DFT_DOWNLOAD_SPOOL_ROOT": self.settings.download_spool_root,
        }
        for name, path in paths.items():
            if path is None:
                raise RuntimeError(f"{name} is not configured")
            descriptor_bound_mps = (
                name == "MONOMER_DFT_GPU_MPS_PIPE_ROOT"
                and bool(self.settings.mps_pipe_directories)
            )
            if (
                (not descriptor_bound_mps and path.is_symlink())
                or not path.is_dir()
            ):
                raise RuntimeError(f"{name} must be a real directory: {path}")
            try:
                validate_dev_runtime_path(
                    name,
                    path,
                    runtime_root=runtime_root,
                    leaf_kind="directory",
                )
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
        if self.settings.broker_uds is not None:
            try:
                validate_dev_runtime_path(
                    "MONOMER_DFT_GPU_BROKER_UDS",
                    self.settings.broker_uds,
                    runtime_root=runtime_root,
                    leaf_kind="socket",
                )
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
        if self.settings.gpu_external_reservations is not None:
            try:
                validate_dev_runtime_path(
                    "MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS",
                    self.settings.gpu_external_reservations,
                    runtime_root=runtime_root,
                    leaf_kind="file",
                )
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc

    def close(self) -> None:
        with self._lock:
            self._calculator = None
            self._calculators.clear()
            self._model_details = {}
            if self._torch is not None and self._torch.cuda.is_available():
                self._torch.cuda.empty_cache()

    def empty_cuda_cache(self) -> None:
        with self._lock:
            if self._torch is not None and self._torch.cuda.is_available():
                self._torch.cuda.empty_cache()

    def synchronize(self) -> None:
        with self._lock:
            if self._torch is None or not self._torch.cuda.is_available():
                raise RuntimeError("CUDA runtime is not ready")
            self._torch.cuda.synchronize(0)

    def probe(self) -> RuntimeProbe:
        with self._lock:
            model_path = self._model_path
            loaded = self._calculator is not None
            return RuntimeProbe(
                ready=loaded and self._error is None,
                model_loaded=loaded,
                model_name=self.settings.model_name,
                model_file=str(model_path) if model_path is not None else "",
                model_sha256=self._model_sha256,
                aimnet_origin=self._aimnet_origin,
                torch_version=self._torch_version,
                cuda_runtime=self._cuda_runtime,
                gpu_name=self._gpu_name,
                visible_gpu_count=self._visible_gpu_count,
                logical_device=self.settings.logical_device,
                loaded_at_unix=self._loaded_at_unix,
                error=self._error,
                models={key: dict(value) for key, value in self._model_details.items()},
                aimnet_version=self._aimnet_version,
                aimnet_commit=self._aimnet_commit,
                aimnet_wheel_sha256=self._aimnet_wheel_sha256,
                warp_version=self._warp_version,
                gpu_uuid=self._gpu_uuid,
            )
