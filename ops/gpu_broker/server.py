from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import socket
import socketserver
import stat
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gpu_resource.transient_scope import (
    SCOPE_SLICE,
    scope_control_group,
    scope_unit_name,
    user_manager_control_group,
    validate_lease_id,
)

from .broker import (
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
logger = logging.getLogger("nexpoly_gpu_broker")


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
    registration_id: str | None
    component: str | None
    environment: str | None
    compose_project: str | None
    compose_service: str | None
    gpu_uuids: frozenset[str]


@dataclass(frozen=True, slots=True)
class SystemdGpuClaim:
    unit: str
    main_pid: int
    gpu_uuids: frozenset[str]


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


def load_external_reservations(path: Path) -> ExternalReservationPolicy:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise BrokerError(
            "external_inventory_unavailable",
            "external GPU reservation inventory is missing or unsafe",
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BrokerError(
                "external_inventory_unavailable",
                "external GPU reservation inventory must be a regular file",
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
            with os.fdopen(descriptor, encoding="utf-8") as handle:
                descriptor = -1
                payload = json.load(handle)
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
        allowed_policy = {
            EXPECTED_GPU_UUIDS[index]
            for index in DEVICE_POLICY[(strings[1], strings[0])]
        }
        if not set(gpu_uuids).issubset(allowed_policy):
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
    for unit, raw in raw_systemd.items():
        if (
            not isinstance(unit, str)
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
        managed_systemd[unit] = frozenset(raw["gpu_uuids"])
    return ExternalReservationPolicy(
        blocked_gpu_uuids=frozenset(blocked),
        managed_docker_claims=managed_docker,
        managed_systemd_claims=managed_systemd,
    )


def query_docker_gpu_claims(*, run=subprocess.run) -> tuple[DockerGpuClaim, ...]:
    try:
        listed = run(
            ["docker", "container", "ls", "--quiet", "--no-trunc"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        container_ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        if any(
            len(container_id) != 64
            or any(character not in "0123456789abcdef" for character in container_id)
            for container_id in container_ids
        ):
            raise ValueError("invalid Docker container ID")
        if not container_ids:
            return ()
        inspected = run(
            ["docker", "container", "inspect", *container_ids],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        payload = json.loads(inspected.stdout)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        raise BrokerError(
            "gpu_claim_inventory_unavailable", "Docker GPU claim inventory failed"
        ) from exc
    if not isinstance(payload, list) or len(payload) != len(container_ids):
        raise BrokerError(
            "gpu_claim_inventory_unavailable", "Docker inspect inventory is incomplete"
        )
    claims: list[DockerGpuClaim] = []
    seen_registrations: set[str] = set()
    for raw in payload:
        try:
            container_id = raw["Id"]
            state = raw["State"]
            config = raw["Config"]
            host_config = raw["HostConfig"]
            if (
                container_id not in container_ids
                or state.get("Running") is not True
                or isinstance(state.get("Pid"), bool)
                or not isinstance(state.get("Pid"), int)
                or state["Pid"] <= 0
                or not isinstance(config.get("Labels") or {}, dict)
                or not isinstance(config.get("Env") or [], list)
                or not isinstance(host_config.get("DeviceRequests") or [], list)
            ):
                raise ValueError("invalid Docker inspect identity")
            labels = config.get("Labels") or {}
            device_request_claims: set[str] = set()
            environment_claims: set[str] = set()
            has_gpu_device_request = False
            for request in host_config.get("DeviceRequests") or []:
                if not isinstance(request, dict):
                    raise ValueError("invalid Docker DeviceRequest")
                capabilities = request.get("Capabilities") or []
                is_gpu = request.get("Driver") == "nvidia" or any(
                    isinstance(group, list) and "gpu" in group for group in capabilities
                )
                if not is_gpu:
                    continue
                has_gpu_device_request = True
                device_ids = request.get("DeviceIDs") or []
                if device_ids:
                    device_request_claims.update(_resolve_gpu_claim_tokens(device_ids))
                elif request.get("Count") not in {0, None}:
                    device_request_claims.update(EXPECTED_GPU_UUIDS.values())
            for environment_entry in config.get("Env") or []:
                if not isinstance(environment_entry, str):
                    raise ValueError("invalid Docker environment")
                if not environment_entry.startswith("NVIDIA_VISIBLE_DEVICES="):
                    continue
                value = environment_entry.split("=", 1)[1].strip()
                if value.lower() in {"", "none", "void"}:
                    continue
                if value.lower() == "all":
                    environment_claims.update(EXPECTED_GPU_UUIDS.values())
                else:
                    environment_claims.update(_resolve_gpu_claim_tokens(value.split(",")))
            claimed = (
                device_request_claims
                if has_gpu_device_request
                else environment_claims
            )
            if not claimed:
                continue
            registration_id = labels.get("com.nexpoly.gpu.registration")
            if registration_id is not None:
                if not isinstance(registration_id, str) or not registration_id:
                    raise ValueError("invalid managed registration label")
                if registration_id in seen_registrations:
                    raise ValueError("duplicate managed registration label")
                seen_registrations.add(registration_id)
            claims.append(
                DockerGpuClaim(
                    container_id=container_id,
                    init_pid=state["Pid"],
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
                "gpu_claim_inventory_unavailable", "Docker GPU claim is invalid"
            ) from exc
    return tuple(claims)


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


def _read_systemd_environment_file(path: Path) -> dict[str, str]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise BrokerError(
            "gpu_claim_inventory_unavailable", "systemd EnvironmentFile is unsafe"
        )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise BrokerError(
            "gpu_claim_inventory_unavailable", "systemd EnvironmentFile is unreadable"
        ) from exc
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
    return result


def query_systemd_gpu_claims(
    *,
    run=subprocess.run,
    read_process_environment=_read_process_environment,
) -> tuple[SystemdGpuClaim, ...]:
    """Inventory active user/system services that declare visible GPUs."""

    claims: dict[str, set[str]] = {}
    main_pids: dict[str, int] = {}
    for scope in ("user", "system"):
        prefix = ["systemctl", "--user"] if scope == "user" else ["systemctl"]
        try:
            listed = run(
                [
                    *prefix,
                    "list-units",
                    "--type=service",
                    "--state=running",
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
                f"cannot query active {scope} systemd services",
            ) from exc
        if listed.returncode != 0:
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                f"cannot query active {scope} systemd services",
            )
        units: set[str] = set()
        for line in listed.stdout.splitlines():
            if not line.strip():
                continue
            unit = line.split(None, 1)[0]
            if not unit.endswith(".service"):
                raise BrokerError(
                    "gpu_claim_inventory_unavailable",
                    "systemd service inventory is invalid",
                )
            units.add(unit)
        if not units:
            continue
        try:
            shown = run(
                [
                    *prefix,
                    "show",
                    "--property=Id",
                    "--property=MainPID",
                    "--property=Environment",
                    "--property=EnvironmentFiles",
                    *sorted(units),
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
        if len(blocks) != len(units):
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "systemd GPU declaration response is incomplete",
            )
        seen_units: set[str] = set()
        for block in blocks:
            properties: dict[str, str] = {}
            for line in block.splitlines():
                if "=" not in line:
                    raise BrokerError(
                        "gpu_claim_inventory_unavailable",
                        "systemd GPU declaration response is invalid",
                    )
                name, value = line.split("=", 1)
                properties[name] = value
            if set(properties) != {
                "Id",
                "MainPID",
                "Environment",
                "EnvironmentFiles",
            }:
                raise BrokerError(
                    "gpu_claim_inventory_unavailable",
                    "systemd GPU declaration response is incomplete",
                )
            unit = properties["Id"]
            if unit not in units or unit in seen_units:
                raise BrokerError(
                    "gpu_claim_inventory_unavailable",
                    "systemd GPU declaration identity is invalid",
                )
            seen_units.add(unit)
            if not properties["MainPID"].isdigit():
                raise BrokerError(
                    "gpu_claim_inventory_unavailable", "systemd MainPID is invalid"
                )
            main_pid = int(properties["MainPID"])
            main_pids[unit] = main_pid
            try:
                environment_entries = shlex.split(properties["Environment"])
                environment_file_entries = shlex.split(
                    properties["EnvironmentFiles"]
                )
            except ValueError as exc:
                raise BrokerError(
                    "gpu_claim_inventory_unavailable",
                    "systemd Environment declaration is invalid",
                ) from exc
            configured: dict[str, str] = {}
            for entry in environment_entries:
                if "=" not in entry:
                    raise BrokerError(
                        "gpu_claim_inventory_unavailable",
                        "systemd Environment declaration is invalid",
                    )
                name, value = entry.split("=", 1)
                configured[name] = value
            file_paths = [
                entry.removeprefix("-")
                for entry in environment_file_entries
                if entry.startswith("/") or entry.startswith("-/")
            ]
            unparsed_file_tokens = [
                entry
                for entry in environment_file_entries
                if not (
                    entry.startswith("/")
                    or entry.startswith("-/")
                    or entry.startswith("(ignore_errors=")
                )
            ]
            if unparsed_file_tokens:
                raise BrokerError(
                    "gpu_claim_inventory_unavailable",
                    "systemd EnvironmentFiles declaration is invalid",
                )
            for path in file_paths:
                configured.update(_read_systemd_environment_file(Path(path)))
            relevant_names = ("CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES")
            if not any(name in configured for name in relevant_names):
                continue
            if main_pid <= 0:
                raise BrokerError(
                    "gpu_claim_inventory_unavailable",
                    "GPU-declaring systemd service has no live MainPID",
                )
            live_environment = read_process_environment(main_pid)
            for name in relevant_names:
                matches = [
                    value.strip()
                    for value in (
                        configured.get(name),
                        live_environment.get(name),
                    )
                    if value is not None
                ]
                for value in matches:
                    if value.lower() in {"", "none", "void"}:
                        continue
                    if value.lower() == "all":
                        claims.setdefault(unit, set()).update(
                            EXPECTED_GPU_UUIDS.values()
                        )
                    else:
                        try:
                            claims.setdefault(unit, set()).update(
                                _resolve_gpu_claim_tokens(value.split(","))
                            )
                        except ValueError as exc:
                            raise BrokerError(
                                "gpu_claim_inventory_unavailable",
                                "systemd GPU declaration is outside governance",
                            ) from exc
    return tuple(
        SystemdGpuClaim(unit=unit, main_pid=main_pids[unit], gpu_uuids=frozenset(uuids))
        for unit, uuids in sorted(claims.items())
    )


def validate_policy_document(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise BrokerError("invalid_policy", "GPU policy document is missing or unsafe")
    try:
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


def query_compute_processes() -> dict[str, frozenset[int]]:
    try:
        completed = subprocess.run(
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


class ExternalGpuGuard:
    """Blocks statically registered claims and unowned live CUDA processes."""

    def __init__(
        self,
        policy: ExternalReservationPolicy,
        *,
        process_query=query_compute_processes,
        docker_claim_query=query_docker_gpu_claims,
        systemd_claim_query=query_systemd_gpu_claims,
        unmanaged_mps_client_query=None,
        cache_seconds: float = 0.2,
    ) -> None:
        self.policy = policy
        self._process_query = process_query
        self._docker_claim_query = docker_claim_query
        self._systemd_claim_query = systemd_claim_query
        self._unmanaged_mps_client_query = unmanaged_mps_client_query
        self._cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._cached: dict[str, frozenset[int]] | None = None
        self._cached_docker_claims: tuple[DockerGpuClaim, ...] | None = None
        self._cached_systemd_claims: tuple[SystemdGpuClaim, ...] | None = None

    def __call__(
        self,
        _index: int,
        uuid: str,
        leases: tuple[Lease, ...],
        owner: OwnerIdentity,
        component: str,
        environment: str,
    ) -> bool:
        if uuid in self.policy.blocked_gpu_uuids:
            return True
        try:
            processes, docker_claims, systemd_claims = self._inventories()
            if (
                self._unmanaged_mps_client_query is not None
                and self._unmanaged_mps_client_query(_index, uuid, leases)
            ):
                return True
        except Exception:
            # Allocation fails closed if host process visibility is lost.
            return True
        for claim in docker_claims:
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
                # The managed MD container is a CPU-only supervisor while it
                # is idle.  Its exact immutable DeviceRequest may expose every
                # policy candidate without owning capacity; per-job execution
                # leases own all CUDA/MPS clients.  Unknown or mismatched
                # registrations still fail above, and the process/MPS audits
                # below reject a supervisor that creates a CUDA client without
                # such a live lease.  No other component receives this narrow
                # declaration-only exception.
                if registration.component != "md":
                    return True
        for claim in systemd_claims:
            if uuid not in claim.gpu_uuids:
                continue
            if self.policy.managed_systemd_claims.get(claim.unit) != claim.gpu_uuids:
                return True
            eligible_owners = [
                lease.owner_pid
                for lease in leases
                if lease.gpu_uuid == uuid
            ]
            eligible_owners.append(owner.pid)
            if not any(
                _pid_is_or_descends_from(candidate, claim.main_pid)
                for candidate in eligible_owners
            ):
                return True
        owners = [lease.owner_pid for lease in leases if lease.gpu_uuid == uuid]
        for pid in processes.get(uuid, frozenset()):
            if _is_mps_server(pid):
                continue
            if not any(_pid_is_or_descends_from(pid, owner) for owner in owners):
                return True
        return False

    def _inventories(
        self,
    ) -> tuple[
        dict[str, frozenset[int]],
        tuple[DockerGpuClaim, ...],
        tuple[SystemdGpuClaim, ...],
    ]:
        with self._lock:
            now = time.monotonic()
            if (
                self._cached is None
                or self._cached_docker_claims is None
                or self._cached_systemd_claims is None
                or now - self._cached_at > self._cache_seconds
            ):
                self._cached = self._process_query()
                self._cached_docker_claims = self._docker_claim_query()
                self._cached_systemd_claims = self._systemd_claim_query()
                self._cached_at = now
            return (
                self._cached,
                self._cached_docker_claims,
                self._cached_systemd_claims,
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
        self._require_bound_scope(lease)
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
        if self._unit_status(unit) != expected or not pids:
            raise BrokerError(
                "workload_identity_mismatch",
                "transient GPU scope is not active with a live workload",
            )
        if (
            lease.workload_pid is None
            or lease.workload_process_start_ticks is None
            or lease.workload_process_group_id is None
            or pids != {lease.workload_pid}
        ):
            raise BrokerError(
                "workload_identity_mismatch",
                "active transient GPU scope workload identity differs",
            )
        self._require_process_identity(
            lease.workload_pid,
            lease.workload_process_start_ticks,
            lease.workload_process_group_id,
        )
        return target

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


def _is_mps_server(pid: int) -> bool:
    try:
        comm = Path(f"/proc/{pid}/comm").read_text(encoding="ascii").strip().lower()
        status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
    except OSError:
        return False
    uid_line = next(
        (line for line in status.splitlines() if line.startswith("Uid:")),
        None,
    )
    if uid_line is None:
        return False
    try:
        real_uid = int(uid_line.split(":", 1)[1].split()[0])
    except (ValueError, IndexError):
        return False
    return "nvidia-cuda-mps" in comm and real_uid == 1001


class MpsRuntimeGuard:
    """Fail-closed readiness and orphan-client checks for per-GPU MPS pipes."""

    def __init__(
        self,
        state_root: Path,
        *,
        command: str = "nvidia-cuda-mps-control",
        run=subprocess.run,
    ) -> None:
        self.state_root = state_root
        self.command = command
        self._run = run

    def __call__(self, index: int, uuid: str) -> bool:
        if EXPECTED_GPU_UUIDS.get(index) != uuid:
            return False
        pipe_directory = self.pipe_directory(index)
        if pipe_directory.is_symlink() or not pipe_directory.is_dir():
            return False
        try:
            pipe_stat = pipe_directory.stat()
            control_stat = (pipe_directory / "control").lstat()
        except OSError:
            return False
        if pipe_stat.st_uid != 1001 or control_stat.st_uid != 1001:
            return False
        if stat.S_ISLNK(control_stat.st_mode):
            return False
        return stat.S_ISSOCK(control_stat.st_mode) or stat.S_ISFIFO(control_stat.st_mode)

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
    ) -> bool:
        """Return true when MPS reports a client outside all live reservations."""

        if not self(index, uuid):
            raise BrokerError(
                "mps_control_unavailable",
                "GPU MPS control channel is unavailable during allocation audit",
            )
        clients = self._query_clients(index)
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

    def _query_clients(self, index: int) -> tuple["MpsClient", ...]:
        output = self._run_control(index, "ps")
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
        return tuple(clients)

    @staticmethod
    def _device_matches(reported_uuid: str, expected_uuid: str) -> bool:
        return (
            len(reported_uuid) >= 12
            and reported_uuid.startswith("GPU-")
            and all(character in "0123456789abcdefABCDEF-" for character in reported_uuid[4:])
            and expected_uuid.startswith(reported_uuid)
        )

    def _run_control(self, index: int, command: str) -> str:
        env = os.environ.copy()
        try:
            completed = self._run(
                [self.command],
                input=command + "\n",
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
                env={
                    **env,
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


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    args = parse_args()
    if os.geteuid() != 1001 or os.getegid() != 1001:
        raise BrokerError(
            "invalid_service_identity",
            "GPU broker must run as the shared 1001:1001 service identity",
        )
    validate_policy_document(args.policy)
    validate_gpu_inventory(query_gpu_inventory())
    mps_guard = MpsRuntimeGuard(args.mps_state_root)
    cgroup_controller = JobCgroupController()
    external_policy = load_external_reservations(args.external_reservations)
    external_guard = ExternalGpuGuard(
        external_policy,
        unmanaged_mps_client_query=mps_guard.unmanaged_client_alive,
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
