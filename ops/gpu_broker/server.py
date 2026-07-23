from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import re
import shlex
import socket
import socketserver
import stat
import struct
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from gpu_resource.transient_scope import (
    SCOPE_SLICE,
    scope_control_group,
    scope_unit_name,
    user_manager_control_group,
    validate_lease_id,
)

from .broker import (
    BASE_DEVICE_POLICY,
    COMPONENT_BUDGETS_MIB,
    COMPONENT_THREAD_PERCENT,
    DEVICE_POLICY,
    EXPECTED_GPU_UUIDS,
    GPU_TOTAL_BUDGET_MIB,
    BrokerError,
    HostGpuBroker,
    Lease,
    OwnerIdentity,
    read_boot_id,
    read_process_start_ticks,
    validate_gpu_inventory,
)


MAX_REQUEST_BYTES = 64 * 1024
# One allow decision performs two complete Docker/systemd CAS inventories,
# two MPS authority audits and a trailing NVML read.  A populated user manager
# can legitimately require about five seconds for that proof, so the deadline
# must exceed the generic Broker client's transport timeout from older builds.
DEFAULT_EXTERNAL_ADMISSION_TIMEOUT_SECONDS = 10.0
MAX_EXTERNAL_ADMISSION_TIMEOUT_SECONDS = 30.0
logger = logging.getLogger("nexpoly_gpu_broker")
_LOCAL_INHERITED_FD_RE = re.compile(
    r"^/proc/(self|[1-9][0-9]*)/fd/([1-9][0-9]*)$"
)
_DOCKER_STARTED_AT_RE = re.compile(
    r"^(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
    r"T(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]{1,9}))?Z$"
)


@dataclass(frozen=True, slots=True)
class ManagedDockerClaim:
    component: str
    environment: str
    compose_project: str
    compose_service: str
    gpu_uuids: frozenset[str]


@dataclass(frozen=True, slots=True)
class ExternalReservationPolicy:
    blocked_gpu_uuids: frozenset[str]
    managed_docker_claims: dict[str, ManagedDockerClaim]
    managed_systemd_claims: dict[str, frozenset[str]]


@dataclass(frozen=True, slots=True)
class DockerGpuClaim:
    container_id: str
    init_pid: int
    started_at: str
    restart_count: int
    registration_id: str | None
    component: str | None
    environment: str | None
    compose_project: str | None
    compose_service: str | None
    gpu_uuids: frozenset[str]


@dataclass(frozen=True, slots=True)
class SystemdGpuDeclarer:
    pid: int
    process_start_ticks: int
    process_cgroup: str
    gpu_uuids: frozenset[str]


@dataclass(frozen=True, slots=True)
class MpsAuthoritySnapshot:
    """One descriptor-bound MPS control-plane CAS result."""

    server_pids: frozenset[int]
    gpu_declarers: frozenset[SystemdGpuDeclarer]
    clients: frozenset["MpsClient"]
    descriptor_authority: bool


@dataclass(frozen=True, slots=True)
class SystemdGpuClaim:
    scope: str
    unit: str
    main_pid: int
    control_group: str
    process_pids: frozenset[int]
    gpu_uuids: frozenset[str]
    static_gpu_uuids: frozenset[str] = frozenset()
    active_gpu_uuids: frozenset[str] = frozenset()
    live_gpu_declarers: tuple[SystemdGpuDeclarer, ...] = ()


class SystemdProcessDisappeared(BrokerError):
    """A previously stable PID vanished during systemd identity revalidation."""

    def __init__(
        self,
        pid: int,
        expected_start_ticks: int,
        expected_control_group: str,
    ) -> None:
        super().__init__(
            "gpu_claim_inventory_changed",
            f"systemd process PID {pid} disappeared during audit",
        )
        self.pid = pid
        self.expected_start_ticks = expected_start_ticks
        self.expected_control_group = expected_control_group


class SystemdMembershipChanged(BrokerError):
    """A systemd unit's complete PID identity membership changed mid-audit."""

    def __init__(
        self,
        scope: str,
        unit: str,
        control_group: str,
        expected_identities: tuple[tuple[int, int, str], ...],
        current_identities: tuple[tuple[int, int, str], ...],
    ) -> None:
        super().__init__(
            "gpu_claim_inventory_changed",
            f"{scope} systemd unit {unit} membership changed during audit",
        )
        self.scope = scope
        self.unit = unit
        self.control_group = control_group
        self.expected_identities = tuple(sorted(expected_identities))
        self.current_identities = tuple(sorted(current_identities))


@dataclass(frozen=True, slots=True)
class _SystemdUnitAuthority:
    unit: str
    active_state: str
    sub_state: str
    invocation_id: str
    main_pid: str
    control_group: str
    user: str
    environment: str
    environment_files: tuple[str, ...]
    pass_environment: str
    unset_environment: str


@dataclass(frozen=True, slots=True)
class _SystemdEnvironmentFileSnapshot:
    declared_path: str
    declared_identity: tuple[int, int, int, int, int, int, int, int, int]
    declared_parent_identity: tuple[int, int, int, int, int, int, int, int, int]
    link_target: str | None
    resolved_path: str
    target_identity: tuple[int, int, int, int, int, int, int, int, int]
    target_parent_identity: tuple[int, int, int, int, int, int, int, int, int]
    content_sha256: str


@dataclass(frozen=True, slots=True)
class _SystemdMissingEnvironmentFileSnapshot:
    declared_path: str
    declared_parent_identity: tuple[int, int, int, int, int, int, int, int, int]


def _remaining_admission_seconds(
    deadline: float | None,
    *,
    maximum: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> float | None:
    if deadline is None:
        return maximum
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise BrokerError(
            "gpu_admission_timeout",
            "external GPU admission deadline expired",
        )
    return remaining if maximum is None else min(maximum, remaining)


def _ensure_admission_open(
    deadline: float | None,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    _remaining_admission_seconds(deadline, monotonic=monotonic)


def _deadline_bounded_run(
    run: Callable[..., subprocess.CompletedProcess[str]],
    *,
    deadline: float | None,
    monotonic: Callable[[], float] = time.monotonic,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    if deadline is None:
        return run

    def bounded(
        command: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        configured_timeout = kwargs.get("timeout")
        maximum = (
            float(configured_timeout)
            if isinstance(configured_timeout, (int, float))
            and not isinstance(configured_timeout, bool)
            else None
        )
        kwargs["timeout"] = _remaining_admission_seconds(
            deadline,
            maximum=maximum,
            monotonic=monotonic,
        )
        completed = run(command, **kwargs)
        _ensure_admission_open(deadline, monotonic=monotonic)
        return completed

    return bounded


def _call_with_optional_deadline(
    callback: Callable[..., Any],
    *arguments: object,
    deadline: float,
    monotonic: Callable[[], float],
) -> Any:
    """Preserve old callback signatures while budgeting deadline-aware ones."""

    _ensure_admission_open(deadline, monotonic=monotonic)
    try:
        parameters = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        supports_deadline = False
    else:
        supports_deadline = any(
            parameter.name == "deadline"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    result = (
        callback(*arguments, deadline=deadline)
        if supports_deadline
        else callback(*arguments)
    )
    _ensure_admission_open(deadline, monotonic=monotonic)
    return result


def _validated_docker_started_at(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Docker StartedAt is invalid")
    match = _DOCKER_STARTED_AT_RE.fullmatch(value)
    if match is None:
        raise ValueError("Docker StartedAt is invalid")
    try:
        datetime(
            int(match["year"]),
            int(match["month"]),
            int(match["day"]),
            int(match["hour"]),
            int(match["minute"]),
            int(match["second"]),
        )
    except ValueError as exc:
        raise ValueError("Docker StartedAt is invalid") from exc
    return value


def query_gpu_inventory() -> dict[int, str]:
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BrokerError("gpu_inventory_unavailable", "nvidia-smi inventory failed") from exc
    inventory: dict[int, str] = {}
    for line in completed.stdout.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 2:
            raise BrokerError("gpu_inventory_unavailable", "invalid nvidia-smi inventory")
        try:
            index = int(fields[0])
        except ValueError as exc:
            raise BrokerError("gpu_inventory_unavailable", "invalid GPU index") from exc
        inventory[index] = fields[1]
    return inventory


def _open_external_reservations(path: Path) -> int:
    """Open an ordinary policy or duplicate one exact inherited local FD."""

    raw = str(path)
    descriptor_match = _LOCAL_INHERITED_FD_RE.fullmatch(raw)
    try:
        if descriptor_match is None:
            if raw.startswith("/proc/"):
                raise OSError("descriptor authority path is ambiguous")
            return os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        process = descriptor_match.group(1)
        descriptor = int(descriptor_match.group(2))
        if (
            descriptor <= 2
            or (process != "self" and int(process) != os.getpid())
        ):
            raise OSError("descriptor authority is not local")
        return os.dup(descriptor)
    except OSError as exc:
        raise BrokerError(
            "external_inventory_unavailable",
            "external GPU reservation inventory is missing or unsafe",
        ) from exc


def load_external_reservations(path: Path) -> ExternalReservationPolicy:
    descriptor = _open_external_reservations(path)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise BrokerError(
                "external_inventory_unavailable",
                "external GPU reservation inventory must be a private regular file",
            )
        if stat.S_IMODE(metadata.st_mode) != 0o600 or (
            metadata.st_uid != 1001 or metadata.st_gid != 1001
        ):
            raise BrokerError(
                "external_inventory_unavailable",
                "external GPU reservation inventory must be 0600 and owned by 1001:1001",
            )
        if metadata.st_size > 64 * 1024:
            raise BrokerError(
                "external_inventory_unavailable",
                "external GPU reservation inventory is oversized",
            )
        try:
            raw_payload = os.pread(descriptor, 64 * 1024 + 1, 0)
            after = os.fstat(descriptor)
            if (
                len(raw_payload) != metadata.st_size
                or after.st_dev != metadata.st_dev
                or after.st_ino != metadata.st_ino
                or after.st_size != metadata.st_size
                or after.st_mtime_ns != metadata.st_mtime_ns
                or after.st_ctime_ns != metadata.st_ctime_ns
            ):
                raise OSError(
                    "external GPU reservation inventory changed while read"
                )
            payload = json.loads(raw_payload.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BrokerError(
                "external_inventory_unavailable",
                "external GPU reservation inventory is unreadable",
            ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "schema_version",
            "blocked_gpu_uuids",
            "managed_docker_claims",
            "managed_systemd_claims",
        }
        or payload.get("schema_version") != 1
        or isinstance(payload.get("schema_version"), bool)
    ):
        raise BrokerError(
            "external_inventory_unavailable",
            "external GPU reservation inventory schema is invalid",
        )
    blocked = payload.get("blocked_gpu_uuids")
    if not isinstance(blocked, dict):
        raise BrokerError(
            "external_inventory_unavailable",
            "blocked_gpu_uuids must be an object",
        )
    for uuid, reason in blocked.items():
        if uuid not in EXPECTED_GPU_UUIDS.values():
            raise BrokerError("external_inventory_unavailable", "blocked GPU UUID is invalid")
        if not isinstance(reason, str) or not reason.strip():
            raise BrokerError("external_inventory_unavailable", "blocked GPU reason is invalid")
    raw_docker = payload.get("managed_docker_claims")
    if not isinstance(raw_docker, dict):
        raise BrokerError(
            "external_inventory_unavailable", "managed_docker_claims must be an object"
        )
    managed_docker: dict[str, ManagedDockerClaim] = {}
    for registration_id, raw in raw_docker.items():
        if (
            not isinstance(registration_id, str)
            or not registration_id
            or not isinstance(raw, dict)
            or set(raw)
            != {
                "component",
                "environment",
                "compose_project",
                "compose_service",
                "gpu_uuids",
            }
        ):
            raise BrokerError(
                "external_inventory_unavailable", "managed Docker registration is invalid"
            )
        strings = tuple(
            raw.get(name)
            for name in (
                "component",
                "environment",
                "compose_project",
                "compose_service",
            )
        )
        gpu_uuids = raw.get("gpu_uuids")
        if (
            any(not isinstance(value, str) or not value for value in strings)
            or strings[0] not in COMPONENT_BUDGETS_MIB
            or strings[1] not in {"prod", "dev"}
            or not isinstance(gpu_uuids, list)
            or not gpu_uuids
            or len(gpu_uuids) != len(set(gpu_uuids))
            or any(uuid not in EXPECTED_GPU_UUIDS.values() for uuid in gpu_uuids)
        ):
            raise BrokerError(
                "external_inventory_unavailable", "managed Docker registration is invalid"
            )
        baseline_policy = {
            EXPECTED_GPU_UUIDS[index]
            for index in BASE_DEVICE_POLICY[(strings[1], strings[0])]
        }
        if not set(gpu_uuids).issubset(baseline_policy):
            raise BrokerError(
                "external_inventory_unavailable",
                "managed Docker GPUs are outside component policy",
            )
        managed_docker[registration_id] = ManagedDockerClaim(
            component=strings[0],
            environment=strings[1],
            compose_project=strings[2],
            compose_service=strings[3],
            gpu_uuids=frozenset(gpu_uuids),
        )
    raw_systemd = payload.get("managed_systemd_claims")
    if not isinstance(raw_systemd, dict):
        raise BrokerError(
            "external_inventory_unavailable", "managed_systemd_claims must be an object"
        )
    managed_systemd: dict[str, frozenset[str]] = {}
    for identity, raw in raw_systemd.items():
        scope, separator, unit = (
            identity.partition(":") if isinstance(identity, str) else ("", "", "")
        )
        if (
            scope not in {"user", "system"}
            or separator != ":"
            or not isinstance(unit, str)
            or not unit.endswith(".service")
            or not isinstance(raw, dict)
            or set(raw) != {"gpu_uuids", "reason"}
            or not isinstance(raw.get("reason"), str)
            or not raw["reason"].strip()
            or not isinstance(raw.get("gpu_uuids"), list)
            or not raw["gpu_uuids"]
            or len(raw["gpu_uuids"]) != len(set(raw["gpu_uuids"]))
            or any(
                uuid not in EXPECTED_GPU_UUIDS.values() for uuid in raw["gpu_uuids"]
            )
        ):
            raise BrokerError(
                "external_inventory_unavailable", "managed systemd registration is invalid"
            )
        managed_systemd[identity] = frozenset(raw["gpu_uuids"])
    return ExternalReservationPolicy(
        blocked_gpu_uuids=frozenset(blocked),
        managed_docker_claims=managed_docker,
        managed_systemd_claims=managed_systemd,
    )


def query_docker_gpu_claims(
    *,
    run=subprocess.run,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[DockerGpuClaim, ...]:
    run = _deadline_bounded_run(
        run,
        deadline=deadline,
        monotonic=monotonic,
    )

    def list_container_ids() -> tuple[str, ...]:
        _ensure_admission_open(deadline, monotonic=monotonic)
        try:
            listed = run(
                ["docker", "container", "ls", "--quiet", "--no-trunc"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "Docker GPU claim inventory failed",
            ) from exc
        container_ids = tuple(
            line.strip() for line in listed.stdout.splitlines() if line.strip()
        )
        if (
            len(container_ids) != len(set(container_ids))
            or any(
                len(container_id) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in container_id
                )
                for container_id in container_ids
            )
        ):
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "Docker container inventory identity is invalid",
            )
        return tuple(sorted(container_ids))

    def inspect_claims(
        container_ids: tuple[str, ...],
    ) -> tuple[DockerGpuClaim, ...]:
        _ensure_admission_open(deadline, monotonic=monotonic)
        try:
            inspected = run(
                ["docker", "container", "inspect", *container_ids],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            payload = json.loads(inspected.stdout)
        except (
            OSError,
            ValueError,
            json.JSONDecodeError,
            subprocess.SubprocessError,
        ) as exc:
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "Docker GPU claim inventory failed",
            ) from exc
        if not isinstance(payload, list) or len(payload) != len(container_ids):
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "Docker inspect inventory is incomplete",
            )
        expected_ids = set(container_ids)
        seen_container_ids: set[str] = set()
        seen_registrations: set[str] = set()
        claims: list[DockerGpuClaim] = []
        for raw in payload:
            _ensure_admission_open(deadline, monotonic=monotonic)
            try:
                container_id = raw["Id"]
                state = raw["State"]
                restart_count = raw["RestartCount"]
                config = raw["Config"]
                host_config = raw["HostConfig"]
                if (
                    isinstance(container_id, str)
                    and container_id in seen_container_ids
                ):
                    raise BrokerError(
                        "gpu_claim_inventory_unavailable",
                        "Docker inspect identities are not one-to-one with its list",
                    )
                if (
                    not isinstance(container_id, str)
                    or container_id not in expected_ids
                    or state.get("Running") is not True
                    or isinstance(state.get("Pid"), bool)
                    or not isinstance(state.get("Pid"), int)
                    or state["Pid"] <= 0
                    or isinstance(restart_count, bool)
                    or not isinstance(restart_count, int)
                    or restart_count < 0
                    or not isinstance(config.get("Labels") or {}, dict)
                    or not isinstance(config.get("Env") or [], list)
                    or not isinstance(
                        host_config.get("DeviceRequests") or [], list
                    )
                ):
                    raise ValueError("invalid Docker inspect identity")
                seen_container_ids.add(container_id)
                started_at = _validated_docker_started_at(
                    state.get("StartedAt")
                )
                labels = config.get("Labels") or {}
                device_request_claims: set[str] = set()
                environment_claims: set[str] = set()
                has_gpu_device_request = False
                for request in host_config.get("DeviceRequests") or []:
                    if not isinstance(request, dict):
                        raise ValueError("invalid Docker DeviceRequest")
                    capabilities = request.get("Capabilities") or []
                    is_gpu = request.get("Driver") == "nvidia" or any(
                        isinstance(group, list) and "gpu" in group
                        for group in capabilities
                    )
                    if not is_gpu:
                        continue
                    has_gpu_device_request = True
                    device_ids = request.get("DeviceIDs") or []
                    if device_ids:
                        device_request_claims.update(
                            _resolve_gpu_claim_tokens(device_ids)
                        )
                    elif request.get("Count") not in {0, None}:
                        device_request_claims.update(EXPECTED_GPU_UUIDS.values())
                for environment_entry in config.get("Env") or []:
                    if not isinstance(environment_entry, str):
                        raise ValueError("invalid Docker environment")
                    if not environment_entry.startswith(
                        "NVIDIA_VISIBLE_DEVICES="
                    ):
                        continue
                    value = environment_entry.split("=", 1)[1].strip()
                    if value.lower() in {"", "none", "void"}:
                        continue
                    if value.lower() == "all":
                        environment_claims.update(EXPECTED_GPU_UUIDS.values())
                    else:
                        environment_claims.update(
                            _resolve_gpu_claim_tokens(value.split(","))
                        )
                claimed = (
                    device_request_claims
                    if has_gpu_device_request
                    else environment_claims
                )
                if not claimed:
                    continue
                registration_id = labels.get("com.nexpoly.gpu.registration")
                if registration_id is not None:
                    if (
                        not isinstance(registration_id, str)
                        or not registration_id
                    ):
                        raise ValueError("invalid managed registration label")
                    if registration_id in seen_registrations:
                        raise ValueError("duplicate managed registration label")
                    seen_registrations.add(registration_id)
                claims.append(
                    DockerGpuClaim(
                        container_id=container_id,
                        init_pid=state["Pid"],
                        started_at=started_at,
                        restart_count=restart_count,
                        registration_id=registration_id,
                        component=labels.get("com.nexpoly.gpu.component"),
                        environment=labels.get("com.nexpoly.gpu.environment"),
                        compose_project=labels.get("com.docker.compose.project"),
                        compose_service=labels.get("com.docker.compose.service"),
                        gpu_uuids=frozenset(claimed),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise BrokerError(
                    "gpu_claim_inventory_unavailable",
                    "Docker GPU claim is invalid",
                ) from exc
        if seen_container_ids != expected_ids:
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "Docker inspect identities are not one-to-one with its list",
            )
        return tuple(sorted(claims, key=lambda claim: claim.container_id))

    initial_ids = list_container_ids()
    if not initial_ids:
        if list_container_ids() != initial_ids:
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "Docker container inventory changed during audit",
            )
        return ()
    initial_claims = inspect_claims(initial_ids)
    middle_ids = list_container_ids()
    if middle_ids != initial_ids:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            "Docker container inventory changed during audit",
        )
    final_claims = inspect_claims(middle_ids)
    if final_claims != initial_claims:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            "Docker container fingerprint changed during audit",
        )
    if list_container_ids() != initial_ids:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            "Docker container inventory changed during audit",
        )
    return final_claims


def _resolve_gpu_claim_tokens(tokens: object) -> frozenset[str]:
    if not isinstance(tokens, (list, tuple)):
        raise ValueError("GPU claim tokens must be a list")
    resolved: set[str] = set()
    for raw_token in tokens:
        if not isinstance(raw_token, str):
            raise ValueError("GPU claim token must be a string")
        token = raw_token.strip()
        if token.isdigit() and int(token) in EXPECTED_GPU_UUIDS:
            resolved.add(EXPECTED_GPU_UUIDS[int(token)])
        elif token in EXPECTED_GPU_UUIDS.values():
            resolved.add(token)
        else:
            raise ValueError(f"GPU claim is outside governance: {token}")
    return frozenset(resolved)


def _mps_device_matches(reported_uuid: str, expected_uuid: str) -> bool:
    return (
        isinstance(reported_uuid, str)
        and len(reported_uuid) >= 12
        and reported_uuid.startswith("GPU-")
        and all(
            character in "0123456789abcdefABCDEF-"
            for character in reported_uuid[4:]
        )
        and expected_uuid.startswith(reported_uuid)
    )


def _read_process_environment(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError as exc:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            f"cannot read live environment for systemd MainPID {pid}",
        ) from exc
    if len(raw) > 1024 * 1024:
        raise BrokerError(
            "gpu_claim_inventory_unavailable", "systemd live environment is oversized"
        )
    result: dict[str, str] = {}
    try:
        for item in raw.rstrip(b"\0").split(b"\0") if raw else ():
            name, value = item.decode("utf-8").split("=", 1)
            if not name or name in result:
                raise ValueError("duplicate environment key")
            result[name] = value
    except (UnicodeError, ValueError) as exc:
        raise BrokerError(
            "gpu_claim_inventory_unavailable", "systemd live environment is invalid"
        ) from exc
    return result


def _systemd_environment_file_snapshot(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _capture_systemd_environment_file(
    path: Path,
) -> tuple[dict[str, str], _SystemdEnvironmentFileSnapshot]:
    """Read and fingerprint one stable, non-writable EnvironmentFile."""

    if not path.is_absolute():
        raise BrokerError(
            "gpu_claim_inventory_unavailable", "systemd EnvironmentFile is unsafe"
        )
    try:
        declared_parent_before = path.parent.stat(follow_symlinks=False)
        declared_before = path.lstat()
    except OSError as exc:
        raise BrokerError(
            "gpu_claim_inventory_unavailable", "systemd EnvironmentFile is unsafe"
        ) from exc

    read_path = path
    link_target: str | None = None
    if stat.S_ISLNK(declared_before.st_mode):
        # Root-owned compatibility links (for example
        # /etc/default/locale -> ../locale.conf) are valid systemd inputs.
        # A workload-owned link is mutable declaration authority and is
        # therefore rejected even when its current target appears safe.
        if declared_before.st_uid != 0:
            raise BrokerError(
                "gpu_claim_inventory_unavailable", "systemd EnvironmentFile is unsafe"
            )
        try:
            link_target = os.readlink(path)
            read_path = path.resolve(strict=True)
            target_before = read_path.stat(follow_symlinks=False)
        except (OSError, RuntimeError) as exc:
            raise BrokerError(
                "gpu_claim_inventory_unavailable", "systemd EnvironmentFile is unsafe"
            ) from exc
        if (
            not stat.S_ISREG(target_before.st_mode)
            or target_before.st_uid != 0
            or target_before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise BrokerError(
                "gpu_claim_inventory_unavailable", "systemd EnvironmentFile is unsafe"
            )
    else:
        target_before = declared_before
    try:
        target_parent_before = read_path.parent.stat(follow_symlinks=False)
    except OSError as exc:
        raise BrokerError(
            "gpu_claim_inventory_unavailable", "systemd EnvironmentFile is unsafe"
        ) from exc

    if (
        not stat.S_ISREG(target_before.st_mode)
        or target_before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or target_before.st_nlink != 1
        or target_before.st_size > 1024 * 1024
    ):
        raise BrokerError(
            "gpu_claim_inventory_unavailable", "systemd EnvironmentFile is unsafe"
        )

    descriptor = -1
    try:
        descriptor = os.open(
            read_path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        opened_before = os.fstat(descriptor)
        if _systemd_environment_file_snapshot(
            opened_before
        ) != _systemd_environment_file_snapshot(target_before):
            raise BrokerError(
                "gpu_claim_inventory_unavailable", "systemd EnvironmentFile is unsafe"
            )
        raw = os.pread(descriptor, 1024 * 1024 + 1, 0)
        opened_after = os.fstat(descriptor)
        declared_parent_after = path.parent.stat(follow_symlinks=False)
        declared_after = path.lstat()
        target_parent_after = read_path.parent.stat(follow_symlinks=False)
        if (
            len(raw) != opened_before.st_size
            or _systemd_environment_file_snapshot(opened_after)
            != _systemd_environment_file_snapshot(opened_before)
            or _systemd_environment_file_snapshot(declared_after)
            != _systemd_environment_file_snapshot(declared_before)
            or _systemd_environment_file_snapshot(declared_parent_after)
            != _systemd_environment_file_snapshot(declared_parent_before)
            or _systemd_environment_file_snapshot(target_parent_after)
            != _systemd_environment_file_snapshot(target_parent_before)
        ):
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "systemd EnvironmentFile identity changed while read",
            )
        if link_target is None:
            if _systemd_environment_file_snapshot(
                declared_after
            ) != _systemd_environment_file_snapshot(opened_after):
                raise BrokerError(
                    "gpu_claim_inventory_unavailable",
                    "systemd EnvironmentFile identity changed while read",
                )
        else:
            resolved_after = path.resolve(strict=True)
            target_after = resolved_after.stat(follow_symlinks=False)
            if (
                os.readlink(path) != link_target
                or resolved_after != read_path
                or _systemd_environment_file_snapshot(target_after)
                != _systemd_environment_file_snapshot(opened_after)
            ):
                raise BrokerError(
                    "gpu_claim_inventory_unavailable",
                    "systemd EnvironmentFile identity changed while read",
                )
        lines = raw.decode("utf-8").splitlines()
    except BrokerError:
        raise
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise BrokerError(
            "gpu_claim_inventory_unavailable", "systemd EnvironmentFile is unreadable"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    result: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        try:
            fields = shlex.split(stripped, comments=True)
        except ValueError as exc:
            raise BrokerError(
                "gpu_claim_inventory_unavailable", "systemd EnvironmentFile is invalid"
            ) from exc
        for field in fields:
            if "=" not in field:
                raise BrokerError(
                    "gpu_claim_inventory_unavailable", "systemd EnvironmentFile is invalid"
                )
            name, value = field.split("=", 1)
            if not name:
                raise BrokerError(
                    "gpu_claim_inventory_unavailable", "systemd EnvironmentFile is invalid"
                )
            result[name] = value
    return (
        result,
        _SystemdEnvironmentFileSnapshot(
            declared_path=str(path),
            declared_identity=_systemd_environment_file_snapshot(
                declared_before
            ),
            declared_parent_identity=_systemd_environment_file_snapshot(
                declared_parent_before
            ),
            link_target=link_target,
            resolved_path=str(read_path),
            target_identity=_systemd_environment_file_snapshot(opened_after),
            target_parent_identity=_systemd_environment_file_snapshot(
                target_parent_after
            ),
            content_sha256=sha256(raw).hexdigest(),
        ),
    )


def _read_systemd_environment_file(path: Path) -> dict[str, str]:
    result, _snapshot = _capture_systemd_environment_file(path)
    return result


def _capture_missing_systemd_environment_file(
    path: Path,
) -> _SystemdMissingEnvironmentFileSnapshot:
    try:
        parent_before = path.parent.stat(follow_symlinks=False)
        path.lstat()
    except FileNotFoundError:
        try:
            parent_after = path.parent.stat(follow_symlinks=False)
        except OSError as exc:
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "systemd optional EnvironmentFile authority is unavailable",
            ) from exc
        if _systemd_environment_file_snapshot(
            parent_after
        ) != _systemd_environment_file_snapshot(parent_before):
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "systemd optional EnvironmentFile identity changed during audit",
            )
        return _SystemdMissingEnvironmentFileSnapshot(
            declared_path=str(path),
            declared_parent_identity=_systemd_environment_file_snapshot(
                parent_after
            ),
        )
    except OSError as exc:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            "systemd optional EnvironmentFile authority is unavailable",
        ) from exc
    raise BrokerError(
        "gpu_claim_inventory_unavailable",
        "systemd optional EnvironmentFile appeared during audit",
    )


def _read_unified_process_cgroup(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    except OSError as exc:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            f"cannot read systemd cgroup for PID {pid}",
        ) from exc
    matches = [
        line.split(":", 2)[2]
        for line in raw.splitlines()
        if line.startswith("0::") and len(line.split(":", 2)) == 3
    ]
    if len(matches) != 1:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            "systemd process cgroup identity is invalid",
        )
    return matches[0]


def _read_process_uids(pid: int) -> tuple[int, int, int, int]:
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
        line = next(
            item for item in status.splitlines() if item.startswith("Uid:")
        )
        values = tuple(int(value) for value in line.split(":", 1)[1].split())
    except (OSError, UnicodeError, StopIteration, ValueError, IndexError) as exc:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            f"cannot read systemd process credentials for PID {pid}",
        ) from exc
    if len(values) != 4:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            f"systemd process credentials are invalid for PID {pid}",
        )
    return values[0], values[1], values[2], values[3]


