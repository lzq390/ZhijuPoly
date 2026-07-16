from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import Awaitable, Sequence
from pathlib import Path
from typing import Any, TypeVar

from gpu_resource import GpuBrokerClientError, ManagedGpuLease


_CleanupResult = TypeVar("_CleanupResult")


async def await_safety_cleanup(
    awaitable: Awaitable[_CleanupResult],
) -> _CleanupResult:
    """Finish one host-safety operation before propagating cancellation.

    A Worker shutdown can cancel a job more than once (for example, the
    outer shutdown timeout can cancel a task that is already unwinding).  A
    single ``asyncio.shield`` only survives the first request.  Keep the
    cleanup in its own task, absorb every caller-side cancellation until that
    task has an authoritative result, then propagate the most recent request.
    """

    cleanup_task = asyncio.ensure_future(awaitable)
    deferred_cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(cleanup_task)
            break
        except asyncio.CancelledError as exc:
            if cleanup_task.done():
                # If the cleanup itself was cancelled, result() re-raises and
                # must not be mistaken for a caller-side deferred request.
                result = cleanup_task.result()
                deferred_cancellation = exc
                break
            deferred_cancellation = exc
    if deferred_cancellation is not None:
        raise deferred_cancellation
    return result


async def create_fenced_subprocess_exec(
    command: Sequence[str],
    *,
    execution_lease: ManagedGpuLease | None,
    **kwargs: Any,
) -> asyncio.subprocess.Process:
    """Start a session child behind a pipe gate, then register its host identity."""

    if execution_lease is None:
        return await asyncio.create_subprocess_exec(
            *command,
            start_new_session=True,
            **kwargs,
        )
    gate_reader, gate_writer = os.pipe()
    env = dict(kwargs.pop("env", os.environ))
    repository_root = Path(__file__).resolve().parents[3]
    exec_gate = repository_root / "gpu_resource" / "exec_gate.py"
    if not exec_gate.is_file() or exec_gate.is_symlink():
        _close_fd(gate_reader)
        _close_fd(gate_writer)
        raise GpuBrokerClientError(
            "gpu_runtime_unhealthy",
            "audited GPU execution gate is unavailable",
        )
    env["NEXPOLY_GPU_EXEC_GATE_FD"] = str(gate_reader)
    try:
        try:
            process = await asyncio.create_subprocess_exec(
                # Isolated/no-site startup is part of the fence.  In
                # particular, target PYTHONPATH/sitecustomize must not run
                # before the Broker has registered this exact host PID.
                sys.executable,
                "-I",
                "-S",
                str(exec_gate),
                "--",
                *command,
                env=env,
                pass_fds=(gate_reader,),
                start_new_session=True,
                **kwargs,
            )
        except BaseException:
            _close_fd(gate_writer)
            raise
    finally:
        _close_fd(gate_reader)
    registration_task = asyncio.create_task(
        asyncio.to_thread(execution_lease.register_workload, process.pid)
    )
    try:
        await asyncio.shield(registration_task)
    except BaseException:
        # Cancellation cannot cancel the host-side registration thread.  Keep
        # the exec gate closed, collect the authoritative registration result,
        # and prove the dedicated cgroup empty before returning control.
        _close_fd(gate_writer)
        await await_safety_cleanup(
            _cleanup_failed_fenced_spawn(
                process,
                registration_task,
                execution_lease,
            )
        )
        raise
    try:
        os.write(gate_writer, b"1")
    except Exception:
        _close_fd(gate_writer)
        await await_safety_cleanup(
            _cleanup_registered_fenced_spawn(process, execution_lease)
        )
        raise
    finally:
        _close_fd(gate_writer)
    return process


