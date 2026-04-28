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
        allowed_origins: str | None = None,
        model_enabled: bool | None = None,
        model_dir: str | None = None,
    ) -> None:
        env_values = dotenv_values(DEFAULT_ENV_FILE) if DEFAULT_ENV_FILE.exists() else {}

        raw_sqlite_db_path = sqlite_db_path or os.getenv(
            "SQLITE_DB_PATH",
            env_values.get("SQLITE_DB_PATH", "backend/data/polyprop.db"),
        )
        raw_csv_source_path = csv_source_path or os.getenv(
            "CSV_SOURCE_PATH",
            env_values.get("CSV_SOURCE_PATH", "database/polyprop_9_properties_clean.csv"),
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
        raw_allowed_origins = allowed_origins or os.getenv(
            "ALLOWED_ORIGINS",
            env_values.get("ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:3000"),
        )
        raw_model_dir = model_dir or os.getenv(
            "MODEL_DIR",
            env_values.get("MODEL_DIR", "model"),
        )
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
        self.allowed_origins = raw_allowed_origins
        self.model_dir = _resolve_from_root(raw_model_dir)
        self.model_enabled = bool(raw_model_enabled)

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
    def allowed_origins_list(self) -> list[str]:
        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]

    @property
    def model_dir_path(self) -> Path:
        return Path(self.model_dir)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