def _systemd_cgroup_contains(candidate: str, control_group: str) -> bool:
    return (
        candidate == control_group
        or candidate.startswith(control_group.rstrip("/") + "/")
    )


def _systemd_cgroup_is_user_manager_sibling(
    candidate: str,
    manager_control_group: str,
) -> bool:
    """Recognize one canonical login scope directly beside user@.service."""

    if (
        not isinstance(candidate, str)
        or not candidate.startswith("/")
        or candidate == "/"
        or candidate.endswith("/")
        or "//" in candidate
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in candidate
        )
        or any(part in {"", ".", ".."} for part in candidate.split("/")[1:])
    ):
        return False
    user_slice_control_group, separator, _manager_unit = (
        manager_control_group.rpartition("/")
    )
    if not separator:
        return False
    return re.fullmatch(
        re.escape(user_slice_control_group)
        + r"/session-[A-Za-z0-9][A-Za-z0-9_-]*\.scope",
        candidate,
    ) is not None


def _read_control_group_processes(
    control_group: str,
    *,
    read_process_cgroup=_read_unified_process_cgroup,
) -> frozenset[int]:
    if (
        not control_group.startswith("/")
        or control_group == "/"
        or "\n" in control_group
        or any(part in {"", ".", ".."} for part in control_group.split("/")[1:])
    ):
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            "systemd ControlGroup is invalid",
        )
    processes: set[int] = set()
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError as exc:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            "cannot enumerate systemd cgroup processes",
        ) from exc
    for entry in entries:
        if not entry.name.isdigit() or entry.name.startswith("0"):
            continue
        pid = int(entry.name)
        try:
            process_cgroup = read_process_cgroup(pid)
        except BrokerError:
            continue
        if _systemd_cgroup_contains(process_cgroup, control_group):
            processes.add(pid)
    return frozenset(processes)


def _snapshot_systemd_process_cgroups(
    *,
    proc_root: Path = Path("/proc"),
    read_process_cgroup=_read_unified_process_cgroup,
    read_process_start_ticks=read_process_start_ticks,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[tuple[int, int, str], ...]:
    """Take one host PID/cgroup snapshot for the entire systemd inventory."""

    _ensure_admission_open(deadline, monotonic=monotonic)
    try:
        entries = tuple(proc_root.iterdir())
    except OSError as exc:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            "cannot enumerate systemd cgroup processes",
        ) from exc
    snapshot: list[tuple[int, int, str]] = []
    for entry in entries:
        _ensure_admission_open(deadline, monotonic=monotonic)
        if not entry.name.isdigit() or entry.name.startswith("0"):
            continue
        pid = int(entry.name)
        try:
            start_before = read_process_start_ticks(pid)
            control_group = read_process_cgroup(pid)
            start_after = read_process_start_ticks(pid)
        except BrokerError:
            # Processes may exit between /proc enumeration and identity read.
            continue
        except Exception as exc:
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "cannot identify systemd cgroup process",
            ) from exc
        if (
            not isinstance(start_before, int)
            or isinstance(start_before, bool)
            or start_before <= 0
            or not isinstance(start_after, int)
            or isinstance(start_after, bool)
            or start_after <= 0
            or not isinstance(control_group, str)
            or not control_group.startswith("/")
        ):
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "systemd process identity is invalid",
            )
        if start_before != start_after:
            # A PID may legitimately be recycled during the global enumeration;
            # it is excluded rather than attributed to either process.
            continue
        snapshot.append((pid, start_before, control_group))
    _ensure_admission_open(deadline, monotonic=monotonic)
    return tuple(snapshot)


def _read_stable_systemd_process_identity(
    pid: int,
    *,
    read_process_cgroup=_read_unified_process_cgroup,
    read_process_start_ticks=read_process_start_ticks,
) -> tuple[int, str]:
    """Read one PID/cgroup identity without crossing PID reuse or migration."""

    try:
        start_before = read_process_start_ticks(pid)
        control_group = read_process_cgroup(pid)
        start_after = read_process_start_ticks(pid)
    except BrokerError as exc:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            f"cannot identify systemd process PID {pid}",
        ) from exc
    except Exception as exc:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            f"cannot identify systemd process PID {pid}",
        ) from exc
    if (
        not isinstance(start_before, int)
        or isinstance(start_before, bool)
        or start_before <= 0
        or not isinstance(start_after, int)
        or isinstance(start_after, bool)
        or start_after <= 0
        or start_before != start_after
        or not isinstance(control_group, str)
        or not control_group.startswith("/")
    ):
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            f"systemd process identity changed for PID {pid}",
        )
    return start_before, control_group


def _verify_systemd_process_identity(
    pid: int,
    expected_start_ticks: int,
    expected_control_group: str,
    *,
    read_process_cgroup=_read_unified_process_cgroup,
    read_process_start_ticks=read_process_start_ticks,
) -> None:
    try:
        current = _read_stable_systemd_process_identity(
            pid,
            read_process_cgroup=read_process_cgroup,
            read_process_start_ticks=read_process_start_ticks,
        )
    except BrokerError as exc:
        cause: BaseException | None = exc
        while cause is not None and not isinstance(
            cause, (FileNotFoundError, ProcessLookupError)
        ):
            cause = cause.__cause__
        if cause is None:
            raise
        raise SystemdProcessDisappeared(
            pid,
            expected_start_ticks,
            expected_control_group,
        ) from exc
    if current != (expected_start_ticks, expected_control_group):
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            f"systemd process identity changed for PID {pid}",
        )


def _parse_systemd_environment(
    raw: str,
    *,
    declaration: str,
    require_values: bool,
) -> tuple[str, ...]:
    try:
        entries = tuple(shlex.split(raw))
    except ValueError as exc:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            f"systemd {declaration} declaration is invalid",
        ) from exc
    for entry in entries:
        name = entry.split("=", 1)[0]
        if (
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
            or (require_values and "=" not in entry)
        ):
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                f"systemd {declaration} declaration is invalid",
            )
    return entries


def _query_systemd_manager_environment(
    prefix: list[str],
    scope: str,
    *,
    run=subprocess.run,
) -> dict[str, str]:
    try:
        completed = run(
            [*prefix, "show-environment"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            f"cannot query {scope} systemd manager environment",
        ) from exc
    if completed.returncode != 0:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            f"cannot query {scope} systemd manager environment",
        )
    environment: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if "=" not in line:
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "systemd manager environment is invalid",
            )
        name, value = line.split("=", 1)
        if (
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
            or name in environment
        ):
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "systemd manager environment is invalid",
            )
        environment[name] = value
    return environment


_AUDITED_SYSTEMD_ACTIVE_STATES = frozenset(
    {"active", "activating", "reloading", "deactivating"}
)


def _query_systemd_unit_authorities(
    scope: str,
    *,
    run=subprocess.run,
) -> tuple[_SystemdUnitAuthority, ...]:
    """Return one canonical list/show snapshot for a systemd manager."""

    prefix = ["systemctl", "--user"] if scope == "user" else ["systemctl"]
    try:
        listed = run(
            [
                *prefix,
                "list-units",
                "--type=service",
                "--state=active,activating,reloading,deactivating",
                "--no-legend",
                "--plain",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            f"cannot query live {scope} systemd services",
        ) from exc
    if listed.returncode != 0:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            f"cannot query live {scope} systemd services",
        )
    listed_states: dict[str, tuple[str, str]] = {}
    for line in listed.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split(None, 4)
        if len(fields) < 4:
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "systemd service inventory is invalid",
            )
        unit, _load_state, active_state, sub_state = fields[:4]
        if (
            not unit.endswith(".service")
            or unit in listed_states
            or active_state not in _AUDITED_SYSTEMD_ACTIVE_STATES
            or re.fullmatch(r"[A-Za-z0-9_.:@-]+", sub_state) is None
        ):
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "systemd service inventory is invalid",
            )
        listed_states[unit] = (active_state, sub_state)
    if not listed_states:
        return ()
    try:
        shown = run(
            [
                *prefix,
                "show",
                "--property=Id",
                "--property=ActiveState",
                "--property=SubState",
                "--property=InvocationID",
                "--property=MainPID",
                "--property=ControlGroup",
                "--property=User",
                "--property=Environment",
                "--property=EnvironmentFiles",
                "--property=PassEnvironment",
                "--property=UnsetEnvironment",
                *sorted(listed_states),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            f"cannot query {scope} systemd GPU declarations",
        ) from exc
    if shown.returncode != 0:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            f"cannot query {scope} systemd GPU declarations",
        )
    blocks = [block for block in shown.stdout.strip().split("\n\n") if block]
    if len(blocks) != len(listed_states):
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            "systemd GPU declaration response is incomplete",
        )
    authorities: list[_SystemdUnitAuthority] = []
    seen_units: set[str] = set()
    required_scalar_names = {
        "Id",
        "ActiveState",
        "SubState",
        "InvocationID",
        "MainPID",
        "ControlGroup",
    }
    optional_scalar_names = {
        "User",
        "Environment",
        "PassEnvironment",
        "UnsetEnvironment",
    }
    for block in blocks:
        scalar_properties: dict[str, str] = {}
        environment_files: list[str] = []
        for line in block.splitlines():
            if "=" not in line:
                raise BrokerError(
                    "gpu_claim_inventory_unavailable",
                    "systemd GPU declaration response is invalid",
                )
            name, value = line.split("=", 1)
            if name == "EnvironmentFiles":
                environment_files.append(value)
            elif (
                name not in required_scalar_names | optional_scalar_names
                or name in scalar_properties
            ):
                raise BrokerError(
                    "gpu_claim_inventory_unavailable",
                    "systemd GPU declaration response is invalid",
                )
            else:
                scalar_properties[name] = value
        if (
            not required_scalar_names.issubset(scalar_properties)
            or set(scalar_properties)
            - required_scalar_names
            - optional_scalar_names
        ):
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "systemd GPU declaration response is incomplete",
            )
        for name in optional_scalar_names:
            scalar_properties.setdefault(name, "")
        unit = scalar_properties["Id"]
        active_state = scalar_properties["ActiveState"]
        sub_state = scalar_properties["SubState"]
        invocation_id = scalar_properties["InvocationID"]
        if (
            unit not in listed_states
            or unit in seen_units
            or listed_states[unit] != (active_state, sub_state)
            or active_state not in _AUDITED_SYSTEMD_ACTIVE_STATES
            or re.fullmatch(r"[A-Za-z0-9_.:@-]+", sub_state) is None
            or re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None
        ):
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "systemd GPU declaration identity is invalid",
            )
        seen_units.add(unit)
        authorities.append(
            _SystemdUnitAuthority(
                unit=unit,
                active_state=active_state,
                sub_state=sub_state,
                invocation_id=invocation_id,
                main_pid=scalar_properties["MainPID"],
                control_group=scalar_properties["ControlGroup"],
                user=scalar_properties["User"],
                environment=scalar_properties["Environment"],
                environment_files=tuple(environment_files),
                pass_environment=scalar_properties["PassEnvironment"],
                unset_environment=scalar_properties["UnsetEnvironment"],
            )
        )
    if seen_units != set(listed_states):
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            "systemd GPU declaration response is incomplete",
        )
    return tuple(sorted(authorities, key=lambda authority: authority.unit))


