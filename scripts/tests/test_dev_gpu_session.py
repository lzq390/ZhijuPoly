from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import dev_gpu_session as session  # noqa: E402


def empty_snapshot() -> session.TargetSnapshot:
    return session.TargetSnapshot((), (), ())


def mps_snapshot(
    *,
    server_pids: frozenset[int] = frozenset(),
    client_pids: frozenset[int] = frozenset(),
    declarers: frozenset[object] = frozenset(),
) -> object:
    from ops.gpu_broker.server import MpsAuthoritySnapshot, MpsClient

    server_pid = next(iter(server_pids), 7001)
    return MpsAuthoritySnapshot(
        server_pids=server_pids,
        gpu_declarers=declarers,
        clients=frozenset(
            MpsClient(
                client_pid=pid,
                client_id=index,
                server_pid=server_pid,
                device_uuid=session.GPU_UUID,
                namespace_id=1,
                command="python",
            )
            for index, pid in enumerate(sorted(client_pids))
        ),
        descriptor_authority=True,
    )


def dft_residency_record(
    *,
    lease_id: str = "d1" * 16,
    workload_pid: int = 8123,
    workload_start_ticks: int = 456,
) -> dict[str, object]:
    from gpu_resource.transient_scope import scope_control_group
    from ops.gpu_broker.broker import Lease

    control_group = scope_control_group(lease_id, uid=1001)
    return Lease(
        lease_id=lease_id,
        fencing_token=1,
        broker_instance_id="broker",
        request_id="dft:dev:residency",
        kind="residency",
        placement="preferred",
        component="dft",
        environment="dev",
        client_id="dft-dev",
        gpu_index=1,
        gpu_uuid=session.GPU_UUID,
        memory_mib=4096,
        thread_percent=50,
        owner_pid=workload_pid,
        owner_process_start_ticks=workload_start_ticks,
        owner_boot_id="boot",
        preferred=True,
        parent_lease_id=None,
        status="active",
        created_at=1.0,
        heartbeat_at=2.0,
        workload_pid=workload_pid,
        workload_process_start_ticks=workload_start_ticks,
        workload_process_group_id=workload_pid,
        workload_cgroup=f"0::{control_group}",
    ).public_dict()


def md_execution_record(
    *,
    lease_id: str = "e2" * 16,
    fencing_token: int = 2,
    workload_pid: int = 76740,
    workload_start_ticks: int = 789,
) -> dict[str, object]:
    from gpu_resource.transient_scope import scope_control_group
    from ops.gpu_broker.broker import Lease

    control_group = scope_control_group(lease_id, uid=1001)
    return Lease(
        lease_id=lease_id,
        fencing_token=fencing_token,
        broker_instance_id="broker",
        request_id="md:dev:runtime-probe",
        kind="execution",
        placement="any",
        component="md",
        environment="dev",
        client_id="md-dev",
        gpu_index=1,
        gpu_uuid=session.GPU_UUID,
        memory_mib=8192,
        thread_percent=50,
        owner_pid=workload_pid,
        owner_process_start_ticks=workload_start_ticks,
        owner_boot_id="boot",
        preferred=True,
        parent_lease_id=None,
        status="active",
        created_at=1.0,
        heartbeat_at=2.0,
        workload_pid=workload_pid,
        workload_process_start_ticks=workload_start_ticks,
        workload_process_group_id=workload_pid,
        workload_cgroup=f"0::{control_group}",
    ).public_dict()


def dft_membership_error(
    *,
    scope: str = "system",
    expected: tuple[tuple[int, int, str], ...] | None = None,
    current: tuple[tuple[int, int, str], ...] | None = None,
    unit: str | None = None,
    control_group: str | None = None,
) -> Exception:
    from gpu_resource.transient_scope import (
        scope_control_group,
        scope_unit_name,
        user_manager_control_group,
    )
    from ops.gpu_broker.server import SystemdMembershipChanged

    dft_cgroup = scope_control_group("d1" * 16, uid=1001)
    manager_cgroup = user_manager_control_group(1001)
    resolved_unit = (
        "user@1001.service"
        if scope == "system"
        else scope_unit_name("d1" * 16)
    )
    resolved_control_group = (
        manager_cgroup if scope == "system" else dft_cgroup
    )
    stable_cgroup = manager_cgroup if scope == "system" else dft_cgroup
    if expected is None:
        expected = (
            (7000, 100, stable_cgroup),
            (8124, 457, dft_cgroup),
        )
    if current is None:
        current = ((7000, 100, stable_cgroup),)
    return SystemdMembershipChanged(
        scope,
        resolved_unit if unit is None else unit,
        resolved_control_group if control_group is None else control_group,
        expected,
        current,
    )


def backend_residency_record(*, status: str = "active") -> dict[str, object]:
    from ops.gpu_broker.broker import Lease

    pid = 6101
    return Lease(
        lease_id="b1" * 16,
        fencing_token=4,
        broker_instance_id="broker",
        request_id="backend:dev:residency",
        kind="residency",
        placement="preferred",
        component="backend",
        environment="dev",
        client_id="backend-dev",
        gpu_index=1,
        gpu_uuid=session.GPU_UUID,
        memory_mib=8192,
        thread_percent=100,
        owner_pid=pid,
        owner_process_start_ticks=333,
        owner_boot_id="boot",
        preferred=True,
        parent_lease_id=None,
        status=status,
        created_at=1.0,
        heartbeat_at=2.0,
        workload_pid=pid,
        workload_process_start_ticks=333,
        workload_process_group_id=pid,
        workload_cgroup="0::/docker/backend.scope",
    ).public_dict()


def backend_docker_claim() -> object:
    from ops.gpu_broker.server import DockerGpuClaim

    return DockerGpuClaim(
        container_id="b" * 64,
        init_pid=6101,
        started_at="2026-07-19T06:00:00.123456789Z",
        restart_count=0,
        registration_id="backend-dev",
        component="backend",
        environment="dev",
        compose_project="nexpoly_dev",
        compose_service="backend",
        gpu_uuids=frozenset({session.GPU_UUID}),
    )


def patch_backend_identity(
    monkeypatch: pytest.MonkeyPatch,
    *,
    start_ticks: object = 333,
) -> None:
    from ops.gpu_broker import server

    monkeypatch.setattr(server, "read_boot_id", lambda: "boot")
    monkeypatch.setattr(
        server,
        "read_process_start_ticks",
        start_ticks if callable(start_ticks) else lambda _pid: start_ticks,
    )
    monkeypatch.setattr(
        server,
        "_read_process_uids",
        lambda _pid: (1001, 1001, 1001, 1001),
    )
    monkeypatch.setattr(server.os, "getpgid", lambda _pid: 6101)
    monkeypatch.setattr(
        server,
        "_read_unified_process_cgroup",
        lambda _pid: "/docker/backend.scope",
    )
    monkeypatch.setattr(
        server,
        "_pid_is_or_descends_from",
        lambda pid, ancestor: pid == ancestor == 6101,
    )


def mps_declarer(pid: int, start_ticks: int = 100) -> object:
    from ops.gpu_broker.server import SystemdGpuDeclarer

    return SystemdGpuDeclarer(
        pid=pid,
        process_start_ticks=start_ticks,
        process_cgroup="/user.slice/user-1001.slice/nexpoly-mps.scope",
        gpu_uuids=frozenset({session.GPU_UUID}),
    )


def patch_fast_guard_runtime(
    monkeypatch: pytest.MonkeyPatch,
    controller: session.SessionController,
    *,
    authorities: tuple[object, object],
    compute: tuple[frozenset[int], frozenset[int]],
    docker: tuple[tuple[object, ...], tuple[object, ...]] = ((), ()),
    exact_pids: frozenset[int] = frozenset({8123}),
    compute_declarers: tuple[
        dict[int, object], dict[int, object]
    ] | None = None,
) -> None:
    from ops.gpu_broker import server

    authority_reads = iter(authorities)
    compute_reads = iter(compute)
    docker_reads = iter(docker)
    if compute_declarers is None:
        known_declarers = {
            declarer.pid: declarer
            for authority in authorities
            for declarer in authority.gpu_declarers
        }
        compute_declarers = (
            {
                pid: known_declarers[pid]
                for pid in compute[0]
                if pid in known_declarers
            },
            {
                pid: known_declarers[pid]
                for pid in compute[1]
                if pid in known_declarers
            },
        )
    declarer_reads = iter(compute_declarers)
    monkeypatch.setattr(
        controller,
        "_mps_authority",
        lambda: next(authority_reads),
    )
    monkeypatch.setattr(
        server,
        "query_gpu_inventory",
        lambda: {session.GPU_INDEX: session.GPU_UUID},
    )
    monkeypatch.setattr(
        server,
        "query_compute_processes",
        lambda: {session.GPU_UUID: next(compute_reads)},
    )
    monkeypatch.setattr(
        session,
        "capture_compute_process_declarers",
        lambda pids: {
            pid: declarer
            for pid, declarer in next(declarer_reads).items()
            if pid in pids
        },
    )
    monkeypatch.setattr(
        server,
        "query_docker_gpu_claims",
        lambda: next(docker_reads),
    )
    monkeypatch.setattr(
        server,
        "process_is_exact_dft_residency_descendant",
        lambda pid, *_args, **_kwargs: pid in exact_pids,
    )
    monkeypatch.setattr(
        session,
        "require_gpu1_default_compute_mode",
        lambda: None,
    )


def patch_full_audit_runtime(
    monkeypatch: pytest.MonkeyPatch,
    controller: session.SessionController,
    status: dict[str, object],
    *,
    snapshot: session.TargetSnapshot | None = None,
    authority: object | None = None,
    trailing_compute: frozenset[int] = frozenset(),
    trailing_docker: tuple[object, ...] = (),
    trailing_systemd: tuple[object, ...] | None = None,
) -> SimpleNamespace:
    from ops.gpu_broker import server

    if snapshot is None:
        snapshot = empty_snapshot()
    if authority is None:
        authority = mps_snapshot()
    snapshot = dft_broad_snapshot(
        monkeypatch,
        status,
        authority,
        snapshot,
    )
    if trailing_systemd is None:
        trailing_systemd = snapshot.systemd_claims
    monkeypatch.setattr(
        session,
        "consistent_broker_snapshot",
        lambda _client, **_kwargs: (status, snapshot),
    )
    monkeypatch.setattr(controller, "_mps_authority", lambda: authority)
    monkeypatch.setattr(
        server,
        "query_compute_processes",
        lambda: {session.GPU_UUID: trailing_compute},
    )
    monkeypatch.setattr(
        server,
        "query_docker_gpu_claims",
        lambda: trailing_docker,
    )
    monkeypatch.setattr(
        server,
        "query_systemd_gpu_claims",
        lambda **_kwargs: trailing_systemd,
    )
    monkeypatch.setattr(
        session,
        "require_gpu1_default_compute_mode",
        lambda: None,
    )
    known_declarers = {
        declarer.pid: declarer
        for declarer in (
            *authority.gpu_declarers,
            *snapshot.process_declarers,
            *(
                declarer
                for claim in (*snapshot.systemd_claims, *trailing_systemd)
                for declarer in claim.live_gpu_declarers
            ),
        )
    }
    monkeypatch.setattr(
        session,
        "capture_compute_process_declarers",
        lambda pids: {
            pid: known_declarers[pid]
            for pid in pids
            if pid in known_declarers
        },
    )
    return SimpleNamespace(status=lambda: status)


def dft_resident_authority(
    *,
    server_pid: int = 7001,
    workload_pid: int = 8123,
) -> object:
    return mps_snapshot(
        server_pids=frozenset({server_pid}),
        client_pids=frozenset({workload_pid}),
        declarers=frozenset(
            {mps_declarer(7000), mps_declarer(server_pid, 101)}
        ),
    )


def dft_broad_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    status: dict[str, object],
    authority: object,
    snapshot: session.TargetSnapshot,
) -> session.TargetSnapshot:
    """Model the real system user-manager DFT/MPS claim for audit tests."""

    from ops.gpu_broker import server

    if snapshot.systemd_claims:
        return snapshot
    records = status.get("leases")
    if not isinstance(records, list):
        return snapshot
    matches = [
        record
        for record in records
        if isinstance(record, dict)
        and record.get("component") == "dft"
        and record.get("kind") == "residency"
        and record.get("status") == "active"
    ]
    if len(matches) != 1:
        return snapshot
    lease = matches[0]
    root_pid = int(lease["workload_pid"])
    root_start = int(lease["workload_process_start_ticks"])
    control_group = str(lease["workload_cgroup"])[3:]
    root = server.SystemdGpuDeclarer(
        pid=root_pid,
        process_start_ticks=root_start,
        process_cgroup=control_group,
        gpu_uuids=frozenset({session.GPU_UUID}),
    )
    manager_control_group = server.user_manager_control_group(1001)
    mps_declarers = tuple(
        declarer
        for declarer in authority.gpu_declarers
        if server._systemd_cgroup_contains(
            declarer.process_cgroup,
            manager_control_group,
        )
    )
    claim = server.SystemdGpuClaim(
        scope="system",
        unit="user@1001.service",
        main_pid=7000,
        control_group=manager_control_group,
        process_pids=frozenset(
            {root_pid, *(item.pid for item in mps_declarers), 8999}
        ),
        gpu_uuids=frozenset({session.GPU_UUID}),
        active_gpu_uuids=frozenset({session.GPU_UUID}),
        live_gpu_declarers=(root, *mps_declarers),
    )
    monkeypatch.setattr(server, "read_boot_id", lambda: str(lease["owner_boot_id"]))
    monkeypatch.setattr(
        server,
        "read_process_start_ticks",
        lambda pid: (
            int(lease["owner_process_start_ticks"])
            if pid == int(lease["owner_pid"])
            else root_start
        ),
    )
    monkeypatch.setattr(
        server,
        "_read_process_uids",
        lambda _pid: (1001, 1001, 1001, 1001),
    )
    monkeypatch.setattr(server.os, "getpgid", lambda _pid: root_pid)
    monkeypatch.setattr(
        server,
        "_read_unified_process_cgroup",
        lambda pid: control_group if pid == root_pid else "/mps.scope",
    )
    monkeypatch.setattr(
        server,
        "_pid_is_or_descends_from",
        lambda pid, ancestor: pid == root_pid == ancestor,
    )
    known_process_declarers = {
        declarer.pid: declarer
        for declarer in (root, *authority.gpu_declarers)
    }
    process_declarers = snapshot.process_declarers or tuple(
        known_process_declarers[pid]
        for pid in snapshot.process_pids
        if pid in known_process_declarers
    )
    return session.TargetSnapshot(
        snapshot.process_pids,
        snapshot.docker_claims,
        (claim,),
        process_declarers,
    )


