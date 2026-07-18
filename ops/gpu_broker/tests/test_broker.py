from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from gpu_resource import (
    GpuBrokerClient,
    GpuBrokerClientError,
    GpuLease,
    ManagedGpuLease,
    mps_client_environment,
)
from ops.gpu_broker.broker import (
    BrokerError,
    EXPECTED_GPU_UUIDS,
    HostGpuBroker,
    Lease,
    OwnerIdentity,
    read_boot_id,
    read_process_start_ticks,
    validate_gpu_inventory,
)
from ops.gpu_broker.server import (
    DockerGpuClaim,
    ExternalGpuGuard,
    ExternalReservationPolicy,
    GpuBrokerUnixServer,
    JobCgroupController,
    ManagedDockerClaim,
    MpsClient,
    MpsRuntimeGuard,
    SystemdGpuClaim,
    load_external_reservations,
    query_docker_gpu_claims,
    query_systemd_gpu_claims,
    resolve_workload_identity,
)


def _owner() -> OwnerIdentity:
    return OwnerIdentity(
        pid=os.getpid(),
        process_start_ticks=read_process_start_ticks(os.getpid()),
        boot_id=read_boot_id(),
    )


def _bind_test_workload(lease) -> None:
    lease.workload_pid = os.getpid()
    lease.workload_process_start_ticks = read_process_start_ticks(os.getpid())
    lease.workload_process_group_id = os.getpgid(os.getpid())
    lease.workload_cgroup = Path(f"/proc/{os.getpid()}/cgroup").read_text(
        encoding="utf-8"
    ).strip()


def _acquire(
    broker: HostGpuBroker,
    *,
    component: str,
    environment: str,
    kind: str,
    wait: float = 0,
    placement: str | None = None,
    parent_lease_id: str | None = None,
    client_id: str | None = None,
):
    budget = {"backend": 8192, "dft": 4096, "md": 8192}[component]
    if placement is None:
        placement = "any" if component == "md" else "preferred"
    return broker.acquire(
        kind=kind,
        placement=placement,
        component=component,
        environment=environment,
        client_id=client_id or f"{component}-{environment}",
        owner=_owner(),
        memory_mib=budget,
        thread_percent=100 if component == "backend" else 50,
        wait_timeout_seconds=wait,
        parent_lease_id=parent_lease_id,
    )


def test_fixed_budgets_allow_20480_mib_colocation_and_overflow(tmp_path: Path) -> None:
    broker = HostGpuBroker(
        tmp_path / "state.json",
        gpu_externally_busy=lambda index, *_args: index == 3,
    )

    backend = _acquire(broker, component="backend", environment="prod", kind="residency")
    dft = _acquire(broker, component="dft", environment="prod", kind="residency")
    md = _acquire(broker, component="md", environment="prod", kind="execution")
    overflow = _acquire(
        broker,
        component="md",
        environment="prod",
        kind="execution",
        client_id="md-prod-second-job",
    )

    assert (backend.gpu_index, dft.gpu_index, md.gpu_index) == (2, 2, 2)
    assert broker.status()["usage_mib"]["2"] == 20_480
    assert overflow.gpu_index == 1
    assert all(lease["gpu_index"] != 0 for lease in broker.status()["leases"])


def test_budget_and_device_policy_are_not_client_overridable(tmp_path: Path) -> None:
    broker = HostGpuBroker(tmp_path / "state.json")

    with pytest.raises(BrokerError, match="exactly 8192") as error:
        broker.acquire(
            kind="execution",
            placement="any",
            component="md",
            environment="prod",
            client_id="md-prod",
            owner=_owner(),
            memory_mib=4096,
            thread_percent=50,
        )
    assert error.value.code == "invalid_budget"

    with pytest.raises(BrokerError, match="exactly 50%") as error:
        broker.acquire(
            kind="execution",
            placement="any",
            component="md",
            environment="prod",
            client_id="md-prod",
            owner=_owner(),
            memory_mib=8192,
            thread_percent=100,
        )
    assert error.value.code == "invalid_budget"

    backend = _acquire(broker, component="backend", environment="dev", kind="residency")
    assert backend.gpu_index == 1


def test_legacy_acquire_identity_is_bounded_for_maximum_client_id(tmp_path: Path) -> None:
    broker = HostGpuBroker(tmp_path / "state.json")

    lease = _acquire(
        broker,
        component="md",
        environment="prod",
        kind="execution",
        client_id="x" * 128,
    )

    assert lease.request_id.startswith("legacy:")
    assert len(lease.request_id) <= 128


def test_dft_preferred_and_overflow_placement_are_strict(tmp_path: Path) -> None:
    primary_busy = HostGpuBroker(
        tmp_path / "primary-busy.json",
        gpu_externally_busy=lambda index, *_args: index == 2,
    )
    with pytest.raises(BrokerError) as error:
        _acquire(
            primary_busy,
            component="dft",
            environment="prod",
            kind="residency",
            placement="preferred",
        )
    assert error.value.code == "gpu_capacity_unavailable"
    assert primary_busy.status()["leases"] == []

    prod = HostGpuBroker(tmp_path / "prod-overflow.json")
    prod_overflow = _acquire(
        prod,
        component="dft",
        environment="prod",
        kind="execution",
        placement="overflow",
    )
    assert prod_overflow.gpu_index == 3
    assert prod_overflow.preferred is False

    prod_gpu3_busy = HostGpuBroker(
        tmp_path / "prod-gpu3-busy.json",
        gpu_externally_busy=lambda index, *_args: index == 3,
    )
    borrowed_dev_primary = _acquire(
        prod_gpu3_busy,
        component="dft",
        environment="prod",
        kind="execution",
        placement="overflow",
    )
    assert borrowed_dev_primary.gpu_index == 1

    dev = HostGpuBroker(tmp_path / "dev-overflow.json")
    dev_overflow = _acquire(
        dev,
        component="dft",
        environment="dev",
        kind="execution",
        placement="overflow",
    )
    assert dev_overflow.gpu_index == 3
    assert dev_overflow.gpu_index != 2


def test_dft_resident_execution_is_fenced_to_parent_without_double_counting(
    tmp_path: Path,
) -> None:
    broker = HostGpuBroker(tmp_path / "state.json")
    residency = _acquire(
        broker,
        component="dft",
        environment="prod",
        kind="residency",
        placement="preferred",
    )
    execution = _acquire(
        broker,
        component="dft",
        environment="prod",
        kind="execution",
        placement="preferred",
        parent_lease_id=residency.lease_id,
    )

    assert execution.gpu_index == residency.gpu_index == 2
    assert execution.parent_lease_id == residency.lease_id
    assert broker.status()["usage_mib"]["2"] == 4096


