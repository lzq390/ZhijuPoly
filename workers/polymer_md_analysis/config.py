from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys

GIB = 1024 ** 3


@dataclass(frozen=True)
class AnalysisSettings:
    job_root: Path
    spool_root: Path
    timeout_seconds: float = 90.0
    spool_budget_bytes: int = 48 * GIB
    min_free_bytes: int = GIB
    free_fraction: float = .05
    child_python: str = sys.executable
    terminate_grace_seconds: float = 2.0
    probe_on_start: bool = True

    def __post_init__(self):
        if not self.job_root.is_absolute() or not self.spool_root.is_absolute():
            raise ValueError("analysis roots must be absolute")
        if any(part == ".." for path in (self.job_root, self.spool_root) for part in path.parts):
            raise ValueError("analysis roots must be canonical")
        if (self.timeout_seconds <= 0 or self.spool_budget_bytes <= 0 or self.min_free_bytes < 0
                or not 0 <= self.free_fraction < 1 or self.terminate_grace_seconds < 0):
            raise ValueError("invalid analysis resource budget")
        if self.job_root == self.spool_root or self.spool_root.is_relative_to(self.job_root) or self.job_root.is_relative_to(self.spool_root):
            raise ValueError("analysis spool must be independent of simulation files")

    @classmethod
    def from_env(cls):
        root = Path(__file__).resolve().parents[2]
        return cls(
            job_root=Path(os.environ.get("POLYMER_MD_ANALYSIS_JOB_ROOT", str(root / ".runtime/polymer-md/jobs"))),
            spool_root=Path(os.environ.get("POLYMER_MD_ANALYSIS_SPOOL_ROOT", str(root / ".runtime/polymer-md-analysis/spool"))),
            timeout_seconds=float(os.environ.get("POLYMER_MD_ANALYSIS_TIMEOUT_SECONDS", "90")),
            spool_budget_bytes=int(os.environ.get("POLYMER_MD_ANALYSIS_SPOOL_BUDGET_BYTES", str(48 * GIB))),
            min_free_bytes=int(os.environ.get("POLYMER_MD_ANALYSIS_MIN_FREE_BYTES", str(GIB))),
            free_fraction=float(os.environ.get("POLYMER_MD_ANALYSIS_FREE_FRACTION", ".05")),
            child_python=os.environ.get("POLYMER_MD_ANALYSIS_PYTHON", sys.executable),
        )
