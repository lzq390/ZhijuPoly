from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    host: str
    port: int
    mode: str
    worker_id: str
    worker_version: str
    app_postgres_dsn: str
    db_configured: bool
    model_dir: Path
    model_id: str
    model_revision: str | None
    device: str
    max_active_jobs: int
    max_concurrent_jobs: int
    health_probe_timeout_seconds: float
    default_candidate_count: int
    default_temperature: float
    default_top_k: int
    default_top_p: float
    default_max_length: int


def load_settings() -> WorkerSettings:
    dsn = os.getenv("APP_POSTGRES_DSN", "").strip()
    model_revision = os.getenv("POLYTAO_MODEL_REVISION", "").strip() or None
    return WorkerSettings(
        host=os.getenv("POLYTAO_WORKER_HOST", "0.0.0.0").strip(),
        port=int(os.getenv("POLYTAO_WORKER_PORT", "8020")),
        mode=os.getenv("POLYTAO_WORKER_MODE", "real").strip().lower(),
        worker_id=os.getenv("POLYTAO_WORKER_ID", "polytao-worker").strip(),
        worker_version=os.getenv("POLYTAO_WORKER_VERSION", "0.1.0").strip(),
        app_postgres_dsn=dsn,
        db_configured=bool(dsn),
        model_dir=Path(os.getenv("POLYTAO_MODEL_DIR", "/app/model/polytao")).expanduser(),
        model_id=os.getenv("POLYTAO_MODEL_ID", "hkqiu/PolymerGenerationPretrainedModel").strip(),
        model_revision=model_revision,
        device=os.getenv("POLYTAO_DEVICE", "auto").strip().lower(),
        max_active_jobs=max(1, int(os.getenv("POLYTAO_MAX_ACTIVE_JOBS", "1"))),
        max_concurrent_jobs=max(1, int(os.getenv("POLYTAO_MAX_CONCURRENT_JOBS", "1"))),
        health_probe_timeout_seconds=max(1.0, float(os.getenv("POLYTAO_HEALTH_PROBE_TIMEOUT_SECONDS", "10"))),
        default_candidate_count=max(1, int(os.getenv("POLYTAO_DEFAULT_CANDIDATE_COUNT", "10"))),
        default_temperature=float(os.getenv("POLYTAO_DEFAULT_TEMPERATURE", "1.0")),
        default_top_k=max(1, int(os.getenv("POLYTAO_DEFAULT_TOP_K", "100"))),
        default_top_p=float(os.getenv("POLYTAO_DEFAULT_TOP_P", "0.999")),
        default_max_length=max(16, int(os.getenv("POLYTAO_DEFAULT_MAX_LENGTH", "300"))),
    )
