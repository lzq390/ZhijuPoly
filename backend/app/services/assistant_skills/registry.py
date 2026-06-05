from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class SkillExecutionError(RuntimeError):
    """Raised when a registered assistant skill cannot complete."""


class SkillClarificationRequired(RuntimeError):
    """Raised when a skill needs more user input before execution."""


@dataclass(frozen=True)
class AssistantSkillContext:
    model_enabled: bool
    model_dir: Path


SkillExecutor = Callable[[BaseModel, AssistantSkillContext], dict[str, Any]]


@dataclass(frozen=True)
class AssistantSkill:
    name: str
    display_name: str
    description: str
    input_model: type[BaseModel]
    executor: SkillExecutor

    def validate_arguments(self, arguments: dict[str, Any]) -> BaseModel:
        return self.input_model.model_validate(arguments)

    def execute(self, arguments: BaseModel, context: AssistantSkillContext) -> dict[str, Any]:
        return self.executor(arguments, context)

    def prompt_catalog_line(self) -> str:
        schema = self.input_model.model_json_schema()
        return (
            f"- name: {self.name}; display: {self.display_name}; "
            f"description: {self.description}; input_schema: {schema}"
        )


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, AssistantSkill] = {}

    def register(self, skill: AssistantSkill) -> None:
        if skill.name in self._skills:
            raise ValueError(f"duplicate assistant skill: {skill.name}")
        self._skills[skill.name] = skill

    def get(self, name: str) -> AssistantSkill | None:
        return self._skills.get(name)

    def catalog_for_prompt(self) -> str:
        if not self._skills:
            return "- No executable skills are registered."
        return "\n".join(skill.prompt_catalog_line() for skill in self._skills.values())


def build_default_skill_registry() -> SkillRegistry:
    from app.services.assistant_skills.predict_properties import build_predict_properties_skill

    registry = SkillRegistry()
    registry.register(build_predict_properties_skill())
    return registry
