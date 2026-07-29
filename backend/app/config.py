from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

try:
    from dotenv import dotenv_values
except ImportError:  # pragma: no cover - keeps local import scripts usable before dependency install
    def dotenv_values(path: Path) -> dict[str, str]:
        return {}


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = PROJECT_ROOT / "backend"
DEFAULT_ENV_FILE = BACKEND_DIR / ".env"


def _first_non_blank(*values: str | None, default: str = "") -> str:
    for value in values:
        if value is not None and value.strip():
            return value.strip()
    return default

def _resolve_from_root(value: str) -> str:
    windows_drive_match = re.match(r"^([A-Za-z]):[\\/](.*)$", value)
    if windows_drive_match and os.name != "nt":
        drive = windows_drive_match.group(1).lower()
        remainder = windows_drive_match.group(2).replace("\\", "/")
        return str((Path("/mnt") / drive / remainder).resolve())

    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return str(path.resolve())


def _absolute_from_root(value: str) -> str:
    windows_drive_match = re.match(r"^([A-Za-z]):[\\/](.*)$", value)
    if windows_drive_match and os.name != "nt":
        drive = windows_drive_match.group(1).lower()
        remainder = windows_drive_match.group(2).replace("\\", "/")
        return str(Path("/mnt") / drive / remainder)

    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return os.path.abspath(path)


