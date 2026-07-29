from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Sequence
from pathlib import Path
from typing import Any, TypeVar

from gpu_resource import (
    GpuBrokerClientError,
    ManagedGpuLease,
    transient_scope_command,
    wait_for_scope_membership,
)


_CleanupResult = TypeVar("_CleanupResult")
logger = logging.getLogger("monomer_md_worker.process_control")


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
    scope_command_builder=transient_scope_command,
    scope_membership_waiter=wait_for_scope_membership,
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
    user_runtime = f"/run/user/{os.geteuid()}"
    env["XDG_RUNTIME_DIR"] = user_runtime
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={user_runtime}/bus"
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
            gated_command = (
                # Isolated/no-site startup is part of the fence.  In
                # particular, target PYTHONPATH/sitecustomize must not run
                # before the Broker has registered this exact host PID.
                sys.executable,
                "-I",
                "-S",
                str(exec_gate),
                "--",
                *command,
            )
            scoped_command = scope_command_builder(
                execution_lease.lease.lease_id,
                gated_command,
            )
            spawn_task = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *scoped_command,
                    env=env,
                    pass_fds=(gate_reader,),
                    start_new_session=True,
                    **kwargs,
                )
            )
            try:
                process = await asyncio.shield(spawn_task)
            except BaseException:
                # asyncio can deliver cancellation after the OS child exists
                # but before create_subprocess_exec publishes its Process
                # object. Collect that result and fence the still-gated child;
                # otherwise an exact systemd scope and GPU lease are orphaned.
                cleanup_gate_writer = gate_writer
                gate_writer = -1
                await await_safety_cleanup(
                    _cleanup_cancelled_spawn(
                        spawn_task,
                        execution_lease,
                        cleanup_gate_writer,
                        scope_membership_waiter,
                    )
                )
                raise
        except BaseException:
            _close_fd(gate_writer)
            raise
    finally:
        _close_fd(gate_reader)
    scope_transition_task = asyncio.create_task(
        asyncio.to_thread(
            scope_membership_waiter,
            process.pid,
            execution_lease.lease.lease_id,
        )
    )
    try:
        await asyncio.shield(scope_transition_task)
    except BaseException:
        # create_subprocess_exec can return before systemd has moved and exec'd
        # the same PID. Keep the CUDA gate closed until the exact transition
        # is proven. If the transition won the cancellation race, register the
        # still-gated PID before collecting it so Broker release can use the
        # exact cgroup instead of an unregistered, whole-card MPS audit.
        await await_safety_cleanup(
            _cleanup_cancelled_scope_transition(
                process,
                scope_transition_task,
                execution_lease,
                gate_writer,
            )
        )
        raise
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
    loop = asyncio.get_running_loop()
    process_started_at = loop.time()
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
        await await_safety_cleanup(
            _terminate_cancelled_process_group(
                process,
                process_wait=process_wait,
                process_started_at=process_started_at,
                execution_lease=execution_lease,
            )
        )
        raise
    finally:
        for task in waiters:
            if not task.done():
                task.cancel()
        await await_safety_cleanup(
            asyncio.gather(*waiters, return_exceptions=True)
        )


MAX_TERMINATION_GRACE_SECONDS = 10.0
PROCESS_GROUP_POLL_SECONDS = 0.05
PROCESS_GROUP_KILL_OBSERVE_SECONDS = 1.0
MPS_CLIENT_CANCELLATION_STABILIZATION_SECONDS = 30.0


async def _terminate_cancelled_process_group(
    process: asyncio.subprocess.Process,
    *,
    process_wait: asyncio.Task[int],
    process_started_at: float,
    execution_lease: ManagedGpuLease | None,
) -> None:
    """Delay user cancellation until a new MPS client can service teardown."""

    if execution_lease is not None and not process_wait.done():
        loop = asyncio.get_running_loop()
        remaining = (
            process_started_at
            + MPS_CLIENT_CANCELLATION_STABILIZATION_SECONDS
            - loop.time()
        )
        if remaining > 0:
            await asyncio.wait({process_wait}, timeout=remaining)
    process_already_waited = (
        process_wait.done()
        and not process_wait.cancelled()
        and process_wait.exception() is None
    )
    await _terminate_process_group(
        process,
        process_already_waited=process_already_waited,
        execution_lease=execution_lease,
    )


