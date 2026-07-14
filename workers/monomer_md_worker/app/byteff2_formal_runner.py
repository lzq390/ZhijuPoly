from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .byteff2_env import ByteFF2SubprocessEnvironment, build_byteff2_environment
from .config import WorkerSettings
from .formal_protocols import (
    estimate_requested_steps,
    required_result_file,
    sanitize_formal_config,
)
from .models import JobRequest
from .process_control import create_fenced_subprocess_exec, wait_for_process_group
from gpu_resource import ManagedGpuLease, mps_client_environment


class FormalProtocolRunResult:
    def __init__(self, *, result: dict[str, Any], completed_steps: int) -> None:
        self.result = result
        self.completed_steps = completed_steps


class ByteFF2FormalRunner:
    def __init__(
        self,
        settings: WorkerSettings,
        *,
        environment: ByteFF2SubprocessEnvironment | None = None,
    ) -> None:
        self._settings = settings
        self._environment = environment or build_byteff2_environment(settings)

    async def run(
        self,
        request: JobRequest,
        output_dir: Path,
        *,
        execution_lease: ManagedGpuLease | None = None,
    ) -> FormalProtocolRunResult:
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

        environment = self._environment
        if protocol == "Transport" and environment.transport_error is not None:
            raise RuntimeError(environment.transport_error)
        env = environment.as_dict()
        if self._settings.gpu_broker_enabled and execution_lease is None:
            raise RuntimeError("Broker-governed MD execution requires an active GPU lease")
        if execution_lease is not None:
            env.update(
                mps_client_environment(
                    execution_lease.lease,
                    pipe_root=self._settings.gpu_mps_pipe_root,
                )
            )
            result_gpu_device = str(execution_lease.lease.gpu_index)
        else:
            env["CUDA_MPS_ACTIVE_THREAD_PERCENTAGE"] = "50"
            result_gpu_device = self._settings.cuda_visible_devices

        stdout_path = output_dir / "worker_stdout.log"
        stderr_path = output_dir / "worker_stderr.log"
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = await create_fenced_subprocess_exec(
                (
                    self._settings.byteff2_python,
                    str(run_md_path),
                    "--config",
                    str(config_path),
                ),
                execution_lease=execution_lease,
                cwd=output_dir,
                env=env,
                stdout=stdout,
                stderr=stderr,
            )
            process_group_id = process.pid
            try:
                return_code = await wait_for_process_group(
                    process,
                    timeout_seconds=self._settings.formal_timeout_seconds,
                    execution_lease=execution_lease,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    f"ByteFF2 {protocol} timed out after {self._settings.formal_timeout_seconds}s"
                ) from exc
        if return_code != 0:
            raise RuntimeError(
                f"ByteFF2 {protocol} failed with exit code {return_code}; "
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
        byteff2_git_sha = resolve_byteff2_commit(self._settings.byteff2_root)
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
            "gpu_device": result_gpu_device,
            "physical_result": True,
        }
        if execution_lease is not None:
            result.update(_lease_provenance(execution_lease))
        _write_json(output_dir / "formal_results.json", result)
        artifact_manifest = _artifact_manifest(output_dir)
        result["artifact_manifest"] = artifact_manifest
        result["artifacts"] = _frontend_artifacts(artifact_manifest)
        _write_json(output_dir / "formal_results.json", result)
        return FormalProtocolRunResult(result=result, completed_steps=completed_steps)


def _lease_provenance(execution_lease: ManagedGpuLease) -> dict[str, Any]:
    lease = execution_lease.lease
    return {
        "execution_path": "broker",
        "gpu_uuid": lease.gpu_uuid,
        "gpu_budget_mib": lease.memory_mib,
        "gpu_thread_percent": lease.thread_percent,
        "gpu_lease_id": lease.lease_id,
        "gpu_fencing_token": lease.fencing_token,
        "gpu_broker_instance_id": lease.broker_instance_id,
        "gpu_preferred_device": lease.preferred,
    }


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


def resolve_byteff2_commit(root: Path) -> str | None:
    marker = root / "BYTEFF2-COMMIT"
    if marker.exists():
        if not marker.is_file() or marker.is_symlink():
            raise RuntimeError("BYTEFF2-COMMIT must be a regular file")
        commit = marker.read_text(encoding="ascii").strip()
        if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
            raise RuntimeError("BYTEFF2-COMMIT must contain a full lowercase commit SHA")
        return commit
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
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
