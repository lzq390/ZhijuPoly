from __future__ import annotations

import importlib
import inspect
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from rdkit import Chem

from app.utils.exceptions import InvalidImageError, ModelArtifactError, StructureRecognitionError


ALLOWED_IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
MOLSCRIBE_CHECKPOINT_NAMES = ("swin_base_char_aux_1m.pth",)
MOLSCRIBE_CHECKPOINT_SUFFIXES = {".ckpt", ".pth", ".pt"}


@dataclass(frozen=True)
class RecognizedStructure:
    smiles: str
    molfile: str | None = None
    confidence: float | None = None
    warnings: list[str] | None = None


_RECOGNIZER_CACHE: dict[tuple[str, str], Any] = {}
_RECOGNIZER_CACHE_LOCK = Lock()


def _detect_image_type(image_bytes: bytes) -> str | None:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(image_bytes) >= 12 and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_structure_image(
    image_bytes: bytes,
    *,
    content_type: str | None = None,
    max_bytes: int,
) -> tuple[str, str]:
    if not image_bytes:
        raise InvalidImageError("image file is empty")

    if len(image_bytes) > max_bytes:
        raise InvalidImageError("image file is too large", status_code=413)

    detected_type = _detect_image_type(image_bytes)
    if detected_type is None:
        raise InvalidImageError("unsupported image type", status_code=415)

    normalized_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized_content_type and normalized_content_type not in {"application/octet-stream", detected_type}:
        if normalized_content_type not in ALLOWED_IMAGE_TYPES:
            raise InvalidImageError("unsupported image type", status_code=415)
        raise InvalidImageError("image content does not match declared type", status_code=415)

    return detected_type, ALLOWED_IMAGE_TYPES[detected_type]


def _mol_from_molfile(molfile: str) -> Chem.Mol | None:
    if not molfile.strip():
        return None
    return Chem.MolFromMolBlock(molfile, sanitize=True, removeHs=False)


def _smiles_from_mol(mol: Chem.Mol) -> str:
    return Chem.MolToSmiles(mol, canonical=True)


def _normalize_smiles(smiles: str) -> str | None:
    value = smiles.strip()
    if not value:
        return None
    mol = Chem.MolFromSmiles(value)
    if mol is None:
        return None
    return _smiles_from_mol(mol)


def _normalize_confidence(confidence: float | None, warnings: list[str]) -> float | None:
    if confidence is None:
        return None
    if 1.0 < confidence <= 100.0:
        warnings.append("recognition confidence was normalized from percentage")
        return confidence / 100.0
    if confidence < 0.0 or confidence > 1.0:
        warnings.append("recognition confidence was outside the supported range")
        return None
    return confidence


def resolve_molscribe_checkpoint(model_path: Path) -> Path:
    """Resolve the exact MolScribe checkpoint accepted by the runtime loader.

    Keep this resolver public so startup/preflight checks and the actual model
    loader cannot drift to different filename or directory rules.
    """
    resolved_model_path = model_path.expanduser().resolve()
    if not resolved_model_path.exists():
        raise ModelArtifactError(f"OCSR model path not found: {resolved_model_path}")

    if resolved_model_path.is_file():
        if resolved_model_path.suffix.lower() not in MOLSCRIBE_CHECKPOINT_SUFFIXES:
            raise ModelArtifactError(
                f"MolScribe checkpoint must be a .pth, .pt, or .ckpt file: {resolved_model_path}"
            )
        return resolved_model_path

    for checkpoint_name in MOLSCRIBE_CHECKPOINT_NAMES:
        checkpoint = resolved_model_path / checkpoint_name
        if checkpoint.is_file():
            return checkpoint.resolve()

    candidates = sorted(
        path.resolve()
        for path in resolved_model_path.rglob("*")
        if path.is_file() and path.suffix.lower() in MOLSCRIBE_CHECKPOINT_SUFFIXES
    )
    if not candidates:
        raise ModelArtifactError(
            "MolScribe checkpoint not found in OCSR model directory: "
            f"{resolved_model_path}. Place swin_base_char_aux_1m.pth there or set "
            "OCSR_MODEL_DIR to the checkpoint file."
        )
    if len(candidates) > 1:
        preview = ", ".join(str(path) for path in candidates[:5])
        raise ModelArtifactError(
            "Multiple MolScribe checkpoints were found. Set OCSR_MODEL_DIR to the exact checkpoint file. "
            f"Candidates: {preview}"
        )
    return candidates[0]


