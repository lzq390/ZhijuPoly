"""GPU Broker boundary used by the CPU-only DFT supervisor.

The concrete host Broker may evolve independently.  The Worker depends only
on this narrow lease/fencing interface and never makes scheduling decisions
from transient NVML free-memory readings.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from .config import (
    GPU_UUID_BY_INDEX,
    validate_dev_runtime_path,
    validate_private_dev_runtime_root,
)


STABLE_ACQUIRE_COLLECTION_GRACE_SECONDS = 1.0
STABLE_ACQUIRE_SCHEDULING_ALLOWANCE_SECONDS = 2.0
DEFAULT_BROKER_CLIENT_TIMEOUT_SECONDS = 12.0

# These codes are emitted only when HostGpuBroker.acquire has authoritatively
# proved that this request owns no lease (or that its waiter was removed).
# Everything else is ambiguous at this adapter boundary because
# ``acquire_managed`` also activates the lease and may report an error after
# allocation. In particular, Broker state persistence failures can surface as
# ``internal_error`` or ``unsafe_state`` after the in-memory lease was created.
_AUTHORITATIVE_ACQUIRE_NO_LEASE_CODES = frozenset(
    {
        "acquire_cancelled",
        "broker_draining",
        "gpu_capacity_unavailable",
        "invalid_budget",
        "invalid_owner",
        "request_id_conflict",
    }
)


class GpuBrokerError(RuntimeError):
    pass


class GpuCapacityUnavailable(GpuBrokerError):
    pass


class GpuRuntimeUnhealthy(GpuBrokerError):
    pass


class GpuLeaseLost(GpuBrokerError):
    pass


class GpuAcquireCancelled(GpuBrokerError):
    pass


class GpuTerminationUnsafe(GpuRuntimeUnhealthy):
    pass


@dataclass(frozen=True, slots=True)
class GpuLease:
    lease_id: str
    gpu_index: str
    gpu_uuid: str
    kind: Literal["residency", "execution"]
    budget_mib: int
    active_thread_percentage: int
    fencing_token: int
    preferred: bool
    broker_instance_id: str
    placement: Literal["preferred", "overflow"] = "preferred"
    parent_lease_id: str | None = None
    client_environment: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "gpu_index": self.gpu_index,
            "gpu_uuid": self.gpu_uuid,
            "kind": self.kind,
            "budget_mib": self.budget_mib,
            "active_thread_percentage": self.active_thread_percentage,
            "fencing_token": self.fencing_token,
            "preferred": self.preferred,
            "broker_instance_id": self.broker_instance_id,
            "placement": self.placement,
            "parent_lease_id": self.parent_lease_id,
        }


class GpuBrokerClient(Protocol):
    def acquire(
        self,
        *,
        kind: Literal["residency", "execution"],
        gpu_index: str,
        budget_mib: int,
        active_thread_percentage: int,
        preferred: bool,
        placement: Literal["preferred", "overflow"],
        parent_lease_id: str | None,
        owner: dict[str, Any],
        wait_timeout_seconds: float = 0.0,
        cancelled: Any | None = None,
    ) -> GpuLease: ...

    def activate(self, lease: GpuLease, *, pid: int, process_start_time: int) -> None: ...

    def heartbeat(self, lease: GpuLease) -> None: ...

    def release(self, lease: GpuLease) -> None: ...

    def quarantine(self, lease: GpuLease, *, reason: str) -> None: ...

    def prepare_process_termination(self, lease: GpuLease) -> dict[str, Any]: ...

    def abandon(self, lease: GpuLease) -> None: ...


class DisabledBrokerClient:
    """Process-local leases for an explicitly Broker-disabled direct mode.

    This is not a host capacity or security authority. Development uses it
    only for an audited idle-GPU smoke; production direct mode relies on the
    separate GPU2 guard and configuration policy. It preserves the same lease
    and fencing contract so later enabling the host Broker does not change
    Worker execution semantics.
    """

    def __init__(self, *, blocked_devices: set[str] | None = None) -> None:
        self.managed_placement = False
        self._blocked = set(blocked_devices or ())
        self._quarantined: set[str] = set()
        self._leases: dict[str, GpuLease] = {}
        self._next_fence = 1
        self._lock = threading.Lock()
        self.instance_id = f"disabled-{uuid.uuid4().hex}"

    def acquire(
        self,
        *,
        kind: Literal["residency", "execution"],
        gpu_index: str,
        budget_mib: int,
        active_thread_percentage: int,
        preferred: bool,
        placement: Literal["preferred", "overflow"],
        parent_lease_id: str | None,
        owner: dict[str, Any],
        wait_timeout_seconds: float = 0.0,
        cancelled: Any | None = None,
    ) -> GpuLease:
        del owner, wait_timeout_seconds
        if callable(cancelled) and cancelled():
            raise GpuAcquireCancelled("GPU acquisition was cancelled")
        if gpu_index == "0" or gpu_index not in GPU_UUID_BY_INDEX:
            raise GpuRuntimeUnhealthy("GPU identity is outside the approved inventory")
        with self._lock:
            if gpu_index in self._blocked:
                raise GpuCapacityUnavailable(f"GPU {gpu_index} has no admitted capacity")
            if gpu_index in self._quarantined:
                raise GpuRuntimeUnhealthy(f"GPU {gpu_index} is quarantined")
            lease = GpuLease(
                lease_id=uuid.uuid4().hex,
                gpu_index=gpu_index,
                gpu_uuid=GPU_UUID_BY_INDEX[gpu_index],
                kind=kind,
                budget_mib=budget_mib,
                active_thread_percentage=active_thread_percentage,
                fencing_token=self._next_fence,
                preferred=preferred,
                broker_instance_id=self.instance_id,
                placement=placement,
                parent_lease_id=parent_lease_id,
            )
            self._next_fence += 1
            self._leases[lease.lease_id] = lease
            return lease

    def activate(self, lease: GpuLease, *, pid: int, process_start_time: int) -> None:
        if pid <= 0 or process_start_time <= 0:
            raise GpuLeaseLost("executor process identity is invalid")
        self._require(lease)

    def heartbeat(self, lease: GpuLease) -> None:
        self._require(lease)

    def release(self, lease: GpuLease) -> None:
        with self._lock:
            current = self._leases.get(lease.lease_id)
            if current is not None and current.fencing_token == lease.fencing_token:
                self._leases.pop(lease.lease_id, None)

    def quarantine(self, lease: GpuLease, *, reason: str) -> None:
        del reason
        with self._lock:
            self._quarantined.add(lease.gpu_index)
            for lease_id, current in tuple(self._leases.items()):
                if current.gpu_index == lease.gpu_index:
                    self._leases.pop(lease_id, None)

    def prepare_process_termination(self, lease: GpuLease) -> dict[str, Any]:
        self._require(lease)
        return {
            "safe_to_signal": True,
            "client_pids": [],
            "prepared_at": time.time(),
        }

    def abandon(self, lease: GpuLease) -> None:
        self.quarantine(lease, reason="gpu_runtime_corruption")

    def _require(self, lease: GpuLease) -> None:
        with self._lock:
            current = self._leases.get(lease.lease_id)
            if current is None or current.fencing_token != lease.fencing_token:
                raise GpuLeaseLost("GPU lease is no longer current")


class SharedGpuBrokerAdapter:
    """DFT adapter over the repository-wide newline-JSON Broker client.

    The import is deliberately lazy: the GPU resource layer lands before this
    Worker branch in the integration stack. No second Broker wire protocol is
    implemented here.
    """

    def __init__(
        self,
        uds: Path,
        *,
        environment: str,
        client_id: str,
        mps_pipe_root: Path,
        dev_runtime_root: Path,
        mps_pipe_directories: tuple[tuple[int, Path], ...] = (),
    ) -> None:
        if environment != "dev":
            raise GpuRuntimeUnhealthy(
                "DFT Worker Broker environment must be dev; production is hard-off"
            )
        try:
            runtime_root = validate_private_dev_runtime_root(dev_runtime_root)
            uds = validate_dev_runtime_path(
                "MONOMER_DFT_GPU_BROKER_UDS",
                uds,
                runtime_root=runtime_root,
                leaf_kind="socket",
            )
            mps_pipe_root = validate_dev_runtime_path(
                "MONOMER_DFT_GPU_MPS_PIPE_ROOT",
                mps_pipe_root,
                runtime_root=runtime_root,
                leaf_kind="directory",
            )
        except ValueError as exc:
            raise GpuRuntimeUnhealthy(str(exc)) from exc
        try:
            from gpu_resource import GpuBrokerClient as SharedClient
            from gpu_resource import GpuBrokerClientError
            from gpu_resource import mps_client_environment
        except ImportError as exc:  # fail closed before DFT submission opens.
            raise GpuRuntimeUnhealthy(
                "shared GPU Broker client is not installed in this release"
            ) from exc
        self._error_type = GpuBrokerClientError
        self._mps_client_environment = mps_client_environment
        self._client = SharedClient(
            uds,
            timeout_seconds=DEFAULT_BROKER_CLIENT_TIMEOUT_SECONDS,
        )
        self.managed_placement = True
        self.environment = environment
        self.client_id = client_id
        self.mps_pipe_root = mps_pipe_root
        self.mps_pipe_directories = dict(mps_pipe_directories)
        self._managed: dict[str, Any] = {}
        self._lease_contracts: dict[str, GpuLease] = {}
        self._managed_lock = threading.RLock()
        self._inflight_acquire_lock = threading.Lock()
        self._inflight_acquire_request_ids: set[str] = set()
        self._admission_uncertain = False

    @property
    def admission_uncertain(self) -> bool:
        with self._inflight_acquire_lock:
            return bool(getattr(self, "_admission_uncertain", False))

    def acquire(
        self,
        *,
        kind: Literal["residency", "execution"],
        gpu_index: str,
        budget_mib: int,
        active_thread_percentage: int,
        preferred: bool,
        placement: Literal["preferred", "overflow"],
        parent_lease_id: str | None,
        owner: dict[str, Any],
        wait_timeout_seconds: float = 0.0,
        cancelled: Any | None = None,
    ) -> GpuLease:
        with self._inflight_acquire_lock:
            if bool(getattr(self, "_admission_uncertain", False)):
                raise GpuRuntimeUnhealthy(
                    "a previous GPU admission has unresolved ownership; "
                    "the Worker must be restarted"
                )
        request_id = str(owner.get("request_id") or "")
        if not request_id:
            request_identity = json.dumps(
                {
                    "client_id": self.client_id,
                    "kind": kind,
                    "placement": placement,
                    "parent_lease_id": parent_lease_id,
                    "owner": owner,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            request_id = "dft-" + hashlib.sha256(request_identity).hexdigest()
        acquire_arguments = {
            "kind": kind,
            "placement": placement,
            "component": "dft",
            "environment": self.environment,
            "client_id": self.client_id,
            "memory_mib": budget_mib,
            "thread_percent": active_thread_percentage,
            "wait_timeout_seconds": max(0.0, float(wait_timeout_seconds)),
            "heartbeat_interval_seconds": 5.0,
            "parent_lease_id": parent_lease_id,
            "request_id": request_id,
        }
        try:
            managed = self._acquire_managed_cancellable(
                acquire_arguments,
                request_id=request_id,
                wait_timeout_seconds=max(0.0, float(wait_timeout_seconds)),
                cancelled=cancelled,
            )
        except self._error_type as exc:
            self._raise_shared(exc)
        shared = managed.lease
        expected = {
            "kind": kind,
            "placement": placement,
            "component": "dft",
            "environment": self.environment,
            "client_id": self.client_id,
            "memory_mib": budget_mib,
            "thread_percent": active_thread_percentage,
            "parent_lease_id": parent_lease_id,
            "request_id": request_id,
        }
        if any(getattr(shared, key, None) != value for key, value in expected.items()):
            with contextlib.suppress(Exception):
                managed.close()
            raise GpuRuntimeUnhealthy(
                "GPU Broker returned a lease outside the DFT resource contract"
            )
        mps_arguments: dict[str, Any] = {
            "pipe_root": self.mps_pipe_root,
        }
        if self.mps_pipe_directories:
            mps_arguments["pipe_directories"] = self.mps_pipe_directories
        try:
            client_environment = self._mps_client_environment(
                shared,
                **mps_arguments,
            )
        except self._error_type as exc:
            with contextlib.suppress(Exception):
                managed.close()
            self._raise_shared(exc)
        expected_pipe_directory = self.mps_pipe_directories.get(
            shared.gpu_index,
            self.mps_pipe_root / f"mps-{shared.gpu_index}" / "pipe",
        )
        expected_environment = {
            "CUDA_VISIBLE_DEVICES": shared.gpu_uuid,
            "CUDA_MPS_PIPE_DIRECTORY": str(expected_pipe_directory),
            "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(active_thread_percentage),
            "CUDA_MPS_CLIENT_PRIORITY": "1",
            "CUDA_MPS_PINNED_DEVICE_MEM_LIMIT": (
                f"{shared.gpu_uuid}={budget_mib}M"
            ),
        }
        if client_environment != expected_environment:
            with contextlib.suppress(Exception):
                managed.fail_closed()
            raise GpuRuntimeUnhealthy(
                "shared MPS client environment differs from the DFT lease"
            )
        lease = GpuLease(
            lease_id=shared.lease_id,
            gpu_index=str(shared.gpu_index),
            gpu_uuid=shared.gpu_uuid,
            kind=kind,
            budget_mib=shared.memory_mib,
            active_thread_percentage=shared.thread_percent,
            fencing_token=shared.fencing_token,
            preferred=shared.preferred,
            broker_instance_id=shared.broker_instance_id,
            placement=placement,
            parent_lease_id=shared.parent_lease_id,
            client_environment=tuple(sorted(client_environment.items())),
        )
        policy = ("1", "3")
        allowed_indices = policy[:1] if placement == "preferred" else policy[1:]
        if lease.gpu_index not in allowed_indices or lease.preferred != preferred:
            with contextlib.suppress(Exception):
                managed.close()
            raise GpuCapacityUnavailable(
                "GPU Broker selected a different policy position"
            )
        with self._managed_lock:
            self._managed[lease.lease_id] = managed
            self._lease_contracts[lease.lease_id] = lease
        return lease

    def _acquire_managed_cancellable(
        self,
        acquire_arguments: dict[str, Any],
        *,
        request_id: str,
        wait_timeout_seconds: float,
        cancelled: Any | None,
    ) -> Any:
        with self._inflight_acquire_lock:
            if request_id in self._inflight_acquire_request_ids:
                raise GpuRuntimeUnhealthy(
                    "a stable GPU capacity waiter is already owned by this Worker"
                )
            self._inflight_acquire_request_ids.add(request_id)

        try:
            return self._acquire_managed_cancellable_owned(
                acquire_arguments,
                request_id=request_id,
                wait_timeout_seconds=wait_timeout_seconds,
                cancelled=cancelled,
            )
        finally:
            # The owned helper never returns until its acquire thread has an
            # authoritative terminal response and any raced lease is closed.
            with self._inflight_acquire_lock:
                self._inflight_acquire_request_ids.discard(request_id)

    def _acquire_managed_cancellable_owned(
        self,
        acquire_arguments: dict[str, Any],
        *,
        request_id: str,
        wait_timeout_seconds: float,
        cancelled: Any | None,
    ) -> Any:
        finished = threading.Event()
        cancel_requested = threading.Event()
        cancel_authoritative = threading.Event()
        cancel_decided = threading.Event()
        result: dict[str, Any] = {}
        attempt_state_lock = threading.Lock()
        attempt_generation = 0
        attempt_inflight_generation: int | None = None
        ambiguous_cancel_generation: int | None = None
        original_timeout = max(0.0, wait_timeout_seconds)
        admission_deadline = time.monotonic() + original_timeout
        client_timeout = max(
            0.0,
            float(
                getattr(
                    self._client,
                    "timeout_seconds",
                    DEFAULT_BROKER_CLIENT_TIMEOUT_SECONDS,
                )
            ),
        )
        local_deadline = (
            admission_deadline
            + (2.0 * client_timeout)
            + STABLE_ACQUIRE_SCHEDULING_ALLOWANCE_SECONDS
        )

        def finish_admission_uncertain(message: str) -> None:
            with self._inflight_acquire_lock:
                self._admission_uncertain = True
            result["admission_uncertain"] = GpuRuntimeUnhealthy(message)
            finished.set()

        def acquire_waiter() -> None:
            nonlocal attempt_generation
            nonlocal attempt_inflight_generation
            nonlocal ambiguous_cancel_generation
            recovery_backoff_seconds = 0.05
            while True:
                if callable(cancelled) and cancelled():
                    with attempt_state_lock:
                        cancel_requested.set()

                wait_for_cancel_decision = False
                cancel_without_acquire = False
                unsafe_authoritative_cancel = False
                with attempt_state_lock:
                    if cancel_requested.is_set() and not cancel_decided.is_set():
                        wait_for_cancel_decision = True
                    elif cancel_authoritative.is_set():
                        unsafe_authoritative_cancel = bool(
                            ambiguous_cancel_generation is not None
                        )
                        cancel_without_acquire = not unsafe_authoritative_cancel
                    elif cancel_requested.is_set() and attempt_generation == 0:
                        # Cancellation won before this owner issued any acquire
                        # RPC. A negative cancel response proves there is no
                        # request identity to recover.
                        cancel_without_acquire = True
                    else:
                        attempt_generation += 1
                        current_generation = attempt_generation
                        attempt_inflight_generation = current_generation

                if wait_for_cancel_decision:
                    # A cancel RPC may already be linearized in the Broker while
                    # its response is still in transit. Do not start another
                    # same-ID recovery until the decision is known.
                    cancel_decided.wait()
                    continue
                if unsafe_authoritative_cancel:
                    finish_admission_uncertain(
                        "an ambiguous recovery acquire raced an authoritative "
                        "Broker cancellation; ownership is unresolved"
                    )
                    return
                if cancel_without_acquire:
                    result["cancelled"] = True
                    finished.set()
                    return

                call_arguments = dict(acquire_arguments)
                call_arguments["wait_timeout_seconds"] = (
                    original_timeout
                    if current_generation == 1
                    else max(0.0, admission_deadline - time.monotonic())
                )
                if cancel_requested.is_set():
                    # An ambiguous/negative cancel decision can hide a raced
                    # allocation. Recover only that exact stable request ID and
                    # never leave another long-lived waiter behind.
                    call_arguments["wait_timeout_seconds"] = 0.0
                try:
                    managed = self._client.acquire_managed(**call_arguments)
                except self._error_type as exc:
                    # Fail closed by proof, not by an error blacklist. The
                    # shared managed-acquire spans acquire, optional parent
                    # lookup, activation and cleanup; most errors therefore do
                    # not prove that allocation never happened. Recover the
                    # exact stable request ID unless the Broker returned one of
                    # the small, acquire-only terminal codes above.
                    ambiguous_response = (
                        getattr(exc, "code", None)
                        not in _AUTHORITATIVE_ACQUIRE_NO_LEASE_CODES
                    )
                    with attempt_state_lock:
                        if attempt_inflight_generation == current_generation:
                            attempt_inflight_generation = None
                        if (
                            ambiguous_response
                            and cancel_requested.is_set()
                        ):
                            # Any acquire generation that overlapped cancel may
                            # have reached the Broker after cancel=True removed
                            # a same-ID waiter. The request ID is intentionally
                            # stable across transport recovery, and the Broker
                            # has no cancellation tombstone proving that an
                            # ambiguous response did not create a post-cancel
                            # lease. This includes the first local generation:
                            # the same identity may already exist in the Broker.
                            ambiguous_cancel_generation = current_generation
                        unsafe_after_cancel = bool(
                            ambiguous_response
                            and cancel_authoritative.is_set()
                            and ambiguous_cancel_generation == current_generation
                        )
                    if unsafe_after_cancel:
                        finish_admission_uncertain(
                            "an ambiguous recovery acquire raced an authoritative "
                            "Broker cancellation; ownership is unresolved"
                        )
                        return
                    if not ambiguous_response:
                        result["error"] = exc
                        finished.set()
                        return
                    # A transport or malformed-response error is not evidence
                    # that the Broker did not allocate.  Retrying the exact same
                    # request ID is the only safe recovery operation.  Once the
                    # admission deadline passes retries become zero-wait probes;
                    # they continue until the Broker proves grant or terminal
                    # rejection, so no live-owner suspect lease is orphaned.
                    cancel_requested.wait(recovery_backoff_seconds)
                    recovery_backoff_seconds = min(
                        recovery_backoff_seconds * 2.0,
                        1.0,
                    )
                    continue
                except BaseException as exc:
                    with attempt_state_lock:
                        if attempt_inflight_generation == current_generation:
                            attempt_inflight_generation = None
                    result["error"] = exc
                    finished.set()
                    return

                with attempt_state_lock:
                    if attempt_inflight_generation == current_generation:
                        attempt_inflight_generation = None

                if cancel_requested.is_set():
                    # Cancellation may race a grant. Collect and close that
                    # exact managed lease instead of leaving untracked budget.
                    try:
                        managed.close()
                    except BaseException as cleanup_error:
                        with contextlib.suppress(Exception):
                            managed.fail_closed()
                        with self._inflight_acquire_lock:
                            self._admission_uncertain = True
                        result["cleanup_error"] = cleanup_error
                        finished.set()
                        return
                    result["cancelled"] = True
                else:
                    result["managed"] = managed
                finished.set()
                return

        waiter = threading.Thread(
            target=acquire_waiter,
            name=f"dft-gpu-acquire-{request_id[-12:]}",
            daemon=True,
        )
        waiter.start()
        cancellation_observed = False
        user_cancellation_observed = False
        deadline_exceeded = False
        cancel_error: BaseException | None = None
        while not finished.wait(0.05):
            user_cancelled = callable(cancelled) and bool(cancelled())
            user_cancellation_observed = user_cancellation_observed or user_cancelled
            deadline_exceeded = deadline_exceeded or time.monotonic() >= local_deadline
            if not user_cancelled and not deadline_exceeded:
                continue
            if cancellation_observed:
                if finished.wait(STABLE_ACQUIRE_COLLECTION_GRACE_SECONDS):
                    break
                with self._inflight_acquire_lock:
                    self._admission_uncertain = True
                if deadline_exceeded and not user_cancelled:
                    raise GpuRuntimeUnhealthy(
                        "GPU admission ownership could not be resolved within "
                        "the bounded transport deadline"
                    ) from cancel_error
                cancellation = GpuAcquireCancelled(
                    "GPU acquisition cancellation is unresolved; Worker restart required"
                )
                if cancel_error is not None:
                    raise cancellation from cancel_error
                raise cancellation
            cancellation_observed = True
            with attempt_state_lock:
                cancel_requested.set()
            try:
                cancelled_authoritatively = (
                    self._client.cancel_acquire(request_id) is True
                )
            except BaseException as exc:
                # Transport uncertainty is not an authoritative cancellation.
                # Continue to collect the original acquire thread; if it was
                # granted, acquire_waiter closes that exact managed lease.
                cancel_error = exc
                cancelled_authoritatively = False
            finally:
                # Release the recovery-owner barrier for both authoritative and
                # ambiguous cancellation decisions.
                with attempt_state_lock:
                    if cancelled_authoritatively:
                        cancel_authoritative.set()
                    cancel_decided.set()

        # Cancellation can become visible after the ownership thread stores a
        # grant but before this caller consumes it. Linearize the cancellation
        # decision here as well so the grant is either recovered and closed or
        # known to have survived the Broker cancel race.
        if (
            not cancellation_observed
            and callable(cancelled)
            and bool(cancelled())
        ):
            cancellation_observed = True
            user_cancellation_observed = True
            with attempt_state_lock:
                cancel_requested.set()
            # The acquire owner already returned a concrete managed grant, so
            # no Broker decision is needed to identify it. Close that exact
            # object below; a cancel RPC here would only add another transport
            # race after ownership is already known.
            with attempt_state_lock:
                cancel_decided.set()

        cancellation_observed = cancellation_observed or cancel_requested.is_set() or (
            callable(cancelled) and bool(cancelled())
        )
        error = result.get("error")
        admission_uncertain_error = result.get("admission_uncertain")
        if isinstance(admission_uncertain_error, GpuRuntimeUnhealthy):
            raise admission_uncertain_error
        if cancellation_observed:
            if result.get("cleanup_error") is not None:
                raise GpuRuntimeUnhealthy(
                    "a raced GPU lease could not be released safely"
                ) from result["cleanup_error"]
            late_grant = result.get("managed")
            if late_grant is not None:
                # Cancellation can become visible after acquire_waiter stores a
                # successful grant but before this owner consumes it.  The grant
                # is not registered in ``_managed`` yet, so this boundary is the
                # only code able to close that exact lease.
                try:
                    late_grant.close()
                except BaseException as cleanup_error:
                    with contextlib.suppress(Exception):
                        late_grant.fail_closed()
                    with self._inflight_acquire_lock:
                        self._admission_uncertain = True
                    raise GpuRuntimeUnhealthy(
                        "a late GPU grant could not be released safely"
                    ) from cleanup_error
            if error is not None and not isinstance(error, self._error_type):
                raise error
            if deadline_exceeded and not user_cancellation_observed:
                raise GpuRuntimeUnhealthy(
                    "GPU admission exceeded its bounded transport deadline"
                ) from cancel_error
            cancellation = GpuAcquireCancelled("GPU acquisition was cancelled")
            if cancel_error is not None:
                raise cancellation from cancel_error
            raise cancellation
        if error is not None:
            raise error
        if result.get("cleanup_error") is not None:
            raise GpuRuntimeUnhealthy(
                "a raced GPU lease could not be released safely"
            ) from result["cleanup_error"]
        if result.get("cancelled") is True:
            raise GpuAcquireCancelled("GPU acquisition was cancelled")
        managed = result.get("managed")
        if managed is None:
            raise GpuRuntimeUnhealthy("GPU Broker waiter returned without a lease")
        return managed

    def activate(self, lease: GpuLease, *, pid: int, process_start_time: int) -> None:
        managed = self._managed_lease(lease)
        try:
            if read_process_start_time(pid) != process_start_time:
                managed.fail_closed()
                raise GpuLeaseLost(
                    "executor PID was reused before Broker workload registration"
                )
            if lease.kind == "execution" and lease.parent_lease_id is not None:
                with self._managed_lock:
                    parent = self._lease_contracts.get(lease.parent_lease_id)
                if (
                    parent is None
                    or parent.kind != "residency"
                    or parent.gpu_uuid != lease.gpu_uuid
                ):
                    managed.fail_closed()
                    raise GpuLeaseLost(
                        "parented execution lease lost its resident workload"
                    )
                # The Broker inherits the already-fenced resident workload.
                # Registering it again would create ambiguous MPS ownership.
                managed.assert_healthy()
                return
            registered = managed.register_workload(pid)
            if (
                registered.lease_id != lease.lease_id
                or registered.fencing_token != lease.fencing_token
                or str(registered.gpu_index) != lease.gpu_index
                or registered.gpu_uuid != lease.gpu_uuid
            ):
                managed.fail_closed()
                raise GpuLeaseLost(
                    "Broker workload registration changed the fenced lease identity"
                )
            managed.assert_healthy()
        except self._error_type as exc:
            self._raise_shared(exc)

    def heartbeat(self, lease: GpuLease) -> None:
        managed = self._managed_lease(lease)
        try:
            # Result acceptance uses this method after CUDA synchronization, so
            # cached heartbeat state is insufficient. ``confirm_current`` is a
            # synchronous, lease-lock-serialized Broker heartbeat that proves
            # the exact fencing token is still current at this boundary.
            confirmed = managed.confirm_current()
            if (
                confirmed.lease_id != lease.lease_id
                or confirmed.fencing_token != lease.fencing_token
                or str(confirmed.gpu_index) != lease.gpu_index
                or confirmed.gpu_uuid != lease.gpu_uuid
                or confirmed.broker_instance_id != lease.broker_instance_id
            ):
                with contextlib.suppress(Exception):
                    managed.fail_closed()
                raise GpuLeaseLost(
                    "synchronous Broker confirmation changed the fenced lease identity"
                )
        except self._error_type as exc:
            self._raise_shared(exc)

    def release(self, lease: GpuLease) -> None:
        with self._managed_lock:
            managed = self._managed.get(lease.lease_id)
        if managed is None:
            return
        try:
            managed.close()
        except self._error_type as exc:
            self._raise_shared(exc)
        with self._managed_lock:
            if self._managed.get(lease.lease_id) is managed:
                self._managed.pop(lease.lease_id, None)
                self._lease_contracts.pop(lease.lease_id, None)

    def quarantine(self, lease: GpuLease, *, reason: str) -> None:
        managed = self._managed_lease(lease)
        shared_reason = "gpu_fatal" if reason == "cuda_fatal" else reason
        try:
            managed.quarantine(reason=shared_reason)
        except self._error_type as exc:
            self._raise_shared(exc)

    def prepare_process_termination(self, lease: GpuLease) -> dict[str, Any]:
        managed = self._managed_lease(lease)
        try:
            evidence = managed.prepare_process_termination()
        except self._error_type as exc:
            with contextlib.suppress(Exception):
                managed.quarantine(reason="gpu_runtime_corruption")
            raise GpuTerminationUnsafe(str(exc)) from exc
        if evidence.get("safe_to_signal") is not True:
            with contextlib.suppress(Exception):
                managed.fail_closed()
            raise GpuTerminationUnsafe(
                "GPU Broker did not prove MPS termination safe"
            )
        return dict(evidence)

    def abandon(self, lease: GpuLease) -> None:
        managed = self._managed_lease(lease)
        with contextlib.suppress(Exception):
            managed.quarantine(reason="gpu_runtime_corruption")
        managed.fail_closed()

    def _managed_lease(self, lease: GpuLease) -> Any:
        with self._managed_lock:
            managed = self._managed.get(lease.lease_id)
            contract = self._lease_contracts.get(lease.lease_id)
        if managed is None or contract != lease:
            raise GpuLeaseLost("GPU lease is no longer managed by this Worker")
        return managed

    def _raise_shared(self, exc: BaseException) -> None:
        code = getattr(exc, "code", "gpu_broker_unavailable")
        if code == "acquire_cancelled":
            raise GpuAcquireCancelled(str(exc)) from exc
        if code == "gpu_capacity_unavailable":
            raise GpuCapacityUnavailable(str(exc)) from exc
        if code in {
            "gpu_lease_lost",
            "unknown_lease",
            "stale_fencing_token",
            "lease_owner_mismatch",
            "request_owner_mismatch",
            "workload_identity_unavailable",
            "workload_identity_mismatch",
        }:
            raise GpuLeaseLost(str(exc)) from exc
        if code in {
            "gpu_runtime_unhealthy",
            "gpu_inventory_unavailable",
            "gpu_uuid_mismatch",
            "gpu_quarantined",
            "external_inventory_unavailable",
            "gpu_process_inventory_unavailable",
        }:
            raise GpuRuntimeUnhealthy(str(exc)) from exc
        raise GpuBrokerError(str(exc)) from exc


def process_start_time(pid: int) -> int:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        command_end = raw.rfind(")")
        if command_end < 0:
            raise ValueError("missing process command terminator")
        # The command in field 2 may contain spaces and parentheses. Fields
        # after its final ')' start at proc field 3 (state), so zero-based
        # suffix field 19 is proc field 22 (starttime).
        suffix_fields = raw[command_end + 1 :].split()
        start_time = int(suffix_fields[19])
        if start_time <= 0:
            raise ValueError("invalid process start time")
        return start_time
    except (OSError, IndexError, ValueError) as exc:
        raise GpuLeaseLost("unable to establish executor process start time") from exc


read_process_start_time = process_start_time


def monotonic_milliseconds() -> int:
    return int(time.monotonic() * 1000)


def verify_host_gpu_inventory(indices: tuple[str, ...]) -> None:
    """Fail closed if the approved index-to-UUID mapping drifted."""
    try:
        completed = subprocess.run(
            (
                "nvidia-smi",
                "--query-gpu=index,uuid",
                "--format=csv,noheader,nounits",
            ),
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GpuRuntimeUnhealthy("unable to audit the host GPU inventory") from exc
    actual: dict[str, str] = {}
    for row in completed.stdout.splitlines():
        fields = [field.strip() for field in row.split(",", 1)]
        if len(fields) == 2:
            actual[fields[0]] = fields[1]
    for index in indices:
        if index == "0" or actual.get(index) != GPU_UUID_BY_INDEX.get(index):
            raise GpuRuntimeUnhealthy(
                f"GPU {index} UUID does not match the approved host inventory"
            )


def audit_isolated_gpu_availability(indices: tuple[str, ...]) -> set[str]:
    """Return GPUs unavailable to broker-disabled, exclusive dev smoke.

    This path is intentionally conservative. Shared Backend/DFT/MD operation
    requires the Host Broker; the fallback merely permits an otherwise idle
    development machine to exercise the executor split.
    """
    blocked: set[str] = set()
    try:
        completed = subprocess.run(
            (
                "nvidia-smi",
                "--query-compute-apps=gpu_uuid,pid",
                "--format=csv,noheader,nounits",
            ),
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GpuRuntimeUnhealthy("unable to audit live CUDA processes") from exc
    uuid_to_index = {uuid: index for index, uuid in GPU_UUID_BY_INDEX.items()}
    for row in completed.stdout.splitlines():
        fields = [field.strip() for field in row.split(",", 1)]
        if len(fields) != 2 or not fields[1].isdigit():
            raise GpuRuntimeUnhealthy("invalid CUDA process inventory")
        index = uuid_to_index.get(fields[0])
        if index in indices:
            blocked.add(index)

    try:
        containers = subprocess.run(
            ("docker", "ps", "-q"),
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5.0,
        ).stdout.split()
        if containers:
            inspected = subprocess.run(
                ("docker", "inspect", *containers),
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10.0,
            )
            payload = json.loads(inspected.stdout)
            if not isinstance(payload, list):
                raise ValueError("docker inspect root is not an array")
            for container in payload:
                if not isinstance(container, dict):
                    raise ValueError("docker inspect entry is invalid")
                requests = container.get("HostConfig", {}).get("DeviceRequests") or []
                for request in requests:
                    if not isinstance(request, dict):
                        raise ValueError("Docker GPU request is invalid")
                    capabilities = request.get("Capabilities") or []
                    is_gpu = request.get("Driver") == "nvidia" or any(
                        isinstance(group, list) and "gpu" in group
                        for group in capabilities
                    )
                    if not is_gpu:
                        continue
                    device_ids = request.get("DeviceIDs") or []
                    if not device_ids:
                        blocked.update(indices)
                        continue
                    for raw_device in device_ids:
                        device = str(raw_device)
                        for index in indices:
                            if device in {index, GPU_UUID_BY_INDEX[index]}:
                                blocked.add(index)
                environment = container.get("Config", {}).get("Env") or []
                for item in environment:
                    if not isinstance(item, str) or not item.startswith(
                        "NVIDIA_VISIBLE_DEVICES="
                    ):
                        continue
                    visible = item.split("=", 1)[1]
                    if visible == "all":
                        blocked.update(indices)
                    else:
                        for device in visible.split(","):
                            for index in indices:
                                if device.strip() in {index, GPU_UUID_BY_INDEX[index]}:
                                    blocked.add(index)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError) as exc:
        raise GpuRuntimeUnhealthy("unable to audit Docker GPU reservations") from exc
    return blocked
