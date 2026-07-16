from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

from gpu_resource import ManagedGpuLease, mps_client_environment

from .byteff2_env import build_byteff2_environment
from .byteff2_runtime_assets import validate_byteff2_runtime_assets
from .config import WorkerSettings
from .formal_protocols import FORMAL_PROTOCOLS
from .process_control import (
    create_fenced_subprocess_exec,
    terminate_process_group,
    wait_for_process_group,
)
from .runtime_probe import (
    SAFE_TRANSPORT_RUNTIME_ERRORS,
    TRANSPORT_CUDA_SMOKE_DISABLED,
)


MAX_PROBE_STDOUT_BYTES = 64 * 1024


class RuntimeProbeRunner(Protocol):
    @property
    def byteff2_environment(self): ...

    async def acquire_runtime_probe_lease(
        self,
        worker_instance_id: str,
        *,
        timeout_seconds: float,
    ) -> ManagedGpuLease | None: ...

    async def release_execution_lease(
        self, lease: ManagedGpuLease | None
    ) -> None: ...


@dataclass(frozen=True)
class ProtocolRuntimeSnapshot:
    protocol: str
    supported: bool
    runtime_ready: bool
    runtime_error: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "run_mode": "formal",
            "supported": self.supported,
            "runtime_ready": self.runtime_ready,
            "runtime_error": self.runtime_error,
        }


@dataclass(frozen=True)
class RuntimeSnapshot:
    byteff2_root_exists: bool
    runtime_ready: bool
    runtime_error: str | None
    protocols: tuple[ProtocolRuntimeSnapshot, ...]

    def protocols_dict(self) -> dict[str, Any]:
        return {item.protocol: item.as_dict() for item in self.protocols}


@dataclass(frozen=True)
class _ProbeCompleted:
    returncode: int
    stdout: str
    stdout_oversized: bool = False


def initial_runtime_snapshot(settings: WorkerSettings) -> RuntimeSnapshot:
    root_exists = _byteff2_root_exists(settings)
    if settings.mode == "dry-run":
        return RuntimeSnapshot(root_exists, True, None, ())
    error = "monomer MD runtime startup probe has not completed"
    return RuntimeSnapshot(root_exists, False, error, _unready_protocols(error))


def degraded_runtime_snapshot(
    settings: WorkerSettings, error: str
) -> RuntimeSnapshot:
    safe_error = _bounded_error(error)
    return RuntimeSnapshot(
        _byteff2_root_exists(settings),
        False,
        safe_error,
        _unready_protocols(safe_error),
    )