def query_systemd_gpu_claims(
    *,
    compute_processes: dict[str, frozenset[int]] | None = None,
    compute_process_identities: dict[int, tuple[int, str]] | None = None,
    run=subprocess.run,
    read_process_environment=_read_process_environment,
    read_control_group_processes=_read_control_group_processes,
    compute_process_query=None,
    read_process_cgroup=_read_unified_process_cgroup,
    read_process_uids=_read_process_uids,
    read_process_start_ticks=read_process_start_ticks,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[SystemdGpuClaim, ...]:
    """Inventory GPU-visible services without reading unrelated root envs.

    The system manager is an explicitly trusted root control plane.  Its
    unmarked services are skipped unless an NVIDIA compute PID is already in
    their cgroup.  User services are fully inspected because they share the
    Broker UID.  Global NVIDIA and Docker inventories remain independent,
    fail-closed admission gates in :class:`ExternalGpuGuard`.
    """

    run = _deadline_bounded_run(
        run,
        deadline=deadline,
        monotonic=monotonic,
    )
    relevant_names = ("CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES")
    if compute_processes is not None and compute_process_query is not None:
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            "systemd NVIDIA process authority is ambiguous",
        )
    if compute_processes is None:
        compute_processes = (
            {} if compute_process_query is None else compute_process_query()
        )
    if not isinstance(compute_processes, dict) or any(
        not isinstance(uuid, str)
        or not isinstance(pids, frozenset)
        or any(
            not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            for pid in pids
        )
        for uuid, pids in compute_processes.items()
    ):
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            "systemd NVIDIA process inventory is invalid",
        )
    compute_pids = frozenset(
        pid for pids in compute_processes.values() for pid in pids
    )
    if compute_process_identities is None:
        compute_process_identities = {}
    if not isinstance(compute_process_identities, dict) or any(
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or pid not in compute_pids
        or not isinstance(identity, tuple)
        or len(identity) != 2
        or not isinstance(identity[0], int)
        or isinstance(identity[0], bool)
        or identity[0] <= 0
        or not isinstance(identity[1], str)
        or not identity[1].startswith("/")
        or identity[1] == "/"
        or identity[1].endswith("/")
        or "//" in identity[1]
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in identity[1]
        )
        or any(part in {"", ".", ".."} for part in identity[1].split("/")[1:])
        for pid, identity in compute_process_identities.items()
    ):
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            "systemd NVIDIA process identity hints are invalid",
        )

    process_cgroup_snapshot = (
        _snapshot_systemd_process_cgroups(
            read_process_cgroup=read_process_cgroup,
            read_process_start_ticks=read_process_start_ticks,
            deadline=deadline,
            monotonic=monotonic,
        )
        if read_control_group_processes is _read_control_group_processes
        else None
    )
    captured_process_identities = {
        pid: (start_ticks, process_cgroup)
        for pid, start_ticks, process_cgroup in process_cgroup_snapshot or ()
    }
    claims: list[SystemdGpuClaim] = []
    verified_process_identities: set[tuple[int, int, str]] = set()
    authority_snapshots: dict[str, tuple[_SystemdUnitAuthority, ...]] = {}
    manager_environment_snapshots: dict[str, dict[str, str]] = {}
    environment_file_snapshots: list[
        _SystemdEnvironmentFileSnapshot
        | _SystemdMissingEnvironmentFileSnapshot
    ] = []
    observed_memberships: dict[
        tuple[str, str, str], dict[int, tuple[int, str]]
    ] = {}
    for scope in ("user", "system"):
        _ensure_admission_open(deadline, monotonic=monotonic)
        prefix = ["systemctl", "--user"] if scope == "user" else ["systemctl"]
        authorities = _query_systemd_unit_authorities(scope, run=run)
        authority_snapshots[scope] = authorities
        if not authorities:
            continue
        manager_environment: dict[str, str] | None = None
        for authority in authorities:
            _ensure_admission_open(deadline, monotonic=monotonic)
            scalar_properties = {
                "Id": authority.unit,
                "MainPID": authority.main_pid,
                "ControlGroup": authority.control_group,
                "User": authority.user,
                "Environment": authority.environment,
                "PassEnvironment": authority.pass_environment,
                "UnsetEnvironment": authority.unset_environment,
            }
            environment_files = list(authority.environment_files)
            unit = scalar_properties["Id"]
            if (
                authority.active_state
                in {"activating", "reloading", "deactivating"}
                and environment_files
            ):
                # systemd may consume a mutable EnvironmentFile after this
                # audit while MainPID is still zero or membership is changing.
                # There is no stable execution identity to bind, so admission
                # remains closed until the unit reaches a steady active state.
                raise BrokerError(
                    "gpu_claim_inventory_unavailable",
                    "transitional systemd EnvironmentFile authority is unsafe",
                )
            if not scalar_properties["MainPID"].isdigit():
                raise BrokerError(
                    "gpu_claim_inventory_unavailable", "systemd MainPID is invalid"
                )
            main_pid = int(scalar_properties["MainPID"])
            control_group = scalar_properties["ControlGroup"]
            if control_group:
                if (
                    not control_group.startswith("/")
                    or control_group == "/"
                    or "\n" in control_group
                    or any(
                        part in {"", ".", ".."}
                        for part in control_group.split("/")[1:]
                    )
                ):
                    raise BrokerError(
                        "gpu_claim_inventory_unavailable",
                        "systemd ControlGroup is invalid",
                    )
                try:
                    if process_cgroup_snapshot is not None:
                        process_identities = {
                            pid: (start_ticks, process_cgroup)
                            for pid, start_ticks, process_cgroup in process_cgroup_snapshot
                            if _systemd_cgroup_contains(
                                process_cgroup, control_group
                            )
                        }
                        process_pids = frozenset(process_identities)
                    else:
                        process_pids = read_control_group_processes(control_group)
                        if (
                            not isinstance(process_pids, frozenset)
                            or any(
                                not isinstance(pid, int)
                                or isinstance(pid, bool)
                                or pid <= 0
                                for pid in process_pids
                            )
                        ):
                            raise BrokerError(
                                "gpu_claim_inventory_unavailable",
                                "systemd cgroup process inventory is invalid",
                            )
                        process_identities = {}
                        for pid in process_pids:
                            identity = _read_stable_systemd_process_identity(
                                pid,
                                read_process_cgroup=read_process_cgroup,
                                read_process_start_ticks=read_process_start_ticks,
                            )
                            if not _systemd_cgroup_contains(
                                identity[1], control_group
                            ):
                                raise BrokerError(
                                    "gpu_claim_inventory_unavailable",
                                    "systemd cgroup process identity differs",
                                )
                            process_identities[pid] = identity
                    for pid, identity in compute_process_identities.items():
                        if not _systemd_cgroup_contains(identity[1], control_group):
                            continue
                        captured_identity = process_identities.get(pid)
                        if captured_identity is not None and captured_identity != identity:
                            raise BrokerError(
                                "gpu_claim_inventory_unavailable",
                                "NVIDIA PID identity conflicts with systemd inventory",
                            )
                        if captured_identity is None:
                            _verify_systemd_process_identity(
                                pid,
                                identity[0],
                                identity[1],
                                read_process_cgroup=read_process_cgroup,
                                read_process_start_ticks=read_process_start_ticks,
                            )
                            process_identities[pid] = identity
                    process_pids = frozenset(process_identities)
                except BrokerError:
                    raise
                except Exception as exc:
                    raise BrokerError(
                        "gpu_claim_inventory_unavailable",
                        "cannot enumerate systemd cgroup processes",
                    ) from exc
            else:
                process_pids = (
                    frozenset({main_pid}) if main_pid > 0 else frozenset()
                )
                process_identities: dict[int, tuple[int, str]] = {}

            environment_entries = _parse_systemd_environment(
                scalar_properties["Environment"],
                declaration="Environment",
                require_values=True,
            )
            pass_entries = _parse_systemd_environment(
                scalar_properties["PassEnvironment"],
                declaration="PassEnvironment",
                require_values=False,
            )
            unset_entries = _parse_systemd_environment(
                scalar_properties["UnsetEnvironment"],
                declaration="UnsetEnvironment",
                require_values=False,
            )
            pass_names = frozenset(entry.split("=", 1)[0] for entry in pass_entries)
            if pass_names.intersection(relevant_names):
                if manager_environment is None:
                    manager_environment = _query_systemd_manager_environment(
                        prefix,
                        scope,
                        run=run,
                    )
                    manager_environment_snapshots[scope] = dict(
                        manager_environment
                    )
            configured: dict[str, str] = {
                name: value
                for name in pass_names
                if manager_environment is not None
                and (value := manager_environment.get(name)) is not None
            }
            for entry in environment_entries:
                name, value = entry.split("=", 1)
                configured[name] = value
            for declaration in environment_files:
                if not declaration:
                    continue
                try:
                    file_tokens = shlex.split(declaration)
                except ValueError as exc:
                    raise BrokerError(
                        "gpu_claim_inventory_unavailable",
                        "systemd EnvironmentFiles declaration is invalid",
                    ) from exc
                if (
                    len(file_tokens) != 2
                    or file_tokens[1]
                    not in {"(ignore_errors=yes)", "(ignore_errors=no)"}
                ):
                    raise BrokerError(
                        "gpu_claim_inventory_unavailable",
                        "systemd EnvironmentFiles declaration is invalid",
                    )
                raw_path = file_tokens[0]
                prefixed_optional = raw_path.startswith("-/")
                if prefixed_optional:
                    raw_path = raw_path[1:]
                ignore_missing = file_tokens[1] == "(ignore_errors=yes)"
                if (
                    not raw_path.startswith("/")
                    or raw_path.startswith("//")
                    or (prefixed_optional and not ignore_missing)
                ):
                    raise BrokerError(
                        "gpu_claim_inventory_unavailable",
                        "systemd EnvironmentFiles declaration is invalid",
                    )
                environment_path = Path(raw_path)
                if ignore_missing:
                    try:
                        environment_path.lstat()
                    except FileNotFoundError:
                        environment_file_snapshots.append(
                            _capture_missing_systemd_environment_file(
                                environment_path
                            )
                        )
                        continue
                    except OSError as exc:
                        raise BrokerError(
                            "gpu_claim_inventory_unavailable",
                            "systemd EnvironmentFile is unsafe",
                        ) from exc
                file_environment, file_snapshot = (
                    _capture_systemd_environment_file(environment_path)
                )
                configured.update(file_environment)
                environment_file_snapshots.append(file_snapshot)
            for entry in unset_entries:
                if "=" in entry:
                    name, value = entry.split("=", 1)
                    if configured.get(name) == value:
                        configured.pop(name, None)
                else:
                    configured.pop(entry, None)

            active_uuids: set[str] = set()
            for uuid, pids in compute_processes.items():
                if uuid not in EXPECTED_GPU_UUIDS.values():
                    continue
                for pid in pids:
                    captured_identity = captured_process_identities.get(pid)
                    hinted_identity = compute_process_identities.get(pid)
                    if (
                        captured_identity is not None
                        and hinted_identity is not None
                        and captured_identity != hinted_identity
                    ):
                        raise BrokerError(
                            "gpu_claim_inventory_unavailable",
                            "NVIDIA PID identity conflicts with adjacent capture",
                        )
                    if captured_identity is None:
                        if hinted_identity is None:
                            process_identity = _read_stable_systemd_process_identity(
                                pid,
                                read_process_cgroup=read_process_cgroup,
                                read_process_start_ticks=read_process_start_ticks,
                            )
                        else:
                            _verify_systemd_process_identity(
                                pid,
                                hinted_identity[0],
                                hinted_identity[1],
                                read_process_cgroup=read_process_cgroup,
                                read_process_start_ticks=read_process_start_ticks,
                            )
                            process_identity = hinted_identity
                    else:
                        _verify_systemd_process_identity(
                            pid,
                            captured_identity[0],
                            captured_identity[1],
                            read_process_cgroup=read_process_cgroup,
                            read_process_start_ticks=read_process_start_ticks,
                        )
                        process_identity = captured_identity
                    process_cgroup = process_identity[1]
                    if control_group and _systemd_cgroup_contains(
                        process_cgroup,
                        control_group,
                    ):
                        if process_identities.get(pid) != process_identity:
                            raise BrokerError(
                                "gpu_claim_inventory_unavailable",
                                "NVIDIA PID identity changed during systemd inventory",
                            )
                        active_uuids.add(uuid)
                        break

            statically_relevant = bool(
                set(configured).intersection(relevant_names)
                or pass_names.intersection(relevant_names)
            )
            if scope == "system" and not statically_relevant and not active_uuids:
                # Root/systemd is the trusted host configuration boundary.
                # An unrelated root process is not ptrace-readable by UID1001;
                # global nvidia-smi still catches it the instant it owns a GPU.
                continue
            if control_group:
                observed_memberships[(scope, unit, control_group)] = dict(
                    process_identities
                )

            if main_pid > 0:
                if not control_group or main_pid not in process_identities:
                    raise BrokerError(
                        "gpu_claim_inventory_unavailable",
                        "systemd MainPID is outside its ControlGroup",
                    )
                expected_main_identity = process_identities[main_pid]
                _verify_systemd_process_identity(
                    main_pid,
                    expected_main_identity[0],
                    expected_main_identity[1],
                    read_process_cgroup=read_process_cgroup,
                    read_process_start_ticks=read_process_start_ticks,
                )

            live_environments: list[
                tuple[dict[str, str], int, int, str]
            ] = []
            for pid in sorted(process_pids):
                _ensure_admission_open(deadline, monotonic=monotonic)
                expected_identity = process_identities.get(pid)
                if expected_identity is None:
                    raise BrokerError(
                        "gpu_claim_inventory_unavailable",
                        f"systemd process identity is missing for PID {pid}",
                    )
                _verify_systemd_process_identity(
                    pid,
                    expected_identity[0],
                    expected_identity[1],
                    read_process_cgroup=read_process_cgroup,
                    read_process_start_ticks=read_process_start_ticks,
                )
                try:
                    process_uids = read_process_uids(pid)
                except BrokerError:
                    raise
                except Exception as exc:
                    raise BrokerError(
                        "gpu_claim_inventory_unavailable",
                        f"cannot read systemd process credentials for PID {pid}",
                    ) from exc
                if process_uids != (1001, 1001, 1001, 1001):
                    # A user service may contain a narrowly privileged helper
                    # (for example fusermount). Root remains part of the
                    # trusted host boundary; nvidia-smi independently catches
                    # any such process once it owns a GPU.
                    _verify_systemd_process_identity(
                        pid,
                        expected_identity[0],
                        expected_identity[1],
                        read_process_cgroup=read_process_cgroup,
                        read_process_start_ticks=read_process_start_ticks,
                    )
                    continue
                try:
                    environment = read_process_environment(pid)
                    if not isinstance(environment, dict) or any(
                        not isinstance(name, str)
                        or not name
                        or not isinstance(value, str)
                        for name, value in environment.items()
                    ):
                        raise ValueError("live environment is invalid")
                except BrokerError as exc:
                    if scope == "system" and (statically_relevant or active_uuids):
                        continue
                    raise BrokerError(
                        "gpu_claim_inventory_unavailable",
                        f"cannot safely read live environment for systemd PID {pid}",
                    ) from exc
                except Exception as exc:
                    if scope == "system" and (statically_relevant or active_uuids):
                        # Static declarations and exact NVIDIA PID attribution
                        # are sufficient to block/authorize this GPU without
                        # ptrace access to a cross-UID root process.
                        continue
                    raise BrokerError(
                        "gpu_claim_inventory_unavailable",
                        f"cannot safely read live environment for systemd PID {pid}",
                    ) from exc
                _verify_systemd_process_identity(
                    pid,
                    expected_identity[0],
                    expected_identity[1],
                    read_process_cgroup=read_process_cgroup,
                    read_process_start_ticks=read_process_start_ticks,
                )
                live_environments.append(
                    (
                        environment,
                        pid,
                        expected_identity[0],
                        expected_identity[1],
                    )
                )

            def declared_gpu_uuids(environment: dict[str, str]) -> frozenset[str]:
                declared: set[str] = set()
                for name in relevant_names:
                    value = environment.get(name)
                    if value is None:
                        continue
                    normalized = value.strip()
                    if normalized.lower() in {"", "none", "void"}:
                        continue
                    if normalized.lower() == "all":
                        declared.update(EXPECTED_GPU_UUIDS.values())
                        continue
                    try:
                        declared.update(
                            _resolve_gpu_claim_tokens(normalized.split(","))
                        )
                    except ValueError as exc:
                        raise BrokerError(
                            "gpu_claim_inventory_unavailable",
                            "systemd GPU declaration is outside governance",
                        ) from exc
                return frozenset(declared)

            static_gpu_uuids = declared_gpu_uuids(configured)
            live_gpu_declarers = tuple(
                SystemdGpuDeclarer(
                    pid=pid,
                    process_start_ticks=start_ticks,
                    process_cgroup=process_cgroup,
                    gpu_uuids=gpu_uuids,
                )
                for environment, pid, start_ticks, process_cgroup in live_environments
                if (gpu_uuids := declared_gpu_uuids(environment))
            )
            gpu_uuids = set(active_uuids)
            gpu_uuids.update(static_gpu_uuids)
            for declarer in live_gpu_declarers:
                gpu_uuids.update(declarer.gpu_uuids)
            for pid, (start_ticks, process_cgroup) in sorted(
                process_identities.items()
            ):
                _verify_systemd_process_identity(
                    pid,
                    start_ticks,
                    process_cgroup,
                    read_process_cgroup=read_process_cgroup,
                    read_process_start_ticks=read_process_start_ticks,
                )
                verified_process_identities.add(
                    (pid, start_ticks, process_cgroup)
                )
            if gpu_uuids:
                claims.append(
                    SystemdGpuClaim(
                        scope=scope,
                        unit=unit,
                        main_pid=main_pid,
                        control_group=control_group,
                        process_pids=process_pids,
                        gpu_uuids=frozenset(gpu_uuids),
                        static_gpu_uuids=static_gpu_uuids,
                        active_gpu_uuids=frozenset(active_uuids),
                        live_gpu_declarers=live_gpu_declarers,
                    )
                )
    # Bind the list/show control-plane identity before accepting the process
    # evidence. InvocationID closes same-name unit restarts while ActiveState
    # and SubState keep activating/reloading services inside the audit.
    for scope in ("user", "system"):
        _ensure_admission_open(deadline, monotonic=monotonic)
        current_authorities = _query_systemd_unit_authorities(scope, run=run)
        if current_authorities != authority_snapshots[scope]:
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                f"{scope} systemd unit authority changed during audit",
            )
        if scope in manager_environment_snapshots:
            prefix = (
                ["systemctl", "--user"] if scope == "user" else ["systemctl"]
            )
            if _query_systemd_manager_environment(
                prefix,
                scope,
                run=run,
            ) != manager_environment_snapshots[scope]:
                raise BrokerError(
                    "gpu_claim_inventory_unavailable",
                    f"{scope} systemd manager environment changed during audit",
                )

    final_process_cgroup_snapshot = (
        _snapshot_systemd_process_cgroups(
            read_process_cgroup=read_process_cgroup,
            read_process_start_ticks=read_process_start_ticks,
            deadline=deadline,
            monotonic=monotonic,
        )
        if process_cgroup_snapshot is not None
        else None
    )
    for (scope, unit, control_group), expected_identities in sorted(
        observed_memberships.items()
    ):
        _ensure_admission_open(deadline, monotonic=monotonic)
        if final_process_cgroup_snapshot is not None:
            current_identities = {
                pid: (start_ticks, process_cgroup)
                for pid, start_ticks, process_cgroup in final_process_cgroup_snapshot
                if _systemd_cgroup_contains(process_cgroup, control_group)
            }
        else:
            try:
                current_pids = read_control_group_processes(control_group)
            except BrokerError:
                raise
            except Exception as exc:
                raise BrokerError(
                    "gpu_claim_inventory_unavailable",
                    "cannot enumerate systemd cgroup processes",
                ) from exc
            if (
                not isinstance(current_pids, frozenset)
                or any(
                    not isinstance(pid, int)
                    or isinstance(pid, bool)
                    or pid <= 0
                    for pid in current_pids
                )
            ):
                raise BrokerError(
                    "gpu_claim_inventory_unavailable",
                    "systemd cgroup process inventory is invalid",
                )
            current_identities = {
                pid: _read_stable_systemd_process_identity(
                    pid,
                    read_process_cgroup=read_process_cgroup,
                    read_process_start_ticks=read_process_start_ticks,
                )
                for pid in current_pids
            }
            if any(
                not _systemd_cgroup_contains(identity[1], control_group)
                for identity in current_identities.values()
            ):
                raise BrokerError(
                    "gpu_claim_inventory_unavailable",
                    "systemd cgroup process identity differs",
                )
        if current_identities != expected_identities:
            raise SystemdMembershipChanged(
                scope,
                unit,
                control_group,
                tuple(
                    (pid, start_ticks, process_cgroup)
                    for pid, (start_ticks, process_cgroup) in sorted(
                        expected_identities.items()
                    )
                ),
                tuple(
                    (pid, start_ticks, process_cgroup)
                    for pid, (start_ticks, process_cgroup) in sorted(
                        current_identities.items()
                    )
                ),
            )

    # User inventory runs before system inventory. Revalidate every enumerated
    # identity after both control-plane and membership CAS checks.
    for pid, start_ticks, process_cgroup in sorted(verified_process_identities):
        _ensure_admission_open(deadline, monotonic=monotonic)
        _verify_systemd_process_identity(
            pid,
            start_ticks,
            process_cgroup,
            read_process_cgroup=read_process_cgroup,
            read_process_start_ticks=read_process_start_ticks,
        )
    # EnvironmentFiles are mutable authorities independent of systemd's
    # EnvironmentFiles= path string. Re-open them only after unit, manager,
    # membership and process-identity CAS checks so an activating MainPID=0
    # service cannot change its future GPU visibility inside that window.
    for expected_snapshot in environment_file_snapshots:
        _ensure_admission_open(deadline, monotonic=monotonic)
        if isinstance(
            expected_snapshot,
            _SystemdMissingEnvironmentFileSnapshot,
        ):
            current_missing_snapshot = (
                _capture_missing_systemd_environment_file(
                    Path(expected_snapshot.declared_path)
                )
            )
            if current_missing_snapshot != expected_snapshot:
                raise BrokerError(
                    "gpu_claim_inventory_unavailable",
                    "systemd optional EnvironmentFile identity changed during audit",
                )
            continue
        _environment, current_snapshot = _capture_systemd_environment_file(
            Path(expected_snapshot.declared_path)
        )
        if current_snapshot != expected_snapshot:
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "systemd EnvironmentFile identity changed during audit",
            )
    _ensure_admission_open(deadline, monotonic=monotonic)
    return tuple(sorted(claims, key=lambda claim: (claim.scope, claim.unit)))


def validate_policy_document(path: Path) -> None:
    try:
        descriptor_match = re.fullmatch(
            rf"/proc/{os.getpid()}/fd/([1-9][0-9]*)",
            str(path),
        )
        if descriptor_match is not None:
            descriptor = int(descriptor_match.group(1))
            if descriptor <= 2:
                raise OSError("policy descriptor is reserved")
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_gid != os.getegid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > 64 * 1024
            ):
                raise OSError("policy descriptor metadata is unsafe")
            raw = os.pread(descriptor, metadata.st_size + 1, 0)
            if len(raw) != metadata.st_size:
                raise OSError("policy descriptor changed during read")
            payload = json.loads(raw.decode("utf-8"))
        else:
            if path.is_symlink() or not path.is_file():
                raise OSError("policy path is missing or unsafe")
            payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BrokerError("invalid_policy", "GPU policy document is unreadable") from exc
    expected = {
        "schema_version": 1,
        "gpu_total_budget_mib": GPU_TOTAL_BUDGET_MIB,
        "component_budgets_mib": COMPONENT_BUDGETS_MIB,
        "component_thread_percent": COMPONENT_THREAD_PERCENT,
        "gpu_uuids": {str(index): uuid for index, uuid in EXPECTED_GPU_UUIDS.items()},
        "device_policy": {
            f"{environment}.{component}": list(indices)
            for (environment, component), indices in DEVICE_POLICY.items()
        },
    }
    if payload != expected:
        raise BrokerError(
            "invalid_policy",
            "GPU policy document differs from the compiled fail-closed policy",
        )