def test_dry_run_from_foreign_cwd_has_no_runtime_side_effects(tmp_path: Path) -> None:
    before = session.CONTROLLER_RECORD.exists()
    completed = subprocess.run(
        [sys.executable, str(session.__file__), "up", "--dry-run"],
        cwd=tmp_path,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONPATH": ""},
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    payload = json.loads(completed.stdout)

    assert payload["dry_run"] is True
    assert payload["gpu_index"] == 1
    assert payload["compute_mode"] == "Default"
    assert payload["exclusive"] is False
    assert payload["gpu3_untouched"] is True
    assert session.CONTROLLER_RECORD.exists() is before


def test_startup_double_audit_is_separated_and_reads_twice() -> None:
    calls = 0
    pauses: list[float] = []

    def collect() -> session.TargetSnapshot:
        nonlocal calls
        calls += 1
        return empty_snapshot()

    session.require_double_free_audit(
        collect,
        pause=pauses.append,
        interval_seconds=1.0,
    )

    assert calls == 2
    assert pauses == [1.0]


@pytest.mark.parametrize(
    "snapshot",
    [
        session.TargetSnapshot((991,), (), ()),
        session.TargetSnapshot(
            (),
            (SimpleNamespace(container_id="f" * 64, gpu_uuids=frozenset({session.GPU_UUID})),),
            (),
        ),
    ],
)
def test_double_audit_fails_closed_for_unknown_gpu1_authority(snapshot) -> None:
    calls = 0

    def collect() -> session.TargetSnapshot:
        nonlocal calls
        calls += 1
        return snapshot

    with pytest.raises(session.DevGpuSessionError, match="GPU1 is busy"):
        session.require_double_free_audit(collect, pause=lambda _seconds: None)
    assert calls == 2


def test_child_broker_descriptor_authority_is_process_local() -> None:
    assert session.SessionController._child_authority_path(7) == Path(
        "/proc/self/fd/7"
    )
    with pytest.raises(session.DevGpuSessionError, match="descriptor authority"):
        session.SessionController._child_authority_path(2)


def test_controller_safe_environment_binds_local_user_manager() -> None:
    controller = object.__new__(session.SessionController)
    environment = controller._safe_env()
    runtime_directory = f"/run/user/{os.geteuid()}"

    assert environment["XDG_RUNTIME_DIR"] == runtime_directory
    assert environment["DBUS_SESSION_BUS_ADDRESS"] == (
        f"unix:path={runtime_directory}/bus"
    )


def test_controller_mps_inventory_uses_descriptor_bound_cuda_paths() -> None:
    controller = object.__new__(session.SessionController)
    controller.root_fd = 7
    controller.reservations_fd = 8
    controller.pipe_fd = 9
    controller.log_fd = 10
    controller.slot_fd = 11

    environment = controller._mps_env()

    authority = f"/proc/{os.getpid()}/fd"
    assert environment["CUDA_VISIBLE_DEVICES"] == session.GPU_UUID
    assert environment["CUDA_MPS_PIPE_DIRECTORY"] == f"{authority}/9"
    assert environment["CUDA_MPS_LOG_DIRECTORY"] == f"{authority}/10"


def test_inventory_filters_gpu3_and_never_treats_polyprop_as_gpu1(monkeypatch) -> None:
    import ops.gpu_broker.server as broker

    gpu3 = broker.EXPECTED_GPU_UUIDS[3]
    gpu3_claim = SimpleNamespace(gpu_uuids=frozenset({gpu3}))
    monkeypatch.setattr(
        broker,
        "query_gpu_inventory",
        lambda: {1: session.GPU_UUID, 3: gpu3},
    )
    monkeypatch.setattr(
        broker,
        "query_compute_processes",
        lambda: {gpu3: frozenset({333})},
    )
    monkeypatch.setattr(broker, "query_docker_gpu_claims", lambda: (gpu3_claim,))
    monkeypatch.setattr(
        broker,
        "query_systemd_gpu_claims",
        lambda **_kwargs: (gpu3_claim,),
    )

    snapshot = session.collect_target_snapshot()

    assert snapshot == empty_snapshot()


def test_target_inventory_binds_nvml_pid_to_adjacent_process_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ops.gpu_broker.server as broker

    declarer = mps_declarer(7001, 101)
    events: list[str] = []

    def compute_processes() -> dict[str, frozenset[int]]:
        events.append("compute")
        return {session.GPU_UUID: frozenset({7001})}

    def capture(pids: frozenset[int]) -> dict[int, object]:
        events.append("capture")
        return {7001: declarer} if pids == frozenset({7001}) else {}

    def docker_claims() -> tuple[object, ...]:
        events.append("docker")
        return ()

    def systemd_claims(**_kwargs) -> tuple[object, ...]:
        events.append("systemd")
        return ()

    monkeypatch.setattr(
        broker,
        "query_gpu_inventory",
        lambda: {1: session.GPU_UUID},
    )
    monkeypatch.setattr(
        broker,
        "query_compute_processes",
        compute_processes,
    )
    monkeypatch.setattr(broker, "query_docker_gpu_claims", docker_claims)
    monkeypatch.setattr(
        broker,
        "query_systemd_gpu_claims",
        systemd_claims,
    )
    monkeypatch.setattr(
        session,
        "capture_compute_process_declarers",
        capture,
    )
    monkeypatch.setattr(
        session,
        "require_gpu1_default_compute_mode",
        lambda: None,
    )

    snapshot = session.collect_target_snapshot()

    assert snapshot.process_pids == (7001,)
    assert snapshot.process_declarers == (declarer,)
    assert events == ["compute", "capture", "docker", "systemd"]


def test_running_foreign_process_only_drains_broker_and_never_signals(monkeypatch) -> None:
    calls: list[bool] = []
    broker = SimpleNamespace(set_draining=lambda value: calls.append(value) or {})
    monkeypatch.setattr(
        session.signal,
        "pidfd_send_signal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("signal forbidden")),
        raising=False,
    )

    reasons = session.drain_on_contamination(
        session.TargetSnapshot((404,), (), ()),
        authorized_mps_pids=frozenset({101}),
        broker_client=broker,
    )

    assert reasons == ("foreign CUDA PID(s): 404",)
    assert calls == [True]


def test_private_mps_client_inventory_is_strict_and_tracks_pids() -> None:
    payload = (
        b"PID ID SERVER DEVICE NAMESPACE COMMAND\n"
        b"8123 0 7001 0 0 python\n"
        b"8456 1 7001 0 0 gmx mdrun\n"
    )

    assert session.parse_mps_client_inventory(payload) == frozenset({8123, 8456})
    with pytest.raises(session.DevGpuSessionError, match="row is invalid"):
        session.parse_mps_client_inventory(
            b"PID ID SERVER DEVICE NAMESPACE COMMAND\nnot-a-row\n"
        )


def test_audit_marks_unleased_private_mps_client_foreign(tmp_path, monkeypatch) -> None:
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.dft_warmup_open = False
    client = SimpleNamespace(
        status=lambda: {
            "broker_instance_id": "b",
            "next_fencing_token": 1,
            "draining": False,
            "leases": [],
        }
    )
    monkeypatch.setattr(
        session,
        "consistent_broker_snapshot",
        lambda _client, **_kwargs: (client.status(), empty_snapshot()),
    )
    authority = mps_snapshot(
        server_pids=frozenset({7001}),
        client_pids=frozenset({8123}),
    )
    monkeypatch.setattr(controller, "_mps_authority", lambda: authority)

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons == ("unknown private MPS client PID(s): 8123",)


def test_full_audit_discards_one_torn_mps_client_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.gpu_broker.broker import BrokerError

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 1,
        "draining": False,
        "leases": [],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.dft_warmup_open = False
    client = patch_full_audit_runtime(monkeypatch, controller, status)
    authority = mps_snapshot()
    calls = 0

    def snapshot():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise BrokerError(
                "mps_control_unavailable",
                "MPS ps row is invalid",
            )
        return authority

    monkeypatch.setattr(controller, "_mps_authority", snapshot)

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons == ()
    assert calls == 4
    assert controller.full_audit_generation == 1


def test_full_audit_fails_closed_on_repeated_torn_mps_client_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.gpu_broker.broker import BrokerError

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 1,
        "draining": False,
        "leases": [],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.dft_warmup_open = False
    client = patch_full_audit_runtime(monkeypatch, controller, status)
    calls = 0

    def snapshot():
        nonlocal calls
        calls += 1
        raise BrokerError(
            "mps_control_unavailable",
            "MPS ps row is invalid",
        )

    monkeypatch.setattr(controller, "_mps_authority", snapshot)

    with pytest.raises(
        session.DevGpuSessionError,
        match="authority changed throughout trailing full audits",
    ):
        controller._audit(client)

    assert calls == session.FULL_AUDIT_ATTEMPTS


def test_direct_user_scope_does_not_expand_managed_workload_authority() -> None:
    lease_id = "a1" * 16
    workload_pid = 8123
    child_pid = 8456
    control_group = (
        "/user.slice/user-1001.slice/user@1001.service/nexpoly.slice/"
        "nexpoly-gpu.slice/nexpoly-gpu-jobs.slice/"
        f"nexpoly-gpu-job-{lease_id}.scope"
    )
    claim = SimpleNamespace(
        scope="user",
        unit=f"nexpoly-gpu-job-{lease_id}.scope",
        main_pid=workload_pid,
        control_group=control_group,
        process_pids=frozenset({workload_pid, child_pid}),
        gpu_uuids=frozenset({session.GPU_UUID}),
    )
    snapshot = session.TargetSnapshot((workload_pid,), (), (claim,))
    lease = dft_residency_record(
        lease_id=lease_id,
        workload_pid=workload_pid,
    )
    lease.update(
        component="md",
        kind="execution",
        placement="any",
        owner_pid=8001,
        workload_cgroup=f"0::{control_group}",
    )
    status = {"leases": [lease]}

    managed = session.SessionController._managed_workload_pids(status, snapshot)

    assert managed == frozenset()
    assert session.foreign_gpu1_reasons(
        snapshot,
        authorized_mps_pids=frozenset(),
        managed_workload_pids=managed,
    ) == (
        f"foreign CUDA PID(s): {workload_pid}",
        f"foreign systemd claim: user:nexpoly-gpu-job-{lease_id}.scope",
    )


def test_scope_descendants_remain_foreign_when_control_group_identity_differs() -> None:
    lease_id = "a1" * 16
    workload_pid = 8123
    child_pid = 8456
    claim = SimpleNamespace(
        scope="user",
        unit=f"nexpoly-gpu-job-{lease_id}.scope",
        main_pid=workload_pid,
        control_group="/user.slice/forged.scope",
        process_pids=frozenset({workload_pid, child_pid}),
        gpu_uuids=frozenset({session.GPU_UUID}),
    )
    snapshot = session.TargetSnapshot((workload_pid,), (), (claim,))
    lease = dft_residency_record(
        lease_id=lease_id,
        workload_pid=workload_pid,
    )
    lease.update(
        component="md",
        kind="execution",
        placement="any",
        owner_pid=8001,
        workload_cgroup="0::/user.slice/exact.scope",
    )
    status = {"leases": [lease]}

    managed = session.SessionController._managed_workload_pids(status, snapshot)

    assert child_pid not in managed
    assert session.foreign_gpu1_reasons(
        snapshot,
        authorized_mps_pids=frozenset(),
        managed_workload_pids=managed,
    ) == (
        f"foreign CUDA PID(s): {workload_pid}",
        f"foreign systemd claim: user:nexpoly-gpu-job-{lease_id}.scope",
    )


def test_exact_dft_user_manager_claim_authorizes_only_residency_declarers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.gpu_broker import server

    workload_pid = 8123
    compiler_pid = 8456
    unrelated_cpu_pid = 8999
    workload_start_ticks = 456
    compiler_start_ticks = 789
    lease = dft_residency_record(
        workload_pid=workload_pid,
        workload_start_ticks=workload_start_ticks,
    )
    control_group = str(lease["workload_cgroup"])[3:]
    claim = server.SystemdGpuClaim(
        scope="system",
        unit="user@1001.service",
        main_pid=7001,
        control_group=server.user_manager_control_group(1001),
        process_pids=frozenset(
            {workload_pid, compiler_pid, unrelated_cpu_pid}
        ),
        gpu_uuids=frozenset({session.GPU_UUID}),
        active_gpu_uuids=frozenset({session.GPU_UUID}),
        live_gpu_declarers=(
            server.SystemdGpuDeclarer(
                pid=workload_pid,
                process_start_ticks=workload_start_ticks,
                process_cgroup=control_group,
                gpu_uuids=frozenset({session.GPU_UUID}),
            ),
            server.SystemdGpuDeclarer(
                pid=compiler_pid,
                process_start_ticks=compiler_start_ticks,
                process_cgroup=control_group,
                gpu_uuids=frozenset({session.GPU_UUID}),
            ),
        ),
    )
    monkeypatch.setattr(
        server,
        "read_process_start_ticks",
        lambda pid: {
            workload_pid: workload_start_ticks,
            compiler_pid: compiler_start_ticks,
        }[pid],
    )
    monkeypatch.setattr(server, "read_boot_id", lambda: "boot")
    monkeypatch.setattr(
        server,
        "_read_process_uids",
        lambda _pid: (1001, 1001, 1001, 1001),
    )
    monkeypatch.setattr(server.os, "getpgid", lambda _pid: workload_pid)
    monkeypatch.setattr(
        server,
        "_read_unified_process_cgroup",
        lambda pid: control_group if pid in {workload_pid, compiler_pid} else "/foreign",
    )
    monkeypatch.setattr(
        server,
        "_pid_is_or_descends_from",
        lambda pid, ancestor: pid == ancestor
        or (pid == compiler_pid and ancestor == workload_pid),
    )
    snapshot = session.TargetSnapshot(
        (workload_pid, compiler_pid),
        (),
        (claim,),
    )
    status = {"leases": [lease]}

    managed_nvml, managed_clients, managed_claims = (
        session.SessionController._managed_workload_authority(status, snapshot)
    )

    assert managed_nvml == frozenset()
    assert managed_clients == frozenset({workload_pid, compiler_pid})
    assert unrelated_cpu_pid not in managed_clients
    assert managed_claims == frozenset(
        {("system", "user@1001.service", claim.control_group)}
    )
    assert session.foreign_gpu1_reasons(
        snapshot,
        authorized_mps_pids=frozenset(),
        managed_workload_pids=managed_nvml,
        managed_systemd_claims=managed_claims,
    ) == (f"foreign CUDA PID(s): {workload_pid},{compiler_pid}",)

    mps_nvml, mps_clients, _claims = (
        session.SessionController._managed_workload_authority(
            status,
            snapshot,
            authorized_mps_client_pids=frozenset(
                {workload_pid, compiler_pid}
            ),
        )
    )
    assert mps_nvml == mps_clients == frozenset(
        {workload_pid, compiler_pid}
    )


@pytest.mark.parametrize("fault", ("reserved", "suspect", "owner"))
def test_controller_rejects_nonactive_backend_lease_even_without_cuda_pid(
    fault: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = backend_residency_record(
        status=fault if fault in {"reserved", "suspect"} else "active"
    )
    if fault == "owner":
        record["owner_pid"] = 6102
    status = {"leases": [record]}
    snapshot = session.TargetSnapshot((), (backend_docker_claim(),), ())
    patch_backend_identity(monkeypatch)

    with pytest.raises(session.DevGpuSessionError, match="exact active Docker"):
        session.SessionController._managed_workload_authority(status, snapshot)


def test_controller_backend_authority_requires_mps_client_for_nvml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {"leases": [backend_residency_record()]}
    claim = backend_docker_claim()
    snapshot = session.TargetSnapshot((6101,), (claim,), ())
    patch_backend_identity(monkeypatch)

    direct_nvml, managed_clients, _claims = (
        session.SessionController._managed_workload_authority(status, snapshot)
    )
    assert direct_nvml == frozenset()
    assert managed_clients == frozenset({6101})
    mps_nvml, mps_clients, _claims = (
        session.SessionController._managed_workload_authority(
            status,
            snapshot,
            authorized_mps_client_pids=frozenset({6101}),
        )
    )
    assert mps_nvml == mps_clients == frozenset({6101})

    idle = session.TargetSnapshot((), (claim,), ())
    assert session.SessionController._managed_workload_authority(
        {"leases": []},
        idle,
    ) == (frozenset(), frozenset(), frozenset())
    assert session.foreign_gpu1_reasons(
        idle,
        authorized_mps_pids=frozenset(),
    ) == ()


def test_fixed_controller_python_has_required_pidfd_contract() -> None:
    completed = subprocess.run(
        [
            "/usr/bin/python3",
            "-I",
            "-c",
            "import os,signal; assert callable(os.pidfd_open); assert callable(signal.pidfd_send_signal)",
        ],
        check=False,
    )

    assert completed.returncode == 0


def test_gpu1_only_broker_policy_is_process_local() -> None:
    program = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from ops.gpu_broker.broker import DEVICE_POLICY; "
        "print(','.join(map(str, DEVICE_POLICY[('dev','dft')])))"
    )
    base_environment = {"PATH": "/usr/local/bin:/usr/bin:/bin"}
    ordinary = subprocess.run(
        ["/usr/bin/python3", "-I", "-c", program, str(REPOSITORY_ROOT)],
        env=base_environment,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    restricted = subprocess.run(
        ["/usr/bin/python3", "-I", "-c", program, str(REPOSITORY_ROOT)],
        env={**base_environment, "NEXPOLY_GPU1_ONLY_SESSION": "1"},
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )

    assert ordinary.stdout.strip() == "1,3"
    assert restricted.stdout.strip() == "1"


def test_exact_nexpoly_backend_claim_is_not_classified_as_foreign() -> None:
    claim = SimpleNamespace(
        container_id="a" * 64,
        registration_id="backend-dev",
        component="backend",
        environment="dev",
        compose_project="nexpoly_dev",
        compose_service="backend",
        gpu_uuids=frozenset({session.GPU_UUID}),
    )
    snapshot = session.TargetSnapshot((111,), (claim,), ())

    assert session.foreign_gpu1_reasons(
        snapshot,
        authorized_mps_pids=frozenset({111}),
    ) == ()


def test_unknown_mps_client_blocks_owned_cleanup_without_kill(tmp_path, monkeypatch) -> None:
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.broker = SimpleNamespace(terminate=lambda: pytest.fail("broker must stay up"))
    controller.mps_started = True
    client = SimpleNamespace(
        set_draining=lambda _value: {"draining": True, "leases": []}
    )
    monkeypatch.setattr(
        controller,
        "_mps_command",
        lambda _action: (_ for _ in ()).throw(
            session.DevGpuSessionError("MPS still has active clients")
        ),
    )

    with pytest.raises(session.DevGpuSessionError, match="active clients"):
        controller._cleanup(client)


def test_mps_control_is_hard_pinned_to_gpu1(tmp_path, monkeypatch) -> None:
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.root_fd = controller.reservations_fd = 10
    controller.slot_fd = controller.pipe_fd = controller.log_fd = 11
    observed: list[tuple[str, ...]] = []

    def run(command, **_kwargs):
        observed.append(tuple(command))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(session.subprocess, "run", run)
    monkeypatch.setattr(session, "process_start_ticks", lambda _pid: 123)
    controller._mps_command("start")

    assert observed == [(str(session.MPS_CONTROL), "start", "1")]
    assert "3" not in observed[0]


def test_broker_snapshot_retries_across_a_legitimate_lease_transition() -> None:
    empty = {
        "broker_instance_id": "broker",
        "next_fencing_token": 1,
        "draining": False,
        "leases": [],
    }
    lease = {
        "lease_id": "lease-1",
        "fencing_token": 1,
        "gpu_uuid": session.GPU_UUID,
        "owner_pid": 77,
        "workload_pid": 88,
        "status": "active",
    }
    active = {**empty, "next_fencing_token": 2, "leases": [lease]}
    statuses = iter((empty, active, active, active))
    client = SimpleNamespace(status=lambda: next(statuses))
    snapshots = iter((empty_snapshot(), session.TargetSnapshot((88,), (), ())))

    status, snapshot = session.consistent_broker_snapshot(
        client, lambda: next(snapshots)
    )

    assert status == active
    assert snapshot.process_pids == (88,)


def test_broker_snapshot_retries_acquire_release_aba_after_inventory_error() -> None:
    from ops.gpu_broker.server import SystemdProcessDisappeared
    from gpu_resource.transient_scope import scope_control_group

    dft_lease = dft_residency_record()
    before = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [dft_lease],
    }
    issued = md_execution_record()
    heartbeating_dft_lease = {**dft_lease, "heartbeat_at": 7.0}
    after = {
        **before,
        "next_fencing_token": 3,
        "last_released_lease": issued,
        "leases": [heartbeating_dft_lease],
    }
    statuses = iter((before, after, after, after))
    client = SimpleNamespace(status=lambda: next(statuses))
    calls = 0

    def collect() -> session.TargetSnapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SystemdProcessDisappeared(
                76743,
                790,
                scope_control_group(issued["lease_id"], uid=1001),
            )
        return empty_snapshot()

    status, snapshot = session.consistent_broker_snapshot(client, collect)

    assert status == after
    assert snapshot == empty_snapshot()
    assert calls == 2


def test_broker_snapshot_retries_new_md_lease_visible_before_release() -> None:
    from gpu_resource.transient_scope import scope_control_group
    from ops.gpu_broker.server import SystemdProcessDisappeared

    dft_lease = dft_residency_record()
    md_lease = md_execution_record()
    before = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [dft_lease],
    }
    active = {
        **before,
        "next_fencing_token": 3,
        "leases": [{**dft_lease, "heartbeat_at": 7.0}, md_lease],
    }
    released = {
        **active,
        "last_released_lease": md_lease,
        "leases": [{**dft_lease, "heartbeat_at": 8.0}],
    }
    statuses = iter((before, active, active, released, released, released))
    client = SimpleNamespace(status=lambda: next(statuses))
    calls = 0

    def collect() -> session.TargetSnapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SystemdProcessDisappeared(
                156657,
                790,
                scope_control_group(md_lease["lease_id"], uid=1001),
            )
        return empty_snapshot()

    status, snapshot = session.consistent_broker_snapshot(client, collect)

    assert status == released
    assert snapshot == empty_snapshot()
    assert calls == 3


@pytest.mark.parametrize("direction", ("enter", "exit", "both"))
def test_broker_snapshot_retries_structured_md_membership_aba(
    direction: str,
) -> None:
    from gpu_resource.transient_scope import (
        scope_control_group,
        user_manager_control_group,
    )
    from ops.gpu_broker.server import SystemdMembershipChanged

    dft_lease = dft_residency_record()
    md_lease = md_execution_record()
    md_cgroup = scope_control_group(md_lease["lease_id"], uid=1001)
    before = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [dft_lease],
    }
    active = {
        **before,
        "next_fencing_token": 3,
        "leases": [{**dft_lease, "heartbeat_at": 7.0}, md_lease],
    }
    released = {
        **active,
        "last_released_lease": md_lease,
        "leases": [{**dft_lease, "heartbeat_at": 8.0}],
    }
    statuses = iter((before, active, active, released, released, released))
    client = SimpleNamespace(status=lambda: next(statuses))
    calls = 0
    manager_cgroup = user_manager_control_group(1001)
    stable = (7000, 100, manager_cgroup)
    expected = (stable, (193345, 790, md_cgroup))
    current = (stable,)
    if direction == "enter":
        expected, current = current, expected
    elif direction == "both":
        current = (stable, (193346, 791, md_cgroup))

    def collect() -> session.TargetSnapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SystemdMembershipChanged(
                "system",
                "user@1001.service",
                manager_cgroup,
                expected,
                current,
            )
        return empty_snapshot()

    status, snapshot = session.consistent_broker_snapshot(client, collect)

    assert status == released
    assert snapshot == empty_snapshot()
    assert calls == 3