def test_fatal_quarantine_is_fenced_persistent_and_blocks_new_work(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    broker = HostGpuBroker(state_path)
    residency = _acquire(
        broker,
        component="backend",
        environment="dev",
        kind="residency",
    )
    with pytest.raises(BrokerError) as invalid:
        broker.quarantine(
            residency.lease_id,
            residency.fencing_token,
            owner=_owner(),
            reason="arbitrary_reason",
        )
    assert invalid.value.code == "invalid_request"
    with pytest.raises(BrokerError) as stale:
        broker.quarantine(
            residency.lease_id,
            residency.fencing_token + 1,
            owner=_owner(),
            reason="gpu_xid",
        )
    assert stale.value.code == "stale_fencing_token"

    quarantined = broker.quarantine(
        residency.lease_id,
        residency.fencing_token,
        owner=_owner(),
        reason="gpu_xid",
    )
    assert quarantined["gpu_uuid"] == residency.gpu_uuid
    broker.release(residency.lease_id, residency.fencing_token, owner=_owner())

    restarted = HostGpuBroker(state_path)
    assert restarted.status()["quarantined_gpus"][residency.gpu_uuid]["reason"] == "gpu_xid"
    with pytest.raises(BrokerError) as unavailable:
        _acquire(
            restarted,
            component="backend",
            environment="dev",
            kind="residency",
        )
    assert unavailable.value.code == "gpu_capacity_unavailable"


def test_missing_mps_pipe_makes_gpu_ineligible(tmp_path: Path) -> None:
    mps_guard = MpsRuntimeGuard(tmp_path / "mps-state")
    broker = HostGpuBroker(
        tmp_path / "state.json",
        gpu_runtime_healthy=mps_guard,
    )
    with pytest.raises(BrokerError) as error:
        _acquire(
            broker,
            component="backend",
            environment="dev",
            kind="residency",
        )
    assert error.value.code == "gpu_capacity_unavailable"


def test_mps_client_environment_uses_uuid_cap_priority_and_private_pipe(
    tmp_path: Path,
) -> None:
    pipe_directory = tmp_path / "mps-1" / "pipe"
    pipe_directory.mkdir(parents=True)
    os.mkfifo(pipe_directory / "control", 0o600)
    lease = GpuLease(
        lease_id="lease-1",
        fencing_token=1,
        broker_instance_id="broker-1",
        kind="execution",
        placement="any",
        component="md",
        environment="dev",
        client_id="md-dev",
        gpu_index=1,
        gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        memory_mib=8192,
        thread_percent=50,
        preferred=True,
        parent_lease_id=None,
        status="active",
    )

    assert mps_client_environment(lease, pipe_root=tmp_path) == {
        "CUDA_VISIBLE_DEVICES": lease.gpu_uuid,
        "CUDA_MPS_PIPE_DIRECTORY": str(pipe_directory),
        "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": "50",
        "CUDA_MPS_CLIENT_PRIORITY": "1",
        "CUDA_MPS_PINNED_DEVICE_MEM_LIMIT": f"{lease.gpu_uuid}=8192M",
    }


def test_residency_is_single_owner_and_idempotent_for_same_process(tmp_path: Path) -> None:
    broker = HostGpuBroker(tmp_path / "state.json")
    first = _acquire(broker, component="backend", environment="prod", kind="residency")
    repeated = _acquire(broker, component="backend", environment="prod", kind="residency")
    assert repeated.lease_id == first.lease_id
    assert len(broker.status()["leases"]) == 1

    with pytest.raises(BrokerError) as error:
        broker.acquire(
            kind="residency",
            placement="preferred",
            component="backend",
            environment="prod",
            client_id="other-backend",
            owner=_owner(),
            memory_mib=8192,
            thread_percent=100,
            wait_timeout_seconds=0,
        )
    assert error.value.code == "gpu_capacity_unavailable"


def test_prod_waiter_has_priority_over_earlier_dev_waiter(tmp_path: Path) -> None:
    allowed_indices: set[int] = set()
    broker = HostGpuBroker(
        tmp_path / "state.json",
        gpu_externally_busy=lambda index, *_args: index not in allowed_indices,
        mps_clients_alive=lambda _lease: False,
    )
    results: dict[str, object] = {}

    def acquire(name: str, environment: str) -> None:
        try:
            results[name] = _acquire(
                broker,
                component="md",
                environment=environment,
                kind="execution",
                wait=5,
            )
        except Exception as exc:  # pragma: no cover - assertion reports it
            results[name] = exc

    dev_thread = threading.Thread(target=acquire, args=("dev", "dev"))
    prod_thread = threading.Thread(target=acquire, args=("prod", "prod"))
    dev_thread.start()
    while broker.status()["waiters"] != 1:
        time.sleep(0.01)
    prod_thread.start()
    while broker.status()["waiters"] != 2:
        time.sleep(0.01)

    allowed_indices.add(1)
    broker.set_draining(False)
    prod_thread.join(timeout=2)
    assert not prod_thread.is_alive()
    assert getattr(results["prod"], "gpu_index") == 1
    assert dev_thread.is_alive()

    prod_lease = results["prod"]
    broker.release(prod_lease.lease_id, prod_lease.fencing_token, owner=_owner())
    dev_thread.join(timeout=2)
    assert not dev_thread.is_alive()
    assert getattr(results["dev"], "gpu_index") == 1


def test_persistence_preserves_leases_and_monotonic_fencing(tmp_path: Path) -> None:
    state_path = tmp_path / "private" / "state.json"
    first = HostGpuBroker(state_path)
    lease_one = _acquire(first, component="backend", environment="prod", kind="residency")

    second = HostGpuBroker(state_path)
    assert second.status()["leases"][0]["lease_id"] == lease_one.lease_id
    assert second.status()["leases"][0]["status"] == "suspect"
    second.heartbeat(lease_one.lease_id, lease_one.fencing_token, owner=_owner())
    lease_two = _acquire(second, component="dft", environment="prod", kind="residency")
    assert lease_two.fencing_token > lease_one.fencing_token
    assert state_path.stat().st_mode & 0o777 == 0o600
    assert state_path.parent.stat().st_mode & 0o777 == 0o700


def test_expired_live_owner_is_suspect_and_dead_owner_is_reclaimed(tmp_path: Path) -> None:
    now = [100.0]
    alive = [True]
    broker = HostGpuBroker(
        tmp_path / "state.json",
        heartbeat_timeout_seconds=10,
        now=lambda: now[0],
        process_alive=lambda _owner: alive[0],
    )
    lease = _acquire(broker, component="md", environment="dev", kind="execution")
    broker.activate(lease.lease_id, lease.fencing_token, owner=_owner())
    _bind_test_workload(lease)

    now[0] = 111
    assert broker.status()["leases"][0]["status"] == "suspect"
    assert broker.status()["usage_mib"]["1"] == 8192

    alive[0] = False
    assert broker.status()["leases"] == []
    assert broker.status()["usage_mib"]["1"] == 0


def test_stale_fencing_token_cannot_mutate_lease(tmp_path: Path) -> None:
    broker = HostGpuBroker(tmp_path / "state.json")
    lease = _acquire(broker, component="md", environment="dev", kind="execution")

    with pytest.raises(BrokerError) as error:
        broker.release(lease.lease_id, lease.fencing_token + 1, owner=_owner())
    assert error.value.code == "stale_fencing_token"
    assert len(broker.status()["leases"]) == 1


def test_uuid_mapping_fails_closed_on_index_drift() -> None:
    with pytest.raises(BrokerError) as error:
        validate_gpu_inventory(
            {
                1: "GPU-wrong",
                2: "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe",
                3: "GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5",
            }
        )
    assert error.value.code == "gpu_uuid_mismatch"


def test_external_reservation_inventory_blocks_gpu3(tmp_path: Path) -> None:
    if os.getuid() != 1001 or os.getgid() != 1001:
        pytest.skip("runtime inventory deliberately requires owner 1001:1001")
    inventory = tmp_path / "external.json"
    inventory.write_text(
        '{"schema_version":1,"blocked_gpu_uuids":'
        '{"GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5":"docker claim"},'
        '"managed_docker_claims":{},"managed_systemd_claims":{}}\n',
        encoding="utf-8",
    )
    inventory.chmod(0o600)
    assert load_external_reservations(inventory).blocked_gpu_uuids == {
        "GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5"
    }


def test_execution_acquire_is_idempotent_for_exact_job_identity(tmp_path: Path) -> None:
    broker = HostGpuBroker(tmp_path / "state.json")
    first = _acquire(broker, component="md", environment="dev", kind="execution")
    repeated = _acquire(broker, component="md", environment="dev", kind="execution")

    assert repeated.lease_id == first.lease_id
    assert repeated.fencing_token == first.fencing_token
    assert len(broker.status()["leases"]) == 1


def test_termination_evidence_is_fresh_for_every_cleanup_attempt(
    tmp_path: Path,
) -> None:
    callbacks: list[str] = []

    def terminate(lease):
        callbacks.append(lease.lease_id)
        return (12_345,)

    broker = HostGpuBroker(
        tmp_path / "state.json",
        terminate_mps_clients=terminate,
        freeze_workload=lambda lease: f"freeze-{len(callbacks)}-{lease.lease_id}",
        kill_workload=lambda _lease: None,
        workload_empty=lambda _lease: True,
    )
    lease = _acquire(broker, component="md", environment="dev", kind="execution")
    broker.activate(lease.lease_id, lease.fencing_token, owner=_owner())
    _bind_test_workload(lease)

    first = broker.prepare_process_termination(
        lease.lease_id,
        lease.fencing_token,
        owner=_owner(),
    )
    repeated = broker.prepare_process_termination(
        lease.lease_id,
        lease.fencing_token,
        owner=_owner(),
    )

    assert first["freeze_token"] != repeated["freeze_token"]
    assert first["safe_to_signal"] is True
    assert first["client_pids"] == [12_345]
    assert callbacks == [lease.lease_id, lease.lease_id]
    assert broker.status()["leases"][0]["status"] == "terminating"
    with pytest.raises(BrokerError) as error:
        broker.activate(lease.lease_id, lease.fencing_token, owner=_owner())
    assert error.value.code == "invalid_lease_state"


def test_mps_termination_failure_quarantines_and_retains_suspect_lease(
    tmp_path: Path,
) -> None:
    def terminate(_lease):
        raise BrokerError("mps_termination_failed", "CUDA_ERROR_UNKNOWN")

    broker = HostGpuBroker(
        tmp_path / "state.json",
        terminate_mps_clients=terminate,
        freeze_workload=lambda lease: f"freeze-{lease.lease_id}",
        kill_workload=lambda _lease: None,
        workload_empty=lambda _lease: True,
    )
    lease = _acquire(broker, component="md", environment="dev", kind="execution")
    broker.activate(lease.lease_id, lease.fencing_token, owner=_owner())
    _bind_test_workload(lease)

    with pytest.raises(BrokerError) as error:
        broker.prepare_process_termination(
            lease.lease_id,
            lease.fencing_token,
            owner=_owner(),
        )
    assert error.value.code == "gpu_runtime_unhealthy"
    status = broker.status()
    assert status["leases"][0]["status"] == "suspect"
    assert status["leases"][0]["mps_termination_status"] == "failed"
    assert status["usage_mib"]["1"] == 8192
    assert status["quarantined_gpus"][lease.gpu_uuid]["reason"] == (
        "gpu_runtime_corruption"
    )
    with pytest.raises(BrokerError) as unavailable:
        _acquire(broker, component="backend", environment="dev", kind="residency")
    assert unavailable.value.code == "gpu_capacity_unavailable"


def test_mps_guard_uses_host_ps_pid_and_waits_for_cuda_success(tmp_path: Path) -> None:
    pipe_directory = tmp_path / "mps-1" / "pipe"
    pipe_directory.mkdir(parents=True)
    os.mkfifo(pipe_directory / "control", 0o600)
    commands: list[str] = []
    partial_uuid = "GPU-0e19c809-f81d"

    def run(command, **kwargs):
        control_command = kwargs["input"].strip()
        commands.append(control_command)
        stdout = (
            "PID ID SERVER DEVICE NAMESPACE COMMAND\n"
            f"{os.getpid()} 0 6472 {partial_uuid} 4026531836 ./client\n"
            if control_command == "ps"
            else "0\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    guard = MpsRuntimeGuard(tmp_path, run=run)
    broker = HostGpuBroker(tmp_path / "state.json")
    lease = _acquire(broker, component="md", environment="dev", kind="execution")
    _bind_test_workload(lease)

    assert guard.terminate_lease_clients(lease) == (os.getpid(),)
    assert commands == ["ps", f"terminate_client 6472 {os.getpid()}"]


def test_mps_guard_rejects_non_successful_termination(tmp_path: Path) -> None:
    pipe_directory = tmp_path / "mps-1" / "pipe"
    pipe_directory.mkdir(parents=True)
    os.mkfifo(pipe_directory / "control", 0o600)

    def run(command, **kwargs):
        control_command = kwargs["input"].strip()
        stdout = (
            "PID ID SERVER DEVICE NAMESPACE COMMAND\n"
            f"{os.getpid()} 0 6472 GPU-0e19c809-f81d 4026531836 ./client\n"
            if control_command == "ps"
            else "CUDA_ERROR_UNKNOWN\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    guard = MpsRuntimeGuard(tmp_path, run=run)
    broker = HostGpuBroker(tmp_path / "state.json")
    lease = _acquire(broker, component="md", environment="dev", kind="execution")
    _bind_test_workload(lease)

    with pytest.raises(BrokerError) as error:
        guard.terminate_lease_clients(lease)
    assert error.value.code == "mps_termination_failed"


def test_register_workload_fences_live_descendant_pid_start_group_and_cgroup(
    tmp_path: Path,
) -> None:
    broker = HostGpuBroker(
        tmp_path / "state.json",
        resolve_workload_identity=resolve_workload_identity,
        mps_clients_alive=lambda _lease: False,
        workload_empty=lambda _lease: True,
        cleanup_workload=lambda _lease: None,
    )
    lease = _acquire(broker, component="md", environment="dev", kind="execution")
    broker.activate(lease.lease_id, lease.fencing_token, owner=_owner())
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        start_ticks = read_process_start_ticks(child.pid)
        registered = broker.register_workload(
            lease.lease_id,
            lease.fencing_token,
            owner=_owner(),
            workload_pid=child.pid,
            workload_process_start_ticks=start_ticks,
            workload_process_group_id=child.pid,
        )

        assert registered.workload_pid == child.pid
        assert registered.workload_process_start_ticks == start_ticks
        assert registered.workload_process_group_id == child.pid
        assert registered.workload_cgroup
        with pytest.raises(BrokerError) as reused:
            broker.register_workload(
                lease.lease_id,
                lease.fencing_token,
                owner=_owner(),
                workload_pid=child.pid,
                workload_process_start_ticks=start_ticks + 1,
                workload_process_group_id=child.pid,
            )
        assert reused.value.code == "workload_identity_unavailable"
    finally:
        child.terminate()
        child.wait(timeout=5)
    broker.release(lease.lease_id, lease.fencing_token, owner=_owner())


def test_dft_residency_defers_to_executor_and_parented_execution_is_logical(
    tmp_path: Path,
) -> None:
    parent_release_checks: list[tuple[str, str]] = []

    def resolve(_lease, pid, start_ticks, process_group_id):
        return pid, start_ticks, process_group_id, "0::/nexpoly-gpu-jobs/executor"

    def parent_safe(child, parent, live_leases):
        parent_release_checks.append((child.lease_id, parent.lease_id))
        assert child in live_leases
        assert parent in live_leases
        return True

    broker = HostGpuBroker(
        tmp_path / "state.json",
        resolve_workload_identity=resolve,
        parented_execution_safe_to_release=parent_safe,
        mps_clients_alive=lambda _lease: False,
        workload_empty=lambda _lease: True,
        cleanup_workload=lambda _lease: None,
    )
    residency = _acquire(
        broker,
        component="dft",
        environment="dev",
        kind="residency",
        client_id="dft-dev-worker",
    )
    residency = broker.activate(
        residency.lease_id, residency.fencing_token, owner=_owner()
    )
    assert residency.workload_pid is None

    start_ticks = read_process_start_ticks(os.getpid())
    residency = broker.register_workload(
        residency.lease_id,
        residency.fencing_token,
        owner=_owner(),
        workload_pid=os.getpid(),
        workload_process_start_ticks=start_ticks,
        workload_process_group_id=os.getpid(),
    )
    execution = _acquire(
        broker,
        component="dft",
        environment="dev",
        kind="execution",
        placement="preferred",
        parent_lease_id=residency.lease_id,
        client_id="dft-dev-worker",
    )
    execution = broker.activate(
        execution.lease_id, execution.fencing_token, owner=_owner()
    )
    assert execution.workload_pid == residency.workload_pid
    assert execution.workload_cgroup == residency.workload_cgroup
    with pytest.raises(BrokerError) as rebound:
        broker.register_workload(
            execution.lease_id,
            execution.fencing_token,
            owner=_owner(),
            workload_pid=os.getpid(),
            workload_process_start_ticks=start_ticks,
            workload_process_group_id=os.getpid(),
        )
    assert rebound.value.code == "invalid_lease_state"

    broker.release(execution.lease_id, execution.fencing_token, owner=_owner())
    assert parent_release_checks == [(execution.lease_id, residency.lease_id)]


def test_parented_release_allows_governed_shared_mps_clients_and_rejects_alien(
    tmp_path: Path,
    monkeypatch,
) -> None:
    guard = MpsRuntimeGuard(tmp_path)
    monkeypatch.setattr(MpsRuntimeGuard, "__call__", lambda *_args: True)
    client_cgroups = {
        101_001: "0::/nexpoly/backend",
        101_002: "0::/nexpoly/dft",
        101_003: "0::/nexpoly/md",
        101_999: "0::/unmanaged",
    }
    monkeypatch.setattr(
        "ops.gpu_broker.server._read_cgroup",
        lambda pid: client_cgroups[pid],
    )
    clients = [
        MpsClient(101_001, 1, 9001, EXPECTED_GPU_UUIDS[2], 1, "backend"),
        MpsClient(101_002, 2, 9001, EXPECTED_GPU_UUIDS[2], 1, "dft"),
        MpsClient(101_003, 3, 9001, EXPECTED_GPU_UUIDS[2], 1, "md"),
    ]
    monkeypatch.setattr(guard, "_query_clients", lambda _index: tuple(clients))
    broker = HostGpuBroker(
        tmp_path / "state.json",
        parented_execution_safe_to_release=(
            guard.parented_execution_safe_to_release
        ),
    )
    backend = _acquire(
        broker, component="backend", environment="prod", kind="residency"
    )
    dft = _acquire(
        broker,
        component="dft",
        environment="prod",
        kind="residency",
        client_id="dft-prod-worker",
    )
    md = _acquire(broker, component="md", environment="prod", kind="execution")
    for lease, pid, cgroup in (
        (backend, 201_001, "0::/nexpoly/backend"),
        (dft, 201_002, "0::/nexpoly/dft"),
        (md, 201_003, "0::/nexpoly/md"),
    ):
        broker.activate(lease.lease_id, lease.fencing_token, owner=_owner())
        lease.workload_pid = pid
        lease.workload_process_start_ticks = pid + 1
        lease.workload_process_group_id = pid
        lease.workload_cgroup = cgroup
    execution = _acquire(
        broker,
        component="dft",
        environment="prod",
        kind="execution",
        placement="preferred",
        parent_lease_id=dft.lease_id,
        client_id="dft-prod-worker",
    )
    broker.activate(execution.lease_id, execution.fencing_token, owner=_owner())
    live = tuple(broker._leases.values())

    assert guard.parented_execution_safe_to_release(execution, dft, live) is True
    clients.append(
        MpsClient(101_999, 9, 9001, EXPECTED_GPU_UUIDS[2], 1, "alien")
    )
    assert guard.parented_execution_safe_to_release(execution, dft, live) is False
    clients.pop()

    broker.release(execution.lease_id, execution.fencing_token, owner=_owner())
    assert execution.lease_id not in {
        item["lease_id"] for item in broker.status()["leases"]
    }


def test_parented_release_with_unmanaged_mps_client_fails_closed(tmp_path: Path) -> None:
    broker = HostGpuBroker(
        tmp_path / "state.json",
        parented_execution_safe_to_release=lambda _child, _parent, _leases: False,
    )
    residency = _acquire(
        broker,
        component="dft",
        environment="dev",
        kind="residency",
        client_id="dft-dev-worker",
    )
    broker.activate(residency.lease_id, residency.fencing_token, owner=_owner())
    _bind_test_workload(residency)
    execution = _acquire(
        broker,
        component="dft",
        environment="dev",
        kind="execution",
        placement="preferred",
        parent_lease_id=residency.lease_id,
        client_id="dft-dev-worker",
    )
    broker.activate(execution.lease_id, execution.fencing_token, owner=_owner())

    with pytest.raises(BrokerError) as error:
        broker.release(execution.lease_id, execution.fencing_token, owner=_owner())

    assert error.value.code == "gpu_runtime_unhealthy"
    status = broker.status()
    assert any(item["lease_id"] == execution.lease_id for item in status["leases"])
    assert execution.gpu_uuid in status["quarantined_gpus"]


def test_execution_termination_fails_closed_without_cgroup_authority(
    tmp_path: Path,
) -> None:
    broker = HostGpuBroker(
        tmp_path / "state.json",
        terminate_mps_clients=lambda _lease: (),
    )
    lease = _acquire(broker, component="md", environment="dev", kind="execution")
    broker.activate(lease.lease_id, lease.fencing_token, owner=_owner())
    _bind_test_workload(lease)

    with pytest.raises(BrokerError) as error:
        broker.prepare_process_termination(
            lease.lease_id, lease.fencing_token, owner=_owner()
        )

    assert error.value.code == "gpu_runtime_unhealthy"
    assert broker.status()["leases"][0]["status"] == "suspect"


def test_stable_waiter_is_persisted_and_explicitly_cancelled(tmp_path: Path) -> None:
    broker = HostGpuBroker(
        tmp_path / "state.json",
        gpu_externally_busy=lambda index, *_args: index == 3,
    )
    _acquire(broker, component="backend", environment="dev", kind="residency")
    _acquire(broker, component="dft", environment="dev", kind="residency")
    _acquire(broker, component="md", environment="dev", kind="execution")
    errors: list[str] = []

    def wait_for_capacity() -> None:
        try:
            broker.acquire(
                request_id="md:dev:queued-job",
                kind="execution",
                placement="any",
                component="md",
                environment="dev",
                client_id="md-dev-queued-job",
                owner=_owner(),
                memory_mib=8192,
                thread_percent=50,
                wait_timeout_seconds=5,
            )
        except BrokerError as exc:
            errors.append(exc.code)

    thread = threading.Thread(target=wait_for_capacity)
    thread.start()
    deadline = time.monotonic() + 2
    while broker.status()["waiters"] != 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    payload = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert payload["waiters"][0]["request_id"] == "md:dev:queued-job"
    restarted_state = tmp_path / "restarted-state.json"
    restarted_state.write_text(json.dumps(payload), encoding="utf-8")
    restarted_state.chmod(0o600)
    restarted = HostGpuBroker(
        restarted_state,
        gpu_externally_busy=lambda index, *_args: index == 3,
    )
    assert restarted.status()["waiters"] == 1
    assert restarted.cancel_acquire("md:dev:queued-job", owner=_owner()) is True
    assert broker.cancel_acquire("md:dev:queued-job", owner=_owner()) is True
    thread.join(timeout=2)
    assert errors == ["acquire_cancelled"]


def test_job_cgroup_controller_refuses_missing_host_delegation(tmp_path: Path) -> None:
    with pytest.raises(BrokerError) as error:
        JobCgroupController(tmp_path / "missing")
    assert error.value.code == "workload_control_unavailable"


def test_job_cgroup_controller_requires_freeze_kill_and_event_controls(
    tmp_path: Path,
) -> None:
    root = tmp_path / "delegated"
    root.mkdir(mode=0o700)
    for name in ("cgroup.controllers", "cgroup.procs", "cgroup.subtree_control"):
        (root / name).write_text("", encoding="ascii")

    with pytest.raises(BrokerError) as error:
        JobCgroupController(root)

    assert error.value.code == "workload_control_unavailable"


def test_release_retains_and_quarantines_lease_when_mps_client_survives(
    tmp_path: Path,
) -> None:
    broker = HostGpuBroker(
        tmp_path / "state.json",
        mps_clients_alive=lambda _lease: True,
    )
    lease = _acquire(broker, component="md", environment="dev", kind="execution")
    broker.activate(lease.lease_id, lease.fencing_token, owner=_owner())

    with pytest.raises(BrokerError) as error:
        broker.release(lease.lease_id, lease.fencing_token, owner=_owner())

    assert error.value.code == "gpu_runtime_unhealthy"
    status = broker.status()
    assert status["leases"][0]["status"] == "suspect"
    assert status["usage_mib"]["1"] == 8192
    assert status["quarantined_gpus"][lease.gpu_uuid]["reason"] == (
        "gpu_runtime_corruption"
    )


def test_reparented_mps_client_is_owned_by_registered_process_group(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pipe_directory = tmp_path / "mps-1" / "pipe"
    pipe_directory.mkdir(parents=True)
    os.mkfifo(pipe_directory / "control", 0o600)
    orphan_pid = 987_654

    def run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "PID ID SERVER DEVICE NAMESPACE COMMAND\n"
                f"{orphan_pid} 0 6472 GPU-0e19c809-f81d 4026531836 ./orphan\n"
            ),
            stderr="",
        )

    guard = MpsRuntimeGuard(tmp_path, run=run)
    broker = HostGpuBroker(tmp_path / "state.json")
    lease = _acquire(broker, component="md", environment="dev", kind="execution")
    lease.workload_pid = 123_456
    lease.workload_process_start_ticks = 111
    lease.workload_process_group_id = 777
    lease.workload_cgroup = "0::/nexpoly-md"
    monkeypatch.setattr("ops.gpu_broker.server.os.getpgid", lambda _pid: 777)
    monkeypatch.setattr(
        "ops.gpu_broker.server._read_cgroup",
        lambda _pid: "0::/nexpoly-md",
    )

    assert guard.lease_client_alive(lease) is True


def test_registered_workload_rejects_descendant_or_pgid_outside_its_cgroup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pipe_directory = tmp_path / "mps-1" / "pipe"
    pipe_directory.mkdir(parents=True)
    os.mkfifo(pipe_directory / "control", 0o600)
    escaped_pid = 987_654

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "PID ID SERVER DEVICE NAMESPACE COMMAND\n"
                f"{escaped_pid} 0 6472 GPU-0e19c809-f81d 4026531836 ./escaped\n"
            ),
            stderr="",
        )

    guard = MpsRuntimeGuard(tmp_path, run=run)
    broker = HostGpuBroker(tmp_path / "state.json")
    lease = _acquire(broker, component="md", environment="dev", kind="execution")
    lease.workload_pid = 123_456
    lease.workload_process_start_ticks = 111
    lease.workload_process_group_id = 777
    lease.workload_cgroup = "0::/nexpoly-md"
    monkeypatch.setattr(
        "ops.gpu_broker.server._pid_is_or_descends_from",
        lambda _pid, _owner: True,
    )
    monkeypatch.setattr("ops.gpu_broker.server.os.getpgid", lambda _pid: 777)
    monkeypatch.setattr(
        "ops.gpu_broker.server._read_cgroup",
        lambda _pid: "0::/escaped",
    )

    assert guard.lease_client_alive(lease) is False