def query_compute_processes(
    *,
    run=subprocess.run,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, frozenset[int]]:
    run = _deadline_bounded_run(
        run,
        deadline=deadline,
        monotonic=monotonic,
    )
    try:
        completed = run(
            [
                "nvidia-smi",
                "--query-compute-apps=gpu_uuid,pid",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BrokerError(
            "gpu_process_inventory_unavailable",
            "nvidia-smi compute process inventory failed",
        ) from exc
    processes: dict[str, set[int]] = {}
    for line in completed.stdout.splitlines():
        _ensure_admission_open(deadline, monotonic=monotonic)
        if not line.strip():
            continue
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 2:
            raise BrokerError(
                "gpu_process_inventory_unavailable",
                "invalid nvidia-smi compute process inventory",
            )
        try:
            pid = int(fields[1])
        except ValueError as exc:
            raise BrokerError(
                "gpu_process_inventory_unavailable", "invalid CUDA process PID"
            ) from exc
        processes.setdefault(fields[0], set()).add(pid)
    return {uuid: frozenset(pids) for uuid, pids in processes.items()}


@dataclass(frozen=True, slots=True)
class _ExternalInventorySnapshot:
    processes: dict[str, frozenset[int]]
    docker_claims: tuple[DockerGpuClaim, ...]
    systemd_claims: tuple[SystemdGpuClaim, ...]

    def target_fingerprint(
        self,
        uuid: str,
    ) -> tuple[
        frozenset[int],
        tuple[DockerGpuClaim, ...],
        tuple[SystemdGpuClaim, ...],
    ]:
        return (
            self.processes.get(uuid, frozenset()),
            tuple(
                claim
                for claim in self.docker_claims
                if uuid in claim.gpu_uuids
            ),
            tuple(
                claim
                for claim in self.systemd_claims
                if uuid in claim.gpu_uuids
            ),
        )


class _ExternalGpuAdmission:
    """One request-local inventory shared by its ordered GPU candidates."""

    def __init__(
        self,
        guard: "ExternalGpuGuard",
        *,
        leases: tuple[Lease, ...],
        owner: OwnerIdentity,
        component: str,
        environment: str,
        client_id: str | None,
        parent_lease_id: str | None,
    ) -> None:
        self.guard = guard
        self.leases = leases
        self.owner = owner
        self.component = component
        self.environment = environment
        self.client_id = client_id
        self.parent_lease_id = parent_lease_id
        self.deadline = (
            guard._monotonic() + guard._admission_timeout_seconds
        )
        self._initial: _ExternalInventorySnapshot | None = None
        self._initial_mps_key: (
            tuple[int, str, MpsAuthoritySnapshot] | None
        ) = None
        self._inventory_failed = False

    def initial_inventory(
        self,
        index: int,
        uuid: str,
        mps_authority: MpsAuthoritySnapshot,
    ) -> _ExternalInventorySnapshot:
        if self._inventory_failed:
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "initial external GPU inventory is unavailable",
            )
        # Only descriptor/composite authority binds GPU declarer identities to
        # this inventory.  Keep the legacy production callback's established
        # cross-candidate sharing behavior unchanged.
        key = (
            (index, uuid, mps_authority)
            if self.guard._mps_authority_query is not None
            else None
        )
        if self._initial is None:
            try:
                self._initial = self.guard._inventories(
                    deadline=self.deadline
                )
                self._initial_mps_key = key
            except Exception:
                self._inventory_failed = True
                raise
        if key is None or self._initial_mps_key == key:
            return self._initial
        # A shared request inventory collected for another candidate was not
        # enclosed by this target's MPS authority.  Re-sample instead of
        # retroactively authorizing a same-PID server on the cached snapshot.
        return self.guard._inventories(deadline=self.deadline)

    def __call__(self, index: int, uuid: str) -> bool:
        return self.guard._candidate_busy(self, index, uuid)

    def finalize(self, _index: int, _uuid: str) -> None:
        """Fence lease insertion to the same request-local deadline."""

        _ensure_admission_open(
            self.deadline,
            monotonic=self.guard._monotonic,
        )


def exact_dft_residency_scope_authority(
    lease: Lease,
    *,
    index: int,
    uuid: str,
) -> tuple[int, int, str] | None:
    """Return the exact active DFT residency workload identity, if any.

    This is deliberately narrower than a generic managed lease check.  The
    development DFT executor is the only workload whose compiler descendants
    legitimately make the UID user-manager claim dynamic while a session is
    running.
    """

    broker_uid = 1001
    if (
        os.geteuid() != broker_uid
        or lease.kind != "residency"
        or lease.placement != "preferred"
        or lease.preferred is not True
        or lease.component != "dft"
        or lease.environment != "dev"
        or not isinstance(lease.client_id, str)
        or not lease.client_id
        or lease.gpu_index != index
        or lease.gpu_uuid != uuid
        or lease.parent_lease_id is not None
        or lease.status != "active"
        or lease.mps_termination_status != "none"
    ):
        return None
    workload_pid = lease.workload_pid
    workload_start_ticks = lease.workload_process_start_ticks
    workload_process_group_id = lease.workload_process_group_id
    if (
        isinstance(workload_pid, bool)
        or not isinstance(workload_pid, int)
        or isinstance(workload_start_ticks, bool)
        or not isinstance(workload_start_ticks, int)
        or isinstance(workload_process_group_id, bool)
        or not isinstance(workload_process_group_id, int)
        or workload_pid <= 0
        or workload_start_ticks <= 0
        or workload_process_group_id <= 0
        or workload_process_group_id != workload_pid
    ):
        return None
    try:
        expected_control_group = scope_control_group(
            lease.lease_id,
            uid=broker_uid,
        )
    except (TypeError, ValueError):
        return None
    if lease.workload_cgroup != f"0::{expected_control_group}":
        return None
    return workload_pid, workload_start_ticks, expected_control_group


def process_is_exact_dft_residency_descendant(
    pid: int,
    lease: Lease,
    *,
    index: int,
    uuid: str,
) -> bool:
    """Bind one live PID to the exact DFT residency root without trusting a list.

    This helper is intentionally independent of the systemd inventory.  It is
    used only by the development controller while that inventory is changing
    during DFT compiler warmup.  Both the root and the observed process are
    re-read so PID reuse, credential changes, cgroup moves and ancestry races
    fail closed.
    """

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    authority = exact_dft_residency_scope_authority(
        lease,
        index=index,
        uuid=uuid,
    )
    if authority is None:
        return False
    workload_pid, workload_start_ticks, expected_control_group = authority

    def root_identity() -> tuple[int, tuple[int, int, int, int], int, str]:
        return (
            read_process_start_ticks(workload_pid),
            _read_process_uids(workload_pid),
            os.getpgid(workload_pid),
            _read_unified_process_cgroup(workload_pid),
        )

    def process_identity() -> tuple[int, tuple[int, int, int, int], int, str]:
        return (
            read_process_start_ticks(pid),
            _read_process_uids(pid),
            os.getpgid(pid),
            _read_unified_process_cgroup(pid),
        )

    expected_root = (
        workload_start_ticks,
        (1001, 1001, 1001, 1001),
        workload_pid,
        expected_control_group,
    )
    try:
        root_before = root_identity()
        process_before = process_identity()
        if (
            root_before != expected_root
            or process_before[1] != (1001, 1001, 1001, 1001)
            or process_before[2] != workload_pid
            or process_before[3] != expected_control_group
            or not _pid_is_or_descends_from(pid, workload_pid)
        ):
            return False
        process_after = process_identity()
        descends_after = _pid_is_or_descends_from(pid, workload_pid)
        root_after = root_identity()
    except (BrokerError, OSError, TypeError, ValueError):
        return False
    return (
        process_before == process_after
        and descends_after
        and root_before == root_after == expected_root
    )


def exact_md_execution_scope_authority(
    lease: Lease,
    *,
    index: int,
    uuid: str,
) -> tuple[int, int, str] | None:
    """Return one exact independent GPU1 MD execution workload identity."""

    broker_uid = 1001
    if (
        os.geteuid() != broker_uid
        or index != 1
        or uuid != EXPECTED_GPU_UUIDS[1]
        or lease.kind != "execution"
        or lease.placement != "any"
        or lease.preferred is not True
        or lease.component != "md"
        or lease.environment != "dev"
        or not isinstance(lease.client_id, str)
        or not lease.client_id
        or lease.gpu_index != index
        or lease.gpu_uuid != uuid
        or lease.memory_mib != COMPONENT_BUDGETS_MIB["md"]
        or lease.thread_percent != COMPONENT_THREAD_PERCENT["md"]
        or lease.parent_lease_id is not None
        or lease.status != "active"
        or lease.mps_termination_status != "none"
    ):
        return None
    workload_pid = lease.workload_pid
    workload_start_ticks = lease.workload_process_start_ticks
    workload_process_group_id = lease.workload_process_group_id
    if (
        isinstance(workload_pid, bool)
        or not isinstance(workload_pid, int)
        or isinstance(workload_start_ticks, bool)
        or not isinstance(workload_start_ticks, int)
        or isinstance(workload_process_group_id, bool)
        or not isinstance(workload_process_group_id, int)
        or workload_pid <= 0
        or workload_start_ticks <= 0
        or workload_process_group_id != workload_pid
    ):
        return None
    try:
        expected_control_group = scope_control_group(
            lease.lease_id,
            uid=broker_uid,
        )
    except (TypeError, ValueError):
        return None
    if lease.workload_cgroup != f"0::{expected_control_group}":
        return None
    return workload_pid, workload_start_ticks, expected_control_group


def _exact_dev_gpu1_host_scope_authority(
    lease: Lease,
    *,
    index: int,
    uuid: str,
) -> tuple[str, int, int, str] | None:
    """Limit broad user-manager authority to DFT residency and MD jobs."""

    if (
        index != 1
        or uuid != EXPECTED_GPU_UUIDS[1]
        or not isinstance(lease, Lease)
    ):
        return None
    if lease.component == "dft":
        if (
            lease.memory_mib != COMPONENT_BUDGETS_MIB["dft"]
            or lease.thread_percent != COMPONENT_THREAD_PERCENT["dft"]
        ):
            return None
        authority = exact_dft_residency_scope_authority(
            lease,
            index=index,
            uuid=uuid,
        )
        component = "dft"
    elif lease.component == "md":
        authority = exact_md_execution_scope_authority(
            lease,
            index=index,
            uuid=uuid,
        )
        component = "md"
    else:
        return None
    if authority is None:
        return None
    return component, *authority


def _declarer_is_exact_host_scope_descendant(
    declarer: SystemdGpuDeclarer,
    authority: tuple[str, int, int, str],
    *,
    uuid: str,
) -> bool:
    """Double-read one GPU declarer against one Broker-fenced scope root."""

    _component, workload_pid, workload_start_ticks, control_group = authority
    if (
        not isinstance(declarer, SystemdGpuDeclarer)
        or isinstance(declarer.pid, bool)
        or not isinstance(declarer.pid, int)
        or declarer.pid <= 0
        or isinstance(declarer.process_start_ticks, bool)
        or not isinstance(declarer.process_start_ticks, int)
        or declarer.process_start_ticks <= 0
        or declarer.process_cgroup != control_group
        or declarer.gpu_uuids != frozenset({uuid})
    ):
        return False

    expected_uids = (1001, 1001, 1001, 1001)

    def identity(pid: int) -> tuple[int, tuple[int, int, int, int], int, str]:
        return (
            read_process_start_ticks(pid),
            _read_process_uids(pid),
            os.getpgid(pid),
            _read_unified_process_cgroup(pid),
        )

    expected_root = (
        workload_start_ticks,
        expected_uids,
        workload_pid,
        control_group,
    )
    expected_process = (
        declarer.process_start_ticks,
        expected_uids,
        workload_pid,
        control_group,
    )
    try:
        root_before = identity(workload_pid)
        process_before = (
            root_before
            if declarer.pid == workload_pid
            else identity(declarer.pid)
        )
        descends_before = _pid_is_or_descends_from(
            declarer.pid,
            workload_pid,
        )
        process_after = identity(declarer.pid)
        descends_after = _pid_is_or_descends_from(
            declarer.pid,
            workload_pid,
        )
        root_after = (
            process_after
            if declarer.pid == workload_pid
            else identity(workload_pid)
        )
    except (BrokerError, OSError, TypeError, ValueError):
        return False
    return (
        root_before == root_after == expected_root
        and process_before == process_after == expected_process
        and descends_before
        and descends_after
    )


def _lease_owner_identity_is_stable(lease: Lease) -> bool:
    """Double-read the active lease owner and bind it to this host boot."""

    if (
        isinstance(lease.owner_pid, bool)
        or not isinstance(lease.owner_pid, int)
        or lease.owner_pid <= 0
        or isinstance(lease.owner_process_start_ticks, bool)
        or not isinstance(lease.owner_process_start_ticks, int)
        or lease.owner_process_start_ticks <= 0
        or not isinstance(lease.owner_boot_id, str)
        or not lease.owner_boot_id
    ):
        return False
    expected = (
        lease.owner_process_start_ticks,
        (1001, 1001, 1001, 1001),
    )
    try:
        if lease.owner_boot_id != read_boot_id():
            return False
        before = (
            read_process_start_ticks(lease.owner_pid),
            _read_process_uids(lease.owner_pid),
        )
        after = (
            read_process_start_ticks(lease.owner_pid),
            _read_process_uids(lease.owner_pid),
        )
    except (BrokerError, OSError, TypeError, ValueError):
        return False
    return before == after == expected


def _parented_dft_execution_is_exact_inheritance(
    lease: Lease,
    dft_residencies: tuple[Lease, ...],
    *,
    index: int,
    uuid: str,
) -> bool:
    """Ensure a logical resident DFT execution never contributes a new root."""

    parents = tuple(
        parent
        for parent in dft_residencies
        if parent.lease_id == lease.parent_lease_id
    )
    return (
        len(parents) == 1
        and lease.kind == "execution"
        and lease.placement == "preferred"
        and lease.preferred is True
        and lease.component == "dft"
        and lease.environment == "dev"
        and lease.client_id == parents[0].client_id
        and lease.gpu_index == index
        and lease.gpu_uuid == uuid
        and lease.memory_mib == COMPONENT_BUDGETS_MIB["dft"]
        and lease.thread_percent == COMPONENT_THREAD_PERCENT["dft"]
        and lease.owner_pid == parents[0].owner_pid
        and lease.owner_process_start_ticks
        == parents[0].owner_process_start_ticks
        and lease.owner_boot_id == parents[0].owner_boot_id
        and lease.status == "active"
        and lease.mps_termination_status == "none"
        and (
            lease.workload_pid,
            lease.workload_process_start_ticks,
            lease.workload_process_group_id,
            lease.workload_cgroup,
        )
        == (
            parents[0].workload_pid,
            parents[0].workload_process_start_ticks,
            parents[0].workload_process_group_id,
            parents[0].workload_cgroup,
        )
    )


def claim_is_exact_dev_gpu1_host_workloads_scope(
    claim: SystemdGpuClaim,
    *,
    index: int,
    uuid: str,
    leases: tuple[Lease, ...],
    authorized_mps_declarers: frozenset[SystemdGpuDeclarer] = frozenset(),
    authorized_mps_server_pids: frozenset[int] = frozenset(),
) -> bool:
    """Prove the complete GPU part of the broad UID 1001 manager claim.

    CPU-only manager members are irrelevant.  Every live GPU declarer must be
    either an exact descriptor-authorized MPS process or belong uniquely to
    one active, independent DFT-residency/MD-execution transient scope.  Every
    eligible Broker root must itself be present, preventing a partial claim
    from authorizing a stale or unrelated child.
    """

    if (
        index != 1
        or uuid != EXPECTED_GPU_UUIDS[1]
        or not isinstance(claim, SystemdGpuClaim)
        or claim.scope != "system"
        or claim.unit != "user@1001.service"
        or claim.control_group != user_manager_control_group(1001)
        or claim.gpu_uuids != frozenset({uuid})
        or bool(claim.static_gpu_uuids)
        or not isinstance(claim.active_gpu_uuids, frozenset)
        or not claim.active_gpu_uuids <= frozenset({uuid})
        or not isinstance(claim.live_gpu_declarers, tuple)
        or not isinstance(claim.process_pids, frozenset)
        or not isinstance(leases, tuple)
        or not isinstance(authorized_mps_declarers, frozenset)
        or not isinstance(authorized_mps_server_pids, frozenset)
        or any(
            not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            for pid in authorized_mps_server_pids
        )
    ):
        return False
    target_declarers = claim.live_gpu_declarers
    if (
        not target_declarers
        or any(
            not isinstance(declarer, SystemdGpuDeclarer)
            or declarer.gpu_uuids != frozenset({uuid})
            or declarer.pid not in claim.process_pids
            or not _systemd_cgroup_contains(
                declarer.process_cgroup,
                claim.control_group,
            )
            for declarer in target_declarers
        )
        or len({declarer.pid for declarer in target_declarers})
        != len(target_declarers)
        or any(
            not isinstance(declarer, SystemdGpuDeclarer)
            or declarer.gpu_uuids != frozenset({uuid})
            for declarer in authorized_mps_declarers
        )
        or len({declarer.pid for declarer in authorized_mps_declarers})
        != len(authorized_mps_declarers)
        or not authorized_mps_server_pids.issubset(
            {declarer.pid for declarer in authorized_mps_declarers}
        )
    ):
        return False
    target_set = frozenset(target_declarers)
    # The development controller may be launched from a login ``session-*.scope``
    # while DFT/MD executors are placed below the user manager by
    # ``systemd-run --user``.  Those are sibling cgroups.  Descriptor-authorized
    # MPS processes outside this claim are validated by MpsRuntimeGuard and must
    # not be required to appear in the user-manager claim.  Conversely, every
    # authorized MPS declarer whose cgroup is inside this claim must be present;
    # otherwise the systemd/environment snapshot is incomplete and fails closed.
    claim_mps_declarers = frozenset(
        declarer
        for declarer in authorized_mps_declarers
        if _systemd_cgroup_contains(
            declarer.process_cgroup,
            claim.control_group,
        )
    )
    # Under MPS, NVML attributes compute activity to the MPS server.  When the
    # descriptor-owned control/server processes inherit the controller's direct
    # login scope beside ``user@1001.service``, the exact user-manager claim
    # therefore has no active GPU UUID despite containing the declared DFT/MD
    # clients.  The empty set is safe only for that proven sibling layout; all
    # other claims retain the exact active-GPU requirement.
    active_gpu_attribution_is_exact = claim.active_gpu_uuids == frozenset({uuid})
    sibling_mps_control_declarers = frozenset(
        declarer
        for declarer in authorized_mps_declarers
        if declarer.pid not in authorized_mps_server_pids
    )
    sibling_mps_cgroups = frozenset(
        declarer.process_cgroup for declarer in authorized_mps_declarers
    )
    empty_active_gpu_attribution_is_exact_sibling_mps = (
        not claim.active_gpu_uuids
        and len(authorized_mps_server_pids) == 1
        and len(authorized_mps_declarers) == 2
        and len(sibling_mps_control_declarers) == 1
        and len(sibling_mps_cgroups) == 1
        and not claim_mps_declarers
        and all(
            _systemd_cgroup_is_user_manager_sibling(
                declarer.process_cgroup,
                claim.control_group,
            )
            for declarer in authorized_mps_declarers
        )
    )
    if (
        not claim_mps_declarers <= target_set
        or not (
            active_gpu_attribution_is_exact
            or empty_active_gpu_attribution_is_exact_sibling_mps
        )
    ):
        return False

    authority_entries: list[tuple[Lease, tuple[str, int, int, str]]] = []
    for lease in leases:
        authority = _exact_dev_gpu1_host_scope_authority(
            lease,
            index=index,
            uuid=uuid,
        )
        if authority is not None and _lease_owner_identity_is_stable(lease):
            authority_entries.append((lease, authority))
    authorities = tuple(authority for _lease, authority in authority_entries)
    dft_residencies = tuple(
        lease
        for lease, authority in authority_entries
        if authority[0] == "dft"
    )
    if (
        len(dft_residencies) != 1
        or len({(authority[1], authority[3]) for authority in authorities})
        != len(authorities)
        or any(
            lease.component == "dft"
            and lease.environment == "dev"
            and lease.gpu_index == index
            and lease.gpu_uuid == uuid
            and lease.kind == "execution"
            and lease.parent_lease_id is not None
            and lease.status == "active"
            and not _parented_dft_execution_is_exact_inheritance(
                lease,
                dft_residencies,
                index=index,
                uuid=uuid,
            )
            for lease in leases
        )
        or any(
            _declarer_is_exact_host_scope_descendant(
                declarer,
                authority,
                uuid=uuid,
            )
            for declarer in authorized_mps_declarers
            for authority in authorities
        )
    ):
        return False

    workload_declarers = tuple(
        declarer
        for declarer in target_declarers
        if declarer not in authorized_mps_declarers
    )
    matched_authorities: set[int] = set()
    for declarer in workload_declarers:
        matches = tuple(
            position
            for position, authority in enumerate(authorities)
            if _declarer_is_exact_host_scope_descendant(
                declarer,
                authority,
                uuid=uuid,
            )
        )
        if len(matches) != 1:
            return False
        matched_authorities.add(matches[0])
    return (
        len(matched_authorities) == len(authorities)
        and all(
            any(
                declarer.pid == authority[1]
                and declarer.process_start_ticks == authority[2]
                and declarer.process_cgroup == authority[3]
                for declarer in workload_declarers
            )
            for authority in authorities
        )
    )


def claim_is_exact_dft_residency_scope(
    claim: SystemdGpuClaim,
    *,
    index: int,
    uuid: str,
    lease: Lease,
    authorized_mps_declarers: frozenset[SystemdGpuDeclarer] = frozenset(),
) -> bool:
    """Recognize one exact dev DFT residency on any policy GPU.

    This compatibility path intentionally retains the pre-session behavior
    for legacy/non-descriptor admission, including dev GPU3.  The stricter
    composite GPU1 partition is selected separately by descriptor authority.
    """

    authority = exact_dft_residency_scope_authority(
        lease,
        index=index,
        uuid=uuid,
    )
    if authority is None:
        return False
    workload_pid, workload_start_ticks, expected_control_group = authority
    target_declarers = tuple(
        declarer
        for declarer in claim.live_gpu_declarers
        if uuid in declarer.gpu_uuids
    )
    expected_workload_declarer = SystemdGpuDeclarer(
        pid=workload_pid,
        process_start_ticks=workload_start_ticks,
        process_cgroup=expected_control_group,
        gpu_uuids=frozenset({uuid}),
    )
    try:
        target_declarer_set = frozenset(target_declarers)
        declarers_are_exact = (
            isinstance(authorized_mps_declarers, frozenset)
            and all(
                isinstance(declarer, SystemdGpuDeclarer)
                and declarer.gpu_uuids == frozenset({uuid})
                for declarer in authorized_mps_declarers
            )
            and bool(target_declarers)
            and len({declarer.pid for declarer in target_declarers})
            == len(target_declarers)
            and expected_workload_declarer in target_declarers
            and expected_workload_declarer not in authorized_mps_declarers
            and authorized_mps_declarers <= target_declarer_set
            and len(
                {declarer.pid for declarer in authorized_mps_declarers}
            )
            == len(authorized_mps_declarers)
            and all(
                declarer.pid in claim.process_pids
                and declarer.gpu_uuids == frozenset({uuid})
                and (
                    declarer in authorized_mps_declarers
                    or (
                        declarer.process_cgroup == expected_control_group
                        and _pid_is_or_descends_from(
                            declarer.pid,
                            workload_pid,
                        )
                        and read_process_start_ticks(declarer.pid)
                        == declarer.process_start_ticks
                        and _read_unified_process_cgroup(declarer.pid)
                        == expected_control_group
                    )
                )
                for declarer in target_declarers
            )
        )
    except (BrokerError, OSError, TypeError, ValueError):
        return False
    return (
        claim.scope == "system"
        and claim.unit == "user@1001.service"
        and claim.control_group == user_manager_control_group(1001)
        and claim.gpu_uuids == frozenset({uuid})
        and not claim.static_gpu_uuids
        and claim.active_gpu_uuids == frozenset({uuid})
        and declarers_are_exact
    )


def exact_dev_gpu1_backend_docker_workload_pids(
    leases: tuple[Lease, ...],
    docker_claims: tuple[DockerGpuClaim, ...],
) -> frozenset[int]:
    """Return the sole Backend root only after exact lease/container binding."""

    candidates = tuple(
        lease
        for lease in leases
        if isinstance(lease, Lease)
        and lease.kind == "residency"
        and lease.placement == "preferred"
        and lease.preferred is True
        and lease.component == "backend"
        and lease.environment == "dev"
        and lease.client_id == "backend-dev"
        and lease.request_id == "backend:dev:residency"
        and lease.gpu_index == 1
        and lease.gpu_uuid == EXPECTED_GPU_UUIDS[1]
        and lease.memory_mib == COMPONENT_BUDGETS_MIB["backend"]
        and lease.thread_percent == COMPONENT_THREAD_PERCENT["backend"]
        and lease.parent_lease_id is None
        and lease.status == "active"
        and lease.mps_termination_status == "none"
    )
    claims = tuple(
        claim
        for claim in docker_claims
        if isinstance(claim, DockerGpuClaim)
        and claim.registration_id == "backend-dev"
        and claim.component == "backend"
        and claim.environment == "dev"
        and claim.compose_project == "nexpoly_dev"
        and claim.compose_service == "backend"
        and claim.gpu_uuids == frozenset({EXPECTED_GPU_UUIDS[1]})
    )
    if len(candidates) != 1 or len(claims) != 1:
        return frozenset()
    lease = candidates[0]
    claim = claims[0]
    try:
        current_boot_id = read_boot_id()
    except (BrokerError, OSError, TypeError, ValueError):
        return frozenset()
    workload_pid = lease.workload_pid
    workload_start_ticks = lease.workload_process_start_ticks
    workload_process_group_id = lease.workload_process_group_id
    if (
        isinstance(workload_pid, bool)
        or not isinstance(workload_pid, int)
        or isinstance(workload_start_ticks, bool)
        or not isinstance(workload_start_ticks, int)
        or isinstance(workload_process_group_id, bool)
        or not isinstance(workload_process_group_id, int)
        or workload_pid <= 0
        or workload_start_ticks <= 0
        or workload_process_group_id <= 0
        or lease.owner_pid != workload_pid
        or lease.owner_process_start_ticks != workload_start_ticks
        or lease.owner_boot_id != current_boot_id
        or isinstance(claim.init_pid, bool)
        or not isinstance(claim.init_pid, int)
        or claim.init_pid <= 0
        or not isinstance(lease.workload_cgroup, str)
        or not lease.workload_cgroup.startswith("0::/")
        or "\n" in lease.workload_cgroup
    ):
        return frozenset()
    expected_cgroup = lease.workload_cgroup[3:]
    expected_workload = (
        workload_start_ticks,
        (1001, 1001, 1001, 1001),
        workload_process_group_id,
        expected_cgroup,
    )

    def identity(pid: int) -> tuple[int, tuple[int, int, int, int], int, str]:
        return (
            read_process_start_ticks(pid),
            _read_process_uids(pid),
            os.getpgid(pid),
            _read_unified_process_cgroup(pid),
        )

    try:
        workload_before = identity(workload_pid)
        init_before = (
            workload_before
            if claim.init_pid == workload_pid
            else identity(claim.init_pid)
        )
        descends_before = _pid_is_or_descends_from(
            workload_pid,
            claim.init_pid,
        )
        workload_after = identity(workload_pid)
        init_after = (
            workload_after
            if claim.init_pid == workload_pid
            else identity(claim.init_pid)
        )
        descends_after = _pid_is_or_descends_from(
            workload_pid,
            claim.init_pid,
        )
    except (BrokerError, OSError, TypeError, ValueError):
        return frozenset()
    if (
        workload_before != workload_after
        or workload_before != expected_workload
        or init_before != init_after
        or init_before[1] != (1001, 1001, 1001, 1001)
        or init_before[3] != expected_cgroup
        or not descends_before
        or not descends_after
    ):
        return frozenset()
    return frozenset({workload_pid})


class ExternalGpuGuard:
    """Blocks static claims and live clients using request-local CAS audits."""

    def __init__(
        self,
        policy: ExternalReservationPolicy,
        *,
        process_query=query_compute_processes,
        docker_claim_query=query_docker_gpu_claims,
        systemd_claim_query=query_systemd_gpu_claims,
        unmanaged_mps_client_query=None,
        authorized_mps_server_pids=None,
        mps_authority_query=None,
        allow_descriptor_mps_authority: bool = False,
        cache_seconds: float = 0.0,
        admission_timeout_seconds: float = (
            DEFAULT_EXTERNAL_ADMISSION_TIMEOUT_SECONDS
        ),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(admission_timeout_seconds, bool)
            or not isinstance(admission_timeout_seconds, (int, float))
            or not 0
            < float(admission_timeout_seconds)
            <= MAX_EXTERNAL_ADMISSION_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "admission_timeout_seconds must be positive and at most "
                f"{MAX_EXTERNAL_ADMISSION_TIMEOUT_SECONDS:g} seconds"
            )
        self.policy = policy
        self._process_query = process_query
        self._docker_claim_query = docker_claim_query
        self._systemd_claim_query = systemd_claim_query
        self._unmanaged_mps_client_query = unmanaged_mps_client_query
        if (
            authorized_mps_server_pids is not None
            and mps_authority_query is not None
        ):
            raise ValueError(
                "configure either legacy MPS servers or one MPS authority snapshot"
            )
        self._authorized_mps_server_pids = authorized_mps_server_pids
        self._mps_authority_query = mps_authority_query
        self._allow_descriptor_mps_authority = allow_descriptor_mps_authority
        self._admission_timeout_seconds = float(
            admission_timeout_seconds
        )
        self._monotonic = monotonic
        # Kept as a source-compatible constructor argument. Inventories are
        # shared only by one HostGpuBroker candidate search, never by TTL.
        _ = cache_seconds

    def begin_admission(
        self,
        *,
        leases: tuple[Lease, ...],
        owner: OwnerIdentity,
        component: str,
        environment: str,
        client_id: str | None = None,
        parent_lease_id: str | None = None,
    ) -> _ExternalGpuAdmission:
        return _ExternalGpuAdmission(
            self,
            leases=leases,
            owner=owner,
            component=component,
            environment=environment,
            client_id=client_id,
            parent_lease_id=parent_lease_id,
        )

    def __call__(
        self,
        _index: int,
        uuid: str,
        leases: tuple[Lease, ...],
        owner: OwnerIdentity,
        component: str,
        environment: str,
        *,
        client_id: str | None = None,
        parent_lease_id: str | None = None,
    ) -> bool:
        # Direct callers receive an isolated one-candidate audit.
        return self.begin_admission(
            leases=leases,
            owner=owner,
            component=component,
            environment=environment,
            client_id=client_id,
            parent_lease_id=parent_lease_id,
        )(_index, uuid)

    def _candidate_busy(
        self,
        admission: _ExternalGpuAdmission,
        index: int,
        uuid: str,
    ) -> bool:
        # An inherited descriptor root is the development/acceptance authority.
        # It must never be usable as a production control plane, nor may it
        # authorize the production-only GPU2.
        if self._allow_descriptor_mps_authority and (
            admission.environment != "dev" or index == 2
        ):
            return True
        if uuid in self.policy.blocked_gpu_uuids:
            return True
        try:
            initial_mps, initial_unmanaged = self._mps_audit(
                index,
                uuid,
                admission.leases,
                deadline=admission.deadline,
            )
            if initial_unmanaged:
                return True
            initial = admission.initial_inventory(
                index,
                uuid,
                initial_mps,
            )
            if self._snapshot_blocks_candidate(
                initial,
                index=index,
                uuid=uuid,
                leases=admission.leases,
                owner=admission.owner,
                component=admission.component,
                environment=admission.environment,
                mps_authority=initial_mps,
            ):
                return True

            # Only a candidate that appears free pays for the final snapshot.
            # Docker and systemd each perform their own internal CAS; comparing
            # the target fingerprint here binds them to the initial request-local
            # inventory. One MPS authority snapshot encloses both inventories;
            # its final CAS also catches clients that do not change NVML's shared
            # server PID.
            final = self._inventories(deadline=admission.deadline)
            if (
                final.target_fingerprint(uuid)
                != initial.target_fingerprint(uuid)
            ):
                return True
            if self._snapshot_blocks_candidate(
                final,
                index=index,
                uuid=uuid,
                leases=admission.leases,
                owner=admission.owner,
                component=admission.component,
                environment=admission.environment,
                mps_authority=initial_mps,
            ):
                return True

            # Docker/systemd take time after the snapshot's first nvidia-smi
            # query. Re-read the target before the final MPS seal. A same-UID
            # direct process or MPS client can still race its respective final
            # read; UID 1001 is the documented trust boundary for that residual.
            trailing_processes = self._query_compute_processes(
                deadline=admission.deadline
            )
            busy = trailing_processes.get(
                uuid,
                frozenset(),
            ) != final.processes.get(uuid, frozenset())
            if busy:
                return True
            final_mps, final_unmanaged = self._mps_audit(
                index,
                uuid,
                admission.leases,
                deadline=admission.deadline,
            )
            if final_unmanaged or final_mps != initial_mps:
                return True
            admission.finalize(index, uuid)
            return False
        except Exception:
            # Allocation fails closed if any authority snapshot is unavailable.
            return True

    def _mps_audit(
        self,
        index: int,
        uuid: str,
        leases: tuple[Lease, ...],
        *,
        deadline: float,
    ) -> tuple[MpsAuthoritySnapshot, bool]:
        if self._mps_authority_query is not None:
            authority = _call_with_optional_deadline(
                self._mps_authority_query,
                index,
                uuid,
                deadline=deadline,
                monotonic=self._monotonic,
            )
        else:
            authorized = (
                frozenset()
                if self._authorized_mps_server_pids is None
                else _call_with_optional_deadline(
                    self._authorized_mps_server_pids,
                    index,
                    uuid,
                    deadline=deadline,
                    monotonic=self._monotonic,
                )
            )
            authority = MpsAuthoritySnapshot(
                server_pids=authorized,
                gpu_declarers=frozenset(),
                clients=frozenset(),
                descriptor_authority=False,
            )
        if (
            not isinstance(authority, MpsAuthoritySnapshot)
            or not isinstance(authority.server_pids, frozenset)
            or not isinstance(authority.gpu_declarers, frozenset)
            or not isinstance(authority.clients, frozenset)
            or not isinstance(authority.descriptor_authority, bool)
            or (
                self._allow_descriptor_mps_authority
                and self._mps_authority_query is not None
                and not authority.descriptor_authority
            )
            or len(authority.server_pids) > 1
            or any(
                not isinstance(pid, int)
                or isinstance(pid, bool)
                or pid <= 0
                for pid in authority.server_pids
            )
            or any(
                not isinstance(declarer, SystemdGpuDeclarer)
                or declarer.pid <= 0
                or declarer.process_start_ticks <= 0
                or declarer.gpu_uuids != frozenset({uuid})
                for declarer in authority.gpu_declarers
            )
            or (
                self._mps_authority_query is not None
                and (
                    len({item.pid for item in authority.gpu_declarers})
                    != len(authority.gpu_declarers)
                    or len(authority.gpu_declarers)
                    != 1 + len(authority.server_pids)
                    or not authority.server_pids
                    <= {item.pid for item in authority.gpu_declarers}
                )
            )
            or any(
                not isinstance(client, MpsClient)
                or client.server_pid not in authority.server_pids
                or not _mps_device_matches(client.device_uuid, uuid)
                for client in authority.clients
            )
        ):
            raise BrokerError(
                "mps_control_unavailable",
                "MPS authority returned an invalid identity snapshot",
            )
        unmanaged = (
            False
            if self._unmanaged_mps_client_query is None
            else _call_with_optional_deadline(
                self._unmanaged_mps_client_query,
                index,
                uuid,
                leases,
                deadline=deadline,
                monotonic=self._monotonic,
            )
        )
        if not isinstance(unmanaged, bool):
            raise BrokerError(
                "mps_control_unavailable",
                "MPS client authority returned an invalid result",
            )
        return authority, unmanaged

    def _snapshot_blocks_candidate(
        self,
        snapshot: _ExternalInventorySnapshot,
        *,
        index: int,
        uuid: str,
        leases: tuple[Lease, ...],
        owner: OwnerIdentity,
        component: str,
        environment: str,
        mps_authority: MpsAuthoritySnapshot,
    ) -> bool:
        authorized_mps_servers = mps_authority.server_pids
        descriptor_gpu1_authority = (
            self._allow_descriptor_mps_authority
            and mps_authority.descriptor_authority
            and index == 1
            and uuid == EXPECTED_GPU_UUIDS[1]
        )
        descriptor_backend_pids = (
            exact_dev_gpu1_backend_docker_workload_pids(
                leases,
                snapshot.docker_claims,
            )
            if descriptor_gpu1_authority
            else frozenset()
        )
        if descriptor_gpu1_authority and any(
            lease.gpu_index == index
            and lease.gpu_uuid == uuid
            and lease.component == "backend"
            for lease in leases
        ) and not descriptor_backend_pids:
            return True
        for claim in snapshot.docker_claims:
            if uuid not in claim.gpu_uuids:
                continue
            registration = self.policy.managed_docker_claims.get(
                claim.registration_id or ""
            )
            if registration is None or (
                claim.component != registration.component
                or claim.environment != registration.environment
                or claim.compose_project != registration.compose_project
                or claim.compose_service != registration.compose_service
                or claim.gpu_uuids != registration.gpu_uuids
            ):
                return True
            eligible_owners = [
                lease.owner_pid
                for lease in leases
                if lease.gpu_uuid == uuid
                and lease.component == registration.component
                and lease.environment == registration.environment
            ]
            if (
                component == registration.component
                and environment == registration.environment
            ):
                eligible_owners.append(owner.pid)
            if not any(
                _pid_is_or_descends_from(candidate, claim.init_pid)
                for candidate in eligible_owners
            ):
                # The exact managed MD container is an idle CPU supervisor.
                if registration.component != "md":
                    return True

        systemd_authorized_mps_servers: set[int] = set()
        descriptor_host_workload_pids: set[int] = set()
        for claim in snapshot.systemd_claims:
            if uuid not in claim.gpu_uuids:
                continue
            if descriptor_gpu1_authority:
                if claim_is_exact_dev_gpu1_host_workloads_scope(
                    claim,
                    index=index,
                    uuid=uuid,
                    leases=leases,
                    authorized_mps_declarers=mps_authority.gpu_declarers,
                    authorized_mps_server_pids=mps_authority.server_pids,
                ):
                    descriptor_host_workload_pids.update(
                        declarer.pid
                        for declarer in claim.live_gpu_declarers
                        if declarer not in mps_authority.gpu_declarers
                        and declarer.gpu_uuids == frozenset({uuid})
                    )
                    continue
            elif self._claim_is_unique_dft_residency_scope(
                claim,
                index=index,
                uuid=uuid,
                leases=leases,
                authorized_mps_declarers=mps_authority.gpu_declarers,
            ):
                continue
            identity = f"{claim.scope}:{claim.unit}"
            if self.policy.managed_systemd_claims.get(identity) != claim.gpu_uuids:
                return True
            exact_mps_identity = f"system:nexpoly-gpu-mps@{index}.service"
            if (
                identity == exact_mps_identity
                and claim.gpu_uuids == frozenset({uuid})
                and authorized_mps_servers
                and authorized_mps_servers.issubset(claim.process_pids)
            ):
                systemd_authorized_mps_servers.update(
                    authorized_mps_servers
                )
                continue
            eligible_owners = [
                lease.owner_pid for lease in leases if lease.gpu_uuid == uuid
            ]
            eligible_owners.append(owner.pid)
            if not any(
                candidate in claim.process_pids
                or (
                    claim.main_pid > 0
                    and _pid_is_or_descends_from(candidate, claim.main_pid)
                )
                for candidate in eligible_owners
            ):
                return True

        if descriptor_gpu1_authority:
            mps_client_pids = frozenset(
                client.client_pid for client in mps_authority.clients
            )
            descriptor_workload_pids = (
                descriptor_backend_pids
                | frozenset(descriptor_host_workload_pids)
            ) & mps_client_pids
            for pid in snapshot.processes.get(uuid, frozenset()):
                if pid in authorized_mps_servers:
                    continue
                if pid not in descriptor_workload_pids:
                    return True
            return False

        owners = [lease.owner_pid for lease in leases if lease.gpu_uuid == uuid]
        for pid in snapshot.processes.get(uuid, frozenset()):
            if pid in authorized_mps_servers:
                if (
                    self._allow_descriptor_mps_authority
                    or pid in systemd_authorized_mps_servers
                ):
                    continue
                return True
            if not any(_pid_is_or_descends_from(pid, owner) for owner in owners):
                return True
        return False

    @staticmethod
    def _claim_is_unique_dft_residency_scope(
        claim: SystemdGpuClaim,
        *,
        index: int,
        uuid: str,
        leases: tuple[Lease, ...],
        authorized_mps_declarers: frozenset[SystemdGpuDeclarer],
    ) -> bool:
        """Retain exact DFT-only authority for legacy/non-descriptor paths."""

        candidates = tuple(
            lease
            for lease in leases
            if exact_dft_residency_scope_authority(
                lease,
                index=index,
                uuid=uuid,
            )
            is not None
        )
        if len(candidates) != 1:
            return False
        return claim_is_exact_dft_residency_scope(
            claim,
            index=index,
            uuid=uuid,
            lease=candidates[0],
            authorized_mps_declarers=authorized_mps_declarers,
        )

    def _query_compute_processes(
        self,
        *,
        deadline: float,
    ) -> dict[str, frozenset[int]]:
        if self._process_query is query_compute_processes:
            processes = self._process_query(
                deadline=deadline,
                monotonic=self._monotonic,
            )
        else:
            processes = _call_with_optional_deadline(
                self._process_query,
                deadline=deadline,
                monotonic=self._monotonic,
            )
        if not isinstance(processes, dict) or any(
            not isinstance(uuid, str)
            or not isinstance(pids, frozenset)
            or any(
                not isinstance(pid, int)
                or isinstance(pid, bool)
                or pid <= 0
                for pid in pids
            )
            for uuid, pids in processes.items()
        ):
            raise BrokerError(
                "gpu_process_inventory_unavailable",
                "compute process inventory is invalid",
            )
        return dict(processes)

    def _inventories(
        self,
        *,
        deadline: float | None = None,
    ) -> _ExternalInventorySnapshot:
        # A request receives one initial snapshot; a potentially allowed target
        # receives a second, independently linearized snapshot.
        if deadline is None:
            deadline = (
                self._monotonic() + self._admission_timeout_seconds
            )
        processes = self._query_compute_processes(deadline=deadline)
        if (
            self._docker_claim_query is query_docker_gpu_claims
            and self._systemd_claim_query is query_systemd_gpu_claims
        ):
            # The default read-only authorities are independent after the
            # compute snapshot is fixed. Overlap their subprocess-heavy CAS
            # audits so the full allow path retains client-deadline margin.
            # Injected authorities stay serial for a deterministic contract.
            executor = ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="gpu-authority",
            )
            authority_futures = []
            try:
                docker_future = executor.submit(
                    self._docker_claim_query,
                    deadline=deadline,
                    monotonic=self._monotonic,
                )
                authority_futures.append(docker_future)
                systemd_future = executor.submit(
                    self._systemd_claim_query,
                    compute_processes=processes,
                    deadline=deadline,
                    monotonic=self._monotonic,
                )
                authority_futures.append(systemd_future)
                pending = {docker_future, systemd_future}
                while pending:
                    remaining = _remaining_admission_seconds(
                        deadline,
                        monotonic=self._monotonic,
                    )
                    completed, pending = wait(
                        pending,
                        timeout=remaining,
                        return_when=FIRST_COMPLETED,
                    )
                    if not completed:
                        raise BrokerError(
                            "gpu_admission_timeout",
                            "external GPU authority deadline expired",
                        )
                    for completed_future in completed:
                        completed_future.result()
                docker_claims = docker_future.result()
                systemd_claims = systemd_future.result()
            finally:
                for future in authority_futures:
                    future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
        else:
            docker_claims = _call_with_optional_deadline(
                self._docker_claim_query,
                deadline=deadline,
                monotonic=self._monotonic,
            )
            if self._systemd_claim_query is query_systemd_gpu_claims:
                systemd_claims = self._systemd_claim_query(
                    compute_processes=processes,
                    deadline=deadline,
                    monotonic=self._monotonic,
                )
            else:
                systemd_claims = _call_with_optional_deadline(
                    self._systemd_claim_query,
                    deadline=deadline,
                    monotonic=self._monotonic,
                )
        if (
            not isinstance(docker_claims, tuple)
            or any(not isinstance(claim, DockerGpuClaim) for claim in docker_claims)
            or len({claim.container_id for claim in docker_claims})
            != len(docker_claims)
        ):
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "Docker GPU claim inventory is invalid",
            )
        docker_claims = tuple(
            sorted(docker_claims, key=lambda claim: claim.container_id)
        )
        if (
            not isinstance(systemd_claims, tuple)
            or any(
                not isinstance(claim, SystemdGpuClaim)
                for claim in systemd_claims
            )
            or len({(claim.scope, claim.unit) for claim in systemd_claims})
            != len(systemd_claims)
        ):
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "systemd GPU claim inventory is invalid",
            )
        systemd_claims = tuple(
            sorted(
                systemd_claims,
                key=lambda claim: (claim.scope, claim.unit),
            )
        )
        _ensure_admission_open(deadline, monotonic=self._monotonic)
        return _ExternalInventorySnapshot(
            processes=processes,
            docker_claims=docker_claims,
            systemd_claims=systemd_claims,
        )