def test_broker_snapshot_retries_released_md_membership_aba() -> None:
    from gpu_resource.transient_scope import (
        scope_control_group,
        user_manager_control_group,
    )
    from ops.gpu_broker.server import SystemdMembershipChanged

    dft_lease = dft_residency_record()
    md_lease = md_execution_record()
    md_cgroup = scope_control_group(md_lease["lease_id"], uid=1001)
    before = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [dft_lease],
    }
    after = {
        **before,
        "next_fencing_token": 3,
        "last_released_lease": md_lease,
        "leases": [{**dft_lease, "heartbeat_at": 7.0}],
    }
    statuses = iter((before, after, after, after))
    client = SimpleNamespace(status=lambda: next(statuses))
    calls = 0
    manager_cgroup = user_manager_control_group(1001)
    stable = (7000, 100, manager_cgroup)

    def collect() -> session.TargetSnapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SystemdMembershipChanged(
                "system",
                "user@1001.service",
                manager_cgroup,
                (stable, (193345, 790, md_cgroup)),
                (stable,),
            )
        return empty_snapshot()

    status, snapshot = session.consistent_broker_snapshot(client, collect)

    assert status == after
    assert snapshot == empty_snapshot()
    assert calls == 2


def test_broker_snapshot_retries_visible_md_release() -> None:
    from gpu_resource.transient_scope import scope_control_group
    from ops.gpu_broker.server import SystemdProcessDisappeared

    dft_lease = dft_residency_record()
    md_lease = md_execution_record()
    active = {
        "broker_instance_id": "broker",
        "next_fencing_token": 3,
        "draining": False,
        "quarantined_gpus": {},
        "last_released_lease": None,
        "leases": [dft_lease, md_lease],
    }
    released = {
        **active,
        "last_released_lease": md_lease,
        "leases": [{**dft_lease, "heartbeat_at": 7.0}],
    }
    statuses = iter((active, released, released, released))
    client = SimpleNamespace(status=lambda: next(statuses))
    calls = 0

    def collect() -> session.TargetSnapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SystemdProcessDisappeared(
                156657,
                790,
                scope_control_group(md_lease["lease_id"], uid=1001),
            )
        return empty_snapshot()

    status, snapshot = session.consistent_broker_snapshot(client, collect)

    assert status == released
    assert snapshot == empty_snapshot()
    assert calls == 2


@pytest.mark.parametrize(
    "mutation",
    (
        "stale_tombstone",
        "tombstone_live_collision",
        "wrong_tombstone_instance",
        "invalid_tombstone_token",
        "existing_lease_change",
        "multiple_removals",
    ),
)
def test_visible_md_release_rejects_nonexclusive_transition(
    mutation: str,
) -> None:
    from gpu_resource.transient_scope import scope_control_group
    from ops.gpu_broker.server import SystemdProcessDisappeared

    dft_lease = dft_residency_record()
    md_lease = md_execution_record()
    active = {
        "broker_instance_id": "broker",
        "next_fencing_token": 3,
        "draining": False,
        "quarantined_gpus": {},
        "last_released_lease": None,
        "leases": [dft_lease, md_lease],
    }
    released = {
        **active,
        "last_released_lease": md_lease,
        "leases": [{**dft_lease, "heartbeat_at": 7.0}],
    }
    if mutation == "stale_tombstone":
        active["last_released_lease"] = {**md_lease, "heartbeat_at": 1.0}
    elif mutation == "tombstone_live_collision":
        released["last_released_lease"] = dft_lease
    elif mutation == "wrong_tombstone_instance":
        released["last_released_lease"] = {
            **md_lease,
            "broker_instance_id": "other-broker",
        }
    elif mutation == "invalid_tombstone_token":
        released["last_released_lease"] = {
            **md_lease,
            "fencing_token": 3,
        }
    elif mutation == "existing_lease_change":
        released["leases"] = [
            {
                **dft_lease,
                "heartbeat_at": 7.0,
                "request_id": "dft:dev:changed",
            }
        ]
    else:
        extra = md_execution_record(
            lease_id="f3" * 16,
            fencing_token=3,
            workload_pid=156658,
            workload_start_ticks=791,
        )
        active.update(next_fencing_token=4, leases=[dft_lease, md_lease, extra])
        released["next_fencing_token"] = 4

    statuses = iter((active, released))
    client = SimpleNamespace(status=lambda: next(statuses))
    error = SystemdProcessDisappeared(
        156657,
        790,
        scope_control_group(md_lease["lease_id"], uid=1001),
    )
    calls = 0

    def collect() -> session.TargetSnapshot:
        nonlocal calls
        calls += 1
        raise error

    with pytest.raises(SystemdProcessDisappeared) as raised:
        session.consistent_broker_snapshot(client, collect)

    assert raised.value is error
    assert calls == 1


@pytest.mark.parametrize(
    "fault",
    (
        "foreign",
        "pid_reuse",
        "wrong_unit",
        "malformed_cgroup",
        "outside_unchanged",
        "unchanged",
    ),
)
def test_md_transition_never_hides_unproven_membership_churn(fault: str) -> None:
    from gpu_resource.transient_scope import (
        scope_control_group,
        user_manager_control_group,
    )
    from ops.gpu_broker.server import SystemdMembershipChanged

    dft_lease = dft_residency_record()
    md_lease = md_execution_record()
    md_cgroup = scope_control_group(md_lease["lease_id"], uid=1001)
    before = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [dft_lease],
    }
    active = {
        **before,
        "next_fencing_token": 3,
        "leases": [dft_lease, md_lease],
    }
    statuses = iter((before, active))
    client = SimpleNamespace(status=lambda: next(statuses))
    expected = ((193345, 790, md_cgroup),)
    current: tuple[tuple[int, int, str], ...] = ()
    unit = "user@1001.service"
    if fault == "foreign":
        expected += ((90001, 123, "/user.slice/foreign.scope"),)
    elif fault == "pid_reuse":
        current = ((193345, 791, md_cgroup),)
    elif fault == "wrong_unit":
        unit = "foreign.service"
    elif fault == "malformed_cgroup":
        expected = ((193345, 790, f"{md_cgroup}/../foreign.scope"),)
    elif fault == "outside_unchanged":
        outside = (90001, 123, "/system.slice/foreign.service")
        expected = (*expected, outside)
        current = (outside,)
    else:
        current = expected

    with pytest.raises(SystemdMembershipChanged, match="membership changed"):
        session.consistent_broker_snapshot(
            client,
            lambda: (_ for _ in ()).throw(
                SystemdMembershipChanged(
                    "system",
                    unit,
                    user_manager_control_group(1001),
                    expected,
                    current,
                )
            ),
        )


def test_visible_md_transition_remains_bounded_by_authority_attempts() -> None:
    from gpu_resource.transient_scope import scope_control_group
    from ops.gpu_broker.server import SystemdProcessDisappeared

    dft_lease = dft_residency_record()
    md_lease = md_execution_record()
    before = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [dft_lease],
    }
    active = {
        **before,
        "next_fencing_token": 3,
        "leases": [dft_lease, md_lease],
    }
    statuses = iter((before, active))
    client = SimpleNamespace(status=lambda: next(statuses))

    with pytest.raises(
        session.DevGpuSessionError,
        match="authority changed throughout",
    ):
        session.consistent_broker_snapshot(
            client,
            lambda: (_ for _ in ()).throw(
                SystemdProcessDisappeared(
                    156657,
                    790,
                    scope_control_group(md_lease["lease_id"], uid=1001),
                )
            ),
            attempts=1,
        )


def test_visible_md_issue_never_hides_an_unrelated_live_lease_change() -> None:
    from gpu_resource.transient_scope import scope_control_group
    from ops.gpu_broker.server import SystemdProcessDisappeared

    dft_lease = dft_residency_record()
    md_lease = md_execution_record()
    before = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [dft_lease],
    }
    after = {
        **before,
        "next_fencing_token": 3,
        "leases": [md_lease],
    }
    statuses = iter((before, after))
    client = SimpleNamespace(status=lambda: next(statuses))
    calls = 0

    def collect() -> session.TargetSnapshot:
        nonlocal calls
        calls += 1
        raise SystemdProcessDisappeared(
            156657,
            790,
            scope_control_group(md_lease["lease_id"], uid=1001),
        )

    with pytest.raises(SystemdProcessDisappeared, match="disappeared during audit"):
        session.consistent_broker_snapshot(client, collect)

    assert calls == 1


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("broker_instance_id", "other-broker"),
        ("request_id", "changed-request"),
        ("memory_mib", 8192),
        ("thread_percent", 99),
        ("created_at", 99.0),
        ("mps_terminated_client_pids", [999]),
        ("mps_termination_at", 3.0),
    ),
)
def test_visible_md_issue_allows_only_existing_lease_heartbeat_change(
    field: str,
    value: object,
) -> None:
    from gpu_resource.transient_scope import scope_control_group
    from ops.gpu_broker.server import SystemdProcessDisappeared

    dft_lease = dft_residency_record()
    changed_dft = {**dft_lease, "heartbeat_at": 7.0, field: value}
    md_lease = md_execution_record()
    before = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [dft_lease],
    }
    after = {
        **before,
        "next_fencing_token": 3,
        "leases": [changed_dft, md_lease],
    }
    statuses = iter((before, after))
    client = SimpleNamespace(status=lambda: next(statuses))
    calls = 0

    def collect() -> session.TargetSnapshot:
        nonlocal calls
        calls += 1
        raise SystemdProcessDisappeared(
            156657,
            790,
            scope_control_group(md_lease["lease_id"], uid=1001),
        )

    with pytest.raises(SystemdProcessDisappeared, match="disappeared during audit"):
        session.consistent_broker_snapshot(client, collect)

    assert calls == 1


def test_visible_md_issue_never_attributes_a_foreign_cgroup() -> None:
    from ops.gpu_broker.server import SystemdProcessDisappeared

    dft_lease = dft_residency_record()
    before = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [dft_lease],
    }
    after = {
        **before,
        "next_fencing_token": 3,
        "leases": [dft_lease, md_execution_record()],
    }
    statuses = iter((before, after))
    client = SimpleNamespace(status=lambda: next(statuses))

    with pytest.raises(SystemdProcessDisappeared, match="disappeared during audit"):
        session.consistent_broker_snapshot(
            client,
            lambda: (_ for _ in ()).throw(
                SystemdProcessDisappeared(
                    90001,
                    123,
                    "/user.slice/foreign.scope",
                )
            ),
        )


@pytest.mark.parametrize("duplicate_field", ("lease_id", "fencing_token"))
def test_visible_md_issue_rejects_duplicate_lease_authority(
    duplicate_field: str,
) -> None:
    from gpu_resource.transient_scope import scope_control_group
    from ops.gpu_broker.server import SystemdProcessDisappeared

    dft_lease = dft_residency_record()
    md_lease = md_execution_record()
    duplicate = (
        dict(md_lease)
        if duplicate_field == "lease_id"
        else md_execution_record(
            lease_id="f3" * 16,
            fencing_token=int(md_lease["fencing_token"]),
            workload_pid=156658,
            workload_start_ticks=791,
        )
    )
    before = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [dft_lease],
    }
    after = {
        **before,
        "next_fencing_token": 3,
        "leases": [dft_lease, md_lease, duplicate],
    }
    statuses = iter((before, after))
    client = SimpleNamespace(status=lambda: next(statuses))

    with pytest.raises(SystemdProcessDisappeared, match="disappeared during audit"):
        session.consistent_broker_snapshot(
            client,
            lambda: (_ for _ in ()).throw(
                SystemdProcessDisappeared(
                    156657,
                    790,
                    scope_control_group(md_lease["lease_id"], uid=1001),
                )
            ),
        )


@pytest.mark.parametrize("mutation", ("generation", "instance", "drain", "quarantine", "non_exact"))
def test_visible_md_issue_rejects_noncanonical_transition(mutation: str) -> None:
    from gpu_resource.transient_scope import scope_control_group
    from ops.gpu_broker.server import SystemdProcessDisappeared

    dft_lease = dft_residency_record()
    md_lease = md_execution_record()
    before = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [dft_lease],
    }
    after = {
        **before,
        "next_fencing_token": 3,
        "leases": [dft_lease, md_lease],
    }
    if mutation == "generation":
        second = md_execution_record(
            lease_id="f3" * 16,
            fencing_token=3,
            workload_pid=156658,
            workload_start_ticks=791,
        )
        after.update(next_fencing_token=4, leases=[dft_lease, md_lease, second])
    elif mutation == "instance":
        after["broker_instance_id"] = "other-broker"
    elif mutation == "drain":
        after["draining"] = True
    elif mutation == "quarantine":
        after["quarantined_gpus"] = {session.GPU_UUID: {"reason": "gpu_xid"}}
    else:
        after["leases"] = [dft_lease, {**md_lease, "placement": "preferred"}]
    statuses = iter((before, after))
    client = SimpleNamespace(status=lambda: next(statuses))

    with pytest.raises(SystemdProcessDisappeared, match="disappeared during audit"):
        session.consistent_broker_snapshot(
            client,
            lambda: (_ for _ in ()).throw(
                SystemdProcessDisappeared(
                    156657,
                    790,
                    scope_control_group(md_lease["lease_id"], uid=1001),
                )
            ),
        )


def test_broker_snapshot_never_hides_generic_foreign_error_during_aba() -> None:
    from ops.gpu_broker.broker import BrokerError

    before = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [],
    }
    after = {
        **before,
        "next_fencing_token": 3,
        "last_released_lease": md_execution_record(),
    }
    statuses = iter((before, after))
    client = SimpleNamespace(status=lambda: next(statuses))
    calls = 0

    def collect() -> session.TargetSnapshot:
        nonlocal calls
        calls += 1
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            "cannot identify foreign systemd process PID 90001",
        )

    with pytest.raises(BrokerError, match="foreign systemd process"):
        session.consistent_broker_snapshot(client, collect)

    assert calls == 1


def test_broker_snapshot_never_attributes_foreign_scope_to_matching_md_aba() -> None:
    from ops.gpu_broker.server import SystemdProcessDisappeared

    before = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [],
    }
    after = {
        **before,
        "next_fencing_token": 3,
        "last_released_lease": md_execution_record(),
    }
    statuses = iter((before, after))
    client = SimpleNamespace(status=lambda: next(statuses))
    calls = 0

    def collect() -> session.TargetSnapshot:
        nonlocal calls
        calls += 1
        raise SystemdProcessDisappeared(
            90001,
            123,
            "/user.slice/foreign.scope",
        )

    with pytest.raises(SystemdProcessDisappeared, match="disappeared during audit"):
        session.consistent_broker_snapshot(client, collect)

    assert calls == 1


def test_broker_snapshot_resamples_successful_acquire_release_aba() -> None:
    before = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [],
    }
    after = {
        **before,
        "next_fencing_token": 3,
        "last_released_lease": md_execution_record(),
    }
    statuses = iter((before, after, after, after))
    client = SimpleNamespace(status=lambda: next(statuses))
    snapshots = iter(
        (
            session.TargetSnapshot((76743,), (), ()),
            empty_snapshot(),
        )
    )

    status, snapshot = session.consistent_broker_snapshot(
        client,
        lambda: next(snapshots),
    )

    assert status == after
    assert snapshot == empty_snapshot()


@pytest.mark.parametrize("generation", (None, True, 0, "2"))
def test_broker_authority_token_rejects_invalid_generation(generation: object) -> None:
    status = {
        "broker_instance_id": "broker",
        "draining": False,
        "leases": [],
    }
    if generation is not None:
        status["next_fencing_token"] = generation

    with pytest.raises(
        session.DevGpuSessionError,
        match="invalid fencing authority generation",
    ):
        session.broker_authority_token(status)


@pytest.mark.parametrize(
    "field",
    (
        "lease_id",
        "fencing_token",
        "kind",
        "placement",
        "preferred",
        "component",
        "environment",
        "client_id",
        "gpu_index",
        "gpu_uuid",
        "parent_lease_id",
        "status",
        "mps_termination_status",
        "owner_pid",
        "owner_process_start_ticks",
        "owner_boot_id",
        "workload_pid",
        "workload_process_start_ticks",
        "workload_process_group_id",
        "workload_cgroup",
    ),
)
def test_broker_authority_token_binds_exact_dft_matcher_fields(field: str) -> None:
    lease = dft_residency_record()
    before = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [lease],
    }
    changed_lease = dict(lease)
    changed_lease[field] = "changed"
    after = {**before, "leases": [changed_lease]}

    assert session.broker_authority_token(before) != session.broker_authority_token(
        after
    )


@pytest.mark.parametrize("scope", ("system", "user"))
def test_broker_snapshot_resamples_exact_dft_membership_churn(scope: str) -> None:
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    client = SimpleNamespace(status=lambda: status)
    calls = 0

    def collect() -> session.TargetSnapshot:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise dft_membership_error(scope=scope)
        return empty_snapshot()

    observed_status, snapshot = session.consistent_broker_snapshot(
        client,
        collect,
        membership_churn_retries=2,
    )

    assert observed_status == status
    assert snapshot == empty_snapshot()
    assert calls == 3


def test_broker_snapshot_fails_closed_after_bounded_dft_churn() -> None:
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    client = SimpleNamespace(status=lambda: status)
    calls = 0

    def collect() -> session.TargetSnapshot:
        nonlocal calls
        calls += 1
        raise dft_membership_error()

    with pytest.raises(
        session.DevGpuSessionError,
        match="exact DFT residency membership remained unstable",
    ):
        session.consistent_broker_snapshot(
            client,
            collect,
            membership_churn_retries=1,
        )

    assert calls == 2


def test_broker_snapshot_fails_closed_at_dft_churn_deadline() -> None:
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    client = SimpleNamespace(status=lambda: status)
    calls = 0

    def collect() -> session.TargetSnapshot:
        nonlocal calls
        calls += 1
        raise dft_membership_error()

    ticks = iter((0.0, 13.0))
    with pytest.raises(
        session.DevGpuSessionError,
        match="exact DFT residency membership remained unstable",
    ):
        session.consistent_broker_snapshot(
            client,
            collect,
            membership_churn_retries=8,
            membership_churn_timeout_seconds=12.0,
            monotonic=lambda: next(ticks),
        )

    assert calls == 1


def test_broker_snapshot_never_retries_unknown_membership_churn() -> None:
    from ops.gpu_broker.broker import BrokerError

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    client = SimpleNamespace(status=lambda: status)
    calls = 0

    def collect() -> session.TargetSnapshot:
        nonlocal calls
        calls += 1
        raise BrokerError(
            "gpu_claim_inventory_changed",
            "system systemd unit foreign.service membership changed during audit",
        )

    with pytest.raises(BrokerError, match="foreign.service"):
        session.consistent_broker_snapshot(client, collect)

    assert calls == 1


