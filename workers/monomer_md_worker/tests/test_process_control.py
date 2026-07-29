from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from gpu_resource import GpuBrokerClientError, transient_scope_command
from workers.monomer_md_worker.app import process_control


def _identity_scope_command(
    _lease_id: object,
    command: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(command)


def _identity_scope_membership(
    _pid: int,
    _lease_id: object,
) -> int:
    return 1


class _Process:
    pid = 43_210
    returncode = 0

    async def wait(self) -> int:
        return 0


class _Lease:
    def __init__(self, events: list[object], *, prepare_fails: bool = False) -> None:
        self.events = events
        self.prepare_fails = prepare_fails
        self.failed_closed = False
        self.quarantined = False

    def prepare_process_termination(self) -> dict[str, object]:
        self.events.append("mps-terminate-client")
        if self.prepare_fails:
            raise GpuBrokerClientError(
                "gpu_runtime_unhealthy",
                "MPS context could not be made inactive",
            )
        return {
            "safe_to_signal": True,
            "client_pids": [99],
            "prepared_at": 1.0,
        }

    def fail_closed(self) -> None:
        self.failed_closed = True
        self.events.append("fail-closed")

    def quarantine(self, *, reason: str) -> dict[str, object]:
        assert reason == "gpu_runtime_corruption"
        self.quarantined = True
        self.events.append("quarantine")
        return {"reason": reason}


def test_broker_disabled_spawn_never_creates_a_transient_scope() -> None:
    calls: list[object] = []

    async def scenario() -> int:
        process = await process_control.create_fenced_subprocess_exec(
            [sys.executable, "-c", "pass"],
            execution_lease=None,
            scope_command_builder=lambda *_args: calls.append(_args),
        )
        return await process.wait()

    assert asyncio.run(scenario()) == 0
    assert calls == []


def test_mps_context_is_terminated_before_any_posix_signal(monkeypatch) -> None:
    events: list[object] = []
    group_states = iter((True, False))
    monkeypatch.setattr(
        process_control,
        "_process_group_alive",
        lambda _pid: next(group_states, False),
    )
    monkeypatch.setattr(
        process_control.os,
        "killpg",
        lambda _pid, sent_signal: events.append(sent_signal),
    )
    lease = _Lease(events)

    asyncio.run(
        process_control.terminate_process_group(
            _Process(),  # type: ignore[arg-type]
            process_already_waited=True,
            execution_lease=lease,  # type: ignore[arg-type]
        )
    )

    assert events == ["mps-terminate-client", signal.SIGTERM]
    assert lease.failed_closed is False


def test_completed_fenced_process_defers_empty_scope_proof_to_lease_release(
    monkeypatch,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(process_control, "_process_group_alive", lambda _pid: False)
    lease = _Lease(events)

    asyncio.run(
        process_control.terminate_process_group(
            _Process(),  # type: ignore[arg-type]
            process_already_waited=True,
            execution_lease=lease,  # type: ignore[arg-type]
        )
    )

    assert events == []
    assert lease.failed_closed is False


def test_mps_termination_failure_sends_no_signal_and_never_releases(monkeypatch) -> None:
    events: list[object] = []
    monkeypatch.setattr(process_control, "_process_group_alive", lambda _pid: True)
    monkeypatch.setattr(
        process_control.os,
        "killpg",
        lambda _pid, sent_signal: events.append(sent_signal),
    )
    lease = _Lease(events, prepare_fails=True)

    with pytest.raises(GpuBrokerClientError) as error:
        asyncio.run(
            process_control.terminate_process_group(
                _Process(),  # type: ignore[arg-type]
                process_already_waited=True,
                execution_lease=lease,  # type: ignore[arg-type]
            )
        )

    assert error.value.code == "gpu_runtime_unhealthy"
    assert events == ["mps-terminate-client", "fail-closed"]
    assert lease.failed_closed is True


def test_signal_failure_quarantines_gpu_and_keeps_lease_fail_closed(monkeypatch) -> None:
    events: list[object] = []
    monkeypatch.setattr(process_control, "_process_group_alive", lambda _pid: True)

    def fail_signal(_pid: int, _sent_signal: signal.Signals) -> None:
        raise PermissionError("host PID namespace mismatch")

    monkeypatch.setattr(process_control.os, "killpg", fail_signal)
    lease = _Lease(events)

    with pytest.raises(GpuBrokerClientError) as error:
        asyncio.run(
            process_control.terminate_process_group(
                _Process(),  # type: ignore[arg-type]
                process_already_waited=True,
                execution_lease=lease,  # type: ignore[arg-type]
            )
        )

    assert error.value.code == "gpu_runtime_unhealthy"
    assert events == [
        "mps-terminate-client",
        "quarantine",
        "fail-closed",
    ]
    assert lease.quarantined is True
    assert lease.failed_closed is True


def test_cancel_during_host_registration_keeps_gate_until_pid_is_registered() -> None:
    started = threading.Event()
    allow_registration = threading.Event()

    class Lease:
        workload_pid: int | None = None
        lease = SimpleNamespace(lease_id="a" * 32)

        def register_workload(self, workload_pid: int) -> None:
            self.workload_pid = workload_pid
            started.set()
            allow_registration.wait(timeout=2)

        def prepare_process_termination(self) -> dict[str, object]:
            return {
                "safe_to_signal": True,
                "client_pids": [],
                "prepared_at": 1.0,
                "freeze_token": "test",
            }

    lease = Lease()

    async def scenario() -> None:
        task = asyncio.create_task(
            process_control.create_fenced_subprocess_exec(
                [sys.executable, "-c", "raise AssertionError('gate opened')"],
                execution_lease=lease,  # type: ignore[arg-type]
                scope_command_builder=_identity_scope_command,
                scope_membership_waiter=_identity_scope_membership,
            )
        )
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert lease.workload_pid is not None
        assert process_control._process_group_alive(lease.workload_pid) is True
        allow_registration.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert lease.workload_pid is not None
    assert process_control._process_group_alive(lease.workload_pid) is False


def test_cancel_during_scope_transition_registers_before_cleanup_without_opening_gate() -> None:
    transition_started = threading.Event()
    allow_transition = threading.Event()
    transitioned_pid: list[int] = []
    registrations: list[int] = []

    class Lease:
        lease = SimpleNamespace(lease_id="e" * 32)

        def register_workload(self, workload_pid: int) -> None:
            registrations.append(workload_pid)

    def wait_for_transition(pid: int, _lease_id: object) -> int:
        transitioned_pid.append(pid)
        transition_started.set()
        allow_transition.wait(timeout=2)
        return 1

    async def scenario() -> None:
        task = asyncio.create_task(
            process_control.create_fenced_subprocess_exec(
                [sys.executable, "-c", "raise AssertionError('gate opened')"],
                execution_lease=Lease(),  # type: ignore[arg-type]
                scope_command_builder=_identity_scope_command,
                scope_membership_waiter=wait_for_transition,
            )
        )
        assert await asyncio.to_thread(transition_started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        allow_transition.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert registrations == transitioned_pid
    assert len(transitioned_pid) == 1
    assert process_control._process_group_alive(transitioned_pid[0]) is False


def test_cancel_during_subprocess_creation_collects_and_registers_gated_child(
    monkeypatch,
) -> None:
    real_create_subprocess_exec = asyncio.create_subprocess_exec
    spawn_created = threading.Event()
    allow_spawn_result = asyncio.Event()
    spawned_pid: list[int] = []
    registrations: list[int] = []

    class Lease:
        lease = SimpleNamespace(lease_id="a" * 32)

        def register_workload(self, workload_pid: int) -> None:
            registrations.append(workload_pid)

    async def delayed_create_subprocess_exec(*args, **kwargs):
        process = await real_create_subprocess_exec(*args, **kwargs)
        spawned_pid.append(process.pid)
        spawn_created.set()
        await allow_spawn_result.wait()
        return process

    monkeypatch.setattr(
        process_control.asyncio,
        "create_subprocess_exec",
        delayed_create_subprocess_exec,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            process_control.create_fenced_subprocess_exec(
                [sys.executable, "-c", "raise AssertionError('gate opened')"],
                execution_lease=Lease(),  # type: ignore[arg-type]
                scope_command_builder=_identity_scope_command,
                scope_membership_waiter=_identity_scope_membership,
            )
        )
        assert await asyncio.to_thread(spawn_created.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        allow_spawn_result.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert registrations == spawned_pid
    assert len(spawned_pid) == 1
    assert process_control._process_group_alive(spawned_pid[0]) is False


def test_fenced_spawn_does_not_run_sitecustomize_before_registration(
    tmp_path: Path,
) -> None:
    registration_started = threading.Event()
    allow_registration = threading.Event()
    sitecustomize_marker = tmp_path / "sitecustomize-ran"
    target_marker = tmp_path / "target-ran"
    (tmp_path / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(sitecustomize_marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )

    class Lease:
        lease = SimpleNamespace(lease_id="b" * 32)

        def register_workload(self, _workload_pid: int) -> None:
            registration_started.set()
            allow_registration.wait(timeout=2)

    async def scenario() -> None:
        task = asyncio.create_task(
            process_control.create_fenced_subprocess_exec(
                [
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; "
                        f"Path({str(target_marker)!r}).write_text('ran')"
                    ),
                ],
                execution_lease=Lease(),  # type: ignore[arg-type]
                env={**os.environ, "PYTHONPATH": str(tmp_path)},
                scope_command_builder=_identity_scope_command,
                scope_membership_waiter=_identity_scope_membership,
            )
        )
        assert await asyncio.to_thread(registration_started.wait, 1)
        await asyncio.sleep(0.05)
        assert not sitecustomize_marker.exists()
        assert not target_marker.exists()
        allow_registration.set()
        process = await task
        assert await process.wait() == 0

    asyncio.run(scenario())
    assert sitecustomize_marker.is_file()
    assert target_marker.is_file()


def test_systemd_scope_exec_preserves_pid_gate_fds_environment_and_stdout(
    tmp_path: Path,
) -> None:
    systemd_run = tmp_path / "bin" / "systemd-run"
    systemd_run.parent.mkdir()
    arguments_path = tmp_path / "scope-arguments"
    systemd_run.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {arguments_path}\n"
        "while [ \"$1\" != '--' ]; do shift; done\n"
        "shift\n"
        "exec \"$@\"\n",
        encoding="utf-8",
    )
    systemd_run.chmod(0o700)
    lease_id = "d" * 32
    registered: list[int] = []

    class Lease:
        lease = SimpleNamespace(lease_id=lease_id)

        def register_workload(self, workload_pid: int) -> None:
            registered.append(workload_pid)

    async def scenario() -> tuple[int, bytes]:
        process = await process_control.create_fenced_subprocess_exec(
            [
                sys.executable,
                "-c",
                (
                    "import os; "
                    "print(f'target:{os.getpid()}:{os.environ[\"XDG_RUNTIME_DIR\"]}:'"
                    "f'{os.environ[\"DBUS_SESSION_BUS_ADDRESS\"]}', flush=True)"
                ),
            ],
            execution_lease=Lease(),  # type: ignore[arg-type]
            scope_command_builder=lambda exact_lease_id, command: (
                transient_scope_command(
                    exact_lease_id,
                    command,
                    systemd_run=systemd_run,
                )
            ),
            scope_membership_waiter=_identity_scope_membership,
            stdout=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "XDG_RUNTIME_DIR": "/tmp/forged-runtime",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/forged-bus",
            },
        )
        stdout, _stderr = await process.communicate()
        return process.pid, stdout

    pid, stdout = asyncio.run(scenario())
    assert registered == [pid]
    user_runtime = f"/run/user/{os.geteuid()}"
    assert stdout == (
        f"target:{pid}:{user_runtime}:unix:path={user_runtime}/bus\n".encode()
    )
    arguments = arguments_path.read_text(encoding="utf-8").splitlines()
    assert arguments[:10] == [
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
    ]
    assert arguments[10:14] == [
        sys.executable,
        "-I",
        "-S",
        os.fspath(
            Path(process_control.__file__).resolve().parents[3]
            / "gpu_resource"
            / "exec_gate.py"
        ),
    ]


def test_fenced_spawn_failure_closes_both_gate_descriptors(monkeypatch) -> None:
    async def fail_spawn(*_args, **_kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(
        process_control.asyncio,
        "create_subprocess_exec",
        fail_spawn,
    )

    class Lease:
        lease = SimpleNamespace(lease_id="c" * 32)

    async def scenario() -> None:
        before = len(list(Path("/proc/self/fd").iterdir()))
        for _ in range(64):
            with pytest.raises(OSError, match="spawn failed"):
                await process_control.create_fenced_subprocess_exec(
                    [sys.executable, "-c", "pass"],
                    execution_lease=Lease(),  # type: ignore[arg-type]
                    scope_command_builder=_identity_scope_command,
                    scope_membership_waiter=_identity_scope_membership,
                )
        after = len(list(Path("/proc/self/fd").iterdir()))
        assert after <= before + 1

    asyncio.run(scenario())


def test_repeated_cancel_waits_for_mps_and_process_cleanup(monkeypatch) -> None:
    prepare_started = threading.Event()
    allow_prepare = threading.Event()
    events: list[object] = []
    group_states = iter((True, False))
    monkeypatch.setattr(
        process_control,
        "_process_group_alive",
        lambda _pid: next(group_states, False),
    )
    monkeypatch.setattr(
        process_control.os,
        "killpg",
        lambda _pid, sent_signal: events.append(sent_signal),
    )

    class BlockingLease(_Lease):
        def prepare_process_termination(self) -> dict[str, object]:
            prepare_started.set()
            allow_prepare.wait(timeout=2)
            return super().prepare_process_termination()

    lease = BlockingLease(events)

    async def scenario() -> None:
        task = asyncio.create_task(
            process_control.terminate_process_group(
                _Process(),  # type: ignore[arg-type]
                process_already_waited=True,
                execution_lease=lease,  # type: ignore[arg-type]
            )
        )
        assert await asyncio.to_thread(prepare_started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        allow_prepare.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert events == ["mps-terminate-client", signal.SIGTERM]
    assert lease.failed_closed is False


def test_job_cancel_waits_for_new_mps_client_before_safe_termination(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        process_control,
        "MPS_CLIENT_CANCELLATION_STABILIZATION_SECONDS",
        0.03,
    )
    termination_calls: list[tuple[float, bool]] = []

    class BlockingProcess:
        pid = 43_211
        returncode = None

        def __init__(self) -> None:
            self.finished = asyncio.Event()

        async def wait(self) -> int:
            await self.finished.wait()
            return -signal.SIGTERM

    async def fake_terminate(
        process,
        *,
        process_already_waited,
        execution_lease,
    ) -> None:
        assert execution_lease is not None
        termination_calls.append(
            (time.monotonic(), process_already_waited)
        )
        process.returncode = -signal.SIGTERM
        process.finished.set()

    monkeypatch.setattr(
        process_control,
        "_terminate_process_group",
        fake_terminate,
    )

    async def scenario() -> float:
        process = BlockingProcess()
        task = asyncio.create_task(
            process_control.wait_for_process_group(
                process,  # type: ignore[arg-type]
                timeout_seconds=10,
                execution_lease=_Lease([]),  # type: ignore[arg-type]
            )
        )
        await asyncio.sleep(0)
        cancelled_at = time.monotonic()
        task.cancel()
        await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.monotonic() - cancelled_at

    elapsed = asyncio.run(scenario())

    assert 0.025 <= elapsed < 0.25
    assert len(termination_calls) == 1
    assert termination_calls[0][1] is False


def test_process_group_deadline_constants_bound_real_cleanup(
    monkeypatch,
) -> None:
    signals: list[signal.Signals] = []
    monkeypatch.setattr(
        process_control,
        "MAX_TERMINATION_GRACE_SECONDS",
        0.02,
    )
    monkeypatch.setattr(
        process_control,
        "PROCESS_GROUP_POLL_SECONDS",
        0.005,
    )
    monkeypatch.setattr(
        process_control,
        "PROCESS_GROUP_KILL_OBSERVE_SECONDS",
        0.03,
    )
    monkeypatch.setattr(
        process_control,
        "_process_group_alive",
        lambda _pid: True,
    )
    monkeypatch.setattr(
        process_control.os,
        "killpg",
        lambda _pid, sent_signal: signals.append(sent_signal),
    )

    started = time.monotonic()
    with pytest.raises(
        GpuBrokerClientError,
        match="survived MPS-safe termination",
    ):
        asyncio.run(
            process_control.terminate_process_group(
                _Process(),  # type: ignore[arg-type]
                process_already_waited=True,
            )
        )
    elapsed = time.monotonic() - started

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert 0.04 <= elapsed < 0.25