class Settings:
    def __init__(
        self,
        sqlite_db_path: str | None = None,
        csv_source_path: str | None = None,
        property_filter_csv_path: str | None = None,
        experimental_process_csv_path: str | None = None,
        experimental_property_csv_path: str | None = None,
        knowledge_zip_path: str | None = None,
        fumol_zip_path: str | None = None,
        fumol_db_path: str | None = None,
        pi_reverse_db_path: str | None = None,
        legacy_main_sqlite_source_path: str | None = None,
        legacy_pi_sqlite_source_path: str | None = None,
        legacy_dft_sqlite_source_path: str | None = None,
        pi_reverse_csv_path: str | None = None,
        pi_reverse_backend: str | None = None,
        app_postgres_dsn: str | None = None,
        structured_data_backend: str | None = None,
        deployment_drain_enabled: bool | None = None,
        pi_postgres_dsn: str | None = None,
        lab_data_postgres_dsn: str | None = None,
        pi_reverse_tg_window_celsius: float | None = None,
        pi_reverse_tg_max_window_celsius: float | None = None,
        pi_reverse_max_scan_rows: int | None = None,
        pi_reverse_timeout_seconds: float | None = None,
        pi_reverse_job_workers: int | None = None,
        pi_reverse_job_batch_size: int | None = None,
        pi_reverse_progress_interval_rows: int | None = None,
        structure_3d_timeout_seconds: float | None = None,
        monomer_md_worker_base_url: str | None = None,
        monomer_md_worker_timeout_seconds: float | None = None,
        monomer_md_default_steps: int | None = None,
        monomer_md_submit_enabled: bool | None = None,
        monomer_md_rate_limit_per_ip_per_minute: int | None = None,
        monomer_md_rate_limit_window_seconds: int | None = None,
        monomer_md_max_active_jobs: int | None = None,
        monomer_dft_worker_base_url: str | None = None,
        monomer_dft_worker_uds: str | None = None,
        monomer_dft_worker_timeout_seconds: float | None = None,
        monomer_dft_submit_enabled: bool | None = None,
        monomer_dft_max_active_jobs: int | None = None,
        monomer_dft_reconcile_interval_seconds: float | None = None,
        monomer_dft_artifact_retention_days: int | None = None,
        monomer_dft_validation_concurrency: int | None = None,
        monomer_dft_download_max_concurrent: int | None = None,
        monomer_dft_download_spool_root: str | None = None,
        allowed_origins: str | None = None,
        dev_gpu_operator_enabled: bool | None = None,
        dev_gpu_operator_frontend_port: int | None = None,
        dev_gpu_operator_socket_path: str | None = None,
        dev_gpu_operator_timeout_seconds: float | None = None,
        model_enabled: bool | None = None,
        model_dir: str | None = None,
        online_knowledge_api_key: str | None = None,
        online_knowledge_base_url: str | None = None,
        online_knowledge_model: str | None = None,
        online_knowledge_max_papers: int | None = None,
        assistant_api_key: str | None = None,
        assistant_base_url: str | None = None,
        assistant_model: str | None = None,
        assistant_image_max_bytes: int | None = None,
        ocsr_enabled: bool | None = None,
        ocsr_model_dir: str | None = None,
        ocsr_device: str | None = None,
        ocsr_max_image_bytes: int | None = None,
        gen_model_enabled: bool | None = None,
        gen_model_dir: str | None = None,
        gen_device: str | None = None,
        gen_job_workers: int | None = None,
        gen_max_active_jobs: int | None = None,
        gpu_preload_mode: str | None = None,
        gpu_max_concurrent_inferences: int | None = None,
        gpu_max_waiting_inferences: int | None = None,
        gpu_sync_queue_timeout_seconds: float | None = None,
        gpu_async_queue_timeout_seconds: float | None = None,
        gpu_broker_enabled: bool | None = None,
        gpu_broker_socket_path: str | None = None,
        gpu_mps_pipe_root: str | None = None,
        gpu_broker_environment: str | None = None,
        gpu_broker_wait_timeout_seconds: float | None = None,
        gpu_broker_heartbeat_interval_seconds: float | None = None,
        polytao_enabled: bool | None = None,
        polytao_model_dir: str | None = None,
        polytao_device: str | None = None,
        polytao_model_id: str | None = None,
        polytao_model_revision: str | None = None,
        polytao_job_threads: int | None = None,
        polytao_job_workers: int | None = None,
        polytao_rate_limit_per_ip_per_minute: int | None = None,
        polytao_rate_limit_window_seconds: int | None = None,
        polytao_max_active_jobs: int | None = None,
        retro_model_enabled: bool | None = None,
        retro_model_id: str | None = None,
        retro_device: str | None = None,
        smipoly_enabled: bool | None = None,
    ) -> None:
        env_values = dotenv_values(DEFAULT_ENV_FILE) if DEFAULT_ENV_FILE.exists() else {}

        raw_sqlite_db_path = sqlite_db_path or os.getenv(
            "SQLITE_DB_PATH",
            env_values.get("SQLITE_DB_PATH", "backend/data/polyprop.db"),
        )
        raw_csv_source_path = csv_source_path or os.getenv(
            "CSV_SOURCE_PATH",
            env_values.get("CSV_SOURCE_PATH", "database/data1.csv"),
        )
        raw_property_filter_csv_path = property_filter_csv_path or os.getenv(
            "PROPERTY_FILTER_CSV_PATH",
            env_values.get("PROPERTY_FILTER_CSV_PATH", "database/PolymerDatabaseV2.0_reliable085_standardized.csv"),
        )
        raw_experimental_process_csv_path = experimental_process_csv_path or os.getenv(
            "EXPERIMENTAL_PROCESS_CSV_PATH",
            env_values.get("EXPERIMENTAL_PROCESS_CSV_PATH", "database/polymer_process_material_filtered_cleaned_office_utf8_bom.csv"),
        )
        raw_experimental_property_csv_path = experimental_property_csv_path or os.getenv(
            "EXPERIMENTAL_PROPERTY_CSV_PATH",
            env_values.get("EXPERIMENTAL_PROPERTY_CSV_PATH", "database/polymer_property_detail_cleaned_office_utf8_bom.csv"),
        )
        raw_knowledge_zip_path = knowledge_zip_path or os.getenv(
            "KNOWLEDGE_ZIP_PATH",
            env_values.get("KNOWLEDGE_ZIP_PATH", "database/data_txt.zip"),
        )
        raw_fumol_zip_path = fumol_zip_path or os.getenv(
            "FUMOL_ZIP_PATH",
            env_values.get("FUMOL_ZIP_PATH", "D:/database/fumol/FuMolDatabase.zip"),
        )
        raw_fumol_db_path = fumol_db_path or os.getenv(
            "FUMOL_DB_PATH",
            env_values.get("FUMOL_DB_PATH", "backend/data/fumol.db"),
        )
        raw_pi_reverse_db_path = pi_reverse_db_path or os.getenv(
            "PI_REVERSE_DB_PATH",
            env_values.get("PI_REVERSE_DB_PATH", "backend/data/pi_reverse_design.db"),
        )
        raw_legacy_main_sqlite_source_path = legacy_main_sqlite_source_path or os.getenv(
            "LEGACY_MAIN_SQLITE_SOURCE_PATH",
            env_values.get("LEGACY_MAIN_SQLITE_SOURCE_PATH", raw_sqlite_db_path),
        )
        raw_legacy_pi_sqlite_source_path = legacy_pi_sqlite_source_path or os.getenv(
            "LEGACY_PI_SQLITE_SOURCE_PATH",
            env_values.get("LEGACY_PI_SQLITE_SOURCE_PATH", raw_pi_reverse_db_path),
        )
        raw_legacy_dft_sqlite_source_path = legacy_dft_sqlite_source_path or os.getenv(
            "LEGACY_DFT_SQLITE_SOURCE_PATH",
            env_values.get("LEGACY_DFT_SQLITE_SOURCE_PATH", raw_fumol_db_path),
        )
        raw_pi_reverse_csv_path = pi_reverse_csv_path
        if raw_pi_reverse_csv_path is None:
            raw_pi_reverse_csv_path = os.getenv(
                "PI_REVERSE_CSV_PATH",
                env_values.get("PI_REVERSE_CSV_PATH", ""),
            )
        raw_pi_reverse_backend = pi_reverse_backend or os.getenv(
            "PI_REVERSE_BACKEND",
            env_values.get("PI_REVERSE_BACKEND", "postgres"),
        )
        raw_app_postgres_dsn = app_postgres_dsn or os.getenv(
            "APP_POSTGRES_DSN",
            env_values.get("APP_POSTGRES_DSN", ""),
        )
        raw_structured_data_backend = structured_data_backend or os.getenv(
            "STRUCTURED_DATA_BACKEND",
            env_values.get("STRUCTURED_DATA_BACKEND", "postgres"),
        )
        raw_deployment_drain_enabled = deployment_drain_enabled
        if raw_deployment_drain_enabled is None:
            raw_deployment_drain_enabled = os.getenv(
                "DEPLOYMENT_DRAIN_ENABLED",
                str(env_values.get("DEPLOYMENT_DRAIN_ENABLED", "false")),
            ).strip().lower() in {"1", "true", "yes", "on"}
        raw_pi_postgres_dsn = pi_postgres_dsn or os.getenv(
            "PI_POSTGRES_DSN",
            env_values.get("PI_POSTGRES_DSN", raw_app_postgres_dsn),
        )
        raw_lab_data_postgres_dsn = lab_data_postgres_dsn
        if raw_lab_data_postgres_dsn is None:
            raw_lab_data_postgres_dsn = os.getenv(
                "LAB_DATA_POSTGRES_DSN",
                env_values.get("LAB_DATA_POSTGRES_DSN", ""),
            )
        raw_lab_data_postgres_dsn = raw_lab_data_postgres_dsn.strip() or raw_app_postgres_dsn
        raw_pi_reverse_tg_window_celsius = (
            str(pi_reverse_tg_window_celsius)
            if pi_reverse_tg_window_celsius is not None
            else os.getenv(
                "PI_REVERSE_TG_WINDOW_CELSIUS",
                str(env_values.get("PI_REVERSE_TG_WINDOW_CELSIUS", "50")),
            )
        )
        raw_pi_reverse_tg_max_window_celsius = (
            str(pi_reverse_tg_max_window_celsius)
            if pi_reverse_tg_max_window_celsius is not None
            else os.getenv(
                "PI_REVERSE_TG_MAX_WINDOW_CELSIUS",
                str(env_values.get("PI_REVERSE_TG_MAX_WINDOW_CELSIUS", "200")),
            )
        )
        raw_pi_reverse_max_scan_rows = (
            str(pi_reverse_max_scan_rows)
            if pi_reverse_max_scan_rows is not None
            else os.getenv(
                "PI_REVERSE_MAX_SCAN_ROWS",
                str(env_values.get("PI_REVERSE_MAX_SCAN_ROWS", "500000")),
            )
        )
        raw_pi_reverse_timeout_seconds = (
            str(pi_reverse_timeout_seconds)
            if pi_reverse_timeout_seconds is not None
            else os.getenv(
                "PI_REVERSE_TIMEOUT_SECONDS",
                str(env_values.get("PI_REVERSE_TIMEOUT_SECONDS", "30")),
            )
        )
        raw_pi_reverse_job_workers = (
            str(pi_reverse_job_workers)
            if pi_reverse_job_workers is not None
            else os.getenv(
                "PI_REVERSE_JOB_WORKERS",
                str(env_values.get("PI_REVERSE_JOB_WORKERS", "1")),
            )
        )
        raw_pi_reverse_job_batch_size = (
            str(pi_reverse_job_batch_size)
            if pi_reverse_job_batch_size is not None
            else os.getenv(
                "PI_REVERSE_JOB_BATCH_SIZE",
                str(env_values.get("PI_REVERSE_JOB_BATCH_SIZE", "20000")),
            )
        )
        raw_pi_reverse_progress_interval_rows = (
            str(pi_reverse_progress_interval_rows)
            if pi_reverse_progress_interval_rows is not None
            else os.getenv(
                "PI_REVERSE_PROGRESS_INTERVAL_ROWS",
                str(env_values.get("PI_REVERSE_PROGRESS_INTERVAL_ROWS", "50000")),
            )
        )
        raw_structure_3d_timeout_seconds = (
            str(structure_3d_timeout_seconds)
            if structure_3d_timeout_seconds is not None
            else os.getenv(
                "STRUCTURE_3D_TIMEOUT_SECONDS",
                str(env_values.get("STRUCTURE_3D_TIMEOUT_SECONDS", "8")),
            )
        )
        raw_monomer_md_worker_base_url = monomer_md_worker_base_url
        if raw_monomer_md_worker_base_url is None:
            raw_monomer_md_worker_base_url = os.getenv(
                "MONOMER_MD_WORKER_BASE_URL",
                env_values.get("MONOMER_MD_WORKER_BASE_URL", ""),
            )
        raw_monomer_md_worker_timeout_seconds = (
            str(monomer_md_worker_timeout_seconds)
            if monomer_md_worker_timeout_seconds is not None
            else os.getenv(
                "MONOMER_MD_WORKER_TIMEOUT_SECONDS",
                str(env_values.get("MONOMER_MD_WORKER_TIMEOUT_SECONDS", "15")),
            )
        )
        raw_monomer_md_default_steps = (
            str(monomer_md_default_steps)
            if monomer_md_default_steps is not None
            else os.getenv(
                "MONOMER_MD_DEFAULT_STEPS",
                str(env_values.get("MONOMER_MD_DEFAULT_STEPS", "300")),
            )
        )
        raw_monomer_md_submit_enabled = monomer_md_submit_enabled
        if raw_monomer_md_submit_enabled is None:
            raw_monomer_md_submit_enabled = os.getenv(
                "MONOMER_MD_SUBMIT_ENABLED",
                str(env_values.get("MONOMER_MD_SUBMIT_ENABLED", "true")),
            ).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        raw_monomer_md_rate_limit_per_ip_per_minute = (
            str(monomer_md_rate_limit_per_ip_per_minute)
            if monomer_md_rate_limit_per_ip_per_minute is not None
            else os.getenv(
                "MONOMER_MD_RATE_LIMIT_PER_IP_PER_MINUTE",
                str(env_values.get("MONOMER_MD_RATE_LIMIT_PER_IP_PER_MINUTE", "3")),
            )
        )
        raw_monomer_md_rate_limit_window_seconds = (
            str(monomer_md_rate_limit_window_seconds)
            if monomer_md_rate_limit_window_seconds is not None
            else os.getenv(
                "MONOMER_MD_RATE_LIMIT_WINDOW_SECONDS",
                str(env_values.get("MONOMER_MD_RATE_LIMIT_WINDOW_SECONDS", "60")),
            )
        )
        raw_monomer_md_max_active_jobs = (
            str(monomer_md_max_active_jobs)
            if monomer_md_max_active_jobs is not None
            else os.getenv(
                "MONOMER_MD_MAX_ACTIVE_JOBS",
                str(env_values.get("MONOMER_MD_MAX_ACTIVE_JOBS", "3")),
            )
        )
        raw_monomer_dft_worker_base_url = monomer_dft_worker_base_url
        if raw_monomer_dft_worker_base_url is None:
            raw_monomer_dft_worker_base_url = os.getenv(
                "MONOMER_DFT_WORKER_BASE_URL",
                env_values.get("MONOMER_DFT_WORKER_BASE_URL", "http://monomer-dft-worker"),
            )
        raw_monomer_dft_worker_uds = monomer_dft_worker_uds
        if raw_monomer_dft_worker_uds is None:
            raw_monomer_dft_worker_uds = os.getenv(
                "MONOMER_DFT_WORKER_UDS",
                env_values.get("MONOMER_DFT_WORKER_UDS", ""),
            )
        raw_monomer_dft_worker_timeout_seconds = (
            str(monomer_dft_worker_timeout_seconds)
            if monomer_dft_worker_timeout_seconds is not None
            else os.getenv(
                "MONOMER_DFT_WORKER_TIMEOUT_SECONDS",
                str(env_values.get("MONOMER_DFT_WORKER_TIMEOUT_SECONDS", "30")),
            )
        )
        raw_monomer_dft_submit_enabled = monomer_dft_submit_enabled
        if raw_monomer_dft_submit_enabled is None:
            raw_monomer_dft_submit_enabled = os.getenv(
                "MONOMER_DFT_SUBMIT_ENABLED",
                str(env_values.get("MONOMER_DFT_SUBMIT_ENABLED", "false")),
            ).strip().lower() in {"1", "true", "yes", "on"}
        raw_monomer_dft_max_active_jobs = (
            str(monomer_dft_max_active_jobs)
            if monomer_dft_max_active_jobs is not None
            else os.getenv(
                "MONOMER_DFT_MAX_ACTIVE_JOBS",
                str(env_values.get("MONOMER_DFT_MAX_ACTIVE_JOBS", "9")),
            )
        )
        raw_monomer_dft_reconcile_interval_seconds = (
            str(monomer_dft_reconcile_interval_seconds)
            if monomer_dft_reconcile_interval_seconds is not None
            else os.getenv(
                "MONOMER_DFT_RECONCILE_INTERVAL_SECONDS",
                str(env_values.get("MONOMER_DFT_RECONCILE_INTERVAL_SECONDS", "2")),
            )
        )
        raw_monomer_dft_artifact_retention_days = (
            str(monomer_dft_artifact_retention_days)
            if monomer_dft_artifact_retention_days is not None
            else os.getenv(
                "MONOMER_DFT_ARTIFACT_RETENTION_DAYS",
                str(env_values.get("MONOMER_DFT_ARTIFACT_RETENTION_DAYS", "30")),
            )
        )
        raw_monomer_dft_validation_concurrency = (
            str(monomer_dft_validation_concurrency)
            if monomer_dft_validation_concurrency is not None
            else os.getenv(
                "MONOMER_DFT_VALIDATION_CONCURRENCY",
                str(env_values.get("MONOMER_DFT_VALIDATION_CONCURRENCY", "2")),
            )
        )
        raw_monomer_dft_download_max_concurrent = (
            str(monomer_dft_download_max_concurrent)
            if monomer_dft_download_max_concurrent is not None
            else os.getenv(
                "MONOMER_DFT_DOWNLOAD_MAX_CONCURRENT",
                str(env_values.get("MONOMER_DFT_DOWNLOAD_MAX_CONCURRENT", "2")),
            )
        )
        raw_monomer_dft_download_spool_root = (
            monomer_dft_download_spool_root
            if monomer_dft_download_spool_root is not None
            else os.getenv(
                "MONOMER_DFT_DOWNLOAD_SPOOL_ROOT",
                str(
                    env_values.get(
                        "MONOMER_DFT_DOWNLOAD_SPOOL_ROOT",
                        "/tmp/monomer-dft-downloads",
                    )
                ),
            )
        )
        raw_allowed_origins = allowed_origins or os.getenv(
            "ALLOWED_ORIGINS",
            env_values.get("ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:3000"),
        )
        raw_dev_gpu_operator_enabled = dev_gpu_operator_enabled
        if raw_dev_gpu_operator_enabled is None:
            raw_dev_gpu_operator_enabled = os.getenv(
                "DEV_GPU_OPERATOR_ENABLED",
                str(env_values.get("DEV_GPU_OPERATOR_ENABLED", "false")),
            ).strip().lower() in {"1", "true", "yes", "on"}
        raw_dev_gpu_operator_frontend_port = (
            str(dev_gpu_operator_frontend_port)
            if dev_gpu_operator_frontend_port is not None
            else os.getenv(
                "DEV_GPU_OPERATOR_FRONTEND_PORT",
                str(env_values.get("DEV_GPU_OPERATOR_FRONTEND_PORT", "0")),
            )
        )
        raw_dev_gpu_operator_socket_path = (
            dev_gpu_operator_socket_path
            or os.getenv(
                "DEV_GPU_OPERATOR_SOCKET_PATH",
                str(
                    env_values.get(
                        "DEV_GPU_OPERATOR_SOCKET_PATH",
                        "/app/gpu-operator/operator.sock",
                    )
                ),
            )
        )
        raw_dev_gpu_operator_timeout_seconds = (
            str(dev_gpu_operator_timeout_seconds)
            if dev_gpu_operator_timeout_seconds is not None
            else os.getenv(
                "DEV_GPU_OPERATOR_TIMEOUT_SECONDS",
                str(env_values.get("DEV_GPU_OPERATOR_TIMEOUT_SECONDS", "3")),
            )
        )
        raw_model_dir = model_dir or os.getenv(
            "MODEL_DIR",
            env_values.get("MODEL_DIR", "model"),
        )
        raw_online_knowledge_api_key = online_knowledge_api_key
        if raw_online_knowledge_api_key is None:
            raw_online_knowledge_api_key = os.getenv(
                "ONLINE_KNOWLEDGE_API_KEY",
                env_values.get("ONLINE_KNOWLEDGE_API_KEY", ""),
            )
        raw_online_knowledge_base_url = online_knowledge_base_url or os.getenv(
            "ONLINE_KNOWLEDGE_BASE_URL",
            env_values.get("ONLINE_KNOWLEDGE_BASE_URL", "https://api.vectorengine.ai/v1"),
        )
        raw_online_knowledge_model = online_knowledge_model or os.getenv(
            "ONLINE_KNOWLEDGE_MODEL",
            env_values.get("ONLINE_KNOWLEDGE_MODEL", "gpt-4.1-nano-2025-04-14"),
        )
        raw_online_knowledge_max_papers = (
            str(online_knowledge_max_papers)
            if online_knowledge_max_papers is not None
            else os.getenv(
                "ONLINE_KNOWLEDGE_MAX_PAPERS",
                str(env_values.get("ONLINE_KNOWLEDGE_MAX_PAPERS", "20")),
            )
        )
        raw_assistant_api_key = _first_non_blank(
            assistant_api_key,
            os.getenv("ASSISTANT_API_KEY"),
            env_values.get("ASSISTANT_API_KEY"),
            raw_online_knowledge_api_key,
        )
        raw_assistant_base_url = _first_non_blank(
            assistant_base_url,
            os.getenv("ASSISTANT_BASE_URL"),
            env_values.get("ASSISTANT_BASE_URL"),
            raw_online_knowledge_base_url,
        )
        raw_assistant_model = _first_non_blank(
            assistant_model,
            os.getenv("ASSISTANT_MODEL"),
            env_values.get("ASSISTANT_MODEL"),
            default="gpt-5.5",
        )
        raw_assistant_image_max_bytes = (
            str(assistant_image_max_bytes)
            if assistant_image_max_bytes is not None
            else os.getenv(
                "ASSISTANT_IMAGE_MAX_BYTES",
                str(env_values.get("ASSISTANT_IMAGE_MAX_BYTES", "5242880")),
            )
        )
        raw_ocsr_model_dir = ocsr_model_dir
        if raw_ocsr_model_dir is None:
            raw_ocsr_model_dir = (
                os.getenv("OCSR_MODEL_PATH")
                or env_values.get("OCSR_MODEL_PATH")
                or os.getenv("OCSR_MODEL_DIR")
                or env_values.get("OCSR_MODEL_DIR", "model/ocsr")
            )
        raw_ocsr_device = ocsr_device or os.getenv(
            "OCSR_DEVICE",
            env_values.get("OCSR_DEVICE", "auto"),
        )
        raw_ocsr_max_image_bytes = (
            str(ocsr_max_image_bytes)
            if ocsr_max_image_bytes is not None
            else os.getenv(
                "OCSR_MAX_IMAGE_BYTES",
                str(env_values.get("OCSR_MAX_IMAGE_BYTES", "5242880")),
            )
        )
        raw_gen_model_dir = gen_model_dir or os.getenv(
            "GEN_MODEL_DIR",
            env_values.get("GEN_MODEL_DIR", "model/conditional_generation"),
        )
        raw_gen_device = gen_device or os.getenv(
            "GEN_DEVICE",
            env_values.get("GEN_DEVICE", "auto"),
        )
        raw_gen_job_workers = (
            str(gen_job_workers)
            if gen_job_workers is not None
            else os.getenv(
                "GEN_JOB_WORKERS",
                str(env_values.get("GEN_JOB_WORKERS", "1")),
            )
        )
        raw_gen_max_active_jobs = (
            str(gen_max_active_jobs)
            if gen_max_active_jobs is not None
            else os.getenv(
                "GEN_MAX_ACTIVE_JOBS",
                str(env_values.get("GEN_MAX_ACTIVE_JOBS", "8")),
            )
        )
        raw_gpu_preload_mode = gpu_preload_mode or os.getenv(
            "GPU_PRELOAD_MODE",
            env_values.get("GPU_PRELOAD_MODE", "lazy"),
        )
        raw_gpu_max_concurrent_inferences = (
            str(gpu_max_concurrent_inferences)
            if gpu_max_concurrent_inferences is not None
            else os.getenv(
                "GPU_MAX_CONCURRENT_INFERENCES",
                str(env_values.get("GPU_MAX_CONCURRENT_INFERENCES", "1")),
            )
        )
        raw_gpu_max_waiting_inferences = (
            str(gpu_max_waiting_inferences)
            if gpu_max_waiting_inferences is not None
            else os.getenv(
                "GPU_MAX_WAITING_INFERENCES",
                str(env_values.get("GPU_MAX_WAITING_INFERENCES", "8")),
            )
        )
        raw_gpu_sync_queue_timeout_seconds = (
            str(gpu_sync_queue_timeout_seconds)
            if gpu_sync_queue_timeout_seconds is not None
            else os.getenv(
                "GPU_SYNC_QUEUE_TIMEOUT_SECONDS",
                str(env_values.get("GPU_SYNC_QUEUE_TIMEOUT_SECONDS", "30")),
            )
        )
        raw_gpu_async_queue_timeout_seconds = (
            str(gpu_async_queue_timeout_seconds)
            if gpu_async_queue_timeout_seconds is not None
            else os.getenv(
                "GPU_ASYNC_QUEUE_TIMEOUT_SECONDS",
                str(env_values.get("GPU_ASYNC_QUEUE_TIMEOUT_SECONDS", "600")),
            )
        )
        raw_gpu_broker_enabled = gpu_broker_enabled
        if raw_gpu_broker_enabled is None:
            raw_gpu_broker_enabled = os.getenv(
                "GPU_BROKER_ENABLED",
                str(env_values.get("GPU_BROKER_ENABLED", "false")),
            ).strip().lower() in {"1", "true", "yes", "on"}
        raw_gpu_broker_socket_path = gpu_broker_socket_path or os.getenv(
            "GPU_BROKER_SOCKET_PATH",
            env_values.get("GPU_BROKER_SOCKET_PATH", "/run/user/1001/nexpoly-gpu/broker.sock"),
        )
        raw_gpu_mps_pipe_root = gpu_mps_pipe_root or os.getenv(
            "GPU_MPS_PIPE_ROOT",
            env_values.get(
                "GPU_MPS_PIPE_ROOT",
                "/data/lzq/gith/nexpoly-runtime/state/gpu-resource",
            ),
        )
        raw_gpu_broker_environment = gpu_broker_environment or os.getenv(
            "GPU_BROKER_ENVIRONMENT",
            env_values.get("GPU_BROKER_ENVIRONMENT", "dev"),
        )
        raw_gpu_broker_wait_timeout_seconds = (
            str(gpu_broker_wait_timeout_seconds)
            if gpu_broker_wait_timeout_seconds is not None
            else os.getenv(
                "GPU_BROKER_WAIT_TIMEOUT_SECONDS",
                str(env_values.get("GPU_BROKER_WAIT_TIMEOUT_SECONDS", "45")),
            )
        )
        raw_gpu_broker_heartbeat_interval_seconds = (
            str(gpu_broker_heartbeat_interval_seconds)
            if gpu_broker_heartbeat_interval_seconds is not None
            else os.getenv(
                "GPU_BROKER_HEARTBEAT_INTERVAL_SECONDS",
                str(env_values.get("GPU_BROKER_HEARTBEAT_INTERVAL_SECONDS", "5")),
            )
        )
        raw_polytao_enabled = polytao_enabled
        if raw_polytao_enabled is None:
            raw_polytao_enabled_value = os.getenv(
                "POLYTAO_ENABLED",
                env_values.get(
                    "POLYTAO_ENABLED",
                    os.getenv("POLYTAO_SUBMIT_ENABLED", env_values.get("POLYTAO_SUBMIT_ENABLED", "true")),
                ),
            )
            raw_polytao_enabled = str(raw_polytao_enabled_value).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        raw_polytao_model_dir = polytao_model_dir or os.getenv(
            "POLYTAO_MODEL_DIR",
            env_values.get("POLYTAO_MODEL_DIR", "model/polytao"),
        )
        raw_polytao_device = polytao_device or os.getenv(
            "POLYTAO_DEVICE",
            env_values.get("POLYTAO_DEVICE", "auto"),
        )
        raw_polytao_model_id = polytao_model_id or os.getenv(
            "POLYTAO_MODEL_ID",
            env_values.get("POLYTAO_MODEL_ID", "hkqiu/PolymerGenerationPretrainedModel"),
        )
        raw_polytao_model_revision = polytao_model_revision
        if raw_polytao_model_revision is None:
            raw_polytao_model_revision = os.getenv(
                "POLYTAO_MODEL_REVISION",
                env_values.get("POLYTAO_MODEL_REVISION", ""),
            )
        if polytao_job_threads is not None:
            raw_polytao_job_threads = str(polytao_job_threads)
        elif polytao_job_workers is not None:
            raw_polytao_job_threads = str(polytao_job_workers)
        else:
            raw_polytao_job_threads = os.getenv("POLYTAO_JOB_THREADS")
            if raw_polytao_job_threads is None:
                raw_polytao_job_threads = env_values.get("POLYTAO_JOB_THREADS")
            if raw_polytao_job_threads is None:
                raw_polytao_job_threads = os.getenv(
                    "POLYTAO_JOB_WORKERS",
                    str(env_values.get("POLYTAO_JOB_WORKERS", "1")),
                )
        raw_polytao_rate_limit_per_ip_per_minute = (
            str(polytao_rate_limit_per_ip_per_minute)
            if polytao_rate_limit_per_ip_per_minute is not None
            else os.getenv(
                "POLYTAO_RATE_LIMIT_PER_IP_PER_MINUTE",
                str(env_values.get("POLYTAO_RATE_LIMIT_PER_IP_PER_MINUTE", "5")),
            )
        )
        raw_polytao_rate_limit_window_seconds = (
            str(polytao_rate_limit_window_seconds)
            if polytao_rate_limit_window_seconds is not None
            else os.getenv(
                "POLYTAO_RATE_LIMIT_WINDOW_SECONDS",
                str(env_values.get("POLYTAO_RATE_LIMIT_WINDOW_SECONDS", "60")),
            )
        )
        raw_polytao_max_active_jobs = (
            str(polytao_max_active_jobs)
            if polytao_max_active_jobs is not None
            else os.getenv(
                "POLYTAO_MAX_ACTIVE_JOBS",
                str(env_values.get("POLYTAO_MAX_ACTIVE_JOBS", "1")),
            )
        )
        raw_retro_model_id = retro_model_id or os.getenv(
            "RETRO_MODEL_ID",
            env_values.get("RETRO_MODEL_ID", "sagawa/ReactionT5v2-retrosynthesis-USPTO_50k"),
        )
        raw_retro_device = retro_device or os.getenv(
            "RETRO_DEVICE",
            env_values.get("RETRO_DEVICE", "auto"),
        )
        raw_gen_model_enabled = gen_model_enabled
        if raw_gen_model_enabled is None:
            raw_gen_model_enabled = os.getenv(
                "GEN_MODEL_ENABLED",
                str(env_values.get("GEN_MODEL_ENABLED", "false")),
            ).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        raw_retro_model_enabled = retro_model_enabled
        if raw_retro_model_enabled is None:
            raw_retro_model_enabled = os.getenv(
                "RETRO_MODEL_ENABLED",
                str(env_values.get("RETRO_MODEL_ENABLED", "true")),
            ).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        raw_smipoly_enabled = smipoly_enabled
        if raw_smipoly_enabled is None:
            raw_smipoly_enabled = os.getenv(
                "SMIPOLY_ENABLED",
                str(env_values.get("SMIPOLY_ENABLED", "true")),
            ).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        raw_model_enabled = model_enabled
        if raw_model_enabled is None:
            raw_model_enabled = os.getenv(
                "MODEL_ENABLED",
                str(env_values.get("MODEL_ENABLED", "true")),
            ).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        raw_ocsr_enabled = ocsr_enabled
        if raw_ocsr_enabled is None:
            raw_ocsr_enabled = os.getenv(
                "OCSR_ENABLED",
                str(env_values.get("OCSR_ENABLED", "true")),
            ).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }

        self.sqlite_db_path = _resolve_from_root(raw_sqlite_db_path)
        self.csv_source_path = _resolve_from_root(raw_csv_source_path)
        self.property_filter_csv_path = _resolve_from_root(raw_property_filter_csv_path)
        self.experimental_process_csv_path = _resolve_from_root(raw_experimental_process_csv_path)
        self.experimental_property_csv_path = _resolve_from_root(raw_experimental_property_csv_path)
        self.knowledge_zip_path = _resolve_from_root(raw_knowledge_zip_path)
        self.fumol_zip_path = _resolve_from_root(raw_fumol_zip_path)
        self.fumol_db_path = _resolve_from_root(raw_fumol_db_path)
        self.pi_reverse_db_path = _resolve_from_root(raw_pi_reverse_db_path)
        self.legacy_main_sqlite_source_path = _absolute_from_root(raw_legacy_main_sqlite_source_path)
        self.legacy_pi_sqlite_source_path = _absolute_from_root(raw_legacy_pi_sqlite_source_path)
        self.legacy_dft_sqlite_source_path = _absolute_from_root(raw_legacy_dft_sqlite_source_path)
        self.pi_reverse_csv_path = (
            _resolve_from_root(raw_pi_reverse_csv_path.strip())
            if raw_pi_reverse_csv_path.strip()
            else ""
        )
        self.pi_reverse_backend = raw_pi_reverse_backend.strip().lower()
        if self.pi_reverse_backend != "postgres":
            raise ValueError("PI_REVERSE_BACKEND must be 'postgres'")
        self.structured_data_backend = raw_structured_data_backend.strip().lower()
        if self.structured_data_backend != "postgres":
            raise ValueError("STRUCTURED_DATA_BACKEND must be 'postgres'")
        self.deployment_drain_enabled = bool(raw_deployment_drain_enabled)
        self.app_postgres_dsn = raw_app_postgres_dsn.strip()
        self.pi_postgres_dsn = raw_pi_postgres_dsn.strip()
        self.lab_data_postgres_dsn = raw_lab_data_postgres_dsn.strip()
        self.pi_reverse_tg_window_celsius = float(raw_pi_reverse_tg_window_celsius)
        self.pi_reverse_tg_max_window_celsius = float(raw_pi_reverse_tg_max_window_celsius)
        self.pi_reverse_max_scan_rows = int(raw_pi_reverse_max_scan_rows)
        self.pi_reverse_timeout_seconds = float(raw_pi_reverse_timeout_seconds)
        self.pi_reverse_job_workers = max(1, int(raw_pi_reverse_job_workers))
        self.pi_reverse_job_batch_size = max(1, int(raw_pi_reverse_job_batch_size))
        self.pi_reverse_progress_interval_rows = max(1, int(raw_pi_reverse_progress_interval_rows))
        self.structure_3d_timeout_seconds = max(1.0, float(raw_structure_3d_timeout_seconds))
        self.monomer_md_worker_base_url = raw_monomer_md_worker_base_url.strip().rstrip("/")
        self.monomer_md_worker_timeout_seconds = max(1.0, float(raw_monomer_md_worker_timeout_seconds))
        self.monomer_md_default_steps = max(1, int(raw_monomer_md_default_steps))
        self.monomer_md_submit_enabled = bool(raw_monomer_md_submit_enabled)
        self.monomer_md_rate_limit_per_ip_per_minute = max(1, int(raw_monomer_md_rate_limit_per_ip_per_minute))
        self.monomer_md_rate_limit_window_seconds = max(1, int(raw_monomer_md_rate_limit_window_seconds))
        self.monomer_md_max_active_jobs = max(1, int(raw_monomer_md_max_active_jobs))
        self.monomer_dft_worker_base_url = raw_monomer_dft_worker_base_url.strip().rstrip("/")
        self.monomer_dft_worker_uds = (
            _absolute_from_root(raw_monomer_dft_worker_uds.strip())
            if raw_monomer_dft_worker_uds.strip()
            else ""
        )
        self.monomer_dft_worker_timeout_seconds = max(
            1.0, float(raw_monomer_dft_worker_timeout_seconds)
        )
        self.monomer_dft_submit_enabled = bool(raw_monomer_dft_submit_enabled)
        self.monomer_dft_max_active_jobs = int(raw_monomer_dft_max_active_jobs)
        if self.monomer_dft_max_active_jobs != 9:
            raise ValueError(
                "MONOMER_DFT_MAX_ACTIVE_JOBS must be exactly 9 "
                "(one running job plus eight queued jobs)"
            )
        self.monomer_dft_reconcile_interval_seconds = max(
            0.25, float(raw_monomer_dft_reconcile_interval_seconds)
        )
        self.monomer_dft_artifact_retention_days = max(
            1, int(raw_monomer_dft_artifact_retention_days)
        )
        self.monomer_dft_validation_concurrency = int(
            raw_monomer_dft_validation_concurrency
        )
        if not 1 <= self.monomer_dft_validation_concurrency <= 4:
            raise ValueError(
                "MONOMER_DFT_VALIDATION_CONCURRENCY must be between 1 and 4"
            )
        self.monomer_dft_download_max_concurrent = int(
            raw_monomer_dft_download_max_concurrent
        )
        if self.monomer_dft_download_max_concurrent != 2:
            raise ValueError("MONOMER_DFT_DOWNLOAD_MAX_CONCURRENT must be exactly 2")
        self.monomer_dft_download_spool_root = _absolute_from_root(
            raw_monomer_dft_download_spool_root.strip()
        )
        if self.monomer_dft_submit_enabled and not self.monomer_dft_worker_uds:
            raise ValueError(
                "MONOMER_DFT_WORKER_UDS is required when "
                "MONOMER_DFT_SUBMIT_ENABLED is true"
            )
        self.allowed_origins = raw_allowed_origins
        self.dev_gpu_operator_enabled = bool(raw_dev_gpu_operator_enabled)
        self.dev_gpu_operator_frontend_port = int(
            raw_dev_gpu_operator_frontend_port
        )
        if (
            self.dev_gpu_operator_enabled
            and self.dev_gpu_operator_frontend_port != 9001
        ):
            raise ValueError(
                "DEV_GPU_OPERATOR_FRONTEND_PORT must be exactly 9001 when "
                "the development GPU operator is enabled"
            )
        self.dev_gpu_operator_socket_path = _absolute_from_root(
            raw_dev_gpu_operator_socket_path.strip()
        )
        self.dev_gpu_operator_timeout_seconds = min(
            5.0,
            max(0.1, float(raw_dev_gpu_operator_timeout_seconds)),
        )
        self.model_dir = _resolve_from_root(raw_model_dir)
        self.model_enabled = bool(raw_model_enabled)
        self.online_knowledge_api_key = raw_online_knowledge_api_key.strip()
        self.online_knowledge_base_url = raw_online_knowledge_base_url.strip()
        self.online_knowledge_model = raw_online_knowledge_model.strip()
        self.online_knowledge_max_papers = min(2000, max(1, int(raw_online_knowledge_max_papers)))
        self.assistant_api_key = raw_assistant_api_key.strip()
        self.assistant_base_url = raw_assistant_base_url.strip()
        self.assistant_model = raw_assistant_model.strip()
        self.assistant_image_max_bytes = max(1, int(raw_assistant_image_max_bytes))
        self.ocsr_enabled = bool(raw_ocsr_enabled)
        self.ocsr_model_dir = _resolve_from_root(raw_ocsr_model_dir)
        self.ocsr_device = raw_ocsr_device.strip().lower()
        self.ocsr_max_image_bytes = max(1, int(raw_ocsr_max_image_bytes))
        self.gen_model_enabled = bool(raw_gen_model_enabled)
        self.gen_model_dir = _resolve_from_root(raw_gen_model_dir)
        self.gen_device = raw_gen_device.strip().lower()
        self.gen_job_workers = max(1, int(raw_gen_job_workers))
        self.gen_max_active_jobs = max(1, int(raw_gen_max_active_jobs))
        self.gpu_preload_mode = str(raw_gpu_preload_mode).strip().lower()
        if self.gpu_preload_mode not in {"lazy", "required"}:
            raise ValueError("GPU_PRELOAD_MODE must be one of: lazy, required")
        self.gpu_max_concurrent_inferences = max(1, int(raw_gpu_max_concurrent_inferences))
        self.gpu_max_waiting_inferences = max(0, int(raw_gpu_max_waiting_inferences))
        self.gpu_sync_queue_timeout_seconds = max(0.001, float(raw_gpu_sync_queue_timeout_seconds))
        self.gpu_async_queue_timeout_seconds = max(0.001, float(raw_gpu_async_queue_timeout_seconds))
        self.gpu_broker_enabled = bool(raw_gpu_broker_enabled)
        self.gpu_broker_socket_path = str(raw_gpu_broker_socket_path).strip()
        if not self.gpu_broker_socket_path:
            raise ValueError("GPU_BROKER_SOCKET_PATH must not be empty")
        self.gpu_mps_pipe_root = str(raw_gpu_mps_pipe_root).strip()
        if not self.gpu_mps_pipe_root:
            raise ValueError("GPU_MPS_PIPE_ROOT must not be empty")
        self.gpu_broker_environment = str(raw_gpu_broker_environment).strip().lower()
        if self.gpu_broker_environment not in {"prod", "dev"}:
            raise ValueError("GPU_BROKER_ENVIRONMENT must be one of: prod, dev")
        self.gpu_broker_wait_timeout_seconds = max(
            0.0, float(raw_gpu_broker_wait_timeout_seconds)
        )
        self.gpu_broker_heartbeat_interval_seconds = max(
            0.1, float(raw_gpu_broker_heartbeat_interval_seconds)
        )
        self.polytao_enabled = bool(raw_polytao_enabled)
        self.polytao_submit_enabled = self.polytao_enabled
        self.polytao_model_dir = _resolve_from_root(raw_polytao_model_dir)
        self.polytao_device = raw_polytao_device.strip().lower()
        self.polytao_model_id = str(raw_polytao_model_id).strip()
        self.polytao_model_revision = str(raw_polytao_model_revision or "").strip() or None
        self.polytao_job_threads = max(1, int(raw_polytao_job_threads))
        # Compatibility alias for callers that still use the historical name.
        self.polytao_job_workers = self.polytao_job_threads
        self.polytao_rate_limit_per_ip_per_minute = max(1, int(raw_polytao_rate_limit_per_ip_per_minute))
        self.polytao_rate_limit_window_seconds = max(1, int(raw_polytao_rate_limit_window_seconds))
        self.polytao_max_active_jobs = max(1, int(raw_polytao_max_active_jobs))
        self.retro_model_enabled = bool(raw_retro_model_enabled)
        self.retro_model_id = raw_retro_model_id.strip()
        self.retro_device = raw_retro_device.strip().lower()
        self.smipoly_enabled = bool(raw_smipoly_enabled)

    @property
    def sqlite_db_file(self) -> Path:
        return Path(self.sqlite_db_path)

    @property
    def csv_source_file(self) -> Path:
        return Path(self.csv_source_path)

    @property
    def property_filter_csv_file(self) -> Path:
        return Path(self.property_filter_csv_path)

    @property
    def experimental_process_csv_file(self) -> Path:
        return Path(self.experimental_process_csv_path)

    @property
    def experimental_property_csv_file(self) -> Path:
        return Path(self.experimental_property_csv_path)

    @property
    def knowledge_zip_file(self) -> Path:
        return Path(self.knowledge_zip_path)

    @property
    def fumol_zip_file(self) -> Path:
        return Path(self.fumol_zip_path)

    @property
    def fumol_db_file(self) -> Path:
        return Path(self.fumol_db_path)

    @property
    def pi_reverse_db_file(self) -> Path:
        return Path(self.pi_reverse_db_path)

    @property
    def legacy_main_sqlite_source_file(self) -> Path:
        return Path(self.legacy_main_sqlite_source_path)

    @property
    def legacy_pi_sqlite_source_file(self) -> Path:
        return Path(self.legacy_pi_sqlite_source_path)

    @property
    def legacy_dft_sqlite_source_file(self) -> Path:
        return Path(self.legacy_dft_sqlite_source_path)

    @property
    def pi_reverse_csv_file(self) -> Path | None:
        if not self.pi_reverse_csv_path:
            return None
        return Path(self.pi_reverse_csv_path)

    @property
    def allowed_origins_list(self) -> list[str]:
        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]

    @property
    def model_dir_path(self) -> Path:
        return Path(self.model_dir)

    @property
    def gen_model_dir_path(self) -> Path:
        return Path(self.gen_model_dir)

    @property
    def ocsr_model_dir_path(self) -> Path:
        return Path(self.ocsr_model_dir)

    @property
    def polytao_model_dir_path(self) -> Path:
        return Path(self.polytao_model_dir)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
