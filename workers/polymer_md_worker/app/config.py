from __future__ import annotations

from dataclasses import dataclass
import os
import math
from pathlib import Path


@dataclass(frozen=True)
class WorkerSettings:
    job_root: Path
    runner_python: str
    authorization_url: str
    authorization_token: str
    runtime_manifest: str | None = None
    max_queued_jobs: int = 8
    stage_timeout_seconds: float = 24 * 3600
    job_timeout_seconds: float = 96 * 3600
    start_drained: bool = False
    max_job_bytes: int = 20 * 1024**3
    poll_seconds: float = 0.5
    cpu_count: int = 16
    memory_bytes: int = 32 * 1024**3

    def __post_init__(self):
        if self.cpu_count != 16 or self.memory_bytes != 32 * 1024**3:
            raise ValueError("development worker requires the fixed 16 CPU / 32 GiB scope")
        if not 1 <= self.max_queued_jobs <= 100:
            raise ValueError("invalid worker queue capacity")
        if any(isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in
               (self.stage_timeout_seconds, self.job_timeout_seconds, self.poll_seconds)):
            raise ValueError("invalid worker time budget")
        if not isinstance(self.start_drained, bool):
            raise ValueError("invalid worker startup maintenance policy")


def load_settings() -> WorkerSettings:
    repo = Path(__file__).resolve().parents[3]
    startup = os.environ.get("POLYMER_MD_START_DRAINED", "true")
    if startup not in {"true", "false"}:
        raise ValueError("POLYMER_MD_START_DRAINED must be true or false")
    return WorkerSettings(
        job_root=Path(os.environ.get("POLYMER_MD_JOB_ROOT", str(repo / ".runtime/polymer-md/jobs"))),
        runner_python=os.environ.get("POLYMER_MD_RUNNER_PYTHON", str(repo / ".runtime/polymer-md/qm-environment/bin/python")),
        authorization_url=os.environ.get("POLYMER_MD_START_AUTHORIZATION_URL", "http://127.0.0.1:8000/internal/polymer-md"),
        authorization_token=os.environ.get("POLYMER_MD_START_AUTHORIZATION_TOKEN", ""),
        runtime_manifest=os.environ.get("POLYMER_MD_RUNTIME_MANIFEST"),
        max_queued_jobs=int(os.environ.get("POLYMER_MD_MAX_QUEUED_JOBS", "8")),
        start_drained=startup == "true",
    )