def test_peer_mps_client_does_not_keep_expired_lease_alive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    guard = MpsRuntimeGuard(tmp_path)
    monkeypatch.setattr(MpsRuntimeGuard, "__call__", lambda *_args: True)
    peer_pid = 987_654
    monkeypatch.setattr(
        guard,
        "_query_clients",
        lambda _index: (
            MpsClient(peer_pid, 1, 9001, EXPECTED_GPU_UUIDS[1], 1, "peer"),
        ),
    )
    monkeypatch.setattr(
        "ops.gpu_broker.server._read_cgroup",
        lambda _pid: "0::/nexpoly/dft",
    )
    broker = HostGpuBroker(tmp_path / "state.json")
    expired = _acquire(
        broker, component="backend", environment="dev", kind="residency"
    )
    expired.workload_pid = 123_456
    expired.workload_process_start_ticks = 111
    expired.workload_process_group_id = 123_456
    expired.workload_cgroup = "0::/nexpoly/backend"

    assert guard.orphan_client_alive(expired) is False

    monkeypatch.setattr(
        "ops.gpu_broker.server._read_cgroup",
        lambda _pid: "0::/nexpoly/backend/child",
    )
    assert guard.orphan_client_alive(expired) is True


def test_root_cgroup_alone_cannot_claim_an_unrelated_mps_client(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pipe_directory = tmp_path / "mps-1" / "pipe"
    pipe_directory.mkdir(parents=True)
    os.mkfifo(pipe_directory / "control", 0o600)
    unrelated_pid = 987_654

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "PID ID SERVER DEVICE NAMESPACE COMMAND\n"
                f"{unrelated_pid} 0 6472 GPU-0e19c809-f81d 4026531836 ./client\n"
            ),
            stderr="",
        )

    guard = MpsRuntimeGuard(tmp_path, run=run)
    broker = HostGpuBroker(tmp_path / "state.json")
    lease = _acquire(broker, component="backend", environment="dev", kind="residency")
    lease.workload_pid = 123_456
    lease.workload_process_start_ticks = 111
    lease.workload_process_group_id = 123_456
    lease.workload_cgroup = "0::/"
    monkeypatch.setattr(
        "ops.gpu_broker.server._pid_is_or_descends_from",
        lambda _pid, _owner: False,
    )
    monkeypatch.setattr("ops.gpu_broker.server.os.getpgid", lambda _pid: 999_999)
    monkeypatch.setattr("ops.gpu_broker.server._read_cgroup", lambda _pid: "0::/")

    assert guard.unmanaged_client_alive(1, lease.gpu_uuid, (lease,)) is True