def _resolve_torch_device(device: str) -> tuple[Any, str]:
    normalized_device = (device or "auto").strip().lower()
    if normalized_device == "gpu":
        normalized_device = "cuda"

    try:
        torch = importlib.import_module("torch")
    except ImportError as exc:
        raise ModelArtifactError(
            "PyTorch is not installed. Install MolScribe and its runtime dependencies to enable image recognition."
        ) from exc

    if normalized_device == "auto":
        normalized_device = "cuda" if _cuda_is_usable(torch) else "cpu"
    elif normalized_device.startswith("cuda") and not _cuda_is_usable(torch):
        raise ModelArtifactError(f"OCSR_DEVICE={device} was requested, but CUDA is not usable")
    elif normalized_device == "mps":
        mps_backend = getattr(getattr(torch, "backends", None), "mps", None)
        if mps_backend is None or not mps_backend.is_available():
            raise ModelArtifactError("OCSR_DEVICE=mps was requested, but Apple MPS is not available")

    try:
        return torch.device(normalized_device), normalized_device
    except (RuntimeError, ValueError) as exc:
        raise ModelArtifactError(f"unsupported OCSR_DEVICE value: {device}") from exc


def _cuda_is_usable(torch_module: Any) -> bool:
    if not torch_module.cuda.is_available():
        return False
    try:
        # Allocation can succeed even when the installed CUDA build cannot run
        # convolution kernels for the local GPU architecture.
        torch_module.empty(1, device="cuda")
        sample = torch_module.empty((1, 1, 3, 3), device="cuda")
        kernel = torch_module.ones((1, 1, 1, 1), device="cuda")
        torch_module.nn.functional.conv2d(sample, kernel)
        torch_module.cuda.synchronize()
    except Exception:
        return False
    return True


def _normalize_result(raw_result: Any) -> RecognizedStructure:
    molfile: str | None = None
    smiles: str | None = None
    confidence: float | None = None
    warnings: list[str] = []

    if isinstance(raw_result, str):
        smiles = raw_result
    elif isinstance(raw_result, dict):
        molfile = next(
            (
                str(raw_result[key])
                for key in ("molfile", "mol_block", "molblock", "sdf", "mol")
                if raw_result.get(key)
            ),
            None,
        )
        smiles = next(
            (
                str(raw_result[key])
                for key in ("smiles", "smiles_string", "canonical_smiles", "prediction", "predicted_smiles")
                if raw_result.get(key)
            ),
            None,
        )
        raw_confidence = raw_result.get("confidence", raw_result.get("score"))
        if raw_confidence is not None:
            try:
                confidence = float(raw_confidence)
            except (TypeError, ValueError):
                warnings.append("recognition confidence could not be parsed")
    elif isinstance(raw_result, (list, tuple)) and raw_result:
        if isinstance(raw_result[0], dict):
            result = _normalize_result(raw_result[0])
            if len(raw_result) > 1 and result.confidence is None:
                try:
                    result_warnings = list(result.warnings or [])
                    return RecognizedStructure(
                        smiles=result.smiles,
                        molfile=result.molfile,
                        confidence=_normalize_confidence(float(raw_result[1]), result_warnings),
                        warnings=result_warnings,
                    )
                except (TypeError, ValueError):
                    return result
            return result
        if isinstance(raw_result[0], str):
            smiles = raw_result[0]
        if len(raw_result) > 1:
            try:
                confidence = float(raw_result[1])
            except (TypeError, ValueError):
                pass
    else:
        raise StructureRecognitionError("recognition did not return a structure")

    mol_from_block = _mol_from_molfile(molfile) if molfile else None
    normalized_smiles = _normalize_smiles(smiles) if smiles else None

    if molfile and mol_from_block is None:
        warnings.append("recognized molfile could not be validated by RDKit; SMILES fallback is available")

    if normalized_smiles is None and mol_from_block is not None:
        normalized_smiles = _smiles_from_mol(mol_from_block)

    if normalized_smiles is None:
        raise StructureRecognitionError("recognition result is not a valid chemical structure")

    confidence = _normalize_confidence(confidence, warnings)

    return RecognizedStructure(
        smiles=normalized_smiles,
        molfile=molfile if molfile else None,
        confidence=confidence,
        warnings=warnings,
    )


