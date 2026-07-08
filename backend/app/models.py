from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MatchMode = Literal["structure", "property"]
SmilesLookupTable = Literal["polymers", "properties", "pi_candidates"]
RetrosynthesisTargetRole = Literal["auto", "diamine", "dianhydride", "other"]
PolymerizationTargetClass = Literal[
    "polyolefin",
    "polyester",
    "polyether",
    "polyamide",
    "polyimide",
    "polyurethane",
    "polyoxazolidone",
    "all",
]


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


class MonomerPolymerizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    monomer_a_smiles: str = Field(min_length=1, max_length=1000)
    monomer_b_smiles: str | None = Field(default=None, max_length=1000)
    target_class: PolymerizationTargetClass = "polyimide"
    max_results: int = Field(default=10, ge=1, le=20)

    @field_validator("monomer_b_smiles", mode="before")
    @classmethod
    def normalize_optional_monomer(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class MonomerPolymerizationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    role: Literal["monomer_a", "monomer_b"]
    input_smiles: str
    canonical_smiles: str


class MonomerPolymerizationCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rank: int = Field(ge=1)
    monomer_a_smiles: str
    monomer_b_smiles: str | None = None
    polymer_smiles: str
    polymer_class: str
    reaction_id: int | None = None
    reaction_name: str | None = None
    reactset: list[str] = Field(default_factory=list)
    structure_svg: str | None = None


class MonomerPolymerizationTargetRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_monomers: int = Field(ge=1, le=2)
    max_monomers: int = Field(default=2, ge=1, le=2)
    monomer_b_required: bool
    note: str


class MonomerPolymerizationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    input_monomers: list[MonomerPolymerizationInput] = Field(default_factory=list)
    target_class: PolymerizationTargetClass
    query_time_ms: float = Field(ge=0.0)
    total: int = Field(ge=0)
    results: list[MonomerPolymerizationCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class MonomerPolymerizationStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool
    available: bool
    default_target_class: PolymerizationTargetClass = "polyimide"
    available_target_classes: list[PolymerizationTargetClass] = Field(default_factory=list)
    target_requirements: dict[PolymerizationTargetClass, MonomerPolymerizationTargetRequirement] = Field(default_factory=dict)
    max_results_limit: int = Field(default=20, ge=1)
    message: str


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


PropertyFilterType = Literal["standardized", "raw"]


class PropertyFilterOption(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    filter_type: PropertyFilterType
    option_key: str
    label: str
    property_key: str | None = None
    property_name: str | None = None
    property_unit_clean: str | None = None
    canonical_unit: str | None = None
    rows: int = Field(ge=0)
    unique_smiles: int = Field(ge=0)
    min_value: float | None = None
    p5_value: float | None = None
    median_value: float | None = None
    p95_value: float | None = None
    max_value: float | None = None


class PropertyFilterOptionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query_time_ms: float = Field(ge=0.0)
    total_records: int = Field(ge=0)
    mapped_records: int = Field(ge=0)
    raw_records: int = Field(ge=0)
    data_source: str = "postgres"
    source_status: str = "ready"
    source_message: str | None = None
    options: list[PropertyFilterOption] = Field(default_factory=list)


class PropertyFilterCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    filter_type: PropertyFilterType
    property_key: str | None = Field(default=None, max_length=120)
    canonical_unit: str | None = Field(default=None, max_length=120)
    property_name: str | None = Field(default=None, max_length=240)
    property_unit_clean: str | None = Field(default=None, max_length=120)
    min_value: float | None = None
    max_value: float | None = None

    @model_validator(mode="after")
    def validate_filter(self) -> "PropertyFilterCondition":
        if self.min_value is None and self.max_value is None:
            raise ValueError("At least one threshold bound is required")
        if self.min_value is not None and self.max_value is not None and self.min_value > self.max_value:
            raise ValueError("min_value cannot be greater than max_value")
        if self.filter_type == "standardized" and not self.property_key:
            raise ValueError("property_key is required for standardized filters")
        if self.filter_type == "raw" and not self.property_name:
            raise ValueError("property_name is required for raw filters")
        return self


class PropertyFilterSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    filters: list[PropertyFilterCondition] = Field(min_length=1, max_length=8)
    q: str = Field(default="", max_length=200)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=100)


class PropertyFilterRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    filter_record_id: int
    source_row_number: int
    polymer_name: str | None = None
    smiles: str | None = None
    canonical_smiles: str | None = None
    property_category: str
    property_name: str
    property_value: str
    property_value_num: float | None = None
    property_unit_raw: str | None = None
    property_unit_clean: str | None = None
    property_key: str | None = None
    property_label: str | None = None
    canonical_value: float | None = None
    canonical_unit: str | None = None
    unit_conversion_status: str | None = None
    value_origin: str | None = None
    label_source: str | None = None
    reliable_score: float | None = None
    soft_quality_flags: str | None = None
    duplicate_flag: str | None = None
    filter_index: int


class PropertyFilterSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    smiles: str | None = None
    canonical_smiles: str | None = None
    polymer_name: str | None = None
    matched_filters: int = Field(ge=0)
    records: list[PropertyFilterRecord] = Field(default_factory=list)


class PropertyFilterSearchResponse(BaseModel):
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
    results: list[PropertyFilterSearchResult] = Field(default_factory=list)


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


class PolytaoDescriptorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    smiles: str = Field(min_length=1, max_length=2048)


class PolytaoDescriptorValue(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    name: str
    value: float = Field(allow_inf_nan=False)


class PolytaoDescriptorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    input_smiles: str
    canonical_smiles: str
    descriptors: list[PolytaoDescriptorValue]
    prompt: str
    query_time_ms: float = Field(ge=0.0)


class PolytaoGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    descriptors: dict[str, float]
    input_smiles: str | None = Field(default=None, max_length=2048)
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


class PolytaoCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rank: int = Field(ge=1)
    generated_smiles: str
    raw_smiles: str
    structure_svg: str | None = None
    valid_smiles: bool = True
    sa_score: float | None = Field(default=None, ge=0.0)
    warnings: list[str] = Field(default_factory=list)


class PolytaoGenerationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    prompt: str
    query_time_ms: float = Field(ge=0.0)
    requested_count: int = Field(ge=1)
    returned_count: int = Field(ge=0)
    attempts: int = Field(ge=0)
    filter_counter: dict[str, int] = Field(default_factory=dict)
    results: list[PolytaoCandidate] = Field(default_factory=list)


class PolytaoStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool
    available: bool
    worker_base_url_configured: bool
    worker_status: str | None = None
    worker_mode: str | None = None
    db_configured: bool | None = None
    db_ready: bool | None = None
    db_error: str | None = None
    runtime_ready: bool | None = None
    runtime_error: str | None = None
    active_jobs: int | None = Field(default=None, ge=0)
    model_id: str | None = None
    model_revision: str | None = None
    default_params: dict[str, float | int | str | bool | None] = Field(default_factory=dict)
    worker_version: str | None = None
    message: str


PolytaoJobStatus = Literal["pending", "submitted", "running", "completed", "failed", "cancelled"]


class PolytaoJobCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: PolytaoJobStatus


class PolytaoJobStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: PolytaoJobStatus
    input_smiles: str | None = None
    canonical_smiles: str | None = None
    prompt: str
    requested_count: int = Field(ge=1)
    returned_count: int = Field(ge=0)
    attempts: int = Field(ge=0)
    progress_percent: int = Field(ge=0, le=100)
    progress_stage: str
    progress_message: str
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    worker_id: str | None = None
    worker_job_id: str | None = None
    worker_version: str | None = None
    engine: str
    error_message: str | None = None
    result: PolytaoGenerationResponse | None = None


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

MonomerMdJobStatus = Literal["pending", "submitted", "running", "completed", "failed", "cancelled"]
MonomerMdProtocol = Literal["DensityDemo", "Density", "Transport", "HVap", "Dielectric", "Compressibility"]
MonomerMdRunMode = Literal["demo", "formal"]


class MonomerMdRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    smiles: str | None = Field(default=None, min_length=1, max_length=1000)
    protocol: MonomerMdProtocol = "DensityDemo"
    run_mode: MonomerMdRunMode = "demo"
    config_json: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_request_shape(self) -> "MonomerMdRunRequest":
        if self.run_mode == "demo" and self.protocol == "DensityDemo":
            if not self.smiles:
                raise ValueError("smiles is required for DensityDemo")
            return self
        if self.run_mode != "formal":
            raise ValueError("formal ByteFF2 protocols must use run_mode='formal'")
        if self.protocol == "DensityDemo":
            raise ValueError("DensityDemo must use run_mode='demo'")
        if self.config_json is None:
            raise ValueError("config_json is required for formal ByteFF2 protocols")
        return self


class MonomerMdStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    available: bool
    default_steps: int = Field(ge=1)
    worker_base_url_configured: bool
    worker_status: str | None = None
    worker_mode: str | None = None
    db_configured: bool | None = None
    byteff2_root_exists: bool | None = None
    runtime_ready: bool | None = None
    runtime_error: str | None = None
    active_jobs: int | None = Field(default=None, ge=0)
    protocols: dict[str, Any] = Field(default_factory=dict)
    message: str


class MonomerMdProtocolCatalogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    available: bool
    protocols: list[dict[str, Any]] = Field(default_factory=list)
    message: str


class MonomerMdJobCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: MonomerMdJobStatus


class MonomerMdJobStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: MonomerMdJobStatus
    input_smiles: str
    canonical_smiles: str
    protocol: MonomerMdProtocol = "DensityDemo"
    run_mode: MonomerMdRunMode = "demo"
    config_json: dict[str, Any] = Field(default_factory=dict)
    components: dict[str, Any] = Field(default_factory=dict)
    requested_steps: int = Field(ge=1)
    completed_steps: int = Field(ge=0)
    progress_percent: int = Field(ge=0, le=100)
    progress_stage: str
    progress_message: str
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    worker_id: str | None = None
    worker_job_id: str | None = None
    worker_version: str | None = None
    engine: str
    artifact_root: str | None = None
    artifacts: dict[str, Any] = Field(default_factory=dict)
    artifact_manifest: dict[str, Any] = Field(default_factory=dict)
    artifact_deleted_at: str | None = None
    artifact_delete_message: str | None = None
    result_summary: dict[str, Any] = Field(default_factory=dict)
    byteff2_git_sha: str | None = None
    gpu_device: str | None = None
    error_category: str | None = None
    error_message: str | None = None
    result: dict[str, Any] | None = None