def _pid_is_or_descends_from(pid: int, owner_pid: int) -> bool:
    current = pid
    visited: set[int] = set()
    for _ in range(128):
        if current == owner_pid:
            return True
        if current <= 1 or current in visited:
            return False
        visited.add(current)
        try:
            status = Path(f"/proc/{current}/status").read_text(encoding="ascii")
        except OSError:
            return False
        parent_line = next((line for line in status.splitlines() if line.startswith("PPid:")), None)
        if parent_line is None:
            return False
        try:
            current = int(parent_line.split(":", 1)[1].strip())
        except ValueError:
            return False
    return False


def _read_cgroup(pid: int) -> str:
    try:
        value = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise BrokerError(
            "workload_identity_unavailable",
            f"cannot read cgroup identity for host PID {pid}",
        ) from exc
    if not value:
        raise BrokerError(
            "workload_identity_unavailable",
            f"cgroup identity is empty for host PID {pid}",
        )
    return value


def _cgroup_is_same_or_descendant(workload: str, owner: str) -> bool:
    def paths(value: str) -> dict[tuple[str, str], str]:
        result: dict[tuple[str, str], str] = {}
        for line in value.splitlines():
            fields = line.split(":", 2)
            if len(fields) != 3 or not fields[2].startswith("/"):
                return {}
            key = (fields[0], fields[1])
            if key in result:
                return {}
            result[key] = fields[2]
        return result

    workload_paths = paths(workload)
    owner_paths = paths(owner)
    if not workload_paths or not owner_paths:
        return False
    if not set(owner_paths).issubset(workload_paths):
        return False
    return all(
        workload_paths[key] == owner_path
        or workload_paths[key].startswith(owner_path.rstrip("/") + "/")
        for key, owner_path in owner_paths.items()
    )


