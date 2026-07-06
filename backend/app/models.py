from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MatchMode = Literal["structure", "property"]
SmilesLookupTable = Literal["polymers", "properties", "pi_candidates"]
RetrosynthesisTargetRole = Literal["auto", "diamine", "dianhydride", "other"]


class SmilesQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    smiles: str = Field(min_length=1)
    match_mode: MatchMode = "structure"
    similarity_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    top_k: int = Field(default=10, ge=1, le=100)
    property_name: str | None = None

    @field_validator("match_mode", mode="before")
    @classmethod
    def normalize_match_mode(cls, match_mode: object) -> object:
        legacy_modes = {
            "exact": "structure",
            "similarity": "property",
        }
        if isinstance(match_mode, str):
            return legacy_modes.get(match_mode, match_mode)
        return match_mode

    @field_validator("property_name")
    @classmethod
    def validate_property_name(cls, property_name: str | None) -> str | None:
        if property_name is None:
            return None

        normalized = property_name.strip()
        if not normalized:
            raise ValueError("property_name must not be empty")
        return normalized


class PropertyItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    property_category: str
    property_name: str
    property_value: str
    property_value_num: float | None = None
    property_unit: str | None = None
    label_source: str | None = None


class PropertyGroups(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thermal: list[PropertyItem] = Field(default_factory=list)
    mechanical: list[PropertyItem] = Field(default_factory=list)
    electrical: list[PropertyItem] = Field(default_factory=list)
    chemical: list[PropertyItem] = Field(default_factory=list)
    optical: list[PropertyItem] = Field(default_factory=list)
    other: list[PropertyItem] = Field(default_factory=list)


class PolymerResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    polymer_id: str
    polymer_name: str
    smiles: str
    canonical_smiles: str | None = None
    similarity_score: float | None = Field(default=None, ge=0.0, le=1.0)
    structure_svg: str | None = None
    matched_property_name: str | None = None
    matched_property_value: float | None = None
    matched_property_unit: str | None = None
    matched_property_source: str | None = None
    properties: PropertyGroups


class SmilesQueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match_type: MatchMode
    query_time_ms: float = Field(ge=0.0)
    total: int = Field(ge=0)
    predicted_property_name: str | None = None
    predicted_property_value: float | None = None
    predicted_property_unit: str | None = None
    results: list[PolymerResult] = Field(default_factory=list)


class PredictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    smiles: str = Field(min_length=1)
    properties: list[str] = Field(min_length=1)

    @field_validator("properties")
    @classmethod
    def validate_properties(cls, properties: list[str]) -> list[str]:
        normalized = [value.strip() for value in properties]
        if any(not value for value in normalized):
            raise ValueError("prediction properties must not be empty")
        return normalized


class PredictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    predictions: dict[str, float]
    query_time_ms: float = Field(ge=0.0)


class Structure3DRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    smiles: str = Field(min_length=1)


class Structure3DResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    molblock: str
    capped_smiles: str
    format: Literal["mol"]


class SmilesStandardizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    smiles: str = Field(min_length=1)


class SmilesStandardizeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    input_smiles: str
    standardized_smiles: str
    changed: bool
    query_time_ms: float = Field(ge=0.0)


class StructureImageRecognitionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    smiles: str = Field(min_length=1)
    molfile: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list)
    query_time_ms: float = Field(ge=0.0)

    @field_validator("smiles")
    @classmethod
    def validate_smiles(cls, smiles: str) -> str:
        normalized = smiles.strip()
        if not normalized:
            raise ValueError("smiles must not be empty")
        return normalized


class MonomerRetrosynthesisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    smiles: str = Field(min_length=1)
    target_role: RetrosynthesisTargetRole = "auto"
    num_beams: int = Field(default=5, ge=1, le=20)
    num_return_sequences: int = Field(default=5, ge=1, le=10)
    max_new_tokens: int = Field(default=128, ge=16, le=256)

    @model_validator(mode="after")
    def validate_generation_shape(self) -> "MonomerRetrosynthesisRequest":
        if self.num_return_sequences > self.num_beams:
            raise ValueError("num_return_sequences must be less than or equal to num_beams")
        return self