def test_external_guard_allows_owned_pid_and_blocks_unknown_cuda_pid(tmp_path: Path) -> None:
    broker = HostGpuBroker(tmp_path / "state.json")
    lease = _acquire(broker, component="backend", environment="dev", kind="residency")
    policy = ExternalReservationPolicy(frozenset(), {}, {})
    owned = ExternalGpuGuard(
        policy,
        process_query=lambda: {lease.gpu_uuid: frozenset({os.getpid()})},
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: (),
        cache_seconds=0,
    )
    unknown = ExternalGpuGuard(
        policy,
        process_query=lambda: {lease.gpu_uuid: frozenset({999_999})},
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: (),
        cache_seconds=0,
    )

    assert owned(lease.gpu_index, lease.gpu_uuid, (lease,), _owner(), "backend", "dev") is False
    assert unknown(lease.gpu_index, lease.gpu_uuid, (lease,), _owner(), "backend", "dev") is True


def test_external_guard_blocks_unowned_or_unqueryable_mps_client(tmp_path: Path) -> None:
    broker = HostGpuBroker(tmp_path / "state.json")
    lease = _acquire(broker, component="backend", environment="dev", kind="residency")
    policy = ExternalReservationPolicy(frozenset(), {}, {})
    seen: list[tuple[int, str, tuple[Lease, ...]]] = []

    def unowned(index: int, uuid: str, leases: tuple[Lease, ...]) -> bool:
        seen.append((index, uuid, leases))
        return True

    blocked = ExternalGpuGuard(
        policy,
        process_query=lambda: {},
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: (),
        unmanaged_mps_client_query=unowned,
        cache_seconds=0,
    )
    unavailable = ExternalGpuGuard(
        policy,
        process_query=lambda: {},
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: (),
        unmanaged_mps_client_query=lambda *_args: (_ for _ in ()).throw(
            BrokerError("mps_control_unavailable", "offline")
        ),
        cache_seconds=0,
    )

    assert blocked(1, lease.gpu_uuid, (lease,), _owner(), "backend", "dev") is True
    assert seen == [(1, lease.gpu_uuid, (lease,))]
    assert unavailable(1, lease.gpu_uuid, (lease,), _owner(), "backend", "dev") is True


