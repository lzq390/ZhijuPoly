from __future__ import annotations

import json
import os
import re
import socket
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4


MAX_RESPONSE_BYTES = 256 * 1024
GPU_UUIDS = {
    1: "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
    2: "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe",
    3: "GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5",
}
GPU_DEVICE_POLICY = {
    ("prod", "backend"): (2,),
    ("prod", "dft"): (2, 3, 1),
    ("prod", "md"): (2, 3, 1),
    ("dev", "backend"): (1,),
    ("dev", "dft"): (1, 3),
    ("dev", "md"): (1, 3),
}
GPU_COMPONENT_BUDGETS_MIB = {"backend": 8_192, "dft": 4_096, "md": 8_192}
GPU_COMPONENT_THREAD_PERCENT = {"backend": 100, "dft": 50, "md": 50}
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_AUTHORITATIVE_LEASE_LOSS_CODES = frozenset(
    {
        "gpu_lease_lost",
        "unknown_lease",
        "stale_fencing_token",
        "lease_owner_mismatch",
        "invalid_lease_state",
    }
)


class GpuBrokerClientError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GpuLease:
    lease_id: str
    fencing_token: int
    broker_instance_id: str
    kind: str
    placement: str
    component: str
    environment: str
    client_id: str
    gpu_index: int
    gpu_uuid: str
    memory_mib: int
    thread_percent: int
    preferred: bool
    parent_lease_id: str | None
    status: str
    request_id: str = ""
    workload_pid: int | None = None
    workload_process_start_ticks: int | None = None
    workload_process_group_id: int | None = None
    workload_cgroup: str | None = None

    @classmethod
    def from_payload(cls, payload: object) -> "GpuLease":
        if not isinstance(payload, dict):
            raise GpuBrokerClientError("invalid_response", "lease response must be an object")
        required_strings = (
            "lease_id",
            "broker_instance_id",
            "kind",
            "placement",
            "component",
            "environment",
            "client_id",
            "gpu_uuid",
            "status",
        )
        for name in required_strings:
            if not isinstance(payload.get(name), str) or not payload[name]:
                raise GpuBrokerClientError("invalid_response", f"lease {name} is invalid")
        for name in ("fencing_token", "gpu_index", "memory_mib", "thread_percent"):
            value = payload.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise GpuBrokerClientError("invalid_response", f"lease {name} is invalid")
        if not isinstance(payload.get("preferred"), bool):
            raise GpuBrokerClientError("invalid_response", "lease preferred is invalid")
        parent = payload.get("parent_lease_id")
        if parent is not None and (not isinstance(parent, str) or not parent):
            raise GpuBrokerClientError("invalid_response", "lease parent_lease_id is invalid")
        workload_values = (
            payload.get("workload_pid"),
            payload.get("workload_process_start_ticks"),
            payload.get("workload_process_group_id"),
            payload.get("workload_cgroup"),
        )
        if any(value is not None for value in workload_values):
            if (
                not all(value is not None for value in workload_values)
                or any(
                    isinstance(value, bool) or not isinstance(value, int) or value <= 0
                    for value in workload_values[:3]
                )
                or not isinstance(workload_values[3], str)
                or not workload_values[3]
            ):
                raise GpuBrokerClientError(
                    "invalid_response", "lease workload identity is invalid"
                )
        lease = cls(
            lease_id=payload["lease_id"],
            fencing_token=payload["fencing_token"],
            broker_instance_id=payload["broker_instance_id"],
            kind=payload["kind"],
            placement=payload["placement"],
            component=payload["component"],
            environment=payload["environment"],
            client_id=payload["client_id"],
            gpu_index=payload["gpu_index"],
            gpu_uuid=payload["gpu_uuid"],
            memory_mib=payload["memory_mib"],
            thread_percent=payload["thread_percent"],
            preferred=payload["preferred"],
            parent_lease_id=parent,
            status=payload["status"],
            # request_id was added without changing the wire schema.  Reading
            # an older persisted response remains safe because its immutable
            # lease ID is an equally unique recovery key.
            request_id=payload.get("request_id", payload["lease_id"]),
            workload_pid=workload_values[0],
            workload_process_start_ticks=workload_values[1],
            workload_process_group_id=workload_values[2],
            workload_cgroup=workload_values[3],
        )
        lease._validate_policy()
        return lease

    def _validate_policy(self) -> None:
        if self.fencing_token <= 0:
            raise GpuBrokerClientError("invalid_response", "lease fencing token is invalid")
        if _CLIENT_ID_RE.fullmatch(self.client_id) is None:
            raise GpuBrokerClientError("invalid_response", "lease client_id is invalid")
        if _REQUEST_ID_RE.fullmatch(self.request_id) is None:
            raise GpuBrokerClientError("invalid_response", "lease request_id is invalid")
        if self.kind not in {"residency", "execution"}:
            raise GpuBrokerClientError("invalid_response", "lease kind is invalid")
        if self.placement not in {"preferred", "overflow", "any"}:
            raise GpuBrokerClientError("invalid_response", "lease placement is invalid")
        if self.status not in {"reserved", "active", "suspect", "terminating"}:
            raise GpuBrokerClientError("invalid_response", "lease status is invalid")
        policy = GPU_DEVICE_POLICY.get((self.environment, self.component))
        if policy is None:
            raise GpuBrokerClientError("invalid_response", "lease policy identity is invalid")
        if self.gpu_index not in policy or GPU_UUIDS.get(self.gpu_index) != self.gpu_uuid:
            raise GpuBrokerClientError("invalid_response", "lease GPU mapping is invalid")
        if self.memory_mib != GPU_COMPONENT_BUDGETS_MIB[self.component]:
            raise GpuBrokerClientError("invalid_response", "lease memory budget is invalid")
        if self.thread_percent != GPU_COMPONENT_THREAD_PERCENT[self.component]:
            raise GpuBrokerClientError("invalid_response", "lease thread budget is invalid")
        if self.preferred != (self.gpu_index == policy[0]):
            raise GpuBrokerClientError("invalid_response", "lease preferred marker is invalid")
        if self.component == "backend" and (
            self.kind != "residency"
            or self.placement != "preferred"
            or self.parent_lease_id is not None
            or not self.preferred
        ):
            raise GpuBrokerClientError("invalid_response", "Backend lease policy is invalid")
        if self.component == "md" and (
            self.kind != "execution"
            or self.placement != "any"
            or self.parent_lease_id is not None
        ):
            raise GpuBrokerClientError("invalid_response", "MD lease policy is invalid")
        if self.component == "dft":
            if self.kind == "residency" and (
                self.placement != "preferred"
                or self.parent_lease_id is not None
                or not self.preferred
            ):
                raise GpuBrokerClientError(
                    "invalid_response", "DFT residency lease policy is invalid"
                )
            if self.kind == "execution" and self.parent_lease_id is not None and (
                self.placement != "preferred" or not self.preferred
            ):
                raise GpuBrokerClientError(
                    "invalid_response", "DFT resident execution lease policy is invalid"
                )
            if self.kind == "execution" and self.parent_lease_id is None and (
                self.placement != "overflow" or self.preferred
            ):
                raise GpuBrokerClientError(
                    "invalid_response", "DFT overflow lease policy is invalid"
                )


