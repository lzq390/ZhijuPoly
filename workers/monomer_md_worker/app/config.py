from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

WorkerMode = Literal["dry-run", "real"]


def _get_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _get_mode() -> WorkerMode:
    raw = os.getenv("MONOMER_MD_WORKER_MODE", "dry-run").strip().lower()
    if raw not in {"dry-run", "real"}:
        raise ValueError("MONOMER_MD_WORKER_MODE must be 'dry-run' or 'real'")
    return raw  # type: ignore[return-value]


@dataclass(frozen=True)
class WorkerSettings:
    mode: WorkerMode
    app_postgres_dsn: str | None
    job_table: str
    job_id_column: str
    status_column: str
    result_column: str
    error_column: str
    output_dir_column: str
    artifacts_column: str
    completed_steps_column: str
    progress_percent_column: str
    progress_stage_column: str
    progress_message_column: str
    worker_id_column: str
    worker_job_id_column: str
    worker_version_column: str
    started_at_column: str
    finished_at_column: str
    updated_at_column: str
    byteff2_root: Path
    byteff2_python: str
    byteff2_demo_command: str | None
    job_root: Path
    default_steps: int
    max_steps: int
    report_interval: int
    timeout_seconds: int
    health_probe_timeout_seconds: int
    max_concurrent_jobs: int
    max_active_jobs: int
    cuda_visible_devices: str
    worker_id: str
    worker_version: str
    formal_timeout_seconds: int = 43200
    protocol_column: str = "protocol"
    run_mode_column: str = "run_mode"
    artifact_manifest_column: str = "artifact_manifest"
    result_summary_column: str = "result_summary"
    byteff2_git_sha_column: str = "byteff2_git_sha"
    gpu_device_column: str = "gpu_device"
    error_category_column: str = "error_category"

    @property
    def db_configured(self) -> bool:
        return bool(self.app_postgres_dsn)


def load_settings() -> WorkerSettings:
    default_steps = _get_int("MONOMER_MD_DEFAULT_STEPS", 1000)
    max_steps = _get_int("MONOMER_MD_MAX_STEPS", default_steps)
    if max_steps < default_steps:
        raise ValueError("MONOMER_MD_MAX_STEPS must be >= MONOMER_MD_DEFAULT_STEPS")

    return WorkerSettings(
        mode=_get_mode(),
        app_postgres_dsn=os.getenv("APP_POSTGRES_DSN") or None,
        job_table=os.getenv("MONOMER_MD_JOB_TABLE", "md.monomer_md_jobs"),
        job_id_column=os.getenv("MONOMER_MD_JOB_ID_COLUMN", "job_id"),
        status_column=os.getenv("MONOMER_MD_STATUS_COLUMN", "status"),
        result_column=os.getenv("MONOMER_MD_RESULT_COLUMN", "result_data"),
        error_column=os.getenv("MONOMER_MD_ERROR_COLUMN", "error_message"),
        output_dir_column=os.getenv("MONOMER_MD_OUTPUT_DIR_COLUMN", "artifact_root"),
        artifacts_column=os.getenv("MONOMER_MD_ARTIFACTS_COLUMN", "artifacts"),
        completed_steps_column=os.getenv(
            "MONOMER_MD_COMPLETED_STEPS_COLUMN", "completed_steps"
        ),
        progress_percent_column=os.getenv(
            "MONOMER_MD_PROGRESS_PERCENT_COLUMN", "progress_percent"
        ),
        progress_stage_column=os.getenv(
            "MONOMER_MD_PROGRESS_STAGE_COLUMN", "progress_stage"
        ),
        progress_message_column=os.getenv(
            "MONOMER_MD_PROGRESS_MESSAGE_COLUMN", "progress_message"
        ),
        worker_id_column=os.getenv("MONOMER_MD_WORKER_ID_COLUMN", "worker_id"),
        worker_job_id_column=os.getenv(
            "MONOMER_MD_WORKER_JOB_ID_COLUMN", "worker_job_id"
        ),
        worker_version_column=os.getenv(
            "MONOMER_MD_WORKER_VERSION_COLUMN", "worker_version"
        ),
        started_at_column=os.getenv("MONOMER_MD_STARTED_AT_COLUMN", "started_at"),
        finished_at_column=os.getenv("MONOMER_MD_FINISHED_AT_COLUMN", "finished_at"),
        updated_at_column=os.getenv("MONOMER_MD_UPDATED_AT_COLUMN", "updated_at"),
        byteff2_root=Path(os.getenv("BYTEFF2_ROOT", "/data/lzq/gith/byteff2")),
        byteff2_python=os.getenv("BYTEFF2_PYTHON", "python"),
        byteff2_demo_command=os.getenv("BYTEFF2_DEMO_COMMAND") or None,
        job_root=Path(os.getenv("MONOMER_MD_JOB_ROOT", "/tmp/monomer-md-jobs")),
        default_steps=default_steps,
        max_steps=max_steps,
        report_interval=_get_int("MONOMER_MD_REPORT_INTERVAL", 10),
        timeout_seconds=_get_int("MONOMER_MD_TIMEOUT_SECONDS", 3600),
        formal_timeout_seconds=_get_int("MONOMER_MD_FORMAL_TIMEOUT_SECONDS", 43200),
        health_probe_timeout_seconds=_get_int("MONOMER_MD_HEALTH_PROBE_TIMEOUT_SECONDS", 5),
        max_concurrent_jobs=_get_int("MONOMER_MD_MAX_CONCURRENT_JOBS", 1),
        max_active_jobs=_get_int(
            "MONOMER_MD_MAX_ACTIVE_JOBS",
            _get_int("MONOMER_MD_MAX_CONCURRENT_JOBS", 1),
        ),
        cuda_visible_devices=os.getenv(
            "MONOMER_MD_CUDA_VISIBLE_DEVICES",
            os.getenv("NEXPOLY_GPU_DEVICE", "2"),
        ),
        worker_id=os.getenv("MONOMER_MD_WORKER_ID", "monomer-md-worker"),
        worker_version=os.getenv("MONOMER_MD_WORKER_VERSION", "0.1.0"),
        protocol_column=os.getenv("MONOMER_MD_PROTOCOL_COLUMN", "protocol"),
        run_mode_column=os.getenv("MONOMER_MD_RUN_MODE_COLUMN", "run_mode"),
        artifact_manifest_column=os.getenv("MONOMER_MD_ARTIFACT_MANIFEST_COLUMN", "artifact_manifest"),
        result_summary_column=os.getenv("MONOMER_MD_RESULT_SUMMARY_COLUMN", "result_summary"),
        byteff2_git_sha_column=os.getenv("MONOMER_MD_BYTEFF2_GIT_SHA_COLUMN", "byteff2_git_sha"),
        gpu_device_column=os.getenv("MONOMER_MD_GPU_DEVICE_COLUMN", "gpu_device"),
        error_category_column=os.getenv("MONOMER_MD_ERROR_CATEGORY_COLUMN", "error_category"),
    )