async def wait_for_process_group(
    process: asyncio.subprocess.Process,
    *,
    timeout_seconds: float,
    execution_lease: ManagedGpuLease | None,
) -> int:
    process_wait = asyncio.create_task(process.wait())
    lease_wait: asyncio.Task[None] | None = None
    if execution_lease is not None:
        lease_wait = asyncio.create_task(_wait_for_lease_loss(execution_lease))
    waiters: set[asyncio.Task[object]] = {process_wait}
    if lease_wait is not None:
        waiters.add(lease_wait)
    try:
        done, _pending = await asyncio.wait(
            waiters,
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            await terminate_process_group(
                process,
                execution_lease=execution_lease,
            )
            raise asyncio.TimeoutError
        if lease_wait is not None and lease_wait in done:
            await terminate_process_group(
                process,
                execution_lease=execution_lease,
            )
            execution_lease.assert_healthy()
            raise GpuBrokerClientError("gpu_lease_lost", "GPU execution lease was lost")
        return_code = process_wait.result()
        # A launcher may exit while leaving an OpenMM/MPS grandchild behind.
        # Clear the entire process group before the reservation is released.
        await terminate_process_group(
            process,
            process_already_waited=True,
            execution_lease=execution_lease,
        )
        if execution_lease is not None:
            execution_lease.assert_healthy()
        return return_code
    except asyncio.CancelledError:
        await terminate_process_group(
            process,
            execution_lease=execution_lease,
        )
        raise
    finally:
        for task in waiters:
            if not task.done():
                task.cancel()
        await await_safety_cleanup(
            asyncio.gather(*waiters, return_exceptions=True)
        )


async def terminate_process_group(
    process: asyncio.subprocess.Process,
    *,
    process_already_waited: bool = False,
    execution_lease: ManagedGpuLease | None = None,
) -> None:
    await await_safety_cleanup(
        _terminate_process_group(
            process,
            process_already_waited=process_already_waited,
            execution_lease=execution_lease,
        )
    )


async def _terminate_process_group(
    process: asyncio.subprocess.Process,
    *,
    process_already_waited: bool = False,
    execution_lease: ManagedGpuLease | None = None,
) -> None:
    if execution_lease is not None:
        try:
            # The dedicated cgroup, not the original PGID, is authoritative:
            # launchers may exit or descendants may call setsid().  Broker
            # preparation freezes the cgroup, terminates its MPS clients,
            # kills every member, and proves it empty.
            await _prepare_termination_shielded(execution_lease)
        except Exception:
            execution_lease.fail_closed()
            raise
    group_alive = _process_group_alive(process.pid)
    if group_alive:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            group_alive = False
        except OSError as exc:
            await _quarantine_failed_cleanup(execution_lease)
            raise GpuBrokerClientError(
                "gpu_runtime_unhealthy",
                "MPS-safe process group termination could not send SIGTERM",
            ) from exc
    if not process_already_waited and process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass
    if group_alive:
        for _ in range(20):
            if not _process_group_alive(process.pid):
                group_alive = False
                break
            await asyncio.sleep(0.05)
    if group_alive:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            group_alive = False
        except OSError as exc:
            await _quarantine_failed_cleanup(execution_lease)
            raise GpuBrokerClientError(
                "gpu_runtime_unhealthy",
                "MPS-safe process group termination could not send SIGKILL",
            ) from exc
    if process.returncode is None:
        await process.wait()
    if group_alive:
        for _ in range(100):
            if not _process_group_alive(process.pid):
                return
            await asyncio.sleep(0.05)
        await _quarantine_failed_cleanup(execution_lease)
        raise GpuBrokerClientError(
            "gpu_runtime_unhealthy",
            "process group survived MPS-safe termination; GPU remains quarantined",
        )


async def _quarantine_failed_cleanup(
    execution_lease: ManagedGpuLease | None,
) -> None:
    if execution_lease is None:
        return
    try:
        await asyncio.to_thread(
            execution_lease.quarantine,
            reason="gpu_runtime_corruption",
        )
    except Exception:
        pass
    execution_lease.fail_closed()


async def _wait_for_lease_loss(execution_lease: ManagedGpuLease) -> None:
    while not execution_lease.lost:
        await asyncio.sleep(0.25)


async def _prepare_termination_shielded(
    execution_lease: ManagedGpuLease,
) -> None:
    await await_safety_cleanup(
        asyncio.to_thread(execution_lease.prepare_process_termination)
    )


async def _cleanup_failed_fenced_spawn(
    process: asyncio.subprocess.Process,
    registration_task: asyncio.Task[Any],
    execution_lease: ManagedGpuLease,
) -> None:
    registered = False
    try:
        await registration_task
        registered = True
    except Exception:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        if registered:
            await _prepare_termination_shielded(execution_lease)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()


async def _cleanup_registered_fenced_spawn(
    process: asyncio.subprocess.Process,
    execution_lease: ManagedGpuLease,
) -> None:
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        await _prepare_termination_shielded(execution_lease)
        await process.wait()


def _close_fd(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _process_group_alive(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