def test_mps_allocation_audit_rejects_unowned_and_wrong_device_clients(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pipe_directory = tmp_path / "mps-1" / "pipe"
    pipe_directory.mkdir(parents=True)
    os.mkfifo(pipe_directory / "control", 0o600)
    reported_pid = 987_654
    reported_device = ["GPU-0e19c809-f81d"]

    def run(command, **kwargs):
        assert kwargs["input"].strip() == "ps"
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "PID ID SERVER DEVICE NAMESPACE COMMAND\n"
                f"{reported_pid} 0 6472 {reported_device[0]} 4026531836 ./client\n"
            ),
            stderr="",
        )

    guard = MpsRuntimeGuard(tmp_path, run=run)
    broker = HostGpuBroker(tmp_path / "state.json")
    lease = _acquire(broker, component="backend", environment="dev", kind="residency")

    monkeypatch.setattr(
        "ops.gpu_broker.server._pid_is_or_descends_from",
        lambda _pid, _owner: False,
    )
    monkeypatch.setattr("ops.gpu_broker.server.os.getpgid", lambda _pid: 999_999)
    monkeypatch.setattr(
        "ops.gpu_broker.server._read_cgroup",
        lambda _pid: "0::/external",
    )
    assert guard.unmanaged_client_alive(1, lease.gpu_uuid, (lease,)) is True

    reported_device[0] = "GPU-89c7c52c-e252"
    with pytest.raises(BrokerError) as error:
        guard.unmanaged_client_alive(1, lease.gpu_uuid, (lease,))
    assert error.value.code == "mps_control_unavailable"


def test_docker_device_claim_without_cuda_pid_still_blocks_gpu1() -> None:
    gpu1 = "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771"
    claim = DockerGpuClaim(
        container_id="a" * 64,
        init_pid=os.getpid(),
        registration_id=None,
        component=None,
        environment=None,
        compose_project="nexpoly_dev",
        compose_service="backend",
        gpu_uuids=frozenset({gpu1}),
    )
    guard = ExternalGpuGuard(
        ExternalReservationPolicy(frozenset(), {}, {}),
        process_query=lambda: {},
        docker_claim_query=lambda: (claim,),
        systemd_claim_query=lambda: (),
        cache_seconds=0,
    )

    assert guard(1, gpu1, (), _owner(), "backend", "dev") is True


def test_docker_inspect_device_request_is_authoritative_over_image_all_env() -> None:
    container_id = "c" * 64
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        if command[2] == "ls":
            return subprocess.CompletedProcess(
                command, 0, stdout=container_id + "\n", stderr=""
            )
        payload = [
            {
                "Id": container_id,
                "State": {"Running": True, "Pid": os.getpid()},
                "Config": {
                    "Labels": {
                        "com.nexpoly.gpu.registration": "backend-dev",
                        "com.nexpoly.gpu.component": "backend",
                        "com.nexpoly.gpu.environment": "dev",
                        "com.docker.compose.project": "nexpoly_dev",
                        "com.docker.compose.service": "backend",
                    },
                    "Env": ["NVIDIA_VISIBLE_DEVICES=all"],
                },
                "HostConfig": {
                    "DeviceRequests": [
                        {
                            "Driver": "nvidia",
                            "DeviceIDs": ["1"],
                            "Capabilities": [["gpu"]],
                            "Count": 0,
                        }
                    ]
                },
            }
        ]
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload), stderr=""
        )

    claims = query_docker_gpu_claims(run=run)

    assert len(claims) == 1
    assert claims[0].gpu_uuids == {
        "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771"
    }
    assert claims[0].registration_id == "backend-dev"
    assert len(calls) == 2


