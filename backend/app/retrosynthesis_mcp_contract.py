"""Scientific-runtime-independent public contract for retrosynthesis MCP."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

MODEL_TOOL_ORDER = ("get_retrosynthesis_capabilities", "resolve_retrosynthesis_target",
                    "prepare_monomer_retrosynthesis", "get_retrosynthesis_draft", "get_retrosynthesis_job")
UI_TOOL_ORDER = ("resolve_retrosynthesis_target", "get_retrosynthesis_draft", "update_retrosynthesis_draft",
                 "confirm_monomer_retrosynthesis", "cancel_retrosynthesis_draft")
TOOL_ORDER = MODEL_TOOL_ORDER + UI_TOOL_ORDER[2:]
TOOL_NAMES, MODEL_TOOL_NAMES, UI_TOOL_NAMES = map(frozenset, (TOOL_ORDER, MODEL_TOOL_ORDER, UI_TOOL_ORDER))
MAX_RESPONSE_BYTES = 256 * 1024
MAX_BACKEND_BYTES = 2 * 1024 * 1024
CONVERSATION_PATTERN = r"^[A-Za-z0-9_.-]{1,128}$"
JobStatus = Literal["pending", "queued", "running", "completed", "failed", "timed_out", "interrupted", "unknown"]
Role = Literal["auto", "diamine", "dianhydride", "other"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Parameters(StrictModel):
    target_role: Role = "auto"
    num_return_sequences: int = Field(default=5, ge=1, le=10)
    num_beams: int = Field(default=5, ge=1, le=20)
    max_new_tokens: int = Field(default=128, ge=16, le=256)

    @model_validator(mode="after")
    def beam_bounds(self):
        if self.num_return_sequences > self.num_beams:
            raise ValueError("Candidate count cannot exceed beam count")
        return self


class PartialParameters(StrictModel):
    target_role: Role | None = None
    num_return_sequences: int | None = Field(default=None, ge=1, le=10)
    num_beams: int | None = Field(default=None, ge=1, le=20)
    max_new_tokens: int | None = Field(default=None, ge=16, le=256)


class TargetInput(StrictModel):
    source: Literal["smiles", "name", "saved_structure"]
    smiles: str | None = Field(default=None, min_length=1, max_length=8000)
    name: str | None = Field(default=None, min_length=1, max_length=1000)
    record_id: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
    query: str | None = Field(default=None, max_length=1000)
    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=20, ge=1, le=20)

    @model_validator(mode="after")
    def source_fields(self):
        supplied = {key for key in ("smiles", "name", "record_id", "query", "cursor") if getattr(self, key) is not None}
        allowed = {"smiles": {"smiles"}, "name": {"name"},
                   "saved_structure": {"record_id", "query", "cursor"}}[self.source]
        if not supplied <= allowed or (self.source == "smiles" and not self.smiles) or (self.source == "name" and not self.name):
            raise ValueError("Target fields do not match source")
        if self.record_id is not None and (self.query is not None or self.cursor is not None):
            raise ValueError("Choose a record or a search")
        return self


class TargetSource(StrictModel):
    type: Literal["smiles", "name", "saved_structure"]
    label: str | None = None
    record_id: str | None = None


class ResolvedTarget(StrictModel):
    canonical_smiles: str = Field(min_length=1, max_length=8000)
    input_smiles: str = Field(min_length=1, max_length=8000)
    inferred_target_role: Role
    preview_svg: str | None = None
    source: TargetSource


class CapabilitiesInput(StrictModel):
    pass


class ConversationInput(StrictModel):
    conversation_id: str = Field(pattern=CONVERSATION_PATTERN)


class ResolveInput(ConversationInput):
    target: TargetInput


class PrepareInput(ConversationInput):
    target: TargetInput | None = None
    parameters: PartialParameters = Field(default_factory=PartialParameters)


class DraftInput(ConversationInput):
    draft_id: str = Field(pattern=r"^rtd_[a-f0-9]{32}$")


class ApprovedInput(DraftInput):
    revision: int = Field(ge=1)


class UpdateInput(ApprovedInput):
    target: TargetInput | None = None
    parameters: PartialParameters = Field(default_factory=PartialParameters)


class JobInput(ConversationInput):
    job_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
    wait_seconds: int = Field(default=0, ge=0, le=20)


class Output(StrictModel):
    ok: Literal[True] = True
    request_id: str = ""


class CapabilitiesOutput(Output):
    enabled: bool
    available: bool
    schema_ready: bool
    accepting: bool
    model_available: bool
    name_resolution_available: bool
    structure_library_available: bool
    submission_ready: bool
    parameters: dict[str, Any]
    limitations: list[str]


class ResolveOutput(Output):
    status: Literal["resolved", "needs_clarification"]
    target: ResolvedTarget | None = None
    candidates: list[ResolvedTarget] = Field(default_factory=list, max_length=20)
    next_cursor: str | None = None
    message: str | None = None

    @model_validator(mode="after")
    def resolved_target_required(self):
        if (self.status == "resolved") != (self.target is not None):
            raise ValueError("Resolved status requires exactly one target")
        return self


class SubmissionOutput(Output):
    job_id: str
    status: JobStatus
    replayed: bool
    effective_parameters: dict[str, Any]
    poll_after_seconds: int = 2


class DraftOutput(Output):
    draft_id: str
    conversation_id: str
    revision: int
    status: Literal["editing", "confirmed", "submitted", "submission_unknown", "rejected", "cancelled"]
    ready: bool
    editable: bool
    target: TargetInput | None
    resolved_target: ResolvedTarget | None
    target_candidates: list[ResolvedTarget]
    next_cursor: str | None = None
    parameters: Parameters
    explicit_parameters: list[str] = Field(default_factory=list)
    parameter_fields: dict[str, Any]
    missing_fields: list[str]
    errors: list[dict[str, str]]
    job_id: str | None = None
    submission_error: str | None = None
    submission: SubmissionOutput | None = None
    created_at: str
    updated_at: str


class Reactant(StrictModel):
    input_smiles: str
    canonical_smiles: str | None
    valid_smiles: bool
    heavy_atom_count: int | None


class Candidate(StrictModel):
    rank: int = Field(ge=1)
    reactants_smiles: str
    canonical_reactants_smiles: str | None
    reactants: list[Reactant]
    valid_smiles: bool
    all_reactants_smaller_than_target: bool | None = None
    reaction_hint: str | None = None


class PredictionResult(StrictModel):
    input_smiles: str
    canonical_smiles: str
    target_role: Role
    inferred_target_role: Role
    model_id: str
    query_time_ms: float = Field(ge=0)
    total: int = Field(ge=0, le=10)
    candidates: list[Candidate] = Field(max_length=10)

    @model_validator(mode="after")
    def candidate_count(self):
        if self.total != len(self.candidates):
            raise ValueError("Candidate count mismatch")
        ranks = [candidate.rank for candidate in self.candidates]
        if ranks != sorted(set(ranks)):
            raise ValueError("Candidate ranks must be increasing and unique")
        return self


class JobOutput(Output):
    job_id: str
    status: JobStatus
    stage: str
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    snapshot_at: str
    error_code: str | None = None
    result: PredictionResult | None = None
    warnings: list[str] = Field(default_factory=list)


class ErrorDetail(StrictModel):
    code: str
    message: str
    retryable: bool = False


class ErrorOutput(StrictModel):
    ok: Literal[False] = False
    error: ErrorDetail
    request_id: str


INPUT_MODELS = dict(zip(TOOL_ORDER, (CapabilitiesInput, ResolveInput, PrepareInput, DraftInput,
                                   JobInput, UpdateInput, ApprovedInput, ApprovedInput)))
OUTPUT_MODELS = dict(zip(TOOL_ORDER, (CapabilitiesOutput, ResolveOutput, DraftOutput, DraftOutput,
                                    JobOutput, DraftOutput, DraftOutput, DraftOutput)))