def _cgroup_has_scoped_path(value: str) -> bool:
    """Root cgroups are not specific enough to prove MPS client ownership."""

    paths: list[str] = []
    for line in value.splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3 or not fields[2].startswith("/"):
            return False
        paths.append(fields[2])
    return bool(paths) and any(path != "/" for path in paths)


def resolve_workload_identity(
    lease: Lease,
    namespace_pid: int,
    process_start_ticks: int,
    namespace_process_group_id: int,
    *,
    require_owner_cgroup: bool = True,
) -> tuple[int, int, int, str]:
    """Translate a child namespace PID into a fenced host process group."""

    if namespace_process_group_id != namespace_pid:
        raise BrokerError(
            "workload_identity_mismatch",
            "execution workload must be its start_new_session process-group leader",
        )
    candidates: list[int] = []
    for status_path in Path("/proc").glob("[0-9]*/status"):
        try:
            host_pid = int(status_path.parent.name)
            status = status_path.read_text(encoding="ascii")
        except (OSError, ValueError):
            continue
        namespace_line = next(
            (line for line in status.splitlines() if line.startswith("NSpid:")),
            None,
        )
        if namespace_line is None:
            namespace_pids = (host_pid,)
        else:
            try:
                namespace_pids = tuple(
                    int(value) for value in namespace_line.split(":", 1)[1].split()
                )
            except ValueError:
                continue
        if not namespace_pids or namespace_pids[-1] != namespace_pid:
            continue
        try:
            if read_process_start_ticks(host_pid) != process_start_ticks:
                continue
            host_process_group_id = os.getpgid(host_pid)
        except (BrokerError, OSError):
            continue
        if host_pid == lease.owner_pid or not _pid_is_or_descends_from(
            host_pid, lease.owner_pid
        ):
            continue
        if host_process_group_id != host_pid:
            continue
        candidates.append(host_pid)
    if len(candidates) != 1:
        raise BrokerError(
            "workload_identity_unavailable",
            "cannot resolve one live descendant workload in the host PID namespace",
        )
    host_pid = candidates[0]
    workload_cgroup = _read_cgroup(host_pid)
    if require_owner_cgroup:
        owner_cgroup = _read_cgroup(lease.owner_pid)
        if not _cgroup_is_same_or_descendant(workload_cgroup, owner_cgroup):
            raise BrokerError(
                "workload_identity_mismatch",
                "workload cgroup is outside the lease owner cgroup",
            )
    return (
        host_pid,
        process_start_ticks,
        host_pid,
        workload_cgroup,
    )