@pytest.mark.parametrize(
    "fault",
    (
        "generic",
        "foreign",
        "mixed",
        "pid_reuse",
        "wrong_control_group",
        "wrong_unit",
        "malformed_cgroup",
        "prefix_collision",
        "duplicate_pid",
        "outside_unchanged",
        "empty_delta",
    ),
)
def test_broker_snapshot_never_retries_unproven_dft_membership_churn(
    fault: str,
) -> None:
    from gpu_resource.transient_scope import (
        scope_control_group,
        user_manager_control_group,
    )
    from ops.gpu_broker.broker import BrokerError

    dft_cgroup = scope_control_group("d1" * 16, uid=1001)
    manager_cgroup = user_manager_control_group(1001)
    stable = (7000, 100, manager_cgroup)
    exact = (8124, 457, dft_cgroup)
    foreign = (90001, 123, f"{manager_cgroup}/foreign.scope")
    error: Exception
    if fault == "generic":
        error = BrokerError(
            "gpu_claim_inventory_changed",
            "system systemd unit user@1001.service membership changed during audit",
        )
    elif fault == "foreign":
        error = dft_membership_error(expected=(stable, foreign), current=(stable,))
    elif fault == "mixed":
        error = dft_membership_error(
            expected=(stable, exact, foreign),
            current=(stable,),
        )
    elif fault == "pid_reuse":
        error = dft_membership_error(
            expected=(stable, exact),
            current=(stable, (8124, 458, dft_cgroup)),
        )
    elif fault == "wrong_control_group":
        error = dft_membership_error(control_group=dft_cgroup)
    elif fault == "wrong_unit":
        error = dft_membership_error(unit="foreign.service")
    elif fault == "malformed_cgroup":
        error = dft_membership_error(
            expected=(stable, (8124, 457, f"{dft_cgroup}/../foreign.scope")),
            current=(stable,),
        )
    elif fault == "prefix_collision":
        error = dft_membership_error(
            expected=(stable, (8124, 457, f"{dft_cgroup}-foreign.scope")),
            current=(stable,),
        )
    elif fault == "duplicate_pid":
        error = dft_membership_error(
            expected=(stable, exact, (8124, 458, dft_cgroup)),
            current=(stable,),
        )
    elif fault == "outside_unchanged":
        outside = (90002, 124, "/system.slice/foreign.service")
        error = dft_membership_error(
            expected=(stable, outside, exact),
            current=(stable, outside),
        )
    else:
        error = dft_membership_error(expected=(stable,), current=(stable,))

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    client = SimpleNamespace(status=lambda: status)
    calls = 0

    def collect() -> session.TargetSnapshot:
        nonlocal calls
        calls += 1
        raise error

    with pytest.raises(BrokerError, match="membership changed"):
        session.consistent_broker_snapshot(client, collect)

    assert calls == 1


def test_gpu1_default_compute_mode_inventory_is_strict() -> None:
    def result(stdout: str) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    session.require_gpu1_default_compute_mode(
        run=lambda *_args, **_kwargs: result(
            f"{session.GPU_UUID}, Default\n"
        )
    )
    with pytest.raises(session.DevGpuSessionError, match="Default compute mode"):
        session.require_gpu1_default_compute_mode(
            run=lambda *_args, **_kwargs: result(
                f"{session.GPU_UUID}, Exclusive_Process\n"
            )
        )


def test_exact_dft_churn_inner_budget_survives_more_than_three_transitions() -> None:
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    client = SimpleNamespace(status=lambda: status)
    calls = 0
    guarded: list[dict[str, object]] = []

    def collect() -> session.TargetSnapshot:
        nonlocal calls
        calls += 1
        if calls <= 5:
            raise dft_membership_error()
        return empty_snapshot()

    observed, snapshot = session.consistent_broker_snapshot(
        client,
        collect,
        membership_churn_retries=6,
        membership_churn_timeout_seconds=90,
        membership_churn_guard=lambda value: guarded.append(value) or True,
    )

    assert observed == status
    assert snapshot == empty_snapshot()
    assert calls == 6
    assert guarded == [status] * 5


def test_fast_dft_guard_accepts_stable_exact_children_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    authority = mps_snapshot(
        server_pids=frozenset({7001}),
        client_pids=frozenset({8123}),
        declarers=frozenset(
            {mps_declarer(7000), mps_declarer(7001, 101)}
        ),
    )
    patch_fast_guard_runtime(
        monkeypatch,
        controller,
        authorities=(authority, authority),
        compute=(frozenset({7001, 8123}), frozenset({7001, 8123})),
    )
    states: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        controller,
        "_state",
        lambda value, **extra: states.append((value, extra)),
    )

    assert controller._fast_dft_churn_guard(
        SimpleNamespace(status=lambda: status),
        status,
    ) is True
    assert controller.fast_audit_sequence == 1
    assert states[-1][0] == "stabilizing"
    assert states[-1][1]["mps_membership_changed"] is False


def test_fast_dft_guard_accepts_exact_lazy_server_and_client_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    control = mps_declarer(7000)
    before = mps_snapshot(declarers=frozenset({control}))
    after = mps_snapshot(
        server_pids=frozenset({7001}),
        client_pids=frozenset({8123}),
        declarers=frozenset({control, mps_declarer(7001, 101)}),
    )
    # The lazy server can enter NVML after the first authority snapshot.  The
    # second exact authority is allowed to explain only that one PID.
    patch_fast_guard_runtime(
        monkeypatch,
        controller,
        authorities=(before, after),
        compute=(frozenset({7001}), frozenset({7001, 8123})),
    )
    states: list[dict[str, object]] = []
    monkeypatch.setattr(
        controller,
        "_state",
        lambda _value, **extra: states.append(extra),
    )

    assert controller._fast_dft_churn_guard(
        SimpleNamespace(status=lambda: status),
        status,
    ) is True
    assert states[-1]["mps_membership_changed"] is True


@pytest.mark.parametrize("identity_fault", ("start_ticks", "cgroup"))
def test_fast_dft_guard_never_retroactively_exempts_reused_server_pid(
    identity_fault: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    control = mps_declarer(7000)
    server = mps_declarer(7001, 101)
    before = mps_snapshot(declarers=frozenset({control}))
    after = mps_snapshot(
        server_pids=frozenset({7001}),
        client_pids=frozenset({8123}),
        declarers=frozenset({control, server}),
    )
    reused_server = (
        mps_declarer(7001, 999)
        if identity_fault == "start_ticks"
        else replace(server, process_cgroup="/user.slice/foreign.scope")
    )
    patch_fast_guard_runtime(
        monkeypatch,
        controller,
        authorities=(before, after),
        compute=(frozenset({7001}), frozenset({7001, 8123})),
        compute_declarers=(
            {7001: reused_server},
            {7001: server},
        ),
    )

    with pytest.raises(session.DevGpuSessionError, match="foreign PID 7001"):
        controller._fast_dft_churn_guard(
            SimpleNamespace(status=lambda: status),
            status,
        )


@pytest.mark.parametrize("foreign_source", ("compute", "mps", "docker"))
def test_fast_dft_guard_fails_closed_for_foreign_authority(
    foreign_source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    client_pids = frozenset({9999}) if foreign_source == "mps" else frozenset()
    authority = mps_snapshot(
        server_pids=frozenset({7001}),
        client_pids=client_pids,
        declarers=frozenset(
            {mps_declarer(7000), mps_declarer(7001, 101)}
        ),
    )
    compute = (
        frozenset({7001, 9999})
        if foreign_source == "compute"
        else frozenset({7001})
    )
    claim = SimpleNamespace(gpu_uuids=frozenset({session.GPU_UUID}))
    docker = ((claim,), (claim,)) if foreign_source == "docker" else ((), ())
    patch_fast_guard_runtime(
        monkeypatch,
        controller,
        authorities=(authority, authority),
        compute=(compute, compute),
        docker=docker,
    )

    with pytest.raises(session.DevGpuSessionError):
        controller._fast_dft_churn_guard(
            SimpleNamespace(status=lambda: status),
            status,
        )


def test_fast_dft_guard_rejects_duplicate_lease_and_90_second_expiry(
    tmp_path: Path,
) -> None:
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    duplicate = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [
            dft_residency_record(),
            dft_residency_record(lease_id="d2" * 16),
        ],
    }
    with pytest.raises(session.DevGpuSessionError, match="one exact"):
        controller._fast_dft_churn_guard(
            SimpleNamespace(status=lambda: duplicate),
            duplicate,
        )

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    controller.dft_churn_started_at = (
        session.time.monotonic()
        - session.DFT_WARMUP_CHURN_TIMEOUT_SECONDS
        - 0.1
    )
    with pytest.raises(session.DevGpuSessionError, match="within 90 seconds"):
        controller._fast_dft_churn_guard(
            SimpleNamespace(status=lambda: status),
            status,
        )


def test_warmup_retries_only_internal_mps_membership_cas_until_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.gpu_broker.broker import BrokerError

    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    authority = dft_resident_authority()
    calls = 0

    def snapshot():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise BrokerError(
                "mps_authority_changed",
                "lazy server appeared",
            )
        return authority

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    states: list[str] = []
    monkeypatch.setattr(controller, "_mps_authority", snapshot)
    monkeypatch.setattr(
        controller,
        "_state",
        lambda value, **_extra: states.append(value),
    )
    monkeypatch.setattr(session.time, "sleep", lambda _seconds: None)

    assert controller._mps_authority_for_audit(
        SimpleNamespace(status=lambda: status)
    ) == authority
    assert calls == 2
    assert states == ["stabilizing"]


@pytest.mark.parametrize("failure", ("identity", "foreign", "expired"))
def test_warmup_never_retries_unproven_or_expired_mps_churn(
    failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.gpu_broker.broker import BrokerError

    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    calls = 0

    def snapshot():
        nonlocal calls
        calls += 1
        raise BrokerError(
            (
                "mps_control_unavailable"
                if failure == "identity"
                else "mps_authority_changed"
            ),
            "unstable MPS authority",
        )

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": (
            [] if failure == "foreign" else [dft_residency_record()]
        ),
    }
    if failure == "expired":
        controller.dft_churn_started_at = (
            session.time.monotonic()
            - session.DFT_WARMUP_CHURN_TIMEOUT_SECONDS
            - 0.1
        )
    monkeypatch.setattr(controller, "_mps_authority", snapshot)
    monkeypatch.setattr(session.time, "sleep", lambda _seconds: None)

    expected = (
        session.DevGpuSessionError
        if failure in {"foreign", "expired"}
        else BrokerError
    )
    with pytest.raises(expected):
        controller._mps_authority_for_audit(
            SimpleNamespace(status=lambda: status)
        )
    assert calls == 1


def test_full_audit_seals_only_one_live_exact_dft_residency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.dft_stabilization_generation = 1
    authority = dft_resident_authority()
    resident_snapshot = session.TargetSnapshot((7001,), (), ())
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=resident_snapshot,
        authority=authority,
        trailing_compute=frozenset({7001}),
    )
    monkeypatch.setattr(
        controller,
        "_exact_dft_descendants",
        lambda _status, pids: pids == frozenset({8123}),
    )

    controller._audit(client)

    assert controller.dft_stabilized is True
    assert controller.dft_warmup_open is False
    assert controller.full_audit_generation == 1
    assert controller.last_audit_stabilization_generation == 1


@pytest.mark.parametrize(
    "stabilization_generation",
    (
        pytest.param(1, id="completed-generation"),
        pytest.param(2, id="late-signal"),
    ),
)
def test_completed_dft_stabilization_is_not_resealed_during_backend_rollout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stabilization_generation: int,
) -> None:
    from gpu_resource.transient_scope import scope_control_group
    from ops.gpu_broker import server

    dft_record = dft_residency_record()
    backend_record = backend_residency_record()
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 5,
        "draining": False,
        "leases": [dft_record, backend_record],
    }
    claim = backend_docker_claim()
    authority = mps_snapshot(
        server_pids=frozenset({7001}),
        client_pids=frozenset({6101, 8123}),
        declarers=frozenset(
            {mps_declarer(7000), mps_declarer(7001, 101)}
        ),
    )
    snapshot = session.TargetSnapshot((7001,), (claim,), ())
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.plane_ready_published = True
    controller.dft_stabilization_generation = stabilization_generation
    controller.last_audit_stabilization_generation = 1
    controller.dft_stabilized = True
    controller.dft_warmup_open = False
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=snapshot,
        authority=authority,
        trailing_compute=frozenset({7001}),
        trailing_docker=(claim,),
    )
    dft_control_group = scope_control_group("d1" * 16, uid=1001)
    monkeypatch.setattr(server, "read_boot_id", lambda: "boot")
    monkeypatch.setattr(
        server,
        "read_process_start_ticks",
        lambda pid: 333 if pid == 6101 else 456,
    )
    monkeypatch.setattr(
        server,
        "_read_process_uids",
        lambda _pid: (1001, 1001, 1001, 1001),
    )
    monkeypatch.setattr(
        server.os,
        "getpgid",
        lambda pid: 6101 if pid == 6101 else 8123,
    )
    monkeypatch.setattr(
        server,
        "_read_unified_process_cgroup",
        lambda pid: (
            "/docker/backend.scope" if pid == 6101 else dft_control_group
        ),
    )
    monkeypatch.setattr(
        server,
        "_pid_is_or_descends_from",
        lambda pid, ancestor: pid == ancestor and pid in {6101, 8123},
    )

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons == ()
    assert controller.dft_stabilized is True
    assert controller.dft_warmup_open is False
    assert (
        controller.last_audit_stabilization_generation
        == stabilization_generation
    )
    assert controller.full_audit_generation == 1


def test_pending_dft_stabilization_still_requires_a_sole_residency_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from ops.gpu_broker.broker import Lease

    residency = Lease(**dft_residency_record())
    execution = replace(
        residency,
        lease_id="e1" * 16,
        fencing_token=2,
        request_id="dft:dev:execution:stabilization-race",
        kind="execution",
        parent_lease_id=residency.lease_id,
    )
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 3,
        "draining": False,
        "leases": [residency.public_dict(), execution.public_dict()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.dft_stabilization_generation = 1
    authority = dft_resident_authority()
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=session.TargetSnapshot((7001,), (), ()),
        authority=authority,
        trailing_compute=frozenset({7001}),
    )
    monkeypatch.setattr(
        controller,
        "_exact_dft_descendants",
        lambda _status, pids: pids == frozenset({8123}),
    )

    with pytest.raises(
        session.DevGpuSessionError,
        match="DFT stabilization encountered another Broker lease",
    ):
        controller._audit(client)

    assert controller.dft_stabilized is False
    assert controller.dft_warmup_open is True
    assert controller.last_audit_stabilization_generation == 0
    assert controller.full_audit_generation == 0


@pytest.mark.parametrize(
    ("authority", "snapshot", "trailing_compute", "message"),
    (
        (
            mps_snapshot(declarers=frozenset({mps_declarer(7000)})),
            empty_snapshot(),
            frozenset(),
            "lacks one descriptor-owned MPS server",
        ),
        (
            mps_snapshot(
                server_pids=frozenset({7001}),
                declarers=frozenset(
                    {mps_declarer(7000), mps_declarer(7001, 101)}
                ),
            ),
            session.TargetSnapshot((7001,), (), ()),
            frozenset({7001}),
            "root lacks an active private MPS client",
        ),
        (
            dft_resident_authority(),
            empty_snapshot(),
            frozenset(),
            "server is absent from the stable NVML inventory",
        ),
    ),
)
def test_full_audit_never_seals_without_real_dft_cuda_residency(
    authority: object,
    snapshot: session.TargetSnapshot,
    trailing_compute: frozenset[int],
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.dft_stabilization_generation = 1
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=snapshot,
        authority=authority,
        trailing_compute=trailing_compute,
    )
    monkeypatch.setattr(
        controller,
        "_exact_dft_descendants",
        lambda _status, pids: pids == frozenset({8123}),
    )

    with pytest.raises(session.DevGpuSessionError, match=message):
        controller._audit(client)

    assert controller.dft_stabilized is False


@pytest.mark.parametrize("identity_fault", ("start_ticks", "cgroup"))
def test_full_audit_never_retroactively_authorizes_reused_mps_server_pid(
    identity_fault: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.dft_stabilization_generation = 1
    original = dft_resident_authority()
    original_server = next(
        item for item in original.gpu_declarers if item.pid == 7001
    )
    replacement_server = replace(
        original_server,
        **(
            {"process_start_ticks": 999}
            if identity_fault == "start_ticks"
            else {"process_cgroup": "/user.slice/foreign.scope"}
        ),
    )
    replacement = replace(
        original,
        gpu_declarers=frozenset(
            replacement_server if item.pid == 7001 else item
            for item in original.gpu_declarers
        ),
    )
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=session.TargetSnapshot((7001,), (), ()),
        authority=original,
        trailing_compute=frozenset({7001}),
    )
    current_authority = [original]
    inventory_calls = 0

    def collect(_client, **_kwargs):
        nonlocal inventory_calls
        inventory_calls += 1
        # The old server dies after the enclosing authority read and its PID is
        # immediately reused by a different server identity before NVML.
        current_authority[0] = replacement
        return status, dft_broad_snapshot(
            monkeypatch,
            status,
            current_authority[0],
            session.TargetSnapshot((7001,), (), ()),
        )

    monkeypatch.setattr(session, "consistent_broker_snapshot", collect)
    monkeypatch.setattr(
        controller,
        "_mps_authority",
        lambda: current_authority[0],
    )
    monkeypatch.setattr(
        "ops.gpu_broker.server.query_systemd_gpu_claims",
        lambda **_kwargs: dft_broad_snapshot(
            monkeypatch,
            status,
            current_authority[0],
            session.TargetSnapshot((7001,), (), ()),
        ).systemd_claims,
    )
    monkeypatch.setattr(
        controller,
        "_exact_dft_descendants",
        lambda _status, pids: pids == frozenset({8123}),
    )

    _status, _snapshot, reasons = controller._audit(client)

    # A server identity replacement already present in the adjacent process
    # inventory is contamination evidence, not retryable DFT client growth.
    assert inventory_calls == 1
    assert reasons
    assert any("7001" in reason for reason in reasons)
    assert controller.dft_stabilized is False


def test_full_audit_never_seals_missing_or_stale_dft_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.dft_stabilization_generation = 1
    empty_status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [],
    }
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        empty_status,
    )
    with pytest.raises(session.DevGpuSessionError, match="one exact"):
        controller._audit(client)

    stale_status = {
        **empty_status,
        "leases": [dft_residency_record()],
    }
    stale = session.SessionController(run, "a" * 40, "b" * 40)
    stale.dft_stabilization_generation = 1
    authority = dft_resident_authority()
    stale_client = patch_full_audit_runtime(
        monkeypatch,
        stale,
        stale_status,
        snapshot=session.TargetSnapshot((7001,), (), ()),
        authority=authority,
        trailing_compute=frozenset({7001}),
    )
    monkeypatch.setattr(
        stale,
        "_exact_dft_descendants",
        lambda _status, _pids: False,
    )
    with pytest.raises(session.DevGpuSessionError, match="root died"):
        stale._audit(stale_client)


def test_stabilization_signal_mid_audit_requires_the_next_full_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    authority = dft_resident_authority()
    resident_snapshot = dft_broad_snapshot(
        monkeypatch,
        status,
        authority,
        session.TargetSnapshot((7001,), (), ()),
    )
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=resident_snapshot,
        authority=authority,
        trailing_compute=frozenset({7001}),
    )
    first = True

    def collect(_client, **_kwargs):
        nonlocal first
        if first:
            first = False
            controller.dft_stabilization_generation += 1
        return status, resident_snapshot

    monkeypatch.setattr(session, "consistent_broker_snapshot", collect)
    monkeypatch.setattr(
        controller,
        "_exact_dft_descendants",
        lambda _status, _pids: True,
    )

    controller._audit(client)
    assert controller.dft_stabilized is False
    assert controller.last_audit_stabilization_generation == 0

    controller._audit(client)
    assert controller.dft_stabilized is True
    assert controller.last_audit_stabilization_generation == 1
    assert controller.full_audit_generation == 2


