"""Bounded JSON contracts shared by the collector and the read-only API."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator


def _timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp requires a timezone")
    return value


def _uuid(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("UUID must use canonical spelling")
    return value


Timestamp = Annotated[str, Field(min_length=20, max_length=40), AfterValidator(_timestamp)]
Uuid = Annotated[str, AfterValidator(_uuid)]
Percent = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
Nonnegative = Annotated[float, Field(ge=0, le=1e18, allow_inf_nan=False)]
ByteCount = Annotated[int, Field(ge=0, le=2**63 - 1)]


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class Cpu(StrictModel):
    utilization_percent: Percent | None


class Memory(StrictModel):
    total_bytes: ByteCount
    available_bytes: ByteCount
    used_bytes: ByteCount
    used_percent: Percent

    @model_validator(mode="after")
    def consistent(self):
        if self.total_bytes <= 0 or self.used_bytes + self.available_bytes != self.total_bytes:
            raise ValueError("inconsistent memory totals")
        return self


class Disk(StrictModel):
    mount_point: Annotated[str, Field(min_length=1, max_length=256)]
    label: Annotated[str, Field(min_length=1, max_length=64)]
    total_bytes: ByteCount | None
    used_bytes: ByteCount | None
    available_bytes: ByteCount | None
    used_percent: Percent | None
    status: Literal["ok", "unavailable"]


class Gpu(StrictModel):
    index: Annotated[int, Field(ge=0, le=1023)]
    uuid: Annotated[str, Field(min_length=5, max_length=128, pattern=r"^GPU-[A-Za-z0-9-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=256)]
    utilization_percent: Percent | None
    memory_total_bytes: ByteCount | None
    memory_used_bytes: ByteCount | None
    temperature_celsius: Annotated[float, Field(ge=-100, le=250, allow_inf_nan=False)] | None
    power_watts: Annotated[float, Field(ge=0, le=10000, allow_inf_nan=False)] | None


class Component(StrictModel):
    status: Literal["ok", "warming", "unavailable"]
    last_success_at: Timestamp | None
    error_code: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")] | None


class Components(StrictModel):
    cpu: Component
    memory: Component
    disks: Component
    gpus: Component


HostLabel = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x1f\x7f-\x9f]+$")]


class HostInfo(StrictModel):
    cpu_model: HostLabel | None
    cpu_socket_count: Annotated[int, Field(ge=1, le=1024)] | None
    cpu_physical_core_count: Annotated[int, Field(ge=1, le=65536)] | None
    os_name: HostLabel | None
    kernel_release: Annotated[str, Field(max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._+~-]*$")] | None
    nvidia_driver_version: Annotated[str, Field(max_length=31, pattern=r"^[0-9]{1,4}(\.[0-9]{1,4}){1,3}$")] | None

    @field_validator("cpu_model", "os_name")
    @classmethod
    def trimmed_label(cls, value):
        if value is not None and value != value.strip():
            raise ValueError("host labels require trimmed text")
        return value

    @model_validator(mode="after")
    def consistent_topology(self):
        if self.cpu_socket_count is not None and self.cpu_physical_core_count is not None and self.cpu_physical_core_count < self.cpu_socket_count:
            raise ValueError("CPU core count cannot be smaller than socket count")
        return self


class Envelope(StrictModel):
    schema_version: Literal[1]
    node_id: Literal["local-server"]
    collector_id: Uuid
    started_at: Timestamp
    captured_at: Timestamp

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema version must be an integer")
        return value

    @model_validator(mode="after")
    def ordered(self):
        if datetime.fromisoformat(self.started_at.replace("Z", "+00:00")) > datetime.fromisoformat(self.captured_at.replace("Z", "+00:00")):
            raise ValueError("capture predates collector start")
        return self


class Snapshot(Envelope):
    hostname: Annotated[str, Field(min_length=1, max_length=253)]
    host_info: HostInfo | None = None
    sequence: Annotated[int, Field(ge=1, le=2**63 - 1)]
    logical_cpu_count: Annotated[int, Field(ge=1, le=65536)]
    uptime_seconds: Nonnegative | None
    load_average: Annotated[list[Nonnegative], Field(min_length=3, max_length=3)] | None
    cpu: Cpu | None
    memory: Memory | None
    disks: Annotated[list[Disk], Field(max_length=16)]
    gpus: Annotated[list[Gpu], Field(max_length=128)]
    components: Components

    @model_validator(mode="after")
    def unique_devices(self):
        if len({item.uuid for item in self.gpus}) != len(self.gpus) or len({item.index for item in self.gpus}) != len(self.gpus):
            raise ValueError("duplicate GPU identity")
        if len({item.mount_point for item in self.disks}) != len(self.disks):
            raise ValueError("duplicate disk mount")
        captured = datetime.fromisoformat(self.captured_at.replace("Z", "+00:00"))
        for component in (self.components.cpu, self.components.memory, self.components.disks, self.components.gpus):
            if component.last_success_at is not None and datetime.fromisoformat(component.last_success_at.replace("Z", "+00:00")) > captured:
                raise ValueError("component success cannot be in the future")
        return self


class HistoryGpu(StrictModel):
    uuid: Annotated[str, Field(min_length=5, max_length=128, pattern=r"^GPU-[A-Za-z0-9-]+$")]
    utilization_percent: Percent | None
    memory_used_bytes: Nonnegative | None


class HistoryPoint(StrictModel):
    timestamp: Timestamp
    cpu_percent: Percent | None
    memory_percent: Percent | None
    gpus: Annotated[list[HistoryGpu], Field(max_length=128)]

    @model_validator(mode="after")
    def unique_devices(self):
        if len({item.uuid for item in self.gpus}) != len(self.gpus):
            raise ValueError("duplicate history GPU identity")
        return self


class History(Envelope):
    interval_seconds: Literal[30]
    points: Annotated[list[HistoryPoint], Field(max_length=120)]

    @field_validator("interval_seconds", mode="before")
    @classmethod
    def integer_interval(cls, value):
        if type(value) is not int:
            raise ValueError("history interval must be an integer")
        return value

    @model_validator(mode="after")
    def ordered_points(self):
        times = [datetime.fromisoformat(point.timestamp.replace("Z", "+00:00")) for point in self.points]
        captured = datetime.fromisoformat(self.captured_at.replace("Z", "+00:00"))
        if any(a >= b for a, b in zip(times, times[1:])) or (times and (times[-1] > captured or times[0] < captured - timedelta(hours=1))):
            raise ValueError("history points must be ordered within the last hour")
        return self


def validate_snapshot(payload: object) -> Snapshot:
    return Snapshot.model_validate(payload)


def validate_history(payload: object) -> History:
    return History.model_validate(payload)
