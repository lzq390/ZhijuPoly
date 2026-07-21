from __future__ import annotations

import json
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
    client = SimpleNamespace(
        status=lambda: {"broker_instance_id": "b", "draining": False, "leases": []}
    )
    monkeypatch.setattr(
        session,
        "consistent_broker_snapshot",
        lambda _client: (client.status(), empty_snapshot()),
    )
    monkeypatch.setattr(controller, "_authorized_mps", lambda: frozenset({7001}))
    monkeypatch.setattr(controller, "_mps_client_pids", lambda: frozenset({8123}))

    _status, _snapshot, reasons = controller._audit(client)

    assert reasons == ("unknown private MPS client PID(s): 8123",)


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
    empty = {"broker_instance_id": "broker", "draining": False, "leases": []}
    lease = {
        "lease_id": "lease-1",
        "fencing_token": 1,
        "gpu_uuid": session.GPU_UUID,
        "owner_pid": 77,
        "workload_pid": 88,
        "status": "active",
    }
    active = {**empty, "leases": [lease]}
    statuses = iter((empty, active, active, active))
    client = SimpleNamespace(status=lambda: next(statuses))
    snapshots = iter((empty_snapshot(), session.TargetSnapshot((88,), (), ())))

    status, snapshot = session.consistent_broker_snapshot(
        client, lambda: next(snapshots)
    )

    assert status == active
    assert snapshot.process_pids == (88,)


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


def test_ready_is_published_only_after_explicit_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / ("run-" + "d" * 32)
    run.mkdir()
    controller = session.SessionController(run, "a" * 40, "b" * 40)
    controller.activation_requested = True
    controller.gpu3_guard = {"guard": "same"}
    states: list[str] = []
    monkeypatch.setattr(controller, "_audit", lambda _client: ({}, empty_snapshot(), ()))
    monkeypatch.setattr(controller, "_authorized_mps", lambda: frozenset({101}))
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