class JobCgroupController:
    """Fail-closed control of lease-named transient user scope cgroups.

    The Worker creates a scope with ``systemd-run --user --scope`` before
    registration.  The Broker never attempts to move a process across cgroup
    ownership boundaries.  It verifies the exact systemd unit, cgroup-v2
    path, live PID/start-time/UID and initially exclusive membership, then
    uses only that already-bound scope for freeze, kill and emptiness proofs.
    """

    def __init__(
        self,
        *,
        identity_resolver=None,
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        run=subprocess.run,
        uid: int | None = None,
        process_uid_resolver=None,
        process_start_ticks_reader=read_process_start_ticks,
        process_group_reader=os.getpgid,
        process_state_reader=None,
        now_ns=time.monotonic_ns,
        monotonic=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.uid = os.geteuid() if uid is None else uid
        if (
            isinstance(self.uid, bool)
            or not isinstance(self.uid, int)
            or self.uid <= 0
        ):
            raise BrokerError(
                "workload_control_unavailable",
                "GPU scope controller UID is invalid",
            )
        self._identity_resolver = identity_resolver or (
            lambda lease, pid, start_ticks, group_id: resolve_workload_identity(
                lease,
                pid,
                start_ticks,
                group_id,
                require_owner_cgroup=False,
            )
        )
        self._run = run
        self._process_uid_resolver = (
            process_uid_resolver or self._read_process_uids
        )
        self._process_start_ticks_reader = process_start_ticks_reader
        self._process_group_reader = process_group_reader
        self._process_state_reader = (
            process_state_reader or self._read_process_state
        )
        self._now_ns = now_ns
        self._monotonic = monotonic
        self._sleep = sleep
        self._manager_control_group = user_manager_control_group(self.uid)
        try:
            if (
                not cgroup_root.is_absolute()
                or cgroup_root.is_symlink()
                or not cgroup_root.is_dir()
            ):
                raise OSError("cgroup root is missing or unsafe")
            resolved_root = cgroup_root.resolve(strict=True)
            if resolved_root != cgroup_root:
                raise OSError("cgroup root has a non-canonical identity")
            self._cgroup_root = resolved_root
            manager_path = self._path_for_control_group(
                self._manager_control_group
            )
            resolved_manager = manager_path.resolve(strict=True)
        except OSError as exc:
            raise BrokerError(
                "workload_control_unavailable",
                "user manager cgroup is missing or unsafe",
            ) from exc
        if (
            manager_path.is_symlink()
            or not manager_path.is_dir()
            or resolved_manager != manager_path
        ):
            raise BrokerError(
                "workload_control_unavailable",
                "user manager cgroup is missing or unsafe",
            )
        manager_stat = manager_path.stat()
        if (
            manager_stat.st_uid != self.uid
            or stat.S_IMODE(manager_stat.st_mode) & 0o022
        ):
            raise BrokerError(
                "workload_control_unavailable",
                "user manager cgroup has an unexpected owner",
            )
        manager_identity = self._user_manager_identity()
        if manager_identity != self._manager_control_group:
            raise BrokerError(
                "workload_control_unavailable",
                "systemd user manager cgroup identity differs",
            )

    def resolve_and_assign(
        self,
        lease: Lease,
        namespace_pid: int,
        process_start_ticks: int,
        namespace_process_group_id: int,
    ) -> tuple[int, int, int, str]:
        host_pid, start_ticks, group_id, workload_cgroup = self._identity_resolver(
            lease,
            namespace_pid,
            process_start_ticks,
            namespace_process_group_id,
        )
        expected = self._expected_control_group(lease)
        if self._unified_path(workload_cgroup) != expected:
            raise BrokerError(
                "workload_identity_mismatch",
                "workload is not in its exact lease-named user scope",
            )
        self._require_process_identity(host_pid, start_ticks, group_id)
        target = self._existing_scope_path(lease)
        unit = self._scope_unit(lease)
        status = self._unit_status(unit)
        if status != {
            "Id": unit,
            "ControlGroup": expected,
            "LoadState": "loaded",
            "ActiveState": "active",
            "Slice": SCOPE_SLICE,
        }:
            raise BrokerError(
                "workload_identity_mismatch",
                "transient GPU scope unit identity differs",
            )
        pids = self._scope_pids(target)
        if pids != {host_pid}:
            raise BrokerError(
                "workload_identity_mismatch",
                "new transient GPU scope contains a foreign or reused process",
            )
        return host_pid, start_ticks, group_id, workload_cgroup

    def validate_active(self, lease: Lease) -> None:
        """Re-bind the exact live scope before asking MPS to terminate it."""

        self._active_scope_path(lease)

    def freeze(self, lease: Lease) -> str:
        target = self._active_scope_path(lease)
        try:
            (target / "cgroup.freeze").write_text("1", encoding="ascii")
        except OSError as exc:
            raise BrokerError(
                "workload_control_unavailable", "cannot freeze workload cgroup"
            ) from exc
        deadline = self._monotonic() + 2.0
        while True:
            try:
                events = (target / "cgroup.events").read_text(encoding="ascii")
            except OSError as exc:
                raise BrokerError(
                    "workload_control_unavailable",
                    "cannot verify workload cgroup freeze",
                ) from exc
            if any(line.strip() == "frozen 1" for line in events.splitlines()):
                return f"{lease.lease_id}:{self._now_ns()}"
            if self._monotonic() >= deadline:
                raise BrokerError(
                    "workload_control_unavailable",
                    "workload cgroup did not enter frozen state",
                )
            self._sleep(0.02)

    def kill(self, lease: Lease) -> None:
        try:
            (self._active_scope_path(lease) / "cgroup.kill").write_text(
                "1", encoding="ascii"
            )
        except OSError as exc:
            raise BrokerError(
                "workload_control_unavailable", "cannot kill workload cgroup"
            ) from exc

    def empty(self, lease: Lease) -> bool:
        self._require_bound_scope(lease)
        target = self._path_for_control_group(
            self._expected_control_group(lease)
        )
        deadline = self._monotonic() + 2.0
        while True:
            if target.exists():
                try:
                    self._validate_scope_path(target)
                    pids = self._scope_pids(target)
                    events = (target / "cgroup.events").read_text(
                        encoding="ascii"
                    )
                except OSError as exc:
                    raise BrokerError(
                        "workload_control_unavailable",
                        "cannot verify workload scope emptiness",
                    ) from exc
                status = self._unit_status(self._scope_unit(lease))
                if (
                    status["Id"] != self._scope_unit(lease)
                    or status["ControlGroup"]
                    != self._expected_control_group(lease)
                    or status["LoadState"] != "loaded"
                    or status["ActiveState"]
                    not in {"active", "deactivating", "inactive", "failed"}
                    or status["Slice"] != SCOPE_SLICE
                ):
                    raise BrokerError(
                        "workload_identity_mismatch",
                        "remaining transient GPU scope unit identity differs",
                    )
                populated = {
                    line.strip()
                    for line in events.splitlines()
                    if line.startswith("populated ")
                }
                if not pids and populated == {"populated 0"}:
                    return True
            elif self._scope_disappeared_safely(lease):
                return True
            if self._monotonic() >= deadline:
                return False
            self._sleep(0.02)

    def cleanup(self, lease: Lease) -> None:
        if not self.empty(lease):
            raise BrokerError(
                "workload_termination_failed", "workload scope is not empty"
            )
        target = self._path_for_control_group(
            self._expected_control_group(lease)
        )
        unit = self._scope_unit(lease)
        if target.exists():
            completed = self._systemctl("stop", unit)
            if completed.returncode != 0:
                raise BrokerError(
                    "workload_control_unavailable",
                    "cannot stop empty transient GPU scope",
                )
        deadline = self._monotonic() + 2.0
        while target.exists() or not self._scope_disappeared_safely(lease):
            if self._monotonic() >= deadline:
                raise BrokerError(
                    "workload_control_unavailable",
                    "transient GPU scope was not collected",
                )
            self._sleep(0.02)

    def _scope_unit(self, lease: Lease) -> str:
        try:
            validate_lease_id(lease.lease_id)
            return scope_unit_name(lease.lease_id)
        except ValueError as exc:
            raise BrokerError(
                "workload_control_unavailable",
                "lease ID cannot name an exact transient GPU scope",
            ) from exc

    def _expected_control_group(self, lease: Lease) -> str:
        self._scope_unit(lease)
        return scope_control_group(lease.lease_id, uid=self.uid)

    def _path_for_control_group(self, control_group: str) -> Path:
        if (
            not isinstance(control_group, str)
            or not control_group.startswith("/")
            or "\x00" in control_group
            or any(part in {"", ".", ".."} for part in control_group.split("/")[1:])
        ):
            raise BrokerError(
                "workload_control_unavailable",
                "transient GPU scope cgroup path is unsafe",
            )
        path = self._cgroup_root.joinpath(*control_group.split("/")[1:])
        try:
            if not path.resolve(strict=False).is_relative_to(
                self._cgroup_root.resolve(strict=True)
            ):
                raise ValueError("path escaped cgroup root")
        except (OSError, ValueError) as exc:
            raise BrokerError(
                "workload_control_unavailable",
                "transient GPU scope cgroup path is unsafe",
            ) from exc
        return path

    def _validate_scope_path(self, path: Path) -> None:
        try:
            resolved = path.resolve(strict=True)
            metadata = path.stat()
        except OSError as exc:
            raise BrokerError(
                "workload_control_unavailable",
                "transient GPU scope cgroup is missing or unsafe",
            ) from exc
        if (
            path.is_symlink()
            or not path.is_dir()
            or resolved != path
            or metadata.st_uid != self.uid
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise BrokerError(
                "workload_control_unavailable",
                "transient GPU scope cgroup is missing or unsafe",
            )
        required_access = {
            "cgroup.events": os.R_OK,
            "cgroup.freeze": os.R_OK | os.W_OK,
            "cgroup.kill": os.W_OK,
            "cgroup.procs": os.R_OK,
        }
        if any(
            (path / name).is_symlink()
            or not (path / name).is_file()
            or not os.access(path / name, access)
            for name, access in required_access.items()
        ):
            raise BrokerError(
                "workload_control_unavailable",
                "transient GPU scope controls are incomplete",
            )

    def _existing_scope_path(self, lease: Lease) -> Path:
        target = self._path_for_control_group(
            self._expected_control_group(lease)
        )
        self._validate_scope_path(target)
        return target

    def _active_scope_path(self, lease: Lease) -> Path:
        try:
            self._require_bound_scope(lease)
        except BrokerError as exc:
            self._log_workload_revalidation_failure(
                lease,
                check="lease_scope_binding",
                broker_error_code=exc.code,
            )
            raise
        target = self._existing_scope_path(lease)
        unit = self._scope_unit(lease)
        expected = {
            "Id": unit,
            "ControlGroup": self._expected_control_group(lease),
            "LoadState": "loaded",
            "ActiveState": "active",
            "Slice": SCOPE_SLICE,
        }
        pids = self._scope_pids(target)
        if self._unit_status(unit) != expected:
            self._log_workload_revalidation_failure(
                lease,
                check="scope_unit_state",
                broker_error_code="workload_identity_mismatch",
            )
            raise BrokerError(
                "workload_identity_mismatch",
                "transient GPU scope is not active with a live workload",
            )
        if not pids:
            self._log_workload_revalidation_failure(
                lease,
                check="scope_membership",
                broker_error_code="workload_identity_mismatch",
            )
            raise BrokerError(
                "workload_identity_mismatch",
                "transient GPU scope is not active with a live workload",
            )
        workload_identity_missing = (
            lease.workload_pid is None
            or lease.workload_process_start_ticks is None
            or lease.workload_process_group_id is None
        )
        exact_root_membership = (
            not workload_identity_missing and pids == {lease.workload_pid}
        )
        exact_dft_descendant_membership = (
            not workload_identity_missing
            and not exact_root_membership
            and self._is_exact_dft_descendant_membership(lease, pids)
        )
        if workload_identity_missing or not (
            exact_root_membership or exact_dft_descendant_membership
        ):
            self._log_workload_revalidation_failure(
                lease,
                check="scope_membership",
                broker_error_code="workload_identity_mismatch",
            )
            raise BrokerError(
                "workload_identity_mismatch",
                "active transient GPU scope workload identity differs",
            )
        try:
            self._require_process_identity(
                lease.workload_pid,
                lease.workload_process_start_ticks,
                lease.workload_process_group_id,
            )
        except BrokerError as exc:
            self._log_workload_revalidation_failure(
                lease,
                check="process_identity",
                broker_error_code=exc.code,
            )
            raise
        if exact_dft_descendant_membership:
            try:
                membership_after = self._scope_pids(target)
            except BrokerError as exc:
                self._log_workload_revalidation_failure(
                    lease,
                    check="scope_membership",
                    broker_error_code=exc.code,
                )
                raise
            if membership_after != pids:
                self._log_workload_revalidation_failure(
                    lease,
                    check="scope_membership",
                    broker_error_code="workload_identity_mismatch",
                )
                raise BrokerError(
                    "workload_identity_mismatch",
                    "active transient GPU scope membership changed during validation",
                )
        return target

    @staticmethod
    def _is_exact_dft_descendant_membership(
        lease: Lease,
        pids: set[int],
    ) -> bool:
        workload_pid = lease.workload_pid
        expected_uuid = EXPECTED_GPU_UUIDS.get(lease.gpu_index)
        if (
            isinstance(workload_pid, bool)
            or not isinstance(workload_pid, int)
            or workload_pid <= 0
            or workload_pid not in pids
            or expected_uuid is None
            or lease.gpu_uuid != expected_uuid
        ):
            return False
        authority = exact_dft_residency_scope_authority(
            lease,
            index=lease.gpu_index,
            uuid=expected_uuid,
        )
        if authority is None or authority[0] != workload_pid:
            return False
        return all(
            process_is_exact_dft_residency_descendant(
                pid,
                lease,
                index=lease.gpu_index,
                uuid=expected_uuid,
            )
            for pid in sorted(pids)
        )

    @staticmethod
    def _log_workload_revalidation_failure(
        lease: Lease,
        *,
        check: str,
        broker_error_code: str,
    ) -> None:
        logger.error(
            "gpu_workload_revalidation_failed lease_id=%s fencing_token=%d "
            "check=%s broker_error_code=%s",
            lease.lease_id,
            lease.fencing_token,
            check,
            broker_error_code,
        )

    def _require_bound_scope(self, lease: Lease) -> None:
        expected = self._expected_control_group(lease)
        if (
            not isinstance(lease.workload_cgroup, str)
            or self._unified_path(lease.workload_cgroup) != expected
        ):
            raise BrokerError(
                "workload_identity_mismatch",
                "lease is not bound to its exact transient GPU scope",
            )

    def _scope_pids(self, target: Path) -> set[int]:
        try:
            pids = {
                int(raw)
                for raw in (target / "cgroup.procs")
                .read_text(encoding="ascii")
                .split()
            }
        except (OSError, ValueError) as exc:
            raise BrokerError(
                "workload_control_unavailable",
                "cannot read transient GPU scope process inventory",
            ) from exc
        if any(pid <= 0 for pid in pids):
            raise BrokerError(
                "workload_control_unavailable",
                "transient GPU scope process inventory is invalid",
            )
        for pid in pids:
            self._require_process_uid(pid)
        return pids

    def _require_process_uid(self, pid: int) -> None:
        try:
            uids = tuple(self._process_uid_resolver(pid))
        except (OSError, TypeError, ValueError) as exc:
            raise BrokerError(
                "workload_identity_unavailable",
                "cannot establish transient GPU scope process UID",
            ) from exc
        if len(uids) != 4 or set(uids) != {self.uid}:
            raise BrokerError(
                "workload_identity_mismatch",
                "transient GPU scope contains a process with another UID",
            )

    def _require_process_identity(
        self,
        pid: int,
        start_ticks: int,
        group_id: int,
    ) -> None:
        self._require_process_uid(pid)
        try:
            actual_start = self._process_start_ticks_reader(pid)
            actual_group = self._process_group_reader(pid)
        except (BrokerError, OSError) as exc:
            raise BrokerError(
                "workload_identity_unavailable",
                "transient GPU scope process identity disappeared",
            ) from exc
        if (
            actual_start != start_ticks
            or group_id != pid
            or actual_group != pid
        ):
            raise BrokerError(
                "workload_identity_mismatch",
                "transient GPU scope PID/start-time/process-group differs",
            )

    def _scope_disappeared_safely(self, lease: Lease) -> bool:
        status = self._unit_status(self._scope_unit(lease))
        if status != {
            "Id": self._scope_unit(lease),
            "ControlGroup": "",
            "LoadState": "not-found",
            "ActiveState": "inactive",
            "Slice": "",
        }:
            return False
        if (
            lease.workload_pid is None
            or lease.workload_process_start_ticks is None
        ):
            return False
        try:
            current_start = self._process_start_ticks_reader(lease.workload_pid)
        except (BrokerError, OSError):
            return True
        if current_start != lease.workload_process_start_ticks:
            return True
        try:
            # cgroup.kill can make the target a zombie while its Worker parent
            # is synchronously waiting for this Broker call.  A zombie cannot
            # execute or hold a cgroup membership; requiring the parent to
            # reap it here would deadlock the termination handshake.
            return self._process_state_reader(lease.workload_pid) in {"Z", "X"}
        except (OSError, ValueError):
            return False

    @staticmethod
    def _read_process_uids(pid: int) -> tuple[int, int, int, int]:
        try:
            status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
            uid_line = next(
                line for line in status.splitlines() if line.startswith("Uid:")
            )
            uids = tuple(
                int(value) for value in uid_line.split(":", 1)[1].split()
            )
        except (OSError, StopIteration, ValueError) as exc:
            raise OSError("process UID inventory is unavailable") from exc
        if len(uids) != 4:
            raise ValueError("process UID inventory is invalid")
        return uids  # type: ignore[return-value]

    @staticmethod
    def _read_process_state(pid: int) -> str:
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            final_parenthesis = raw.rfind(")")
            fields_after_comm = raw[final_parenthesis + 2 :].split()
            state = fields_after_comm[0]
        except (OSError, IndexError) as exc:
            raise OSError("process state is unavailable") from exc
        if final_parenthesis < 1 or len(state) != 1:
            raise ValueError("process state is invalid")
        return state

    def _user_manager_identity(self) -> str:
        completed = self._systemctl(
            "show",
            "--property=ControlGroup",
            "--no-pager",
        )
        if completed.returncode != 0:
            raise BrokerError(
                "workload_control_unavailable",
                "cannot query systemd user manager",
            )
        rows = completed.stdout.splitlines()
        if len(rows) != 1 or not rows[0].startswith("ControlGroup="):
            raise BrokerError(
                "workload_control_unavailable",
                "systemd user manager identity is invalid",
            )
        return rows[0].split("=", 1)[1]

    def _unit_status(self, unit: str) -> dict[str, str]:
        completed = self._systemctl(
            "show",
            unit,
            "--property=Id,ControlGroup,LoadState,ActiveState,Slice",
            "--no-pager",
        )
        if completed.returncode != 0:
            raise BrokerError(
                "workload_control_unavailable",
                "cannot query transient GPU scope",
            )
        result: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            if "=" not in line:
                raise BrokerError(
                    "workload_control_unavailable",
                    "transient GPU scope status is invalid",
                )
            key, value = line.split("=", 1)
            if key in result:
                raise BrokerError(
                    "workload_control_unavailable",
                    "transient GPU scope status is duplicated",
                )
            result[key] = value
        if set(result) != {
            "Id",
            "ControlGroup",
            "LoadState",
            "ActiveState",
            "Slice",
        }:
            raise BrokerError(
                "workload_control_unavailable",
                "transient GPU scope status is incomplete",
            )
        return result

    def _systemctl(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            return self._run(
                ("/usr/bin/systemctl", "--user", *arguments),
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BrokerError(
                "workload_control_unavailable",
                "systemd user manager command failed",
            ) from exc

    @staticmethod
    def _unified_path(cgroup: str) -> str:
        matches = [
            line.split(":", 2)[2]
            for line in cgroup.splitlines()
            if line.startswith("0::") and len(line.split(":", 2)) == 3
        ]
        if len(matches) != 1:
            raise BrokerError(
                "workload_identity_mismatch", "workload is not in cgroup v2"
            )
        return matches[0]


class MpsRuntimeGuard:
    """Fail-closed readiness and orphan-client checks for per-GPU MPS pipes."""

    def __init__(
        self,
        state_root: Path,
        *,
        run=subprocess.run,
        control_executable: Path = Path("/usr/bin/nvidia-cuda-mps-control"),
        server_executable: Path = Path("/usr/bin/nvidia-cuda-mps-server"),
        proc_root: Path = Path("/proc"),
        read_process_environment=_read_process_environment,
        read_process_cgroup=_read_cgroup,
        read_start_ticks=read_process_start_ticks,
    ) -> None:
        if (
            not control_executable.is_absolute()
            or not server_executable.is_absolute()
        ):
            raise ValueError("MPS executable authority paths must be absolute")
        self.state_root = state_root
        self._run = run
        self.control_executable = control_executable
        self.server_executable = server_executable
        self.proc_root = proc_root
        self._read_process_environment = read_process_environment
        self._read_process_cgroup = read_process_cgroup
        self._read_start_ticks = read_start_ticks

    @property
    def descriptor_authority(self) -> bool:
        """Whether the Broker owns an inherited, process-local state root."""

        match = re.fullmatch(
            rf"/proc/{os.getpid()}/fd/([1-9][0-9]*)",
            str(self.state_root),
        )
        if match is None:
            return False
        descriptor = int(match.group(1))
        if descriptor <= 2:
            return False
        try:
            opened = os.fstat(descriptor)
            reached = self.state_root.stat()
        except OSError:
            return False
        return (
            stat.S_ISDIR(opened.st_mode)
            and opened.st_uid == 1001
            and opened.st_gid == 1001
            and stat.S_IMODE(opened.st_mode) == 0o700
            and (reached.st_dev, reached.st_ino)
            == (opened.st_dev, opened.st_ino)
        )

    def __call__(self, index: int, uuid: str) -> bool:
        if EXPECTED_GPU_UUIDS.get(index) != uuid:
            return False
        return self._pipe_ready(self.pipe_directory(index))

    def authorized_server_pids(
        self,
        index: int,
        uuid: str,
        *,
        deadline: float | None = None,
    ) -> frozenset[int]:
        """Compatibility view: only servers may be exempted from NVML."""

        return self.authority_snapshot(
            index,
            uuid,
            deadline=deadline,
        ).server_pids

    def authority_snapshot(
        self,
        index: int,
        uuid: str,
        *,
        deadline: float | None = None,
    ) -> MpsAuthoritySnapshot:
        """Return one atomic control/server declarer authority snapshot.

        NVIDIA attributes MPS clients to the shared server in NVML.  Therefore
        only ``server_pids`` may be exempted from the process gate.  The full
        declarer identities additionally allow the broad UID user-manager
        systemd claim to contain the exact descriptor-owned control/server
        processes without trusting a PID or environment alone.
        """

        _ensure_admission_open(deadline)
        if not self(index, uuid):
            raise BrokerError(
                "mps_control_unavailable",
                "MPS pipe authority is unavailable",
            )
        pipe_directory = self.pipe_directory(index)
        pipe_before = self._pipe_identity(pipe_directory)
        first_servers = self._server_list(index, deadline=deadline)
        if len(first_servers) > 1:
            raise BrokerError(
                "mps_control_unavailable",
                "MPS control authority reported multiple servers",
            )
        control_pid = self._control_pid(pipe_directory, uuid)
        control_cgroup = self._validate_process_identity(
            control_pid,
            self.control_executable,
            kind="control",
        )
        server_pid = next(iter(first_servers)) if first_servers else None
        server_cgroup = (
            self._validate_process_identity(
                server_pid,
                self.server_executable,
                kind="server",
            )
            if server_pid is not None
            else None
        )
        if not _cgroup_has_scoped_path(control_cgroup) or (
            server_cgroup is not None and control_cgroup != server_cgroup
        ):
            raise BrokerError(
                "mps_control_unavailable",
                "MPS control and server cgroup authority differs",
        )
        try:
            control_environment = self._read_process_environment(control_pid)
            reported_pipe = self._resolve_process_authority_path(
                control_environment["CUDA_MPS_PIPE_DIRECTORY"],
                control_pid,
            )
            expected_pipe = pipe_directory.resolve(strict=True)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise BrokerError(
                "mps_control_unavailable",
                "MPS control environment authority is unavailable",
            ) from exc
        if (
            not isinstance(control_environment, dict)
            or control_environment.get("CUDA_VISIBLE_DEVICES") != uuid
            or reported_pipe != expected_pipe
            or self._pipe_identity(reported_pipe) != pipe_before
        ):
            raise BrokerError(
                "mps_control_unavailable",
                "MPS control environment authority differs",
            )

        def reject_server_membership_change(
            observed_servers: frozenset[int],
        ) -> None:
            if not first_servers and len(observed_servers) == 1:
                lazy_server_pid = next(iter(observed_servers))
                current_control = self._validated_gpu_declarer(
                    control_pid,
                    self.control_executable,
                    kind="control",
                    uuid=uuid,
                    pipe_directory=pipe_directory,
                    pipe_identity=pipe_before,
                )
                current_server = self._validated_gpu_declarer(
                    lazy_server_pid,
                    self.server_executable,
                    kind="server",
                    uuid=uuid,
                    pipe_directory=pipe_directory,
                    pipe_identity=pipe_before,
                )
                if (
                    current_control.process_cgroup
                    == self._unified_cgroup_path(control_cgroup)
                    == current_server.process_cgroup
                ):
                    raise BrokerError(
                        "mps_authority_changed",
                        "descriptor-owned MPS server lazily appeared during audit",
                    )
            raise BrokerError(
                "mps_control_unavailable",
                "MPS server authority changed during audit",
            )

        clients = self._query_clients(index, deadline=deadline)
        middle_servers = self._server_list(index, deadline=deadline)
        if (
            self._control_pid(pipe_directory, uuid) != control_pid
            or self._pipe_identity(pipe_directory) != pipe_before
        ):
            raise BrokerError(
                "mps_control_unavailable",
                "MPS control authority changed during audit",
            )
        if middle_servers != first_servers:
            reject_server_membership_change(middle_servers)
        if any(
            server_pid is None
            or client.server_pid != server_pid
            or not self._device_matches(client.device_uuid, uuid)
            for client in clients
        ):
            raise BrokerError(
                "mps_control_unavailable",
                "MPS client inventory differs from server authority",
            )
        control_declarer = self._validated_gpu_declarer(
            control_pid,
            self.control_executable,
            kind="control",
            uuid=uuid,
            pipe_directory=pipe_directory,
            pipe_identity=pipe_before,
        )
        declarers = {control_declarer}
        if server_pid is not None:
            server_declarer = self._validated_gpu_declarer(
                server_pid,
                self.server_executable,
                kind="server",
                uuid=uuid,
                pipe_directory=pipe_directory,
                pipe_identity=pipe_before,
            )
            declarers.add(server_declarer)
        if (
            control_declarer.process_cgroup
            != self._unified_cgroup_path(control_cgroup)
            or (
                server_cgroup is not None
                and server_declarer.process_cgroup
                != self._unified_cgroup_path(server_cgroup)
            )
        ):
            raise BrokerError(
                "mps_control_unavailable",
                "MPS process authority changed during audit",
            )
        trailing_clients = self._query_clients(index, deadline=deadline)
        trailing_servers = self._server_list(index, deadline=deadline)
        if (
            self._control_pid(pipe_directory, uuid) != control_pid
            or self._pipe_identity(pipe_directory) != pipe_before
        ):
            raise BrokerError(
                "mps_control_unavailable",
                "MPS control authority changed after declarer validation",
            )
        if trailing_servers != first_servers:
            reject_server_membership_change(trailing_servers)
        trailing_declarers = {
            self._validated_gpu_declarer(
                control_pid,
                self.control_executable,
                kind="control",
                uuid=uuid,
                pipe_directory=pipe_directory,
                pipe_identity=pipe_before,
            )
        }
        if server_pid is not None:
            trailing_declarers.add(
                self._validated_gpu_declarer(
                    server_pid,
                    self.server_executable,
                    kind="server",
                    uuid=uuid,
                    pipe_directory=pipe_directory,
                    pipe_identity=pipe_before,
                )
            )
        if trailing_declarers != declarers:
            raise BrokerError(
                "mps_control_unavailable",
                "MPS declarer authority changed during final identity audit",
            )
        if any(
            server_pid is None
            or client.server_pid != server_pid
            or not self._device_matches(client.device_uuid, uuid)
            for client in trailing_clients
        ):
            raise BrokerError(
                "mps_control_unavailable",
                "MPS trailing client inventory differs from server authority",
            )
        if frozenset(trailing_clients) != frozenset(clients):
            if frozenset(clients) < frozenset(trailing_clients):
                raise BrokerError(
                    "mps_authority_changed",
                    "descriptor-owned MPS client membership grew during audit",
                )
            raise BrokerError(
                "mps_control_unavailable",
                "MPS client identity disappeared or changed during audit",
            )
        _ensure_admission_open(deadline)
        return MpsAuthoritySnapshot(
            server_pids=first_servers,
            gpu_declarers=frozenset(trailing_declarers),
            clients=frozenset(clients),
            descriptor_authority=self.descriptor_authority,
        )

    @staticmethod
    def _unified_cgroup_path(raw: str) -> str:
        matches = [
            line.split(":", 2)[2]
            for line in raw.splitlines()
            if line.startswith("0::") and len(line.split(":", 2)) == 3
        ]
        if len(matches) != 1 or not matches[0].startswith("/"):
            raise BrokerError(
                "mps_control_unavailable",
                "MPS process is not in an exact cgroup-v2 path",
            )
        return matches[0]

    def _validated_gpu_declarer(
        self,
        pid: int,
        executable: Path,
        *,
        kind: str,
        uuid: str,
        pipe_directory: Path,
        pipe_identity: tuple[
            tuple[int, int, int, int, int],
            tuple[int, int, int, int, int],
        ],
    ) -> SystemdGpuDeclarer:
        try:
            start_before = self._read_start_ticks(pid)
            cgroup_before = self._validate_process_identity(
                pid,
                executable,
                kind=kind,
            )
            environment_before = self._read_process_environment(pid)
            reported_pipe_before = self._resolve_process_authority_path(
                environment_before["CUDA_MPS_PIPE_DIRECTORY"],
                pid,
            )
            cgroup_after = self._validate_process_identity(
                pid,
                executable,
                kind=kind,
            )
            environment_after = self._read_process_environment(pid)
            reported_pipe_after = self._resolve_process_authority_path(
                environment_after["CUDA_MPS_PIPE_DIRECTORY"],
                pid,
            )
            start_after = self._read_start_ticks(pid)
            expected_pipe = pipe_directory.resolve(strict=True)
        except (
            BrokerError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            raise BrokerError(
                "mps_control_unavailable",
                f"MPS {kind} declarer identity is unavailable",
            ) from exc
        if (
            isinstance(start_before, bool)
            or not isinstance(start_before, int)
            or start_before <= 0
            or start_before != start_after
            or cgroup_before != cgroup_after
            or environment_before != environment_after
            or self._environment_gpu_uuids(environment_before)
            != frozenset({uuid})
            or reported_pipe_before != expected_pipe
            or reported_pipe_after != expected_pipe
            or self._pipe_identity(reported_pipe_after) != pipe_identity
        ):
            raise BrokerError(
                "mps_control_unavailable",
                f"MPS {kind} declarer identity changed during audit",
            )
        return SystemdGpuDeclarer(
            pid=pid,
            process_start_ticks=start_before,
            process_cgroup=self._unified_cgroup_path(cgroup_after),
            gpu_uuids=frozenset({uuid}),
        )

    @staticmethod
    def _environment_gpu_uuids(
        environment: dict[str, str],
    ) -> frozenset[str]:
        declared: set[str] = set()
        for name in ("CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES"):
            value = environment.get(name)
            if value is None:
                continue
            normalized = value.strip()
            if normalized.lower() in {"", "none", "void"}:
                continue
            if normalized.lower() == "all":
                declared.update(EXPECTED_GPU_UUIDS.values())
            else:
                try:
                    declared.update(
                        _resolve_gpu_claim_tokens(normalized.split(","))
                    )
                except ValueError as exc:
                    raise BrokerError(
                        "mps_control_unavailable",
                        "MPS process GPU environment is outside governance",
                    ) from exc
        return frozenset(declared)

    @staticmethod
    def _resolve_process_authority_path(raw: str, pid: int) -> Path:
        if not isinstance(raw, str) or not raw.startswith("/"):
            raise ValueError("MPS process authority path is invalid")
        self_prefix = "/proc/self/"
        exact_prefix = f"/proc/{pid}/"
        if raw.startswith(self_prefix):
            raw = exact_prefix + raw.removeprefix(self_prefix)
        elif raw.startswith("/proc/") and not raw.startswith(exact_prefix):
            raise ValueError("MPS process authority belongs to another process")
        return Path(raw).resolve(strict=True)

    @staticmethod
    def _pipe_identity(
        pipe_directory: Path,
    ) -> tuple[
        tuple[int, int, int, int, int],
        tuple[int, int, int, int, int],
    ]:
        try:
            pipe_stat = pipe_directory.lstat()
            control_stat = (pipe_directory / "control").lstat()
        except OSError as exc:
            raise BrokerError(
                "mps_control_unavailable",
                "MPS pipe identity is unsafe",
            ) from exc
        if (
            not stat.S_ISDIR(pipe_stat.st_mode)
            or stat.S_ISLNK(pipe_stat.st_mode)
            or pipe_stat.st_uid != 1001
            or pipe_stat.st_gid != 1001
            or stat.S_IMODE(pipe_stat.st_mode) != 0o700
            or stat.S_ISLNK(control_stat.st_mode)
            or not (
                stat.S_ISSOCK(control_stat.st_mode)
                or stat.S_ISFIFO(control_stat.st_mode)
            )
            or control_stat.st_nlink != 1
            or control_stat.st_uid != 1001
            or control_stat.st_gid != 1001
            or stat.S_IMODE(control_stat.st_mode) not in {0o600, 0o666}
        ):
            raise BrokerError(
                "mps_control_unavailable",
                "MPS pipe identity is unsafe",
            )
        # This CAS rejects persistent replacement and aliasing. NVIDIA's
        # control CLI accepts only a pathname, not an already-open endpoint, so
        # a malicious same-UID process could still perform an ABA swap entirely
        # inside one command. UID1001 is therefore the explicit local trust
        # boundary; workload namespaces must not receive writable authority over
        # this directory.
        return (
            (
                pipe_stat.st_dev,
                pipe_stat.st_ino,
                stat.S_IFMT(pipe_stat.st_mode) | stat.S_IMODE(pipe_stat.st_mode),
                pipe_stat.st_uid,
                pipe_stat.st_gid,
            ),
            (
                control_stat.st_dev,
                control_stat.st_ino,
                stat.S_IFMT(control_stat.st_mode)
                | stat.S_IMODE(control_stat.st_mode),
                control_stat.st_uid,
                control_stat.st_gid,
            ),
        )

    @classmethod
    def _pipe_ready(cls, pipe_directory: Path) -> bool:
        try:
            cls._pipe_identity(pipe_directory)
        except BrokerError:
            return False
        return True

    def _discover_control_pid(
        self,
        pipe_directory: Path,
        expected_uuid: str,
    ) -> int:
        """Discover NVIDIA's daemon when it does not publish a PID file."""

        try:
            entries = list(self.proc_root.iterdir())
        except OSError as exc:
            raise BrokerError(
                "mps_control_unavailable",
                "MPS control process inventory is unavailable",
            ) from exc
        if len(entries) > 1_000_000:
            raise BrokerError(
                "mps_control_unavailable",
                "MPS control process inventory is oversized",
            )
        candidates: list[int] = []
        for entry in entries:
            if not entry.name.isdecimal() or entry.name.startswith("0"):
                continue
            pid = int(entry.name)
            try:
                cgroup = self._validate_process_identity(
                    pid,
                    self.control_executable,
                    kind="control",
                )
                environment = self._read_process_environment(pid)
                reported_pipe = self._resolve_process_authority_path(
                    environment["CUDA_MPS_PIPE_DIRECTORY"],
                    pid,
                )
                expected_pipe = pipe_directory.resolve(strict=True)
            except (
                BrokerError,
                KeyError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ):
                continue
            if (
                _cgroup_has_scoped_path(cgroup)
                and environment.get("CUDA_VISIBLE_DEVICES") == expected_uuid
                and reported_pipe == expected_pipe
            ):
                candidates.append(pid)
        if len(candidates) != 1:
            raise BrokerError(
                "mps_control_unavailable",
                "MPS control process authority is ambiguous",
            )
        return candidates[0]

    def _control_pid(
        self,
        pipe_directory: Path,
        expected_uuid: str,
    ) -> int:
        path = pipe_directory / "nvidia-cuda-mps-control.pid"
        descriptor = -1
        try:
            declared_before = path.lstat()
        except FileNotFoundError:
            return self._discover_control_pid(
                pipe_directory,
                expected_uuid,
            )
        try:
            if (
                stat.S_ISLNK(declared_before.st_mode)
                or not stat.S_ISREG(declared_before.st_mode)
                or declared_before.st_nlink != 1
                or declared_before.st_uid != 1001
                or declared_before.st_gid != 1001
                or stat.S_IMODE(declared_before.st_mode) & 0o022
                or declared_before.st_size > 32
            ):
                raise OSError("unsafe MPS control PID file")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            opened_before = os.fstat(descriptor)
            raw = os.pread(descriptor, 33, 0)
            opened_after = os.fstat(descriptor)
            declared_after = path.lstat()
            snapshots = (
                declared_before,
                opened_before,
                opened_after,
                declared_after,
            )
            identities = {
                (
                    item.st_dev,
                    item.st_ino,
                    item.st_mode,
                    item.st_uid,
                    item.st_gid,
                    item.st_nlink,
                    item.st_size,
                    item.st_mtime_ns,
                    item.st_ctime_ns,
                )
                for item in snapshots
            }
            if len(identities) != 1:
                raise OSError("MPS control PID file changed")
            value = raw.decode("ascii")
            if not re.fullmatch(r"[1-9][0-9]*\n?", value):
                raise ValueError("invalid MPS control PID")
            pid = int(value.strip())
        except (OSError, UnicodeError, ValueError) as exc:
            raise BrokerError(
                "mps_control_unavailable",
                "MPS control PID authority is unavailable",
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return pid

    def _server_list(
        self,
        index: int,
        *,
        deadline: float | None = None,
    ) -> frozenset[int]:
        output = self._run_control(
            index,
            "get_server_list",
            deadline=deadline,
        )
        if output == "":
            return frozenset()
        if output.endswith("\n"):
            body = output[:-1]
        else:
            body = output
        lines = body.split("\n")
        if (
            not lines
            or any(re.fullmatch(r"[1-9][0-9]*", line) is None for line in lines)
        ):
            raise BrokerError(
                "mps_control_unavailable",
                "MPS server list is invalid",
            )
        pids = frozenset(int(line) for line in lines)
        if len(pids) != len(lines):
            raise BrokerError(
                "mps_control_unavailable",
                "MPS server list is duplicated",
            )
        return pids

    def _validate_process_identity(
        self,
        pid: int,
        executable: Path,
        *,
        kind: str,
    ) -> str:
        try:
            start_before = self._read_start_ticks(pid)
            status = (self.proc_root / str(pid) / "status").read_text(
                encoding="ascii"
            )
            expected_lstat = executable.lstat()
            expected_stat = executable.stat()
            actual_stat = (self.proc_root / str(pid) / "exe").stat()
            cgroup = self._read_process_cgroup(pid)
            start_after = self._read_start_ticks(pid)
        except (OSError, UnicodeError, BrokerError) as exc:
            raise BrokerError(
                "mps_control_unavailable",
                f"MPS {kind} process authority is unavailable",
            ) from exc
        identities: dict[str, tuple[int, ...]] = {}
        for name in ("Uid", "Gid"):
            line = next(
                (item for item in status.splitlines() if item.startswith(f"{name}:")),
                None,
            )
            try:
                values = tuple(
                    int(value) for value in line.split(":", 1)[1].split()
                )
            except (AttributeError, IndexError, ValueError) as exc:
                raise BrokerError(
                    "mps_control_unavailable",
                    f"MPS {kind} process credentials are invalid",
                ) from exc
            identities[name] = values
        if (
            identities["Uid"] != (1001, 1001, 1001, 1001)
            or identities["Gid"] != (1001, 1001, 1001, 1001)
            or start_before != start_after
            or not isinstance(start_before, int)
            or isinstance(start_before, bool)
            or start_before <= 0
            or stat.S_ISLNK(expected_lstat.st_mode)
            or not stat.S_ISREG(expected_stat.st_mode)
            or expected_stat.st_uid != 0
            or expected_stat.st_gid != 0
            or expected_stat.st_nlink != 1
            or stat.S_IMODE(expected_stat.st_mode) & 0o022
            or (actual_stat.st_dev, actual_stat.st_ino)
            != (expected_stat.st_dev, expected_stat.st_ino)
        ):
            raise BrokerError(
                "mps_control_unavailable",
                f"MPS {kind} process authority differs",
            )
        return cgroup

    def orphan_client_alive(self, lease: Lease) -> bool:
        # If the control channel has disappeared, the Broker cannot prove that
        # an MPS client is gone and therefore retains the reservation.
        if not self(lease.gpu_index, lease.gpu_uuid):
            return True
        try:
            return any(
                self._device_matches(client.device_uuid, lease.gpu_uuid)
                and self._client_belongs_to_lease(client, lease)
                for client in self._query_clients(lease.gpu_index)
            )
        except BrokerError:
            return True

    def lease_client_alive(self, lease: Lease) -> bool:
        if not self(lease.gpu_index, lease.gpu_uuid):
            raise BrokerError(
                "mps_control_unavailable",
                "leased GPU MPS control channel is unavailable",
            )
        return bool(
            self._strict_lease_clients(
                lease,
                self._query_clients(lease.gpu_index),
            )
        )

    def unmanaged_client_alive(
        self,
        index: int,
        uuid: str,
        leases: tuple[Lease, ...],
        *,
        deadline: float | None = None,
    ) -> bool:
        """Return true when MPS reports a client outside all live reservations."""

        _ensure_admission_open(deadline)
        if not self(index, uuid):
            raise BrokerError(
                "mps_control_unavailable",
                "GPU MPS control channel is unavailable during allocation audit",
            )
        clients = self._query_clients(index, deadline=deadline)
        for client in clients:
            if not self._device_matches(client.device_uuid, uuid):
                raise BrokerError(
                    "mps_control_unavailable",
                    "per-GPU MPS server reported a client on an unexpected device",
                )
            if not any(
                lease.gpu_uuid == uuid
                and self._client_belongs_to_lease(client, lease)
                for lease in leases
            ):
                return True
        _ensure_admission_open(deadline)
        return False

    def lease_client_alive_after_grace(
        self,
        lease: Lease,
        *,
        timeout_seconds: float = 5.0,
    ) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if not self.lease_client_alive(lease):
                return False
            if time.monotonic() >= deadline:
                return True
            time.sleep(0.1)

    def parented_execution_safe_to_release(
        self,
        child: Lease,
        parent: Lease,
        live_leases: tuple[Lease, ...],
    ) -> bool:
        """Prove a logical DFT execution did not introduce an alien client.

        The resident executor intentionally remains an MPS client after each
        attempt. A parented execution lease is a zero-accounting attempt fence:
        it does not own a process or require whole-GPU exclusivity. Every MPS
        client must instead map to some live, unparented governed workload on
        the card. This admits a co-resident Backend/DFT/MD set while preserving
        fail-closed handling for an unmanaged client.
        """

        if (
            child.parent_lease_id != parent.lease_id
            or child.gpu_uuid != parent.gpu_uuid
            or child.gpu_index != parent.gpu_index
            or (
                child.workload_pid,
                child.workload_process_start_ticks,
                child.workload_process_group_id,
                child.workload_cgroup,
            )
            != (
                parent.workload_pid,
                parent.workload_process_start_ticks,
                parent.workload_process_group_id,
                parent.workload_cgroup,
            )
        ):
            return False
        if not self(parent.gpu_index, parent.gpu_uuid):
            raise BrokerError(
                "mps_control_unavailable",
                "resident DFT MPS control channel is unavailable",
            )
        clients = self._query_clients(parent.gpu_index)
        return all(
            self._device_matches(client.device_uuid, parent.gpu_uuid)
            and any(
                authority.parent_lease_id is None
                and authority.gpu_uuid == parent.gpu_uuid
                and authority.status in {"active", "suspect", "terminating"}
                and authority.workload_pid is not None
                and self._client_belongs_to_lease(client, authority)
                for authority in live_leases
            )
            for client in clients
        )

    def terminate_lease_clients(self, lease: Lease) -> tuple[int, ...]:
        """Use host PIDs from MPS `ps`, then wait for CUDA_SUCCESS per client."""

        if not self(lease.gpu_index, lease.gpu_uuid):
            raise BrokerError(
                "mps_control_unavailable",
                "leased GPU MPS control channel is unavailable",
            )
        if lease.workload_pid is None or lease.workload_process_group_id is None:
            raise BrokerError(
                "workload_identity_unavailable",
                "execution workload was not registered before MPS termination",
            )
        clients = {
            (client.server_pid, client.client_pid): client
            for client in self._strict_lease_clients(
                lease,
                self._query_clients(lease.gpu_index),
            )
        }
        terminated: list[int] = []
        for server_pid, client_pid in sorted(clients):
            output = self._run_control(
                lease.gpu_index,
                f"terminate_client {server_pid} {client_pid}",
            ).strip()
            # NVIDIA documents `0` as CUDA_SUCCESS. Anything else means the
            # context is not proven INACTIVE and no POSIX signal is safe.
            if output not in {"0", "CUDA_SUCCESS"}:
                raise BrokerError(
                    "mps_termination_failed",
                    f"MPS terminate_client did not return CUDA_SUCCESS for PID {client_pid}",
                )
            terminated.append(client_pid)
        return tuple(terminated)

    def _strict_lease_clients(
        self,
        lease: Lease,
        clients: tuple["MpsClient", ...],
    ) -> tuple["MpsClient", ...]:
        """Classify exact-scope clients without treating unknown as absent."""

        if (
            not isinstance(lease.workload_cgroup, str)
            or not _cgroup_has_scoped_path(lease.workload_cgroup)
        ):
            raise BrokerError(
                "workload_identity_unavailable",
                "lease lacks an exact scoped cgroup for MPS client audit",
            )
        owned: list[MpsClient] = []
        for client in clients:
            if not self._device_matches(client.device_uuid, lease.gpu_uuid):
                raise BrokerError(
                    "mps_control_unavailable",
                    "per-GPU MPS server reported a client on an unexpected device",
                )
            try:
                client_cgroup = _read_cgroup(client.client_pid)
            except (OSError, BrokerError) as exc:
                raise BrokerError(
                    "mps_control_unavailable",
                    "cannot bind an MPS client to its host cgroup",
                ) from exc
            if _cgroup_is_same_or_descendant(
                client_cgroup,
                lease.workload_cgroup,
            ):
                owned.append(client)
        return tuple(owned)

    def _client_belongs_to_lease(self, client: "MpsClient", lease: Lease) -> bool:
        if lease.workload_pid is None:
            return _pid_is_or_descends_from(client.client_pid, lease.owner_pid)
        try:
            client_cgroup = _read_cgroup(client.client_pid)
        except (OSError, BrokerError):
            return False
        if lease.workload_cgroup is None:
            return False
        # Once the workload has been registered, its dedicated scoped cgroup
        # is the authoritative ownership boundary. Process ancestry and PGID
        # are mutable and cannot substitute for membership in the frozen/killed
        # cgroup used by prepare_process_termination().
        return _cgroup_has_scoped_path(
            lease.workload_cgroup
        ) and _cgroup_is_same_or_descendant(client_cgroup, lease.workload_cgroup)

    def pipe_directory(self, index: int) -> Path:
        return self.state_root / f"mps-{index}" / "pipe"

    def _query_clients(
        self,
        index: int,
        *,
        deadline: float | None = None,
    ) -> tuple["MpsClient", ...]:
        output = self._run_control(index, "ps", deadline=deadline)
        # NVIDIA MPS has two observed, exact no-client responses: an empty
        # stdout while a server is alive with zero clients, and a single
        # ``Server not found`` line when no server has been created yet.
        # Do not normalize arbitrary whitespace or extra lines into either
        # trusted state.
        if output == "" or output in {"Server not found", "Server not found\n"}:
            return ()
        lines = output.splitlines()
        if not lines or any(not line.strip() for line in lines):
            raise BrokerError("mps_control_unavailable", "MPS ps response is invalid")
        header = lines[0].split()
        if header != ["PID", "ID", "SERVER", "DEVICE", "NAMESPACE", "COMMAND"]:
            raise BrokerError("mps_control_unavailable", "MPS ps header is invalid")
        if len(lines) == 1:
            raise BrokerError(
                "mps_control_unavailable",
                "MPS ps header-only response is not a canonical idle state",
            )
        clients: list[MpsClient] = []
        for line in lines[1:]:
            fields = line.split(maxsplit=5)
            if len(fields) != 6 or not fields[5]:
                raise BrokerError("mps_control_unavailable", "MPS ps row is invalid")
            try:
                client_pid = int(fields[0])
                client_id = int(fields[1])
                server_pid = int(fields[2])
                namespace_id = int(fields[4])
            except ValueError as exc:
                raise BrokerError("mps_control_unavailable", "MPS ps PID is invalid") from exc
            if min(client_pid, server_pid, namespace_id) <= 0 or client_id < 0:
                raise BrokerError("mps_control_unavailable", "MPS ps identity is invalid")
            device_uuid = fields[3]
            if not device_uuid.startswith("GPU-"):
                raise BrokerError("mps_control_unavailable", "MPS ps device is invalid")
            clients.append(
                MpsClient(
                    client_pid=client_pid,
                    client_id=client_id,
                    server_pid=server_pid,
                    device_uuid=device_uuid,
                    namespace_id=namespace_id,
                    command=fields[5],
                )
            )
        if len(frozenset(clients)) != len(clients):
            raise BrokerError(
                "mps_control_unavailable",
                "MPS ps response contains a duplicate client identity",
            )
        return tuple(clients)

    @staticmethod
    def _device_matches(reported_uuid: str, expected_uuid: str) -> bool:
        return _mps_device_matches(reported_uuid, expected_uuid)

    def _run_control(
        self,
        index: int,
        command: str,
        *,
        deadline: float | None = None,
    ) -> str:
        try:
            completed = self._run(
                [str(self.control_executable)],
                input=command + "\n",
                check=False,
                capture_output=True,
                text=True,
                timeout=_remaining_admission_seconds(
                    deadline,
                    maximum=5.0,
                ),
                env={
                    "LC_ALL": "C",
                    "CUDA_MPS_PIPE_DIRECTORY": str(
                        self.pipe_directory(index)
                    ),
                },
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BrokerError("mps_control_unavailable", "MPS control query failed") from exc
        if (
            completed.returncode != 0
            or completed.stderr
            or len(completed.stdout) > 1024 * 1024
        ):
            raise BrokerError("mps_control_unavailable", "MPS control query failed")
        _ensure_admission_open(deadline)
        return completed.stdout


@dataclass(frozen=True, slots=True)
class MpsClient:
    client_pid: int
    client_id: int
    server_pid: int
    device_uuid: str
    namespace_id: int
    command: str


class BrokerRequestHandler(socketserver.StreamRequestHandler):
    server: "GpuBrokerUnixServer"

    def handle(self) -> None:
        try:
            raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if not raw or len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                raise BrokerError("invalid_request", "request is empty, oversized, or unterminated")
            try:
                request = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise BrokerError("invalid_request", "request must be UTF-8 JSON") from exc
            if not isinstance(request, dict) or request.get("schema_version") != 1:
                raise BrokerError("invalid_request", "unsupported request schema")
            owner = self._peer_owner()
            result = self.server.dispatch(request, owner=owner)
            response = {"ok": True, "result": result}
        except BrokerError as exc:
            response = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        except Exception:
            logger.exception("unhandled GPU Broker request failure")
            response = {
                "ok": False,
                "error": {"code": "internal_error", "message": "GPU broker internal error"},
            }
        self.wfile.write(
            json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )

    def _peer_owner(self) -> OwnerIdentity:
        if not hasattr(socket, "SO_PEERCRED"):
            raise BrokerError("peer_credentials_unavailable", "SO_PEERCRED is required")
        raw = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        pid, _uid, _gid = struct.unpack("3i", raw)
        return OwnerIdentity(
            pid=pid,
            process_start_ticks=read_process_start_ticks(pid),
            boot_id=read_boot_id(),
        )


class GpuBrokerUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, socket_path: Path, broker: HostGpuBroker) -> None:
        self.socket_path = socket_path
        self.broker = broker
        socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(socket_path.parent, 0o700)
        if socket_path.exists():
            if socket_path.is_socket():
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(0.2)
                try:
                    probe.connect(str(socket_path))
                except OSError:
                    socket_path.unlink()
                else:
                    raise BrokerError("broker_already_running", "GPU broker socket is active")
                finally:
                    probe.close()
            else:
                raise BrokerError("unsafe_socket", "broker socket path is occupied")
        previous_umask = os.umask(0o077)
        try:
            super().__init__(str(socket_path), BrokerRequestHandler)
        finally:
            os.umask(previous_umask)
        os.chmod(socket_path, 0o600)

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            try:
                self.socket_path.unlink()
            except FileNotFoundError:
                pass

    def dispatch(self, request: dict[str, Any], *, owner: OwnerIdentity) -> object:
        action = request.get("action")
        if action == "status":
            return self.broker.status()
        if action == "drain":
            if not isinstance(request.get("draining"), bool):
                raise BrokerError("invalid_request", "draining must be boolean")
            return self.broker.set_draining(request["draining"])
        if action == "acquire":
            lease = self.broker.acquire(
                request_id=_string(request, "request_id"),
                kind=_string(request, "kind"),
                placement=_string(request, "placement"),
                component=_string(request, "component"),
                environment=_string(request, "environment"),
                client_id=_string(request, "client_id"),
                owner=owner,
                memory_mib=_integer(request, "memory_mib"),
                thread_percent=_integer(request, "thread_percent"),
                wait_timeout_seconds=_number(request, "wait_timeout_seconds", default=0.0),
                parent_lease_id=_optional_string(request, "parent_lease_id"),
            )
            return lease.public_dict()
        if action == "cancel_acquire":
            return {
                "cancelled": self.broker.cancel_acquire(
                    _string(request, "request_id"), owner=owner
                )
            }
        if action in {"activate", "register", "heartbeat"}:
            method = self.broker.activate if action in {"activate", "register"} else self.broker.heartbeat
            lease = method(
                _string(request, "lease_id"),
                _integer(request, "fencing_token"),
                owner=owner,
            )
            return lease.public_dict()
        if action == "register_workload":
            lease = self.broker.register_workload(
                _string(request, "lease_id"),
                _integer(request, "fencing_token"),
                owner=owner,
                workload_pid=_integer(request, "workload_pid"),
                workload_process_start_ticks=_integer(
                    request, "workload_process_start_ticks"
                ),
                workload_process_group_id=_integer(
                    request, "workload_process_group_id"
                ),
            )
            return lease.public_dict()
        if action == "release":
            self.broker.release(
                _string(request, "lease_id"),
                _integer(request, "fencing_token"),
                owner=owner,
            )
            return {"released": True}
        if action == "quarantine":
            return self.broker.quarantine(
                _string(request, "lease_id"),
                _integer(request, "fencing_token"),
                owner=owner,
                reason=_string(request, "reason"),
            )
        if action == "prepare_process_termination":
            return self.broker.prepare_process_termination(
                _string(request, "lease_id"),
                _integer(request, "fencing_token"),
                owner=owner,
            )
        raise BrokerError("invalid_request", f"unsupported action: {action}")


def _string(request: dict[str, Any], name: str) -> str:
    value = request.get(name)
    if not isinstance(value, str) or not value:
        raise BrokerError("invalid_request", f"{name} must be a non-empty string")
    return value


def _optional_string(request: dict[str, Any], name: str) -> str | None:
    value = request.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise BrokerError("invalid_request", f"{name} must be a non-empty string or null")
    return value


def _integer(request: dict[str, Any], name: str) -> int:
    value = request.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BrokerError("invalid_request", f"{name} must be an integer")
    return value


def _number(request: dict[str, Any], name: str, *, default: float) -> float:
    value = request.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BrokerError("invalid_request", f"{name} must be a number")
    return float(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NexPoly host GPU resource broker")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--external-reservations", type=Path, required=True)
    parser.add_argument("--mps-state-root", type=Path, required=True)
    parser.add_argument("--heartbeat-timeout-seconds", type=float, default=30.0)
    return parser.parse_args()


def process_stable_descriptor_path(path: Path) -> Path:
    """Replace /proc/self with this long-lived Broker's immutable PID."""

    raw = str(path)
    prefix = "/proc/self/fd/"
    if not raw.startswith(prefix):
        if raw.startswith("/proc/"):
            raise BrokerError(
                "invalid_runtime_authority",
                "Broker descriptor authority must be process-local",
            )
        return path
    suffix = raw.removeprefix(prefix)
    descriptor, separator, remainder = suffix.partition("/")
    if (
        not descriptor.isdigit()
        or descriptor.startswith("0")
        or int(descriptor) <= 2
        or (separator and not remainder)
        or (
            separator
            and any(
                part in {"", ".", ".."}
                for part in remainder.split("/")
            )
        )
    ):
        raise BrokerError(
            "invalid_runtime_authority",
            "Broker descriptor authority path is invalid",
        )
    stable = Path(f"/proc/{os.getpid()}/fd/{descriptor}")
    return stable / remainder if separator else stable


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    args = parse_args()
    if os.geteuid() != 1001 or os.getegid() != 1001:
        raise BrokerError(
            "invalid_service_identity",
            "GPU broker must run as the shared 1001:1001 service identity",
        )
    args.socket = process_stable_descriptor_path(args.socket)
    args.external_reservations = process_stable_descriptor_path(
        args.external_reservations
    )
    args.mps_state_root = process_stable_descriptor_path(
        args.mps_state_root
    )
    args.policy = process_stable_descriptor_path(args.policy)
    validate_policy_document(args.policy)
    validate_gpu_inventory(query_gpu_inventory())
    mps_guard = MpsRuntimeGuard(args.mps_state_root)
    cgroup_controller = JobCgroupController()
    external_policy = load_external_reservations(args.external_reservations)
    descriptor_mps_authority = mps_guard.descriptor_authority
    mps_authority_kwargs = (
        {"mps_authority_query": mps_guard.authority_snapshot}
        if descriptor_mps_authority
        else {"authorized_mps_server_pids": mps_guard.authorized_server_pids}
    )
    external_guard = ExternalGpuGuard(
        external_policy,
        unmanaged_mps_client_query=mps_guard.unmanaged_client_alive,
        allow_descriptor_mps_authority=descriptor_mps_authority,
        **mps_authority_kwargs,
    )
    broker = HostGpuBroker(
        args.state,
        heartbeat_timeout_seconds=args.heartbeat_timeout_seconds,
        gpu_runtime_healthy=mps_guard,
        gpu_externally_busy=external_guard,
        orphan_mps_client_alive=mps_guard.orphan_client_alive,
        terminate_mps_clients=mps_guard.terminate_lease_clients,
        mps_clients_alive=mps_guard.lease_client_alive_after_grace,
        resolve_workload_identity=cgroup_controller.resolve_and_assign,
        parented_execution_safe_to_release=(
            mps_guard.parented_execution_safe_to_release
        ),
        validate_workload=cgroup_controller.validate_active,
        freeze_workload=cgroup_controller.freeze,
        kill_workload=cgroup_controller.kill,
        workload_empty=cgroup_controller.empty,
        cleanup_workload=cgroup_controller.cleanup,
    )
    server = GpuBrokerUnixServer(args.socket, broker)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
