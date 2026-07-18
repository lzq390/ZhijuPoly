from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import sys
import threading
import time

import pytest

from gpu_resource import GpuBrokerClientError
from workers.monomer_md_worker.app import process_control


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


def test_cancel_during_host_registration_closes_exec_gate_and_collects_child() -> None:
    started = threading.Event()
    allow_registration = threading.Event()

    class Lease:
        workload_pid: int | None = None

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
            )
        )
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        allow_registration.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert lease.workload_pid is not None
    assert process_control._process_group_alive(lease.workload_pid) is False


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


def test_fenced_spawn_failure_closes_both_gate_descriptors(monkeypatch) -> None:
    async def fail_spawn(*_args, **_kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(
        process_control.asyncio,
        "create_subprocess_exec",
        fail_spawn,
    )

    class Lease:
        pass

    async def scenario() -> None:
        before = len(list(Path("/proc/self/fd").iterdir()))
        for _ in range(64):
            with pytest.raises(OSError, match="spawn failed"):
                await process_control.create_fenced_subprocess_exec(
                    [sys.executable, "-c", "pass"],
                    execution_lease=Lease(),  # type: ignore[arg-type]
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
