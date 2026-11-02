"""Explicit, bounded public contracts for the development property-filter MCP."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import PropertyFilterCondition, PropertyFilterHistogram, PropertyFilterRecord

TOOL_NAMES = frozenset({"list_filter_properties", "search_polymers", "get_property_histogram"})
MAX_RESPONSE_BYTES = 256 * 1024


class MCPFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class FilterInput(PublicModel):
    option_key: str = Field(min_length=1, max_length=512)
    min_value: float | None = None
    max_value: float | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> FilterInput:
        if self.min_value is None and self.max_value is None:
            raise ValueError("At least one bound is required")
        if self.min_value is not None and self.max_value is not None and self.min_value > self.max_value:
            raise ValueError("min_value must not exceed max_value")
        return self


class ListInput(PublicModel):
    q: str = Field(default="", max_length=200)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=50)
    dataset_revision: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def require_page_revision(self) -> ListInput:
        if self.page > 1 and self.dataset_revision is None:
            raise ValueError("dataset_revision is required after the first page")
        return self


class SearchInput(PublicModel):
    dataset_revision: str = Field(min_length=1, max_length=80)
    filters: list[FilterInput] = Field(min_length=1, max_length=8)
    q: str = Field(default="", max_length=200)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=10, ge=1, le=25)
    measurement_limit_per_filter: int = Field(default=1, ge=1, le=3)


class HistogramInput(PublicModel):
    dataset_revision: str = Field(min_length=1, max_length=80)
    option_key: str = Field(min_length=1, max_length=512)


class ErrorDetail(PublicModel):
    code: str
    message: str


class ErrorOutput(PublicModel):
    ok: bool = False
    error: ErrorDetail


class PropertyOutput(PublicModel):
    option_key: str
    filter_type: str
    label: str
    property_key: str | None
    property_name: str | None
    unit: str
    measurement_count: int
    unique_smiles_count: int
    min_value: float | None
    p5_value: float | None
    median_value: float | None
    p95_value: float | None
    max_value: float | None


class ListOutput(PublicModel):
    ok: bool = True
    dataset_revision: str
    source_status: Literal["ready", "empty"] = "ready"
    total_measurement_count: int
    total_option_count: int
    matched_option_count: int
    page: int
    page_size: int
    has_more: bool
    options: list[PropertyOutput]


class MeasurementOutput(PropertyFilterRecord):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    record_ref: str


class EvidenceOutput(PublicModel):
    filter_index: int
    option_key: str
    unit: str
    matching_measurement_count: int
    truncated: bool
    measurements: list[MeasurementOutput]


class MaterialOutput(PublicModel):
    material_ref: str
    smiles: str | None
    canonical_smiles: str | None
    polymer_name: str | None
    matched_filters: int
    matching_evidence_count: int
    evidence: list[EvidenceOutput]


class SearchOutput(PublicModel):
    ok: bool = True
    dataset_revision: str
    source_status: Literal["ready", "empty"] = "ready"
    total_measurement_count: int
    matched_material_count: int
    page: int
    page_size: int
    has_more: bool
    measurement_limit_per_filter: int
    semantics: str = (
        "AND matches measurements grouped by canonical SMILES (falling back to SMILES, then record). "
        "Different conditions may be satisfied by different samples or experiments. "
        "Keywords match name/SMILES substrings. No unit conversion is performed during search. "
        "Evidence can include observed, median and imputed values; matching_evidence_count counts "
        "per-condition matches, so one measurement can count for multiple conditions."
    )
    results: list[MaterialOutput]


class HistogramOutput(PublicModel):
    ok: bool = True
    dataset_revision: str
    source_status: Literal["ready", "empty"] = "ready"
    option_key: str
    unit: str
    distribution_scope: str = "entire_dataset_for_option"
    histogram: PropertyFilterHistogram


INPUT_MODELS = {
    "list_filter_properties": ListInput,
    "search_polymers": SearchInput,
    "get_property_histogram": HistogramInput,
}
OUTPUT_MODELS = {
    "list_filter_properties": ListOutput,
    "search_polymers": SearchOutput,
    "get_property_histogram": HistogramOutput,
}


def dataset_revision(catalog: Any) -> str:
    fields = [catalog.schema_version, catalog.generation, catalog.import_batch_id,
              catalog.source_sha256, catalog.generated_at.isoformat()]
    return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()


def check_revision(requested: str | None, current: str) -> None:
    if requested is not None and requested != current:
        raise MCPFailure("DATASET_REVISION_CHANGED", "Dataset changed; fetch the first property page and restart the query.")


def option_unit(option: Any) -> str:
    return (option.canonical_unit if option.filter_type == "standardized" else option.property_unit_clean) or ""


def bind_condition(option: Any, condition: FilterInput) -> PropertyFilterCondition:
    return PropertyFilterCondition(
        filter_type=option.filter_type,
        property_key=option.property_key,
        property_name=option.property_name,
        canonical_unit=option_unit(option) if option.filter_type == "standardized" else None,
        property_unit_clean=option_unit(option) if option.filter_type == "raw" else None,
        min_value=condition.min_value,
        max_value=condition.max_value,
    )


def public_option(option: Any) -> PropertyOutput:
    return PropertyOutput(
        option_key=option.option_key, filter_type=option.filter_type,
        label=option.label, property_key=option.property_key, property_name=option.property_name,
        unit=option_unit(option), measurement_count=option.rows,
        unique_smiles_count=option.unique_smiles,
        **{key: getattr(option, key) for key in ("min_value", "p5_value", "median_value", "p95_value", "max_value")},
    )