def test_activation_signal_mid_audit_cannot_authorize_that_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.dft_warmup_open = False
    controller.dft_stabilized = True
    client = patch_full_audit_runtime(monkeypatch, controller, status)
    first = True

    def collect(_client, **_kwargs):
        nonlocal first
        if first:
            first = False
            controller.activation_generation += 1
        return status, empty_snapshot()

    monkeypatch.setattr(session, "consistent_broker_snapshot", collect)

    controller._audit(client)
    assert controller.last_audit_activation_generation == 0
    controller._audit(client)
    assert controller.last_audit_activation_generation == 1
    assert controller.full_audit_generation == 2


def test_trailing_audit_recomputes_backend_identity_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [backend_residency_record()],
    }
    claim = backend_docker_claim()
    snapshot = session.TargetSnapshot((6101, 7001), (claim,), ())
    authority = mps_snapshot(
        server_pids=frozenset({7001}),
        client_pids=frozenset({6101}),
        declarers=frozenset(
            {mps_declarer(7000), mps_declarer(7001, 101)}
        ),
    )
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.dft_warmup_open = False
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=snapshot,
        authority=authority,
        trailing_compute=frozenset({6101, 7001}),
        trailing_docker=(claim,),
    )
    reads = 0

    def changing_start(_pid: int) -> int:
        nonlocal reads
        reads += 1
        return 333 if reads <= 2 else 334

    patch_backend_identity(monkeypatch, start_ticks=changing_start)

    with pytest.raises(session.DevGpuSessionError, match="exact active Docker"):
        controller._audit(client)


def test_trailing_audit_rejects_new_static_systemd_gpu_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.gpu_broker import server

    foreign = server.SystemdGpuClaim(
        scope="user",
        unit="foreign-gpu.service",
        main_pid=9901,
        control_group="/user.slice/foreign-gpu.service",
        process_pids=frozenset({9901}),
        gpu_uuids=frozenset({session.GPU_UUID}),
        static_gpu_uuids=frozenset({session.GPU_UUID}),
    )
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.dft_warmup_open = False
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        trailing_systemd=(foreign,),
    )

    _status, _snapshot, reasons = controller._audit(client)
    assert reasons == ("foreign systemd claim: user:foreign-gpu.service",)


@pytest.mark.parametrize("error_kind", ("process", "membership"))
@pytest.mark.parametrize("transition_shape", ("aba", "active_release"))
def test_trailing_audit_resamples_exact_md_scope_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_kind: str,
    transition_shape: str,
) -> None:
    from gpu_resource.transient_scope import (
        scope_control_group,
        user_manager_control_group,
    )
    from ops.gpu_broker import server

    dft_lease = dft_residency_record()
    md_lease = md_execution_record()
    md_cgroup = scope_control_group(md_lease["lease_id"], uid=1001)
    idle = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [dft_lease],
    }
    active = {
        **idle,
        "next_fencing_token": 3,
        "leases": [dft_lease, md_lease],
    }
    before = idle if transition_shape == "aba" else active
    after = {
        **active,
        "last_released_lease": md_lease,
        "leases": [{**dft_lease, "heartbeat_at": 7.0}],
    }
    if error_kind == "process":
        inventory_error: Exception = server.SystemdProcessDisappeared(
            290593,
            790,
            md_cgroup,
        )
    else:
        manager_cgroup = user_manager_control_group(1001)
        inventory_error = server.SystemdMembershipChanged(
            "system",
            "user@1001.service",
            manager_cgroup,
            (
                (7000, 100, manager_cgroup),
                (290593, 790, md_cgroup),
            ),
            ((7000, 100, manager_cgroup),),
        )

    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.dft_warmup_open = False
    controller.dft_stabilized = True
    client = patch_full_audit_runtime(monkeypatch, controller, before)
    rounds = 0
    systemd_reads = 0

    def status():
        return before if rounds == 0 else after

    client.status = status

    def collect(_client, **_kwargs):
        nonlocal rounds
        rounds += 1
        return (before if rounds == 1 else after), empty_snapshot()

    def trailing_systemd(**_kwargs):
        nonlocal systemd_reads
        systemd_reads += 1
        if systemd_reads == 1:
            raise inventory_error
        return ()

    monkeypatch.setattr(session, "consistent_broker_snapshot", collect)
    monkeypatch.setattr(server, "query_systemd_gpu_claims", trailing_systemd)

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons == ()
    assert rounds == 2
    assert systemd_reads == 2


def test_trailing_audit_exact_md_releases_keep_three_round_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gpu_resource.transient_scope import scope_control_group
    from ops.gpu_broker import server

    dft_lease = dft_residency_record()
    tombstone: dict[str, object] | None = None
    transitions: list[
        tuple[dict[str, object], dict[str, object], Exception]
    ] = []
    for index, fencing_token in enumerate((2, 3, 4), start=1):
        md_lease = md_execution_record(
            lease_id=f"{index + 1:02x}" * 16,
            fencing_token=fencing_token,
            workload_pid=290590 + index,
            workload_start_ticks=790 + index,
        )
        active = {
            "broker_instance_id": "broker",
            "next_fencing_token": fencing_token + 1,
            "draining": False,
            "quarantined_gpus": {},
            "last_released_lease": tombstone,
            "leases": [dft_lease, md_lease],
        }
        released = {
            **active,
            "last_released_lease": md_lease,
            "leases": [{**dft_lease, "heartbeat_at": 7.0 + index}],
        }
        transitions.append(
            (
                active,
                released,
                server.SystemdProcessDisappeared(
                    290590 + index,
                    790 + index,
                    scope_control_group(md_lease["lease_id"], uid=1001),
                ),
            )
        )
        tombstone = md_lease

    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.dft_warmup_open = False
    controller.dft_stabilized = True
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        transitions[0][0],
    )
    round_index = -1
    systemd_reads = 0

    def collect(_client, **_kwargs):
        nonlocal round_index
        round_index += 1
        return transitions[round_index][0], empty_snapshot()

    statuses = iter(
        item
        for before, after, _error in transitions
        for item in (before, after)
    )

    def status():
        return next(statuses)

    def trailing_systemd(**_kwargs):
        nonlocal systemd_reads
        systemd_reads += 1
        raise transitions[round_index][2]

    client.status = status
    monkeypatch.setattr(session, "consistent_broker_snapshot", collect)
    monkeypatch.setattr(server, "query_systemd_gpu_claims", trailing_systemd)

    with pytest.raises(
        session.DevGpuSessionError,
        match="authority changed throughout trailing full audits",
    ):
        controller._audit(client)

    assert round_index + 1 == session.FULL_AUDIT_ATTEMPTS
    assert systemd_reads == session.FULL_AUDIT_ATTEMPTS


def test_trailing_audit_propagates_unowned_process_disappearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.gpu_broker import server

    before = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [dft_residency_record()],
    }
    after = {
        **before,
        "next_fencing_token": 3,
        "last_released_lease": md_execution_record(),
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.dft_warmup_open = False
    client = patch_full_audit_runtime(monkeypatch, controller, before)
    statuses = iter((before, after))
    client.status = lambda: next(statuses)
    error = server.SystemdProcessDisappeared(
        90001,
        123,
        "/user.slice/user-1001.slice/user@1001.service/foreign.scope",
    )
    monkeypatch.setattr(
        server,
        "query_systemd_gpu_claims",
        lambda **_kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(server.SystemdProcessDisappeared) as raised:
        controller._audit(client)

    assert raised.value is error


def test_exact_trailing_dft_systemd_churn_uses_warmup_budget_beyond_three_rounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.gpu_broker import server

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }

    def claim(version: int) -> object:
        return server.SystemdGpuClaim(
            scope="system",
            unit="user@1001.service",
            main_pid=7000,
            control_group=server.user_manager_control_group(1001),
            process_pids=frozenset({8123}),
            gpu_uuids=frozenset({session.GPU_UUID}),
            active_gpu_uuids=frozenset({session.GPU_UUID}),
            live_gpu_declarers=(
                server.SystemdGpuDeclarer(
                    pid=8123,
                    process_start_ticks=version,
                    process_cgroup="/dft.scope",
                    gpu_uuids=frozenset({session.GPU_UUID}),
                ),
            ),
        )

    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    client = patch_full_audit_runtime(monkeypatch, controller, status)
    rounds = 0
    fast_rounds: list[int] = []

    def collect(_client, **_kwargs):
        nonlocal rounds
        rounds += 1
        return status, session.TargetSnapshot((), (), (claim(rounds),))

    monkeypatch.setattr(session, "consistent_broker_snapshot", collect)
    monkeypatch.setattr(
        server,
        "query_systemd_gpu_claims",
        lambda **_kwargs: (
            claim(rounds + 100) if rounds <= 4 else claim(rounds),
        ),
    )
    monkeypatch.setattr(
        controller,
        "_managed_workload_authority",
        lambda _status, snapshot, *_args: (
            frozenset(),
            frozenset(),
            frozenset(
                (item.scope, item.unit, item.control_group)
                for item in snapshot.systemd_claims
            ),
        ),
    )
    monkeypatch.setattr(
        controller,
        "_fast_dft_churn_guard",
        lambda _client, _status: fast_rounds.append(rounds) or True,
    )

    _status, _snapshot, reasons = controller._audit(client)
    assert reasons == ()
    assert rounds == 5
    assert fast_rounds == [1, 2, 3, 4]


def test_full_audit_retries_exact_mps_client_membership_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    control = mps_declarer(7000)
    server = mps_declarer(7001, 101)
    before = mps_snapshot(
        server_pids=frozenset({7001}),
        declarers=frozenset({control, server}),
    )
    after = mps_snapshot(
        server_pids=frozenset({7001}),
        client_pids=frozenset({8123}),
        declarers=frozenset({control, server}),
    )
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=session.TargetSnapshot((8123,), (), ()),
        authority=before,
        trailing_compute=frozenset({8123}),
    )
    authorities = iter((before, after, after, after, after))
    monkeypatch.setattr(
        controller,
        "_mps_authority",
        lambda: next(authorities),
    )
    monkeypatch.setattr(
        controller,
        "_fast_dft_churn_guard",
        lambda _client, _status: True,
    )

    controller._audit(client)

    assert controller.full_audit_generation == 1
    assert controller.last_mps_authority == after


def test_full_audit_retries_exact_lazy_mps_server_growth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    control = mps_declarer(7000)
    before = mps_snapshot(declarers=frozenset({control}))
    after = dft_resident_authority()
    captured = dft_broad_snapshot(
        monkeypatch,
        status,
        after,
        session.TargetSnapshot((7001, 8123), (), ()),
    )
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=captured,
        authority=before,
        trailing_compute=frozenset({7001, 8123}),
        trailing_systemd=captured.systemd_claims,
    )
    authorities = iter((before, after, after, after, after))
    guarded: list[int] = []
    monkeypatch.setattr(controller, "_mps_authority", lambda: next(authorities))
    monkeypatch.setattr(
        controller,
        "_fast_dft_churn_guard",
        lambda _client, _status: guarded.append(1) or True,
    )

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons == ()
    assert guarded == [1]
    assert controller.full_audit_generation == 1
    assert controller.last_mps_authority == after


@pytest.mark.parametrize(
    ("server_cgroup", "foreign_pid", "expected_reason"),
    (
        (
            "/user.slice/user-1001.slice/user@1001.service/mps.scope",
            None,
            "foreign CUDA PID(s): 7001",
        ),
        (
            "/user.slice/user-1001.slice/session-33690.scope",
            9999,
            "9999",
        ),
    ),
)
def test_initial_unclaimed_lazy_server_stays_foreign_when_not_sole_sibling(
    server_cgroup: str,
    foreign_pid: int | None,
    expected_reason: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    control = mps_declarer(7000)
    server = replace(
        mps_declarer(7001, 101),
        process_cgroup=server_cgroup,
    )
    before = mps_snapshot(declarers=frozenset({control}))
    after = mps_snapshot(
        server_pids=frozenset({7001}),
        declarers=frozenset({control, server}),
    )
    process_pids = (7001,) if foreign_pid is None else (7001, foreign_pid)
    captured = session.TargetSnapshot(
        process_pids,
        (),
        (),
        (server,),
    )
    authorities = iter((before, after))
    client = SimpleNamespace(status=lambda: status)
    monkeypatch.setattr(
        session,
        "consistent_broker_snapshot",
        lambda _client, **_kwargs: (status, captured),
    )
    monkeypatch.setattr(controller, "_mps_authority", lambda: next(authorities))
    monkeypatch.setattr(
        controller,
        "_exact_dft_descendants",
        lambda _status, pids: pids == frozenset({8123}),
    )
    monkeypatch.setattr(
        controller,
        "_fast_dft_churn_guard",
        lambda *_args: pytest.fail("foreign evidence must not receive a retry"),
    )

    _status, _snapshot, reasons = controller._audit(client)

    assert any(expected_reason in reason for reason in reasons)
    assert controller.full_audit_generation == 0


def test_initial_unclaimed_lazy_server_login_sibling_requires_fast_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    control = mps_declarer(7000)
    server = replace(
        mps_declarer(7001, 101),
        process_cgroup="/user.slice/user-1001.slice/session-33690.scope",
    )
    before = mps_snapshot(declarers=frozenset({control}))
    after = mps_snapshot(
        server_pids=frozenset({7001}),
        declarers=frozenset({control, server}),
    )
    captured = session.TargetSnapshot((7001,), (), (), (server,))
    authorities = iter((before, after))
    client = SimpleNamespace(status=lambda: status)
    guarded: list[int] = []
    monkeypatch.setattr(
        session,
        "consistent_broker_snapshot",
        lambda _client, **_kwargs: (status, captured),
    )
    monkeypatch.setattr(controller, "_mps_authority", lambda: next(authorities))
    monkeypatch.setattr(
        controller,
        "_exact_dft_descendants",
        lambda _status, pids: pids == frozenset({8123}),
    )

    def guard(_client, _status):
        guarded.append(1)
        raise RuntimeError("fast guard required before retry")

    monkeypatch.setattr(controller, "_fast_dft_churn_guard", guard)

    with pytest.raises(RuntimeError, match="fast guard required before retry"):
        controller._audit(client)

    assert guarded == [1]
    assert controller.full_audit_generation == 0


def test_trailing_unclaimed_lazy_server_login_sibling_requires_fast_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    from ops.gpu_broker import server as broker_server

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    control = mps_declarer(7000)
    server = replace(
        mps_declarer(7001, 101),
        process_cgroup="/user.slice/user-1001.slice/session-33690.scope",
    )
    before = mps_snapshot(declarers=frozenset({control}))
    after = mps_snapshot(
        server_pids=frozenset({7001}),
        declarers=frozenset({control, server}),
    )
    authorities = iter((before, before, after))
    compute = iter((frozenset({7001}), frozenset({7001})))
    client = SimpleNamespace(status=lambda: status)
    guarded: list[int] = []
    monkeypatch.setattr(
        session,
        "consistent_broker_snapshot",
        lambda _client, **_kwargs: (status, empty_snapshot()),
    )
    monkeypatch.setattr(controller, "_mps_authority", lambda: next(authorities))
    monkeypatch.setattr(
        broker_server,
        "query_compute_processes",
        lambda: {session.GPU_UUID: next(compute)},
    )
    monkeypatch.setattr(broker_server, "query_docker_gpu_claims", lambda: ())
    monkeypatch.setattr(
        broker_server,
        "query_systemd_gpu_claims",
        lambda **_kwargs: (),
    )
    monkeypatch.setattr(
        session,
        "capture_compute_process_declarers",
        lambda pids: {7001: server} if 7001 in pids else {},
    )
    monkeypatch.setattr(session, "require_gpu1_default_compute_mode", lambda: None)
    monkeypatch.setattr(
        controller,
        "_exact_dft_descendants",
        lambda _status, pids: pids == frozenset({8123}),
    )

    def guard(_client, _status):
        guarded.append(1)
        raise RuntimeError("fast guard required before retry")

    monkeypatch.setattr(controller, "_fast_dft_churn_guard", guard)

    with pytest.raises(RuntimeError, match="fast guard required before retry"):
        controller._audit(client)

    assert guarded == [1]
    assert controller.full_audit_generation == 0


def test_full_audit_discards_broker_change_after_initial_mps_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = {
        "broker_instance_id": "broker",
        "next_fencing_token": 1,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [],
    }
    resident = {
        **empty,
        "next_fencing_token": 2,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    control_only = mps_snapshot(
        declarers=frozenset({mps_declarer(7000)})
    )
    resident_authority = dft_resident_authority()
    captured = dft_broad_snapshot(
        monkeypatch,
        resident,
        resident_authority,
        session.TargetSnapshot((7001, 8123), (), ()),
    )
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        resident,
        snapshot=captured,
        authority=resident_authority,
        trailing_compute=frozenset({7001, 8123}),
        trailing_systemd=captured.systemd_claims,
    )
    broker_reads = 0
    rounds = 0
    mps_reads = 0

    def status():
        nonlocal broker_reads
        broker_reads += 1
        return empty if broker_reads == 1 else resident

    def collect(_client, **_kwargs):
        nonlocal rounds
        rounds += 1
        return resident, captured

    def authority():
        nonlocal mps_reads
        mps_reads += 1
        # The production seal performs this Broker read before the MPS read.
        # Keep the old authority for the first sealed generation, then expose
        # the resident authority only after the next outer Broker seal.  An
        # implementation without that seal therefore cannot self-heal through
        # the pre-existing lazy-MPS-growth retry path.
        return control_only if broker_reads <= 1 else resident_authority

    client.status = status
    monkeypatch.setattr(session, "consistent_broker_snapshot", collect)
    monkeypatch.setattr(controller, "_mps_authority", authority)

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons == ()
    assert rounds == 2
    assert broker_reads > 1
    assert controller.full_audit_generation == 1
    assert controller.last_mps_authority == resident_authority


@pytest.mark.parametrize(
    (
        "plane_ready_published",
        "dft_stabilized",
        "activation_generation",
        "dft_warmup_open",
    ),
    (
        (False, False, 0, True),
        (False, True, 0, False),
        (True, True, 1, False),
    ),
)
def test_initial_mps_broker_seal_change_keeps_three_round_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plane_ready_published: bool,
    dft_stabilized: bool,
    activation_generation: int,
    dft_warmup_open: bool,
) -> None:
    transitions = tuple(
        (
            {
                "broker_instance_id": "broker",
                "next_fencing_token": generation,
                "draining": False,
                "quarantined_gpus": {},
                "leases": [],
            },
            {
                "broker_instance_id": "broker",
                "next_fencing_token": generation + 1,
                "draining": False,
                "quarantined_gpus": {},
                "leases": [],
            },
        )
        for generation in (1, 2, 3)
    )
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.plane_ready_published = plane_ready_published
    controller.dft_stabilized = dft_stabilized
    controller.activation_generation = activation_generation
    controller.dft_warmup_open = dft_warmup_open
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        transitions[0][0],
    )
    rounds = 0
    mps_reads = 0

    def status():
        return transitions[rounds][0]

    def collect(_client, **_kwargs):
        nonlocal rounds
        after = transitions[rounds][1]
        rounds += 1
        return after, empty_snapshot()

    def authority():
        nonlocal mps_reads
        mps_reads += 1
        return mps_snapshot()

    client.status = status
    monkeypatch.setattr(session, "consistent_broker_snapshot", collect)
    monkeypatch.setattr(controller, "_mps_authority", authority)

    with pytest.raises(
        session.DevGpuSessionError,
        match="authority changed throughout trailing full audits",
    ):
        controller._audit(client)

    assert rounds == session.FULL_AUDIT_ATTEMPTS
    # Every mismatched initial seal must abandon the round before a second MPS
    # read or any host-inventory classification can occur.
    assert mps_reads == session.FULL_AUDIT_ATTEMPTS


