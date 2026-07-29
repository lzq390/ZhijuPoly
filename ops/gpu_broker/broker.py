from __future__ import annotations

from collections import deque
import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Callable, Iterable, Mapping
from uuid import uuid4


logger = logging.getLogger("nexpoly_gpu_broker")


GPU_TOTAL_BUDGET_MIB = 20_736
COMPONENT_BUDGETS_MIB = {
    "backend": 8_192,
    "dft": 4_096,
    "md": 8_192,
}
COMPONENT_THREAD_PERCENT = {
    "backend": 100,
    "dft": 50,
    "md": 50,
}
EXPECTED_GPU_UUIDS = {
    1: "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
    2: "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe",
    3: "GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5",
}

# Candidate order is a host policy, not client input.  GPU0 is intentionally
# absent and can never be selected by this broker.
BASE_DEVICE_POLICY = {
    ("prod", "backend"): (2,),
    ("prod", "dft"): (2, 3, 1),
    ("prod", "md"): (2, 3, 1),
    ("dev", "backend"): (1,),
    ("dev", "dft"): (1, 3),
    ("dev", "md"): (1, 3),
}
DEVICE_POLICY = dict(BASE_DEVICE_POLICY)
if os.environ.get("NEXPOLY_GPU1_ONLY_SESSION") == "1":
    # Process-local restriction used only by the opt-in development
    # controller.  The repository policy and production candidates remain
    # untouched, while this Broker process cannot place any dev lease on GPU3.
    DEVICE_POLICY = {
        **DEVICE_POLICY,
        ("dev", "backend"): (1,),
        ("dev", "dft"): (1,),
        ("dev", "md"): (1,),
    }

_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
QUARANTINE_REASONS = frozenset(
    {
        "gpu_fatal",
        "gpu_xid",
        "gpu_ecc_uncorrectable",
        "gpu_runtime_corruption",
    }
)
# Keep the worst-case status response comfortably below the wire client's
# 256-KiB fail-closed cap while retaining far more than the controller's
# bounded 30-second replay window at observed DFT admission rates.
PARENTED_DFT_EXECUTION_LIFECYCLE_JOURNAL_LIMIT = 128


class BrokerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BrokerPersistenceFatal(RuntimeError):
    """The durable Broker authority is indeterminate and must be restarted."""


@dataclass(frozen=True)
class OwnerIdentity:
    pid: int
    process_start_ticks: int
    boot_id: str


@dataclass
class Lease:
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
    owner_pid: int
    owner_process_start_ticks: int
    owner_boot_id: str
    preferred: bool
    parent_lease_id: str | None
    status: str
    created_at: float
    heartbeat_at: float
    request_id: str = ""
    mps_termination_status: str = "none"
    mps_terminated_client_pids: list[int] = field(default_factory=list)
    mps_termination_at: float | None = None
    workload_pid: int | None = None
    workload_process_start_ticks: int | None = None
    workload_process_group_id: int | None = None
    workload_cgroup: str | None = None

    def public_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _ParentedDftExecutionLifecycleEvent:
    sequence: int
    action: str
    root_lease_id: str
    root_fencing_token: int
    next_fencing_token_after: int
    lease_authority_sequence_after: int
    child: Lease

    def public_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "action": self.action,
            "root_lease_id": self.root_lease_id,
            "root_fencing_token": self.root_fencing_token,
            "next_fencing_token_after": self.next_fencing_token_after,
            "lease_authority_sequence_after": (
                self.lease_authority_sequence_after
            ),
            "child": self.child.public_dict(),
        }


@dataclass(frozen=True)
class _Waiter:
    request_id: str
    sequence: int
    priority: int
    kind: str
    placement: str
    component: str
    environment: str
    client_id: str
    owner_pid: int
    owner_process_start_ticks: int
    owner_boot_id: str
    memory_mib: int
    thread_percent: int
    parent_lease_id: str | None
    expires_at: float

    def matches(
        self,
        *,
        kind: str,
        placement: str,
        component: str,
        environment: str,
        client_id: str,
        owner: OwnerIdentity,
        memory_mib: int,
        thread_percent: int,
        parent_lease_id: str | None,
    ) -> bool:
        return (
            self.kind,
            self.placement,
            self.component,
            self.environment,
            self.client_id,
            self.owner_pid,
            self.owner_process_start_ticks,
            self.owner_boot_id,
            self.memory_mib,
            self.thread_percent,
            self.parent_lease_id,
        ) == (
            kind,
            placement,
            component,
            environment,
            client_id,
            owner.pid,
            owner.process_start_ticks,
            owner.boot_id,
            memory_mib,
            thread_percent,
            parent_lease_id,
        )


def read_boot_id(path: Path = Path("/proc/sys/kernel/random/boot_id")) -> str:
    try:
        value = path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise BrokerError("host_identity_unavailable", "cannot read the host boot ID") from exc
    if not value:
        raise BrokerError("host_identity_unavailable", "host boot ID is empty")
    return value


def read_process_start_ticks(pid: int) -> int:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        # comm is parenthesized and may itself contain spaces.  Fields after
        # the final ')' begin at proc field 3; starttime is field 22.
        fields_after_comm = raw[raw.rfind(")") + 2 :].split()
        return int(fields_after_comm[19])
    except (OSError, ValueError, IndexError) as exc:
        raise BrokerError(
            "owner_process_unavailable", f"cannot identify owner process {pid}"
        ) from exc


def process_identity_alive(owner: OwnerIdentity, *, current_boot_id: str) -> bool:
    if owner.boot_id != current_boot_id:
        return False
    try:
        return read_process_start_ticks(owner.pid) == owner.process_start_ticks
    except BrokerError:
        return False


def validate_gpu_inventory(actual: Mapping[int, str]) -> None:
    for index, expected_uuid in EXPECTED_GPU_UUIDS.items():
        actual_uuid = actual.get(index)
        if actual_uuid != expected_uuid:
            raise BrokerError(
                "gpu_uuid_mismatch",
                f"GPU{index} UUID mismatch: expected {expected_uuid}, got {actual_uuid or 'missing'}",
            )