def test_exact_managed_docker_registration_allows_visibility_claim() -> None:
    gpu1 = "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771"
    registration = ManagedDockerClaim(
        component="backend",
        environment="dev",
        compose_project="nexpoly_dev",
        compose_service="backend",
        gpu_uuids=frozenset({gpu1}),
    )
    claim = DockerGpuClaim(
        container_id="b" * 64,
        init_pid=os.getpid(),
        registration_id="backend-dev",
        component="backend",
        environment="dev",
        compose_project="nexpoly_dev",
        compose_service="backend",
        gpu_uuids=frozenset({gpu1}),
    )
    guard = ExternalGpuGuard(
        ExternalReservationPolicy(
            frozenset(),
            {"backend-dev": registration},
            {},
        ),
        process_query=lambda: {},
        docker_claim_query=lambda: (claim,),
        systemd_claim_query=lambda: (),
        cache_seconds=0,
    )

    assert guard(1, gpu1, (), _owner(), "backend", "dev") is False

    mismatched = replace(claim, compose_project="spoofed-project")
    mismatched_guard = ExternalGpuGuard(
        guard.policy,
        process_query=lambda: {},
        docker_claim_query=lambda: (mismatched,),
        systemd_claim_query=lambda: (),
        cache_seconds=0,
    )
    assert mismatched_guard(1, gpu1, (), _owner(), "backend", "dev") is True

    unbound = replace(claim, init_pid=999_999_999)
    unbound_guard = ExternalGpuGuard(
        guard.policy,
        process_query=lambda: {},
        docker_claim_query=lambda: (unbound,),
        systemd_claim_query=lambda: (),
        cache_seconds=0,
    )
    assert unbound_guard(1, gpu1, (), _owner(), "backend", "dev") is True


def test_exact_idle_md_supervisor_claim_does_not_block_other_governed_component() -> None:
    gpu1 = EXPECTED_GPU_UUIDS[1]
    registration = ManagedDockerClaim(
        component="md",
        environment="dev",
        compose_project="nexpoly-dev-monomer-md-worker",
        compose_service="monomer-md-worker",
        gpu_uuids=frozenset({gpu1, EXPECTED_GPU_UUIDS[3]}),
    )
    claim = DockerGpuClaim(
        container_id="d" * 64,
        init_pid=999_999_999,
        registration_id="md-dev",
        component="md",
        environment="dev",
        compose_project="nexpoly-dev-monomer-md-worker",
        compose_service="monomer-md-worker",
        gpu_uuids=frozenset({gpu1, EXPECTED_GPU_UUIDS[3]}),
    )
    policy = ExternalReservationPolicy(
        frozenset(),
        {"md-dev": registration},
        {},
    )
    idle_guard = ExternalGpuGuard(
        policy,
        process_query=lambda: {},
        docker_claim_query=lambda: (claim,),
        systemd_claim_query=lambda: (),
        unmanaged_mps_client_query=lambda *_args: False,
        cache_seconds=0,
    )

    assert idle_guard(1, gpu1, (), _owner(), "backend", "dev") is False
    assert idle_guard(1, gpu1, (), _owner(), "dft", "dev") is False

    # An exact declaration is not execution authority. Any CUDA process or
    # MPS client without a live lease still blocks admission.
    cuda_guard = ExternalGpuGuard(
        policy,
        process_query=lambda: {gpu1: frozenset({claim.init_pid})},
        docker_claim_query=lambda: (claim,),
        systemd_claim_query=lambda: (),
        unmanaged_mps_client_query=lambda *_args: False,
        cache_seconds=0,
    )
    assert cuda_guard(1, gpu1, (), _owner(), "backend", "dev") is True
    unmanaged_mps_guard = ExternalGpuGuard(
        policy,
        process_query=lambda: {},
        docker_claim_query=lambda: (claim,),
        systemd_claim_query=lambda: (),
        unmanaged_mps_client_query=lambda *_args: True,
        cache_seconds=0,
    )
    assert unmanaged_mps_guard(1, gpu1, (), _owner(), "backend", "dev") is True

    mismatched = replace(claim, compose_project="unknown")
    mismatched_guard = ExternalGpuGuard(
        policy,
        process_query=lambda: {},
        docker_claim_query=lambda: (mismatched,),
        systemd_claim_query=lambda: (),
        unmanaged_mps_client_query=lambda *_args: False,
        cache_seconds=0,
    )
    assert mismatched_guard(1, gpu1, (), _owner(), "backend", "dev") is True


def test_systemd_gpu_declaration_requires_exact_managed_unit_registration() -> None:
    gpu2 = "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe"
    claim = SystemdGpuClaim(
        unit="nexpoly-monomer-md-worker.service",
        main_pid=os.getpid(),
        gpu_uuids=frozenset({gpu2}),
    )
    unknown = ExternalGpuGuard(
        ExternalReservationPolicy(frozenset(), {}, {}),
        process_query=lambda: {},
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: (claim,),
        cache_seconds=0,
    )
    managed = ExternalGpuGuard(
        ExternalReservationPolicy(
            frozenset(),
            {},
            {claim.unit: claim.gpu_uuids},
        ),
        process_query=lambda: {},
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: (claim,),
        cache_seconds=0,
    )

    assert unknown(2, gpu2, (), _owner(), "md", "prod") is True
    assert managed(2, gpu2, (), _owner(), "md", "prod") is False

    unbound = replace(claim, main_pid=999_999_999)
    unbound_guard = ExternalGpuGuard(
        managed.policy,
        process_query=lambda: {},
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: (unbound,),
        cache_seconds=0,
    )
    assert unbound_guard(2, gpu2, (), _owner(), "md", "prod") is True


def test_systemd_claim_inventory_reads_active_unit_environment_in_batches() -> None:
    gpu2 = "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe"
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        if "list-units" in command and "--user" in command:
            stdout = "nexpoly-backend.service loaded active running Backend\n"
        elif "show" in command and "--user" in command:
            stdout = (
                "Id=nexpoly-backend.service\n"
                "MainPID=1234\n"
                f'Environment="CUDA_VISIBLE_DEVICES={gpu2}"\n'
                "EnvironmentFiles=\n"
            )
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    claims = query_systemd_gpu_claims(
        run=run,
        read_process_environment=lambda _pid: {"CUDA_VISIBLE_DEVICES": gpu2},
    )

    assert claims == (
        SystemdGpuClaim(
            unit="nexpoly-backend.service",
            main_pid=1234,
            gpu_uuids=frozenset({gpu2}),
        ),
    )
    assert len(calls) == 3


def test_systemd_claim_inventory_reads_environment_files_and_live_process(
    tmp_path: Path,
) -> None:
    gpu1 = "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771"
    environment_file = tmp_path / "worker.env"
    environment_file.write_text(
        f"CUDA_VISIBLE_DEVICES={gpu1}\n", encoding="utf-8"
    )

    def run(command, **_kwargs):
        if "list-units" in command and "--user" in command:
            stdout = "nexpoly-worker.service loaded active running Worker\n"
        elif "show" in command and "--user" in command:
            stdout = (
                "Id=nexpoly-worker.service\n"
                f"MainPID={os.getpid()}\n"
                "Environment=\n"
                f"EnvironmentFiles={environment_file} (ignore_errors=no)\n"
            )
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    claims = query_systemd_gpu_claims(
        run=run,
        read_process_environment=lambda _pid: {"CUDA_VISIBLE_DEVICES": gpu1},
    )

    assert claims == (
        SystemdGpuClaim(
            unit="nexpoly-worker.service",
            main_pid=os.getpid(),
            gpu_uuids=frozenset({gpu1}),
        ),
    )


