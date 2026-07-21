from __future__ import annotations

import json
import os
import shutil
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
    scope_control_group,
    scope_unit_name,
    transient_scope_command,
    user_manager_control_group,
    wait_for_scope_membership,
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
    DEFAULT_EXTERNAL_ADMISSION_TIMEOUT_SECONDS,
    DockerGpuClaim,
    ExternalGpuGuard,
    ExternalReservationPolicy,
    GpuBrokerUnixServer,
    JobCgroupController,
    ManagedDockerClaim,
    MpsClient,
    MpsRuntimeGuard,
    SystemdGpuClaim,
    SystemdGpuDeclarer,
    _read_systemd_environment_file,
    _snapshot_systemd_process_cgroups,
    claim_is_exact_dft_residency_scope,
    load_external_reservations,
    process_stable_descriptor_path,
    query_docker_gpu_claims,
    query_systemd_gpu_claims,
    resolve_workload_identity,
    validate_policy_document,
)

_DOCKER_STARTED_AT = "2026-07-19T06:00:00.123456789Z"


def _owner() -> OwnerIdentity:
    return OwnerIdentity(
        pid=os.getpid(),
        process_start_ticks=read_process_start_ticks(os.getpid()),
        boot_id=read_boot_id(),
    )


def test_default_client_timeout_covers_external_admission_budget() -> None:
    client = GpuBrokerClient("/not-opened")

    assert DEFAULT_EXTERNAL_ADMISSION_TIMEOUT_SECONDS == 10.0
    assert client.timeout_seconds == 12.0
    assert (
        client.timeout_seconds
        > DEFAULT_EXTERNAL_ADMISSION_TIMEOUT_SECONDS
    )


def _bind_test_workload(lease) -> None:
    lease.workload_pid = os.getpid()
    lease.workload_process_start_ticks = read_process_start_ticks(os.getpid())
    lease.workload_process_group_id = os.getpgid(os.getpid())
    lease.workload_cgroup = Path(f"/proc/{os.getpid()}/cgroup").read_text(
        encoding="utf-8"
    ).strip()


