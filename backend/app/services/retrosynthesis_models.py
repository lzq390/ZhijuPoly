"""Strict public contracts for durable, single-target retrosynthesis."""
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.models import MonomerRetrosynthesisRequest


class TargetSource(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    type: Literal["smiles", "name", "saved_structure"] = "smiles"
    label: str | None = Field(None, max_length=1000)
    record_id: UUID | None = None


class ResolveTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source: Literal["smiles", "name", "saved_structure"]
    smiles: str | None = Field(None, min_length=1, max_length=8000)
    name: str | None = Field(None, min_length=1, max_length=1000)
    record_id: UUID | None = None
    query: str = Field("", max_length=1000)
    cursor: str | None = Field(None, max_length=512)
    limit: int = Field(20, ge=1, le=20)

    @model_validator(mode="after")
    def source_input(self):
        if self.source == "smiles" and not self.smiles:
            raise ValueError("smiles is required")
        if self.source == "name" and not self.name:
            raise ValueError("name is required")
        supplied = {"smiles": self.smiles, "name": self.name, "record_id": self.record_id}
        allowed = {"smiles": {"smiles"}, "name": {"name"}, "saved_structure": {"record_id"}}[self.source]
        if any(value is not None and key not in allowed for key, value in supplied.items()):
            raise ValueError("target input does not match its source")
        if self.source != "saved_structure" and (self.query or self.cursor):
            raise ValueError("query and cursor require saved_structure")
        return self


class RetrosynthesisJobRequest(MonomerRetrosynthesisRequest):
    smiles: str = Field(min_length=1, max_length=8000)
    source: TargetSource = Field(default_factory=TargetSource)
