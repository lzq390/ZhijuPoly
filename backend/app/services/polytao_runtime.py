from __future__ import annotations

import importlib
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from app.services.polytao import PolytaoCandidateAccumulator


REQUIRED_MODEL_FILES: tuple[str, ...] = (
    "config.json",
    "pytorch_model.bin",
    "tokenizer.json",
    "spiece.model",
)
MAX_GENERATION_BATCH_SIZE = 2


@dataclass(frozen=True, slots=True)
class RuntimeProbe:
    model_files_ready: bool
    runtime_ready: bool
    runtime_error: str | None = None


@dataclass(slots=True)
class PolytaoGenerationResult:
    result: dict[str, Any]
    query_time_ms: float
    returned_count: int


class BackendPolytaoRuntime:
    def __init__(
        self,
        *,
        model_dir: Path,
        device: str,
        model_id: str,
        model_revision: str | None = None,
    ) -> None:
        self.model_dir = model_dir
        self.device = device.strip().lower()
        self.model_id = model_id
        self.model_revision = model_revision
        self._lock = Lock()
        self._loaded = False
        self._tokenizer: Any = None
        self._model: Any = None
        self._torch: Any = None
        self._device = "cpu"

    def probe(self) -> RuntimeProbe:
        missing = missing_model_files(self.model_dir)
        if missing:
            return RuntimeProbe(
                model_files_ready=False,
                runtime_ready=False,
                runtime_error="missing PolyTAO model files: " + ", ".join(missing),
            )
        try:
            torch = importlib.import_module("torch")
            importlib.import_module("transformers")
            importlib.import_module("rdkit")
        except Exception as exc:
            return RuntimeProbe(
                model_files_ready=True,
                runtime_ready=False,
                runtime_error=f"runtime dependency import failed: {exc}",
            )
        try:
            self._resolve_device(torch)
        except Exception as exc:
            return RuntimeProbe(model_files_ready=True, runtime_ready=False, runtime_error=str(exc))
        return RuntimeProbe(model_files_ready=True, runtime_ready=True)

    def generate(
        self,
        *,
        prompt: str,
        candidate_count: int,
        temperature: float,
        top_k: int,
        top_p: float,
        max_length: int,
    ) -> PolytaoGenerationResult:
        tokenizer, model, torch, device = self._load()
        started_at = time.perf_counter()
        requested = max(1, int(candidate_count))
        max_raw_candidates = min(max(requested * 10, requested), 100)
        sampled_count = 0
        accumulator = PolytaoCandidateAccumulator(requested_count=requested)

        while sampled_count < max_raw_candidates:
            # Decoder KV caches and logits scale with num_return_sequences.
            # A single 100-sequence, max_length=512 call consumes almost the
            # entire 24 GiB card. Preserve the public request limits while
            # sampling in bounded micro-batches inside the same GPU permit.
            batch_count = min(
                MAX_GENERATION_BATCH_SIZE,
                max_raw_candidates - sampled_count,
            )
            encoded = None
            outputs = None
            decoded: list[str] | None = None
            try:
                encoded = tokenizer(prompt, return_tensors="pt")
                encoded = {key: value.to(device) for key, value in encoded.items()}
                with torch.inference_mode():
                    outputs = model.generate(
                        **encoded,
                        do_sample=True,
                        temperature=float(temperature),
                        top_k=int(top_k),
                        top_p=float(top_p),
                        max_length=int(max_length),
                        num_return_sequences=batch_count,
                        pad_token_id=tokenizer.pad_token_id,
                        eos_token_id=tokenizer.eos_token_id,
                    )
                decoded = [
                    tokenizer.decode(output, skip_special_tokens=True)
                    for output in outputs
                ]
            finally:
                del outputs
                del encoded
                _release_cuda_cache(torch, device)
            if not decoded:
                raise RuntimeError("PolyTAO model returned no decoded candidates")
            sampled_count += len(decoded)
            accumulator.add(decoded)
            if accumulator.complete:
                break

        accepted, filters = accumulator.result()
        query_time_ms = (time.perf_counter() - started_at) * 1000
        result = {
            "prompt": prompt,
            "query_time_ms": query_time_ms,
            "requested_count": requested,
            "returned_count": len(accepted),
            # Keep the historical public attempts contract stable. Internal
            # decoder micro-batches are an implementation detail.
            "attempts": 1,
            "filter_counter": filters,
            "results": [
                {
                    "rank": candidate.rank,
                    "generated_smiles": candidate.generated_smiles,
                    "raw_smiles": candidate.raw_smiles,
                    "valid_smiles": candidate.valid_smiles,
                    "sa_score": candidate.sa_score,
                    "warnings": candidate.warnings,
                }
                for candidate in accepted
            ],
        }
        return PolytaoGenerationResult(
            result=result,
            query_time_ms=query_time_ms,
            returned_count=len(accepted),
        )

    def ensure_loaded(self) -> "BackendPolytaoRuntime":
        self._load()
        return self

    def warmup(self) -> None:
        """Run one minimal local generation without creating a business job."""

        tokenizer, model, torch, device = self._load()
        encoded = tokenizer("C", return_tensors="pt")
        encoded = {key: value.to(device) for key, value in encoded.items()}
        with torch.inference_mode():
            model.generate(
                **encoded,
                max_new_tokens=1,
                num_return_sequences=1,
                pad_token_id=tokenizer.pad_token_id,
                eos_token_id=tokenizer.eos_token_id,
            )
        if str(device).startswith("cuda"):
            torch.cuda.synchronize()

    @property
    def loaded(self) -> bool:
        return self._loaded

    def _load(self) -> tuple[Any, Any, Any, str]:
        if self._loaded:
            return self._tokenizer, self._model, self._torch, self._device
        with self._lock:
            if self._loaded:
                return self._tokenizer, self._model, self._torch, self._device
            missing = missing_model_files(self.model_dir)
            if missing:
                raise RuntimeError("missing PolyTAO model files: " + ", ".join(missing))

            torch = importlib.import_module("torch")
            transformers = importlib.import_module("transformers")
            device = self._resolve_device(torch)
            tokenizer = transformers.AutoTokenizer.from_pretrained(
                str(self.model_dir),
                local_files_only=True,
            )
            model = transformers.AutoModelForSeq2SeqLM.from_pretrained(
                str(self.model_dir),
                local_files_only=True,
            )
            model.to(device)
            model.eval()

            self._tokenizer = tokenizer
            self._model = model
            self._torch = torch
            self._device = device
            self._loaded = True
            return tokenizer, model, torch, device

    def _resolve_device(self, torch: Any | None = None) -> str:
        selected = self.device
        if torch is None:
            torch = importlib.import_module("torch")
        if selected in {"", "auto"}:
            return "cuda" if torch.cuda.is_available() else "cpu"
        if selected.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError(f"POLYTAO_DEVICE={selected} requested but CUDA is not available")
        if selected.startswith("cuda"):
            return selected
        if selected == "cpu":
            return selected
        if selected == "mps":
            mps_backend = getattr(getattr(torch, "backends", None), "mps", None)
            if mps_backend is None or not mps_backend.is_available():
                raise RuntimeError("POLYTAO_DEVICE=mps requested but MPS is not available")
            return selected
        raise RuntimeError(f"unsupported POLYTAO_DEVICE value: {selected}")


def missing_model_files(model_dir: Path) -> list[str]:
    return [filename for filename in REQUIRED_MODEL_FILES if not (model_dir / filename).is_file()]


def _release_cuda_cache(torch: Any, device: object) -> None:
    if not str(device).startswith("cuda"):
        return
    try:
        torch.cuda.empty_cache()
    except Exception:
        # Cleanup must never replace the original encode/generate/decode
        # exception. A subsequent call will classify any fatal CUDA state.
        pass