async def probe_runtime_snapshot(
    settings: WorkerSettings,
    *,
    runner: RuntimeProbeRunner,
    worker_instance_id: str,
) -> RuntimeSnapshot:
    """Probe ByteFF2/OpenMM once within one total work-admission deadline.

    Broker-governed CUDA initialization is always run as the sole fenced
    workload of a temporary MD execution lease.  The lease is closed before
    the CPU-only GROMACS delivery check starts because an execution lease may
    not be rebound to a second process.  Once the deadline expires no new
    probe work starts, but mandatory fenced-process and lease cleanup is
    awaited even if host safety takes the function past that deadline.
    """

    deadline = monotonic() + float(settings.health_probe_timeout_seconds)
    root_exists = _byteff2_root_exists(settings)
    if settings.mode == "dry-run":
        return RuntimeSnapshot(root_exists, True, None, ())
    if not root_exists:
        error = f"ByteFF2 root does not exist: {settings.byteff2_root}"
        return RuntimeSnapshot(False, False, error, _unready_protocols(error))

    demo_entry_error = _configured_density_demo_entry_error(settings)
    if demo_entry_error is not None:
        return RuntimeSnapshot(
            True,
            False,
            demo_entry_error,
            _unready_protocols(demo_entry_error),
        )

    try:
        runtime_asset_error = await asyncio.to_thread(
            validate_byteff2_runtime_assets,
            settings.byteff2_root,
            deadline=deadline,
        )
    except TimeoutError:
        return _probe_budget_exhausted_snapshot(
            settings,
            "ByteFF2 runtime asset validation",
        )
    if runtime_asset_error is not None:
        return RuntimeSnapshot(
            True,
            False,
            runtime_asset_error,
            _unready_protocols(runtime_asset_error),
        )

    # The real Worker freezes this immutable environment once and shares it
    # with startup probe, Density, and every formal runner.  Lightweight test
    # doubles may omit the property and use the same safe constructor here.
    environment = getattr(runner, "byteff2_environment", None)
    if environment is None:
        environment = build_byteff2_environment(settings)
    command = [
        settings.byteff2_python,
        str(Path(__file__).with_name("runtime_probe.py")),
    ]
    for protocol in FORMAL_PROTOCOLS:
        command.extend(("--protocol", protocol))
    if settings.transport_cuda_smoke_enabled and environment.transport_error is None:
        command.append("--transport-cuda-smoke")

    lease: ManagedGpuLease | None = None
    runtime_completed: _ProbeCompleted | None = None
    runtime_failure: str | None = None
    release_failure = False
    release_exhausted_budget = False
    remaining = _remaining_probe_budget(deadline)
    if remaining is None:
        return _probe_budget_exhausted_snapshot(settings, "GPU lease acquisition")

    if settings.gpu_broker_enabled:
        try:
            # The runner owns the single timeout/cancellation boundary so a
            # lease that wins the deadline race is always observed and closed.
            lease = await runner.acquire_runtime_probe_lease(
                worker_instance_id,
                timeout_seconds=remaining,
            )
        except asyncio.TimeoutError:
            return _probe_budget_exhausted_snapshot(
                settings, "GPU lease acquisition"
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            error = "runtime startup probe could not acquire a governed GPU lease"
            return RuntimeSnapshot(True, False, error, _unready_protocols(error))
        if lease is None:
            error = "runtime startup probe did not receive a governed GPU lease"
            return RuntimeSnapshot(True, False, error, _unready_protocols(error))

    runtime_env = environment.as_dict()
    try:
        if lease is not None:
            try:
                runtime_env.update(
                    mps_client_environment(
                        lease.lease,
                        pipe_root=settings.gpu_mps_pipe_root,
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                runtime_failure = (
                    "runtime startup probe GPU environment is unavailable"
                )
        if runtime_failure is None:
            remaining = _remaining_probe_budget(deadline)
            if remaining is None:
                runtime_failure = _probe_budget_error(
                    settings, "runtime import and CUDA"
                )
            else:
                try:
                    runtime_completed = await _run_probe_command(
                        command,
                        cwd=settings.byteff2_root,
                        env=runtime_env,
                        deadline=deadline,
                        execution_lease=lease,
                    )
                except FileNotFoundError:
                    runtime_failure = (
                        "runtime import and CUDA probe executable was not found"
                    )
                except asyncio.TimeoutError:
                    runtime_failure = _probe_budget_error(
                        settings, "runtime import and CUDA"
                    )
                except asyncio.CancelledError:
                    raise
                except OSError as exc:
                    runtime_failure = _os_error(
                        "runtime import and CUDA probe", exc
                    )
                except Exception:
                    runtime_failure = "runtime import and CUDA probe failed"
    finally:
        if lease is not None:
            try:
                await runner.release_execution_lease(lease)
            except asyncio.CancelledError:
                raise
            except Exception:
                release_failure = True
            release_exhausted_budget = _remaining_probe_budget(deadline) is None

    if release_failure:
        error = "runtime startup probe GPU lease cleanup failed"
        return RuntimeSnapshot(True, False, error, _unready_protocols(error))
    if release_exhausted_budget:
        error = _probe_budget_error(settings, "GPU lease cleanup")
        return RuntimeSnapshot(True, False, error, _unready_protocols(error))
    if runtime_failure is not None:
        return RuntimeSnapshot(
            True, False, runtime_failure, _unready_protocols(runtime_failure)
        )
    if runtime_completed is None:  # pragma: no cover - defensive invariant
        error = "runtime import and CUDA probe did not complete"
        return RuntimeSnapshot(True, False, error, _unready_protocols(error))
    if runtime_completed.stdout_oversized:
        error = "runtime import and CUDA probe returned an oversized response"
        return RuntimeSnapshot(True, False, error, _unready_protocols(error))

    probe = _parse_probe_output(runtime_completed.stdout)
    if runtime_completed.returncode != 0:
        error = _completed_process_error(
            runtime_completed.returncode, "runtime import and CUDA probe"
        )
        return RuntimeSnapshot(True, False, error, _unready_protocols(error))
    if probe.get("runtime_ready") is not True or not isinstance(
        probe.get("protocols"), dict
    ):
        error = "runtime import and CUDA probe returned an invalid response"
        return RuntimeSnapshot(True, False, error, _unready_protocols(error))

    protocols = _protocols_from_probe(
        probe["protocols"],
        transport_error=(
            environment.transport_error
            if environment.transport_error is not None
            else (
                TRANSPORT_CUDA_SMOKE_DISABLED
                if not settings.transport_cuda_smoke_enabled
                else None
            )
        ),
    )

    remaining = _remaining_probe_budget(deadline)
    if remaining is None:
        error = _probe_budget_error(settings, "gmx")
        return RuntimeSnapshot(
            True, False, error, _protocols_with_error(protocols, error)
        )
    try:
        gmx_completed = await _run_probe_command(
            ["gmx", "--version"],
            cwd=settings.byteff2_root,
            env=environment.as_dict(),
            deadline=deadline,
            execution_lease=None,
        )
    except FileNotFoundError:
        error = "gmx was not found on PATH"
        return RuntimeSnapshot(
            True, False, error, _protocols_with_error(protocols, error)
        )
    except asyncio.TimeoutError:
        error = _probe_budget_error(settings, "gmx")
        return RuntimeSnapshot(
            True, False, error, _protocols_with_error(protocols, error)
        )
    except asyncio.CancelledError:
        raise
    except OSError as exc:
        error = _os_error("gmx probe", exc)
        return RuntimeSnapshot(
            True, False, error, _protocols_with_error(protocols, error)
        )
    except Exception:
        error = "gmx probe failed"
        return RuntimeSnapshot(
            True, False, error, _protocols_with_error(protocols, error)
        )

    if gmx_completed.stdout_oversized:
        error = "gmx probe returned an oversized response"
        return RuntimeSnapshot(
            True, False, error, _protocols_with_error(protocols, error)
        )
    if gmx_completed.returncode != 0:
        error = _completed_process_error(gmx_completed.returncode, "gmx probe")
        return RuntimeSnapshot(
            True, False, error, _protocols_with_error(protocols, error)
        )
    if _remaining_probe_budget(deadline) is None:
        error = _probe_budget_error(settings, "gmx")
        return RuntimeSnapshot(
            True, False, error, _protocols_with_error(protocols, error)
        )
    return RuntimeSnapshot(True, True, None, protocols)


async def _run_probe_command(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    deadline: float,
    execution_lease: ManagedGpuLease | None,
) -> _ProbeCompleted:
    with tempfile.TemporaryFile(mode="w+b") as stdout:
        spawn_budget = _remaining_probe_budget(deadline)
        if spawn_budget is None:
            raise asyncio.TimeoutError
        process = await asyncio.wait_for(
            create_fenced_subprocess_exec(
                command,
                execution_lease=execution_lease,
                cwd=cwd,
                env=env,
                stdout=stdout,
                stderr=asyncio.subprocess.DEVNULL,
            ),
            timeout=spawn_budget,
        )
        execution_budget = _remaining_probe_budget(deadline)
        if execution_budget is None:
            await terminate_process_group(
                process,
                execution_lease=execution_lease,
            )
            raise asyncio.TimeoutError
        returncode = await wait_for_process_group(
            process,
            timeout_seconds=execution_budget,
            execution_lease=execution_lease,
        )
        stdout.seek(0)
        raw_stdout = stdout.read(MAX_PROBE_STDOUT_BYTES + 1)
    oversized = len(raw_stdout) > MAX_PROBE_STDOUT_BYTES
    try:
        decoded = raw_stdout[:MAX_PROBE_STDOUT_BYTES].decode("utf-8")
    except UnicodeDecodeError:
        decoded = ""
    return _ProbeCompleted(returncode, decoded, oversized)


def _byteff2_root_exists(settings: WorkerSettings) -> bool:
    try:
        return settings.byteff2_root.exists()
    except OSError:
        return False


def _remaining_probe_budget(deadline: float) -> float | None:
    remaining = deadline - monotonic()
    return remaining if remaining > 0 else None


def _probe_budget_error(settings: WorkerSettings, stage: str) -> str:
    return (
        "runtime startup probe exceeded total "
        f"{settings.health_probe_timeout_seconds}s budget during {stage} probe"
    )


def _probe_budget_exhausted_snapshot(
    settings: WorkerSettings, stage: str
) -> RuntimeSnapshot:
    error = _probe_budget_error(settings, stage)
    return RuntimeSnapshot(True, False, error, _unready_protocols(error))


def _configured_density_demo_entry_error(settings: WorkerSettings) -> str | None:
    configured = os.getenv("BYTEFF2_DENSITY_DEMO_ENTRY", "").strip()
    if not configured:
        return None
    path = Path(configured)
    if not path.is_absolute():
        path = settings.byteff2_root / path
    try:
        if path.exists():
            return None
    except OSError:
        pass
    return "BYTEFF2_DENSITY_DEMO_ENTRY does not exist"


def _parse_probe_output(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.strip().splitlines()):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        return data if isinstance(data, dict) else {}
    return {}


def _completed_process_error(returncode: int, label: str) -> str:
    return f"{label} exited with code {returncode}"


def _os_error(label: str, error: OSError) -> str:
    if error.errno is None:
        return f"{label} failed with an OS error"
    return f"{label} failed with OS errno {error.errno}"


def _protocols_from_probe(
    probe: dict[str, Any], *, transport_error: str | None
) -> tuple[ProtocolRuntimeSnapshot, ...]:
    snapshots: list[ProtocolRuntimeSnapshot] = []
    for protocol in FORMAL_PROTOCOLS:
        item = probe.get(protocol) if isinstance(probe.get(protocol), dict) else {}
        supported = item.get("supported") is True
        runtime_ready = supported and item.get("runtime_ready") is True
        runtime_error = _safe_protocol_runtime_error(
            protocol, item.get("runtime_error")
        )
        if protocol == "Transport" and transport_error is not None:
            runtime_ready = False
            runtime_error = transport_error
        snapshots.append(
            ProtocolRuntimeSnapshot(
                protocol=protocol,
                supported=supported,
                runtime_ready=runtime_ready,
                runtime_error=runtime_error,
            )
        )
    return tuple(snapshots)


def _safe_protocol_runtime_error(protocol: str, error: Any) -> str | None:
    if error is None:
        return None
    message = str(error)
    if protocol == "Transport" and message in SAFE_TRANSPORT_RUNTIME_ERRORS:
        return message
    return f"{protocol} runtime probe reported unavailable"


def _unready_protocols(error: str) -> tuple[ProtocolRuntimeSnapshot, ...]:
    return tuple(
        ProtocolRuntimeSnapshot(protocol, False, False, error)
        for protocol in FORMAL_PROTOCOLS
    )


def _protocols_with_error(
    protocols: tuple[ProtocolRuntimeSnapshot, ...], error: str
) -> tuple[ProtocolRuntimeSnapshot, ...]:
    return tuple(
        ProtocolRuntimeSnapshot(item.protocol, item.supported, False, error)
        for item in protocols
    )


def _bounded_error(error: str) -> str:
    message = str(error).strip()
    return (message or "runtime startup probe failed")[:500]
