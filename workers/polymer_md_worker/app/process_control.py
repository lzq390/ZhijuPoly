"""Systemd scopes contain MPI descendants, including reparented/detached ranks."""
from __future__ import annotations

import os
import ctypes
import errno
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Any
from uuid import uuid4

from .execution_gate import process_start_ticks

UNIT = re.compile(r"^nexpoly-polymer-md-[a-z0-9_-]+\.scope$")
CGROUP_ROOT = Path("/sys/fs/cgroup")


class ScopeInspectionError(RuntimeError):
    """Only allowlisted command diagnostics may cross into durable job metadata."""

    def __init__(self, *, exception_type: str, returncode: int | None = None, stderr=""):
        super().__init__("cannot confirm systemd scope state")
        # Do not store raw exception messages, stdout, command lines or stderr:
        # systemctl/DBus errors may include private arguments or environment values.
        lowered = str(stderr).lower()[:4096]
        categories = [name for name, patterns in {
            "bus_connection_unavailable": ("failed to connect to bus", "connection reset", "connection refused", "disconnected"),
            "control_timeout": ("timed out", "timeout"),
            "access_denied": ("access denied", "permission denied"),
            "unit_not_found": ("not found", "could not be found", "not loaded"),
        }.items() if any(pattern in lowered for pattern in patterns)]
        self.diagnostic = {"operation": "systemctl_show", "exception_type": exception_type,
                           "returncode": returncode, "stderr_categories": categories or ["unclassified"]}


class ScopeIdentityError(RuntimeError):
    pass