def mps_client_environment(
    lease: GpuLease,
    *,
    pipe_root: str | Path,
) -> dict[str, str]:
    """Build and validate the MPS environment for one fenced lease.

    A separate pipe directory selects the per-card MPS daemon. UUID device
    selection avoids ordinal remapping ambiguity, and the memory/thread values
    are copied from the Broker's fixed reservation rather than client input.
    """

    root = Path(pipe_root)
    if not root.is_absolute():
        raise GpuBrokerClientError(
            "gpu_runtime_unhealthy", "MPS pipe root must be an absolute path"
        )
    pipe_directory = root / f"mps-{lease.gpu_index}" / "pipe"
    if pipe_directory.is_symlink() or not pipe_directory.is_dir():
        raise GpuBrokerClientError(
            "gpu_runtime_unhealthy", "leased GPU MPS pipe directory is unavailable"
        )
    try:
        pipe_stat = pipe_directory.stat()
        control_stat = (pipe_directory / "control").lstat()
    except OSError as exc:
        raise GpuBrokerClientError(
            "gpu_runtime_unhealthy", "leased GPU MPS control channel is unavailable"
        ) from exc
    if pipe_stat.st_uid != os.geteuid() or control_stat.st_uid != os.geteuid():
        raise GpuBrokerClientError(
            "gpu_runtime_unhealthy", "leased GPU MPS channel has an unexpected owner"
        )
    if stat.S_ISLNK(control_stat.st_mode) or not (
        stat.S_ISSOCK(control_stat.st_mode) or stat.S_ISFIFO(control_stat.st_mode)
    ):
        raise GpuBrokerClientError(
            "gpu_runtime_unhealthy", "leased GPU MPS control channel is unsafe"
        )
    priority = "0" if lease.component == "backend" else "1"
    return {
        "CUDA_VISIBLE_DEVICES": lease.gpu_uuid,
        "CUDA_MPS_PIPE_DIRECTORY": str(pipe_directory),
        "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(lease.thread_percent),
        "CUDA_MPS_CLIENT_PRIORITY": priority,
        "CUDA_MPS_PINNED_DEVICE_MEM_LIMIT": (
            f"{lease.gpu_uuid}={lease.memory_mib}M"
        ),
    }


