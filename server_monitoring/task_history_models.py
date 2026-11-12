"""Public, identity-free contracts for retained physical-GPU task history."""
from collections import Counter
from datetime import datetime
import math
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import ByteCount, Envelope, StrictModel, Timestamp


GpuUuid = Annotated[str, Field(min_length=5, max_length=128, pattern=r"^GPU-[A-Za-z0-9-]+$")]
SourceId = Literal["development_dft", "production_dft", "development_md", "production_md",
                   "development_polymer_md", "production_polymer_md"]
SOURCE_IDS = ("development_dft", "production_dft", "development_md", "production_md",
              "development_polymer_md", "production_polymer_md")
Availability = Literal["ready", "partial", "unavailable"]
ErrorCode = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]


def timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class TaskSource(StrictModel):
    source_id: SourceId
    status: Availability
    matched_tasks: Annotated[int, Field(ge=0, le=4096)]
    excluded_tasks: Annotated[int, Field(ge=0, le=16384)]
    error_code: ErrorCode | None

    @model_validator(mode="after")
    def consistent(self):
        if (self.status == "ready") != (self.error_code is None):
            raise ValueError("source availability must explain incomplete reads")
        if self.status == "unavailable" and self.matched_tasks:
            raise ValueError("unavailable source cannot supply tasks")
        return self


class GpuTask(StrictModel):
    id: Annotated[str, Field(pattern=r"^[0-9a-f]{20}$")]
    gpu_uuid: GpuUuid
    module: Literal["monomer_dft", "monomer_md", "polymer_md"]
    environment: Literal["development", "production"]
    calculation_type: Literal["single_point", "optimization", "md_formal", "md_demo", "polymer_md"]
    status: Literal["completed"]
    started_at: Timestamp
    finished_at: Timestamp
    duration_seconds: Annotated[float, Field(ge=0, le=1e18, allow_inf_nan=False)]

    @model_validator(mode="after")
    def consistent(self):
        compatible = {"monomer_dft": {"single_point", "optimization"}, "monomer_md": {"md_formal", "md_demo"},
                      "polymer_md": {"polymer_md"}}
        if self.calculation_type not in compatible[self.module]:
            raise ValueError("calculation type must match its module")
        duration = (timestamp(self.finished_at) - timestamp(self.started_at)).total_seconds()
        if duration < 0 or not math.isclose(duration, self.duration_seconds, abs_tol=0.001, rel_tol=0):
            raise ValueError("task duration must match its execution timestamps")
        return self


class ActiveGpuItem(StrictModel):
    id: Annotated[str, Field(pattern=r"^[0-9a-f]{20}$")]
    gpu_uuid: GpuUuid
    kind: Literal["task", "service", "process"]
    module: Literal["monomer_dft", "monomer_md", "polymer_md", "platform", "nvidia_mps", "other"]
    environment: Literal["development", "production", "unknown"]
    label: Literal["DFT任务", "MD任务", "Polymer MD任务", "DFT服务", "平台服务", "平台后端", "NVIDIA MPS服务", "GPU进程"]
    status: Literal["running", "resident", "observed"]
    memory_used_bytes: ByteCount | None
    started_at: Timestamp | None

    @model_validator(mode="after")
    def consistent(self):
        expected = {"task": "running", "service": "resident", "process": "observed"}
        labels = {("monomer_dft", "task"): {"DFT任务"}, ("monomer_dft", "service"): {"DFT服务"},
                  ("monomer_md", "task"): {"MD任务"}, ("polymer_md", "task"): {"Polymer MD任务"},
                  ("platform", "service"): {"平台服务", "平台后端"}, ("nvidia_mps", "service"): {"NVIDIA MPS服务"},
                  ("other", "process"): {"GPU进程"}}
        if self.status != expected[self.kind] or self.label not in labels.get((self.module, self.kind), set()):
            raise ValueError("activity must distinguish task execution, residency and observation")
        if self.kind == "task" and (self.module not in {"monomer_dft", "monomer_md", "polymer_md"} or self.environment == "unknown"):
            raise ValueError("a running task requires verified attribution")
        if self.kind == "process" and (self.module != "other" or self.environment != "unknown"):
            raise ValueError("an unclassified process cannot claim a business identity")
        return self


class GpuActivity(StrictModel):
    captured_at: Timestamp
    status: Availability
    error_code: ErrorCode | None
    items: Annotated[list[ActiveGpuItem], Field(max_length=512)]

    @model_validator(mode="after")
    def consistent(self):
        if (self.status == "ready") != (self.error_code is None):
            raise ValueError("activity availability must explain incomplete reads")
        if self.status == "unavailable" and self.items:
            raise ValueError("unavailable activity cannot supply items")
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("duplicate active identity")
        if any(item.started_at is not None and timestamp(item.started_at) > timestamp(self.captured_at) for item in self.items):
            raise ValueError("active work cannot start in the future")
        return self


def task_source_id(task) -> str:
    return task.environment + {"monomer_dft": "_dft", "monomer_md": "_md", "polymer_md": "_polymer_md"}[task.module]


class TaskHistory(Envelope):
    coverage: Literal["retained_verified_gpu_records"]
    history_captured_at: Timestamp
    status: Availability
    gpu_uuids: Annotated[list[GpuUuid], Field(max_length=128)]
    sources: Annotated[list[TaskSource], Field(min_length=6, max_length=6)]
    tasks: Annotated[list[GpuTask], Field(max_length=1280)]
    activity: GpuActivity

    @model_validator(mode="after")
    def bounded_history(self):
        if len(set(self.gpu_uuids)) != len(self.gpu_uuids):
            raise ValueError("duplicate physical GPU")
        if {source.source_id for source in self.sources} != set(SOURCE_IDS):
            raise ValueError("expected all fixed history sources")
        if timestamp(self.history_captured_at) > timestamp(self.captured_at) or timestamp(self.activity.captured_at) > timestamp(self.captured_at):
            raise ValueError("component capture cannot be in the future")
        if any(item.gpu_uuid not in self.gpu_uuids for item in self.activity.items):
            raise ValueError("active item must match the current physical inventory")
        expected = "ready" if all(source.status == "ready" for source in self.sources) else "unavailable" if all(source.status == "unavailable" for source in self.sources) else "partial"
        if self.status != expected:
            raise ValueError("history availability must match source availability")
        if self.status == "unavailable" and self.tasks:
            raise ValueError("unavailable history cannot contain tasks")
        if len({task.id for task in self.tasks}) != len(self.tasks):
            raise ValueError("duplicate task identity")
        if any(count > 10 for count in Counter(task.gpu_uuid for task in self.tasks).values()):
            raise ValueError("retain at most ten tasks per physical GPU across all modules")
        finishes = [timestamp(task.finished_at) for task in self.tasks]
        if any(a < b for a, b in zip(finishes, finishes[1:])):
            raise ValueError("tasks must be newest first")
        if any(task.gpu_uuid not in self.gpu_uuids for task in self.tasks) or any(finish > timestamp(self.history_captured_at) for finish in finishes):
            raise ValueError("task must match current inventory and capture time")
        sources = {source.source_id: source for source in self.sources}
        counts = Counter(task_source_id(task) for task in self.tasks)
        if any(counts[source_id] > sources[source_id].matched_tasks for source_id in SOURCE_IDS):
            raise ValueError("returned task count exceeds source coverage")
        return self


def validate_task_history(payload: object) -> TaskHistory:
    return TaskHistory.model_validate(payload)