def test_process_stable_descriptor_path_survives_child_close_fds(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    root.mkdir()
    sentinel = root / "sentinel"
    sentinel.write_text("bound", encoding="utf-8")
    descriptor = os.open(
        root,
        os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY,
    )
    try:
        stable = process_stable_descriptor_path(
            Path(f"/proc/self/fd/{descriptor}/sentinel")
        )
        assert stable == Path(
            f"/proc/{os.getpid()}/fd/{descriptor}/sentinel"
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "print(Path(sys.argv[1]).read_text())"
                ),
                str(stable),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert completed.stdout.strip() == "bound"
        ordinary = tmp_path / "ordinary"
        assert process_stable_descriptor_path(ordinary) == ordinary
    finally:
        os.close(descriptor)


def test_policy_validation_accepts_private_process_descriptor(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "config" / "gpu-broker-policy.json"
    policy = tmp_path / "gpu-policy.json"
    policy.write_bytes(source.read_bytes())
    policy.chmod(0o600)
    descriptor = os.open(policy, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        stable = process_stable_descriptor_path(Path(f"/proc/self/fd/{descriptor}"))
        validate_policy_document(stable)
    finally:
        os.close(descriptor)


def test_policy_validation_rejects_non_private_process_descriptor(
    tmp_path: Path,
) -> None:
    source = Path(__file__).resolve().parents[2] / "config" / "gpu-broker-policy.json"
    policy = tmp_path / "gpu-policy.json"
    policy.write_bytes(source.read_bytes())
    policy.chmod(0o640)
    descriptor = os.open(policy, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        stable = process_stable_descriptor_path(Path(f"/proc/self/fd/{descriptor}"))
        with pytest.raises(BrokerError) as error:
            validate_policy_document(stable)
        assert error.value.code == "invalid_policy"
    finally:
        os.close(descriptor)


def test_broker_main_loads_an_inherited_reservation_descriptor(
    tmp_path: Path,
) -> None:
    if os.getuid() != 1001 or os.getgid() != 1001:
        pytest.skip("runtime Broker deliberately requires owner 1001:1001")
    root = tmp_path / "gpu-resource"
    root.mkdir(mode=0o700)
    inventory = root / "external-reservations.json"
    repository = Path(__file__).resolve().parents[3]
    inventory.write_bytes(
        (
            repository
            / "ops/config/gpu-external-reservations.json"
        ).read_bytes()
    )
    inventory.chmod(0o600)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    nvidia_smi = fake_bin / "nvidia-smi"
    nvidia_smi.write_text(
        (
            "#!/usr/bin/env bash\n"
            "cat <<'EOF'\n"
            "1, GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771\n"
            "2, GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe\n"
            "3, GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5\n"
            "EOF\n"
        ),
        encoding="utf-8",
    )
    nvidia_smi.chmod(0o700)
    root_descriptor = os.open(
        root,
        os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    inventory_descriptor = os.open(
        inventory,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    socket_path = root / "broker.sock"
    process: subprocess.Popen[str] | None = None
    try:
        environment = dict(os.environ)
        environment["PATH"] = (
            f"{fake_bin}:{environment.get('PATH', '/usr/bin:/bin')}"
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ops.gpu_broker.server",
                "--socket",
                f"/proc/self/fd/{root_descriptor}/broker.sock",
                "--state",
                str(tmp_path / "broker-state.json"),
                "--policy",
                str(repository / "ops/config/gpu-broker-policy.json"),
                "--external-reservations",
                f"/proc/self/fd/{inventory_descriptor}",
                "--mps-state-root",
                f"/proc/self/fd/{root_descriptor}",
            ],
            cwd=repository,
            env=environment,
            pass_fds=(root_descriptor, inventory_descriptor),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        while (
            time.monotonic() < deadline
            and process.poll() is None
            and not socket_path.exists()
        ):
            time.sleep(0.02)
        assert process.poll() is None, process.communicate(timeout=1)
        assert socket_path.is_socket()
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)
        os.close(inventory_descriptor)
        os.close(root_descriptor)


@pytest.mark.parametrize(
    "path",
        (
            "/proc/self/fd/0",
            "/proc/self/fd/1",
            "/proc/self/fd/2",
            "/proc/self/fd/01",
            "/proc/self/fd/3/..",
            "/proc/self/fd/not-a-fd",
            f"/proc/{os.getpid()}/fd/3",
            "/proc/999999/fd/3",
        ),
)
def test_process_stable_descriptor_path_rejects_ambiguous_paths(
    path: str,
) -> None:
    with pytest.raises(BrokerError) as error:
        process_stable_descriptor_path(Path(path))
    assert error.value.code == "invalid_runtime_authority"


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


def _scope_lease(lease_id: str = "a" * 32) -> Lease:
    return Lease(
        lease_id=lease_id,
        fencing_token=7,
        broker_instance_id="b" * 32,
        kind="execution",
        placement="preferred",
        component="md",
        environment="dev",
        client_id="md-dev-test",
        gpu_index=1,
        gpu_uuid=EXPECTED_GPU_UUIDS[1],
        memory_mib=8192,
        thread_percent=50,
        owner_pid=11_111,
        owner_process_start_ticks=22_222,
        owner_boot_id="c" * 32,
        preferred=True,
        parent_lease_id=None,
        status="active",
        created_at=1.0,
        heartbeat_at=1.0,
        request_id="md:dev:test",
    )


class _FakeUserSystemd:
    def __init__(
        self,
        *,
        lease_id: str,
        uid: int,
        scope_path: Path,
    ) -> None:
        self.manager_control_group = user_manager_control_group(uid)
        self.unit = scope_unit_name(lease_id)
        self.control_group = scope_control_group(lease_id, uid=uid)
        self.scope_path = scope_path
        self.active = True
        self.overrides: dict[str, str] = {}
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command, **_kwargs):
        command = tuple(command)
        self.commands.append(command)
        if command == (
            "/usr/bin/systemctl",
            "--user",
            "show",
            "--property=ControlGroup",
            "--no-pager",
        ):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"ControlGroup={self.manager_control_group}\n",
                stderr="",
            )
        if len(command) >= 4 and command[2:4] == ("show", self.unit):
            status = (
                {
                    "Id": self.unit,
                    "ControlGroup": self.control_group,
                    "LoadState": "loaded",
                    "ActiveState": "active",
                    "Slice": "nexpoly-gpu-jobs.slice",
                }
                if self.active
                else {
                    "Id": self.unit,
                    "ControlGroup": "",
                    "LoadState": "not-found",
                    "ActiveState": "inactive",
                    "Slice": "",
                }
            )
            status.update(self.overrides)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="".join(f"{key}={value}\n" for key, value in status.items()),
                stderr="",
            )
        if command == (
            "/usr/bin/systemctl",
            "--user",
            "stop",
            self.unit,
        ):
            self.active = False
            if self.scope_path.exists():
                shutil.rmtree(self.scope_path)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected systemctl command: {command!r}")


def _scope_controller(
    tmp_path: Path,
    *,
    lease_id: str = "a" * 32,
    host_pid: int = 43_210,
) -> tuple[
    JobCgroupController,
    Lease,
    Path,
    _FakeUserSystemd,
    dict[int, tuple[int, int, tuple[int, int, int, int]]],
]:
    uid = os.geteuid()
    root = tmp_path / "cgroup"
    root.mkdir(mode=0o700)
    manager = root.joinpath(
        *user_manager_control_group(uid).split("/")[1:]
    )
    manager.mkdir(parents=True, mode=0o755)
    control_group = scope_control_group(lease_id, uid=uid)
    scope_path = root.joinpath(*control_group.split("/")[1:])
    scope_path.mkdir(parents=True, mode=0o755)
    for name, contents in {
        "cgroup.events": "populated 1\nfrozen 1\n",
        "cgroup.freeze": "0",
        "cgroup.kill": "",
        "cgroup.procs": f"{host_pid}\n",
    }.items():
        control = scope_path / name
        control.write_text(contents, encoding="ascii")
        control.chmod(0o600)
    fake_systemd = _FakeUserSystemd(
        lease_id=lease_id,
        uid=uid,
        scope_path=scope_path,
    )
    process_inventory = {
        host_pid: (77_777, host_pid, (uid, uid, uid, uid))
    }

    def process_record(pid: int):
        try:
            return process_inventory[pid]
        except KeyError as exc:
            raise BrokerError(
                "workload_identity_unavailable",
                "test process disappeared",
            ) from exc

    controller = JobCgroupController(
        cgroup_root=root,
        uid=uid,
        run=fake_systemd,
        identity_resolver=lambda _lease, pid, start, group: (
            pid,
            start,
            group,
            f"0::{control_group}",
        ),
        process_uid_resolver=lambda pid: process_record(pid)[2],
        process_start_ticks_reader=lambda pid: process_record(pid)[0],
        process_group_reader=lambda pid: process_record(pid)[1],
        now_ns=lambda: 88_888,
    )
    return (
        controller,
        _scope_lease(lease_id),
        scope_path,
        fake_systemd,
        process_inventory,
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
    pipe_directory.chmod(0o700)
    os.mkfifo(pipe_directory / "control", 0o600)
    (pipe_directory / "control").chmod(0o666)
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


def test_repository_systemd_registrations_bind_the_real_manager_scopes(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[3]
    runtime_policy = tmp_path / "external-reservations.json"
    runtime_policy.write_bytes(
        (
            repository / "ops/config/gpu-external-reservations.json"
        ).read_bytes()
    )
    runtime_policy.chmod(0o600)
    policy = load_external_reservations(runtime_policy)

    assert set(policy.managed_systemd_claims) == {
        "user:nexpoly-monomer-md-worker.service",
        "system:nexpoly-gpu-mps@1.service",
        "system:nexpoly-gpu-mps@2.service",
        "system:nexpoly-gpu-mps@3.service",
    }


def test_global_registrations_remain_valid_when_session_policy_is_narrowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ops.gpu_broker.server as broker_server

    repository = Path(__file__).resolve().parents[3]
    runtime_policy = tmp_path / "external-reservations.json"
    runtime_policy.write_bytes(
        (repository / "ops/config/gpu-external-reservations.json").read_bytes()
    )
    runtime_policy.chmod(0o600)
    monkeypatch.setitem(broker_server.DEVICE_POLICY, ("dev", "md"), (1,))

    policy = load_external_reservations(runtime_policy)

    assert policy.managed_docker_claims["md-dev"].gpu_uuids == frozenset(
        {
            EXPECTED_GPU_UUIDS[1],
            EXPECTED_GPU_UUIDS[3],
        }
    )


def test_external_reservations_accept_only_an_exact_local_inherited_fd(
    tmp_path: Path,
) -> None:
    if os.getuid() != 1001 or os.getgid() != 1001:
        pytest.skip("runtime inventory deliberately requires owner 1001:1001")
    inventory = tmp_path / "external.json"
    inventory.write_text(
        '{"schema_version":1,"blocked_gpu_uuids":{},'
        '"managed_docker_claims":{},"managed_systemd_claims":{}}\n',
        encoding="utf-8",
    )
    inventory.chmod(0o600)
    descriptor = os.open(
        inventory,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        for authority in (
            Path(f"/proc/self/fd/{descriptor}"),
            Path(f"/proc/{os.getpid()}/fd/{descriptor}"),
        ):
            policy = load_external_reservations(authority)
            assert policy.blocked_gpu_uuids == frozenset()
        assert os.lseek(descriptor, 0, os.SEEK_CUR) == 0
        for ambiguous in (
            Path(f"/proc/self/fd/{descriptor}/suffix"),
            Path(f"/proc/{os.getppid()}/fd/{descriptor}"),
            Path("/proc/self/fd/1"),
        ):
            with pytest.raises(BrokerError) as error:
                load_external_reservations(ambiguous)
            assert error.value.code == "external_inventory_unavailable"
    finally:
        os.close(descriptor)

    symlink = tmp_path / "external-link.json"
    symlink.symlink_to(inventory)
    with pytest.raises(BrokerError) as error:
        load_external_reservations(symlink)
    assert error.value.code == "external_inventory_unavailable"


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

    def validate(lease):
        callbacks.append(f"validate:{lease.lease_id}")

    def terminate(lease):
        callbacks.append(f"terminate:{lease.lease_id}")
        return (12_345,)

    def freeze(lease):
        callbacks.append(f"freeze:{lease.lease_id}")
        return f"freeze-{len(callbacks)}-{lease.lease_id}"

    def audit(lease):
        callbacks.append(f"audit:{lease.lease_id}")
        return False

    def kill(lease):
        callbacks.append(f"kill:{lease.lease_id}")

    def empty(lease):
        callbacks.append(f"empty:{lease.lease_id}")
        return True

    broker = HostGpuBroker(
        tmp_path / "state.json",
        validate_workload=validate,
        terminate_mps_clients=terminate,
        freeze_workload=freeze,
        mps_clients_alive=audit,
        kill_workload=kill,
        workload_empty=empty,
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
    expected_attempt = [
        f"validate:{lease.lease_id}",
        f"terminate:{lease.lease_id}",
        f"freeze:{lease.lease_id}",
        f"audit:{lease.lease_id}",
        f"kill:{lease.lease_id}",
        f"empty:{lease.lease_id}",
    ]
    assert callbacks == expected_attempt * 2
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
        validate_workload=lambda _lease: None,
        terminate_mps_clients=terminate,
        freeze_workload=lambda lease: f"freeze-{lease.lease_id}",
        mps_clients_alive=lambda _lease: False,
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


def test_mps_client_reconnect_after_termination_quarantines_before_kill(
    tmp_path: Path,
) -> None:
    callbacks: list[str] = []

    broker = HostGpuBroker(
        tmp_path / "state.json",
        validate_workload=lambda lease: callbacks.append(
            f"validate:{lease.lease_id}"
        ),
        terminate_mps_clients=lambda lease: (
            callbacks.append(f"terminate:{lease.lease_id}") or ()
        ),
        freeze_workload=lambda lease: (
            callbacks.append(f"freeze:{lease.lease_id}")
            or f"freeze:{lease.lease_id}"
        ),
        mps_clients_alive=lambda lease: (
            callbacks.append(f"audit:{lease.lease_id}") or True
        ),
        kill_workload=lambda lease: callbacks.append(f"kill:{lease.lease_id}"),
        workload_empty=lambda lease: (
            callbacks.append(f"empty:{lease.lease_id}") or True
        ),
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
    assert callbacks == [
        f"validate:{lease.lease_id}",
        f"terminate:{lease.lease_id}",
        f"freeze:{lease.lease_id}",
        f"audit:{lease.lease_id}",
    ]
    status = broker.status()
    assert status["leases"][0]["status"] == "suspect"
    assert status["leases"][0]["mps_termination_status"] == "failed"
    assert lease.gpu_uuid in status["quarantined_gpus"]


def test_mps_post_freeze_query_failure_quarantines_before_kill(
    tmp_path: Path,
) -> None:
    killed = False

    def fail_query(_lease):
        raise BrokerError("mps_control_unavailable", "offline")

    def kill(_lease):
        nonlocal killed
        killed = True

    broker = HostGpuBroker(
        tmp_path / "state.json",
        validate_workload=lambda _lease: None,
        terminate_mps_clients=lambda _lease: (),
        freeze_workload=lambda lease: f"freeze:{lease.lease_id}",
        mps_clients_alive=fail_query,
        kill_workload=kill,
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
    assert killed is False
    assert lease.gpu_uuid in broker.status()["quarantined_gpus"]


def test_scope_revalidation_failure_prevents_mps_termination(
    tmp_path: Path,
) -> None:
    terminated = False

    def reject_scope(_lease):
        raise BrokerError("workload_identity_mismatch", "scope drift")

    def terminate(_lease):
        nonlocal terminated
        terminated = True
        return ()

    broker = HostGpuBroker(
        tmp_path / "state.json",
        validate_workload=reject_scope,
        terminate_mps_clients=terminate,
        freeze_workload=lambda lease: f"freeze:{lease.lease_id}",
        mps_clients_alive=lambda _lease: False,
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
    assert terminated is False
    assert lease.gpu_uuid in broker.status()["quarantined_gpus"]


def test_mps_guard_uses_host_ps_pid_and_waits_for_cuda_success(tmp_path: Path) -> None:
    pipe_directory = tmp_path / "mps-1" / "pipe"
    pipe_directory.mkdir(parents=True)
    pipe_directory.chmod(0o700)
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
    pipe_directory.chmod(0o700)
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


@pytest.mark.parametrize(
    "stdout",
    ("", "Server not found", "Server not found\n"),
)
def test_mps_guard_accepts_only_exact_observed_idle_responses(
    tmp_path: Path,
    stdout: str,
) -> None:
    guard = MpsRuntimeGuard(
        tmp_path,
        run=lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=stdout,
            stderr="",
        ),
    )

    assert guard._query_clients(1) == ()


@pytest.mark.parametrize(
    "stdout",
    (
        "\n",
        " \n",
        "Server not found\n\n",
        " server not found\n",
        "PID ID SERVER DEVICE NAMESPACE COMMAND\n",
    ),
)
def test_mps_guard_rejects_noncanonical_idle_responses(
    tmp_path: Path,
    stdout: str,
) -> None:
    guard = MpsRuntimeGuard(
        tmp_path,
        run=lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=stdout,
            stderr="",
        ),
    )

    with pytest.raises(BrokerError) as error:
        guard._query_clients(1)
    assert error.value.code == "mps_control_unavailable"


def test_mps_guard_never_treats_unreadable_client_cgroup_as_absent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    guard = MpsRuntimeGuard(tmp_path)
    monkeypatch.setattr(MpsRuntimeGuard, "__call__", lambda *_args: True)
    monkeypatch.setattr(
        guard,
        "_query_clients",
        lambda _index: (
            MpsClient(
                987_654,
                1,
                9001,
                EXPECTED_GPU_UUIDS[1],
                1,
                "unknown",
            ),
        ),
    )
    monkeypatch.setattr(
        "ops.gpu_broker.server._read_cgroup",
        lambda _pid: (_ for _ in ()).throw(OSError("gone")),
    )
    broker = HostGpuBroker(tmp_path / "state.json")
    lease = _acquire(broker, component="md", environment="dev", kind="execution")
    lease.workload_pid = 123_456
    lease.workload_process_start_ticks = 111
    lease.workload_process_group_id = 123_456
    lease.workload_cgroup = "0::/nexpoly/worker"

    with pytest.raises(BrokerError) as error:
        guard.lease_client_alive(lease)
    assert error.value.code == "mps_control_unavailable"


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


def test_transient_scope_command_uses_only_full_lease_named_user_scope() -> None:
    lease_id = "1a" * 16
    command = transient_scope_command(
        lease_id,
        ("/usr/bin/python3", "-I", "-c", "pass"),
    )

    assert command == (
        "/usr/bin/systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--no-ask-password",
        f"--unit=nexpoly-gpu-job-{lease_id}.scope",
        "--slice=nexpoly-gpu-jobs.slice",
        "--property=KillMode=control-group",
        "--property=CollectMode=inactive-or-failed",
        "--expand-environment=no",
        "--",
        "/usr/bin/python3",
        "-I",
        "-c",
        "pass",
    )
    for invalid in ("short", "A" * 32, "a" * 31, "a" * 33, "../" + "a" * 29):
        with pytest.raises(ValueError, match="complete 32-hex"):
            transient_scope_command(invalid, ("/usr/bin/true",))


def test_scope_transition_waits_for_exact_cgroup_without_pid_reuse() -> None:
    lease_id = "2b" * 16
    expected = scope_control_group(lease_id, uid=os.geteuid())
    observed = iter(
        (
            "0::/user.slice/user-1001.slice/session-1.scope",
            f"0::{expected}",
        )
    )

    assert (
        wait_for_scope_membership(
            54_321,
            lease_id,
            uid=os.geteuid(),
            cgroup_reader=lambda _pid: next(observed),
            start_ticks_reader=lambda _pid: 77_777,
            monotonic=iter((0.0, 0.1)).__next__,
            sleep=lambda _seconds: None,
        )
        == 77_777
    )

    starts = iter((77_777, 77_778))
    with pytest.raises(ValueError, match="PID was reused"):
        wait_for_scope_membership(
            54_321,
            lease_id,
            uid=os.geteuid(),
            cgroup_reader=lambda _pid: "0::/outside.scope",
            start_ticks_reader=lambda _pid: next(starts),
            monotonic=iter((0.0, 0.1)).__next__,
            sleep=lambda _seconds: None,
        )


def test_job_cgroup_controller_refuses_missing_user_manager_cgroup(
    tmp_path: Path,
) -> None:
    with pytest.raises(BrokerError) as error:
        JobCgroupController(cgroup_root=tmp_path / "missing")
    assert error.value.code == "workload_control_unavailable"


def test_job_cgroup_controller_registers_freezes_kills_and_collects_exact_scope(
    tmp_path: Path,
) -> None:
    controller, lease, scope_path, systemd, processes = _scope_controller(
        tmp_path
    )
    pid = next(iter(processes))
    start_ticks, group_id, _uids = processes[pid]

    resolved = controller.resolve_and_assign(
        lease,
        pid,
        start_ticks,
        group_id,
    )
    assert resolved == (
        pid,
        start_ticks,
        group_id,
        f"0::{systemd.control_group}",
    )
    lease.workload_pid = pid
    lease.workload_process_start_ticks = start_ticks
    lease.workload_process_group_id = group_id
    lease.workload_cgroup = resolved[3]

    controller.validate_active(lease)
    assert (scope_path / "cgroup.freeze").read_text(encoding="ascii") == "0"
    assert controller.freeze(lease) == f"{lease.lease_id}:88888"
    assert (scope_path / "cgroup.freeze").read_text(encoding="ascii") == "1"
    controller.kill(lease)
    assert (scope_path / "cgroup.kill").read_text(encoding="ascii") == "1"

    (scope_path / "cgroup.procs").write_text("", encoding="ascii")
    (scope_path / "cgroup.events").write_text(
        "populated 0\nfrozen 1\n", encoding="ascii"
    )
    assert controller.empty(lease) is True
    processes.clear()
    controller.cleanup(lease)
    assert scope_path.exists() is False
    assert systemd.active is False


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("short_lease", "lease ID"),
        ("wrong_cgroup", "exact lease-named"),
        ("unit_id", "unit identity"),
        ("unit_control_group", "unit identity"),
        ("unit_slice", "unit identity"),
        ("foreign_pid", "foreign or reused"),
        ("wrong_uid", "another UID"),
        ("wrong_start", "PID/start-time/process-group"),
        ("wrong_group", "PID/start-time/process-group"),
    ),
)
def test_job_cgroup_controller_rejects_spoofed_or_reused_scope_identity(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    controller, lease, scope_path, systemd, processes = _scope_controller(
        tmp_path,
        lease_id="b" * 32,
    )
    pid = next(iter(processes))
    start_ticks, group_id, uids = processes[pid]

    if mutation == "short_lease":
        lease.lease_id = "bad"
    elif mutation == "wrong_cgroup":
        controller._identity_resolver = lambda *_args: (
            pid,
            start_ticks,
            group_id,
            "0::/arbitrary.scope",
        )
    elif mutation == "unit_id":
        systemd.overrides["Id"] = "nexpoly-gpu-job-" + "c" * 32 + ".scope"
    elif mutation == "unit_control_group":
        systemd.overrides["ControlGroup"] = (
            systemd.control_group.rsplit("/", 1)[0]
            + "/nexpoly-gpu-job-"
            + "c" * 32
            + ".scope"
        )
    elif mutation == "unit_slice":
        systemd.overrides["Slice"] = "app.slice"
    elif mutation == "foreign_pid":
        (scope_path / "cgroup.procs").write_text(
            f"{pid}\n{pid + 1}\n",
            encoding="ascii",
        )
        processes[pid + 1] = (99_999, pid + 1, uids)
    elif mutation == "wrong_uid":
        processes[pid] = (start_ticks, group_id, (uids[0] + 1,) * 4)
    elif mutation == "wrong_start":
        processes[pid] = (start_ticks + 1, group_id, uids)
    elif mutation == "wrong_group":
        processes[pid] = (start_ticks, group_id + 1, uids)

    with pytest.raises(BrokerError, match=message) as error:
        controller.resolve_and_assign(lease, pid, start_ticks, group_id)

    assert error.value.code in {
        "workload_control_unavailable",
        "workload_identity_mismatch",
    }


def test_job_cgroup_controller_rejects_active_pid_replacement_after_registration(
    tmp_path: Path,
) -> None:
    controller, lease, scope_path, _systemd, processes = _scope_controller(
        tmp_path
    )
    pid = next(iter(processes))
    start_ticks, group_id, uids = processes[pid]
    resolved = controller.resolve_and_assign(
        lease, pid, start_ticks, group_id
    )
    lease.workload_pid = pid
    lease.workload_process_start_ticks = start_ticks
    lease.workload_process_group_id = group_id
    lease.workload_cgroup = resolved[3]
    replacement = pid + 1
    processes[replacement] = (start_ticks + 1, replacement, uids)
    (scope_path / "cgroup.procs").write_text(
        f"{replacement}\n", encoding="ascii"
    )

    with pytest.raises(BrokerError, match="workload identity differs") as error:
        controller.kill(lease)

    assert error.value.code == "workload_identity_mismatch"
    assert (scope_path / "cgroup.kill").read_text(encoding="ascii") == ""


def test_job_cgroup_controller_restart_accepts_only_collected_dead_scope(
    tmp_path: Path,
) -> None:
    controller, lease, scope_path, systemd, processes = _scope_controller(
        tmp_path
    )
    pid = next(iter(processes))
    start_ticks, group_id, _uids = processes[pid]
    lease.workload_pid = pid
    lease.workload_process_start_ticks = start_ticks
    lease.workload_process_group_id = group_id
    lease.workload_cgroup = f"0::{systemd.control_group}"
    shutil.rmtree(scope_path)
    systemd.active = False

    # A restarted Broker may finish cleanup only after proving the original
    # PID/start identity is gone or reused.
    monotonic_values = iter((0.0, 0.0, 3.0))
    controller._monotonic = lambda: next(monotonic_values, 3.0)
    controller._sleep = lambda _seconds: None
    assert controller.empty(lease) is False

    # cgroup.kill may leave the exact process as a zombie until the blocked
    # Worker receives the Broker response and can reap it.
    controller._process_state_reader = lambda _pid: "Z"
    assert controller.empty(lease) is True
    controller.cleanup(lease)


def test_job_cgroup_controller_requires_exact_user_manager_bus_identity(
    tmp_path: Path,
) -> None:
    uid = os.geteuid()
    root = tmp_path / "cgroup"
    root.mkdir(mode=0o700)
    manager = root.joinpath(*user_manager_control_group(uid).split("/")[1:])
    manager.mkdir(parents=True, mode=0o755)

    def wrong_manager(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="ControlGroup=/user.slice/user-999.slice/user@999.service\n",
            stderr="",
        )

    with pytest.raises(BrokerError, match="manager cgroup identity differs") as error:
        JobCgroupController(
            cgroup_root=root,
            uid=uid,
            run=wrong_manager,
        )

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
    pipe_directory.chmod(0o700)
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
    pipe_directory.chmod(0o700)
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
    lease = _acquire(
        broker,
        component="md",
        environment="dev",
        kind="execution",
        wait=5,
    )
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
    pipe_directory.chmod(0o700)
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


def test_descriptor_broker_hard_fences_production_requests_and_gpu2(
    tmp_path: Path,
) -> None:
    guard = ExternalGpuGuard(
        ExternalReservationPolicy(frozenset(), {}, {}),
        process_query=lambda: {},
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: (),
        allow_descriptor_mps_authority=True,
    )
    broker = HostGpuBroker(
        tmp_path / "state.json",
        gpu_runtime_healthy=lambda _index, _uuid: True,
        gpu_externally_busy=guard,
    )

    dev = _acquire(
        broker,
        component="backend",
        environment="dev",
        kind="residency",
    )
    assert dev.gpu_index == 1
    assert (
        guard(
            2,
            EXPECTED_GPU_UUIDS[2],
            (),
            _owner(),
            "dft",
            "dev",
        )
        is True
    )
    assert (
        guard(
            1,
            EXPECTED_GPU_UUIDS[1],
            (),
            _owner(),
            "dft",
            "prod",
        )
        is True
    )
    with pytest.raises(BrokerError) as error:
        _acquire(
            broker,
            component="backend",
            environment="prod",
            kind="residency",
        )
    assert error.value.code == "gpu_capacity_unavailable"


def test_external_guard_rechecks_live_compute_at_final_allow_edge() -> None:
    gpu1 = EXPECTED_GPU_UUIDS[1]
    snapshots = iter(({}, {}, {}, {gpu1: frozenset({91_001})}))
    guard = ExternalGpuGuard(
        ExternalReservationPolicy(frozenset(), {}, {}),
        process_query=lambda: next(snapshots),
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: (),
        cache_seconds=60,
    )

    assert guard(1, gpu1, (), _owner(), "backend", "dev") is False
    assert guard(1, gpu1, (), _owner(), "backend", "dev") is True


def test_external_guard_rechecks_idle_docker_claim_at_final_allow_edge() -> None:
    gpu1 = EXPECTED_GPU_UUIDS[1]
    claim = DockerGpuClaim(
        container_id="e" * 64,
        init_pid=91_011,
        started_at=_DOCKER_STARTED_AT,
        restart_count=0,
        registration_id=None,
        component=None,
        environment=None,
        compose_project=None,
        compose_service=None,
        gpu_uuids=frozenset({gpu1}),
    )
    snapshots = iter(((), (claim,)))
    guard = ExternalGpuGuard(
        ExternalReservationPolicy(frozenset(), {}, {}),
        process_query=lambda: {},
        docker_claim_query=lambda: next(snapshots),
        systemd_claim_query=lambda: (),
    )

    assert guard(1, gpu1, (), _owner(), "backend", "dev") is True


def test_external_guard_rechecks_idle_systemd_claim_at_final_allow_edge() -> None:
    gpu1 = EXPECTED_GPU_UUIDS[1]
    claim = SystemdGpuClaim(
        scope="user",
        unit="late-worker.service",
        main_pid=91_012,
        control_group="/user.slice/late-worker.service",
        process_pids=frozenset({91_012}),
        gpu_uuids=frozenset({gpu1}),
    )
    snapshots = iter(((), (claim,)))
    guard = ExternalGpuGuard(
        ExternalReservationPolicy(frozenset(), {}, {}),
        process_query=lambda: {},
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: next(snapshots),
    )

    assert guard(1, gpu1, (), _owner(), "backend", "dev") is True


def test_external_guard_rechecks_mps_clients_at_final_allow_edge() -> None:
    gpu1 = EXPECTED_GPU_UUIDS[1]
    unmanaged = iter((False, True))
    guard = ExternalGpuGuard(
        ExternalReservationPolicy(frozenset(), {}, {}),
        process_query=lambda: {},
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: (),
        unmanaged_mps_client_query=lambda *_args: next(unmanaged),
        authorized_mps_server_pids=lambda *_args: frozenset(),
    )

    assert guard(1, gpu1, (), _owner(), "backend", "dev") is True


def test_external_guard_rechecks_target_compute_after_final_mps_audit() -> None:
    gpu1 = EXPECTED_GPU_UUIDS[1]
    snapshots = iter(
        (
            {},
            {},
            {gpu1: frozenset({91_014})},
        )
    )
    mps_audits = 0

    def mps_clients(*_args) -> bool:
        nonlocal mps_audits
        mps_audits += 1
        return False

    guard = ExternalGpuGuard(
        ExternalReservationPolicy(frozenset(), {}, {}),
        process_query=lambda: next(snapshots),
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: (),
        unmanaged_mps_client_query=mps_clients,
        authorized_mps_server_pids=lambda *_args: frozenset(),
    )

    assert guard(1, gpu1, (), _owner(), "backend", "dev") is True
    assert mps_audits == 2


def test_external_guard_shares_initial_inventory_across_one_candidate_search(
    tmp_path: Path,
) -> None:
    gpu1 = EXPECTED_GPU_UUIDS[1]
    calls = {"compute": 0, "docker": 0, "systemd": 0}
    claim = DockerGpuClaim(
        container_id="f" * 64,
        init_pid=91_013,
        started_at=_DOCKER_STARTED_AT,
        restart_count=0,
        registration_id=None,
        component=None,
        environment=None,
        compose_project=None,
        compose_service=None,
        gpu_uuids=frozenset({gpu1}),
    )

    def compute():
        calls["compute"] += 1
        return {}

    def docker():
        calls["docker"] += 1
        return (claim,)

    def systemd():
        calls["systemd"] += 1
        return ()

    guard = ExternalGpuGuard(
        ExternalReservationPolicy(frozenset(), {}, {}),
        process_query=compute,
        docker_claim_query=docker,
        systemd_claim_query=systemd,
    )
    broker = HostGpuBroker(
        tmp_path / "state.json",
        gpu_runtime_healthy=lambda _index, _uuid: True,
        gpu_externally_busy=guard,
    )

    lease = _acquire(
        broker,
        component="md",
        environment="dev",
        kind="execution",
    )

    assert lease.gpu_index == 3
    assert calls == {"compute": 3, "docker": 2, "systemd": 2}


def test_external_guard_parallelizes_default_docker_and_systemd_authorities(
    monkeypatch,
) -> None:
    rendezvous = threading.Barrier(2, timeout=1)
    first_docker_call = True
    first_systemd_call = True

    def docker_run(command, **_kwargs):
        nonlocal first_docker_call
        assert command[:3] == ["docker", "container", "ls"]
        if first_docker_call:
            first_docker_call = False
            rendezvous.wait()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def systemd_run(command, **_kwargs):
        nonlocal first_systemd_call
        assert command[0] == "systemctl"
        if first_systemd_call:
            first_systemd_call = False
            rendezvous.wait()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setitem(
        query_docker_gpu_claims.__kwdefaults__,
        "run",
        docker_run,
    )
    monkeypatch.setitem(
        query_systemd_gpu_claims.__kwdefaults__,
        "run",
        systemd_run,
    )
    monkeypatch.setitem(
        query_systemd_gpu_claims.__kwdefaults__,
        "read_control_group_processes",
        lambda _path: frozenset(),
    )
    guard = ExternalGpuGuard(
        ExternalReservationPolicy(frozenset(), {}, {}),
        process_query=lambda: {},
    )

    snapshot = guard._inventories()

    assert snapshot.processes == {}
    assert snapshot.docker_claims == ()
    assert snapshot.systemd_claims == ()


def test_external_guard_fails_closed_on_parallel_authority_failure(
    monkeypatch,
) -> None:
    def docker_run(_command, **_kwargs):
        raise OSError("Docker authority unavailable")

    def systemd_run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setitem(
        query_docker_gpu_claims.__kwdefaults__,
        "run",
        docker_run,
    )
    monkeypatch.setitem(
        query_systemd_gpu_claims.__kwdefaults__,
        "run",
        systemd_run,
    )
    monkeypatch.setitem(
        query_systemd_gpu_claims.__kwdefaults__,
        "read_control_group_processes",
        lambda _path: frozenset(),
    )
    guard = ExternalGpuGuard(
        ExternalReservationPolicy(frozenset(), {}, {}),
        process_query=lambda: {},
    )

    assert (
        guard(
            1,
            EXPECTED_GPU_UUIDS[1],
            (),
            _owner(),
            "backend",
            "dev",
        )
        is True
    )


def test_admission_deadline_replies_before_client_timeout_without_lease(
    tmp_path: Path,
    monkeypatch,
) -> None:
    docker_done = threading.Event()
    systemd_done = threading.Event()

    def docker_run(command, **_kwargs):
        time.sleep(0.25)
        docker_done.set()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def systemd_run(command, **_kwargs):
        time.sleep(0.25)
        systemd_done.set()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setitem(
        query_docker_gpu_claims.__kwdefaults__,
        "run",
        docker_run,
    )
    monkeypatch.setitem(
        query_systemd_gpu_claims.__kwdefaults__,
        "run",
        systemd_run,
    )
    monkeypatch.setitem(
        query_systemd_gpu_claims.__kwdefaults__,
        "read_control_group_processes",
        lambda _path: frozenset(),
    )
    guard = ExternalGpuGuard(
        ExternalReservationPolicy(frozenset(), {}, {}),
        process_query=lambda: {},
        admission_timeout_seconds=0.05,
    )
    broker = HostGpuBroker(
        tmp_path / "state.json",
        gpu_externally_busy=guard,
    )
    socket_path = tmp_path / "socket" / "broker.sock"
    server = GpuBrokerUnixServer(socket_path, broker)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = GpuBrokerClient(socket_path, timeout_seconds=0.5)
        started = time.monotonic()
        with pytest.raises(GpuBrokerClientError) as error:
            client.acquire_managed(
                kind="residency",
                placement="preferred",
                component="backend",
                environment="dev",
                client_id="deadline-client",
                memory_mib=8192,
                thread_percent=100,
                wait_timeout_seconds=0,
                request_id="deadline-client-request",
            )
        elapsed = time.monotonic() - started

        assert error.value.code == "gpu_capacity_unavailable"
        assert elapsed < client.timeout_seconds
        assert broker.status()["leases"] == []
        assert broker.status()["waiters"] == 0
        persisted = json.loads(
            (tmp_path / "state.json").read_text(encoding="utf-8")
        )
        assert persisted["leases"] == []
        assert persisted["waiters"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert docker_done.wait(timeout=1)
        assert systemd_done.wait(timeout=1)


def test_parallel_authority_exception_does_not_wait_for_slow_peer(
    monkeypatch,
) -> None:
    slow_peer_done = threading.Event()
    slow_peer_started = threading.Event()

    def docker_run(_command, **_kwargs):
        assert slow_peer_started.wait(timeout=1)
        raise OSError("Docker authority unavailable")

    def systemd_run(command, **_kwargs):
        slow_peer_started.set()
        time.sleep(0.25)
        slow_peer_done.set()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setitem(
        query_docker_gpu_claims.__kwdefaults__,
        "run",
        docker_run,
    )
    monkeypatch.setitem(
        query_systemd_gpu_claims.__kwdefaults__,
        "run",
        systemd_run,
    )
    monkeypatch.setitem(
        query_systemd_gpu_claims.__kwdefaults__,
        "read_control_group_processes",
        lambda _path: frozenset(),
    )
    guard = ExternalGpuGuard(
        ExternalReservationPolicy(frozenset(), {}, {}),
        process_query=lambda: {},
        admission_timeout_seconds=0.2,
    )

    started = time.monotonic()
    assert (
        guard(
            1,
            EXPECTED_GPU_UUIDS[1],
            (),
            _owner(),
            "backend",
            "dev",
        )
        is True
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.15
    assert slow_peer_done.wait(timeout=1)


def test_one_deadline_covers_two_inventories_mps_and_trailing_compute(
    tmp_path: Path,
) -> None:
    now = [100.0]
    calls = {
        "compute": 0,
        "docker": 0,
        "systemd": 0,
        "server": 0,
        "unmanaged": 0,
    }
    seen_deadlines: list[float] = []

    def monotonic() -> float:
        return now[0]

    def record(name: str, advance: float, result):
        def callback(*_args, deadline):
            calls[name] += 1
            seen_deadlines.append(deadline)
            now[0] += advance
            return result

        return callback

    guard = ExternalGpuGuard(
        ExternalReservationPolicy(frozenset(), {}, {}),
        process_query=record("compute", 0.02, {}),
        docker_claim_query=record("docker", 0.02, ()),
        systemd_claim_query=record("systemd", 0.02, ()),
        authorized_mps_server_pids=record(
            "server",
            0.01,
            frozenset(),
        ),
        unmanaged_mps_client_query=record("unmanaged", 0.01, False),
        admission_timeout_seconds=0.17,
        monotonic=monotonic,
    )
    broker = HostGpuBroker(
        tmp_path / "state.json",
        gpu_externally_busy=guard,
    )

    with pytest.raises(BrokerError) as error:
        _acquire(
            broker,
            component="md",
            environment="dev",
            kind="execution",
        )

    assert error.value.code == "gpu_capacity_unavailable"
    assert calls == {
        "compute": 3,
        "docker": 2,
        "systemd": 2,
        "server": 2,
        "unmanaged": 2,
    }
    assert len(set(seen_deadlines)) == 1
    assert broker.status()["leases"] == []
    assert broker.status()["waiters"] == 0


def test_external_admission_finalizer_fences_lease_insertion(
    tmp_path: Path,
) -> None:
    class Admission:
        def __call__(self, _index, _uuid):
            return False

        def finalize(self, _index, _uuid):
            raise BrokerError(
                "gpu_admission_timeout",
                "deadline expired before lease insertion",
            )

    class Authority:
        def begin_admission(self, **_kwargs):
            return Admission()

    broker = HostGpuBroker(
        tmp_path / "state.json",
        gpu_externally_busy=Authority(),
    )

    with pytest.raises(BrokerError) as error:
        _acquire(
            broker,
            component="backend",
            environment="dev",
            kind="residency",
        )

    assert error.value.code == "gpu_admission_timeout"
    assert broker.status()["leases"] == []
    assert broker.status()["waiters"] == 0
    persisted = json.loads(
        (tmp_path / "state.json").read_text(encoding="utf-8")
    )
    assert persisted["leases"] == []
    assert persisted["waiters"] == []


def test_external_guard_never_caches_idle_docker_device_requests() -> None:
    gpu1 = EXPECTED_GPU_UUIDS[1]
    claim = DockerGpuClaim(
        container_id="a" * 64,
        init_pid=91_002,
        started_at=_DOCKER_STARTED_AT,
        restart_count=0,
        registration_id=None,
        component=None,
        environment=None,
        compose_project=None,
        compose_service=None,
        gpu_uuids=frozenset({gpu1}),
    )
    snapshots = iter(((), (), (claim,)))
    guard = ExternalGpuGuard(
        ExternalReservationPolicy(frozenset(), {}, {}),
        process_query=lambda: {},
        docker_claim_query=lambda: next(snapshots),
        systemd_claim_query=lambda: (),
        cache_seconds=60,
    )

    assert guard(1, gpu1, (), _owner(), "backend", "dev") is False
    assert guard(1, gpu1, (), _owner(), "backend", "dev") is True


def test_external_guard_never_caches_idle_systemd_gpu_claims() -> None:
    gpu1 = EXPECTED_GPU_UUIDS[1]
    claim = SystemdGpuClaim(
        scope="user",
        unit="foreign-worker.service",
        main_pid=91_003,
        control_group="/user.slice/foreign-worker.service",
        process_pids=frozenset({91_003}),
        gpu_uuids=frozenset({gpu1}),
    )
    snapshots = iter(((), (), (claim,)))
    guard = ExternalGpuGuard(
        ExternalReservationPolicy(frozenset(), {}, {}),
        process_query=lambda: {},
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: next(snapshots),
        cache_seconds=60,
    )

    assert guard(1, gpu1, (), _owner(), "backend", "dev") is False
    assert guard(1, gpu1, (), _owner(), "backend", "dev") is True


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


def _exact_mps_authority(
    tmp_path: Path,
) -> tuple[MpsRuntimeGuard, dict[str, object]]:
    control_pid = 31_301
    server_pid = 42_402
    gpu_uuid = EXPECTED_GPU_UUIDS[1]
    pipe_directory = tmp_path / "mps-1" / "pipe"
    pipe_directory.mkdir(parents=True)
    pipe_directory.chmod(0o700)
    os.mkfifo(pipe_directory / "control", 0o600)
    pid_file = pipe_directory / "nvidia-cuda-mps-control.pid"
    pid_file.write_text(f"{control_pid}\n", encoding="ascii")
    pid_file.chmod(0o600)

    proc_root = tmp_path / "proc"
    status_paths: dict[int, Path] = {}
    executable_paths: dict[int, Path] = {}
    trusted_executable = Path("/bin/true")
    for pid in (control_pid, server_pid):
        process_directory = proc_root / str(pid)
        process_directory.mkdir(parents=True)
        status_path = process_directory / "status"
        status_path.write_text(
            "Name:\tnvidia-cuda-mps\n"
            "Uid:\t1001\t1001\t1001\t1001\n"
            "Gid:\t1001\t1001\t1001\t1001\n",
            encoding="ascii",
        )
        executable_path = process_directory / "exe"
        executable_path.symlink_to(trusted_executable)
        status_paths[pid] = status_path
        executable_paths[pid] = executable_path

    state: dict[str, object] = {
        "control_pid": control_pid,
        "server_pid": server_pid,
        "gpu_uuid": gpu_uuid,
        "pipe_directory": pipe_directory,
        "pid_file": pid_file,
        "status_paths": status_paths,
        "executable_paths": executable_paths,
        "server_list": f"{server_pid}\n",
        "client_inventory": "",
        "commands": [],
        "run_argvs": [],
        "run_envs": [],
        "command_hook": None,
        "environments": {
            control_pid: {
                "CUDA_VISIBLE_DEVICES": gpu_uuid,
                "CUDA_MPS_PIPE_DIRECTORY": str(pipe_directory),
            }
        },
        "cgroups": {
            control_pid: "0::/user.slice/nexpoly-mps-test.scope",
            server_pid: "0::/user.slice/nexpoly-mps-test.scope",
        },
        "ticks": {control_pid: 101, server_pid: 202},
        "unstable_pid": None,
        "tick_reads": {},
    }

    def run(command, **kwargs):
        control_command = kwargs["input"].strip()
        commands = state["commands"]
        run_argvs = state["run_argvs"]
        run_envs = state["run_envs"]
        assert isinstance(commands, list)
        assert isinstance(run_argvs, list)
        assert isinstance(run_envs, list)
        commands.append(control_command)
        run_argvs.append(tuple(command))
        run_envs.append(dict(kwargs["env"]))
        command_hook = state["command_hook"]
        if command_hook is not None:
            assert callable(command_hook)
            command_hook(control_command)
        if control_command == "get_server_list":
            stdout = state["server_list"]
        elif control_command == "ps":
            stdout = state["client_inventory"]
        else:
            raise AssertionError(f"unexpected MPS control command: {control_command}")
        assert isinstance(stdout, str)
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    def read_environment(pid: int) -> dict[str, str]:
        environments = state["environments"]
        assert isinstance(environments, dict)
        return dict(environments[pid])

    def read_cgroup(pid: int) -> str:
        cgroups = state["cgroups"]
        assert isinstance(cgroups, dict)
        return cgroups[pid]

    def read_ticks(pid: int) -> int:
        tick_reads = state["tick_reads"]
        ticks = state["ticks"]
        assert isinstance(tick_reads, dict)
        assert isinstance(ticks, dict)
        reads = tick_reads.get(pid, 0)
        tick_reads[pid] = reads + 1
        value = ticks[pid]
        if state["unstable_pid"] == pid and reads % 2:
            return value + 1
        return value

    guard = MpsRuntimeGuard(
        tmp_path,
        run=run,
        control_executable=trusted_executable,
        server_executable=trusted_executable,
        proc_root=proc_root,
        read_process_environment=read_environment,
        read_process_cgroup=read_cgroup,
        read_start_ticks=read_ticks,
    )
    return guard, state


def test_external_guard_never_trusts_spoofed_mps_name_without_exact_authority(
    tmp_path: Path,
) -> None:
    gpu_uuid = EXPECTED_GPU_UUIDS[1]
    spoofed_pid = 91_001
    spoofed_proc = tmp_path / "proc" / str(spoofed_pid)
    spoofed_proc.mkdir(parents=True)
    (spoofed_proc / "comm").write_text(
        "nvidia-cuda-mps-server\n",
        encoding="ascii",
    )
    (spoofed_proc / "status").write_text(
        "Uid:\t1001\t1001\t1001\t1001\n",
        encoding="ascii",
    )
    policy = ExternalReservationPolicy(frozenset(), {}, {})

    no_authority = ExternalGpuGuard(
        policy,
        process_query=lambda: {gpu_uuid: frozenset({spoofed_pid})},
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: (),
        cache_seconds=0,
    )
    unavailable_authority = ExternalGpuGuard(
        policy,
        process_query=lambda: {gpu_uuid: frozenset({spoofed_pid})},
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: (),
        authorized_mps_server_pids=lambda *_args: (_ for _ in ()).throw(
            BrokerError("mps_control_unavailable", "unavailable")
        ),
        cache_seconds=0,
    )

    assert no_authority(1, gpu_uuid, (), _owner(), "backend", "dev") is True
    assert (
        unavailable_authority(1, gpu_uuid, (), _owner(), "backend", "dev")
        is True
    )


def test_exact_idle_mps_authority_is_the_only_server_process_exemption(
    tmp_path: Path,
) -> None:
    mps_guard, state = _exact_mps_authority(tmp_path)
    gpu_uuid = state["gpu_uuid"]
    server_pid = state["server_pid"]
    assert isinstance(gpu_uuid, str)
    assert isinstance(server_pid, int)

    assert mps_guard.authorized_server_pids(1, gpu_uuid) == frozenset(
        {server_pid}
    )
    assert state["commands"] == ["get_server_list", "ps", "get_server_list"]

    external_guard = ExternalGpuGuard(
        ExternalReservationPolicy(frozenset(), {}, {}),
        process_query=lambda: {gpu_uuid: frozenset({server_pid})},
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: (),
        unmanaged_mps_client_query=lambda *_args: False,
        authorized_mps_server_pids=mps_guard.authorized_server_pids,
        allow_descriptor_mps_authority=True,
        cache_seconds=0,
    )
    assert (
        external_guard(1, gpu_uuid, (), _owner(), "backend", "dev") is False
    )


def test_mps_control_queries_use_exact_executable_and_minimal_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    mps_guard, state = _exact_mps_authority(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}/attacker:/usr/bin")
    monkeypatch.setenv("LD_PRELOAD", f"{tmp_path}/attacker.so")

    mps_guard.authorized_server_pids(1, EXPECTED_GPU_UUIDS[1])

    assert state["run_argvs"] == [
        (str(mps_guard.control_executable),),
        (str(mps_guard.control_executable),),
        (str(mps_guard.control_executable),),
    ]
    assert state["run_envs"] == [
        {
            "LC_ALL": "C",
            "CUDA_MPS_PIPE_DIRECTORY": str(mps_guard.pipe_directory(1)),
        }
    ] * 3


def test_mps_runtime_rejects_relative_executable_authority(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        MpsRuntimeGuard(
            tmp_path,
            control_executable=Path("nvidia-cuda-mps-control"),
        )


def test_mps_authority_rejects_control_endpoint_replacement_during_audit(
    tmp_path: Path,
) -> None:
    mps_guard, state = _exact_mps_authority(tmp_path)
    pipe_directory = state["pipe_directory"]
    assert isinstance(pipe_directory, Path)

    def replace_control(command: str) -> None:
        if command != "ps":
            return
        control = pipe_directory / "control"
        replacement = pipe_directory / "replacement-control"
        os.mkfifo(replacement, 0o600)
        control.unlink()
        replacement.rename(control)

    state["command_hook"] = replace_control

    with pytest.raises(BrokerError, match="changed during audit") as error:
        mps_guard.authorized_server_pids(1, EXPECTED_GPU_UUIDS[1])

    assert error.value.code == "mps_control_unavailable"


def test_mps_authority_discovers_exact_control_when_nvidia_omits_pid_file(
    tmp_path: Path,
) -> None:
    mps_guard, state = _exact_mps_authority(tmp_path)
    pid_file = state["pid_file"]
    server_pid = state["server_pid"]
    gpu_uuid = state["gpu_uuid"]
    assert isinstance(pid_file, Path)
    assert isinstance(server_pid, int)
    assert isinstance(gpu_uuid, str)
    pid_file.unlink()

    assert mps_guard.authorized_server_pids(1, gpu_uuid) == frozenset(
        {server_pid}
    )


def test_mps_authority_rejects_hardlinked_control_endpoint(tmp_path: Path) -> None:
    mps_guard, state = _exact_mps_authority(tmp_path)
    pipe_directory = state["pipe_directory"]
    assert isinstance(pipe_directory, Path)
    os.link(pipe_directory / "control", tmp_path / "control-alias")

    with pytest.raises(BrokerError, match="pipe authority is unavailable") as error:
        mps_guard.authorized_server_pids(1, EXPECTED_GPU_UUIDS[1])

    assert error.value.code == "mps_control_unavailable"


def test_host_mps_authority_requires_exact_systemd_unit_binding(
    tmp_path: Path,
) -> None:
    mps_guard, state = _exact_mps_authority(tmp_path)
    gpu_uuid = state["gpu_uuid"]
    server_pid = state["server_pid"]
    control_pid = state["control_pid"]
    assert isinstance(gpu_uuid, str)
    assert isinstance(server_pid, int)
    assert isinstance(control_pid, int)
    policy_key = "system:nexpoly-gpu-mps@1.service"

    unbound = ExternalGpuGuard(
        ExternalReservationPolicy(
            frozenset(),
            {},
            {policy_key: frozenset({gpu_uuid})},
        ),
        process_query=lambda: {gpu_uuid: frozenset({server_pid})},
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: (),
        unmanaged_mps_client_query=lambda *_args: False,
        authorized_mps_server_pids=mps_guard.authorized_server_pids,
        cache_seconds=0,
    )
    assert unbound(1, gpu_uuid, (), _owner(), "backend", "dev") is True

    claim = SystemdGpuClaim(
        scope="system",
        unit="nexpoly-gpu-mps@1.service",
        main_pid=0,
        control_group="/system.slice/nexpoly-gpu-mps@1.service",
        process_pids=frozenset({control_pid, server_pid}),
        gpu_uuids=frozenset({gpu_uuid}),
    )
    bound = ExternalGpuGuard(
        unbound.policy,
        process_query=lambda: {gpu_uuid: frozenset({server_pid})},
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: (claim,),
        unmanaged_mps_client_query=lambda *_args: False,
        authorized_mps_server_pids=mps_guard.authorized_server_pids,
        cache_seconds=0,
    )
    assert bound(1, gpu_uuid, (), _owner(), "backend", "dev") is False


@pytest.mark.parametrize(
    "server_list",
    (
        "0\n",
        "042402\n",
        " 42402\n",
        "42402 \n",
        "not-a-pid\n",
        "42402\n42402\n",
    ),
)
def test_mps_server_authority_rejects_invalid_server_list(
    tmp_path: Path,
    server_list: str,
) -> None:
    mps_guard, state = _exact_mps_authority(tmp_path)
    state["server_list"] = server_list

    with pytest.raises(BrokerError) as error:
        mps_guard.authorized_server_pids(1, EXPECTED_GPU_UUIDS[1])

    assert error.value.code == "mps_control_unavailable"


def test_mps_server_authority_rejects_multiple_servers(tmp_path: Path) -> None:
    mps_guard, state = _exact_mps_authority(tmp_path)
    state["server_list"] = "42402\n42403\n"

    with pytest.raises(BrokerError, match="multiple servers") as error:
        mps_guard.authorized_server_pids(1, EXPECTED_GPU_UUIDS[1])

    assert error.value.code == "mps_control_unavailable"


@pytest.mark.parametrize(
    "fault",
    (
        "pid_file",
        "pid_file_mode",
        "control_executable",
        "server_executable",
        "control_credentials",
        "server_credentials",
        "cgroup_mismatch",
        "root_cgroup",
        "visible_device",
        "environment_pipe",
        "pipe_mode",
        "control_mode",
        "start_ticks",
        "client_server",
        "client_device",
    ),
)
def test_mps_server_authority_fails_closed_on_identity_mismatch(
    tmp_path: Path,
    fault: str,
) -> None:
    mps_guard, state = _exact_mps_authority(tmp_path)
    control_pid = state["control_pid"]
    server_pid = state["server_pid"]
    gpu_uuid = state["gpu_uuid"]
    pid_file = state["pid_file"]
    pipe_directory = state["pipe_directory"]
    status_paths = state["status_paths"]
    executable_paths = state["executable_paths"]
    environments = state["environments"]
    cgroups = state["cgroups"]
    assert isinstance(control_pid, int)
    assert isinstance(server_pid, int)
    assert isinstance(gpu_uuid, str)
    assert isinstance(pid_file, Path)
    assert isinstance(pipe_directory, Path)
    assert isinstance(status_paths, dict)
    assert isinstance(executable_paths, dict)
    assert isinstance(environments, dict)
    assert isinstance(cgroups, dict)

    if fault == "pid_file":
        pid_file.write_text("0\n", encoding="ascii")
    elif fault == "pid_file_mode":
        pid_file.chmod(0o622)
    elif fault == "control_executable":
        executable_paths[control_pid].unlink()
        executable_paths[control_pid].symlink_to("/bin/false")
    elif fault == "server_executable":
        executable_paths[server_pid].unlink()
        executable_paths[server_pid].symlink_to("/bin/false")
    elif fault == "control_credentials":
        status_paths[control_pid].write_text(
            "Uid:\t1001\t1001\t1001\t1001\n"
            "Gid:\t1001\t1001\t1001\t1002\n",
            encoding="ascii",
        )
    elif fault == "server_credentials":
        status_paths[server_pid].write_text(
            "Uid:\t1001\t1001\t1001\t0\n"
            "Gid:\t1001\t1001\t1001\t1001\n",
            encoding="ascii",
        )
    elif fault == "cgroup_mismatch":
        cgroups[server_pid] = "0::/user.slice/foreign.scope"
    elif fault == "root_cgroup":
        cgroups[control_pid] = "0::/"
        cgroups[server_pid] = "0::/"
    elif fault == "visible_device":
        environments[control_pid]["CUDA_VISIBLE_DEVICES"] = EXPECTED_GPU_UUIDS[3]
    elif fault == "environment_pipe":
        foreign_pipe = tmp_path / "foreign-pipe"
        foreign_pipe.mkdir()
        environments[control_pid]["CUDA_MPS_PIPE_DIRECTORY"] = str(foreign_pipe)
    elif fault == "pipe_mode":
        pipe_directory.chmod(0o755)
    elif fault == "control_mode":
        (pipe_directory / "control").chmod(0o622)
    elif fault == "start_ticks":
        state["unstable_pid"] = server_pid
    elif fault == "client_server":
        state["client_inventory"] = (
            "PID ID SERVER DEVICE NAMESPACE COMMAND\n"
            f"51001 0 {server_pid + 1} {gpu_uuid} 4026531836 client\n"
        )
    elif fault == "client_device":
        state["client_inventory"] = (
            "PID ID SERVER DEVICE NAMESPACE COMMAND\n"
            f"51001 0 {server_pid} {EXPECTED_GPU_UUIDS[3]} "
            "4026531836 client\n"
        )
    else:
        raise AssertionError(f"unhandled fault: {fault}")

    with pytest.raises(BrokerError) as error:
        mps_guard.authorized_server_pids(1, gpu_uuid)

    assert error.value.code == "mps_control_unavailable"


def test_mps_allocation_audit_rejects_unowned_and_wrong_device_clients(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pipe_directory = tmp_path / "mps-1" / "pipe"
    pipe_directory.mkdir(parents=True)
    pipe_directory.chmod(0o700)
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
        started_at=_DOCKER_STARTED_AT,
        restart_count=0,
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
                "State": {
                    "Running": True,
                    "Pid": os.getpid(),
                    "StartedAt": _DOCKER_STARTED_AT,
                },
                "RestartCount": 0,
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
    assert len(calls) == 5


def test_docker_inventory_rejects_container_added_during_list_cas() -> None:
    first = "a" * 64
    second = "b" * 64
    list_calls = 0

    def run(command, **_kwargs):
        nonlocal list_calls
        if command[2] == "ls":
            list_calls += 1
            stdout = first + "\n" if list_calls == 1 else first + "\n" + second + "\n"
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
        payload = [
            {
                "Id": first,
                "State": {
                    "Running": True,
                    "Pid": 101,
                    "StartedAt": _DOCKER_STARTED_AT,
                },
                "RestartCount": 0,
                "Config": {"Labels": {}, "Env": []},
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

    with pytest.raises(BrokerError, match="changed during audit") as error:
        query_docker_gpu_claims(run=run)

    assert error.value.code == "gpu_claim_inventory_unavailable"


def test_docker_inventory_rejects_duplicate_inspect_identity() -> None:
    first = "a" * 64
    second = "b" * 64

    def run(command, **_kwargs):
        if command[2] == "ls":
            return subprocess.CompletedProcess(
                command, 0, stdout=first + "\n" + second + "\n", stderr=""
            )
        record = {
            "Id": first,
            "State": {
                "Running": True,
                "Pid": 101,
                "StartedAt": _DOCKER_STARTED_AT,
            },
            "RestartCount": 0,
            "Config": {"Labels": {}, "Env": []},
            "HostConfig": {"DeviceRequests": []},
        }
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps([record, record]), stderr=""
        )

    with pytest.raises(BrokerError, match="one-to-one") as error:
        query_docker_gpu_claims(run=run)

    assert error.value.code == "gpu_claim_inventory_unavailable"


def test_docker_inventory_rejects_inspect_fingerprint_change() -> None:
    container_id = "a" * 64
    inspect_calls = 0

    def run(command, **_kwargs):
        nonlocal inspect_calls
        if command[2] == "ls":
            return subprocess.CompletedProcess(
                command, 0, stdout=container_id + "\n", stderr=""
            )
        inspect_calls += 1
        payload = [
            {
                "Id": container_id,
                "State": {
                    "Running": True,
                    "Pid": 100 + inspect_calls,
                    "StartedAt": _DOCKER_STARTED_AT,
                },
                "RestartCount": 0,
                "Config": {"Labels": {}, "Env": []},
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

    with pytest.raises(BrokerError, match="fingerprint changed") as error:
        query_docker_gpu_claims(run=run)

    assert error.value.code == "gpu_claim_inventory_unavailable"


@pytest.mark.parametrize("changed_field", ("started_at", "restart_count"))
def test_docker_inventory_rejects_same_pid_restart_identity_change(
    changed_field: str,
) -> None:
    container_id = "a" * 64
    inspect_calls = 0

    def run(command, **_kwargs):
        nonlocal inspect_calls
        if command[2] == "ls":
            return subprocess.CompletedProcess(
                command, 0, stdout=container_id + "\n", stderr=""
            )
        inspect_calls += 1
        started_at = _DOCKER_STARTED_AT
        restart_count = 0
        if inspect_calls == 2 and changed_field == "started_at":
            started_at = "2026-07-19T06:00:01.123456789Z"
        if inspect_calls == 2 and changed_field == "restart_count":
            restart_count = 1
        payload = [
            {
                "Id": container_id,
                "State": {
                    "Running": True,
                    "Pid": 4242,
                    "StartedAt": started_at,
                },
                "RestartCount": restart_count,
                "Config": {"Labels": {}, "Env": []},
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

    with pytest.raises(BrokerError, match="fingerprint changed") as error:
        query_docker_gpu_claims(run=run)

    assert error.value.code == "gpu_claim_inventory_unavailable"


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
        started_at=_DOCKER_STARTED_AT,
        restart_count=0,
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
        started_at=_DOCKER_STARTED_AT,
        restart_count=0,
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
        scope="user",
        unit="nexpoly-monomer-md-worker.service",
        main_pid=os.getpid(),
        control_group="/user.slice/nexpoly-monomer-md-worker.service",
        process_pids=frozenset({os.getpid()}),
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
            {f"{claim.scope}:{claim.unit}": claim.gpu_uuids},
        ),
        process_query=lambda: {},
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: (claim,),
        cache_seconds=0,
    )

    assert unknown(2, gpu2, (), _owner(), "md", "prod") is True
    assert managed(2, gpu2, (), _owner(), "md", "prod") is False

    unbound = replace(
        claim,
        main_pid=999_999_999,
        process_pids=frozenset({999_999_999}),
    )
    unbound_guard = ExternalGpuGuard(
        managed.policy,
        process_query=lambda: {},
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: (unbound,),
        cache_seconds=0,
    )
    assert unbound_guard(2, gpu2, (), _owner(), "md", "prod") is True


def test_md_and_parented_dft_admissions_accept_live_dft_residency_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    claims: list[SystemdGpuClaim] = []
    processes: dict[str, frozenset[int]] = {}
    mps_state = {"unmanaged": False}
    guard = ExternalGpuGuard(
        ExternalReservationPolicy(
            frozenset({EXPECTED_GPU_UUIDS[3]}),
            {},
            {},
        ),
        process_query=lambda: processes,
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: tuple(claims),
        unmanaged_mps_client_query=lambda *_args: mps_state["unmanaged"],
        cache_seconds=0,
    )
    broker = HostGpuBroker(
        tmp_path / "state.json",
        gpu_runtime_healthy=lambda _index, _uuid: True,
        gpu_externally_busy=guard,
    )
    residency = _acquire(
        broker,
        component="dft",
        environment="dev",
        kind="residency",
    )
    owner = _owner()
    workload_pid = owner.pid
    compiler_pid = 999_999_998
    compiler_start_ticks = owner.process_start_ticks + 1
    control_group = scope_control_group(residency.lease_id, uid=1001)
    residency.status = "active"
    residency.workload_pid = workload_pid
    residency.workload_process_start_ticks = owner.process_start_ticks
    residency.workload_process_group_id = workload_pid
    residency.workload_cgroup = f"0::{control_group}"
    claims.append(
        SystemdGpuClaim(
            scope="system",
            unit="user@1001.service",
            main_pid=workload_pid,
            control_group=user_manager_control_group(1001),
            process_pids=frozenset({workload_pid, compiler_pid}),
            gpu_uuids=frozenset({residency.gpu_uuid}),
            active_gpu_uuids=frozenset({residency.gpu_uuid}),
            live_gpu_declarers=(
                SystemdGpuDeclarer(
                    pid=workload_pid,
                    process_start_ticks=owner.process_start_ticks,
                    process_cgroup=control_group,
                    gpu_uuids=frozenset({residency.gpu_uuid}),
                ),
                SystemdGpuDeclarer(
                    pid=compiler_pid,
                    process_start_ticks=compiler_start_ticks,
                    process_cgroup=control_group,
                    gpu_uuids=frozenset({residency.gpu_uuid}),
                ),
            ),
        )
    )
    processes[residency.gpu_uuid] = frozenset({workload_pid})
    monkeypatch.setattr(
        "ops.gpu_broker.server._read_unified_process_cgroup",
        lambda pid: (
            control_group
            if pid in {workload_pid, compiler_pid}
            else "/foreign"
        ),
    )
    monkeypatch.setattr(
        "ops.gpu_broker.server.read_process_start_ticks",
        lambda pid: {
            workload_pid: owner.process_start_ticks,
            compiler_pid: compiler_start_ticks,
        }[pid],
    )
    monkeypatch.setattr(
        "ops.gpu_broker.server._pid_is_or_descends_from",
        lambda pid, ancestor: (
            pid == ancestor
            or (pid == compiler_pid and ancestor == workload_pid)
        ),
    )
    assert claim_is_exact_dft_residency_scope(
        claims[0],
        index=residency.gpu_index,
        uuid=residency.gpu_uuid,
        lease=residency,
    ) is True
    md_execution = _acquire(
        broker,
        component="md",
        environment="dev",
        kind="execution",
    )
    assert md_execution.gpu_index == residency.gpu_index
    broker.release(
        md_execution.lease_id,
        md_execution.fencing_token,
        owner=owner,
    )
    foreign_pid = 999_999_997
    processes[residency.gpu_uuid] = frozenset(
        {workload_pid, foreign_pid}
    )
    with pytest.raises(BrokerError) as foreign_error:
        _acquire(
            broker,
            component="md",
            environment="dev",
            kind="execution",
        )
    assert foreign_error.value.code == "gpu_capacity_unavailable"
    processes[residency.gpu_uuid] = frozenset({workload_pid})
    mps_state["unmanaged"] = True
    with pytest.raises(BrokerError) as mps_error:
        _acquire(
            broker,
            component="md",
            environment="dev",
            kind="execution",
        )
    assert mps_error.value.code == "gpu_capacity_unavailable"
    mps_state["unmanaged"] = False
    execution = _acquire(
        broker,
        component="dft",
        environment="dev",
        kind="execution",
        parent_lease_id=residency.lease_id,
        client_id=residency.client_id,
    )

    assert execution.parent_lease_id == residency.lease_id
    assert execution.gpu_index == residency.gpu_index
    status = broker.status()
    assert status["usage_mib"][str(residency.gpu_index)] == residency.memory_mib
    assert status["waiters"] == 0


@pytest.mark.parametrize(
    "mismatch",
    (
        "pid",
        "start_ticks",
        "cgroup",
        "gpu_uuid",
        "unrelated_declarer",
        "static_declaration",
        "duplicate_residency",
        "lease_cgroup",
        "lease_kind",
        "lease_mps_terminated",
        "lease_pgid",
        "lease_placement",
        "lease_preferred",
        "lease_status",
    ),
)
def test_md_admission_keeps_mismatched_dft_residency_claim_external(
    tmp_path: Path,
    monkeypatch,
    mismatch: str,
) -> None:
    broker = HostGpuBroker(tmp_path / "state.json")
    lease = _acquire(
        broker,
        component="dft",
        environment="dev",
        kind="residency",
    )
    owner = _owner()
    workload_pid = owner.pid
    control_group = scope_control_group(lease.lease_id, uid=1001)
    lease.status = "active"
    lease.workload_pid = workload_pid
    lease.workload_process_start_ticks = owner.process_start_ticks
    lease.workload_process_group_id = workload_pid
    lease.workload_cgroup = f"0::{control_group}"
    declarer = SystemdGpuDeclarer(
        pid=workload_pid,
        process_start_ticks=owner.process_start_ticks,
        process_cgroup=control_group,
        gpu_uuids=frozenset({lease.gpu_uuid}),
    )
    if mismatch == "pid":
        declarer = replace(declarer, pid=999_999_999)
    elif mismatch == "start_ticks":
        declarer = replace(declarer, process_start_ticks=1)
    elif mismatch == "cgroup":
        declarer = replace(declarer, process_cgroup="/foreign.scope")
    elif mismatch == "gpu_uuid":
        declarer = replace(
            declarer,
            gpu_uuids=frozenset({EXPECTED_GPU_UUIDS[3]}),
        )
    declarers = (declarer,)
    process_pids = {workload_pid}
    unrelated_pid = 999_999_999
    unrelated_start_ticks = owner.process_start_ticks + 1
    if mismatch == "unrelated_declarer":
        declarers += (
            replace(
                declarer,
                pid=unrelated_pid,
                process_start_ticks=unrelated_start_ticks,
            ),
        )
        process_pids.add(unrelated_pid)
    claim = SystemdGpuClaim(
        scope="system",
        unit="user@1001.service",
        main_pid=workload_pid,
        control_group=user_manager_control_group(1001),
        process_pids=frozenset(process_pids),
        gpu_uuids=frozenset({lease.gpu_uuid}),
        static_gpu_uuids=(
            frozenset({lease.gpu_uuid})
            if mismatch == "static_declaration"
            else frozenset()
        ),
        active_gpu_uuids=frozenset({lease.gpu_uuid}),
        live_gpu_declarers=declarers,
    )
    if mismatch == "lease_cgroup":
        lease.workload_cgroup = "0::/foreign.scope"
    elif mismatch == "lease_kind":
        lease.kind = "execution"
    elif mismatch == "lease_mps_terminated":
        lease.mps_termination_status = "safe"
    elif mismatch == "lease_pgid":
        lease.workload_process_group_id = workload_pid + 1
    elif mismatch == "lease_placement":
        lease.placement = "overflow"
    elif mismatch == "lease_preferred":
        lease.preferred = False
    elif mismatch == "lease_status":
        lease.status = "suspect"
    monkeypatch.setattr(
        "ops.gpu_broker.server._read_unified_process_cgroup",
        lambda pid: (
            control_group
            if pid in process_pids
            else "/foreign"
        ),
    )
    monkeypatch.setattr(
        "ops.gpu_broker.server.read_process_start_ticks",
        lambda pid: {
            workload_pid: owner.process_start_ticks,
            unrelated_pid: unrelated_start_ticks,
        }.get(pid, 0),
    )
    monkeypatch.setattr(
        "ops.gpu_broker.server._pid_is_or_descends_from",
        lambda pid, ancestor: pid == ancestor,
    )
    guard = ExternalGpuGuard(
        ExternalReservationPolicy(frozenset(), {}, {}),
        process_query=lambda: {
            lease.gpu_uuid: frozenset({workload_pid}),
        },
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: (claim,),
        unmanaged_mps_client_query=lambda *_args: False,
        cache_seconds=0,
    )
    leases = (lease,)
    if mismatch == "duplicate_residency":
        duplicate_id = "e" * 32
        leases = (
            lease,
            replace(
                lease,
                lease_id=duplicate_id,
                workload_cgroup=(
                    f"0::{scope_control_group(duplicate_id, uid=1001)}"
                ),
            ),
        )

    assert (
        guard(
            lease.gpu_index,
            lease.gpu_uuid,
            leases,
            owner,
            "md",
            "dev",
            client_id="md-dev",
        )
        is True
    )


def test_systemd_claim_inventory_reads_active_unit_environment_in_batches() -> None:
    gpu2 = "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe"
    control_group = "/user.slice/user-1001.slice/nexpoly-backend.service"
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        if "list-units" in command and "--user" in command:
            stdout = "nexpoly-backend.service loaded active running Backend\n"
        elif "show" in command and "--user" in command:
            stdout = (
                "Id=nexpoly-backend.service\n"
                "ActiveState=active\n"
                "SubState=running\n"
                f"InvocationID={'1' * 32}\n"
                "MainPID=1234\n"
                f"ControlGroup={control_group}\n"
                f'Environment="CUDA_VISIBLE_DEVICES={gpu2}"\n'
                "EnvironmentFiles=\n"
            )
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    claims = query_systemd_gpu_claims(
        run=run,
        read_process_environment=lambda _pid: {"CUDA_VISIBLE_DEVICES": gpu2},
        read_control_group_processes=lambda path: (
            frozenset({1234}) if path == control_group else frozenset()
        ),
        read_process_cgroup=lambda _pid: control_group,
        read_process_uids=lambda _pid: (1001, 1001, 1001, 1001),
        read_process_start_ticks=_stable_systemd_ticks,
    )

    assert claims == (
        SystemdGpuClaim(
            scope="user",
            unit="nexpoly-backend.service",
            main_pid=1234,
            control_group=control_group,
            process_pids=frozenset({1234}),
            gpu_uuids=frozenset({gpu2}),
            static_gpu_uuids=frozenset({gpu2}),
            live_gpu_declarers=(
                SystemdGpuDeclarer(
                    pid=1234,
                    process_start_ticks=_stable_systemd_ticks(1234),
                    process_cgroup=control_group,
                    gpu_uuids=frozenset({gpu2}),
                ),
            ),
        ),
    )
    assert len(calls) == 6


def _systemd_declaration_runner(declaration: str):
    authority_defaults = {
        "ActiveState": "active",
        "SubState": "running",
        "InvocationID": "1" * 32,
    }
    declared_names = {
        line.split("=", 1)[0]
        for line in declaration.splitlines()
        if "=" in line
    }
    declaration = (
        declaration.rstrip()
        + "\n"
        + "".join(
            f"{name}={value}\n"
            for name, value in authority_defaults.items()
            if name not in declared_names
        )
    )

    def run(command, **_kwargs):
        if "list-units" in command and "--user" in command:
            stdout = "nexpoly-worker.service loaded active running Worker\n"
        elif "show" in command and "--user" in command:
            stdout = declaration
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    return run


class _ScopedSystemdRunner:
    def __init__(
        self,
        declarations: dict[str, dict[str, str]],
        *,
        manager_environments: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.declarations = declarations
        self.manager_environments = manager_environments or {}
        self.calls: list[list[str]] = []

    @staticmethod
    def _scope(command: list[str]) -> str:
        return "user" if "--user" in command else "system"

    def __call__(self, command, **_kwargs):
        self.calls.append(command)
        scope = self._scope(command)
        if "list-units" in command:
            stdout = "".join(
                f"{unit} loaded active running Test service\n"
                for unit in sorted(self.declarations.get(scope, {}))
            )
        elif "show-environment" in command:
            stdout = "".join(
                f"{name}={value}\n"
                for name, value in sorted(
                    self.manager_environments.get(scope, {}).items()
                )
            )
        elif "show" in command:
            stdout = "\n\n".join(
                self.declarations[scope][unit].rstrip()
                for unit in sorted(self.declarations.get(scope, {}))
            )
            if stdout:
                stdout += "\n"
        else:
            raise AssertionError(f"unexpected systemctl command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


def _systemd_properties(
    unit: str,
    *,
    main_pid: int,
    control_group: str,
    active_state: str = "active",
    sub_state: str = "running",
    invocation_id: str = "1" * 32,
    user: str = "",
    environment: str = "",
    environment_files: tuple[str, ...] = (),
    pass_environment: str = "",
    unset_environment: str = "",
) -> str:
    lines = [
        f"Id={unit}",
        f"ActiveState={active_state}",
        f"SubState={sub_state}",
        f"InvocationID={invocation_id}",
        f"MainPID={main_pid}",
        f"ControlGroup={control_group}",
        f"User={user}",
        f"Environment={environment}",
        f"PassEnvironment={pass_environment}",
        f"UnsetEnvironment={unset_environment}",
    ]
    lines.extend(f"EnvironmentFiles={value}" for value in environment_files)
    return "\n".join(lines) + "\n"


def _stable_systemd_ticks(pid: int) -> int:
    return 100_000 + pid


def test_systemd_inventory_includes_activating_units() -> None:
    unit = "nexpoly-starting-worker.service"
    control_group = f"/user.slice/user-1001.slice/{unit}"
    pid = 3001
    gpu1 = EXPECTED_GPU_UUIDS[1]

    def run(command, **_kwargs):
        if "list-units" in command and "--user" in command:
            assert "--state=active,activating,reloading,deactivating" in command
            stdout = f"{unit} loaded activating start Test service\n"
        elif "show" in command and "--user" in command:
            stdout = _systemd_properties(
                unit,
                active_state="activating",
                sub_state="start",
                main_pid=pid,
                control_group=control_group,
                environment=f"CUDA_VISIBLE_DEVICES={gpu1}",
            )
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    claims = query_systemd_gpu_claims(
        run=run,
        read_process_environment=lambda _pid: {"CUDA_VISIBLE_DEVICES": gpu1},
        read_control_group_processes=lambda path: (
            frozenset({pid}) if path == control_group else frozenset()
        ),
        read_process_cgroup=lambda _pid: control_group,
        read_process_uids=lambda _pid: (1001, 1001, 1001, 1001),
        read_process_start_ticks=_stable_systemd_ticks,
    )

    assert claims[0].unit == unit
    assert claims[0].gpu_uuids == frozenset({gpu1})


def test_systemd_inventory_rejects_cgroup_member_forked_during_audit() -> None:
    unit = "nexpoly-forking-worker.service"
    control_group = f"/user.slice/user-1001.slice/{unit}"
    main_pid = 3011
    child_pid = 3012
    gpu1 = EXPECTED_GPU_UUIDS[1]
    runner = _ScopedSystemdRunner(
        {
            "user": {
                unit: _systemd_properties(
                    unit,
                    main_pid=main_pid,
                    control_group=control_group,
                    environment=f"CUDA_VISIBLE_DEVICES={gpu1}",
                )
            }
        }
    )
    membership_reads = 0

    def members(path: str) -> frozenset[int]:
        nonlocal membership_reads
        assert path == control_group
        membership_reads += 1
        return (
            frozenset({main_pid})
            if membership_reads == 1
            else frozenset({main_pid, child_pid})
        )

    with pytest.raises(BrokerError, match="membership changed") as error:
        query_systemd_gpu_claims(
            run=runner,
            read_process_environment=lambda _pid: {
                "CUDA_VISIBLE_DEVICES": gpu1
            },
            read_control_group_processes=members,
            read_process_cgroup=lambda _pid: control_group,
            read_process_uids=lambda _pid: (1001, 1001, 1001, 1001),
            read_process_start_ticks=_stable_systemd_ticks,
        )

    assert membership_reads == 2
    assert error.value.code == "gpu_claim_inventory_changed"


def test_systemd_inventory_rejects_invocation_change_during_audit() -> None:
    unit = "nexpoly-restarting-worker.service"
    control_group = f"/user.slice/user-1001.slice/{unit}"
    pid = 3021
    gpu1 = EXPECTED_GPU_UUIDS[1]
    show_reads = 0

    def run(command, **_kwargs):
        nonlocal show_reads
        if "list-units" in command and "--user" in command:
            stdout = f"{unit} loaded active running Test service\n"
        elif "show" in command and "--user" in command:
            show_reads += 1
            stdout = _systemd_properties(
                unit,
                main_pid=pid,
                control_group=control_group,
                invocation_id=("1" if show_reads == 1 else "2") * 32,
                environment=f"CUDA_VISIBLE_DEVICES={gpu1}",
            )
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    with pytest.raises(BrokerError, match="authority changed") as error:
        query_systemd_gpu_claims(
            run=run,
            read_process_environment=lambda _pid: {
                "CUDA_VISIBLE_DEVICES": gpu1
            },
            read_control_group_processes=lambda path: (
                frozenset({pid}) if path == control_group else frozenset()
            ),
            read_process_cgroup=lambda _pid: control_group,
            read_process_uids=lambda _pid: (1001, 1001, 1001, 1001),
            read_process_start_ticks=_stable_systemd_ticks,
        )

    assert show_reads == 2
    assert error.value.code == "gpu_claim_inventory_unavailable"


def test_systemd_process_snapshot_excludes_pid_reused_during_identity_read(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    pid = 1234
    (proc_root / str(pid)).mkdir(parents=True)
    ticks = iter((101, 202))

    assert (
        _snapshot_systemd_process_cgroups(
            proc_root=proc_root,
            read_process_cgroup=lambda _pid: "/user.slice/test.service",
            read_process_start_ticks=lambda _pid: next(ticks),
        )
        == ()
    )


def test_systemd_inventory_skips_unmarked_unreadable_root_service() -> None:
    unit = "modem-manager.service"
    control_group = f"/system.slice/{unit}"
    runner = _ScopedSystemdRunner(
        {
            "system": {
                unit: _systemd_properties(
                    unit,
                    main_pid=2647,
                    control_group=control_group,
                )
            }
        }
    )
    environment_reads: list[int] = []

    def unreadable(pid: int) -> dict[str, str]:
        environment_reads.append(pid)
        raise PermissionError("cross-UID /proc access denied")

    claims = query_systemd_gpu_claims(
        run=runner,
        read_process_environment=unreadable,
        read_control_group_processes=lambda path: (
            frozenset({2647}) if path == control_group else frozenset()
        ),
        compute_process_query=lambda: {},
        read_process_cgroup=lambda _pid: control_group,
        read_process_uids=lambda _pid: (0, 0, 0, 0),
        read_process_start_ticks=_stable_systemd_ticks,
    )

    assert claims == ()
    assert environment_reads == []


@pytest.mark.parametrize("fault", ("start_ticks", "cgroup"))
def test_systemd_inventory_rejects_process_identity_change_while_reading_env(
    fault: str,
) -> None:
    unit = "nexpoly-worker.service"
    control_group = f"/user.slice/user-1001.slice/{unit}"
    pid = 3101
    runner = _ScopedSystemdRunner(
        {
            "user": {
                unit: _systemd_properties(
                    unit,
                    main_pid=pid,
                    control_group=control_group,
                    environment=f"CUDA_VISIBLE_DEVICES={EXPECTED_GPU_UUIDS[1]}",
                )
            }
        }
    )
    reads = {"ticks": 0, "cgroup": 0}

    def read_ticks(_pid: int) -> int:
        reads["ticks"] += 1
        if fault == "start_ticks" and reads["ticks"] > 6:
            return 202
        return 101

    def read_cgroup(_pid: int) -> str:
        reads["cgroup"] += 1
        if fault == "cgroup" and reads["cgroup"] > 3:
            return "/user.slice/foreign.service"
        return control_group

    with pytest.raises(BrokerError, match="identity changed") as error:
        query_systemd_gpu_claims(
            run=runner,
            read_process_environment=lambda _pid: {
                "CUDA_VISIBLE_DEVICES": EXPECTED_GPU_UUIDS[1]
            },
            read_control_group_processes=lambda path: (
                frozenset({pid}) if path == control_group else frozenset()
            ),
            compute_process_query=lambda: {},
            read_process_cgroup=read_cgroup,
            read_process_uids=lambda _pid: (1001, 1001, 1001, 1001),
            read_process_start_ticks=read_ticks,
        )

    assert error.value.code == "gpu_claim_inventory_unavailable"


def test_systemd_inventory_rechecks_user_identity_after_system_scope_query() -> None:
    unit = "nexpoly-worker.service"
    control_group = f"/user.slice/user-1001.slice/{unit}"
    pid = 3151
    scoped_runner = _ScopedSystemdRunner(
        {
            "user": {
                unit: _systemd_properties(
                    unit,
                    main_pid=pid,
                    control_group=control_group,
                    environment=f"CUDA_VISIBLE_DEVICES={EXPECTED_GPU_UUIDS[1]}",
                )
            }
        }
    )
    system_scope_started = False

    def run(command, **kwargs):
        nonlocal system_scope_started
        completed = scoped_runner(command, **kwargs)
        if "list-units" in command and "--user" not in command:
            system_scope_started = True
        return completed

    with pytest.raises(BrokerError, match="changed during audit") as error:
        query_systemd_gpu_claims(
            run=run,
            read_process_environment=lambda _pid: {
                "CUDA_VISIBLE_DEVICES": EXPECTED_GPU_UUIDS[1]
            },
            read_control_group_processes=lambda path: (
                frozenset({pid}) if path == control_group else frozenset()
            ),
            compute_process_query=lambda: {},
            read_process_cgroup=lambda _pid: control_group,
            read_process_uids=lambda _pid: (1001, 1001, 1001, 1001),
            read_process_start_ticks=lambda _pid: (
                202 if system_scope_started else 101
            ),
        )

    assert error.value.code == "gpu_claim_inventory_changed"


def test_systemd_inventory_rejects_main_pid_outside_control_group() -> None:
    unit = "nexpoly-worker.service"
    control_group = f"/user.slice/user-1001.slice/{unit}"
    main_pid = 3201
    child_pid = 3202
    runner = _ScopedSystemdRunner(
        {
            "user": {
                unit: _systemd_properties(
                    unit,
                    main_pid=main_pid,
                    control_group=control_group,
                    environment=f"CUDA_VISIBLE_DEVICES={EXPECTED_GPU_UUIDS[1]}",
                )
            }
        }
    )

    with pytest.raises(BrokerError, match="MainPID is outside") as error:
        query_systemd_gpu_claims(
            run=runner,
            read_process_environment=lambda _pid: {},
            read_control_group_processes=lambda path: (
                frozenset({child_pid}) if path == control_group else frozenset()
            ),
            compute_process_query=lambda: {},
            read_process_cgroup=lambda _pid: control_group,
            read_process_uids=lambda _pid: (1001, 1001, 1001, 1001),
            read_process_start_ticks=_stable_systemd_ticks,
        )

    assert error.value.code == "gpu_claim_inventory_unavailable"


def test_systemd_inventory_rejects_positive_main_pid_without_control_group() -> None:
    unit = "nexpoly-worker.service"
    runner = _ScopedSystemdRunner(
        {
            "user": {
                unit: _systemd_properties(
                    unit,
                    main_pid=3301,
                    control_group="",
                    environment=f"CUDA_VISIBLE_DEVICES={EXPECTED_GPU_UUIDS[1]}",
                )
            }
        }
    )

    with pytest.raises(BrokerError, match="MainPID is outside") as error:
        query_systemd_gpu_claims(
            run=runner,
            read_process_environment=lambda _pid: {},
            read_control_group_processes=lambda _path: frozenset(),
            compute_process_query=lambda: {},
            read_process_cgroup=lambda _pid: "/user.slice/foreign.service",
            read_process_uids=lambda _pid: (1001, 1001, 1001, 1001),
            read_process_start_ticks=_stable_systemd_ticks,
        )

    assert error.value.code == "gpu_claim_inventory_unavailable"


def test_systemd_inventory_rejects_nvidia_pid_reuse_during_cgroup_binding() -> None:
    unit = "root-gpu-worker.service"
    control_group = f"/system.slice/{unit}"
    pid = 3401
    runner = _ScopedSystemdRunner(
        {
            "system": {
                unit: _systemd_properties(
                    unit,
                    main_pid=pid,
                    control_group=control_group,
                )
            }
        }
    )
    reads = 0

    def read_ticks(_pid: int) -> int:
        nonlocal reads
        reads += 1
        return 101 if reads <= 2 else 202

    with pytest.raises(BrokerError, match="NVIDIA PID identity changed") as error:
        query_systemd_gpu_claims(
            run=runner,
            read_process_environment=lambda _pid: {},
            read_control_group_processes=lambda path: (
                frozenset({pid}) if path == control_group else frozenset()
            ),
            compute_process_query=lambda: {
                EXPECTED_GPU_UUIDS[1]: frozenset({pid})
            },
            read_process_cgroup=lambda _pid: control_group,
            read_process_uids=lambda _pid: (0, 0, 0, 0),
            read_process_start_ticks=read_ticks,
        )

    assert error.value.code == "gpu_claim_inventory_unavailable"


def test_systemd_inventory_keeps_same_named_user_and_system_units_distinct() -> None:
    unit = "nexpoly-worker.service"
    gpu1 = EXPECTED_GPU_UUIDS[1]
    gpu3 = EXPECTED_GPU_UUIDS[3]
    user_cgroup = f"/user.slice/user-1001.slice/{unit}"
    system_cgroup = f"/system.slice/{unit}"
    runner = _ScopedSystemdRunner(
        {
            "user": {
                unit: _systemd_properties(
                    unit,
                    main_pid=1101,
                    control_group=user_cgroup,
                    environment=f"CUDA_VISIBLE_DEVICES={gpu1}",
                )
            },
            "system": {
                unit: _systemd_properties(
                    unit,
                    main_pid=2101,
                    control_group=system_cgroup,
                    environment=f"CUDA_VISIBLE_DEVICES={gpu3}",
                )
            },
        }
    )
    process_sets = {
        user_cgroup: frozenset({1101, 1102}),
        system_cgroup: frozenset({2101}),
    }
    live_environments = {
        1101: {"CUDA_VISIBLE_DEVICES": gpu1},
        1102: {"CUDA_VISIBLE_DEVICES": gpu1},
        2101: {"CUDA_VISIBLE_DEVICES": gpu3},
    }

    claims = query_systemd_gpu_claims(
        run=runner,
        read_process_environment=lambda pid: live_environments[pid],
        read_control_group_processes=lambda path: process_sets[path],
        compute_process_query=lambda: {},
        read_process_cgroup=lambda pid: (
            user_cgroup if pid in process_sets[user_cgroup] else system_cgroup
        ),
        read_process_uids=lambda _pid: (1001, 1001, 1001, 1001),
        read_process_start_ticks=_stable_systemd_ticks,
    )

    assert claims == (
        SystemdGpuClaim(
            scope="system",
            unit=unit,
            main_pid=2101,
            control_group=system_cgroup,
            process_pids=frozenset({2101}),
            gpu_uuids=frozenset({gpu3}),
            static_gpu_uuids=frozenset({gpu3}),
            live_gpu_declarers=(
                SystemdGpuDeclarer(
                    pid=2101,
                    process_start_ticks=_stable_systemd_ticks(2101),
                    process_cgroup=system_cgroup,
                    gpu_uuids=frozenset({gpu3}),
                ),
            ),
        ),
        SystemdGpuClaim(
            scope="user",
            unit=unit,
            main_pid=1101,
            control_group=user_cgroup,
            process_pids=frozenset({1101, 1102}),
            gpu_uuids=frozenset({gpu1}),
            static_gpu_uuids=frozenset({gpu1}),
            live_gpu_declarers=(
                SystemdGpuDeclarer(
                    pid=1101,
                    process_start_ticks=_stable_systemd_ticks(1101),
                    process_cgroup=user_cgroup,
                    gpu_uuids=frozenset({gpu1}),
                ),
                SystemdGpuDeclarer(
                    pid=1102,
                    process_start_ticks=_stable_systemd_ticks(1102),
                    process_cgroup=user_cgroup,
                    gpu_uuids=frozenset({gpu1}),
                ),
            ),
        ),
    )
    assert {
        f"{claim.scope}:{claim.unit}" for claim in claims
    } == {
        f"system:{unit}",
        f"user:{unit}",
    }


def test_systemd_mps_inventory_with_main_pid_zero_flows_into_guard() -> None:
    unit = "nexpoly-gpu-mps@1.service"
    gpu1 = EXPECTED_GPU_UUIDS[1]
    control_group = f"/system.slice/{unit}"
    server_pid = 4101
    template_environment = (
        "NEXPOLY_GPU_STATE_ROOT=/data/lzq/gith/nexpoly-runtime/state/gpu-resource "
        "NEXPOLY_GPU_EXTERNAL_RESERVATIONS="
        "/data/lzq/gith/nexpoly-runtime/state/gpu-resource/"
        "external-reservations.json "
        "XDG_RUNTIME_DIR=/run/user/1001"
    )
    runner = _ScopedSystemdRunner(
        {
            "system": {
                unit: _systemd_properties(
                    unit,
                    main_pid=0,
                    control_group=control_group,
                    user="1001",
                    environment=template_environment,
                )
            }
        }
    )

    claims = query_systemd_gpu_claims(
        run=runner,
        read_process_environment=lambda _pid: {},
        read_control_group_processes=lambda path: (
            frozenset({server_pid}) if path == control_group else frozenset()
        ),
        compute_process_query=lambda: {gpu1: frozenset({server_pid})},
        read_process_cgroup=lambda pid: (
            control_group if pid == server_pid else "/system.slice/other.service"
        ),
        read_process_uids=lambda _pid: (1001, 1001, 1001, 1001),
        read_process_start_ticks=_stable_systemd_ticks,
    )

    assert claims == (
        SystemdGpuClaim(
            scope="system",
            unit=unit,
            main_pid=0,
            control_group=control_group,
            process_pids=frozenset({server_pid}),
            gpu_uuids=frozenset({gpu1}),
            active_gpu_uuids=frozenset({gpu1}),
        ),
    )

    guard = ExternalGpuGuard(
        ExternalReservationPolicy(
            frozenset(),
            {},
            {f"system:{unit}": frozenset({gpu1})},
        ),
        process_query=lambda: {gpu1: frozenset({server_pid})},
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: claims,
        unmanaged_mps_client_query=lambda *_args: False,
        authorized_mps_server_pids=lambda index, uuid: (
            frozenset({server_pid})
            if (index, uuid) == (1, gpu1)
            else frozenset()
        ),
        cache_seconds=0,
    )
    assert guard(1, gpu1, (), _owner(), "backend", "dev") is False


@pytest.mark.parametrize(
    ("unset_environment", "expected_gpu_uuids"),
    [
        ("", frozenset({EXPECTED_GPU_UUIDS[1]})),
        ("CUDA_VISIBLE_DEVICES", frozenset()),
        (f"CUDA_VISIBLE_DEVICES={EXPECTED_GPU_UUIDS[1]}", frozenset()),
    ],
)
def test_systemd_inventory_applies_pass_then_unset_environment(
    unset_environment: str,
    expected_gpu_uuids: frozenset[str],
) -> None:
    unit = "nexpoly-pass-environment.service"
    control_group = f"/system.slice/{unit}"
    pid = 5101
    runner = _ScopedSystemdRunner(
        {
            "system": {
                unit: _systemd_properties(
                    unit,
                    main_pid=pid,
                    control_group=control_group,
                    pass_environment="CUDA_VISIBLE_DEVICES",
                    unset_environment=unset_environment,
                )
            }
        },
        manager_environments={
            "system": {"CUDA_VISIBLE_DEVICES": EXPECTED_GPU_UUIDS[1]}
        },
    )

    claims = query_systemd_gpu_claims(
        run=runner,
        read_process_environment=lambda _pid: (
            {}
            if unset_environment
            else {"CUDA_VISIBLE_DEVICES": EXPECTED_GPU_UUIDS[1]}
        ),
        read_control_group_processes=lambda path: (
            frozenset({pid}) if path == control_group else frozenset()
        ),
        compute_process_query=lambda: {},
        read_process_cgroup=lambda _pid: control_group,
        read_process_uids=lambda _pid: (1001, 1001, 1001, 1001),
        read_process_start_ticks=_stable_systemd_ticks,
    )

    if expected_gpu_uuids:
        assert claims == (
            SystemdGpuClaim(
                scope="system",
                unit=unit,
                main_pid=pid,
                    control_group=control_group,
                    process_pids=frozenset({pid}),
                    gpu_uuids=expected_gpu_uuids,
                    static_gpu_uuids=expected_gpu_uuids,
                    live_gpu_declarers=(
                        SystemdGpuDeclarer(
                            pid=pid,
                            process_start_ticks=_stable_systemd_ticks(pid),
                            process_cgroup=control_group,
                            gpu_uuids=expected_gpu_uuids,
                        ),
                    ),
                ),
            )
    else:
        assert claims == ()


def test_systemd_inventory_attributes_active_gpu_pid_to_its_exact_gpu() -> None:
    unit = "root-gpu-worker.service"
    gpu1 = EXPECTED_GPU_UUIDS[1]
    gpu3 = EXPECTED_GPU_UUIDS[3]
    control_group = f"/system.slice/{unit}"
    worker_pid = 6101
    unrelated_gpu_pid = 6102
    runner = _ScopedSystemdRunner(
        {
            "system": {
                unit: _systemd_properties(
                    unit,
                    main_pid=worker_pid,
                    control_group=control_group,
                )
            }
        }
    )

    claims = query_systemd_gpu_claims(
        run=runner,
        read_process_environment=lambda _pid: (_ for _ in ()).throw(
            PermissionError("root environment unreadable")
        ),
        read_control_group_processes=lambda path: (
            frozenset({worker_pid}) if path == control_group else frozenset()
        ),
        compute_process_query=lambda: {
            gpu1: frozenset({unrelated_gpu_pid}),
            gpu3: frozenset({worker_pid}),
        },
        read_process_cgroup=lambda pid: (
            control_group if pid == worker_pid else "/system.slice/other.service"
        ),
        read_process_uids=lambda _pid: (0, 0, 0, 0),
        read_process_start_ticks=_stable_systemd_ticks,
    )

    assert claims == (
        SystemdGpuClaim(
            scope="system",
            unit=unit,
            main_pid=worker_pid,
            control_group=control_group,
            process_pids=frozenset({worker_pid}),
            gpu_uuids=frozenset({gpu3}),
            active_gpu_uuids=frozenset({gpu3}),
        ),
    )


def test_systemd_active_pid_blocks_only_the_gpu_it_uses() -> None:
    gpu1 = EXPECTED_GPU_UUIDS[1]
    gpu3 = EXPECTED_GPU_UUIDS[3]
    worker_pid = 6101
    claim = SystemdGpuClaim(
        scope="system",
        unit="root-gpu-worker.service",
        main_pid=worker_pid,
        control_group="/system.slice/root-gpu-worker.service",
        process_pids=frozenset({worker_pid}),
        gpu_uuids=frozenset({gpu3}),
    )
    guard = ExternalGpuGuard(
        ExternalReservationPolicy(frozenset(), {}, {}),
        process_query=lambda: {gpu3: frozenset({worker_pid})},
        docker_claim_query=lambda: (),
        systemd_claim_query=lambda: (claim,),
        cache_seconds=0,
    )

    assert guard(1, gpu1, (), _owner(), "backend", "dev") is False
    assert guard(3, gpu3, (), _owner(), "backend", "dev") is True


def test_systemd_claim_inventory_accepts_omitted_empty_environment_files() -> None:
    gpu2 = "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe"
    control_group = "/user.slice/user-1001.slice/nexpoly-worker.service"
    declaration = (
        "MainPID=1234\n"
        f"ControlGroup={control_group}\n"
        f'Environment="CUDA_VISIBLE_DEVICES={gpu2}"\n'
        "Id=nexpoly-worker.service\n"
    )

    claims = query_systemd_gpu_claims(
        run=_systemd_declaration_runner(declaration),
        read_process_environment=lambda _pid: {"CUDA_VISIBLE_DEVICES": gpu2},
        read_control_group_processes=lambda path: (
            frozenset({1234}) if path == control_group else frozenset()
        ),
        read_process_cgroup=lambda _pid: control_group,
        read_process_uids=lambda _pid: (1001, 1001, 1001, 1001),
        read_process_start_ticks=_stable_systemd_ticks,
    )

    assert claims == (
        SystemdGpuClaim(
            scope="user",
            unit="nexpoly-worker.service",
            main_pid=1234,
            control_group=control_group,
            process_pids=frozenset({1234}),
            gpu_uuids=frozenset({gpu2}),
            static_gpu_uuids=frozenset({gpu2}),
            live_gpu_declarers=(
                SystemdGpuDeclarer(
                    pid=1234,
                    process_start_ticks=_stable_systemd_ticks(1234),
                    process_cgroup=control_group,
                    gpu_uuids=frozenset({gpu2}),
                ),
            ),
        ),
    )


def test_systemd_claim_inventory_detects_gpu_declared_only_in_live_environment() -> None:
    gpu3 = EXPECTED_GPU_UUIDS[3]
    control_group = "/user.slice/user-1001.slice/nexpoly-worker.service"
    declaration = (
        "Id=nexpoly-worker.service\n"
        "MainPID=1234\n"
        f"ControlGroup={control_group}\n"
        "Environment=LANG=C\n"
        "EnvironmentFiles=\n"
    )

    claims = query_systemd_gpu_claims(
        run=_systemd_declaration_runner(declaration),
        read_process_environment=lambda _pid: {
            "LANG": "C",
            "NVIDIA_VISIBLE_DEVICES": gpu3,
        },
        read_control_group_processes=lambda path: (
            frozenset({1234}) if path == control_group else frozenset()
        ),
        read_process_cgroup=lambda _pid: control_group,
        read_process_uids=lambda _pid: (1001, 1001, 1001, 1001),
        read_process_start_ticks=_stable_systemd_ticks,
    )

    assert claims == (
        SystemdGpuClaim(
            scope="user",
            unit="nexpoly-worker.service",
            main_pid=1234,
            control_group=control_group,
            process_pids=frozenset({1234}),
            gpu_uuids=frozenset({gpu3}),
            live_gpu_declarers=(
                SystemdGpuDeclarer(
                    pid=1234,
                    process_start_ticks=_stable_systemd_ticks(1234),
                    process_cgroup=control_group,
                    gpu_uuids=frozenset({gpu3}),
                ),
            ),
        ),
    )


def test_systemd_claim_inventory_fails_closed_when_live_environment_is_unreadable() -> None:
    control_group = "/user.slice/user-1001.slice/nexpoly-worker.service"
    declaration = (
        "Id=nexpoly-worker.service\n"
        "MainPID=1234\n"
        f"ControlGroup={control_group}\n"
        "Environment=LANG=C\n"
        "EnvironmentFiles=\n"
    )

    def unreadable(_pid: int) -> dict[str, str]:
        raise PermissionError("denied")

    with pytest.raises(BrokerError, match="cannot safely read live environment") as error:
        query_systemd_gpu_claims(
            run=_systemd_declaration_runner(declaration),
            read_process_environment=unreadable,
            read_control_group_processes=lambda path: (
                frozenset({1234}) if path == control_group else frozenset()
            ),
            read_process_cgroup=lambda _pid: control_group,
            read_process_uids=lambda _pid: (1001, 1001, 1001, 1001),
            read_process_start_ticks=_stable_systemd_ticks,
        )

    assert error.value.code == "gpu_claim_inventory_unavailable"


@pytest.mark.parametrize(
    "declaration",
    [
        "Id=nexpoly-worker.service\nEnvironment=\n",
        (
            "Id=nexpoly-worker.service\nMainPID=1234\n"
            "MainPID=1234\nEnvironment=\n"
        ),
        (
            "Id=nexpoly-worker.service\nMainPID=1234\n"
            "Environment=\nEnvironment=LANG=C\n"
        ),
        (
            "Id=nexpoly-worker.service\nMainPID=1234\n"
            "Environment=\nUnexpected=value\n"
        ),
    ],
)
def test_systemd_claim_inventory_rejects_missing_duplicate_or_unknown_fields(
    declaration: str,
) -> None:
    with pytest.raises(BrokerError, match="systemd GPU declaration response"):
        query_systemd_gpu_claims(
            run=_systemd_declaration_runner(declaration),
            read_process_environment=lambda _pid: {},
            read_process_uids=lambda _pid: (1001, 1001, 1001, 1001),
        )


def test_systemd_claim_inventory_reads_environment_files_and_live_process(
    tmp_path: Path,
) -> None:
    gpu1 = "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771"
    control_group = "/user.slice/user-1001.slice/nexpoly-worker.service"
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
                "ActiveState=active\n"
                "SubState=running\n"
                f"InvocationID={'1' * 32}\n"
                f"MainPID={os.getpid()}\n"
                f"ControlGroup={control_group}\n"
                "Environment=\n"
                f"EnvironmentFiles={environment_file} (ignore_errors=no)\n"
            )
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    claims = query_systemd_gpu_claims(
        run=run,
        read_process_environment=lambda _pid: {"CUDA_VISIBLE_DEVICES": gpu1},
        read_control_group_processes=lambda path: (
            frozenset({os.getpid()})
            if path == control_group
            else frozenset()
        ),
        read_process_cgroup=lambda _pid: control_group,
    )

    assert claims == (
        SystemdGpuClaim(
            scope="user",
            unit="nexpoly-worker.service",
            main_pid=os.getpid(),
            control_group=control_group,
            process_pids=frozenset({os.getpid()}),
            gpu_uuids=frozenset({gpu1}),
            static_gpu_uuids=frozenset({gpu1}),
            live_gpu_declarers=(
                SystemdGpuDeclarer(
                    pid=os.getpid(),
                    process_start_ticks=read_process_start_ticks(os.getpid()),
                    process_cgroup=control_group,
                    gpu_uuids=frozenset({gpu1}),
                ),
            ),
        ),
    )


def test_systemd_claim_inventory_reads_repeated_and_prefixed_optional_files(
    tmp_path: Path,
) -> None:
    gpu1 = "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771"
    control_group = "/user.slice/user-1001.slice/nexpoly-worker.service"
    unrelated = tmp_path / "unrelated.env"
    unrelated.write_text("LANG=C\n", encoding="utf-8")
    worker = tmp_path / "worker.env"
    worker.write_text(f"CUDA_VISIBLE_DEVICES={gpu1}\n", encoding="utf-8")
    missing = tmp_path / "missing.env"
    declaration = (
        "Id=nexpoly-worker.service\n"
        f"MainPID={os.getpid()}\n"
        f"ControlGroup={control_group}\n"
        "Environment=\n"
        f"EnvironmentFiles={unrelated} (ignore_errors=no)\n"
        f"EnvironmentFiles=-{worker} (ignore_errors=yes)\n"
        f"EnvironmentFiles={missing} (ignore_errors=yes)\n"
        f"EnvironmentFiles=-{missing} (ignore_errors=yes)\n"
    )

    claims = query_systemd_gpu_claims(
        run=_systemd_declaration_runner(declaration),
        read_process_environment=lambda _pid: {"CUDA_VISIBLE_DEVICES": gpu1},
        read_control_group_processes=lambda path: (
            frozenset({os.getpid()})
            if path == control_group
            else frozenset()
        ),
        read_process_cgroup=lambda _pid: control_group,
    )

    assert claims == (
        SystemdGpuClaim(
            scope="user",
            unit="nexpoly-worker.service",
            main_pid=os.getpid(),
            control_group=control_group,
            process_pids=frozenset({os.getpid()}),
            gpu_uuids=frozenset({gpu1}),
            static_gpu_uuids=frozenset({gpu1}),
            live_gpu_declarers=(
                SystemdGpuDeclarer(
                    pid=os.getpid(),
                    process_start_ticks=read_process_start_ticks(os.getpid()),
                    process_cgroup=control_group,
                    gpu_uuids=frozenset({gpu1}),
                ),
            ),
        ),
    )


@pytest.mark.parametrize(
    "environment_files",
    [
        "relative.env (ignore_errors=no)",
        "/missing/marker.env",
        "-/missing/required.env (ignore_errors=no)",
        "/missing/value.env (ignore_errors=maybe)",
        "//ambiguous/path.env (ignore_errors=yes)",
        "/missing/extra.env (ignore_errors=yes) trailing",
    ],
)
def test_systemd_claim_inventory_rejects_ambiguous_environment_file_declarations(
    environment_files: str,
) -> None:
    control_group = "/user.slice/user-1001.slice/nexpoly-worker.service"
    declaration = (
        "Id=nexpoly-worker.service\n"
        "MainPID=1234\n"
        f"ControlGroup={control_group}\n"
        "Environment=\n"
        f"EnvironmentFiles={environment_files}\n"
    )

    with pytest.raises(BrokerError, match="EnvironmentFiles declaration is invalid"):
        query_systemd_gpu_claims(
            run=_systemd_declaration_runner(declaration),
            read_process_environment=lambda _pid: {},
            read_control_group_processes=lambda path: (
                frozenset({1234}) if path == control_group else frozenset()
            ),
            read_process_cgroup=lambda _pid: control_group,
            read_process_uids=lambda _pid: (1001, 1001, 1001, 1001),
            read_process_start_ticks=_stable_systemd_ticks,
        )


def test_systemd_claim_inventory_requires_nonoptional_environment_file(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.env"
    control_group = "/user.slice/user-1001.slice/nexpoly-worker.service"
    declaration = (
        "Id=nexpoly-worker.service\n"
        "MainPID=1234\n"
        f"ControlGroup={control_group}\n"
        "Environment=\n"
        f"EnvironmentFiles={missing} (ignore_errors=no)\n"
    )

    with pytest.raises(BrokerError, match="EnvironmentFile is unsafe"):
        query_systemd_gpu_claims(
            run=_systemd_declaration_runner(declaration),
            read_process_environment=lambda _pid: {},
            read_control_group_processes=lambda path: (
                frozenset({1234}) if path == control_group else frozenset()
            ),
            read_process_cgroup=lambda _pid: control_group,
            read_process_uids=lambda _pid: (1001, 1001, 1001, 1001),
            read_process_start_ticks=_stable_systemd_ticks,
        )


def test_systemd_claim_inventory_does_not_ignore_existing_unsafe_optional_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "worker.env"
    target.write_text("CUDA_VISIBLE_DEVICES=none\n", encoding="utf-8")
    environment_file = tmp_path / "worker-link.env"
    environment_file.symlink_to(target)
    control_group = "/user.slice/user-1001.slice/nexpoly-worker.service"
    declaration = (
        "Id=nexpoly-worker.service\n"
        "MainPID=1234\n"
        f"ControlGroup={control_group}\n"
        "Environment=\n"
        f"EnvironmentFiles=-{environment_file} (ignore_errors=yes)\n"
    )

    with pytest.raises(BrokerError, match="EnvironmentFile is unsafe"):
        query_systemd_gpu_claims(
            run=_systemd_declaration_runner(declaration),
            read_process_environment=lambda _pid: {},
            read_control_group_processes=lambda path: (
                frozenset({1234}) if path == control_group else frozenset()
            ),
            read_process_cgroup=lambda _pid: control_group,
            read_process_uids=lambda _pid: (1001, 1001, 1001, 1001),
            read_process_start_ticks=_stable_systemd_ticks,
        )


def test_systemd_environment_file_rejects_group_or_world_writable_input(
    tmp_path: Path,
) -> None:
    environment_file = tmp_path / "worker.env"
    environment_file.write_text("CUDA_VISIBLE_DEVICES=none\n", encoding="utf-8")
    environment_file.chmod(0o660)

    with pytest.raises(BrokerError, match="EnvironmentFile is unsafe"):
        _read_systemd_environment_file(environment_file)


def test_systemd_environment_file_rejects_hardlink_alias(
    tmp_path: Path,
) -> None:
    environment_file = tmp_path / "worker.env"
    environment_file.write_text("CUDA_VISIBLE_DEVICES=none\n", encoding="utf-8")
    os.link(environment_file, tmp_path / "worker-alias.env")

    with pytest.raises(BrokerError, match="EnvironmentFile is unsafe"):
        _read_systemd_environment_file(environment_file)


def test_systemd_environment_file_rejects_path_replacement_while_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment_file = tmp_path / "worker.env"
    displaced = tmp_path / "worker.original.env"
    environment_file.write_text("CUDA_VISIBLE_DEVICES=none\n", encoding="utf-8")
    real_pread = os.pread

    def replace_after_read(descriptor: int, size: int, offset: int) -> bytes:
        raw = real_pread(descriptor, size, offset)
        environment_file.rename(displaced)
        environment_file.write_text(
            "CUDA_VISIBLE_DEVICES=all\n",
            encoding="utf-8",
        )
        return raw

    monkeypatch.setattr(os, "pread", replace_after_read)

    with pytest.raises(BrokerError, match="identity changed while read"):
        _read_systemd_environment_file(environment_file)


def _audit_activating_environment_file_after_mutation(
    environment_file: Path,
    mutate,
    *,
    optional: bool = False,
    transitional: bool = False,
    mutation_list_call: int = 2,
) -> tuple[SystemdGpuClaim, ...]:
    unit = "nexpoly-starting-env-worker.service"
    control_group = f"/user.slice/user-1001.slice/{unit}"
    declaration = _systemd_properties(
        unit,
        active_state="activating" if transitional else "active",
        sub_state="start" if transitional else "running",
        main_pid=0,
        control_group=control_group,
        environment_files=(
            (
                f"-{environment_file} (ignore_errors=yes)"
                if optional
                else f"{environment_file} (ignore_errors=no)"
            ),
        ),
    )
    user_list_calls = 0

    def run(command, **_kwargs):
        nonlocal user_list_calls
        if "list-units" in command and "--user" in command:
            user_list_calls += 1
            if user_list_calls == mutation_list_call:
                mutate()
            if transitional:
                stdout = f"{unit} loaded activating start Test service\n"
            else:
                stdout = f"{unit} loaded active running Test service\n"
        elif "show" in command and "--user" in command:
            stdout = declaration
        else:
            stdout = ""
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=stdout,
            stderr="",
        )

    return query_systemd_gpu_claims(
        compute_processes={},
        run=run,
        read_control_group_processes=lambda _path: frozenset(),
        read_process_cgroup=lambda _pid: control_group,
        read_process_uids=lambda _pid: (1001, 1001, 1001, 1001),
        read_process_start_ticks=_stable_systemd_ticks,
    )


def test_systemd_environment_file_rejects_replacement_after_initial_read(
    tmp_path: Path,
) -> None:
    environment_file = tmp_path / "worker.env"
    displaced = tmp_path / "worker.original.env"
    environment_file.write_text(
        "CUDA_VISIBLE_DEVICES=none\n",
        encoding="utf-8",
    )
    environment_file.chmod(0o600)

    def replace_path() -> None:
        environment_file.rename(displaced)
        environment_file.write_text(
            f"CUDA_VISIBLE_DEVICES={EXPECTED_GPU_UUIDS[1]}\n",
            encoding="utf-8",
        )
        environment_file.chmod(0o600)

    with pytest.raises(BrokerError, match="EnvironmentFile"):
        _audit_activating_environment_file_after_mutation(
            environment_file,
            replace_path,
        )


def test_systemd_environment_file_rejects_same_inode_rewrite_after_read(
    tmp_path: Path,
) -> None:
    environment_file = tmp_path / "worker.env"
    environment_file.write_text(
        "CUDA_VISIBLE_DEVICES=none\n",
        encoding="utf-8",
    )
    environment_file.chmod(0o600)
    original_inode = environment_file.stat().st_ino

    def rewrite_content() -> None:
        environment_file.write_text(
            f"CUDA_VISIBLE_DEVICES={EXPECTED_GPU_UUIDS[1]}\n",
            encoding="utf-8",
        )
        environment_file.chmod(0o600)
        assert environment_file.stat().st_ino == original_inode

    with pytest.raises(BrokerError, match="EnvironmentFile"):
        _audit_activating_environment_file_after_mutation(
            environment_file,
            rewrite_content,
        )


def test_systemd_environment_file_rejects_path_identity_aba_after_read(
    tmp_path: Path,
) -> None:
    environment_file = tmp_path / "worker.env"
    original = tmp_path / "worker.original.env"
    attacker = tmp_path / "worker.attacker.env"
    environment_file.write_text(
        "CUDA_VISIBLE_DEVICES=none\n",
        encoding="utf-8",
    )
    environment_file.chmod(0o600)
    original_identity = (
        environment_file.stat().st_dev,
        environment_file.stat().st_ino,
    )

    def replace_and_restore() -> None:
        environment_file.rename(original)
        attacker.write_text(
            f"CUDA_VISIBLE_DEVICES={EXPECTED_GPU_UUIDS[1]}\n",
            encoding="utf-8",
        )
        attacker.chmod(0o600)
        attacker.rename(environment_file)
        environment_file.unlink()
        original.rename(environment_file)
        assert (
            environment_file.stat().st_dev,
            environment_file.stat().st_ino,
        ) == original_identity

    with pytest.raises(BrokerError, match="EnvironmentFile"):
        _audit_activating_environment_file_after_mutation(
            environment_file,
            replace_and_restore,
            transitional=True,
            mutation_list_call=1,
        )


def test_transitional_systemd_unit_with_environment_file_fails_closed(
    tmp_path: Path,
) -> None:
    environment_file = tmp_path / "worker.env"
    environment_file.write_text(
        "CUDA_VISIBLE_DEVICES=none\n",
        encoding="utf-8",
    )
    environment_file.chmod(0o600)

    with pytest.raises(
        BrokerError,
        match="transitional systemd EnvironmentFile",
    ):
        _audit_activating_environment_file_after_mutation(
            environment_file,
            lambda: None,
            transitional=True,
            mutation_list_call=99,
        )


def test_systemd_optional_environment_file_rejects_creation_after_missing_snapshot(
    tmp_path: Path,
) -> None:
    environment_file = tmp_path / "optional-worker.env"

    def create_optional_file() -> None:
        environment_file.write_text(
            f"CUDA_VISIBLE_DEVICES={EXPECTED_GPU_UUIDS[1]}\n",
            encoding="utf-8",
        )
        environment_file.chmod(0o600)

    with pytest.raises(BrokerError, match="EnvironmentFile"):
        _audit_activating_environment_file_after_mutation(
            environment_file,
            create_optional_file,
            optional=True,
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
        drained = client.set_draining(True)
        assert drained["draining"] is True
        assert drained["leases"] == []
        resumed = client.set_draining(False)
        assert resumed["draining"] is False
        assert resumed["leases"] == []
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
