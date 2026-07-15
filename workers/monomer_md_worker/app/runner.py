from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import WorkerSettings
from .byteff2_formal_runner import ByteFF2FormalRunner, resolve_byteff2_commit
from .models import JobRequest
from .process_control import create_fenced_subprocess_exec, wait_for_process_group
from gpu_resource import (
    GpuBrokerClient,
    GpuBrokerClientError,
    ManagedGpuLease,
    mps_client_environment,
)

NOT_PHYSICAL_WARNING = (
    "Density demo output is not equilibrated and is not a physical density estimate."
)


@dataclass(frozen=True)
class DemoRunResult:
    result: dict[str, Any]
    output_dir: Path
    completed_steps: int


class MonomerMdRunner:
    def __init__(
        self,
        settings: WorkerSettings,
        *,
        gpu_broker_client: GpuBrokerClient | None = None,
    ) -> None:
        self._settings = settings
        self._formal_runner = ByteFF2FormalRunner(settings)
        self._gpu_broker_client = gpu_broker_client
        if settings.gpu_broker_enabled and self._gpu_broker_client is None:
            self._gpu_broker_client = GpuBrokerClient(settings.gpu_broker_socket_path)

    async def acquire_execution_lease(self, job_id: str) -> ManagedGpuLease | None:
        if self._settings.mode != "real" or not self._settings.gpu_broker_enabled:
            return None
        if self._gpu_broker_client is None:  # pragma: no cover - constructor invariant
            raise RuntimeError("GPU broker client is unavailable")
        job_token = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16]
        client_id = f"md-{self._settings.gpu_broker_environment}-{job_token}"
        request_id = f"md:{self._settings.gpu_broker_environment}:{job_token}"
        # One long-lived Broker waiter preserves global Prod-before-Dev FIFO.
        # Repeated zero-timeout polling would create a fresh queue sequence and
        # can starve or leapfrog other governed workloads.
        acquire_task = asyncio.create_task(
            asyncio.to_thread(
                self._gpu_broker_client.acquire_managed,
                kind="execution",
                placement="any",
                component="md",
                environment=self._settings.gpu_broker_environment,
                client_id=client_id,
                memory_mib=8_192,
                thread_percent=50,
                wait_timeout_seconds=self._settings.gpu_broker_wait_timeout_seconds,
                heartbeat_interval_seconds=self._settings.gpu_broker_heartbeat_interval_seconds,
                request_id=request_id,
            )
        )
        try:
            managed = await asyncio.shield(acquire_task)
        except asyncio.CancelledError:
            # Explicitly cancel the stable waiter, then collect a raced
            # allocation before propagating cancellation.  No reservation is
            # allowed to become an unobserved background-thread result.
            try:
                await asyncio.to_thread(
                    self._gpu_broker_client.cancel_acquire, request_id
                )
            except GpuBrokerClientError:
                pass
            try:
                acquired = await asyncio.shield(acquire_task)
            except GpuBrokerClientError:
                pass
            else:
                await asyncio.to_thread(acquired.close)
            raise
        gpu_uuids = {
            1: "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
            2: "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe",
            3: "GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5",
        }
        policy = (
            (2, 3, 1)
            if self._settings.gpu_broker_environment == "prod"
            else (1, 3)
        )
        lease_payload = managed.lease
        valid_identity = (
            isinstance(getattr(lease_payload, "lease_id", None), str)
            and bool(lease_payload.lease_id)
            and isinstance(getattr(lease_payload, "fencing_token", None), int)
            and not isinstance(lease_payload.fencing_token, bool)
            and lease_payload.fencing_token > 0
            and isinstance(getattr(lease_payload, "broker_instance_id", None), str)
            and bool(lease_payload.broker_instance_id)
        )
        expected_metadata = {
            "kind": "execution",
            "placement": "any",
            "component": "md",
            "environment": self._settings.gpu_broker_environment,
            "client_id": client_id,
            "memory_mib": 8_192,
            "thread_percent": 50,
            "parent_lease_id": None,
            "status": "active",
        }
        gpu_index = getattr(lease_payload, "gpu_index", None)
        valid_gpu = (
            isinstance(gpu_index, int)
            and not isinstance(gpu_index, bool)
            and gpu_index in policy
            and getattr(lease_payload, "gpu_uuid", None) == gpu_uuids[gpu_index]
            and getattr(lease_payload, "preferred", None) == (gpu_index == policy[0])
        )
        if not valid_identity or not valid_gpu or any(
            getattr(lease_payload, name, object()) != expected
            for name, expected in expected_metadata.items()
        ):
            await asyncio.to_thread(managed.close)
            raise RuntimeError("GPU broker returned invalid MD execution lease metadata")
        return managed

    async def release_execution_lease(self, lease: ManagedGpuLease | None) -> None:
        if lease is not None:
            await asyncio.to_thread(lease.close)

    async def run(
        self,
        request: JobRequest,
        steps: int,
        *,
        execution_lease: ManagedGpuLease | None = None,
    ) -> DemoRunResult:
        output_dir = self._job_output_dir(request.job_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        if request.run_mode == "formal":
            formal_result = await self._formal_runner.run(
                request,
                output_dir,
                execution_lease=execution_lease,
            )
            return DemoRunResult(
                result=formal_result.result,
                output_dir=output_dir,
                completed_steps=formal_result.completed_steps,
            )
        if self._settings.mode == "dry-run":
            return await asyncio.to_thread(self._run_dry_run, request, steps, output_dir)
        return await self._run_real(
            request,
            steps,
            output_dir,
            execution_lease=execution_lease,
        )

    def _run_dry_run(
        self, request: JobRequest, steps: int, output_dir: Path
    ) -> DemoRunResult:
        report_interval = min(self._settings.report_interval, steps)
        report_interval = max(report_interval, 1)
        canonical = request.canonical_smiles or request.smiles
        seed = int(hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8], 16)
        base_density = 0.88 + (seed % 220) / 1000.0

        rows: list[dict[str, Any]] = []
        for sample_index, step in enumerate(range(report_interval, steps + 1, report_interval), 1):
            density = base_density + ((sample_index % 9) - 4) * 0.0015
            rows.append(
                {
                    "step": step,
                    "time_ps": round(step * 0.002, 6),
                    "density_g_cm3": round(density, 6),
                    "volume_nm3": round(100.0 / density, 6),
                    "temperature_k": 298.15,
                    "pressure_bar": 1.0,
                }
            )
        if not rows or rows[-1]["step"] != steps:
            rows.append(
                {
                    "step": steps,
                    "time_ps": round(steps * 0.002, 6),
                    "density_g_cm3": round(base_density, 6),
                    "volume_nm3": round(100.0 / base_density, 6),
                    "temperature_k": 298.15,
                    "pressure_bar": 1.0,
                }
            )

        self._write_csv(output_dir / "npt_state.csv", rows)
        (output_dir / "npt.dcd").write_bytes(
            b"DRY-RUN PLACEHOLDER: no molecular trajectory is present.\n"
        )

        result = self._normalize_result(
            {
                "job_id": request.job_id,
                "mode": "dry-run",
                "smiles": request.smiles,
                "canonical_smiles": canonical,
                "steps": steps,
                "report_interval": report_interval,
                "sample_count": len(rows),
                "density_g_cm3": rows[-1]["density_g_cm3"],
                "outputs": {
                    "density_demo_results_json": "density_demo_results.json",
                    "npt_state_csv": "npt_state.csv",
                    "npt_dcd": "npt.dcd",
                },
                "warnings": [
                    "Dry-run output was generated without ByteFF2 or OpenMM.",
                    NOT_PHYSICAL_WARNING,
                ],
            },
            request=request,
            steps=steps,
            output_dir=output_dir,
        )
        self._write_json(output_dir / "density_demo_results.json", result)
        return DemoRunResult(result=result, output_dir=output_dir, completed_steps=steps)

    async def _run_real(
        self,
        request: JobRequest,
        steps: int,
        output_dir: Path,
        *,
        execution_lease: ManagedGpuLease | None,
    ) -> DemoRunResult:
        if not self._settings.byteff2_root.exists():
            raise RuntimeError(f"ByteFF2 root does not exist: {self._settings.byteff2_root}")

        command = self._build_real_command(request, steps, output_dir)
        env = os.environ.copy()
        env["BYTEFF2_ROOT"] = str(self._settings.byteff2_root)
        if execution_lease is not None:
            env.update(
                mps_client_environment(
                    execution_lease.lease,
                    pipe_root=self._settings.gpu_mps_pipe_root,
                )
            )
            result_gpu_device = str(execution_lease.lease.gpu_index)
        else:
            env["CUDA_VISIBLE_DEVICES"] = self._settings.cuda_visible_devices
            env["CUDA_MPS_ACTIVE_THREAD_PERCENTAGE"] = "50"
            result_gpu_device = self._settings.cuda_visible_devices
        env["MONOMER_MD_DEMO_NOT_EQUILIBRATED"] = "1"
        env["PYTHONPATH"] = os.pathsep.join(
            [str(self._settings.byteff2_root), env.get("PYTHONPATH", "")]
        ).strip(os.pathsep)

        stdout_path = output_dir / "worker_stdout.log"
        stderr_path = output_dir / "worker_stderr.log"
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = await create_fenced_subprocess_exec(
                command,
                execution_lease=execution_lease,
                cwd=self._settings.byteff2_root,
                env=env,
                stdout=stdout,
                stderr=stderr,
            )
            try:
                return_code = await wait_for_process_group(
                    process,
                    timeout_seconds=self._settings.timeout_seconds,
                    execution_lease=execution_lease,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    f"ByteFF2 density demo timed out after {self._settings.timeout_seconds}s"
                ) from exc

        if return_code != 0:
            raise RuntimeError(
                "ByteFF2 density demo failed with exit code "
                f"{return_code}; see {stdout_path.name} and {stderr_path.name}"
            )

        result_path = output_dir / "density_demo_results.json"
        npt_state_path = output_dir / "npt_state.csv"
        npt_dcd_path = output_dir / "npt.dcd"
        missing = [
            path.name
            for path in (result_path, npt_state_path, npt_dcd_path)
            if not path.exists()
        ]
        if missing:
            raise RuntimeError(
                "ByteFF2 density demo completed but did not produce required files: "
                + ", ".join(missing)
            )

        with result_path.open("r", encoding="utf-8") as handle:
            raw_result = json.load(handle)
        result = self._normalize_result(
            raw_result, request=request, steps=steps, output_dir=output_dir
        )
        result["gpu_device"] = result_gpu_device
        if execution_lease is not None:
            result.update(_lease_provenance(execution_lease))
        self._write_json(result_path, result)
        return DemoRunResult(result=result, output_dir=output_dir, completed_steps=steps)

    def output_dir_for_job(self, job_id: str) -> Path:
        return self._job_output_dir(job_id)

    def _build_real_command(
        self, request: JobRequest, steps: int, output_dir: Path
    ) -> list[str]:
        report_interval = max(1, min(self._settings.report_interval, steps))
        canonical = request.canonical_smiles or request.smiles
        if self._settings.byteff2_demo_command:
            values = {
                "job_id": shlex.quote(request.job_id),
                "smiles": shlex.quote(request.smiles),
                "canonical_smiles": shlex.quote(canonical),
                "steps": str(steps),
                "output_dir": shlex.quote(str(output_dir)),
                "byteff2_root": shlex.quote(str(self._settings.byteff2_root)),
                "report_interval": str(report_interval),
            }
            return shlex.split(self._settings.byteff2_demo_command.format(**values))

        return [
            self._settings.byteff2_python,
            str(Path(__file__).with_name("byteff2_density_demo.py")),
            "--byteff2-root",
            str(self._settings.byteff2_root),
            "--job-id",
            request.job_id,
            "--smiles",
            canonical,
            "--steps",
            str(steps),
            "--report-interval",
            str(report_interval),
            "--output-dir",
            str(output_dir),
        ]

    def _job_output_dir(self, job_id: str) -> Path:
        safe_job_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", job_id).strip("._-")
        if not safe_job_id:
            raise ValueError("job_id produced an empty output directory name")
        return self._settings.job_root / safe_job_id

    def _normalize_result(
        self,
        result: dict[str, Any],
        *,
        request: JobRequest,
        steps: int,
        output_dir: Path,
    ) -> dict[str, Any]:
        normalized = dict(result)
        state_rows = self._load_state_rows(output_dir)
        density_series = normalized.get("density_series") or self._series_from_rows(
            state_rows,
            key="density",
            label="Density",
            unit="g/cm3",
            fields=("density_g_cm3", "density", "rho"),
        )
        temperature_series = normalized.get("temperature_series") or self._series_from_rows(
            state_rows,
            key="temperature",
            label="Temperature",
            unit="K",
            fields=("temperature_k", "temperature", "temp"),
        )
        energy_series = normalized.get("energy_series") or self._series_from_rows(
            state_rows,
            key="energy",
            label="Total energy",
            unit="kcal/mol",
            fields=("total_energy_kcal_mol", "total_energy", "potential_energy", "energy"),
        )
        summary = self._normalize_summary(
            normalized,
            state_rows,
            density_series,
            temperature_series,
            energy_series,
            steps,
        )
        artifacts = self._normalize_artifacts(normalized.get("artifacts"), normalized.get("outputs"))
        warnings = list(normalized.get("warnings") or [])
        if NOT_PHYSICAL_WARNING not in warnings:
            warnings.append(NOT_PHYSICAL_WARNING)
        normalized.update(
            {
                "job_id": request.job_id,
                "smiles": request.smiles,
                "canonical_smiles": request.canonical_smiles or request.smiles,
                "steps": steps,
                "summary": summary,
                "density_series": density_series,
                "temperature_series": temperature_series,
                "energy_series": energy_series,
                "trajectory_preview": normalized.get("trajectory_preview"),
                "artifacts": artifacts,
                "output_dir": str(output_dir),
                "not_equilibrated": True,
                "physical_density_estimate": False,
                "warnings": warnings,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        normalized.setdefault(
            "outputs",
            {
                "density_demo_results_json": "density_demo_results.json",
                "npt_state_csv": "npt_state.csv",
                "npt_dcd": "npt.dcd",
            },
        )
        if self._settings.mode != "dry-run":
            normalized["byteff2_git_sha"] = resolve_byteff2_commit(
                self._settings.byteff2_root
            )
        return normalized

    @staticmethod
    def _load_state_rows(output_dir: Path) -> list[dict[str, Any]]:
        state_path = output_dir / "npt_state.csv"
        if not state_path.exists():
            return []
        with state_path.open("r", encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]

    @staticmethod
    def _series_from_rows(rows, *, key, label, unit, fields) -> dict[str, Any]:
        points: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            value = None
            for field in fields:
                value = MonomerMdRunner._numeric(row.get(field))
                if value is not None:
                    break
            if value is None:
                continue
            point: dict[str, Any] = {"value": value}
            step = MonomerMdRunner._numeric(row.get("step"))
            time_ps = MonomerMdRunner._numeric(row.get("time_ps"))
            frame = MonomerMdRunner._numeric(row.get("frame"))
            if step is not None:
                point["step"] = int(step)
            if time_ps is not None:
                point["time_ps"] = time_ps
            if frame is not None:
                point["frame"] = int(frame)
            if "step" not in point and "frame" not in point:
                point["frame"] = index
            points.append(point)
        return {"key": key, "label": label, "unit": unit, "points": points}

    @staticmethod
    def _normalize_summary(normalized, state_rows, density_series, temperature_series, energy_series, steps) -> dict[str, Any]:
        raw_summary = normalized.get("summary")
        summary = dict(raw_summary) if isinstance(raw_summary, dict) else {}
        density_values = [point["value"] for point in density_series["points"]]
        temperature_values = [point["value"] for point in temperature_series["points"]]
        energy_values = [point["value"] for point in energy_series["points"]]
        if density_values:
            summary.setdefault("final_density_g_cm3", density_values[-1])
            summary.setdefault("mean_density_g_cm3", MonomerMdRunner._mean(density_values))
        elif normalized.get("density_g_cm3") is not None:
            summary.setdefault("final_density_g_cm3", normalized.get("density_g_cm3"))
        if temperature_values:
            summary.setdefault("final_temperature_k", temperature_values[-1])
            summary.setdefault("mean_temperature_k", MonomerMdRunner._mean(temperature_values))
        if energy_values:
            summary.setdefault("final_total_energy_kcal_mol", energy_values[-1])
            summary.setdefault("mean_total_energy_kcal_mol", MonomerMdRunner._mean(energy_values))
        summary.setdefault("n_steps", steps)
        summary.setdefault("sample_count", normalized.get("sample_count") or len(state_rows))
        summary.setdefault("n_frames", normalized.get("dcd_frame_count") or normalized.get("sample_count") or len(state_rows))
        return summary

    @staticmethod
    def _normalize_artifacts(raw_artifacts, outputs) -> dict[str, Any]:
        artifacts = dict(raw_artifacts) if isinstance(raw_artifacts, dict) else {}
        if isinstance(outputs, dict):
            for name, value in outputs.items():
                artifacts.setdefault(
                    name,
                    {
                        "name": name,
                        "path": str(value),
                        "kind": Path(str(value)).suffix.lstrip(".") or "artifact",
                    },
                )
        return artifacts

    @staticmethod
    def _numeric(value):
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and value.strip():
            try:
                return float(value)
            except ValueError:
                return None
        return None

    @staticmethod
    def _mean(values) -> float:
        return round(sum(values) / len(values), 6)

    @staticmethod
    def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        tmp_path.replace(path)
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