class HostGpuBroker:
    """Thread-safe, persistent GPU reservations with monotonic fencing.

    The broker deliberately accounts reservations rather than instantaneous
    NVML free memory.  A timed-out lease becomes suspect while its exact owner
    process (or an injected MPS-client guard) is alive, so capacity is never
    silently double-booked after a client or broker restart.
    """

    def __init__(
        self,
        state_path: Path,
        *,
        heartbeat_timeout_seconds: float = 30.0,
        now: Callable[[], float] = time.time,
        process_alive: Callable[[OwnerIdentity], bool] | None = None,
        gpu_runtime_healthy: Callable[[int, str], bool] | None = None,
        gpu_externally_busy: (
            Callable[
                [int, str, tuple[Lease, ...], OwnerIdentity, str, str],
                bool,
            ]
            | None
        ) = None,
        orphan_mps_client_alive: Callable[[Lease], bool] | None = None,
        terminate_mps_clients: Callable[[Lease], tuple[int, ...]] | None = None,
        mps_clients_alive: Callable[[Lease], bool] | None = None,
        resolve_workload_identity: (
            Callable[[Lease, int, int, int], tuple[int, int, int, str]] | None
        ) = None,
        parented_execution_safe_to_release: (
            Callable[[Lease, Lease, tuple[Lease, ...]], bool] | None
        ) = None,
        validate_workload: Callable[[Lease], None] | None = None,
        freeze_workload: Callable[[Lease], str] | None = None,
        kill_workload: Callable[[Lease], None] | None = None,
        workload_empty: Callable[[Lease], bool] | None = None,
        cleanup_workload: Callable[[Lease], None] | None = None,
    ) -> None:
        if heartbeat_timeout_seconds <= 0:
            raise ValueError("heartbeat_timeout_seconds must be positive")
        self.state_path = state_path
        self.heartbeat_timeout_seconds = float(heartbeat_timeout_seconds)
        self._now = now
        self._boot_id = read_boot_id()
        self._process_alive = process_alive or (
            lambda owner: process_identity_alive(owner, current_boot_id=self._boot_id)
        )
        self._gpu_runtime_healthy = gpu_runtime_healthy or (
            lambda _index, _uuid: True
        )
        self._gpu_externally_busy = gpu_externally_busy or (
            lambda _index, _uuid, _leases, _owner, _component, _environment: False
        )
        self._orphan_mps_client_alive = orphan_mps_client_alive or (lambda _lease: False)
        self._terminate_mps_clients = terminate_mps_clients
        self._mps_clients_alive = mps_clients_alive
        self._resolve_workload_identity = resolve_workload_identity
        self._parented_execution_safe_to_release = parented_execution_safe_to_release
        self._validate_workload = validate_workload
        self._freeze_workload = freeze_workload
        self._kill_workload = kill_workload
        self._workload_empty = workload_empty
        self._cleanup_workload = cleanup_workload
        self.instance_id = uuid4().hex
        self._condition = threading.Condition(threading.RLock())
        self._fatal_persistence_error: str | None = None
        self._leases: dict[str, Lease] = {}
        # This process-local tombstone is intentionally limited to the latest
        # explicit, guarded release.  It lets an inventory client prove that
        # one exact transient scope which disappeared during an audit belonged
        # to the single Broker generation transition it observed.  Reconciled
        # or failed releases never populate it, and a Broker restart changes
        # instance_id so stale tombstones are never trusted.
        self._last_released_lease: Lease | None = None
        # This journal is deliberately process-local and bound to instance_id.
        # It is evidence about explicit child operations completed by this
        # Broker process, not durable authority reconstructed after restart.
        self._parented_dft_execution_lifecycle_sequence = 0
        self._parented_dft_execution_lifecycle_events: deque[
            _ParentedDftExecutionLifecycleEvent
        ] = deque(maxlen=PARENTED_DFT_EXECUTION_LIFECYCLE_JOURNAL_LIMIT)
        # A process-local sequence fences every successfully persisted lease,
        # fencing-token, quarantine, or draining authority mutation.  Journal
        # consumers require each increment inside their read window to be
        # explained by one exact normal DFT-child event, so a transient
        # suspect/recover ABA or another component's lease cannot be hidden
        # between identical endpoint snapshots.
        self._lease_authority_sequence = 0
        self._committed_lease_authority_fingerprint: str | None = None
        self._quarantined_gpus: dict[str, dict[str, object]] = {}
        self._waiters: dict[str, _Waiter] = {}
        self._next_wait_sequence = 1
        self._next_fencing_token = 1
        self._draining = False
        self._load_state()
        with self._condition:
            self._reconcile_locked()
            self._persist_locked()

    @property
    def boot_id(self) -> str:
        return self._boot_id

    def _publish_parented_dft_execution_lifecycle_locked(
        self,
        action: str,
        *,
        root: Lease,
        child: Lease,
    ) -> None:
        """Publish one persisted, canonical child transition under the lock."""

        expected_status = {
            "issue": "reserved",
            "activate": "active",
            "release": "active",
        }.get(action)
        same_root_authority = (
            root.kind == "residency"
            and root.placement == "preferred"
            and root.component == "dft"
            and root.environment == "dev"
            and root.gpu_index == 1
            and root.gpu_uuid == EXPECTED_GPU_UUIDS[1]
            and root.memory_mib == COMPONENT_BUDGETS_MIB["dft"]
            and root.thread_percent == COMPONENT_THREAD_PERCENT["dft"]
            and root.status == "active"
            and root.mps_termination_status == "none"
            and root.parent_lease_id is None
            and child.kind == "execution"
            and child.placement == "preferred"
            and child.component == "dft"
            and child.parent_lease_id == root.lease_id
            and root.broker_instance_id == self.instance_id
            and child.broker_instance_id == self.instance_id
            and child.environment == root.environment
            and child.client_id == root.client_id
            and child.gpu_index == root.gpu_index
            and child.gpu_uuid == root.gpu_uuid
            and child.memory_mib == root.memory_mib
            and child.thread_percent == root.thread_percent
            and child.owner_pid == root.owner_pid
            and child.owner_process_start_ticks
            == root.owner_process_start_ticks
            and child.owner_boot_id == root.owner_boot_id
            and child.preferred == root.preferred is True
        )
        if expected_status is None or child.status != expected_status or not same_root_authority:
            return
        if action in {"activate", "release"} and (
            child.workload_pid,
            child.workload_process_start_ticks,
            child.workload_process_group_id,
            child.workload_cgroup,
        ) != (
            root.workload_pid,
            root.workload_process_start_ticks,
            root.workload_process_group_id,
            root.workload_cgroup,
        ):
            return

        # ``public_dict`` uses dataclasses.asdict and therefore deep-copies the
        # PID list.  No caller retaining the mutable Lease returned by acquire
        # can rewrite already-published evidence.
        child_snapshot = Lease(**child.public_dict())
        sequence = self._parented_dft_execution_lifecycle_sequence + 1
        event = _ParentedDftExecutionLifecycleEvent(
            sequence=sequence,
            action=action,
            root_lease_id=root.lease_id,
            root_fencing_token=root.fencing_token,
            next_fencing_token_after=self._next_fencing_token,
            lease_authority_sequence_after=self._lease_authority_sequence,
            child=child_snapshot,
        )
        self._parented_dft_execution_lifecycle_events.append(event)
        self._parented_dft_execution_lifecycle_sequence = sequence

    def acquire(
        self,
        *,
        request_id: str | None = None,
        kind: str,
        placement: str,
        component: str,
        environment: str,
        client_id: str,
        owner: OwnerIdentity,
        memory_mib: int,
        thread_percent: int,
        wait_timeout_seconds: float = 0.0,
        parent_lease_id: str | None = None,
    ) -> Lease:
        # Direct in-process callers from the pre-request-ID API still receive a
        # deterministic identity.  Hash the canonical request so the generated
        # value cannot exceed the wire contract when client_id is at its limit.
        legacy_request = (
            f"{component}\0{environment}\0{client_id}\0{kind}\0{placement}\0"
            f"{parent_lease_id or 'none'}"
        )
        stable_request_id = request_id or f"legacy:{sha256(legacy_request.encode()).hexdigest()}"
        self._validate_request(
            request_id=stable_request_id,
            kind=kind,
            placement=placement,
            component=component,
            environment=environment,
            client_id=client_id,
            owner=owner,
            memory_mib=memory_mib,
            thread_percent=thread_percent,
            parent_lease_id=parent_lease_id,
        )
        timeout = max(0.0, float(wait_timeout_seconds))
        deadline = time.monotonic() + timeout
        with self._condition:
            self._require_healthy_locked()
            self._expire_waiters_locked()
            if any(
                lease.request_id == stable_request_id
                for lease in self._leases.values()
            ):
                recovered = self._try_allocate_locked(
                    request_id=stable_request_id,
                    kind=kind,
                    placement=placement,
                    component=component,
                    environment=environment,
                    client_id=client_id,
                    owner=owner,
                    memory_mib=memory_mib,
                    thread_percent=thread_percent,
                    parent_lease_id=parent_lease_id,
                )
                assert recovered is not None
                return recovered
            waiter = self._waiters.get(stable_request_id)
            if waiter is not None and not waiter.matches(
                kind=kind,
                placement=placement,
                component=component,
                environment=environment,
                client_id=client_id,
                owner=owner,
                memory_mib=memory_mib,
                thread_percent=thread_percent,
                parent_lease_id=parent_lease_id,
            ):
                raise BrokerError(
                    "request_id_conflict",
                    "GPU acquire request ID is already bound to another request",
                )
            if waiter is None:
                waiter = _Waiter(
                    request_id=stable_request_id,
                    sequence=self._next_wait_sequence,
                    priority=0 if environment == "prod" else 1,
                    kind=kind,
                    placement=placement,
                    component=component,
                    environment=environment,
                    client_id=client_id,
                    owner_pid=owner.pid,
                    owner_process_start_ticks=owner.process_start_ticks,
                    owner_boot_id=owner.boot_id,
                    memory_mib=memory_mib,
                    thread_percent=thread_percent,
                    parent_lease_id=parent_lease_id,
                    # A zero-wait acquire still receives one atomic admission
                    # attempt before expiring.
                    expires_at=self._now() + max(timeout, 0.25),
                )
                self._next_wait_sequence += 1
                self._waiters[stable_request_id] = waiter
                self._persist_locked()
            try:
                while True:
                    self._require_healthy_locked()
                    self._reconcile_locked()
                    if self._waiters.get(stable_request_id) != waiter:
                        recovered = next(
                            (
                                lease
                                for lease in self._leases.values()
                                if lease.request_id == stable_request_id
                            ),
                            None,
                        )
                        if recovered is not None:
                            return recovered
                        if deadline - time.monotonic() <= 0:
                            raise BrokerError(
                                "gpu_capacity_unavailable",
                                "no policy-eligible GPU capacity is currently available",
                            )
                        raise BrokerError(
                            "acquire_cancelled", "GPU acquire request was cancelled"
                        )
                    if self._draining:
                        raise BrokerError("broker_draining", "GPU broker is draining")
                    if self._queue_head_locked() == waiter:
                        lease_ids_before_allocation = frozenset(self._leases)
                        lease = self._try_allocate_locked(
                            request_id=stable_request_id,
                            kind=kind,
                            placement=placement,
                            component=component,
                            environment=environment,
                            client_id=client_id,
                            owner=owner,
                            memory_mib=memory_mib,
                            thread_percent=thread_percent,
                            parent_lease_id=parent_lease_id,
                        )
                        if lease is not None:
                            newly_issued = (
                                lease.lease_id not in lease_ids_before_allocation
                            )
                            self._waiters.pop(stable_request_id, None)
                            self._persist_locked()
                            if newly_issued and lease.parent_lease_id is not None:
                                root = self._leases.get(lease.parent_lease_id)
                                if root is not None:
                                    self._publish_parented_dft_execution_lifecycle_locked(
                                        "issue",
                                        root=root,
                                        child=lease,
                                    )
                            self._condition.notify_all()
                            return lease
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BrokerError(
                            "gpu_capacity_unavailable",
                            "no policy-eligible GPU capacity is currently available",
                        )
                    self._condition.wait(timeout=min(remaining, 1.0))
            finally:
                if (
                    self._fatal_persistence_error is None
                    and self._waiters.get(stable_request_id) == waiter
                ):
                    self._waiters.pop(stable_request_id, None)
                    self._persist_locked()
                    self._condition.notify_all()

    def cancel_acquire(self, request_id: str, *, owner: OwnerIdentity) -> bool:
        if _REQUEST_ID_RE.fullmatch(request_id) is None:
            raise BrokerError("invalid_request", "request_id is invalid")
        with self._condition:
            self._require_healthy_locked()
            waiter = self._waiters.get(request_id)
            if waiter is not None:
                if (
                    waiter.owner_pid != owner.pid
                    or waiter.owner_process_start_ticks != owner.process_start_ticks
                    or waiter.owner_boot_id != owner.boot_id
                ):
                    raise BrokerError(
                        "request_owner_mismatch",
                        "GPU acquire request belongs to another process",
                    )
                self._waiters.pop(request_id, None)
                self._persist_locked()
                self._condition.notify_all()
                return True
            existing = next(
                (lease for lease in self._leases.values() if lease.request_id == request_id),
                None,
            )
            if existing is not None:
                self._owned_lease_locked(
                    existing.lease_id, existing.fencing_token, owner
                )
            return False

    def activate(
        self, lease_id: str, fencing_token: int, *, owner: OwnerIdentity
    ) -> Lease:
        with self._condition:
            self._require_healthy_locked()
            lease = self._owned_lease_locked(lease_id, fencing_token, owner)
            previous_status = lease.status
            if lease.mps_termination_status != "none":
                raise BrokerError(
                    "invalid_lease_state",
                    "a lease prepared for MPS termination cannot be reactivated",
                )
            if lease.status not in {"reserved", "active", "suspect"}:
                raise BrokerError("invalid_lease_state", f"cannot activate {lease.status} lease")
            lease.status = "active"
            lease.heartbeat_at = self._now()
            if (
                lease.kind == "residency"
                and lease.component == "backend"
                and lease.workload_pid is None
            ):
                # Backend is the CUDA workload itself.  DFT residency is
                # intentionally deferred: its CPU-only supervisor owns the
                # lease, then registers the long-lived executor child before
                # that child is allowed to import CUDA.
                lease.workload_pid = owner.pid
                lease.workload_process_start_ticks = owner.process_start_ticks
                try:
                    lease.workload_process_group_id = os.getpgid(owner.pid)
                    lease.workload_cgroup = Path(
                        f"/proc/{owner.pid}/cgroup"
                    ).read_text(encoding="utf-8").strip()
                except OSError as exc:
                    raise BrokerError(
                        "workload_identity_unavailable",
                        "cannot register residency workload identity",
                    ) from exc
            parent: Lease | None = None
            if lease.parent_lease_id is not None:
                parent = self._leases.get(lease.parent_lease_id)
                if (
                    parent is None
                    or parent.status not in {"active", "suspect"}
                    or parent.workload_pid is None
                    or parent.workload_process_start_ticks is None
                    or parent.workload_process_group_id is None
                    or parent.workload_cgroup is None
                ):
                    raise BrokerError(
                        "invalid_parent_lease",
                        "resident execution requires a registered live residency workload",
                    )
                lease.workload_pid = parent.workload_pid
                lease.workload_process_start_ticks = (
                    parent.workload_process_start_ticks
                )
                lease.workload_process_group_id = parent.workload_process_group_id
                lease.workload_cgroup = parent.workload_cgroup
            self._persist_locked()
            if previous_status == "reserved" and parent is not None:
                self._publish_parented_dft_execution_lifecycle_locked(
                    "activate",
                    root=parent,
                    child=lease,
                )
            return lease

    def register_workload(
        self,
        lease_id: str,
        fencing_token: int,
        *,
        owner: OwnerIdentity,
        workload_pid: int,
        workload_process_start_ticks: int,
        workload_process_group_id: int,
    ) -> Lease:
        """Bind an isolated workload lease to a live descendant.

        MD and overflow DFT execution leases register their per-job child.
        DFT residency registers the long-lived executor child.  A parented DFT
        execution lease is logical admission only and inherits that exact
        parent workload, so it must never be rebound independently.
        """

        if min(
            workload_pid,
            workload_process_start_ticks,
            workload_process_group_id,
        ) <= 0:
            raise BrokerError("invalid_request", "workload process identity is invalid")
        with self._condition:
            self._require_healthy_locked()
            lease = self._owned_lease_locked(lease_id, fencing_token, owner)
            is_deferred_dft_residency = (
                lease.kind == "residency" and lease.component == "dft"
            )
            is_isolated_execution = (
                lease.kind == "execution" and lease.parent_lease_id is None
            )
            if not (is_deferred_dft_residency or is_isolated_execution) or lease.status not in {
                "reserved",
                "active",
                "suspect",
            }:
                raise BrokerError(
                    "invalid_lease_state",
                    "lease cannot register an independent workload",
                )
            if lease.mps_termination_status != "none":
                raise BrokerError(
                    "invalid_lease_state",
                    "a workload cannot register after MPS termination begins",
                )
            if self._resolve_workload_identity is None:
                raise BrokerError(
                    "workload_identity_unavailable",
                    "host workload identity resolver is unavailable",
                )
            resolved = self._resolve_workload_identity(
                lease,
                workload_pid,
                workload_process_start_ticks,
                workload_process_group_id,
            )
            existing = (
                lease.workload_pid,
                lease.workload_process_start_ticks,
                lease.workload_process_group_id,
                lease.workload_cgroup,
            )
            if lease.workload_pid is not None and existing != resolved:
                raise BrokerError(
                    "workload_identity_mismatch",
                    "execution lease is already fenced to another workload",
                )
            (
                lease.workload_pid,
                lease.workload_process_start_ticks,
                lease.workload_process_group_id,
                lease.workload_cgroup,
            ) = resolved
            lease.heartbeat_at = self._now()
            if lease.status == "suspect":
                lease.status = "active"
            self._persist_locked()
            return lease

    def heartbeat(
        self, lease_id: str, fencing_token: int, *, owner: OwnerIdentity
    ) -> Lease:
        with self._condition:
            self._require_healthy_locked()
            lease = self._owned_lease_locked(lease_id, fencing_token, owner)
            lease.heartbeat_at = self._now()
            if lease.status == "suspect" and lease.mps_termination_status == "none":
                lease.status = "active"
            self._persist_locked()
            return lease

    def release(self, lease_id: str, fencing_token: int, *, owner: OwnerIdentity) -> None:
        with self._condition:
            self._require_healthy_locked()
            lease = self._owned_lease_locked(lease_id, fencing_token, owner)
            children = [
                lease
                for lease in self._leases.values()
                if lease.parent_lease_id == lease_id
            ]
            if children:
                raise BrokerError(
                    "lease_has_children", "release child execution leases before residency"
                )
            parent: Lease | None = None
            if lease.parent_lease_id is not None:
                parent = self._leases.get(lease.parent_lease_id)
                if (
                    parent is None
                    or parent.workload_pid is None
                    or (
                        lease.workload_pid,
                        lease.workload_process_start_ticks,
                        lease.workload_process_group_id,
                        lease.workload_cgroup,
                    )
                    != (
                        parent.workload_pid,
                        parent.workload_process_start_ticks,
                        parent.workload_process_group_id,
                        parent.workload_cgroup,
                    )
                ):
                    self._fail_release_closed_locked(lease)
                    raise BrokerError(
                        "gpu_runtime_unhealthy",
                        "parented execution workload no longer matches its residency lease",
                    )
            try:
                if parent is not None:
                    if self._parented_execution_safe_to_release is None:
                        raise BrokerError(
                            "mps_control_unavailable",
                            "parented execution release guard is unavailable",
                        )
                    safe = self._parented_execution_safe_to_release(
                        lease,
                        parent,
                        tuple(self._leases.values()),
                    )
                    if safe is not True:
                        raise BrokerError(
                            "gpu_runtime_unhealthy",
                            "an MPS client is outside the live governed lease inventory",
                        )
                elif lease.status != "reserved" or lease.workload_pid is not None:
                    if self._mps_clients_alive is None:
                        raise BrokerError(
                            "mps_control_unavailable",
                            "host MPS release guard is unavailable",
                        )
                    if self._mps_clients_alive(lease):
                        raise BrokerError(
                            "gpu_runtime_unhealthy",
                            "MPS client remains active",
                        )
                    requires_isolated_cgroup = (
                        lease.workload_pid is not None
                        and (
                            lease.kind == "execution"
                            or (lease.kind == "residency" and lease.component == "dft")
                        )
                    )
                    if requires_isolated_cgroup:
                        if self._workload_empty is None or not self._workload_empty(lease):
                            raise BrokerError(
                                "gpu_runtime_unhealthy",
                                "isolated workload cgroup is not proven empty",
                            )
            except Exception as exc:
                self._fail_release_closed_locked(lease)
                if isinstance(exc, BrokerError) and exc.code == "gpu_runtime_unhealthy":
                    raise
                raise BrokerError(
                    "gpu_runtime_unhealthy",
                    "workload release evidence is unavailable; lease retained",
                ) from exc
            if (
                parent is None
                and lease.workload_pid is not None
                and (
                    lease.kind == "execution"
                    or (lease.kind == "residency" and lease.component == "dft")
                )
                and self._cleanup_workload is not None
            ):
                try:
                    self._cleanup_workload(lease)
                except Exception as exc:
                    self._fail_release_closed_locked(lease)
                    raise BrokerError(
                        "gpu_runtime_unhealthy",
                        "isolated workload cgroup cleanup failed; lease retained",
                    ) from exc
            # Snapshot rather than retain the object returned by acquire();
            # in-process callers must not be able to mutate tombstone proof.
            released_snapshot = Lease(**lease.public_dict())
            del self._leases[lease_id]
            self._persist_locked()
            # A failed state commit is not an explicit successful release and
            # therefore must never create authority for an audit retry.
            self._last_released_lease = released_snapshot
            if parent is not None:
                self._publish_parented_dft_execution_lifecycle_locked(
                    "release",
                    root=parent,
                    child=released_snapshot,
                )
            self._condition.notify_all()

    def _fail_release_closed_locked(self, lease: Lease) -> None:
        lease.status = "suspect"
        self._quarantined_gpus[lease.gpu_uuid] = {
            "gpu_index": lease.gpu_index,
            "gpu_uuid": lease.gpu_uuid,
            "reason": "gpu_runtime_corruption",
            "lease_id": lease.lease_id,
            "fencing_token": lease.fencing_token,
            "quarantined_at": self._now(),
        }
        self._persist_locked()
        self._condition.notify_all()

    def quarantine(
        self,
        lease_id: str,
        fencing_token: int,
        *,
        owner: OwnerIdentity,
        reason: str,
    ) -> dict[str, object]:
        if reason not in QUARANTINE_REASONS:
            raise BrokerError(
                "invalid_request",
                "quarantine reason must be one of: " + ", ".join(sorted(QUARANTINE_REASONS)),
            )
        with self._condition:
            self._require_healthy_locked()
            lease = self._owned_lease_locked(lease_id, fencing_token, owner)
            self._quarantined_gpus[lease.gpu_uuid] = {
                "gpu_index": lease.gpu_index,
                "gpu_uuid": lease.gpu_uuid,
                "reason": reason,
                "lease_id": lease.lease_id,
                "fencing_token": lease.fencing_token,
                "quarantined_at": self._now(),
            }
            self._persist_locked()
            self._condition.notify_all()
            return dict(self._quarantined_gpus[lease.gpu_uuid])

    def prepare_process_termination(
        self,
        lease_id: str,
        fencing_token: int,
        *,
        owner: OwnerIdentity,
    ) -> dict[str, object]:
        """Safely terminate all MPS contexts before a client receives a signal."""

        with self._condition:
            self._require_healthy_locked()
            lease = self._owned_lease_locked(lease_id, fencing_token, owner)
            if not (
                (lease.kind == "execution" and lease.parent_lease_id is None)
                or (lease.kind == "residency" and lease.component == "dft")
            ):
                raise BrokerError(
                    "invalid_lease_state",
                    "termination is valid only for isolated executor workloads",
                )
            if lease.workload_pid is None:
                raise BrokerError(
                    "workload_identity_unavailable",
                    "execution workload must be registered before termination",
                )
            failure_stage = "control_availability"
            try:
                if (
                    self._validate_workload is None
                    or self._freeze_workload is None
                    or self._kill_workload is None
                    or self._workload_empty is None
                ):
                    raise BrokerError(
                        "workload_control_unavailable",
                        "dedicated cgroup freeze/kill control is unavailable",
                    )
                if self._terminate_mps_clients is None:
                    raise BrokerError(
                        "mps_control_unavailable",
                        "host MPS termination control is unavailable",
                    )
                if self._mps_clients_alive is None:
                    raise BrokerError(
                        "mps_control_unavailable",
                        "host MPS post-termination audit is unavailable",
                    )
                # NVIDIA's terminate_client request must run while the client
                # can still service the MPS protocol.  First re-bind the exact
                # lease scope, then terminate all currently reported contexts.
                # Holding the Broker lock prevents a new lease.  The first
                # MPS audit includes its host grace period and must complete
                # while the workload can still service MPS teardown.  Only
                # after the exact lease clients disappear may the dedicated
                # scope be frozen; a second audit then closes the reconnect
                # race before the exact owned scope is killed.
                failure_stage = "workload_revalidation"
                self._validate_workload(lease)
                failure_stage = "mps_client_termination"
                client_pids = self._terminate_mps_clients(lease)
                if (
                    not isinstance(client_pids, tuple)
                    or len(client_pids) != len(set(client_pids))
                    or any(
                        isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
                        for pid in client_pids
                    )
                ):
                    raise BrokerError(
                        "mps_termination_failed",
                        "MPS termination evidence is invalid",
                    )
                failure_stage = "pre_freeze_mps_drain"
                if self._mps_clients_alive(lease):
                    raise BrokerError(
                        "mps_termination_failed",
                        "an exact MPS client survived the pre-freeze grace period",
                    )
                failure_stage = "workload_freeze"
                freeze_token = self._freeze_workload(lease)
                if not isinstance(freeze_token, str) or not freeze_token:
                    raise BrokerError(
                        "workload_control_unavailable",
                        "dedicated cgroup freeze evidence is invalid",
                    )
                failure_stage = "post_freeze_mps_audit"
                if self._mps_clients_alive(lease):
                    raise BrokerError(
                        "mps_termination_failed",
                        "an MPS client appeared or survived after workload freeze",
                    )
                failure_stage = "workload_kill"
                self._kill_workload(lease)
                failure_stage = "workload_empty"
                if not self._workload_empty(lease):
                    raise BrokerError(
                        "workload_termination_failed",
                        "dedicated workload cgroup did not become empty",
                    )
            except Exception as exc:
                broker_error_code = (
                    exc.code if isinstance(exc, BrokerError) else "unexpected_exception"
                )
                logger.error(
                    "gpu_process_termination_proof_failed lease_id=%s "
                    "fencing_token=%d stage=%s broker_error_code=%s",
                    lease.lease_id,
                    lease.fencing_token,
                    failure_stage,
                    broker_error_code,
                )
                lease.mps_termination_status = "failed"
                lease.status = "suspect"
                self._quarantined_gpus[lease.gpu_uuid] = {
                    "gpu_index": lease.gpu_index,
                    "gpu_uuid": lease.gpu_uuid,
                    "reason": "gpu_runtime_corruption",
                    "lease_id": lease.lease_id,
                    "fencing_token": lease.fencing_token,
                    "quarantined_at": self._now(),
                }
                self._persist_locked()
                self._condition.notify_all()
                raise BrokerError(
                    "gpu_runtime_unhealthy",
                    "MPS client termination could not be proven safe; GPU quarantined",
                ) from exc
            lease.mps_termination_status = "safe"
            lease.mps_terminated_client_pids = list(client_pids)
            lease.mps_termination_at = self._now()
            lease.status = "terminating"
            lease.heartbeat_at = self._now()
            self._persist_locked()
            return {
                "safe_to_signal": True,
                "client_pids": list(client_pids),
                "prepared_at": lease.mps_termination_at,
                "freeze_token": freeze_token,
            }

    def set_draining(self, draining: bool) -> dict[str, object]:
        with self._condition:
            self._require_healthy_locked()
            self._draining = bool(draining)
            self._persist_locked()
            self._condition.notify_all()
            return self.status()

    def status(self) -> dict[str, object]:
        with self._condition:
            self._require_healthy_locked()
            self._reconcile_locked()
            usage = {
                str(index): sum(
                    lease.memory_mib
                    for lease in self._leases.values()
                    if lease.gpu_index == index and lease.parent_lease_id is None
                )
                for index in EXPECTED_GPU_UUIDS
            }
            lifecycle_first_sequence = (
                self._parented_dft_execution_lifecycle_events[0].sequence
                if self._parented_dft_execution_lifecycle_events
                else self._parented_dft_execution_lifecycle_sequence + 1
            )
            return {
                "schema_version": 1,
                "broker_instance_id": self.instance_id,
                # Monotonic within the persisted Broker authority.  Including
                # the next token lets inventory clients detect an acquire +
                # release ABA even when the visible lease list is identical
                # before and after their host snapshot.
                "next_fencing_token": self._next_fencing_token,
                "last_released_lease": (
                    None
                    if self._last_released_lease is None
                    else self._last_released_lease.public_dict()
                ),
                "parented_dft_execution_lifecycle_sequence": (
                    self._parented_dft_execution_lifecycle_sequence
                ),
                "parented_dft_execution_lifecycle_first_sequence": (
                    lifecycle_first_sequence
                ),
                "parented_dft_execution_lifecycle_events": [
                    event.public_dict()
                    for event in self._parented_dft_execution_lifecycle_events
                ],
                "lease_authority_sequence": self._lease_authority_sequence,
                "boot_id": self._boot_id,
                "draining": self._draining,
                "gpu_total_budget_mib": GPU_TOTAL_BUDGET_MIB,
                "component_budgets_mib": dict(COMPONENT_BUDGETS_MIB),
                "gpu_uuids": {str(key): value for key, value in EXPECTED_GPU_UUIDS.items()},
                "usage_mib": usage,
                "quarantined_gpus": {
                    uuid: dict(value)
                    for uuid, value in sorted(self._quarantined_gpus.items())
                },
                "waiters": len(self._waiters),
                "leases": [
                    lease.public_dict()
                    for lease in sorted(
                        self._leases.values(), key=lambda item: item.fencing_token
                    )
                ],
            }

    def _validate_request(
        self,
        *,
        request_id: str,
        kind: str,
        placement: str,
        component: str,
        environment: str,
        client_id: str,
        owner: OwnerIdentity,
        memory_mib: int,
        thread_percent: int,
        parent_lease_id: str | None,
    ) -> None:
        if _REQUEST_ID_RE.fullmatch(request_id) is None:
            raise BrokerError("invalid_request", "request_id is invalid")
        if kind not in {"residency", "execution"}:
            raise BrokerError("invalid_request", "kind must be residency or execution")
        if placement not in {"preferred", "overflow", "any"}:
            raise BrokerError(
                "invalid_request", "placement must be preferred, overflow, or any"
            )
        if component not in COMPONENT_BUDGETS_MIB:
            raise BrokerError("invalid_request", f"unknown component: {component}")
        if (environment, component) not in DEVICE_POLICY:
            raise BrokerError("invalid_request", f"unsupported environment: {environment}")
        if _CLIENT_ID_RE.fullmatch(client_id) is None:
            raise BrokerError("invalid_request", "client_id is invalid")
        expected_budget = COMPONENT_BUDGETS_MIB[component]
        if memory_mib != expected_budget:
            raise BrokerError(
                "invalid_budget",
                f"{component} reservations must be exactly {expected_budget} MiB",
            )
        expected_threads = COMPONENT_THREAD_PERCENT[component]
        if isinstance(thread_percent, bool) or thread_percent != expected_threads:
            raise BrokerError(
                "invalid_budget",
                f"{component} MPS clients must use exactly {expected_threads}% active threads",
            )
        if owner.pid <= 0 or owner.process_start_ticks <= 0 or not owner.boot_id:
            raise BrokerError("invalid_owner", "owner process identity is invalid")
        if owner.boot_id != self._boot_id:
            raise BrokerError("invalid_owner", "owner boot ID does not match this host")
        if not self._process_alive(owner):
            raise BrokerError("invalid_owner", "owner process identity is not alive")
        if parent_lease_id is not None and kind != "execution":
            raise BrokerError("invalid_request", "only execution leases may have a parent")
        if component == "backend" and kind != "residency":
            raise BrokerError("invalid_request", "backend requires a residency lease")
        if component == "backend" and placement != "preferred":
            raise BrokerError("invalid_request", "backend residency requires preferred placement")
        if component == "md" and kind != "execution":
            raise BrokerError("invalid_request", "MD requires a per-job execution lease")
        if component == "md" and placement != "any":
            raise BrokerError("invalid_request", "MD execution requires any placement")
        if component == "dft":
            if kind == "residency" and placement != "preferred":
                raise BrokerError("invalid_request", "DFT residency requires preferred placement")
            if kind == "execution" and parent_lease_id is not None and placement != "preferred":
                raise BrokerError(
                    "invalid_request", "resident DFT execution requires preferred placement"
                )
            if kind == "execution" and parent_lease_id is None and placement != "overflow":
                raise BrokerError(
                    "invalid_request", "transient DFT execution requires overflow placement"
                )

    def _queue_head_locked(self) -> _Waiter | None:
        if not self._waiters:
            return None
        return min(
            self._waiters.values(),
            key=lambda item: (item.priority, item.sequence),
        )

    def _try_allocate_locked(
        self,
        *,
        request_id: str,
        kind: str,
        placement: str,
        component: str,
        environment: str,
        client_id: str,
        owner: OwnerIdentity,
        memory_mib: int,
        thread_percent: int,
        parent_lease_id: str | None,
    ) -> Lease | None:
        existing_request = next(
            (
                lease
                for lease in self._leases.values()
                if lease.request_id == request_id
            ),
            None,
        )
        if existing_request is not None:
            expected = (
                kind,
                placement,
                component,
                environment,
                client_id,
                owner.pid,
                owner.process_start_ticks,
                owner.boot_id,
                memory_mib,
                thread_percent,
                parent_lease_id,
            )
            actual = (
                existing_request.kind,
                existing_request.placement,
                existing_request.component,
                existing_request.environment,
                existing_request.client_id,
                existing_request.owner_pid,
                existing_request.owner_process_start_ticks,
                existing_request.owner_boot_id,
                existing_request.memory_mib,
                existing_request.thread_percent,
                existing_request.parent_lease_id,
            )
            if actual != expected:
                raise BrokerError(
                    "request_id_conflict",
                    "GPU acquire request ID is already bound to another request",
                )
            if existing_request.mps_termination_status != "none":
                raise BrokerError(
                    "invalid_lease_state",
                    "a lease prepared for termination cannot be reused",
                )
            return existing_request
        if kind == "residency":
            existing_residency = next(
                (
                    lease
                    for lease in self._leases.values()
                    if lease.kind == "residency"
                    and lease.component == component
                    and lease.environment == environment
                ),
                None,
            )
            if existing_residency is not None:
                if (
                    existing_residency.client_id == client_id
                    and existing_residency.owner_pid == owner.pid
                    and existing_residency.owner_process_start_ticks
                    == owner.process_start_ticks
                    and existing_residency.owner_boot_id == owner.boot_id
                ):
                    # Residency has a single owner.  A new request ID is not
                    # allowed to alias the existing reservation because only
                    # the stable request ID proves a lost-response retry.
                    return None
                return None
        parent: Lease | None = None
        if parent_lease_id is not None:
            parent = self._leases.get(parent_lease_id)
            if parent is None:
                raise BrokerError("unknown_lease", "parent residency lease is unknown")
            if (
                parent.kind != "residency"
                or parent.component != component
                or parent.environment != environment
                or parent.client_id != client_id
                or parent.owner_pid != owner.pid
                or parent.owner_process_start_ticks != owner.process_start_ticks
            ):
                raise BrokerError("invalid_parent_lease", "parent residency lease does not match")
            if any(
                lease.parent_lease_id == parent.lease_id
                for lease in self._leases.values()
            ):
                # The resident DFT worker serializes executions through one
                # long-lived executor.  Enforcing the same contract here keeps
                # zero-accounting child leases and the status response bounded.
                return None
            candidate_indices: Iterable[int] = (parent.gpu_index,)
            reserved_memory = 0
        else:
            policy_indices = DEVICE_POLICY[(environment, component)]
            if placement == "preferred":
                candidate_indices = policy_indices[:1]
            elif placement == "overflow":
                candidate_indices = policy_indices[1:]
            else:
                candidate_indices = policy_indices
            reserved_memory = memory_mib

        live_leases = tuple(self._leases.values())
        begin_external_admission = getattr(
            self._gpu_externally_busy,
            "begin_admission",
            None,
        )
        external_admission = (
            begin_external_admission(
                leases=live_leases,
                owner=owner,
                component=component,
                environment=environment,
                client_id=client_id,
                parent_lease_id=parent_lease_id,
            )
            if callable(begin_external_admission)
            else None
        )
        if external_admission is not None and not callable(external_admission):
            raise BrokerError(
                "gpu_claim_inventory_unavailable",
                "external GPU admission authority is invalid",
            )
        for position, index in enumerate(candidate_indices):
            uuid = EXPECTED_GPU_UUIDS[index]
            if uuid in self._quarantined_gpus:
                continue
            if any(
                lease.gpu_uuid == uuid and lease.status == "suspect"
                for lease in live_leases
            ):
                continue
            if not self._gpu_runtime_healthy(index, uuid):
                continue
            externally_busy = (
                external_admission(index, uuid)
                if external_admission is not None
                else self._gpu_externally_busy(
                    index,
                    uuid,
                    live_leases,
                    owner,
                    component,
                    environment,
                )
            )
            if externally_busy:
                continue
            used = sum(
                lease.memory_mib
                for lease in live_leases
                if lease.gpu_index == index and lease.parent_lease_id is None
            )
            if used + reserved_memory > GPU_TOTAL_BUDGET_MIB:
                continue
            finalize_external_admission = (
                getattr(external_admission, "finalize", None)
                if external_admission is not None
                else None
            )
            if (
                finalize_external_admission is not None
                and not callable(finalize_external_admission)
            ):
                raise BrokerError(
                    "gpu_claim_inventory_unavailable",
                    "external GPU admission finalizer is invalid",
                )
            if callable(finalize_external_admission):
                finalize_external_admission(index, uuid)
            now = self._now()
            lease = Lease(
                lease_id=uuid4().hex,
                fencing_token=self._next_fencing_token,
                broker_instance_id=self.instance_id,
                kind=kind,
                placement=placement,
                component=component,
                environment=environment,
                client_id=client_id,
                gpu_index=index,
                gpu_uuid=uuid,
                # Child execution leases retain the component budget as
                # provenance, while accounting ignores them because their
                # parent residency lease already reserves the memory.
                memory_mib=memory_mib,
                thread_percent=thread_percent,
                owner_pid=owner.pid,
                owner_process_start_ticks=owner.process_start_ticks,
                owner_boot_id=owner.boot_id,
                preferred=index == DEVICE_POLICY[(environment, component)][0],
                parent_lease_id=parent_lease_id,
                status="reserved",
                created_at=now,
                heartbeat_at=now,
                request_id=request_id,
            )
            self._next_fencing_token += 1
            self._leases[lease.lease_id] = lease
            return lease
        return None

    def _owned_lease_locked(
        self, lease_id: str, fencing_token: int, owner: OwnerIdentity
    ) -> Lease:
        lease = self._leases.get(lease_id)
        if lease is None:
            raise BrokerError("unknown_lease", "lease is unknown or has been reclaimed")
        if lease.fencing_token != fencing_token:
            raise BrokerError("stale_fencing_token", "lease fencing token is stale")
        if (
            lease.owner_pid != owner.pid
            or lease.owner_process_start_ticks != owner.process_start_ticks
            or lease.owner_boot_id != owner.boot_id
        ):
            raise BrokerError("lease_owner_mismatch", "lease belongs to another process")
        return lease

    def _reconcile_locked(self) -> None:
        changed = False
        if self._expire_waiters_locked():
            changed = True
        now = self._now()
        for lease_id, lease in list(self._leases.items()):
            owner = OwnerIdentity(
                pid=lease.owner_pid,
                process_start_ticks=lease.owner_process_start_ticks,
                boot_id=lease.owner_boot_id,
            )
            heartbeat_expired = now - lease.heartbeat_at > self.heartbeat_timeout_seconds
            if not heartbeat_expired:
                continue
            if self._process_alive(owner) or self._orphan_mps_client_alive(lease):
                if lease.status != "suspect":
                    lease.status = "suspect"
                    changed = True
                continue
            if any(item.parent_lease_id == lease_id for item in self._leases.values()):
                # Children are processed independently; preserve the parent for
                # this pass to avoid a transient unaccounted execution lease.
                continue
            del self._leases[lease_id]
            changed = True
        if changed:
            self._persist_locked()
            self._condition.notify_all()

    def _expire_waiters_locked(self) -> bool:
        changed = False
        now = self._now()
        for request_id, waiter in list(self._waiters.items()):
            owner = OwnerIdentity(
                pid=waiter.owner_pid,
                process_start_ticks=waiter.owner_process_start_ticks,
                boot_id=waiter.owner_boot_id,
            )
            if waiter.expires_at <= now or not self._process_alive(owner):
                self._waiters.pop(request_id, None)
                changed = True
        return changed

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        if self.state_path.is_symlink() or not self.state_path.is_file():
            raise BrokerError("unsafe_state", "broker state path is not a regular file")
        mode = self.state_path.stat().st_mode & 0o777
        if mode & 0o077:
            raise BrokerError("unsafe_state", "broker state must not be group/world accessible")
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BrokerError("invalid_state", "broker state is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise BrokerError("invalid_state", "unsupported broker state schema")
        next_token = payload.get("next_fencing_token")
        if isinstance(next_token, bool) or not isinstance(next_token, int) or next_token < 1:
            raise BrokerError("invalid_state", "invalid fencing token counter")
        raw_leases = payload.get("leases")
        if not isinstance(raw_leases, list):
            raise BrokerError("invalid_state", "invalid lease list")
        leases: dict[str, Lease] = {}
        try:
            for raw in raw_leases:
                if isinstance(raw, dict) and "placement" not in raw:
                    raw = dict(raw)
                    if raw.get("component") == "md":
                        raw["placement"] = "any"
                    elif raw.get("parent_lease_id") is not None:
                        raw["placement"] = "preferred"
                    else:
                        raw["placement"] = (
                            "preferred" if raw.get("preferred") is True else "overflow"
                        )
                if isinstance(raw, dict) and "request_id" not in raw:
                    raw = dict(raw)
                    raw["request_id"] = raw.get("lease_id", "")
                lease = Lease(**raw)
                if lease.lease_id in leases:
                    raise ValueError("duplicate lease ID")
                if lease.gpu_uuid != EXPECTED_GPU_UUIDS.get(lease.gpu_index):
                    raise ValueError("stored GPU UUID is no longer allowed")
                if lease.component not in COMPONENT_BUDGETS_MIB:
                    raise ValueError("stored component is not allowed")
                if lease.memory_mib != COMPONENT_BUDGETS_MIB[lease.component]:
                    raise ValueError("stored component budget is invalid")
                if lease.thread_percent != COMPONENT_THREAD_PERCENT[lease.component]:
                    raise ValueError("stored component thread budget is invalid")
                if (lease.environment, lease.component) not in DEVICE_POLICY:
                    raise ValueError("stored environment policy is invalid")
                if lease.gpu_index not in DEVICE_POLICY[(lease.environment, lease.component)]:
                    raise ValueError("stored GPU is outside the device policy")
                if lease.kind not in {"residency", "execution"}:
                    raise ValueError("stored lease kind is invalid")
                if _REQUEST_ID_RE.fullmatch(lease.request_id) is None:
                    raise ValueError("stored request ID is invalid")
                if lease.placement not in {"preferred", "overflow", "any"}:
                    raise ValueError("stored lease placement is invalid")
                if lease.component == "backend" and (
                    lease.kind != "residency" or lease.placement != "preferred"
                ):
                    raise ValueError("stored Backend lease violates placement policy")
                if lease.component == "md" and (
                    lease.kind != "execution" or lease.placement != "any"
                ):
                    raise ValueError("stored MD lease violates placement policy")
                if lease.component == "dft":
                    expected_placement = (
                        "preferred"
                        if lease.kind == "residency" or lease.parent_lease_id is not None
                        else "overflow"
                    )
                    if lease.placement != expected_placement:
                        raise ValueError("stored DFT lease violates placement policy")
                if lease.status not in {"reserved", "active", "suspect", "terminating"}:
                    raise ValueError("stored lease status is invalid")
                if lease.mps_termination_status not in {"none", "safe", "failed"}:
                    raise ValueError("stored MPS termination status is invalid")
                if not isinstance(lease.mps_terminated_client_pids, list) or any(
                    isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
                    for pid in lease.mps_terminated_client_pids
                ):
                    raise ValueError("stored MPS client PID list is invalid")
                if lease.mps_termination_status == "none" and (
                    lease.mps_terminated_client_pids or lease.mps_termination_at is not None
                ):
                    raise ValueError("stored MPS termination evidence is inconsistent")
                if lease.mps_termination_status == "safe" and (
                    not (
                        (lease.kind == "execution" and lease.parent_lease_id is None)
                        or (lease.kind == "residency" and lease.component == "dft")
                    )
                    or lease.mps_termination_at is None
                    or lease.status not in {"terminating", "suspect"}
                ):
                    raise ValueError("stored safe MPS termination evidence is inconsistent")
                if lease.mps_termination_status == "failed" and (
                    not (
                        (lease.kind == "execution" and lease.parent_lease_id is None)
                        or (lease.kind == "residency" and lease.component == "dft")
                    )
                    or lease.status != "suspect"
                ):
                    raise ValueError("stored failed MPS termination evidence is inconsistent")
                workload_values = (
                    lease.workload_pid,
                    lease.workload_process_start_ticks,
                    lease.workload_process_group_id,
                    lease.workload_cgroup,
                )
                if any(value is not None for value in workload_values):
                    if (
                        not all(value is not None for value in workload_values)
                        or isinstance(lease.workload_pid, bool)
                        or isinstance(lease.workload_process_start_ticks, bool)
                        or isinstance(lease.workload_process_group_id, bool)
                        or not isinstance(lease.workload_pid, int)
                        or not isinstance(lease.workload_process_start_ticks, int)
                        or not isinstance(lease.workload_process_group_id, int)
                        or min(
                            lease.workload_pid,
                            lease.workload_process_start_ticks,
                            lease.workload_process_group_id,
                        )
                        <= 0
                        or not isinstance(lease.workload_cgroup, str)
                        or not lease.workload_cgroup
                    ):
                        raise ValueError("stored workload identity is invalid")
                if lease.preferred != (
                    lease.gpu_index
                    == DEVICE_POLICY[(lease.environment, lease.component)][0]
                ):
                    raise ValueError("stored preferred marker is inconsistent")
                if lease.fencing_token in {item.fencing_token for item in leases.values()}:
                    raise ValueError("duplicate fencing token")
                leases[lease.lease_id] = lease
        except (TypeError, ValueError) as exc:
            raise BrokerError("invalid_state", f"invalid persisted lease: {exc}") from exc
        for lease in leases.values():
            if lease.parent_lease_id is None:
                continue
            parent = leases.get(lease.parent_lease_id)
            if (
                parent is None
                or parent.kind != "residency"
                or parent.component != lease.component
                or parent.environment != lease.environment
                or parent.client_id != lease.client_id
                or parent.gpu_index != lease.gpu_index
                or parent.owner_pid != lease.owner_pid
                or parent.owner_process_start_ticks != lease.owner_process_start_ticks
                or parent.owner_boot_id != lease.owner_boot_id
            ):
                raise BrokerError("invalid_state", "persisted child lease has an invalid parent")
        self._leases = leases
        for lease in self._leases.values():
            # A new Broker instance must not admit work beside a restored lease
            # until its exact live owner heartbeats or activates it again.
            lease.status = "suspect"
        raw_quarantines = payload.get("quarantined_gpus", {})
        if not isinstance(raw_quarantines, dict):
            raise BrokerError("invalid_state", "invalid GPU quarantine state")
        quarantines: dict[str, dict[str, object]] = {}
        for uuid, raw in raw_quarantines.items():
            if uuid not in EXPECTED_GPU_UUIDS.values() or not isinstance(raw, dict):
                raise BrokerError("invalid_state", "invalid GPU quarantine entry")
            reason = raw.get("reason")
            if reason not in QUARANTINE_REASONS:
                raise BrokerError("invalid_state", "invalid GPU quarantine reason")
            if raw.get("gpu_uuid") != uuid:
                raise BrokerError("invalid_state", "GPU quarantine UUID mismatch")
            quarantines[uuid] = dict(raw)
        self._quarantined_gpus = quarantines
        self._next_fencing_token = max(
            next_token,
            max((lease.fencing_token for lease in leases.values()), default=0) + 1,
        )
        raw_waiters = payload.get("waiters", [])
        if not isinstance(raw_waiters, list):
            raise BrokerError("invalid_state", "invalid acquire waiter list")
        waiters: dict[str, _Waiter] = {}
        try:
            for raw in raw_waiters:
                waiter = _Waiter(**raw)
                if (
                    _REQUEST_ID_RE.fullmatch(waiter.request_id) is None
                    or waiter.request_id in waiters
                    or waiter.sequence <= 0
                    or waiter.priority not in {0, 1}
                    or waiter.expires_at <= 0
                ):
                    raise ValueError("invalid persisted waiter")
                owner = OwnerIdentity(
                    waiter.owner_pid,
                    waiter.owner_process_start_ticks,
                    waiter.owner_boot_id,
                )
                if waiter.expires_at <= self._now() or not self._process_alive(owner):
                    continue
                self._validate_request(
                    request_id=waiter.request_id,
                    kind=waiter.kind,
                    placement=waiter.placement,
                    component=waiter.component,
                    environment=waiter.environment,
                    client_id=waiter.client_id,
                    owner=owner,
                    memory_mib=waiter.memory_mib,
                    thread_percent=waiter.thread_percent,
                    parent_lease_id=waiter.parent_lease_id,
                )
                waiters[waiter.request_id] = waiter
        except (TypeError, ValueError) as exc:
            raise BrokerError("invalid_state", f"invalid persisted waiter: {exc}") from exc
        self._waiters = waiters
        raw_next_wait = payload.get("next_wait_sequence", 1)
        if (
            isinstance(raw_next_wait, bool)
            or not isinstance(raw_next_wait, int)
            or raw_next_wait <= 0
        ):
            raise BrokerError("invalid_state", "invalid waiter sequence counter")
        self._next_wait_sequence = max(
            raw_next_wait,
            max((waiter.sequence for waiter in waiters.values()), default=0) + 1,
        )
        self._draining = payload.get("draining") is True

    def _require_healthy_locked(self) -> None:
        if self._fatal_persistence_error is not None:
            raise BrokerPersistenceFatal(
                "GPU Broker persistence is indeterminate; restart required"
            )

    def _persist_locked(self) -> None:
        self._require_healthy_locked()
        try:
            parent = self.state_path.parent
            if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
                raise BrokerError("unsafe_state", "broker state directory is unsafe")
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(parent, 0o700)
            payload = {
                "schema_version": 1,
                "draining": self._draining,
                "next_fencing_token": self._next_fencing_token,
                "next_wait_sequence": self._next_wait_sequence,
                "quarantined_gpus": self._quarantined_gpus,
                "leases": [
                    asdict(lease)
                    for lease in sorted(
                        self._leases.values(), key=lambda item: item.fencing_token
                    )
                ],
                "waiters": [
                    asdict(waiter)
                    for waiter in sorted(
                        self._waiters.values(), key=lambda item: item.sequence
                    )
                ],
            }
            lease_authority_fingerprint = json.dumps(
                {
                    "draining": payload["draining"],
                    "next_fencing_token": payload["next_fencing_token"],
                    "quarantined_gpus": payload["quarantined_gpus"],
                    "leases": [
                        {
                            key: value
                            for key, value in record.items()
                            if key != "heartbeat_at"
                        }
                        for record in payload["leases"]
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            temporary = parent / f".{self.state_path.name}.{os.getpid()}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(temporary, flags, 0o600)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.state_path)
                os.chmod(self.state_path, 0o600)
                directory_fd = os.open(
                    parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                if self._committed_lease_authority_fingerprint is None:
                    self._committed_lease_authority_fingerprint = (
                        lease_authority_fingerprint
                    )
                elif (
                    lease_authority_fingerprint
                    != self._committed_lease_authority_fingerprint
                ):
                    self._lease_authority_sequence += 1
                    self._committed_lease_authority_fingerprint = (
                        lease_authority_fingerprint
                    )
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        except Exception as exc:
            if self._fatal_persistence_error is None:
                self._fatal_persistence_error = type(exc).__name__
                logger.critical(
                    "GPU Broker persistence failed; refusing further authority "
                    "operations until restart",
                    exc_info=True,
                )
                self._condition.notify_all()
            raise BrokerPersistenceFatal(
                "GPU Broker persistence is indeterminate; restart required"
            ) from exc