async def _wait_for_process_group_exit(
    process_group_id: int,
    *,
    deadline: float,
) -> bool:
    loop = asyncio.get_running_loop()
    while _process_group_alive(process_group_id):
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(PROCESS_GROUP_POLL_SECONDS, remaining))
    return True


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
    group_alive = _process_group_alive(process.pid)
    # A normally completed scope may already be collected by systemd.  Its
    # subsequent lease release still requires both an empty MPS inventory and
    # an empty or safely disappeared exact cgroup; live groups take the
    # stronger freeze/terminate path below.
    if execution_lease is not None and (
        not process_already_waited or group_alive
    ):
        try:
            # The dedicated cgroup, not the original PGID, is authoritative:
            # launchers may exit or descendants may call setsid().  Broker
            # preparation freezes the cgroup, terminates its MPS clients,
            # kills every member, and proves it empty.
            await _prepare_termination_shielded(execution_lease)
        except Exception:
            execution_lease.fail_closed()
            raise
    loop = asyncio.get_running_loop()
    termination_deadline = loop.time() + MAX_TERMINATION_GRACE_SECONDS
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
        remaining = termination_deadline - loop.time()
        try:
            if remaining > 0:
                await asyncio.wait_for(process.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            pass
    if group_alive:
        group_alive = not await _wait_for_process_group_exit(
            process.pid,
            deadline=termination_deadline,
        )
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
    kill_deadline = loop.time() + PROCESS_GROUP_KILL_OBSERVE_SECONDS
    if process.returncode is None:
        remaining = kill_deadline - loop.time()
        try:
            if remaining <= 0:
                raise asyncio.TimeoutError
            await asyncio.wait_for(process.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            await _quarantine_failed_cleanup(execution_lease)
            raise GpuBrokerClientError(
                "gpu_runtime_unhealthy",
                "process leader survived MPS-safe SIGKILL; GPU remains quarantined",
            ) from None
    if group_alive:
        if await _wait_for_process_group_exit(
            process.pid,
            deadline=kill_deadline,
        ):
            return
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


async def _cleanup_cancelled_scope_transition(
    process: asyncio.subprocess.Process,
    transition_task: asyncio.Task[Any],
    execution_lease: ManagedGpuLease,
    gate_writer: int,
) -> None:
    transitioned = False
    try:
        await transition_task
        transitioned = True
    except Exception:
        logger.warning(
            "cancelled fenced subprocess did not complete scope transition",
            exc_info=True,
        )
    if transitioned:
        try:
            # The writer remains open while host registration runs, so the
            # exec gate cannot disappear or import CUDA before the Broker has
            # bound this exact process identity and scope.
            await asyncio.to_thread(execution_lease.register_workload, process.pid)
        except Exception:
            logger.warning(
                "cancelled fenced subprocess could not register its exact scope",
                exc_info=True,
            )
            _close_fd(gate_writer)
            await _cleanup_unregistered_process(process)
            return
        _close_fd(gate_writer)
        await _cleanup_registered_fenced_spawn(process, execution_lease)
        return
    _close_fd(gate_writer)
    await _cleanup_unregistered_process(process)


async def _cleanup_cancelled_spawn(
    spawn_task: asyncio.Task[asyncio.subprocess.Process],
    execution_lease: ManagedGpuLease,
    gate_writer: int,
    scope_membership_waiter,
) -> None:
    try:
        process = await spawn_task
    except BaseException:
        _close_fd(gate_writer)
        logger.warning(
            "cancelled fenced subprocess creation did not publish a process",
            exc_info=True,
        )
        return
    transition_task = asyncio.create_task(
        asyncio.to_thread(
            scope_membership_waiter,
            process.pid,
            execution_lease.lease.lease_id,
        )
    )
    await _cleanup_cancelled_scope_transition(
        process,
        transition_task,
        execution_lease,
        gate_writer,
    )


async def _cleanup_unregistered_process(
    process: asyncio.subprocess.Process,
) -> None:
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except asyncio.TimeoutError:
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