def test_preactivation_rollout_commits_after_three_broker_seal_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = tuple(
        {
            "broker_instance_id": "broker",
            "next_fencing_token": generation,
            "draining": False,
            "quarantined_gpus": {},
            "leases": [],
        }
        for generation in (1, 2, 3, 4)
    )
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.plane_ready_published = True
    controller.dft_stabilized = True
    controller.dft_warmup_open = False
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        statuses[0],
    )
    rounds = 0

    def status():
        return statuses[min(rounds, len(statuses) - 1)]

    def collect(_client, **_kwargs):
        nonlocal rounds
        after = statuses[min(rounds + 1, len(statuses) - 1)]
        rounds += 1
        return after, empty_snapshot()

    client.status = status
    monkeypatch.setattr(session, "consistent_broker_snapshot", collect)

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons == ()
    assert rounds == 4
    assert controller.full_audit_generation == 1


def test_preactivation_rollout_broker_seal_changes_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollout_attempts = 8
    statuses = tuple(
        {
            "broker_instance_id": "broker",
            "next_fencing_token": generation,
            "draining": False,
            "quarantined_gpus": {},
            "leases": [],
        }
        for generation in range(1, rollout_attempts + 2)
    )
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.plane_ready_published = True
    controller.dft_stabilized = True
    controller.dft_warmup_open = False
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        statuses[0],
    )
    rounds = 0
    mps_reads = 0

    def status():
        return statuses[rounds]

    def collect(_client, **_kwargs):
        nonlocal rounds
        rounds += 1
        return statuses[rounds], empty_snapshot()

    def authority():
        nonlocal mps_reads
        mps_reads += 1
        return mps_snapshot()

    client.status = status
    monkeypatch.setattr(session, "consistent_broker_snapshot", collect)
    monkeypatch.setattr(controller, "_mps_authority", authority)

    with pytest.raises(
        session.DevGpuSessionError,
        match="authority changed throughout trailing full audits",
    ):
        controller._audit(client)

    assert session.PREACTIVATION_ROLLOUT_AUDIT_ATTEMPTS == rollout_attempts
    assert rounds == rollout_attempts
    assert mps_reads == rollout_attempts


@pytest.mark.parametrize(
    "docker_error",
    (
        "Docker container inventory changed during audit",
        "Docker container fingerprint changed during audit",
    ),
)
@pytest.mark.parametrize("failure_site", ("initial", "trailing"))
def test_preactivation_rollout_retries_exact_docker_inventory_churn(
    failure_site: str,
    docker_error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.gpu_broker.broker import BrokerError
    from ops.gpu_broker import server

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.plane_ready_published = True
    controller.dft_stabilized = True
    controller.dft_warmup_open = False
    client = patch_full_audit_runtime(monkeypatch, controller, status)
    calls = 0

    def changing_inventory(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise BrokerError("gpu_claim_inventory_unavailable", docker_error)
        return (status, empty_snapshot()) if failure_site == "initial" else ()

    if failure_site == "initial":
        monkeypatch.setattr(
            session,
            "consistent_broker_snapshot",
            changing_inventory,
        )
    else:
        monkeypatch.setattr(server, "query_docker_gpu_claims", changing_inventory)

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons == ()
    assert calls == 4
    assert controller.full_audit_generation == 1


def test_preactivation_docker_inventory_churn_retry_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.gpu_broker.broker import BrokerError

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.plane_ready_published = True
    controller.dft_stabilized = True
    controller.dft_warmup_open = False
    client = patch_full_audit_runtime(monkeypatch, controller, status)
    calls = 0

    def changing_inventory(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise BrokerError(
            "gpu_claim_inventory_unavailable",
            "Docker container inventory changed during audit",
        )

    monkeypatch.setattr(
        session,
        "consistent_broker_snapshot",
        changing_inventory,
    )

    with pytest.raises(
        session.DevGpuSessionError,
        match="authority changed throughout trailing full audits",
    ):
        controller._audit(client)

    assert calls == session.PREACTIVATION_ROLLOUT_AUDIT_ATTEMPTS
    assert controller.full_audit_generation == 0


@pytest.mark.parametrize(
    ("error_code", "error_message", "subclassed"),
    (
        (
            "gpu_claim_inventory_unavailable",
            "Docker GPU claim inventory failed",
            False,
        ),
        (
            "gpu_claim_inventory_unavailable",
            "Docker inspect inventory is incomplete",
            False,
        ),
        (
            "gpu_claim_inventory_unavailable",
            "Docker GPU claim is invalid",
            False,
        ),
        (
            "unexpected_code",
            "Docker container inventory changed during audit",
            False,
        ),
        (
            "gpu_claim_inventory_unavailable",
            "Docker container inventory changed during audit",
            True,
        ),
    ),
)
def test_preactivation_rollout_does_not_retry_other_docker_inventory_errors(
    error_code: str,
    error_message: str,
    subclassed: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.gpu_broker.broker import BrokerError

    class DerivedBrokerError(BrokerError):
        pass

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.plane_ready_published = True
    controller.dft_stabilized = True
    controller.dft_warmup_open = False
    client = patch_full_audit_runtime(monkeypatch, controller, status)
    error_type = DerivedBrokerError if subclassed else BrokerError
    error = error_type(error_code, error_message)
    calls = 0

    def unavailable(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(session, "consistent_broker_snapshot", unavailable)

    with pytest.raises(BrokerError) as captured:
        controller._audit(client)

    assert captured.value is error
    assert calls == 1
    assert controller.full_audit_generation == 0


@pytest.mark.parametrize(
    "last_activation_generation",
    (
        pytest.param(0, id="activation"),
        pytest.param(1, id="steady"),
    ),
)
def test_docker_inventory_churn_outside_preactivation_rollout_fails_closed(
    last_activation_generation: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.gpu_broker.broker import BrokerError

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.plane_ready_published = True
    controller.dft_stabilized = True
    controller.dft_warmup_open = False
    controller.activation_generation = 1
    controller.last_audit_activation_generation = last_activation_generation
    client = patch_full_audit_runtime(monkeypatch, controller, status)
    error = BrokerError(
        "gpu_claim_inventory_unavailable",
        "Docker container inventory changed during audit",
    )
    calls = 0

    def changing_inventory(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(
        session,
        "consistent_broker_snapshot",
        changing_inventory,
    )

    with pytest.raises(BrokerError) as captured:
        controller._audit(client)

    assert captured.value is error
    assert calls == 1
    assert controller.full_audit_generation == 0


def test_activation_signal_during_docker_inventory_churn_disables_rollout_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.gpu_broker.broker import BrokerError

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.plane_ready_published = True
    controller.dft_stabilized = True
    controller.dft_warmup_open = False
    client = patch_full_audit_runtime(monkeypatch, controller, status)
    error = BrokerError(
        "gpu_claim_inventory_unavailable",
        "Docker container inventory changed during audit",
    )
    calls = 0

    def changing_inventory(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        controller.activation_generation += 1
        raise error

    monkeypatch.setattr(
        session,
        "consistent_broker_snapshot",
        changing_inventory,
    )

    with pytest.raises(BrokerError) as captured:
        controller._audit(client)

    assert captured.value is error
    assert calls == 1
    assert controller.activation_generation == 1
    assert controller.full_audit_generation == 0


def test_preactivation_rollout_returns_stable_foreign_evidence_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "quarantined_gpus": {},
        "leases": [],
    }
    foreign_pid = 99001
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.plane_ready_published = True
    controller.dft_stabilized = True
    controller.dft_warmup_open = False
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=session.TargetSnapshot((foreign_pid,), (), ()),
    )
    rounds = 0

    def collect(_client, **_kwargs):
        nonlocal rounds
        rounds += 1
        return status, session.TargetSnapshot((foreign_pid,), (), ())

    monkeypatch.setattr(session, "consistent_broker_snapshot", collect)

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons == (f"foreign CUDA PID(s): {foreign_pid}",)
    assert rounds == 1
    assert controller.full_audit_generation == 0


def test_full_audit_accepts_dft_claim_with_mps_in_sibling_login_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    before = mps_snapshot(declarers=frozenset({mps_declarer(7000)}))
    after = dft_resident_authority()
    captured = dft_broad_snapshot(
        monkeypatch,
        status,
        after,
        session.TargetSnapshot((8123,), (), ()),
    )
    mps_pids = frozenset(declarer.pid for declarer in after.gpu_declarers)
    claim = captured.systemd_claims[0]
    captured = session.TargetSnapshot(
        captured.process_pids,
        captured.docker_claims,
        (
            replace(
                claim,
                process_pids=claim.process_pids - mps_pids,
                live_gpu_declarers=tuple(
                    declarer
                    for declarer in claim.live_gpu_declarers
                    if declarer.pid not in mps_pids
                ),
            ),
        ),
        captured.process_declarers,
    )
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=captured,
        authority=before,
        trailing_compute=frozenset({8123}),
        trailing_systemd=captured.systemd_claims,
    )
    authorities = iter((before, after, after, after, after))
    guarded: list[int] = []
    monkeypatch.setattr(controller, "_mps_authority", lambda: next(authorities))
    monkeypatch.setattr(
        controller,
        "_fast_dft_churn_guard",
        lambda _client, _status: guarded.append(1) or True,
    )

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons == ()
    assert guarded == [1]
    assert controller.full_audit_generation == 1
    assert controller.last_mps_authority == after


def test_full_audit_discards_lazy_server_that_appeared_after_captured_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.gpu_broker import server

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    before = mps_snapshot(declarers=frozenset({mps_declarer(7000)}))
    after = dft_resident_authority()
    initial = dft_broad_snapshot(
        monkeypatch,
        status,
        before,
        empty_snapshot(),
    )
    stable = dft_broad_snapshot(
        monkeypatch,
        status,
        after,
        session.TargetSnapshot((7001, 8123), (), ()),
    )
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=initial,
        authority=before,
        trailing_compute=frozenset({7001, 8123}),
        trailing_systemd=stable.systemd_claims,
    )
    rounds = iter((initial, stable))
    authorities = iter((before, after, after, after, after))
    guarded: list[int] = []
    monkeypatch.setattr(
        session,
        "consistent_broker_snapshot",
        lambda _client, **_kwargs: (status, next(rounds)),
    )
    monkeypatch.setattr(controller, "_mps_authority", lambda: next(authorities))
    monkeypatch.setattr(
        controller,
        "_fast_dft_churn_guard",
        lambda _client, _status: guarded.append(1) or True,
    )
    monkeypatch.setattr(
        server,
        "query_systemd_gpu_claims",
        lambda **_kwargs: stable.systemd_claims,
    )
    stable_declarers = {
        declarer.pid: declarer for declarer in stable.process_declarers
    }
    monkeypatch.setattr(
        session,
        "capture_compute_process_declarers",
        lambda pids: {
            pid: stable_declarers[pid]
            for pid in pids
            if pid in stable_declarers
        },
    )

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons == ()
    assert guarded == [1]
    assert controller.full_audit_generation == 1


def test_trailing_full_audit_retries_exact_lazy_mps_server_growth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.gpu_broker import server

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    before = mps_snapshot(declarers=frozenset({mps_declarer(7000)}))
    after = dft_resident_authority()
    initial = dft_broad_snapshot(
        monkeypatch,
        status,
        before,
        empty_snapshot(),
    )
    trailing = dft_broad_snapshot(
        monkeypatch,
        status,
        after,
        session.TargetSnapshot((7001, 8123), (), ()),
    )
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=initial,
        authority=before,
        trailing_compute=frozenset({7001, 8123}),
        trailing_systemd=trailing.systemd_claims,
    )
    rounds = iter((initial, trailing))
    authorities = iter((before, before, after, after, after, after))
    guarded: list[int] = []
    captured_declarers = {
        declarer.pid: declarer
        for declarer in trailing.process_declarers
    }
    monkeypatch.setattr(
        session,
        "consistent_broker_snapshot",
        lambda _client, **_kwargs: (status, next(rounds)),
    )
    monkeypatch.setattr(controller, "_mps_authority", lambda: next(authorities))
    monkeypatch.setattr(
        controller,
        "_fast_dft_churn_guard",
        lambda _client, _status: guarded.append(1) or True,
    )
    monkeypatch.setattr(
        server,
        "query_systemd_gpu_claims",
        lambda **_kwargs: trailing.systemd_claims,
    )
    monkeypatch.setattr(
        session,
        "capture_compute_process_declarers",
        lambda pids: {
            pid: captured_declarers[pid]
            for pid in pids
            if pid in captured_declarers
        },
    )

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons == ()
    assert guarded == [1]
    assert controller.full_audit_generation == 1
    assert controller.last_mps_authority == after


def test_trailing_full_audit_retries_lazy_server_between_nvml_seals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.gpu_broker import server

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    before = mps_snapshot(declarers=frozenset({mps_declarer(7000)}))
    after = dft_resident_authority()
    initial = dft_broad_snapshot(
        monkeypatch,
        status,
        before,
        empty_snapshot(),
    )
    stable = dft_broad_snapshot(
        monkeypatch,
        status,
        after,
        session.TargetSnapshot((7001, 8123), (), ()),
    )
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=initial,
        authority=before,
    )
    rounds = iter((initial, stable))
    authorities = iter((before, before, after, after, after, after))
    compute = iter(
        (
            frozenset(),
            frozenset({7001, 8123}),
            frozenset({7001, 8123}),
            frozenset({7001, 8123}),
        )
    )
    systemd_claims = iter((initial.systemd_claims, stable.systemd_claims))
    captured_declarers = {
        declarer.pid: declarer
        for declarer in stable.process_declarers
    }
    guarded: list[int] = []
    monkeypatch.setattr(
        session,
        "consistent_broker_snapshot",
        lambda _client, **_kwargs: (status, next(rounds)),
    )
    monkeypatch.setattr(controller, "_mps_authority", lambda: next(authorities))
    monkeypatch.setattr(
        server,
        "query_compute_processes",
        lambda: {session.GPU_UUID: next(compute)},
    )
    monkeypatch.setattr(
        server,
        "query_systemd_gpu_claims",
        lambda **_kwargs: next(systemd_claims),
    )
    monkeypatch.setattr(
        session,
        "capture_compute_process_declarers",
        lambda pids: {
            pid: captured_declarers[pid]
            for pid in pids
            if pid in captured_declarers
        },
    )
    monkeypatch.setattr(
        controller,
        "_fast_dft_churn_guard",
        lambda _client, _status: guarded.append(1) or True,
    )

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons == ()
    assert guarded == [1]
    assert controller.full_audit_generation == 1


@pytest.mark.parametrize("fault", ("identity", "foreign"))
def test_trailing_lazy_server_seal_growth_never_hides_foreign_evidence(
    fault: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    from ops.gpu_broker import server

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    before = mps_snapshot(declarers=frozenset({mps_declarer(7000)}))
    after = dft_resident_authority()
    initial = dft_broad_snapshot(
        monkeypatch,
        status,
        before,
        empty_snapshot(),
    )
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=initial,
        authority=before,
    )
    authorities = iter((before, before, after))
    sealed = {7001, 8123}
    if fault == "foreign":
        sealed.add(9999)
    compute = iter((frozenset(), frozenset(sealed)))
    exact_server = next(
        declarer for declarer in after.gpu_declarers if declarer.pid == 7001
    )
    captured_server = (
        replace(exact_server, process_start_ticks=999)
        if fault == "identity"
        else exact_server
    )
    monkeypatch.setattr(controller, "_mps_authority", lambda: next(authorities))
    monkeypatch.setattr(
        server,
        "query_compute_processes",
        lambda: {session.GPU_UUID: next(compute)},
    )
    monkeypatch.setattr(
        server,
        "query_systemd_gpu_claims",
        lambda **_kwargs: initial.systemd_claims,
    )
    monkeypatch.setattr(
        session,
        "capture_compute_process_declarers",
        lambda pids: {7001: captured_server} if 7001 in pids else {},
    )
    monkeypatch.setattr(
        controller,
        "_fast_dft_churn_guard",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("captured foreign evidence must win")
        ),
    )

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons
    expected_pid = 7001 if fault == "identity" else 9999
    assert any(str(expected_pid) in reason for reason in reasons)


@pytest.mark.parametrize("capture_fault", ("reused", "missing"))
def test_trailing_lazy_growth_rejects_unsealed_untrusted_dft_pid(
    capture_fault: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    from ops.gpu_broker import server

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    before = mps_snapshot(declarers=frozenset({mps_declarer(7000)}))
    after = dft_resident_authority()
    initial = dft_broad_snapshot(
        monkeypatch,
        status,
        before,
        empty_snapshot(),
    )
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=initial,
        authority=before,
    )
    authorities = iter((before, before, after))
    compute = iter(
        (
            frozenset({8123}),
            frozenset({7001, 8123}),
        )
    )
    exact_root = next(
        declarer
        for declarer in initial.systemd_claims[0].live_gpu_declarers
        if declarer.pid == 8123
    )
    exact_server = next(
        declarer for declarer in after.gpu_declarers if declarer.pid == 7001
    )
    capture_reads = 0

    def capture(pids: frozenset[int]) -> dict[int, object]:
        nonlocal capture_reads
        capture_reads += 1
        if capture_reads == 1:
            return (
                {8123: replace(exact_root, process_start_ticks=999)}
                if capture_fault == "reused"
                else {}
            )
        expected = {7001: exact_server, 8123: exact_root}
        return {pid: expected[pid] for pid in pids if pid in expected}
    monkeypatch.setattr(controller, "_mps_authority", lambda: next(authorities))
    monkeypatch.setattr(
        server,
        "query_compute_processes",
        lambda: {session.GPU_UUID: next(compute)},
    )
    monkeypatch.setattr(
        server,
        "query_systemd_gpu_claims",
        lambda **_kwargs: initial.systemd_claims,
    )
    monkeypatch.setattr(
        session,
        "capture_compute_process_declarers",
        capture,
    )
    monkeypatch.setattr(
        controller,
        "_fast_dft_churn_guard",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("captured reused DFT PID must win")
        ),
    )

    _status, _snapshot, reasons = controller._audit(client)

    assert any(
        reason.startswith(
            "managed GPU1 process identity changed after NVML capture: PID(s) "
        )
        and "8123" in reason
        for reason in reasons
    )