def test_nonroot_uds_client_acquires_activates_heartbeats_and_releases(tmp_path: Path) -> None:
    broker = HostGpuBroker(
        tmp_path / "state.json",
        mps_clients_alive=lambda _lease: False,
    )
    socket_path = tmp_path / "socket" / "broker.sock"
    server = GpuBrokerUnixServer(socket_path, broker)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = GpuBrokerClient(socket_path)
        managed = client.acquire_managed(
            kind="execution",
            placement="any",
            component="md",
            environment="dev",
            client_id="md-dev",
            memory_mib=8192,
            thread_percent=50,
            wait_timeout_seconds=0,
            heartbeat_interval_seconds=0.05,
        )
        assert managed.lease.status == "active"
        assert managed.lease.gpu_index == 1
        assert socket_path.stat().st_mode & 0o777 == 0o600
        time.sleep(0.12)
        managed.assert_healthy()
        managed.close()
        assert client.status()["leases"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_client_accepts_only_exact_parent_workload_inheritance(tmp_path: Path) -> None:
    gpu_uuid = "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771"
    parent_workload = {
        "workload_pid": 12_345,
        "workload_process_start_ticks": 678,
        "workload_process_group_id": 12_345,
        "workload_cgroup": "0::/nexpoly-gpu-jobs/parent",
    }
    common = {
        "broker_instance_id": "broker-1",
        "component": "dft",
        "environment": "dev",
        "client_id": "dft-dev",
        "gpu_index": 1,
        "gpu_uuid": gpu_uuid,
        "memory_mib": 4096,
        "thread_percent": 50,
        "preferred": True,
    }
    parent = {
        **common,
        **parent_workload,
        "lease_id": "parent-lease",
        "fencing_token": 1,
        "request_id": "dft:dev:residency",
        "kind": "residency",
        "placement": "preferred",
        "parent_lease_id": None,
        "status": "active",
    }

    class Client(GpuBrokerClient):
        def __init__(self, inherited: dict[str, object]) -> None:
            super().__init__(tmp_path / "unused.sock")
            self.inherited = inherited
            self.released = False

        def _request(self, request, *, extra_timeout_seconds=0.0):
            del extra_timeout_seconds
            if request["action"] == "acquire":
                return {
                    **common,
                    "lease_id": "child-lease",
                    "fencing_token": 2,
                    "request_id": "dft:dev:attempt-1",
                    "kind": "execution",
                    "placement": "preferred",
                    "parent_lease_id": "parent-lease",
                    "status": "reserved",
                }
            if request["action"] == "status":
                return {"leases": [parent]}
            if request["action"] == "activate":
                return {
                    **common,
                    **self.inherited,
                    "lease_id": "child-lease",
                    "fencing_token": 2,
                    "request_id": "dft:dev:attempt-1",
                    "kind": "execution",
                    "placement": "preferred",
                    "parent_lease_id": "parent-lease",
                    "status": "active",
                }
            if request["action"] == "release":
                self.released = True
                return {"released": True}
            raise AssertionError(request)

    exact = Client(parent_workload)
    managed = exact.acquire_managed(
        kind="execution",
        placement="preferred",
        component="dft",
        environment="dev",
        client_id="dft-dev",
        memory_mib=4096,
        thread_percent=50,
        wait_timeout_seconds=0,
        heartbeat_interval_seconds=60,
        parent_lease_id="parent-lease",
        request_id="dft:dev:attempt-1",
    )
    assert managed.lease.workload_cgroup == parent_workload["workload_cgroup"]
    managed.abandon()

    arbitrary = Client(
        {
            **parent_workload,
            "workload_pid": 99_999,
            "workload_process_group_id": 99_999,
        }
    )
    with pytest.raises(GpuBrokerClientError, match="exact residency workload"):
        arbitrary.acquire_managed(
            kind="execution",
            placement="preferred",
            component="dft",
            environment="dev",
            client_id="dft-dev",
            memory_mib=4096,
            thread_percent=50,
            wait_timeout_seconds=0,
            heartbeat_interval_seconds=60,
            parent_lease_id="parent-lease",
            request_id="dft:dev:attempt-1",
        )
    assert arbitrary.released is True


def test_client_refuses_regular_file_instead_of_uds(tmp_path: Path) -> None:
    path = tmp_path / "broker.sock"
    path.write_text("not a socket", encoding="utf-8")
    with pytest.raises(GpuBrokerClientError) as error:
        GpuBrokerClient(path).status()
    assert error.value.code == "gpu_broker_unavailable"


def test_failed_release_is_retried_without_dropping_fail_closed_lease() -> None:
    released = threading.Event()

    class Client:
        calls = 0

        def release(self, _lease) -> None:
            self.calls += 1
            if self.calls == 1:
                raise GpuBrokerClientError("gpu_broker_unavailable", "offline")
            released.set()

    lease = GpuLease(
        lease_id="lease-1",
        fencing_token=1,
        broker_instance_id="broker-1",
        kind="execution",
        placement="any",
        component="md",
        environment="dev",
        client_id="md-dev",
        gpu_index=1,
        gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        memory_mib=8192,
        thread_percent=50,
        preferred=True,
        parent_lease_id=None,
        status="active",
    )
    managed = ManagedGpuLease(
        client=Client(),  # type: ignore[arg-type]
        lease=lease,
        heartbeat_interval_seconds=0.01,
    )

    with pytest.raises(GpuBrokerClientError) as error:
        managed.close()
    assert error.value.code == "gpu_broker_unavailable"
    assert released.wait(timeout=1)


def test_managed_heartbeat_treats_transport_failure_as_suspect_until_recovery() -> None:
    transient_seen = threading.Event()
    allow_recovery = threading.Event()
    recovered = threading.Event()
    allow_authoritative_loss = threading.Event()

    class Client:
        calls = 0

        def heartbeat(self, lease):
            self.calls += 1
            if self.calls == 1:
                transient_seen.set()
                raise GpuBrokerClientError("gpu_broker_unavailable", "offline")
            if self.calls == 2:
                allow_recovery.wait(timeout=2)
                recovered.set()
                return lease
            allow_authoritative_loss.wait(timeout=2)
            raise GpuBrokerClientError("stale_fencing_token", "fenced")

    lease = GpuLease(
        lease_id="lease-1",
        fencing_token=1,
        broker_instance_id="broker-1",
        kind="execution",
        placement="any",
        component="md",
        environment="dev",
        client_id="md-dev",
        gpu_index=1,
        gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        memory_mib=8192,
        thread_percent=50,
        preferred=True,
        parent_lease_id=None,
        status="active",
        request_id="md:dev:test",
    )
    managed = ManagedGpuLease(
        client=Client(),  # type: ignore[arg-type]
        lease=lease,
        heartbeat_interval_seconds=0.01,
    )
    managed.start()
    assert transient_seen.wait(timeout=1)
    deadline = time.monotonic() + 1
    while not managed.suspect and time.monotonic() < deadline:
        time.sleep(0.005)
    assert managed.connectivity_status == "suspect"
    assert managed.lost is False

    allow_recovery.set()
    assert recovered.wait(timeout=1)
    deadline = time.monotonic() + 1
    while managed.suspect and time.monotonic() < deadline:
        time.sleep(0.005)
    assert managed.connectivity_status == "healthy"

    allow_authoritative_loss.set()
    deadline = time.monotonic() + 1
    while not managed.lost and time.monotonic() < deadline:
        time.sleep(0.005)
    assert managed.connectivity_status == "lost"
    with pytest.raises(GpuBrokerClientError) as error:
        managed.assert_healthy()
    assert error.value.code == "gpu_lease_lost"
    managed.abandon()


@pytest.mark.parametrize(
    "loss_code",
    ("unknown_lease", "stale_fencing_token", "lease_owner_mismatch"),
)
def test_managed_synchronous_confirmation_records_authoritative_fence_loss(
    loss_code: str,
) -> None:
    class Client:
        def heartbeat(self, _lease):
            raise GpuBrokerClientError(loss_code, "lease is no longer current")

    lease = GpuLease(
        lease_id="lease-1",
        fencing_token=1,
        broker_instance_id="broker-1",
        kind="execution",
        placement="any",
        component="md",
        environment="dev",
        client_id="md-dev",
        gpu_index=1,
        gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        memory_mib=8192,
        thread_percent=50,
        preferred=True,
        parent_lease_id=None,
        status="active",
        request_id="md:dev:test",
    )
    managed = ManagedGpuLease(
        client=Client(),  # type: ignore[arg-type]
        lease=lease,
        heartbeat_interval_seconds=1.0,
    )

    with pytest.raises(GpuBrokerClientError) as error:
        managed.confirm_current()

    assert error.value.code == loss_code
    assert managed.connectivity_status == "lost"
    with pytest.raises(GpuBrokerClientError) as cached_error:
        managed.assert_healthy()
    assert cached_error.value.code == "gpu_lease_lost"


def test_managed_synchronous_confirmation_marks_transport_uncertain() -> None:
    class Client:
        def heartbeat(self, _lease):
            raise GpuBrokerClientError("gpu_broker_unavailable", "offline")

    lease = GpuLease(
        lease_id="lease-1",
        fencing_token=1,
        broker_instance_id="broker-1",
        kind="execution",
        placement="any",
        component="md",
        environment="dev",
        client_id="md-dev",
        gpu_index=1,
        gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        memory_mib=8192,
        thread_percent=50,
        preferred=True,
        parent_lease_id=None,
        status="active",
        request_id="md:dev:test",
    )
    managed = ManagedGpuLease(
        client=Client(),  # type: ignore[arg-type]
        lease=lease,
        heartbeat_interval_seconds=1.0,
    )

    with pytest.raises(GpuBrokerClientError) as error:
        managed.confirm_current()

    assert error.value.code == "gpu_broker_unavailable"
    assert managed.connectivity_status == "suspect"
    assert managed.lost is False


def test_managed_synchronous_confirmation_rejects_terminating_lease() -> None:
    lease = GpuLease(
        lease_id="lease-1",
        fencing_token=1,
        broker_instance_id="broker-1",
        kind="execution",
        placement="any",
        component="md",
        environment="dev",
        client_id="md-dev",
        gpu_index=1,
        gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        memory_mib=8192,
        thread_percent=50,
        preferred=True,
        parent_lease_id=None,
        status="active",
        request_id="md:dev:test",
    )

    class Client:
        def heartbeat(self, _lease):
            return replace(lease, status="terminating")

    managed = ManagedGpuLease(
        client=Client(),  # type: ignore[arg-type]
        lease=lease,
        heartbeat_interval_seconds=1.0,
    )

    with pytest.raises(GpuBrokerClientError) as error:
        managed.confirm_current()

    assert error.value.code == "gpu_lease_lost"
    assert managed.connectivity_status == "lost"


def test_managed_confirmation_and_close_are_linearized_when_confirmation_wins() -> None:
    confirmation_entered = threading.Event()
    allow_confirmation = threading.Event()
    release_called = threading.Event()
    outcomes: list[str] = []
    lease = GpuLease(
        lease_id="lease-1",
        fencing_token=1,
        broker_instance_id="broker-1",
        kind="execution",
        placement="any",
        component="md",
        environment="dev",
        client_id="md-dev",
        gpu_index=1,
        gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        memory_mib=8192,
        thread_percent=50,
        preferred=True,
        parent_lease_id=None,
        status="active",
        request_id="md:dev:test",
    )

    class Client:
        def heartbeat(self, _lease):
            confirmation_entered.set()
            assert allow_confirmation.wait(2.0)
            outcomes.append("confirmed")
            return lease

        def release(self, _lease):
            outcomes.append("released")
            release_called.set()

    managed = ManagedGpuLease(
        client=Client(),  # type: ignore[arg-type]
        lease=lease,
        heartbeat_interval_seconds=1.0,
    )
    confirm_thread = threading.Thread(target=managed.confirm_current)
    close_thread = threading.Thread(target=managed.close)
    confirm_thread.start()
    assert confirmation_entered.wait(2.0)
    close_thread.start()
    assert not release_called.wait(0.05)
    allow_confirmation.set()
    confirm_thread.join(2.0)
    close_thread.join(2.0)

    assert not confirm_thread.is_alive()
    assert not close_thread.is_alive()
    assert outcomes == ["confirmed", "released"]


def test_managed_confirmation_and_close_are_linearized_when_close_wins() -> None:
    release_entered = threading.Event()
    allow_release = threading.Event()
    heartbeat_calls = 0
    confirmation_error: list[GpuBrokerClientError] = []
    lease = GpuLease(
        lease_id="lease-1",
        fencing_token=1,
        broker_instance_id="broker-1",
        kind="execution",
        placement="any",
        component="md",
        environment="dev",
        client_id="md-dev",
        gpu_index=1,
        gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        memory_mib=8192,
        thread_percent=50,
        preferred=True,
        parent_lease_id=None,
        status="active",
        request_id="md:dev:test",
    )

    class Client:
        def heartbeat(self, _lease):
            nonlocal heartbeat_calls
            heartbeat_calls += 1
            return lease

        def release(self, _lease):
            release_entered.set()
            assert allow_release.wait(2.0)

    managed = ManagedGpuLease(
        client=Client(),  # type: ignore[arg-type]
        lease=lease,
        heartbeat_interval_seconds=1.0,
    )

    def confirm() -> None:
        try:
            managed.confirm_current()
        except GpuBrokerClientError as exc:
            confirmation_error.append(exc)

    close_thread = threading.Thread(target=managed.close)
    confirm_thread = threading.Thread(target=confirm)
    close_thread.start()
    assert release_entered.wait(2.0)
    confirm_thread.start()
    assert heartbeat_calls == 0
    allow_release.set()
    close_thread.join(2.0)
    confirm_thread.join(2.0)

    assert not close_thread.is_alive()
    assert not confirm_thread.is_alive()
    assert heartbeat_calls == 0
    assert [error.code for error in confirmation_error] == ["gpu_lease_lost"]


def test_managed_lease_abandons_without_release_when_mps_prepare_fails() -> None:
    class Client:
        released = False

        def prepare_process_termination(self, _lease):
            raise GpuBrokerClientError("gpu_runtime_unhealthy", "MPS failed")

        def release(self, _lease) -> None:
            self.released = True

    lease = GpuLease(
        lease_id="lease-1",
        fencing_token=1,
        broker_instance_id="broker-1",
        kind="execution",
        placement="any",
        component="md",
        environment="dev",
        client_id="md-dev",
        gpu_index=1,
        gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        memory_mib=8192,
        thread_percent=50,
        preferred=True,
        parent_lease_id=None,
        status="active",
    )
    client = Client()
    managed = ManagedGpuLease(
        client=client,  # type: ignore[arg-type]
        lease=lease,
        heartbeat_interval_seconds=1,
    )

    with pytest.raises(GpuBrokerClientError):
        managed.prepare_process_termination()
    managed.close()

    assert managed.termination_unsafe is True
    assert client.released is False


def test_managed_lease_never_retries_mps_unsafe_release() -> None:
    class Client:
        calls = 0

        def release(self, _lease) -> None:
            self.calls += 1
            raise GpuBrokerClientError(
                "gpu_runtime_unhealthy",
                "MPS client remains active",
            )

    lease = GpuLease(
        lease_id="lease-1",
        fencing_token=1,
        broker_instance_id="broker-1",
        kind="execution",
        placement="any",
        component="md",
        environment="dev",
        client_id="md-dev",
        gpu_index=1,
        gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        memory_mib=8192,
        thread_percent=50,
        preferred=True,
        parent_lease_id=None,
        status="active",
    )
    client = Client()
    managed = ManagedGpuLease(
        client=client,  # type: ignore[arg-type]
        lease=lease,
        heartbeat_interval_seconds=0.01,
    )

    with pytest.raises(GpuBrokerClientError) as error:
        managed.close()
    time.sleep(0.03)

    assert error.value.code == "gpu_runtime_unhealthy"
    assert managed.termination_unsafe is True
    assert client.calls == 1


def test_background_release_retry_stops_after_mps_unsafe_response() -> None:
    unsafe_seen = threading.Event()

    class Client:
        calls = 0

        def release(self, _lease) -> None:
            self.calls += 1
            if self.calls == 1:
                raise GpuBrokerClientError("gpu_broker_unavailable", "offline")
            unsafe_seen.set()
            raise GpuBrokerClientError(
                "gpu_runtime_unhealthy",
                "MPS client remains active",
            )

    lease = GpuLease(
        lease_id="lease-1",
        fencing_token=1,
        broker_instance_id="broker-1",
        kind="execution",
        placement="any",
        component="md",
        environment="dev",
        client_id="md-dev",
        gpu_index=1,
        gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        memory_mib=8192,
        thread_percent=50,
        preferred=True,
        parent_lease_id=None,
        status="active",
    )
    client = Client()
    managed = ManagedGpuLease(
        client=client,  # type: ignore[arg-type]
        lease=lease,
        heartbeat_interval_seconds=0.01,
    )

    with pytest.raises(GpuBrokerClientError) as error:
        managed.close()
    assert error.value.code == "gpu_broker_unavailable"
    assert unsafe_seen.wait(timeout=1)
    time.sleep(0.03)

    assert managed.termination_unsafe is True
    assert client.calls == 2


def test_client_rejects_policy_inconsistent_lease_payload() -> None:
    payload = {
        "lease_id": "lease-1",
        "fencing_token": 1,
        "broker_instance_id": "broker-1",
        "kind": "execution",
        "placement": "any",
        "component": "md",
        "environment": "dev",
        "client_id": "md-dev",
        "gpu_index": 2,
        "gpu_uuid": "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe",
        "memory_mib": 8192,
        "thread_percent": 50,
        "preferred": False,
        "parent_lease_id": None,
        "status": "active",
    }

    with pytest.raises(GpuBrokerClientError) as error:
        GpuLease.from_payload(payload)
    assert error.value.code == "invalid_response"
