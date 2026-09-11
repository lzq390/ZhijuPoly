"""Request and response contracts shared by the formal and lightweight entrypoints."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.models import KnowledgeSearchRequest, KnowledgeSearchResponse, PropertyFilterSearchRequest, PropertyFilterSearchResponse


class RecordingStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recording_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class RecordedKnowledgeSearchRequest(KnowledgeSearchRequest):
    recording_id: str | None = Field(default=None, min_length=1, max_length=64)


class RecordedKnowledgeSearchResponse(KnowledgeSearchResponse):
    search_id: str


class ArticleObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search_id: str = Field(min_length=1, max_length=64)
    knowledge_id: int = Field(gt=0)
    source: Literal["result_card", "drawer_reopen", "reaction_tab"]
    recording_id: str | None = Field(default=None, min_length=1, max_length=64)


class RecordedFilterSearchResponse(PropertyFilterSearchResponse):
    search_id: str


class RecordedFilterSearchRequest(PropertyFilterSearchRequest):
    recording_id: str | None = Field(default=None, min_length=1, max_length=64)


class FilterObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recording_id: str | None = Field(default=None, min_length=1, max_length=64)
    search_id: str = Field(min_length=1, max_length=64)
    result_index: int = Field(ge=0)
    source: Literal["measurement_details", "smiles"]
    filter_index: int | None = Field(default=None, ge=0, le=7)
    smiles_field: Literal["smiles", "canonical_smiles"] | None = None

    @model_validator(mode="after")
    def validate_target(self):
        if self.source == "measurement_details":
            if self.filter_index is None or self.smiles_field is not None:
                raise ValueError("Measurement observations require only filter_index")
        elif self.smiles_field is None or self.filter_index is not None:
            raise ValueError("SMILES observations require only smiles_field")
        return self