def test_exact_lazy_mps_server_growth_rejects_ambiguous_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    control = mps_declarer(7000)
    before = mps_snapshot(declarers=frozenset({control}))
    after = dft_resident_authority()
    monkeypatch.setattr(
        controller,
        "_exact_dft_descendants",
        lambda _status, pids: pids == frozenset({8123}),
    )

    assert controller._exact_dft_lazy_mps_server_growth(
        status,
        before,
        after,
    ) is True
    abbreviated_client = replace(
        next(iter(after.clients)),
        device_uuid=session.GPU_UUID[:12],
    )
    assert controller._exact_dft_lazy_mps_server_growth(
        status,
        before,
        replace(after, clients=frozenset({abbreviated_client})),
    ) is True
    no_client_after = mps_snapshot(
        server_pids=frozenset({7001}),
        declarers=frozenset({control, mps_declarer(7001, 101)}),
    )
    assert controller._exact_dft_lazy_mps_server_growth(
        status,
        before,
        no_client_after,
    ) is True
    monkeypatch.setattr(
        controller,
        "_exact_dft_descendants",
        lambda _status, _pids: False,
    )
    assert controller._exact_dft_lazy_mps_server_growth(
        status,
        before,
        no_client_after,
    ) is False
    monkeypatch.setattr(
        controller,
        "_exact_dft_descendants",
        lambda _status, pids: pids == frozenset({8123}),
    )
    assert controller._exact_dft_lazy_mps_server_growth(
        {**status, "leases": []},
        before,
        after,
    ) is False
    controller.activation_generation = 1
    assert controller._exact_dft_lazy_mps_server_growth(
        status,
        before,
        after,
    ) is False
    controller.activation_generation = 0

    ambiguous = (
        (replace(before, descriptor_authority=False), after),
        (before, replace(after, descriptor_authority=False)),
        (
            mps_snapshot(
                server_pids=frozenset({7001}),
                declarers=frozenset({control, mps_declarer(7001, 101)}),
            ),
            after,
        ),
        (
            before,
            mps_snapshot(
                server_pids=frozenset({7001, 7002}),
                declarers=frozenset(
                    {control, mps_declarer(7001, 101), mps_declarer(7002, 102)}
                ),
            ),
        ),
        (
            before,
            mps_snapshot(
                server_pids=frozenset({7001}),
                client_pids=frozenset({8123}),
                declarers=frozenset(
                    {mps_declarer(7003), mps_declarer(7001, 101)}
                ),
            ),
        ),
        (
            mps_snapshot(
                client_pids=frozenset({8123}),
                declarers=frozenset({control}),
            ),
            after,
        ),
        (
            before,
            mps_snapshot(
                server_pids=frozenset({7001}),
                client_pids=frozenset({9999}),
                declarers=frozenset({control, mps_declarer(7001, 101)}),
            ),
        ),
    )
    assert all(
        controller._exact_dft_lazy_mps_server_growth(
            status,
            candidate_before,
            candidate_after,
        )
        is False
        for candidate_before, candidate_after in ambiguous
    )


@pytest.mark.parametrize(
    ("foreign_source", "expected"),
    (
        ("compute", "foreign CUDA PID(s): 9999"),
        ("docker", "Docker declared GPU1 during DFT-only stabilization"),
        ("systemd", "foreign systemd claim: user:foreign-gpu.service"),
        ("mps", "unknown private MPS client PID(s):"),
        ("unbound_server", "foreign CUDA PID(s): 7001"),
        (
            "reused_server",
            "managed GPU1 process identity changed after NVML capture",
        ),
        (
            "nvml_reuse",
            "managed GPU1 process identity changed after NVML capture",
        ),
        (
            "dft_client_reuse",
            "managed GPU1 process identity changed after NVML capture",
        ),
        (
            "missing_dft_identity",
            "managed GPU1 process identity changed after NVML capture",
        ),
    ),
)
def test_exact_lazy_mps_server_growth_never_hides_captured_foreign_evidence(
    foreign_source: str,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.gpu_broker import server

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    before = mps_snapshot(declarers=frozenset({mps_declarer(7000)}))
    after = (
        mps_snapshot(
            server_pids=frozenset({7001}),
            client_pids=frozenset({8123, 9999}),
            declarers=frozenset(
                {mps_declarer(7000), mps_declarer(7001, 101)}
            ),
        )
        if foreign_source == "mps"
        else dft_resident_authority()
    )
    if foreign_source == "unbound_server":
        from dataclasses import replace

        manager_cgroup = server.user_manager_control_group(1001) + "/mps.scope"
        before = replace(
            before,
            gpu_declarers=frozenset(
                replace(declarer, process_cgroup=manager_cgroup)
                for declarer in before.gpu_declarers
            ),
        )
        after = replace(
            after,
            gpu_declarers=frozenset(
                replace(declarer, process_cgroup=manager_cgroup)
                for declarer in after.gpu_declarers
            ),
        )
    process_pids = (
        (7001, 8123, 9999)
        if foreign_source == "compute"
        else (7001, 8123)
    )
    docker_claims = (
        (backend_docker_claim(),) if foreign_source == "docker" else ()
    )
    captured = dft_broad_snapshot(
        monkeypatch,
        status,
        after,
        session.TargetSnapshot(process_pids, docker_claims, ()),
    )
    if foreign_source == "unbound_server":
        captured = session.TargetSnapshot(
            captured.process_pids,
            captured.docker_claims,
            (),
            captured.process_declarers,
        )
    if foreign_source == "reused_server":
        from dataclasses import replace

        captured = session.TargetSnapshot(
            captured.process_pids,
            captured.docker_claims,
            captured.systemd_claims,
            tuple(
                replace(declarer, process_start_ticks=999)
                if declarer.pid == 7001
                else declarer
                for declarer in captured.process_declarers
            ),
        )
    if foreign_source == "nvml_reuse":
        from dataclasses import replace

        captured = session.TargetSnapshot(
            captured.process_pids,
            captured.docker_claims,
            captured.systemd_claims,
            tuple(
                replace(declarer, process_start_ticks=999)
                if declarer.pid == 7001
                else declarer
                for declarer in captured.process_declarers
            ),
        )
    if foreign_source == "dft_client_reuse":
        from dataclasses import replace

        captured = session.TargetSnapshot(
            captured.process_pids,
            captured.docker_claims,
            captured.systemd_claims,
            tuple(
                replace(declarer, process_start_ticks=999)
                if declarer.pid == 8123
                else declarer
                for declarer in captured.process_declarers
            ),
        )
    if foreign_source == "missing_dft_identity":
        captured = session.TargetSnapshot(
            captured.process_pids,
            captured.docker_claims,
            captured.systemd_claims,
            tuple(
                declarer
                for declarer in captured.process_declarers
                if declarer.pid != 8123
            ),
        )
    if foreign_source == "systemd":
        foreign = server.SystemdGpuClaim(
            scope="user",
            unit="foreign-gpu.service",
            main_pid=9999,
            control_group="/user.slice/foreign-gpu.service",
            process_pids=frozenset({9999}),
            gpu_uuids=frozenset({session.GPU_UUID}),
            static_gpu_uuids=frozenset({session.GPU_UUID}),
        )
        captured = session.TargetSnapshot(
            captured.process_pids,
            captured.docker_claims,
            (*captured.systemd_claims, foreign),
            captured.process_declarers,
        )
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=captured,
        authority=before,
    )
    authorities = iter((before, after))
    monkeypatch.setattr(controller, "_mps_authority", lambda: next(authorities))
    monkeypatch.setattr(
        controller,
        "_fast_dft_churn_guard",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("captured foreign evidence must win")
        ),
    )

    _status, _snapshot, reasons = controller._audit(client)

    assert any(expected in reason for reason in reasons)


def test_full_audit_retries_more_than_three_exact_mps_client_additions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    from ops.gpu_broker.server import MpsClient

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    base = dft_resident_authority()

    def authority(contexts: int) -> object:
        return replace(
            base,
            clients=frozenset(
                MpsClient(
                    client_pid=8123,
                    client_id=index,
                    server_pid=7001,
                    device_uuid=session.GPU_UUID,
                    namespace_id=1,
                    command="python",
                )
                for index in range(contexts)
            ),
        )

    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=session.TargetSnapshot((8123,), (), ()),
        authority=base,
        trailing_compute=frozenset({8123}),
    )
    authorities = iter(
        (
            authority(0), authority(1),
            authority(1), authority(2),
            authority(2), authority(3),
            authority(3), authority(4),
            authority(4), authority(4), authority(4),
        )
    )
    guarded: list[int] = []
    monkeypatch.setattr(controller, "_mps_authority", lambda: next(authorities))
    monkeypatch.setattr(
        controller,
        "_fast_dft_churn_guard",
        lambda _client, _status: guarded.append(1) or True,
    )

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons == ()
    assert len(guarded) == 4
    assert controller.full_audit_generation == 1
    assert controller.last_mps_authority == authority(4)


def test_full_audit_never_extends_unknown_mps_client_growth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    control = mps_declarer(7000)
    server = mps_declarer(7001, 101)
    before = mps_snapshot(
        server_pids=frozenset({7001}),
        declarers=frozenset({control, server}),
    )
    foreign = mps_snapshot(
        server_pids=frozenset({7001}),
        client_pids=frozenset({9999}),
        declarers=frozenset({control, server}),
    )
    client = patch_full_audit_runtime(monkeypatch, controller, status)
    authorities = iter((before, foreign, before, foreign, before, foreign))
    monkeypatch.setattr(controller, "_mps_authority", lambda: next(authorities))
    monkeypatch.setattr(
        controller,
        "_exact_dft_descendants",
        lambda _status, _pids: False,
    )
    monkeypatch.setattr(
        controller,
        "_fast_dft_churn_guard",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("unknown MPS client must not receive the warmup budget")
        ),
    )

    _status, _snapshot, reasons = controller._audit(client)

    assert "unknown private MPS client PID(s): 9999" in reasons


def test_full_audit_keeps_short_budget_for_exact_mps_client_inventory_lag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    authority = dft_resident_authority()
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        authority=authority,
        trailing_compute=frozenset({8123}),
    )
    rounds = 0

    def collect(_client, **_kwargs):
        nonlocal rounds
        rounds += 1
        return status, empty_snapshot()

    monkeypatch.setattr(session, "consistent_broker_snapshot", collect)
    monkeypatch.setattr(
        controller,
        "_exact_dft_descendants",
        lambda _status, pids: pids == frozenset({8123}),
    )
    monkeypatch.setattr(
        controller,
        "_fast_dft_churn_guard",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("stable inventory lag must keep the short budget")
        ),
    )

    with pytest.raises(
        session.DevGpuSessionError,
        match="authority changed throughout trailing full audits",
    ):
        controller._audit(client)
    assert rounds == session.FULL_AUDIT_ATTEMPTS


def test_exact_mps_growth_never_hides_initial_foreign_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    control = mps_declarer(7000)
    server = mps_declarer(7001, 101)
    before = mps_snapshot(
        server_pids=frozenset({7001}),
        declarers=frozenset({control, server}),
    )
    after = mps_snapshot(
        server_pids=frozenset({7001}),
        client_pids=frozenset({8123}),
        declarers=frozenset({control, server}),
    )
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=session.TargetSnapshot((8123, 9999), (), ()),
        authority=before,
    )
    authorities = iter((before, after))
    monkeypatch.setattr(controller, "_mps_authority", lambda: next(authorities))
    monkeypatch.setattr(
        controller,
        "_fast_dft_churn_guard",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("captured foreign evidence must win")
        ),
    )

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons == ("foreign CUDA PID(s): 9999",)


def test_exact_mps_growth_never_hides_dft_warmup_docker_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    control = mps_declarer(7000)
    server = mps_declarer(7001, 101)
    before = mps_snapshot(
        server_pids=frozenset({7001}),
        declarers=frozenset({control, server}),
    )
    after = mps_snapshot(
        server_pids=frozenset({7001}),
        client_pids=frozenset({8123}),
        declarers=frozenset({control, server}),
    )
    claim = backend_docker_claim()
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=session.TargetSnapshot((8123,), (claim,), ()),
        authority=before,
    )
    authorities = iter((before, after))
    monkeypatch.setattr(controller, "_mps_authority", lambda: next(authorities))
    monkeypatch.setattr(
        controller,
        "_fast_dft_churn_guard",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("captured Docker evidence must win")
        ),
    )

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons[0] == "Docker declared GPU1 during DFT-only stabilization"


def test_exact_trailing_growth_never_hides_foreign_or_static_systemd_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.gpu_broker import server

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    control = mps_declarer(7000)
    mps_server = mps_declarer(7001, 101)
    before = mps_snapshot(
        server_pids=frozenset({7001}),
        declarers=frozenset({control, mps_server}),
    )
    after = mps_snapshot(
        server_pids=frozenset({7001}),
        client_pids=frozenset({8123}),
        declarers=frozenset({control, mps_server}),
    )
    initial = dft_broad_snapshot(
        monkeypatch,
        status,
        before,
        empty_snapshot(),
    )
    foreign = server.SystemdGpuClaim(
        scope="user",
        unit="foreign-gpu.service",
        main_pid=9999,
        control_group="/user.slice/foreign-gpu.service",
        process_pids=frozenset({9999}),
        gpu_uuids=frozenset({session.GPU_UUID}),
        static_gpu_uuids=frozenset({session.GPU_UUID}),
    )
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=initial,
        authority=before,
        trailing_compute=frozenset({8123, 9999}),
        trailing_systemd=(*initial.systemd_claims, foreign),
    )
    authorities = iter((before, before, after))
    monkeypatch.setattr(controller, "_mps_authority", lambda: next(authorities))
    monkeypatch.setattr(
        controller,
        "_fast_dft_churn_guard",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("captured trailing foreign evidence must win")
        ),
    )

    _status, _snapshot, reasons = controller._audit(client)

    assert "foreign CUDA PID(s): 9999" in reasons
    assert "foreign systemd claim: user:foreign-gpu.service" in reasons


def test_full_audit_retries_exact_nvml_growth_across_systemd_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.gpu_broker import server

    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    authority = dft_resident_authority()
    resident = dft_broad_snapshot(
        monkeypatch,
        status,
        authority,
        session.TargetSnapshot((8123,), (), ()),
    )
    changing = session.TargetSnapshot((), (), resident.systemd_claims)
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=resident,
        authority=authority,
        trailing_compute=frozenset({8123}),
    )
    rounds = 0
    compute_reads = 0

    def collect(_client, **_kwargs):
        nonlocal rounds
        rounds += 1
        return status, changing if rounds <= 4 else resident

    def compute():
        nonlocal compute_reads
        compute_reads += 1
        if rounds <= 4:
            pids = frozenset() if compute_reads % 2 else frozenset({8123})
        else:
            pids = frozenset({8123})
        return {session.GPU_UUID: pids}

    guarded: list[int] = []
    monkeypatch.setattr(session, "consistent_broker_snapshot", collect)
    monkeypatch.setattr(server, "query_compute_processes", compute)
    monkeypatch.setattr(
        controller,
        "_exact_dft_descendants",
        lambda _status, pids: pids == frozenset({8123}),
    )
    monkeypatch.setattr(
        controller,
        "_fast_dft_churn_guard",
        lambda _client, _status: guarded.append(rounds) or True,
    )

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons == ()
    assert rounds == 5
    assert guarded == [1, 2, 3, 4]


def test_full_audit_retries_more_than_three_exact_nvml_additions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    authority = dft_resident_authority()
    resident = dft_broad_snapshot(
        monkeypatch,
        status,
        authority,
        session.TargetSnapshot((8123,), (), ()),
    )
    changing = dft_broad_snapshot(
        monkeypatch,
        status,
        authority,
        empty_snapshot(),
    )
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        snapshot=resident,
        authority=authority,
        trailing_compute=frozenset({8123}),
    )
    rounds = 0

    def collect(_client, **_kwargs):
        nonlocal rounds
        rounds += 1
        return status, changing if rounds <= 4 else resident

    guarded: list[int] = []
    monkeypatch.setattr(session, "consistent_broker_snapshot", collect)
    monkeypatch.setattr(
        controller,
        "_fast_dft_churn_guard",
        lambda _client, _status: guarded.append(rounds) or True,
    )

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons == ()
    assert rounds == 5
    assert guarded == [1, 2, 3, 4]


def test_full_audit_rejects_stable_unknown_mps_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    authority = mps_snapshot(
        server_pids=frozenset({7001}),
        client_pids=frozenset({9999}),
        declarers=frozenset(
            {mps_declarer(7000), mps_declarer(7001, 101)}
        ),
    )
    client = patch_full_audit_runtime(
        monkeypatch,
        controller,
        status,
        authority=authority,
    )
    monkeypatch.setattr(
        controller,
        "_exact_dft_descendants",
        lambda _status, _pids: False,
    )

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons == ("unknown private MPS client PID(s): 9999",)


def test_warmup_full_audit_strictly_rejects_initial_and_trailing_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {
        "broker_instance_id": "broker",
        "next_fencing_token": 2,
        "draining": False,
        "leases": [dft_residency_record()],
    }
    claim = SimpleNamespace(
        container_id="a" * 64,
        registration_id="backend-dev",
        component="backend",
        environment="dev",
        compose_project="nexpoly_dev",
        compose_service="backend",
        gpu_uuids=frozenset({session.GPU_UUID}),
    )
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    initial = session.SessionController(run, "a" * 40, "b" * 40)
    initial_client = patch_full_audit_runtime(
        monkeypatch,
        initial,
        status,
        snapshot=session.TargetSnapshot((), (claim,), ()),
    )
    _status, _snapshot, reasons = initial._audit(initial_client)
    assert "Docker declared GPU1 during DFT-only stabilization" in reasons

    trailing = session.SessionController(run, "a" * 40, "b" * 40)
    trailing_client = patch_full_audit_runtime(
        monkeypatch,
        trailing,
        status,
        trailing_docker=(claim,),
    )
    _status, _snapshot, reasons = trailing._audit(trailing_client)
    assert "Docker declared GPU1 during DFT-only stabilization" in reasons


def test_steady_full_audit_retains_the_12_second_churn_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.dft_warmup_open = False
    observed: dict[str, object] = {}

    def collect(_client, **kwargs):
        observed.update(kwargs)
        raise RuntimeError("stop after argument capture")

    monkeypatch.setattr(session, "consistent_broker_snapshot", collect)
    monkeypatch.setattr(controller, "_mps_authority", mps_snapshot)
    client = SimpleNamespace(
        status=lambda: {
            "broker_instance_id": "broker",
            "next_fencing_token": 1,
            "draining": False,
            "leases": [],
        }
    )
    with pytest.raises(RuntimeError, match="argument capture"):
        controller._audit(client)

    assert observed["membership_churn_retries"] == 8
    assert observed["membership_churn_timeout_seconds"] == 12.0
    assert observed["membership_churn_guard"] is None


def test_down_waits_for_exact_owned_lease_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = {"pid": 123, "start_ticks": 456}
    exists = iter((True, True, False))
    ticks = iter((0.0, 0.0, 1.0))
    signals: list[int] = []
    sleeps: list[float] = []
    monkeypatch.setattr(session, "_controller_record", lambda: record)
    monkeypatch.setattr(
        session,
        "CONTROLLER_RECORD",
        SimpleNamespace(exists=lambda: next(exists)),
    )
    monkeypatch.setattr(session, "process_start_ticks", lambda _pid: 456)
    monkeypatch.setattr(session.os, "pidfd_open", lambda _pid: 9, raising=False)
    monkeypatch.setattr(session.os, "close", lambda _fd: None)
    monkeypatch.setattr(
        session.signal,
        "pidfd_send_signal",
        lambda _fd, value: signals.append(value),
        raising=False,
    )
    monkeypatch.setattr(
        session,
        "status",
        lambda: {"status": "cleanup-blocked"},
    )
    monkeypatch.setattr(session.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(session.time, "sleep", sleeps.append)

    assert session.down_execute() == {
        "schema_version": 1,
        "status": "stopped",
        "gpu_index": 1,
    }
    assert signals == [session.signal.SIGTERM]
    assert sleeps == [0.25]


def test_down_is_idempotent_after_controller_finished_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session,
        "CONTROLLER_RECORD",
        SimpleNamespace(exists=lambda: False, is_symlink=lambda: False),
    )
    stopped = {"schema_version": 1, "status": "stopped", "gpu_index": 1}
    monkeypatch.setattr(session, "status", lambda: stopped)
    monkeypatch.setattr(
        session,
        "_controller_record",
        lambda: (_ for _ in ()).throw(
            AssertionError("must not read a missing record")
        ),
    )

    assert session.down_execute() == stopped


