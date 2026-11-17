"""Strict public contract for the independent PolyTAO MCP adapter."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, create_model

from app.models import POLYTAO_DESCRIPTOR_NAMES, PolytaoGenerationRequest, PolytaoJobStatus
from app.mcp_security import MCPFailure

MODEL_TOOL_ORDER = ("get_polytao_capabilities", "calculate_polytao_descriptors", "prepare_polytao_generation",
                    "get_polytao_draft", "get_polytao_job")
UI_TOOL_ORDER = ("get_polytao_draft", "update_polytao_draft", "confirm_polytao_generation", "cancel_polytao_draft")
TOOL_ORDER = MODEL_TOOL_ORDER + ("update_polytao_draft", "confirm_polytao_generation", "cancel_polytao_draft")
TOOL_NAMES = frozenset(TOOL_ORDER)
MODEL_TOOL_NAMES = frozenset(MODEL_TOOL_ORDER)
UI_TOOL_NAMES = frozenset(UI_TOOL_ORDER)
MAX_RESPONSE_BYTES = 256 * 1024
MAX_BACKEND_BYTES = 8 * 1024 * 1024
DESCRIPTOR_DESCRIPTIONS = {
    "MolWt": "Molecular weight (g/mol)", "HeavyAtomCount": "Heavy atom count",
    "NHOHCount": "NH and OH group count", "NOCount": "Nitrogen and oxygen atom count",
    "NumAliphaticCarbocycles": "Aliphatic carbocycle count", "NumAliphaticHeterocycles": "Aliphatic heterocycle count",
    "NumAliphaticRings": "Aliphatic ring count", "NumAromaticCarbocycles": "Aromatic carbocycle count",
    "NumAromaticHeterocycles": "Aromatic heterocycle count", "NumAromaticRings": "Aromatic ring count",
    "NumHAcceptors": "Hydrogen bond acceptor count", "NumHDonors": "Hydrogen bond donor count",
    "NumHeteroatoms": "Heteroatom count", "NumRotatableBonds": "Rotatable bond count", "RingCount": "Ring count",
}
DESCRIPTOR_LABELS = {
    "MolWt": "分子量", "HeavyAtomCount": "重原子数", "NHOHCount": "NH 和 OH 基团数", "NOCount": "氮氧原子数",
    "NumAliphaticCarbocycles": "脂肪族碳环数", "NumAliphaticHeterocycles": "脂肪族杂环数",
    "NumAliphaticRings": "脂肪族环总数", "NumAromaticCarbocycles": "芳香碳环数",
    "NumAromaticHeterocycles": "芳香杂环数", "NumAromaticRings": "芳香环总数",
    "NumHAcceptors": "氢键受体数", "NumHDonors": "氢键供体数", "NumHeteroatoms": "杂原子数",
    "NumRotatableBonds": "可旋转键数", "RingCount": "环总数",
}
DESCRIPTOR_HELP = {
    "MolWt": "目标分子量，单位 g/mol；属于生成条件，候选不保证精确达到该数值。",
    "HeavyAtomCount": "除氢之外的重原子数量。", "NHOHCount": "NH 与 OH 基团数量。",
    "NOCount": "氮原子与氧原子的总数量。", "NumAliphaticCarbocycles": "不含杂原子的脂肪族环数量。",
    "NumAliphaticHeterocycles": "含杂原子的脂肪族环数量。", "NumAliphaticRings": "脂肪族碳环与杂环的总数量。",
    "NumAromaticCarbocycles": "不含杂原子的芳香环数量。", "NumAromaticHeterocycles": "含杂原子的芳香环数量。",
    "NumAromaticRings": "芳香碳环与芳香杂环的总数量。", "NumHAcceptors": "氢键受体位点数量。",
    "NumHDonors": "氢键供体位点数量。", "NumHeteroatoms": "除碳和氢之外的杂原子数量。",
    "NumRotatableBonds": "按 RDKit 定义计算的可旋转键数量。", "RingCount": "按 RDKit 定义计算的总环数。",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


Descriptors = create_model("PolytaoDescriptors", __base__=StrictModel, **{
    name: (float, Field(description=DESCRIPTOR_DESCRIPTIONS[name])) for name in POLYTAO_DESCRIPTOR_NAMES
})
PartialDescriptors = create_model("PolytaoPartialDescriptors", __base__=StrictModel, **{
    name: (float | None, Field(default=None, description=DESCRIPTOR_DESCRIPTIONS[name])) for name in POLYTAO_DESCRIPTOR_NAMES
})


class CapabilitiesInput(StrictModel):
    pass


class DescriptorInput(StrictModel):
    smiles: str = Field(min_length=1, max_length=2048)


# Preserve backend defaults and bounds as the source of truth; omit input_smiles.
GenerationParameters = create_model("PolytaoGenerationParameters", __base__=StrictModel, **{
    name: (PolytaoGenerationRequest.model_fields[name].annotation, PolytaoGenerationRequest.model_fields[name])
    for name in ("candidate_count", "temperature", "top_k", "top_p", "max_length")
})
_partial_parameter_fields = {}
for _name, _field in GenerationParameters.model_fields.items():
    _optional_field = deepcopy(_field)
    _optional_field.default = None
    _partial_parameter_fields[_name] = (_field.annotation | None, _optional_field)
PartialParameters = create_model("PolytaoPartialParameters", __base__=StrictModel, **_partial_parameter_fields)


class SubmitInput(GenerationParameters):
    """Internal dispatcher input. This is deliberately not a public tool schema."""
    descriptors: Descriptors
    idempotency_key: str = Field(min_length=1, max_length=128)

    def backend_body(self) -> dict[str, Any]:
        return self.model_dump(exclude={"idempotency_key"})


class PrepareInput(GenerationParameters):
    conversation_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    descriptors: PartialDescriptors = Field(default_factory=PartialDescriptors)
    reference_smiles: str | None = Field(default=None, min_length=1, max_length=2048)


class DraftInput(StrictModel):
    draft_id: str = Field(pattern=r"^ptd_[a-f0-9]{32}$")
    conversation_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")


class ApprovedSubmitInput(DraftInput):
    revision: int = Field(ge=1)


class UpdateDraftInput(ApprovedSubmitInput):
    descriptors: PartialDescriptors = Field(default_factory=PartialDescriptors)
    parameters: PartialParameters = Field(default_factory=PartialParameters)
    reference_smiles: str | None = Field(default=None, min_length=1, max_length=2048)


class JobInput(StrictModel):
    job_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
    wait_seconds: int = Field(default=0, ge=0, le=20)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=25)


class Output(StrictModel):
    ok: Literal[True] = True
    request_id: str = ""


class DescriptorDefinition(StrictModel):
    name: str
    description: str
    unit: str | None = None


class CapabilitiesOutput(Output):
    enabled: bool
    available: bool
    submission_ready: bool
    runtime_ready: bool | None
    runtime_status: str | None
    active_jobs: int | None
    descriptors: list[DescriptorDefinition]
    parameters: dict[str, Any]
    limitations: list[str]


class DescriptorOutput(Output):
    input_smiles: str
    canonical_smiles: str
    descriptors: Descriptors
    query_time_ms: float = Field(ge=0)


class SubmitOutput(Output):
    job_id: str
    status: PolytaoJobStatus
    replayed: bool
    effective_parameters: dict[str, Any]
    poll_after_seconds: int = 2


class DraftDescriptorDefinition(DescriptorDefinition):
    label: str
    required: Literal[True] = True


class DraftValidationError(StrictModel):
    field: str
    message: str


class DraftOutput(Output):
    draft_id: str
    conversation_id: str
    revision: int = Field(ge=1)
    status: Literal["editing", "confirmed", "submitted", "submission_unknown", "rejected", "cancelled"]
    ready: bool
    editable: bool
    descriptors: dict[str, float]
    descriptor_sources: dict[str, Literal["user", "reference"]]
    parameters: GenerationParameters
    missing_fields: list[str]
    errors: list[DraftValidationError]
    descriptor_fields: list[DraftDescriptorDefinition]
    parameter_fields: dict[str, Any]
    reference_smiles: str | None = None
    canonical_smiles: str | None = None
    job_id: str | None = None
    submission_error: str | None = None
    submission: SubmitOutput | None = None
    created_at: str
    updated_at: str


class CandidateOutput(StrictModel):
    rank: int = Field(ge=1)
    generated_smiles: str
    valid_smiles: bool
    sa_score: float | None = Field(default=None, ge=0)
    warnings: list[str] = Field(default_factory=list)


class JobOutput(Output):
    job_id: str
    status: PolytaoJobStatus
    progress_stage: str
    progress_percent: int = Field(ge=0, le=100)
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    snapshot_at: str
    requested_count: int = Field(ge=1)
    returned_count: int = Field(ge=0)
    attempts: int = Field(ge=0)
    filter_counter: dict[str, int]
    results: list[CandidateOutput]
    offset: int
    limit: int
    next_offset: int | None
    error_code: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ErrorDetail(StrictModel):
    code: str
    message: str
    retryable: bool = False


class ErrorOutput(StrictModel):
    ok: Literal[False] = False
    error: ErrorDetail
    request_id: str


INPUT_MODELS = dict(zip(TOOL_ORDER, (CapabilitiesInput, DescriptorInput, PrepareInput, DraftInput,
                                   JobInput, UpdateDraftInput, ApprovedSubmitInput, ApprovedSubmitInput)))
OUTPUT_MODELS = dict(zip(TOOL_ORDER, (CapabilitiesOutput, DescriptorOutput, DraftOutput, DraftOutput,
                                    JobOutput, DraftOutput, DraftOutput, DraftOutput)))
