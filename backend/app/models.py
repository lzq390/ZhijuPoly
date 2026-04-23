from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MatchMode = Literal["structure", "property"]


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