def test_down_times_out_when_owned_lease_never_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = {"pid": 123, "start_ticks": 456}
    ticks = iter((0.0, 0.0, 61.0))
    monkeypatch.setattr(session, "_controller_record", lambda: record)
    monkeypatch.setattr(
        session,
        "CONTROLLER_RECORD",
        SimpleNamespace(exists=lambda: True),
    )
    monkeypatch.setattr(session, "process_start_ticks", lambda _pid: 456)
    monkeypatch.setattr(session.os, "pidfd_open", lambda _pid: 9, raising=False)
    monkeypatch.setattr(session.os, "close", lambda _fd: None)
    monkeypatch.setattr(
        session.signal,
        "pidfd_send_signal",
        lambda *_args: None,
        raising=False,
    )
    monkeypatch.setattr(
        session,
        "status",
        lambda: {"status": "cleanup-blocked"},
    )
    monkeypatch.setattr(session.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(session.time, "sleep", lambda _seconds: None)

    with pytest.raises(session.DevGpuSessionError, match="timed out"):
        session.down_execute()


def test_drain_stops_audits_after_broker_admission_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gpu_resource

    record = {"pid": 123, "start_ticks": 456}
    signals: list[int] = []
    closes: list[int] = []
    draining: list[bool] = []
    monkeypatch.setattr(session, "_controller_record", lambda: record)
    monkeypatch.setattr(session, "process_start_ticks", lambda _pid: 456)
    monkeypatch.setattr(session.os, "pidfd_open", lambda _pid: 9, raising=False)
    monkeypatch.setattr(session.os, "close", closes.append)
    monkeypatch.setattr(
        session.signal,
        "pidfd_send_signal",
        lambda _fd, value: signals.append(value),
        raising=False,
    )
    monkeypatch.setattr(
        gpu_resource,
        "GpuBrokerClient",
        lambda _path: SimpleNamespace(
            set_draining=lambda value: (
                draining.append(value) or {"draining": True, "leases": []}
            )
        ),
    )
    monkeypatch.setattr(
        session,
        "status",
        lambda: {"schema_version": 1, "status": "stopped", "gpu_index": 1},
    )

    assert session.drain_execute() == {"draining": True, "leases": []}
    assert draining == [True]
    assert signals == [session.signal.SIGTERM]
    assert closes == [9]


def test_drain_never_signals_a_replaced_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gpu_resource

    before = {"pid": 123, "start_ticks": 456}
    after = {"pid": 124, "start_ticks": 789}
    records = iter((before, after))
    signals: list[int] = []
    monkeypatch.setattr(session, "_controller_record", lambda: next(records))
    monkeypatch.setattr(session, "process_start_ticks", lambda _pid: 456)
    monkeypatch.setattr(session.os, "pidfd_open", lambda _pid: 9, raising=False)
    monkeypatch.setattr(session.os, "close", lambda _fd: None)
    monkeypatch.setattr(
        session.signal,
        "pidfd_send_signal",
        lambda _fd, value: signals.append(value),
        raising=False,
    )
    monkeypatch.setattr(
        gpu_resource,
        "GpuBrokerClient",
        lambda _path: SimpleNamespace(
            set_draining=lambda _value: {"draining": True, "leases": []}
        ),
    )

    with pytest.raises(session.DevGpuSessionError, match="changed during drain"):
        session.drain_execute()

    assert signals == []


def test_stabilize_command_waits_for_matching_post_signal_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "d" * 32
    record = {
        "pid": 123,
        "start_ticks": 456,
        "session_id": session_id,
        "run_directory": str(tmp_path),
    }
    baseline = {
        "status": "plane-ready",
        "audit_mode": "full",
        "dft_stabilized": False,
        "activation_generation": 0,
        "full_audit_generation": 3,
        "dft_stabilization_generation": 0,
        "last_audit_stabilization_generation": 0,
    }
    stale = {
        **baseline,
        "dft_stabilized": True,
        "full_audit_generation": 4,
        "dft_stabilization_generation": 1,
    }
    accepted = {
        **stale,
        "last_audit_stabilization_generation": 1,
    }
    states = iter((baseline, stale, accepted))
    signals: list[int] = []
    ticks = iter((0.0, 1.0, 2.0))
    monkeypatch.setattr(session, "_controller_record", lambda: record)
    monkeypatch.setattr(session, "status", lambda: next(states))
    monkeypatch.setattr(
        session.os,
        "pidfd_open",
        lambda _pid: 9,
        raising=False,
    )
    monkeypatch.setattr(session.os, "close", lambda _fd: None)
    monkeypatch.setattr(
        session.signal,
        "pidfd_send_signal",
        lambda _fd, value: signals.append(value),
        raising=False,
    )
    monkeypatch.setattr(session.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(session.time, "sleep", lambda _seconds: None)

    result = session.stabilize_execute(session_id)

    assert result == accepted
    assert signals == [session.signal.SIGUSR2]


def test_activate_command_waits_for_matching_post_signal_full_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "d" * 32
    source_sha = "a" * 40
    source_tree = "b" * 40
    record = {
        "pid": 123,
        "start_ticks": 456,
        "session_id": session_id,
        "run_directory": str(tmp_path),
        "source_sha": source_sha,
        "source_tree": source_tree,
    }
    baseline = {
        "status": "plane-ready",
        "audit_mode": "full",
        "dft_stabilized": True,
        "activation_generation": 0,
        "last_audit_activation_generation": 0,
        "full_audit_generation": 7,
    }
    stale = {
        **baseline,
        "status": "ready",
        "activation_generation": 1,
        "full_audit_generation": 8,
    }
    accepted = {**stale, "last_audit_activation_generation": 1}
    states = iter((baseline, stale, accepted))
    worker = {
        "session_id": session_id,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "worker_lock_sha256": "sha256:" + "c" * 64,
    }
    manifest = {
        "schema_version": 1,
        "session_id": session_id,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "backend_container_id": "d" * 64,
        "backend_image_id": "sha256:" + "e" * 64,
        "backend_config_hash": "f" * 64,
        "md_process": worker,
        "dft_process": worker,
    }
    signals: list[int] = []
    ticks = iter((0.0, 1.0, 2.0))
    monkeypatch.setattr(session, "_controller_record", lambda: record)
    monkeypatch.setattr(session, "status", lambda: next(states))
    monkeypatch.setattr(session, "_load_private_json", lambda _path: manifest)
    monkeypatch.setattr(
        session.os,
        "pidfd_open",
        lambda _pid: 9,
        raising=False,
    )
    monkeypatch.setattr(session.os, "close", lambda _fd: None)
    monkeypatch.setattr(
        session.signal,
        "pidfd_send_signal",
        lambda _fd, value: signals.append(value),
        raising=False,
    )
    monkeypatch.setattr(session.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(session.time, "sleep", lambda _seconds: None)

    result = session.activate_execute(session_id)

    assert result == accepted
    assert signals == [session.signal.SIGUSR1]


def test_automatic_recovery_restores_cpu_before_waiting_for_mps_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.stop_requested = True
    controller.automatic_recovery = True
    controller.gpu3_guard = {"guard": "same"}
    order: list[str] = []
    monkeypatch.setattr(
        controller,
        "_recovery_command",
        lambda command: order.append(command) or True,
    )
    monkeypatch.setattr(controller, "_cleanup", lambda _client: order.append("cleanup-mps") or True)
    monkeypatch.setattr(controller, "_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "_remove_controller_record", lambda: None)
    monkeypatch.setattr(session, "read_gpu3_guard_fingerprint", lambda: {"guard": "same"})

    assert controller._serve_loop(SimpleNamespace()) == 0
    assert order == [
        "gpu-session-stop-owned-internal",
        "gpu-session-restore-cpu-internal",
        "cleanup-mps",
    ]


def _late_dft_status(
    monkeypatch: pytest.MonkeyPatch,
    controller: session.SessionController,
    *,
    owner_session_id: str | None = None,
    workload_session_id: str | None = None,
) -> dict[str, object]:
    from ops.gpu_broker import broker, server

    lease = dft_residency_record()
    lease.update(
        owner_pid=7000,
        owner_process_start_ticks=123,
        owner_boot_id="boot",
    )
    owner_environment = {
        "MONOMER_DFT_DEPLOYMENT": "dev",
        "NEXPOLY_DEV_GPU1_ONLY_SESSION": "1",
        "NEXPOLY_DEV_GPU_SESSION_ID": (
            controller.session_id
            if owner_session_id is None
            else owner_session_id
        ),
        "NEXPOLY_DFT_GPU_DEVICE": "1",
        "NEXPOLY_DFT_OVERFLOW_GPU_DEVICES": "",
    }
    workload_environment = {
        **owner_environment,
        "NEXPOLY_DEV_GPU_SESSION_ID": (
            controller.session_id
            if workload_session_id is None
            else workload_session_id
        ),
    }
    environments = {
        7000: owner_environment,
        8123: {
            **workload_environment,
            "MONOMER_DFT_EXECUTOR_PROCESS": "1",
            "NEXPOLY_DFT_EXECUTOR_GPU_DEVICE": "1",
            "NEXPOLY_DFT_EXECUTOR_GPU_UUID": session.GPU_UUID,
        },
    }
    monkeypatch.setattr(broker, "read_boot_id", lambda: "boot")
    monkeypatch.setattr(
        broker,
        "process_identity_alive",
        lambda owner, *, current_boot_id: (
            owner.pid == 7000
            and owner.process_start_ticks == 123
            and owner.boot_id == current_boot_id == "boot"
        ),
    )
    monkeypatch.setattr(
        server,
        "process_is_exact_dft_residency_descendant",
        lambda pid, _lease, *, index, uuid: (
            pid == 8123 and index == 1 and uuid == session.GPU_UUID
        ),
    )
    monkeypatch.setattr(
        server,
        "_read_process_environment",
        lambda pid: dict(environments[pid]),
    )
    return {"draining": True, "leases": [lease]}


def test_late_dft_recovery_binds_owner_and_workload_to_exact_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    status = _late_dft_status(monkeypatch, controller)

    assert controller._exact_session_owned_late_dft_lease(status) == (
        "d1" * 16,
        1,
    )


@pytest.mark.parametrize(
    "residue",
    ("foreign-owner", "foreign-workload", "mixed-inventory", "unknown-lease"),
)
def test_late_recovery_keeps_foreign_or_unknown_lease_isolated(
    residue: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.automatic_recovery = True
    controller.owned_components_stopped = True
    if residue == "foreign-owner":
        status = _late_dft_status(
            monkeypatch,
            controller,
            owner_session_id="e" * 32,
        )
    elif residue == "foreign-workload":
        status = _late_dft_status(
            monkeypatch,
            controller,
            workload_session_id="e" * 32,
        )
    elif residue == "mixed-inventory":
        status = _late_dft_status(monkeypatch, controller)
        status["leases"] = [*status["leases"], md_execution_record()]
    else:
        status = {"draining": True, "leases": [md_execution_record()]}
    states: list[str] = []
    monkeypatch.setattr(
        controller,
        "_recovery_command",
        lambda _command: pytest.fail("foreign/unknown residue must not be stopped"),
    )
    monkeypatch.setattr(
        controller,
        "_state",
        lambda value, **_kwargs: states.append(value),
    )

    assert controller._cleanup(
        SimpleNamespace(set_draining=lambda _value: status)
    ) is False
    assert controller.late_session_owned_stop_attempts == 0
    assert states == ["cleanup-blocked"]


def test_failed_late_exact_dft_stop_sweeps_remain_isolation_waiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.automatic_recovery = True
    controller.owned_components_stopped = True
    states: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        controller,
        "_exact_session_owned_late_dft_lease",
        lambda _status: ("d1" * 16, 1),
    )
    monkeypatch.setattr(
        session.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="late Worker identity is not ready",
        ),
    )
    monkeypatch.setattr(
        controller,
        "_state",
        lambda value, **kwargs: states.append((value, kwargs)),
    )

    assert controller._retry_late_session_owned_stop({}) is True
    assert controller._retry_late_session_owned_stop({}) is True
    assert controller.late_session_owned_stop_attempts == 2
    assert [value for value, _extra in states] == [
        "isolation-waiting",
        "isolation-waiting",
    ]
    assert all(
        extra == {
            "contaminated": True,
            "recovery_command": "gpu-session-stop-owned-internal",
            "recovery_error": "late Worker identity is not ready",
        }
        for _value, extra in states
    )


def test_automatic_recovery_rescans_and_stops_late_exact_dft_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.stop_requested = True
    controller.automatic_recovery = True
    controller.gpu3_guard = {"guard": "same"}
    controller.broker = SimpleNamespace(terminate=lambda: None, wait=lambda timeout: 0)
    late_lease_active = True
    commands: list[str] = []
    states: list[str] = []
    broker_status_calls = 0

    def recover(command: str) -> bool:
        nonlocal late_lease_active
        commands.append(command)
        if command == "gpu-session-stop-owned-internal" and commands.count(command) == 2:
            late_lease_active = False
        return True

    def set_draining(_value: bool) -> dict[str, object]:
        nonlocal broker_status_calls
        broker_status_calls += 1
        if broker_status_calls > 3:
            raise AssertionError("late owned recovery did not converge")
        return {
            "draining": True,
            "leases": (
                [{"gpu_uuid": session.GPU_UUID}]
                if late_lease_active
                else []
            ),
        }

    monkeypatch.setattr(controller, "_recovery_command", recover)
    monkeypatch.setattr(
        controller,
        "_exact_session_owned_late_dft_lease",
        lambda status: ("d1" * 16, 1) if status["leases"] else None,
    )
    monkeypatch.setattr(controller, "_cleanup_owned_tree", lambda: None)
    monkeypatch.setattr(
        controller,
        "_state",
        lambda value, **_kwargs: states.append(value),
    )
    monkeypatch.setattr(controller, "_remove_controller_record", lambda: None)
    monkeypatch.setattr(session, "GPU_ROOT", tmp_path / "gpu-resource")
    monkeypatch.setattr(session, "read_gpu3_guard_fingerprint", lambda: {"guard": "same"})
    monkeypatch.setattr(session.time, "sleep", lambda _seconds: None)

    assert controller._serve_loop(SimpleNamespace(set_draining=set_draining)) == 0
    assert commands == [
        "gpu-session-stop-owned-internal",
        "gpu-session-restore-cpu-internal",
        "gpu-session-stop-owned-internal",
    ]
    assert controller.late_session_owned_stop_attempts == 1
    assert states[-1] == "recovered"


@pytest.mark.parametrize("command_succeeds", (True, False))
def test_late_exact_dft_stop_retries_are_bounded(
    command_succeeds: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.automatic_recovery = True
    controller.owned_components_stopped = True
    status = {
        "draining": True,
        "leases": [{"gpu_uuid": session.GPU_UUID}],
    }
    commands: list[str] = []
    states: list[str] = []
    monkeypatch.setattr(
        controller,
        "_exact_session_owned_late_dft_lease",
        lambda _status: ("d1" * 16, 1),
    )
    monkeypatch.setattr(
        controller,
        "_recovery_command",
        lambda command: commands.append(command) or command_succeeds,
    )
    monkeypatch.setattr(
        controller,
        "_state",
        lambda value, **_kwargs: states.append(value),
    )
    client = SimpleNamespace(set_draining=lambda _value: status)

    results = [
        controller._cleanup(client)
        for _ in range(session.LATE_SESSION_OWNED_STOP_ATTEMPTS + 2)
    ]

    assert results == [False] * (session.LATE_SESSION_OWNED_STOP_ATTEMPTS + 2)
    assert commands == ["gpu-session-stop-owned-internal"] * 8
    assert controller.late_session_owned_stop_attempts == 8
    assert states[-2:] == ["cleanup-blocked", "cleanup-blocked"]


def test_ready_is_published_only_after_explicit_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.activation_generation = 1
    controller.last_audit_activation_generation = 1
    controller.dft_stabilized = True
    controller.last_mps_authority = mps_snapshot(
        server_pids=frozenset({101})
    )
    controller.gpu3_guard = {"guard": "same"}
    states: list[str] = []
    monkeypatch.setattr(controller, "_audit", lambda _client: ({}, empty_snapshot(), ()))
    monkeypatch.setattr(controller, "_cleanup", lambda _client: True)
    monkeypatch.setattr(controller, "_remove_controller_record", lambda: None)
    monkeypatch.setattr(session, "read_gpu3_guard_fingerprint", lambda: {"guard": "same"})

    def state(value: str, **_kwargs) -> None:
        states.append(value)
        if value == "ready":
            controller.stop_requested = True

    monkeypatch.setattr(controller, "_state", state)

    assert controller._serve_loop(SimpleNamespace()) == 0
    assert states[:2] == ["ready", "stopped"]


def test_graceful_stop_discards_an_inflight_audit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.gpu3_guard = {"guard": "same"}
    states: list[str] = []

    def interrupted_audit(_client):
        controller.stop_requested = True
        raise session.DevGpuSessionError("systemd membership changed")

    monkeypatch.setattr(controller, "_audit", interrupted_audit)
    monkeypatch.setattr(controller, "_cleanup", lambda _client: True)
    monkeypatch.setattr(controller, "_state", lambda value, **_kwargs: states.append(value))
    monkeypatch.setattr(controller, "_remove_controller_record", lambda: None)
    monkeypatch.setattr(
        controller,
        "_recovery_command",
        lambda _command: pytest.fail("graceful stop must not enter recovery"),
    )
    monkeypatch.setattr(
        session,
        "read_gpu3_guard_fingerprint",
        lambda: {"guard": "same"},
    )

    assert controller._serve_loop(SimpleNamespace()) == 0
    assert controller.automatic_recovery is False
    assert states == ["stopped"]


def test_pre_plane_failure_removes_controller_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    record = runtime / "gpu-session/controller.json"
    run = runtime / "gpu-session/runs" / ("run-" + "d" * 32)
    run.mkdir(parents=True)
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    monkeypatch.setattr(session, "CONTROLLER_RECORD", record)
    monkeypatch.setattr(session, "_private_directory", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(session, "process_start_ticks", lambda _pid: 123)
    monkeypatch.setattr(session, "process_argv", lambda _pid: ("python", "serve"))
    monkeypatch.setattr(session.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(session.os, "getegid", lambda: 1001)
    monkeypatch.setattr(
        session,
        "read_gpu3_guard_fingerprint",
        lambda: (_ for _ in ()).throw(session.DevGpuSessionError("fingerprint failed")),
    )

    with pytest.raises(session.DevGpuSessionError, match="fingerprint failed"):
        controller.run(("python", "serve"))

    assert not record.exists()
