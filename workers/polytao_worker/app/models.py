from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


POLYTAO_DESCRIPTOR_NAMES: tuple[str, ...] = (
    "MolWt",
    "HeavyAtomCount",
    "NHOHCount",
    "NOCount",
    "NumAliphaticCarbocycles",
    "NumAliphaticHeterocycles",
    "NumAliphaticRings",
    "NumAromaticCarbocycles",
    "NumAromaticHeterocycles",
    "NumAromaticRings",
    "NumHAcceptors",
    "NumHDonors",
    "NumHeteroatoms",
    "NumRotatableBonds",
    "RingCount",
)


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    job_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    descriptors: dict[str, float]
    prompt: str = Field(min_length=1, max_length=1024)
    input_smiles: str | None = Field(default=None, max_length=2048)
    canonical_smiles: str | None = Field(default=None, max_length=2048)
    candidate_count: int = Field(default=10, ge=1, le=50)
    temperature: float = Field(default=1.0, ge=0.1, le=2.0, allow_inf_nan=False)
    top_k: int = Field(default=100, ge=1, le=500)
    top_p: float = Field(default=0.999, gt=0.0, le=1.0, allow_inf_nan=False)
    max_length: int = Field(default=300, ge=16, le=512)

    @field_validator("descriptors")
    @classmethod
    def validate_descriptors(cls, descriptors: dict[str, float]) -> dict[str, float]:
        required = set(POLYTAO_DESCRIPTOR_NAMES)
        actual = set(descriptors)
        missing = sorted(required - actual)
        extra = sorted(actual - required)
        if missing:
            raise ValueError("missing PolyTAO descriptors: " + ", ".join(missing))
        if extra:
            raise ValueError("unknown PolyTAO descriptors: " + ", ".join(extra))
        return descriptors


class JobAccepted(BaseModel):
    job_id: str
    status: str
    mode: str
    worker_id: str
    worker_job_id: str
    worker_version: str


class HealthResponse(BaseModel):
    status: str
    mode: str
    db_configured: bool
    model_dir: str
    model_files_ready: bool
    runtime_ready: bool
    runtime_error: str | None = None
    active_jobs: int
    model_id: str
    model_revision: str | None = None
    default_params: dict[str, float | int]
    worker_id: str
    worker_version: str
