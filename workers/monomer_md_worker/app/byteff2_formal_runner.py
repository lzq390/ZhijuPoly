from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .config import WorkerSettings
from .formal_protocols import (
    estimate_requested_steps,
    required_result_file,
    sanitize_formal_config,
)
from .models import JobRequest


class FormalProtocolRunResult:
    def __init__(self, *, result: dict[str, Any], completed_steps: int) -> None:
        self.result = result
        self.completed_steps = completed_steps


class ByteFF2FormalRunner:
    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings

    def run(self, request: JobRequest, output_dir: Path) -> FormalProtocolRunResult:
        protocol = request.protocol
        if request.config_json is None:
            raise RuntimeError("formal ByteFF2 jobs require config_json")
        if not self._settings.byteff2_root.exists():
            raise RuntimeError(f"ByteFF2 root does not exist: {self._settings.byteff2_root}")

        output_dir.mkdir(parents=True, exist_ok=True)
        final_config = sanitize_formal_config(request.config_json, protocol, str(output_dir))
        config_path = output_dir / "config.json"
        _write_json(config_path, final_config)

        run_md_path = self._settings.byteff2_root / "example" / "4_MD_simulations" / "run_md.py"
        if not run_md_path.exists():
            raise RuntimeError(f"ByteFF2 run_md.py was not found: {run_md_path}")

        env = os.environ.copy()
        env["BYTEFF2_ROOT"] = str(self._settings.byteff2_root)
        env["CUDA_VISIBLE_DEVICES"] = self._settings.cuda_visible_devices
        env["PYTHONPATH"] = os.pathsep.join(
            [str(self._settings.byteff2_root), env.get("PYTHONPATH", "")]
        ).strip(os.pathsep)

        stdout_path = output_dir / "worker_stdout.log"
        stderr_path = output_dir / "worker_stderr.log"
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            try:
                completed = subprocess.run(
                    [self._settings.byteff2_python, str(run_md_path), "--config", str(config_path)],
                    cwd=output_dir,
                    env=env,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=self._settings.formal_timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"ByteFF2 {protocol} timed out after {self._settings.formal_timeout_seconds}s"
                ) from exc
        if completed.returncode != 0:
            raise RuntimeError(
                f"ByteFF2 {protocol} failed with exit code {completed.returncode}; "
                f"see {stdout_path.name} and {stderr_path.name}"
            )

        result_path = output_dir / "outputs" / required_result_file(protocol)
        if not result_path.exists():
            raise RuntimeError(
                f"ByteFF2 {protocol} completed but did not produce required result file: "
                f"{result_path.relative_to(output_dir)}"
            )
        with result_path.open("r", encoding="utf-8") as handle:
            raw_result: Any = json.load(handle)
        if not isinstance(raw_result, dict):
            raise RuntimeError(f"ByteFF2 {protocol} result file must contain a JSON object")

        summary = _summary_from_result(raw_result)
        artifact_manifest = _artifact_manifest(output_dir)
        byteff2_git_sha = _git_sha(self._settings.byteff2_root)
        completed_steps = estimate_requested_steps(protocol, final_config)
        result = {
            "job_id": request.job_id,
            "protocol": protocol,
            "run_mode": "formal",
            "config": _public_config(final_config, output_dir),
            "metrics": raw_result,
            "summary": summary,
            "result_file": str(result_path.relative_to(output_dir)),
            "artifact_manifest": artifact_manifest,
            "artifacts": _frontend_artifacts(artifact_manifest),
            "byteff2_git_sha": byteff2_git_sha,
            "gpu_device": self._settings.cuda_visible_devices,
            "physical_result": True,
        }
        _write_json(output_dir / "formal_results.json", result)
        artifact_manifest = _artifact_manifest(output_dir)
        result["artifact_manifest"] = artifact_manifest
        result["artifacts"] = _frontend_artifacts(artifact_manifest)
        _write_json(output_dir / "formal_results.json", result)
        return FormalProtocolRunResult(result=result, completed_steps=completed_steps)


def _summary_from_result(result: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    units = result.get("units") if isinstance(result.get("units"), dict) else {}
    for key, value in result.items():
        if key == "units":
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            continue
        summary[key] = value
        unit = units.get(key)
        if isinstance(unit, str) and unit:
            summary[f"{key}_unit"] = unit
    return summary


def _artifact_manifest(root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root)
        files.append(
            {
                "name": relative.name,
                "path": str(relative),
                "kind": path.suffix.lstrip(".") or "file",
                "size_bytes": path.stat().st_size,
            }
        )
    return {"root": str(root), "files": files, "deleted": False}


def _frontend_artifacts(manifest: dict[str, Any]) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for item in manifest.get("files", []):
        if not isinstance(item, dict):
            continue
        key = str(item.get("path") or item.get("name") or f"artifact-{len(artifacts) + 1}")
        artifacts[key] = item
    return artifacts


def _public_config(config: dict[str, Any], root: Path) -> dict[str, Any]:
    public = dict(config)
    for field in ("params_dir", "output_dir", "working_dir"):
        value = public.get(field)
        if isinstance(value, str):
            try:
                public[field] = str(Path(value).relative_to(root))
            except ValueError:
                public[field] = value
    return public


def _git_sha(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
    tmp_path.replace(path)
