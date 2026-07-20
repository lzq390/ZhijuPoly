"""Exact transient user-scope contract for Broker-governed GPU workloads."""

from __future__ import annotations

import os
from pathlib import Path
import re
import time
from typing import Sequence


LEASE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SCOPE_PREFIX = "nexpoly-gpu-job-"
SCOPE_SLICE = "nexpoly-gpu-jobs.slice"
SYSTEMD_RUN = Path("/usr/bin/systemd-run")
SCOPE_TRANSITION_TIMEOUT_SECONDS = 5.0


class TransientScopeError(ValueError):
    """A command cannot be represented by the fixed GPU scope contract."""


def validate_lease_id(lease_id: object) -> str:
    if not isinstance(lease_id, str) or LEASE_ID_RE.fullmatch(lease_id) is None:
        raise TransientScopeError(
            "GPU transient scope requires the complete 32-hex lease ID"
        )
    return lease_id


def scope_unit_name(lease_id: object) -> str:
    return f"{SCOPE_PREFIX}{validate_lease_id(lease_id)}.scope"


def user_manager_control_group(uid: int | None = None) -> str:
    resolved_uid = os.geteuid() if uid is None else uid
    if (
        isinstance(resolved_uid, bool)
        or not isinstance(resolved_uid, int)
        or resolved_uid <= 0
    ):
        raise TransientScopeError("GPU transient scope UID is invalid")
    return (
        f"/user.slice/user-{resolved_uid}.slice/"
        f"user@{resolved_uid}.service"
    )


def scope_control_group(lease_id: object, *, uid: int | None = None) -> str:
    """Return the only cgroup-v2 path accepted for one exact lease.

    A hierarchical systemd slice name ``nexpoly-gpu-jobs.slice`` expands to
    the three nested slice components below the user manager.
    """

    return (
        user_manager_control_group(uid)
        + "/nexpoly.slice/nexpoly-gpu.slice/nexpoly-gpu-jobs.slice/"
        + scope_unit_name(lease_id)
    )


def transient_scope_command(
    lease_id: object,
    command: Sequence[str | os.PathLike[str]],
    *,
    systemd_run: Path = SYSTEMD_RUN,
) -> tuple[str, ...]:
    """Wrap an executor command in its exact transient user scope.

    ``systemd-run --scope`` synchronously execs the target, so the PID returned
    by ``Popen``/``create_subprocess_exec`` remains the workload PID.  The
    caller must still keep its CUDA/exec gate closed until the Broker confirms
    that exact PID, start time, UID and cgroup.
    """

    unit = scope_unit_name(lease_id)
    if (
        not systemd_run.is_absolute()
        or systemd_run.name != "systemd-run"
        or any(
            not isinstance(argument, (str, os.PathLike))
            or not os.fspath(argument)
            or "\x00" in os.fspath(argument)
            for argument in command
        )
    ):
        raise TransientScopeError("GPU transient scope command is unsafe")
    return (
        os.fspath(systemd_run),
        "--user",
        "--scope",
        "--quiet",
        "--no-ask-password",
        f"--unit={unit}",
        f"--slice={SCOPE_SLICE}",
        "--property=KillMode=control-group",
        "--property=CollectMode=inactive-or-failed",
        "--expand-environment=no",
        "--",
        *(os.fspath(argument) for argument in command),
    )


def wait_for_scope_membership(
    pid: int,
    lease_id: object,
    *,
    uid: int | None = None,
    timeout_seconds: float = SCOPE_TRANSITION_TIMEOUT_SECONDS,
    cgroup_reader=None,
    start_ticks_reader=None,
    monotonic=time.monotonic,
    sleep=time.sleep,
) -> int:
    """Wait for ``systemd-run`` to move its exec-preserved PID into the scope.

    ``Popen`` returns before the asynchronous user-manager transaction has
    necessarily moved the launcher.  Registration must therefore wait for the
    exact cgroup transition while the target remains blocked on its exec gate.
    The returned start ticks bind the pre- and post-transition PID identity.
    """

    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds <= 0
        or timeout_seconds > 30
    ):
        raise TransientScopeError("GPU transient scope transition input is invalid")
    expected = scope_control_group(lease_id, uid=uid)
    read_cgroup = cgroup_reader or _read_process_cgroup
    read_start_ticks = start_ticks_reader or _read_process_start_ticks
    deadline = monotonic() + float(timeout_seconds)
    initial_start_ticks: int | None = None
    while True:
        try:
            start_ticks = read_start_ticks(pid)
            cgroup = read_cgroup(pid)
        except (OSError, ValueError) as exc:
            raise TransientScopeError(
                "GPU transient scope launcher disappeared during transition"
            ) from exc
        if (
            isinstance(start_ticks, bool)
            or not isinstance(start_ticks, int)
            or start_ticks <= 0
        ):
            raise TransientScopeError(
                "GPU transient scope launcher process identity is invalid"
            )
        if initial_start_ticks is None:
            initial_start_ticks = start_ticks
        elif start_ticks != initial_start_ticks:
            raise TransientScopeError(
                "GPU transient scope launcher PID was reused during transition"
            )
        if _unified_cgroup_path(cgroup) == expected:
            return start_ticks
        if monotonic() >= deadline:
            raise TransientScopeError(
                "GPU transient scope launcher did not enter its exact scope"
            )
        sleep(0.01)


def _read_process_cgroup(pid: int) -> str:
    return Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii")


def _read_process_start_ticks(pid: int) -> int:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    closing_parenthesis = raw.rfind(")")
    if closing_parenthesis < 1:
        raise ValueError("process stat comm field is invalid")
    fields_after_comm = raw[closing_parenthesis + 2 :].split()
    return int(fields_after_comm[19])


def _unified_cgroup_path(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("process cgroup inventory is invalid")
    lines = [line for line in value.splitlines() if line]
    if len(lines) != 1:
        raise ValueError("process cgroup inventory is not unified")
    fields = lines[0].split(":", 2)
    if (
        len(fields) != 3
        or fields[0] != "0"
        or fields[1] != ""
        or not fields[2].startswith("/")
    ):
        raise ValueError("process cgroup inventory is not cgroup v2")
    return fields[2]