class ScopeControl:
    @staticmethod
    def kill_launcher(pid: int, start_ticks: int) -> bool:
        """Fence a pre-scope launcher by Linux pidfd identity, never by PID alone."""
        # Conda's Python may omit os.pidfd_open even on a supported Linux host.
        libc = ctypes.CDLL(None, use_errno=True)
        if not hasattr(libc, "pidfd_open") or not hasattr(libc, "pidfd_send_signal"):
            return False
        libc.pidfd_open.argtypes, libc.pidfd_open.restype = [ctypes.c_int, ctypes.c_uint], ctypes.c_int
        libc.pidfd_send_signal.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
        libc.pidfd_send_signal.restype = ctypes.c_int
        descriptor = libc.pidfd_open(pid, 0)
        if descriptor < 0:
            if ctypes.get_errno() == errno.ESRCH:
                return True
            return False
        try:
            if process_start_ticks(pid) != start_ticks:
                return True  # The recorded process identity has already exited.
            response = libc.pidfd_send_signal(descriptor, signal.SIGKILL, None, 0)
            return response == 0 or ctypes.get_errno() == errno.ESRCH
        finally:
            os.close(descriptor)

    def _call(self, arguments: list[str], timeout=15) -> subprocess.CompletedProcess:
        return subprocess.run(arguments, capture_output=True, text=True, timeout=timeout, check=False)

    def inspect(self, unit: str) -> dict[str, str]:
        if not UNIT.fullmatch(unit):
            raise ValueError("invalid scope identity")
        try:
            result = self._call(["systemctl", "--user", "show", unit, "--property=LoadState,ActiveState,SubState,ControlGroup"], timeout=2)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ScopeInspectionError(exception_type=type(exc).__name__, stderr=getattr(exc, "stderr", "")) from None
        data = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if "LoadState" not in data or (result.returncode and data.get("LoadState") != "not-found"):
            raise ScopeInspectionError(exception_type="InvalidScopeResponse", returncode=result.returncode, stderr=result.stderr)
        return data

    @staticmethod
    def _process_cgroup(pid: int) -> str:
        memberships = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        return next(line[3:] for line in memberships if line.startswith("0::"))

    @staticmethod
    def _workload_group(identity: dict) -> Path:
        group, unit = identity.get("cgroup", ""), identity.get("unit", "")
        if (not UNIT.fullmatch(unit) or not group.startswith(f"/user.slice/user-{os.getuid()}.slice/")
                or str(Path(group)) != group or ".." in Path(group).parts or Path(group).name != unit):
            raise ScopeIdentityError("invalid owned workload cgroup")
        target = CGROUP_ROOT / group.lstrip("/")
        if target.resolve(strict=True) != target:
            raise ScopeIdentityError("workload cgroup is not a direct kernel path")
        return target

    def capture_workload_identity(self, identity: dict) -> dict:
        """Pin the live gated entry and verify the existing fixed kernel budgets."""
        target = self._workload_group(identity)
        metadata = target.stat()
        pid, ticks = identity["entry_pid"], identity["entry_start_ticks"]
        if process_start_ticks(pid) != ticks or self._process_cgroup(pid) != identity["cgroup"]:
            raise ScopeIdentityError("workload process identity changed")
        quota, period = (target / "cpu.max").read_text().split()
        memory = (target / "memory.max").read_text().strip()
        tasks = (target / "pids.max").read_text().strip()
        if (quota == "max" or not 0 < int(quota) <= 16 * int(period) or int(period) <= 0
                or memory == "max" or not 0 < int(memory) <= 32 * 1024**3
                or tasks == "max" or not 0 < int(tasks) <= 4096):
            raise ScopeIdentityError("workload resource limits cannot be confirmed")
        events = dict(line.split() for line in (target / "cgroup.events").read_text().splitlines())
        after = target.stat()
        if (events.get("populated") != "1" or process_start_ticks(pid) != ticks
                or self._process_cgroup(pid) != identity["cgroup"]
                or (metadata.st_dev, metadata.st_ino) != (after.st_dev, after.st_ino)):
            raise ScopeIdentityError("workload cgroup identity changed")
        return {"device": metadata.st_dev, "inode": metadata.st_ino}

    def workload_is_bounded(self, identity: dict) -> bool:
        if not identity.get("gate_granted") or not identity.get("cgroup_identity"):
            return False
        try:
            return self.capture_workload_identity(identity) == identity["cgroup_identity"]
        except (OSError, ValueError, KeyError, RuntimeError, StopIteration):
            return False

    @staticmethod
    def _empty(cgroup: str) -> bool:
        if not cgroup.startswith(f"/user.slice/user-{os.getuid()}.slice/") or ".." in Path(cgroup).parts:
            raise RuntimeError("scope returned an unexpected cgroup")
        target = Path("/sys/fs/cgroup") / cgroup.lstrip("/")
        if not target.exists():
            return True
        events = dict(line.split() for line in (target / "cgroup.events").read_text().splitlines())
        return events.get("populated") == "0"

    def confirm_gone(self, unit: str, known_cgroup: str | None = None) -> bool:
        state = self.inspect(unit)
        group = state.get("ControlGroup") or known_cgroup
        if group:
            return self._empty(group)
        return state.get("LoadState") == "not-found" or state.get("ActiveState") in {"inactive", "failed"}

    def terminate(self, unit: str, known_cgroup: str | None = None) -> bool:
        for signal, budget in [("SIGTERM", 2.0), ("SIGKILL", 10.0)]:
            if self.confirm_gone(unit, known_cgroup):
                return True
            self._call(["systemctl", "--user", "kill", "--kill-whom=all", f"--signal={signal}", unit])
            until = time.monotonic() + budget
            while time.monotonic() < until:
                if self.confirm_gone(unit, known_cgroup):
                    return True
                time.sleep(0.1)
        return False

    def launch(self, *, unit: str, command: list[str], cwd: Path, log, environment: dict[str, str], cpu_count=16, memory_bytes=32 * 1024**3):
        if not UNIT.fullmatch(unit):
            raise ValueError("invalid scope identity")
        args = ["systemd-run", "--user", "--scope", "--quiet", f"--unit={unit}",
                f"--property=CPUQuota={cpu_count * 100}%", f"--property=MemoryMax={memory_bytes}",
                "--property=TasksMax=4096", *command]
        return subprocess.Popen(args, cwd=cwd, env=environment, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    def probe(self) -> dict[str, Any]:
        unit = f"nexpoly-polymer-md-probe-{uuid4().hex}.scope"
        try:
            result = self._call(["systemd-run", "--user", "--scope", "--quiet", f"--unit={unit}",
                                 "--property=CPUQuota=1600%", "--property=MemoryMax=34359738368",
                                 "--property=TasksMax=4096", "/usr/bin/true"])
            ready = result.returncode == 0 and self.confirm_gone(unit)
            return {"ready": ready, "kind": "systemd_user_scope", "cpu_count": 16, "memory_bytes": 32 * 1024**3}
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return {"ready": False, "kind": "systemd_user_scope", "error": "scope_guard_unavailable"}


def scientific_environment() -> dict[str, str]:
    # Backend credentials/authorization material never enter the scientific job.
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TMP", "TEMP",
               "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "LD_LIBRARY_PATH", "POLYMER_MD_RUNTIME_MANIFEST"}
    environment = {key: value for key, value in os.environ.items() if key in allowed or key.startswith("LC_")}
    environment.update(CUDA_VISIBLE_DEVICES="", OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
                       MKL_NUM_THREADS="1", POLYMER_MD_CPU_COUNT="16")
    repo = str(Path(__file__).resolve().parents[3])
    environment["PYTHONPATH"] = repo
    return environment