class RetrosynthesisReactant(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    input_smiles: str
    canonical_smiles: str | None = None
    valid_smiles: bool
    heavy_atom_count: int | None = Field(default=None, ge=0)


class MonomerRetrosynthesisCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rank: int = Field(ge=1)
    raw_output: str
    reactants_smiles: str
    canonical_reactants_smiles: str | None = None
    reactants: list[RetrosynthesisReactant] = Field(default_factory=list)
    valid_smiles: bool
    all_reactants_smaller_than_target: bool | None = None
    reaction_hint: str


class MonomerRetrosynthesisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    input_smiles: str
    canonical_smiles: str
    target_role: RetrosynthesisTargetRole
    inferred_target_role: Literal["diamine", "dianhydride", "other"]
    model_id: str
    device: str
    query_time_ms: float = Field(ge=0.0)
    total: int = Field(ge=0)
    candidates: list[MonomerRetrosynthesisCandidate] = Field(default_factory=list)


class SmilesLookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    smiles: str = Field(min_length=1)
    table: SmilesLookupTable


class SmilesLookupResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    record_id: str
    source_column: str
    smiles: str
    canonical_smiles: str | None = None
    structure_svg: str | None = None
    summary: str
    fields: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class SmilesLookupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query_smiles: str
    canonical_smiles: str
    table: SmilesLookupTable
    exists: bool
    total: int = Field(ge=0)
    query_time_ms: float = Field(ge=0.0)
    results: list[SmilesLookupResult] = Field(default_factory=list)


class StructurePropertyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    property_id: int
    polymer_id: int
    smiles: str
    canonical_smiles: str | None = None
    property_category: str
    property_name: str
    property_value: str
    property_value_num: float | None = None
    property_unit: str | None = None
    label_source: str | None = None


class StructurePropertyBrowseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    query_time_ms: float = Field(ge=0.0)
    total_records: int = Field(ge=0)
    matched_records: int = Field(ge=0)
    data_source: str = "postgres"
    source_status: str = "ready"
    source_message: str | None = None
    results: list[StructurePropertyRecord] = Field(default_factory=list)


class DftMoleculeBrowserRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mol_id: str
    range_group: str
    final_step: int
    n_atoms: int
    trace_points: int
    scf_energy: float | None = None
    zero_point_energy: float | None = None
    thermal_enthalpy: float | None = None
    gibbs_free_energy: float | None = None
    lowest_freq: float | None = None
    dipole_moment: float | None = None
    homo_ev: float | None = None
    lumo_ev: float | None = None
    gap_ev: float | None = None
    is_converged: str | None = None


class DftMoleculeBrowseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    query_time_ms: float = Field(ge=0.0)
    total_records: int = Field(ge=0)
    matched_records: int = Field(ge=0)
    total_step_records: int = Field(ge=0)
    average_steps: float = Field(ge=0.0)
    max_steps: int = Field(ge=0)
    data_source: str = "postgres"
    source_status: str = "ready"
    source_message: str | None = None
    results: list[DftMoleculeBrowserRecord] = Field(default_factory=list)


class DftEnergyStepRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mol_id: str
    step: int
    scf_energy: float | None = None
    homo_ev: float | None = None
    lumo_ev: float | None = None
    gap_ev: float | None = None


class DftEnergyStepBrowseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    query_time_ms: float = Field(ge=0.0)
    total_records: int = Field(ge=0)
    matched_records: int = Field(ge=0)
    data_source: str = "postgres"
    source_status: str = "ready"
    source_message: str | None = None
    results: list[DftEnergyStepRecord] = Field(default_factory=list)


class ExperimentalProcessRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_file: str
    source_row_number: int
    polymer_id: str
    polymer_name: str
    product_name: str
    process_flow_original_text: str
    material_original_text: str


class ExperimentalProcessBrowseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    query_time_ms: float = Field(ge=0.0)
    total_records: int = Field(ge=0)
    matched_records: int = Field(ge=0)
    data_source: str = "csv"
    source_status: str = "ready"
    source_message: str | None = None
    results: list[ExperimentalProcessRecord] = Field(default_factory=list)


class ExperimentalPropertyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_file: str
    source_row_number: int
    polymer_id: str
    polymer_name: str
    property_category: str | None = None
    property_name_en: str
    value: str


class ExperimentalPropertyBrowseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    query_time_ms: float = Field(ge=0.0)
    total_records: int = Field(ge=0)
    matched_records: int = Field(ge=0)
    data_source: str = "csv"
    source_status: str = "ready"
    source_message: str | None = None
    results: list[ExperimentalPropertyRecord] = Field(default_factory=list)


class FormulationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    formulation_id: int
    knowledge_id: int
    source_file: str
    source_row_number: int
    polymer_iupac: str | None = None
    formulation: str | None = None
    catalyst: str | None = None
    temperature: str | None = None
    reaction_time: str | None = None
    solvent: str | None = None


class FormulationBrowseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    query_time_ms: float = Field(ge=0.0)
    total_records: int = Field(ge=0)
    matched_records: int = Field(ge=0)
    data_source: str = "postgres"
    source_status: str = "ready"
    source_message: str | None = None
    results: list[FormulationRecord] = Field(default_factory=list)


class DatasetSummaryItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str
    title: str
    total_records: int = Field(ge=0)
    data_source: str
    source_status: str
    source_message: str | None = None
    latest_import_status: str | None = None
    latest_import_finished_at: str | None = None


class DatasetSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query_time_ms: float = Field(ge=0.0)
    backend: str
    datasets: list[DatasetSummaryItem] = Field(default_factory=list)


class DatabaseAnalyticsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_time_ms: float = Field(ge=0.0)
    backend: str
    source: str = "snapshot"
    generated_at: str | None = None
    datasets: dict[str, Any] = Field(default_factory=dict)


class KnowledgeSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1)
    top_k: int = Field(default=25, ge=1, le=100)
    page: int = Field(default=1, ge=1)
    page_size: int | None = Field(default=None, ge=1, le=100)
    terms: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("terms")
    @classmethod
    def validate_terms(cls, terms: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()

        for term in terms:
            value = term.strip()
            if not value:
                continue

            key = value.casefold()
            if key in seen:
                continue

            seen.add(key)
            normalized.append(value)

        return normalized


class KnowledgeDocumentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    knowledge_id: int
    source_file: str
    source_row_number: int
    source_sequence: str | None = None
    title_zh: str | None = None
    title_en: str | None = None
    abstract: str
    abstract_snippet: str
    claim: str | None = None
    analysis: str | None = None
    is_polymer_synthesis: str | None = None
    judgement_reason: str | None = None
    polymer_iupac: str | None = None
    formulation: str | None = None
    catalyst: str | None = None
    temperature: str | None = None
    reaction_time: str | None = None
    solvent: str | None = None
    matched_terms: list[str] = Field(default_factory=list)
    matched_fields: list[str] = Field(default_factory=list)


class KnowledgeSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    terms: list[str] = Field(default_factory=list)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1)
    query_time_ms: float = Field(ge=0.0)
    total: int = Field(ge=0)
    results: list[KnowledgeDocumentResult] = Field(default_factory=list)

