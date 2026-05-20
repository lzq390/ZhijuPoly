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


class Settings:
    def __init__(
        self,
        sqlite_db_path: str | None = None,
        csv_source_path: str | None = None,
        knowledge_zip_path: str | None = None,
        fumol_zip_path: str | None = None,
        fumol_db_path: str | None = None,
        pi_reverse_db_path: str | None = None,
        pi_reverse_csv_path: str | None = None,
        pi_reverse_backend: str | None = None,
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
        allowed_origins: str | None = None,
        model_enabled: bool | None = None,
        model_dir: str | None = None,
        online_knowledge_api_key: str | None = None,
        online_knowledge_base_url: str | None = None,
        online_knowledge_model: str | None = None,
        online_knowledge_max_papers: int | None = None,
        gen_model_enabled: bool | None = None,
        gen_model_dir: str | None = None,
        gen_device: str | None = None,
        gen_job_workers: int | None = None,
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
        raw_pi_reverse_csv_path = pi_reverse_csv_path
        if raw_pi_reverse_csv_path is None:
            raw_pi_reverse_csv_path = os.getenv(
                "PI_REVERSE_CSV_PATH",
                env_values.get("PI_REVERSE_CSV_PATH", ""),
            )
        raw_pi_reverse_backend = pi_reverse_backend or os.getenv(
            "PI_REVERSE_BACKEND",
            env_values.get("PI_REVERSE_BACKEND", "sqlite"),
        )
        raw_pi_postgres_dsn = pi_postgres_dsn or os.getenv(
            "PI_POSTGRES_DSN",
            env_values.get(
                "PI_POSTGRES_DSN",
                "postgresql://polyprop:polyprop@localhost:55432/polyprop_pi",
            ),
        )
        raw_lab_data_postgres_dsn = lab_data_postgres_dsn
        if raw_lab_data_postgres_dsn is None:
            raw_lab_data_postgres_dsn = os.getenv(
                "LAB_DATA_POSTGRES_DSN",
                env_values.get("LAB_DATA_POSTGRES_DSN", ""),
            )
        raw_lab_data_postgres_dsn = raw_lab_data_postgres_dsn.strip() or raw_pi_postgres_dsn
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
        raw_allowed_origins = allowed_origins or os.getenv(
            "ALLOWED_ORIGINS",
            env_values.get("ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:3000"),
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

        self.sqlite_db_path = _resolve_from_root(raw_sqlite_db_path)
        self.csv_source_path = _resolve_from_root(raw_csv_source_path)
        self.knowledge_zip_path = _resolve_from_root(raw_knowledge_zip_path)
        self.fumol_zip_path = _resolve_from_root(raw_fumol_zip_path)
        self.fumol_db_path = _resolve_from_root(raw_fumol_db_path)
        self.pi_reverse_db_path = _resolve_from_root(raw_pi_reverse_db_path)
        self.pi_reverse_csv_path = (
            _resolve_from_root(raw_pi_reverse_csv_path.strip())
            if raw_pi_reverse_csv_path.strip()
            else ""
        )
        self.pi_reverse_backend = raw_pi_reverse_backend.strip().lower()
        if self.pi_reverse_backend not in {"sqlite", "postgres"}:
            raise ValueError("PI_REVERSE_BACKEND must be either 'sqlite' or 'postgres'")
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
        self.allowed_origins = raw_allowed_origins
        self.model_dir = _resolve_from_root(raw_model_dir)
        self.model_enabled = bool(raw_model_enabled)
        self.online_knowledge_api_key = raw_online_knowledge_api_key.strip()
        self.online_knowledge_base_url = raw_online_knowledge_base_url.strip()
        self.online_knowledge_model = raw_online_knowledge_model.strip()
        self.online_knowledge_max_papers = min(2000, max(1, int(raw_online_knowledge_max_papers)))
        self.gen_model_enabled = bool(raw_gen_model_enabled)
        self.gen_model_dir = _resolve_from_root(raw_gen_model_dir)
        self.gen_device = raw_gen_device.strip().lower()
        self.gen_job_workers = max(1, int(raw_gen_job_workers))

    @property
    def sqlite_db_file(self) -> Path:
        return Path(self.sqlite_db_path)

    @property
    def csv_source_file(self) -> Path:
        return Path(self.csv_source_path)

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
