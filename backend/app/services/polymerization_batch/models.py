from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import PolymerizationTargetClass


class BatchError(Exception):
    def __init__(self, message: str, code: str = "invalid_input", status: int = 422):
        super().__init__(message)
        self.code, self.status = code, status


@dataclass(frozen=True)
class BatchSettings:
    enabled: bool = False
    storage_root: Path = Path(".runtime/monomer-polymerization-batch")
    file_bytes: int = 10 * 1024**2
    request_bytes: int = 22 * 1024**2
    max_rows: int = 5000
    max_pairs: int = 50000
    max_columns: int = 100
    max_cell_chars: int = 32767
    xlsx_uncompressed_bytes: int = 64 * 1024**2
    chunk_size: int = 32
    classification_size: int = 128
    queue_capacity: int = 10
    subprocess_seconds: int = 60
    memory_bytes: int = 2 * 1024**3
    job_seconds: int = 3600
    result_bytes: int = 512 * 1024**2
    import_hours: int = 24
    retention_days: int = 7
    lease_seconds: int = 90

    @classmethod
    def from_env(cls) -> BatchSettings:
        defaults = cls()
        values: dict = {
            "enabled": os.getenv("MONOMER_POLYMERIZATION_BATCH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
                and os.getenv("SMIPOLY_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
            "storage_root": Path(os.getenv("MONOMER_POLYMERIZATION_BATCH_STORAGE_ROOT", str(defaults.storage_root))).resolve(),
        }
        for name in cls.__dataclass_fields__:
            if name in values:
                continue
            value = int(os.getenv(f"MONOMER_POLYMERIZATION_BATCH_{name.upper()}", str(getattr(defaults, name))))
            if value < 1:
                raise ValueError(f"MONOMER_POLYMERIZATION_BATCH_{name.upper()} must be positive")
            values[name] = value
        if values["chunk_size"] > 32:
            raise ValueError("batch chunk_size must be <= 32")
        return cls(**values)

    def public_limits(self) -> dict:
        return {key: getattr(self, key) for key in (
            "file_bytes", "request_bytes", "max_rows", "max_pairs", "max_columns",
            "chunk_size", "queue_capacity", "retention_days", "result_bytes",
        )}


class TableMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sheet: str | None = Field(default=None, max_length=255)
    encoding: Literal["utf-8-sig", "gb18030"] = "utf-8-sig"
    smiles_column: str | None = Field(default=None, max_length=32767)
    id_column: str | None = Field(default=None, max_length=32767)
    name_column: str | None = Field(default=None, max_length=32767)


class BatchPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: TableMapping = Field(default_factory=TableMapping)
    b: TableMapping = Field(default_factory=TableMapping)


class BatchJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    import_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    preview_revision: str = Field(pattern=r"^[0-9a-f]{32}$")
    target_class: PolymerizationTargetClass = "polyimide"


TERMINAL_STATUSES = {"completed", "completed_with_errors", "failed", "cancelled", "expired"}
ACTIVE_STATUSES = {"queued", "running", "cancelling"}


BatchPairStatus = Literal["success", "no_match", "invalid_input", "error", "not_processed"]
BatchJobStatus = Literal["queued", "running", "cancelling", "completed", "completed_with_errors", "failed", "cancelled", "expired"]


class BatchArtifact(BaseModel):
    name: str
    url: str
    size_bytes: int
    sha256: str
    media_type: str


class BatchJob(BaseModel):
    job_id: str
    status: BatchJobStatus
    stage: str
    target_class: PolymerizationTargetClass
    summary: dict
    artifacts: dict[str, BatchArtifact]
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    expires_at: datetime | None
    error_code: str | None
    message: str | None


class BatchCandidate(BaseModel):
    pair_id: str
    a_row: int
    b_row: int
    a_id: str
    b_id: str
    a_name: str
    b_name: str
    a_input_smiles: str
    b_input_smiles: str
    a_canonical_smiles: str
    b_canonical_smiles: str
    candidate_index: int
    polymer_smiles: str
    polymer_class: str
    reaction_id: int | None
    engine_mon1_smiles: str
    engine_mon2_smiles: str
    reactset: list[str]


class BatchResults(BaseModel):
    items: list[BatchCandidate]
    total: int
    next_offset: int | None


class BatchFile(BaseModel):
    filename: str
    format: str
    size_bytes: int
    sha256: str


class BatchTablePreview(BaseModel):
    headers: list[str]
    sheets: list[str]
    sheet: str | None
    row_count: int
    blank_rows: int
    sample: list[dict[str, str]]
    mapping: TableMapping
    error: str | None = None


class BatchImport(BaseModel):
    import_id: str
    expires_at: datetime
    files: dict[str, BatchFile]
    tables: dict[str, BatchTablePreview]


class BatchImportPreview(BatchImport):
    preview_revision: str | None
    can_submit: bool
    statistics: dict | None
    input_errors: list[dict]
    input_error_count: int = 0