AssistantChatRole = Literal["user", "assistant"]


class AssistantChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    role: AssistantChatRole
    content: str = Field(min_length=1, max_length=8000)


class AssistantModuleContext(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=120)
    route: str = Field(min_length=1, max_length=160)
    group: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=400)


class AssistantChatContext(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    active_module: str | None = Field(default=None, max_length=80)
    modules: list[AssistantModuleContext] = Field(default_factory=list, max_length=24)


class AssistantChatStreamRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[AssistantChatMessage] = Field(min_length=1, max_length=30)
    context: AssistantChatContext = Field(default_factory=AssistantChatContext)

    @model_validator(mode="after")
    def require_latest_user_message(self) -> "AssistantChatStreamRequest":
        if self.messages[-1].role != "user":
            raise ValueError("latest assistant chat message must be from the user")
        return self

OnlineKnowledgeMode = Literal["synthesis", "property"]


class OnlineKnowledgeSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    material: str = Field(min_length=1)
    api_key: str | None = Field(default=None, repr=False)
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    mode: OnlineKnowledgeMode = "synthesis"
    max_papers: int = Field(default=100, ge=1, le=2000)
    extraction_delay_seconds: float = Field(default=0.5, ge=0.0, le=5.0)
    use_server_default: bool = False

    @field_validator("api_key", mode="before")
    @classmethod
    def normalize_api_key(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def require_model_access_source(self) -> "OnlineKnowledgeSearchRequest":
        if not self.use_server_default and not self.api_key:
            raise ValueError("API Key is required unless server default configuration is used")
        return self


class OnlineKnowledgeDefaultConfigResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    base_url: str
    model: str
    max_papers: int = Field(ge=1, le=2000)
    has_server_api_key: bool


class OnlineKnowledgeCountItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str
    count: int = Field(ge=0)
    percentage: float = Field(ge=0.0, le=100.0)


class OnlineKnowledgeSynthesis(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    method: str
    reaction_type: str
    product_name: str
    product_abbreviation: str
    temperature: str
    catalyst: str
    solvent: str
    time: str
    atmosphere: str
    pressure: str
    initiator: str
    reactants: str
    properties: str


class OnlineKnowledgePropertyPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    polymer_type: str
    polymer_name: str
    condition_name: str
    condition_value: str
    property_name: str
    property_value: str
    relationship: str
    paper_title: str


class OnlineKnowledgeSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    material: str
    mode: OnlineKnowledgeMode
    query_time_ms: float = Field(ge=0.0)
    totalPapers: int = Field(ge=0)
    max_papers: int = Field(ge=1)
    exampleUsed: bool
    stats: dict[str, Any]
    syntheses: list[OnlineKnowledgeSynthesis] = Field(default_factory=list)
    propertyPoints: list[OnlineKnowledgePropertyPoint] = Field(default_factory=list)
    temperatureDistribution: list[OnlineKnowledgeCountItem] = Field(default_factory=list)
    solventDistribution: list[OnlineKnowledgeCountItem] = Field(default_factory=list)
    catalystTable: list[OnlineKnowledgeCountItem] = Field(default_factory=list)
    tempLabels: list[OnlineKnowledgeCountItem] = Field(default_factory=list)
    conditionSummary: list[str] = Field(default_factory=list)
    reactionTypeTable: list[OnlineKnowledgeCountItem] = Field(default_factory=list)
    propertyNameDistribution: list[OnlineKnowledgeCountItem] = Field(default_factory=list)
    conditionDistribution: list[OnlineKnowledgeCountItem] = Field(default_factory=list)
    polymerTypeDistribution: list[OnlineKnowledgeCountItem] = Field(default_factory=list)
    relationshipDistribution: list[OnlineKnowledgeCountItem] = Field(default_factory=list)
    dataframe: list[dict[str, Any]] = Field(default_factory=list)


class OnlineKnowledgeHistoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    history_id: int = Field(ge=1)
    material: str
    mode: OnlineKnowledgeMode
    timestamp: str
    papers_found: int = Field(ge=0)
    reactions_extracted: int = Field(ge=0)
    max_papers: int = Field(ge=0)
    result_data: OnlineKnowledgeSearchResponse


class OnlineKnowledgeHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    history: list[OnlineKnowledgeHistoryItem] = Field(default_factory=list)


OnlineKnowledgeJobStatus = Literal["pending", "running", "completed", "failed"]


class OnlineKnowledgeJobCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: OnlineKnowledgeJobStatus


class OnlineKnowledgeJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: OnlineKnowledgeJobStatus
    material: str
    mode: OnlineKnowledgeMode
    max_papers: int = Field(ge=1)
    progress_stage: str = ""
    progress_message: str = ""
    processed_papers: int = Field(default=0, ge=0)
    total_papers: int = Field(default=0, ge=0)
    created_at: str
    updated_at: str
    error_message: str | None = None
    result: OnlineKnowledgeSearchResponse | None = None


class OnlineKnowledgeExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    data: list[dict[str, Any]] = Field(min_length=1)
    filename: str | None = None


class OnlineKnowledgeExportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    success: bool
    csv_content: str
    filename: str


class MutationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool


class ReverseDesignTgRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target_tg: float
    smiles: str = Field(min_length=1)
    similarity_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    candidate_size: int = Field(default=200, ge=1, le=200)

    @model_validator(mode="before")
    @classmethod
    def ignore_legacy_client_limits(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data

        normalized = dict(data)
        for key in ("candidate_sample_size", "top_k", "random_seed"):
            normalized.pop(key, None)
        return normalized


class ReverseDesignTgCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rank: int = Field(ge=1)
    pi_id: int
    polymer_smiles: str
    canonical_polym: str | None = None
    monomer_a_smiles: str
    monomer_b_smiles: str
    monomer_a_iupac: str | None = None
    monomer_b_iupac: str | None = None
    monomer_a_structure_svg: str | None = None
    monomer_b_structure_svg: str | None = None
    tg_value: float
    tg_unit: Literal["°C"] = "°C"
    tg_difference: float = Field(ge=0.0)
    similarity_score: float = Field(ge=0.0, le=1.0)
    structure_svg: str | None = None
    knowledge_available: bool = False


class ReverseDesignTgResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_tg: float
    query_time_ms: float = Field(ge=0.0)
    candidate_pool_size: int = Field(ge=0)
    sampled_candidate_count: int = Field(ge=0)
    total: int = Field(ge=0)
    data_source: Literal["pi_reverse_design"] = "pi_reverse_design"
    results: list[ReverseDesignTgCandidate] = Field(default_factory=list)


ReverseDesignJobStatus = Literal["pending", "running", "found_enough", "exhausted", "failed", "cancelled"]


class ReverseDesignTgJobCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: ReverseDesignJobStatus


class ReverseDesignTgJobStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: ReverseDesignJobStatus
    target_tg: float
    similarity_threshold: float
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    scanned_rows: int = Field(ge=0)
    matched_count: int = Field(ge=0)
    current_tg_radius: float | None = Field(default=None, ge=0.0)
    best_similarity_score: float | None = Field(default=None, ge=0.0, le=1.0)
    message: str | None = None
    error: str | None = None
    result: ReverseDesignTgResponse | None = None


class ConditionalGenerationTgRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    smiles: str = Field(min_length=1)
    delta_tg: float = Field(allow_inf_nan=False)
    candidate_count: int = Field(default=10, ge=1, le=50)
    top_k: int = Field(default=5, ge=1, le=20)
    temperature: float = Field(default=1.0, ge=0.1, le=2.0)


class ConditionalGenerationCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rank: int = Field(ge=1)
    generated_smiles: str
    structure_svg: str | None = None
    predicted_tg: float | None = None
    tg_unit: Literal["°C"] = "°C"
    tg_error: float | None = Field(default=None, ge=0.0)
    similarity_score: float | None = Field(default=None, ge=0.0, le=1.0)
    sa_score: float | None = Field(default=None, ge=0.0)


class ConditionalGenerationTgResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_smiles: str
    normalized_input_smiles: str
    delta_tg: float
    query_time_ms: float = Field(ge=0.0)
    requested_count: int = Field(ge=1)
    returned_count: int = Field(ge=0)
    attempts: int = Field(ge=0)
    filter_counter: dict[str, int] = Field(default_factory=dict)
    results: list[ConditionalGenerationCandidate] = Field(default_factory=list)


class ConditionalGenerationTgStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool
    available: bool
    model_dir: str
    missing_artifacts: list[str] = Field(default_factory=list)
    message: str


ConditionalGenerationJobStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


class ConditionalGenerationJobCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: ConditionalGenerationJobStatus


class ConditionalGenerationJobStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: ConditionalGenerationJobStatus
    delta_tg: float
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    attempts: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    message: str | None = None
    error: str | None = None
    result: ConditionalGenerationTgResponse | None = None


class DftPcaPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mol_id: str
    x: float
    y: float
    z: float
    n_atoms: int
    final_step: int
    homo_ev: float | None = None
    lumo_ev: float | None = None
    gap_ev: float | None = None
    dipole_moment: float | None = None


class DftPcaSampleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_time_ms: float = Field(ge=0.0)
    total: int = Field(ge=0)
    results: list[DftPcaPoint] = Field(default_factory=list)


class DftEnergyPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: int
    scf_energy: float | None = None
    homo_ev: float | None = None
    lumo_ev: float | None = None
    gap_ev: float | None = None


class DftMoleculeDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mol_id: str
    range_group: str
    final_step: int
    n_atoms: int
    coordinates: list[tuple[int, float, float, float]]
    scf_energy: float | None = None
    zero_point_energy: float | None = None
    thermal_enthalpy: float | None = None
    gibbs_free_energy: float | None = None
    lowest_freq: float | None = None
    dipole_moment: float | None = None
    homo_ev: float | None = None
    lumo_ev: float | None = None
    gap_ev: float | None = None
    is_converged: str | None = None
    trace: list[DftEnergyPoint] = Field(default_factory=list)


class LabDataTestProjectRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True, populate_by_name=True, str_strip_whitespace=True)

    id: int
    project_name: str = Field(alias="projectName")
    result_unit: str = Field(alias="resultUnit")


class LabDataSampleMeasurementBase(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    sample_id: str = Field(min_length=1, max_length=50, alias="sampleId")
    experiment_project: str = Field(min_length=1, max_length=100, alias="experimentProject")
    instrument_id: str = Field(min_length=1, max_length=50, alias="instrumentId")
    operator: str = Field(min_length=1, max_length=100)
    collection_time: datetime = Field(alias="collectionTime")
    temperature: float | None = None
    concentration: float | None = None
    result_value: float = Field(alias="resultValue")
    result_unit: str = Field(min_length=1, max_length=20, alias="resultUnit")
    remarks: str | None = None


class LabDataSampleMeasurementCreate(LabDataSampleMeasurementBase):
    pass


class LabDataSampleMeasurementRead(LabDataSampleMeasurementBase):
    model_config = ConfigDict(extra="forbid", from_attributes=True, populate_by_name=True, str_strip_whitespace=True)

    id: int


class LabDataSampleMeasurementPageRead(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    items: list[LabDataSampleMeasurementRead]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(alias="pageSize", ge=1)


class LabDataCountRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = Field(ge=0)


class LabDataProjectStatsRead(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    experiment_project: str = Field(alias="experimentProject")
    count: int = Field(ge=0)


class LabDataSummaryRead(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    total_count: int = Field(alias="totalCount", ge=0)
    by_project: list[LabDataProjectStatsRead] = Field(alias="byProject")