def _call_with_supported_kwargs(function: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(*args, **kwargs)

    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
        return function(*args, **kwargs)

    filtered_kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
    return function(*args, **filtered_kwargs)


def _build_molscribe_recognizer(checkpoint_path: Path, torch_device: Any, device_label: str) -> Any:
    try:
        module = importlib.import_module("molscribe")
    except ImportError as exc:
        raise ModelArtifactError(
            f"MolScribe runtime could not be imported: {exc}. Install MolScribe to enable image recognition."
        ) from exc

    recognizer_class = getattr(module, "MolScribe", None)
    if recognizer_class is None:
        try:
            interface_module = importlib.import_module("molscribe.interface")
            recognizer_class = getattr(interface_module, "MolScribe", None)
        except ImportError:
            recognizer_class = None

    if recognizer_class is None:
        if hasattr(module, "predict_image_file") or hasattr(module, "predict_image"):
            return module
        raise ModelArtifactError("MolScribe runtime does not expose a supported recognizer API")

    constructor_attempts = (
        ((str(checkpoint_path),), {"device": torch_device}),
        ((str(checkpoint_path),), {"device": device_label}),
        ((), {"model_path": str(checkpoint_path), "device": torch_device}),
        ((), {"model_path": str(checkpoint_path), "device": device_label}),
        ((str(checkpoint_path),), {}),
    )
    last_error: Exception | None = None
    for args, kwargs in constructor_attempts:
        try:
            return _call_with_supported_kwargs(recognizer_class, *args, **kwargs)
        except Exception as exc:  # pragma: no cover - depends on optional MolScribe versions
            last_error = exc

    raise ModelArtifactError(f"failed to initialize MolScribe recognizer: {last_error}")


def _get_recognizer(model_path: Path, device: str) -> Any:
    checkpoint_path = resolve_molscribe_checkpoint(model_path)
    torch_device, device_label = _resolve_torch_device(device)
    cache_key = (str(checkpoint_path), device_label)
    recognizer = _RECOGNIZER_CACHE.get(cache_key)
    if recognizer is None:
        with _RECOGNIZER_CACHE_LOCK:
            recognizer = _RECOGNIZER_CACHE.get(cache_key)
            if recognizer is None:
                recognizer = _build_molscribe_recognizer(checkpoint_path, torch_device, device_label)
                _RECOGNIZER_CACHE[cache_key] = recognizer
    return recognizer


def load_image_recognition_runtime(model_path: Path, device: str) -> Any:
    """Load and return the cached MolScribe runtime used by inference requests."""
    return _get_recognizer(model_path, device)


def _predict_with_recognizer(recognizer: Any, image_path: Path) -> Any:
    method = getattr(recognizer, "predict_image_file", None)
    if method is not None:
        return _call_prediction_method(
            method,
            "predict_image_file",
            ((str(image_path),), {"return_atoms_bonds": True, "return_confidence": True}),
            ((str(image_path),), {}),
        )

    method = getattr(recognizer, "predict_image_files", None)
    if method is not None:
        result = _call_prediction_method(
            method,
            "predict_image_files",
            (([str(image_path)],), {"return_atoms_bonds": True, "return_confidence": True}),
            (([str(image_path)],), {}),
        )
        if isinstance(result, list) and result:
            return result[0]
        return result

    method = getattr(recognizer, "predict_image", None)
    if method is not None:
        try:
            cv2 = importlib.import_module("cv2")
            image = cv2.imread(str(image_path))
            if image is None:
                raise ValueError(f"image could not be read: {image_path}")
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        except Exception as exc:
            raise ModelArtifactError(f"failed to prepare image for MolScribe predict_image: {exc}") from exc
        return _call_prediction_method(
            method,
            "predict_image",
            ((image,), {"return_atoms_bonds": True, "return_confidence": True}),
            ((image,), {}),
        )

    method = getattr(recognizer, "recognize", None)
    if method is not None:
        return _call_prediction_method(method, "recognize", ((str(image_path),), {}))

    raise ModelArtifactError("OCSR recognizer does not expose a supported prediction API")


def _call_prediction_method(method: Any, method_name: str, *attempts: tuple[tuple[Any, ...], dict[str, Any]]) -> Any:
    first_error: Exception | None = None
    for args, kwargs in attempts:
        try:
            return _call_with_supported_kwargs(method, *args, **kwargs)
        except Exception as exc:  # pragma: no cover - depends on optional MolScribe versions/devices
            if first_error is None:
                first_error = exc

    raise ModelArtifactError(f"MolScribe {method_name} failed: {first_error}") from first_error


def recognize_structure_image_from_bytes(
    image_bytes: bytes,
    *,
    content_type: str | None,
    model_path: Path,
    device: str,
    max_bytes: int,
    runtime: Any | None = None,
) -> RecognizedStructure:
    _detected_type, suffix = validate_structure_image(
        image_bytes,
        content_type=content_type,
        max_bytes=max_bytes,
    )
    recognizer = runtime if runtime is not None else _get_recognizer(model_path, device)

    with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
        handle.write(image_bytes)
        handle.flush()
        raw_result = _predict_with_recognizer(recognizer, Path(handle.name))

    return _normalize_result(raw_result)
