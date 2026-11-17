#!/usr/bin/env python3
"""Bounded, credential-free lifecycle evidence for a single registered guest."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import time

RUNTIME = Path("/run/nexpoly-ci-runner")
MARKER = "exit-diagnostics.json"
VMSTAT = Path("/proc/vmstat")
UNIT_EVENTS = Path("/sys/fs/cgroup/system.slice/nexpoly-ci-bootstrap.service/memory.events")
UNITS = {"docker.service": "docker_state", "user@1001.service": "user_manager_state",
         "cloud-final.service": "cloud_final_state", "apt-daily.service": "apt_daily_state",
         "apt-daily-upgrade.service": "apt_upgrade_state",
         "unattended-upgrades.service": "unattended_upgrades_state"}
UNIT_STATES = frozenset({"active", "inactive", "activating", "deactivating", "failed", "reloading"})
SYSTEM_STATES = frozenset({"initializing", "starting", "running", "degraded", "maintenance", "stopping", "offline"})
SERVICE_RESULTS = frozenset({"success", "protocol", "timeout", "exit-code", "signal", "core-dump",
                             "watchdog", "start-limit-hit", "resources", "oom-kill", "exec-condition"})
REASONS = frozenset({"runner_started", "runner_exit", "bootstrap_signal", "unit_stop"})
COUNTER_LIMIT = (1 << 63) - 1


def uptime_ms() -> int:
    return min(COUNTER_LIMIT, max(0, time.monotonic_ns() // 1_000_000))


def read_counter(path: Path, key: str) -> int | None:
    try:
        # Both are fixed kernel interfaces, never a job-selected pathname.
        with path.open() as handle:
            text = handle.read(65536)
        for line in text.splitlines():
            name, _, value = line.partition(" ")
            if name == key and value.isascii() and value.isdecimal():
                number = int(value)
                return number if 0 <= number <= COUNTER_LIMIT else None
    except (OSError, ValueError):
        pass
    return None


def oom_counters() -> dict[str, int]:
    values = {"oom_kill_count": read_counter(VMSTAT, "oom_kill"),
              "unit_oom_kill_count": read_counter(UNIT_EVENTS, "oom_kill")}
    return {key: value for key, value in values.items() if value is not None}


def unit_states() -> dict[str, str]:
    fields = {name: "unknown" for name in UNITS.values()}
    fields["system_state"] = "unknown"
    try:
        # Fixed queries share one second in total. Never request Environment,
        # ExecStart, command output, journal messages or runner _diag contents.
        deadline = time.monotonic() + 1
        result = subprocess.run(
            ["/usr/bin/systemctl", "show", "--no-pager", "--property=Id,ActiveState",
             "--", *UNITS], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=0.5, check=False,
        )
        for block in result.stdout[:4096].split("\n\n"):
            values = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            field = UNITS.get(values.get("Id"))
            if field:
                state = values.get("ActiveState")
                fields[field] = state if state in UNIT_STATES else "unknown"
        remaining = deadline - time.monotonic()
        if remaining > 0:
            # SystemState is a manager property, not a unit property.
            manager = subprocess.run(
                ["/usr/bin/systemctl", "show", "--no-pager", "--property=SystemState", "--value"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                timeout=remaining, check=False,
            )
            state = manager.stdout[:128].strip()
            fields["system_state"] = state if state in SYSTEM_STATES else "unknown"
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return fields


def exit_fields(returncode: int) -> dict[str, int]:
    if type(returncode) is not int or not -255 <= returncode <= 255:
        return {}
    fields = {"exit_code": returncode}
    if -64 <= returncode < 0:
        fields["signal"] = -returncode
    return fields


class GuestDiagnostics:
    def __init__(self, emit, runtime: Path = RUNTIME):
        self.emit = emit
        self.runtime = runtime
        self.enabled = False
        self.baseline: dict[str, int] = {}
        self.handlers: dict[int, object] = {}

    def enable(self) -> bool:
        """Called only after the registration child and its _diag are gone."""
        if os.geteuid() != 0:
            return False
        try:
            metadata = self.runtime.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o077:
                return False
            baseline = oom_counters()
            fd = os.open(self.runtime / MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump({"schema_version": 1, "baseline": baseline}, handle, separators=(",", ":"))
            self.baseline = baseline
            self.enabled = True
            for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
                previous = signal.getsignal(number)
                if previous != signal.SIG_IGN:
                    self.handlers[number] = previous
                    signal.signal(number, self.handle_signal)
            return True
        except (OSError, ValueError):
            return False  # Diagnostics must not prevent a job from running.

    def restore_signals(self) -> None:
        for number, previous in self.handlers.items():
            signal.signal(number, previous)
        self.handlers.clear()

    def handle_signal(self, number: int, frame) -> None:
        # No subprocess, counter read, sleep, log inspection or blocking channel
        # operation here. Preserve the existing handler/default signal outcome.
        try:
            self.emit("guest_diagnostic", schema_version=1, reason="bootstrap_signal",
                      signal=int(number), uptime_ms=uptime_ms())
        except Exception:
            pass
        previous = self.handlers.get(number, signal.SIG_DFL)
        signal.signal(number, previous)
        if callable(previous):
            previous(number, frame)
        elif previous == signal.SIG_DFL:
            os.kill(os.getpid(), number)

    def record(self, reason: str, **fields: object) -> None:
        if not self.enabled or reason not in REASONS:
            return
        try:
            values: dict[str, object] = {"schema_version": 1, "reason": reason,
                                         "uptime_ms": uptime_ms(), **unit_states()}
            counters = oom_counters()
            values.update(counters)
            for key, value in counters.items():
                baseline = self.baseline.get(key)
                if baseline is not None and value >= baseline:
                    values[key.replace("_count", "_delta")] = value - baseline
            # Refuse extra strings even if a future caller passes exception,
            # command or credential material by mistake.
            if type(fields.get("exit_code")) is int and -255 <= fields["exit_code"] <= 255:
                values["exit_code"] = fields["exit_code"]
            if type(fields.get("signal")) is int and 1 <= fields["signal"] <= 64:
                values["signal"] = fields["signal"]
            if isinstance(fields.get("service_result"), str) and fields["service_result"] in SERVICE_RESULTS | {"unknown"}:
                values["service_result"] = fields["service_result"]
            if isinstance(fields.get("service_exit_kind"), str) and fields["service_exit_kind"] in {"exited", "killed", "dumped", "unknown"}:
                values["service_exit_kind"] = fields["service_exit_kind"]
            self.emit("guest_diagnostic", **values)
        except Exception:
            pass


def load_enabled(emit, runtime: Path = RUNTIME) -> GuestDiagnostics | None:
    if os.geteuid() != 0:
        return None
    try:
        descriptor = os.open(runtime / MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor) as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o077:
                return None
            data = json.loads(handle.read(4097))
        if (not isinstance(data, dict) or set(data) != {"schema_version", "baseline"}
                or type(data["schema_version"]) is not int or data["schema_version"] != 1):
            return None
        baseline = data["baseline"]
        if not isinstance(baseline, dict) or not set(baseline) <= {"oom_kill_count", "unit_oom_kill_count"}:
            return None
        if any(type(value) is not int or not 0 <= value <= COUNTER_LIMIT for value in baseline.values()):
            return None
        diagnostics = GuestDiagnostics(emit, runtime)
        diagnostics.baseline = baseline
        diagnostics.enabled = True
        return diagnostics
    except (OSError, ValueError):
        return None


def systemd_exit_fields() -> dict[str, object]:
    # Read exactly these manager-provided keys; never enumerate the environment.
    result = os.environ.get("SERVICE_RESULT")
    kind = os.environ.get("EXIT_CODE")
    status = os.environ.get("EXIT_STATUS", "")
    fields: dict[str, object] = {"service_result": result if result in SERVICE_RESULTS else "unknown",
                                 "service_exit_kind": kind if kind in {"exited", "killed", "dumped"} else "unknown"}
    if kind == "exited" and status.isascii() and status.isdecimal() and 0 <= int(status) <= 255:
        fields["exit_code"] = int(status)
    elif kind in {"killed", "dumped"}:
        if status.isascii() and status.isdecimal() and 1 <= int(status) <= 64:
            fields["signal"] = int(status)
        else:
            number = getattr(signal, "SIG" + status.removeprefix("SIG"), None)
            if isinstance(number, int) and 1 <= number <= 64:
                fields["signal"] = int(number)
    return fields


def main() -> int:
    # ExecStopPost also runs for failed registration and build-mode skips. The
    # private marker gates every read/query/event until registration completed.
    from bootstrap import emit
    diagnostics = load_enabled(emit)
    if diagnostics is not None:
        diagnostics.record("unit_stop", **systemd_exit_fields())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