class GpuBrokerClient:
    def __init__(self, socket_path: str | Path, *, timeout_seconds: float = 5.0) -> None:
        self.socket_path = Path(socket_path)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = float(timeout_seconds)

    def status(self) -> dict[str, Any]:
        result = self._request({"action": "status"})
        if not isinstance(result, dict):
            raise GpuBrokerClientError("invalid_response", "status response must be an object")
        return result

    def acquire_managed(
        self,
        *,
        kind: str,
        placement: str,
        component: str,
        environment: str,
        client_id: str,
        memory_mib: int,
        thread_percent: int,
        wait_timeout_seconds: float,
        heartbeat_interval_seconds: float = 5.0,
        parent_lease_id: str | None = None,
        request_id: str | None = None,
    ) -> "ManagedGpuLease":
        stable_request_id = request_id or uuid4().hex
        if _REQUEST_ID_RE.fullmatch(stable_request_id) is None:
            raise ValueError("request_id must contain 1-128 safe characters")
        request: dict[str, Any] = {
            "action": "acquire",
            "request_id": stable_request_id,
            "kind": kind,
            "placement": placement,
            "component": component,
            "environment": environment,
            "client_id": client_id,
            "memory_mib": memory_mib,
            "thread_percent": thread_percent,
            "wait_timeout_seconds": wait_timeout_seconds,
        }
        if parent_lease_id is not None:
            request["parent_lease_id"] = parent_lease_id
        lease = GpuLease.from_payload(
            self._request(request, extra_timeout_seconds=max(0.0, wait_timeout_seconds))
        )
        parent_lease: GpuLease | None = None
        try:
            self._validate_acquire_response(
                lease,
                kind=kind,
                placement=placement,
                component=component,
                environment=environment,
                client_id=client_id,
                memory_mib=memory_mib,
                thread_percent=thread_percent,
                parent_lease_id=parent_lease_id,
                request_id=stable_request_id,
            )
            if parent_lease_id is not None:
                parent_lease = self._read_exact_parent_lease(
                    parent_lease_id,
                    child=lease,
                )
        except GpuBrokerClientError:
            try:
                self.release(lease)
            except GpuBrokerClientError:
                pass
            raise
        try:
            active = GpuLease.from_payload(
                self._request(
                    {
                        "action": "activate",
                        "lease_id": lease.lease_id,
                        "fencing_token": lease.fencing_token,
                    }
                )
            )
            self._validate_lease_update(
                lease,
                active,
                allow_workload_change=(
                    lease.kind == "residency" or parent_lease is not None
                ),
            )
            if parent_lease is not None:
                inherited = (
                    active.workload_pid,
                    active.workload_process_start_ticks,
                    active.workload_process_group_id,
                    active.workload_cgroup,
                )
                expected_parent = (
                    parent_lease.workload_pid,
                    parent_lease.workload_process_start_ticks,
                    parent_lease.workload_process_group_id,
                    parent_lease.workload_cgroup,
                )
                if None in expected_parent or inherited != expected_parent:
                    raise GpuBrokerClientError(
                        "invalid_response",
                        "parented execution did not inherit the exact residency workload",
                    )
            if active.status != "active":
                raise GpuBrokerClientError(
                    "invalid_response", "activated lease did not become active"
                )
        except Exception:
            try:
                self.release(lease)
            except GpuBrokerClientError:
                pass
            raise
        managed = ManagedGpuLease(
            client=self,
            lease=active,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        )
        managed.start()
        return managed

    def _read_exact_parent_lease(
        self,
        parent_lease_id: str,
        *,
        child: GpuLease,
    ) -> GpuLease:
        status = self.status()
        raw_leases = status.get("leases")
        if not isinstance(raw_leases, list):
            raise GpuBrokerClientError(
                "invalid_response", "Broker status lease inventory is invalid"
            )
        matches = [
            raw
            for raw in raw_leases
            if isinstance(raw, dict) and raw.get("lease_id") == parent_lease_id
        ]
        if len(matches) != 1:
            raise GpuBrokerClientError(
                "invalid_response", "parent residency lease is missing or duplicated"
            )
        parent = GpuLease.from_payload(matches[0])
        if (
            parent.kind != "residency"
            or parent.component != child.component
            or parent.environment != child.environment
            or parent.client_id != child.client_id
            or parent.gpu_index != child.gpu_index
            or parent.gpu_uuid != child.gpu_uuid
            or parent.status not in {"active", "suspect"}
        ):
            raise GpuBrokerClientError(
                "invalid_response", "parent residency lease does not match child admission"
            )
        return parent

    def cancel_acquire(self, request_id: str) -> bool:
        """Cancel one stable queued acquire without disturbing other waiters.

        A request that was allocated before the cancellation raced is not
        silently released: the caller receives ``False`` and must recover the
        lease with the same request ID before deciding how to tear it down.
        """

        if _REQUEST_ID_RE.fullmatch(request_id) is None:
            raise ValueError("request_id must contain 1-128 safe characters")
        result = self._request({"action": "cancel_acquire", "request_id": request_id})
        if not isinstance(result, dict) or not isinstance(result.get("cancelled"), bool):
            raise GpuBrokerClientError(
                "invalid_response", "acquire cancellation was not acknowledged"
            )
        return result["cancelled"]

    def heartbeat(self, lease: GpuLease) -> GpuLease:
        updated = GpuLease.from_payload(
            self._request(
                {
                    "action": "heartbeat",
                    "lease_id": lease.lease_id,
                    "fencing_token": lease.fencing_token,
                }
            )
        )
        self._validate_lease_update(lease, updated)
        return updated

    def register_workload(
        self,
        lease: GpuLease,
        *,
        workload_pid: int,
        workload_process_start_ticks: int,
        workload_process_group_id: int,
    ) -> GpuLease:
        registered = GpuLease.from_payload(
            self._request(
                {
                    "action": "register_workload",
                    "lease_id": lease.lease_id,
                    "fencing_token": lease.fencing_token,
                    "workload_pid": workload_pid,
                    "workload_process_start_ticks": workload_process_start_ticks,
                    "workload_process_group_id": workload_process_group_id,
                }
            )
        )
        self._validate_lease_update(lease, registered, allow_workload_change=True)
        if (
            registered.workload_pid is None
            or registered.workload_process_start_ticks != workload_process_start_ticks
            or registered.workload_process_group_id is None
            or registered.workload_cgroup is None
        ):
            raise GpuBrokerClientError(
                "invalid_response", "Broker did not register the workload identity"
            )
        return registered

    def release(self, lease: GpuLease) -> None:
        result = self._request(
            {
                "action": "release",
                "lease_id": lease.lease_id,
                "fencing_token": lease.fencing_token,
            }
        )
        if not isinstance(result, dict) or result.get("released") is not True:
            raise GpuBrokerClientError("invalid_response", "release was not acknowledged")

    def quarantine(self, lease: GpuLease, *, reason: str) -> dict[str, Any]:
        result = self._request(
            {
                "action": "quarantine",
                "lease_id": lease.lease_id,
                "fencing_token": lease.fencing_token,
                "reason": reason,
            }
        )
        if not isinstance(result, dict) or result.get("gpu_uuid") != lease.gpu_uuid:
            raise GpuBrokerClientError("invalid_response", "quarantine was not acknowledged")
        return result

    def prepare_process_termination(self, lease: GpuLease) -> dict[str, Any]:
        result = self._request(
            {
                "action": "prepare_process_termination",
                "lease_id": lease.lease_id,
                "fencing_token": lease.fencing_token,
            }
        )
        if not isinstance(result, dict) or result.get("safe_to_signal") is not True:
            raise GpuBrokerClientError(
                "invalid_response", "MPS termination safety was not acknowledged"
            )
        client_pids = result.get("client_pids")
        if not isinstance(client_pids, list) or any(
            isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
            for pid in client_pids
        ):
            raise GpuBrokerClientError(
                "invalid_response", "MPS termination client PID evidence is invalid"
            )
        if len(set(client_pids)) != len(client_pids):
            raise GpuBrokerClientError(
                "invalid_response", "MPS termination client PID evidence is duplicated"
            )
        prepared_at = result.get("prepared_at")
        if (
            isinstance(prepared_at, bool)
            or not isinstance(prepared_at, (int, float))
            or prepared_at <= 0
        ):
            raise GpuBrokerClientError(
                "invalid_response", "MPS termination timestamp evidence is invalid"
            )
        freeze_token = result.get("freeze_token")
        if not isinstance(freeze_token, str) or not freeze_token:
            raise GpuBrokerClientError(
                "invalid_response", "workload cgroup freeze evidence is invalid"
            )
        return result

    @staticmethod
    def _validate_acquire_response(
        lease: GpuLease,
        *,
        kind: str,
        placement: str,
        component: str,
        environment: str,
        client_id: str,
        memory_mib: int,
        thread_percent: int,
        parent_lease_id: str | None,
        request_id: str,
    ) -> None:
        expected = (
            kind,
            placement,
            component,
            environment,
            client_id,
            memory_mib,
            thread_percent,
            parent_lease_id,
            request_id,
        )
        actual = (
            lease.kind,
            lease.placement,
            lease.component,
            lease.environment,
            lease.client_id,
            lease.memory_mib,
            lease.thread_percent,
            lease.parent_lease_id,
            lease.request_id,
        )
        if actual != expected or lease.status not in {"reserved", "active", "suspect"}:
            raise GpuBrokerClientError(
                "invalid_response", "lease does not match the exact acquire request"
            )

    @staticmethod
    def _validate_lease_update(
        previous: GpuLease,
        updated: GpuLease,
        *,
        allow_workload_change: bool = False,
    ) -> None:
        immutable_fields = (
            "lease_id",
            "fencing_token",
            "broker_instance_id",
            "kind",
            "placement",
            "component",
            "environment",
            "client_id",
            "gpu_index",
            "gpu_uuid",
            "memory_mib",
            "thread_percent",
            "preferred",
            "parent_lease_id",
            "request_id",
        )
        if any(
            getattr(previous, field_name) != getattr(updated, field_name)
            for field_name in immutable_fields
        ):
            raise GpuBrokerClientError(
                "invalid_response", "lease identity changed during Broker update"
            )
        previous_workload = (
            previous.workload_pid,
            previous.workload_process_start_ticks,
            previous.workload_process_group_id,
            previous.workload_cgroup,
        )
        updated_workload = (
            updated.workload_pid,
            updated.workload_process_start_ticks,
            updated.workload_process_group_id,
            updated.workload_cgroup,
        )
        if not allow_workload_change and previous_workload != updated_workload:
            raise GpuBrokerClientError(
                "invalid_response", "lease workload identity changed during Broker update"
            )

    def _request(
        self, payload: dict[str, Any], *, extra_timeout_seconds: float = 0.0
    ) -> object:
        try:
            socket_stat = self.socket_path.lstat()
        except OSError as exc:
            raise GpuBrokerClientError(
                "gpu_broker_unavailable", f"GPU broker socket is unavailable: {self.socket_path}"
            ) from exc
        if stat.S_ISLNK(socket_stat.st_mode) or not stat.S_ISSOCK(socket_stat.st_mode):
            raise GpuBrokerClientError(
                "gpu_broker_unavailable", "GPU broker path is not a Unix socket"
            )
        request = dict(payload)
        request["schema_version"] = 1
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        timeout = self.timeout_seconds + max(0.0, extra_timeout_seconds)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout)
                connection.connect(str(self.socket_path))
                connection.sendall(encoded)
                chunks = bytearray()
                while not chunks.endswith(b"\n"):
                    chunk = connection.recv(min(65_536, MAX_RESPONSE_BYTES + 1 - len(chunks)))
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if len(chunks) > MAX_RESPONSE_BYTES:
                        raise GpuBrokerClientError(
                            "invalid_response", "GPU broker response is oversized"
                        )
        except GpuBrokerClientError:
            raise
        except (OSError, TimeoutError) as exc:
            raise GpuBrokerClientError(
                "gpu_broker_unavailable", "GPU broker request failed"
            ) from exc
        if not chunks.endswith(b"\n"):
            raise GpuBrokerClientError("invalid_response", "GPU broker response is incomplete")
        try:
            response = json.loads(bytes(chunks).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GpuBrokerClientError(
                "invalid_response", "GPU broker response is not UTF-8 JSON"
            ) from exc
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise GpuBrokerClientError("invalid_response", "GPU broker response envelope is invalid")
        if response["ok"] is not True:
            error = response.get("error")
            if not isinstance(error, dict):
                raise GpuBrokerClientError("invalid_response", "GPU broker error is invalid")
            code = error.get("code")
            message = error.get("message")
            if not isinstance(code, str) or not isinstance(message, str):
                raise GpuBrokerClientError("invalid_response", "GPU broker error is invalid")
            raise GpuBrokerClientError(code, message)
        return response.get("result")


class ManagedGpuLease:
    def __init__(
        self,
        *,
        client: GpuBrokerClient,
        lease: GpuLease,
        heartbeat_interval_seconds: float,
    ) -> None:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        self.client = client
        self.lease = lease
        self.heartbeat_interval_seconds = float(heartbeat_interval_seconds)
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._suspect = threading.Event()
        self._lost_error: GpuBrokerClientError | None = None
        self._thread: threading.Thread | None = None
        self._release_thread: threading.Thread | None = None
        self._closed = False
        self._termination_safe = False
        self._termination_unsafe = False
        self._termination_evidence: dict[str, Any] | None = None
        self._lease_lock = threading.Lock()

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    @property
    def suspect(self) -> bool:
        return self._suspect.is_set() and not self._lost.is_set()

    @property
    def connectivity_status(self) -> str:
        if self.lost:
            return "lost"
        if self.suspect:
            return "suspect"
        return "healthy"

    @property
    def last_heartbeat_error(self) -> str | None:
        return str(self._lost_error) if self._lost_error is not None else None

    @property
    def termination_safe(self) -> bool:
        return self._termination_safe

    @property
    def termination_unsafe(self) -> bool:
        return self._termination_unsafe

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"gpu-lease-{self.lease.lease_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def assert_healthy(self) -> None:
        if self.lost and self._lost_error is not None:
            raise GpuBrokerClientError("gpu_lease_lost", str(self._lost_error))
        if self.suspect:
            raise GpuBrokerClientError(
                "gpu_runtime_unhealthy",
                "GPU lease heartbeat is temporarily unconfirmed",
            )

    def confirm_current(self) -> GpuLease:
        """Synchronously confirm the current lease and fencing token.

        The background heartbeat is sufficient for liveness monitoring, but a
        caller accepting a completed GPU result needs a linearizable Broker
        decision *after* the device synchronization.  Reading the cached
        ``lost``/``suspect`` flags leaves up to one heartbeat interval in which
        an unknown or stale lease could still publish a result.

        Serialize this request with the managed heartbeat loop and update the
        same health state so an authoritative loss also fences every later
        operation on this managed lease.
        """
        try:
            with self._lease_lock:
                if self._closed:
                    raise GpuBrokerClientError(
                        "gpu_lease_lost", "GPU lease is already closed"
                    )
                updated = self.client.heartbeat(self.lease)
                if updated.status != "active":
                    raise GpuBrokerClientError(
                        "gpu_lease_lost",
                        "GPU lease is no longer active at the result fencing boundary",
                    )
                self.lease = updated
            self._suspect.clear()
            self._lost_error = None
            return updated
        except GpuBrokerClientError as exc:
            self._lost_error = exc
            if exc.code in _AUTHORITATIVE_LEASE_LOSS_CODES:
                self._lost.set()
                self._suspect.clear()
            else:
                self._suspect.set()
            raise

    def quarantine(self, *, reason: str) -> dict[str, Any]:
        with self._lease_lock:
            if self._closed:
                raise GpuBrokerClientError(
                    "gpu_lease_lost", "GPU lease is already closed"
                )
            return self.client.quarantine(self.lease, reason=reason)

    def register_workload(self, workload_pid: int) -> GpuLease:
        """Fence this lease to a start_new_session child before CUDA import."""

        try:
            process_start_ticks = _read_process_start_ticks(workload_pid)
            process_group_id = os.getpgid(workload_pid)
        except (OSError, ValueError, IndexError) as exc:
            self.fail_closed()
            raise GpuBrokerClientError(
                "workload_identity_unavailable",
                "cannot identify the execution workload process group",
            ) from exc
        if process_group_id != workload_pid:
            self.fail_closed()
            raise GpuBrokerClientError(
                "workload_identity_mismatch",
                "execution workload must use start_new_session=True",
            )
        try:
            with self._lease_lock:
                if self._closed:
                    raise GpuBrokerClientError(
                        "gpu_lease_lost", "GPU lease is already closed"
                    )
                registered = self.client.register_workload(
                    self.lease,
                    workload_pid=workload_pid,
                    workload_process_start_ticks=process_start_ticks,
                    workload_process_group_id=process_group_id,
                )
                self.lease = registered
        except GpuBrokerClientError:
            self.fail_closed()
            raise
        return registered

    def prepare_process_termination(self) -> dict[str, Any]:
        if self._termination_unsafe:
            raise GpuBrokerClientError(
                "gpu_runtime_unhealthy",
                "MPS termination safety was previously not established",
            )
        try:
            with self._lease_lock:
                if self._closed:
                    raise GpuBrokerClientError(
                        "gpu_lease_lost", "GPU lease is already closed"
                    )
                result = self.client.prepare_process_termination(self.lease)
        except GpuBrokerClientError:
            self.fail_closed()
            raise
        self._termination_safe = True
        self._termination_evidence = dict(result)
        return result

    def fail_closed(self) -> None:
        self._termination_unsafe = True
        self.abandon()

    def close(self) -> None:
        error: GpuBrokerClientError | None = None
        with self._lease_lock:
            if self._closed:
                return
            # Linearize close against result fencing before sending release.
            # A confirmation that wins this lock is current before close; one
            # that loses observes _closed and cannot publish a result after the
            # release decision.
            self._closed = True
            self._stop.set()
            try:
                self.client.release(self.lease)
            except GpuBrokerClientError as exc:
                if exc.code in {
                    "unknown_lease",
                    "stale_fencing_token",
                    "lease_owner_mismatch",
                }:
                    # Capacity is already gone or fenced away; there is nothing
                    # this owner may safely release.
                    pass
                elif exc.code == "gpu_runtime_unhealthy":
                    # The Broker observed an MPS client or could not prove the
                    # inventory empty. Never retry a release that may overbook.
                    self._termination_unsafe = True
                    error = exc
                else:
                    self._release_thread = threading.Thread(
                        target=self._release_retry_loop,
                        name=f"gpu-release-{self.lease.lease_id[:8]}",
                        daemon=True,
                    )
                    self._release_thread.start()
                    if not self.lost:
                        error = exc
        if self._thread is not None:
            self._thread.join(timeout=self.heartbeat_interval_seconds + 1.0)
        if error is not None:
            raise error

    def abandon(self) -> None:
        """Stop heartbeats without releasing capacity.

        Residency owners use this during process teardown.  The host broker
        keeps the reservation until the exact PID/start-time identity is dead,
        avoiding a window where CUDA memory is still resident but unaccounted.
        """
        with self._lease_lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.heartbeat_interval_seconds + 1.0)

    def __enter__(self) -> "ManagedGpuLease":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval_seconds):
            try:
                with self._lease_lock:
                    if self._closed:
                        return
                    self.lease = self.client.heartbeat(self.lease)
                self._suspect.clear()
                self._lost_error = None
            except GpuBrokerClientError as exc:
                self._lost_error = exc
                if exc.code in _AUTHORITATIVE_LEASE_LOSS_CODES:
                    self._lost.set()
                    self._suspect.clear()
                    return
                # Socket failures and other non-authoritative responses do
                # not prove fencing.  Continue heartbeating while exposing a
                # fail-closed suspect state to readiness and dispatch paths.
                self._suspect.set()

    def _release_retry_loop(self) -> None:
        # Preserve fail-closed accounting while the owner remains alive, then
        # release as soon as a temporarily unavailable Broker returns.
        while True:
            try:
                self.client.release(self.lease)
                return
            except GpuBrokerClientError as exc:
                if exc.code in {
                    "unknown_lease",
                    "stale_fencing_token",
                    "lease_owner_mismatch",
                }:
                    return
                if exc.code == "gpu_runtime_unhealthy":
                    # The Broker retained and quarantined this reservation.
                    # Retrying could only obscure the first unsafe-release
                    # evidence; capacity must remain accounted fail-closed.
                    self._termination_unsafe = True
                    return
                threading.Event().wait(self.heartbeat_interval_seconds)


def _read_process_start_ticks(pid: int) -> int:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    closing_parenthesis = raw.rfind(")")
    if closing_parenthesis < 0:
        raise ValueError("process stat comm field is invalid")
    fields_after_comm = raw[closing_parenthesis + 2 :].split()
    return int(fields_after_comm[19])
