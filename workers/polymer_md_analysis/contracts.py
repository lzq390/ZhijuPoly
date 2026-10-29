from __future__ import annotations

from uuid import UUID
from pydantic import Field, model_validator
from polymer_md.contracts import Artifact, Contract, DistanceRequest, SafeId, Sha256


class SourceRequest(Contract):
    job_id: UUID
    attempt_token: Sha256
    stage_run_id: SafeId
    stage: SafeId
    artifacts: list[Artifact] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def source_identity(self):
        prefix = f"{self.job_id}/{self.attempt_token}/"
        if len({a.artifact_id for a in self.artifacts}) != len(self.artifacts):
            raise ValueError("artifact IDs must be unique")
        if any(a.stage_run_id != self.stage_run_id or not a.storage_ref or not a.storage_ref.startswith(prefix)
               or a.storage_ref.rsplit("/", 1)[-1] != a.filename for a in self.artifacts):
            raise ValueError("artifact does not belong to this stage execution")
        return self


class DistanceAnalysisRequest(SourceRequest):
    distance: DistanceRequest


class TrajectoryAnalysisRequest(SourceRequest):
    frame_index: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=10000, ge=1, le=10000, strict=True)


class AnalysisError(Exception):
    def __init__(self, code: str, message: str, status: int = 503):
        super().__init__(message)
        self.code, self.status = code, status

    def body(self):
        return {"code": self.code, "message": str(self), "retryable": self.status >= 500 or self.status == 429}
