#!/usr/bin/env python3
"""Bounded, credential-free lifecycle evidence for a single registered guest."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
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
HISTORY_UNITS = {"nexpoly-ci-bootstrap.service": "bootstrap", **{
    unit: field.removesuffix("_state") for unit, field in UNITS.items()},
    "shutdown.target": "shutdown", "poweroff.target": "poweroff",
    "reboot.target": "reboot", "halt.target": "halt"}
# Stable systemd catalog IDs, not localized/free-form MESSAGE text.
HISTORY_MESSAGES = {"7d4958e842da4a758f6c1cdc7b36dcc5": "starting",
                    "39f53479d3a045ac8e11786248231fbf": "started",
                    "de5b426a63be47a7b6ac3eaac82e2f6f": "stopping",
                    "9d1aaa27d60140bd96365438aad20286": "stopped",
                    "be02cf6855d2428ba40df7e9d022f03d": "failed"}
JOB_TYPES = frozenset({"start", "stop", "reload", "restart", "try-restart",
                       "reload-or-start", "try-reload", "verify-active", "nop"})
JOB_RESULTS = frozenset({"done", "canceled", "timeout", "failed", "dependency",
                         "skipped", "invalid", "assert", "unsupported", "collected",
                         "once", "frozen"})
HISTORY_FIELDS = ("__MONOTONIC_TIMESTAMP", "_PID", "_UID", "_COMM", "_SYSTEMD_UNIT",
                  "MESSAGE_ID", "UNIT", "JOB_TYPE", "JOB_RESULT", "JOB_ID")
HISTORY_BYTES = 65536
HISTORY_LINES = 64
HISTORY_EVENTS = 16


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


def journal_bytes(deadline: float) -> tuple[bytes, str]:
    """Read at most 64 KiB; ok means query success, not complete journal coverage."""
    command = ["/usr/bin/journalctl", "--boot=0", "--quiet", "--no-pager",
               "--output=json", "--output-fields=" + ",".join(HISTORY_FIELDS),
               "--lines=" + str(HISTORY_LINES), "_PID=1",
               *("UNIT=" + unit for unit in HISTORY_UNITS),
               *("MESSAGE_ID=" + message for message in HISTORY_MESSAGES),
               "+", "_UID=0", "_COMM=needrestart"]
    process = None
    output = bytearray()
    status = "unavailable"
    try:
        if time.monotonic() >= deadline - 0.05:
            return b"", "timeout"
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   stdin=subprocess.DEVNULL, bufsize=0)
        with selectors.DefaultSelector() as reader:
            reader.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic() - 0.05
                if remaining <= 0 or not reader.select(remaining):
                    status = "timeout"
                    break
                chunk = os.read(process.stdout.fileno(), min(8192, HISTORY_BYTES + 1 - len(output)))
                if not chunk:
                    remaining = deadline - time.monotonic() - 0.05
                    status = "ok" if remaining > 0 and process.wait(timeout=remaining) == 0 else "unavailable"
                    break
                output.extend(chunk)
                if len(output) > HISTORY_BYTES:
                    # Keep only complete bounded records; never retain the extra byte.
                    del output[HISTORY_BYTES:]
                    status = "truncated"
                    break
    except subprocess.TimeoutExpired:
        status = "timeout"
    except (OSError, ValueError, subprocess.SubprocessError):
        status = "unavailable"
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
            except OSError:
                pass  # It may have exited between poll() and kill().
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                process.stdout.close()
            except OSError:
                pass
    return bytes(output), status


def decimal_field(value: object, limit: int = COUNTER_LIMIT) -> int | None:
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 20:
        number = int(value)
        if number <= limit:
            return number
    return None


def classify_history(data: bytes, registered_ms: int, now_ms: int) -> list[dict[str, object]]:
    """Classify trusted journal metadata only; never emit raw field values."""
    events = []
    # A partial final line is deliberately ignored after timeout/truncation.
    for line in data[:HISTORY_BYTES].splitlines(keepends=True)[:HISTORY_LINES]:
        if not line.endswith(b"\n"):
            continue
        try:
            item = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(item, dict):
            continue
        stamp = decimal_field(item.get("__MONOTONIC_TIMESTAMP"))
        if stamp is None or not registered_ms <= stamp // 1000 <= now_ms:
            continue
        event: dict[str, object] = {"schema_version": 1, "reason": "unit_history",
                                    "uptime_ms": stamp // 1000}
        unit = item.get("UNIT")
        message = item.get("MESSAGE_ID")
        if (item.get("_PID") == "1" and isinstance(unit, str) and unit in HISTORY_UNITS
                and isinstance(message, str) and message in HISTORY_MESSAGES):
            event.update(history_source="systemd", source_unit=HISTORY_UNITS[unit],
                         unit_transition=HISTORY_MESSAGES[message],
                         systemd_job_type=item.get("JOB_TYPE") if isinstance(item.get("JOB_TYPE"), str)
                         and item["JOB_TYPE"] in JOB_TYPES else "unknown",
                         systemd_job_result=item.get("JOB_RESULT") if isinstance(item.get("JOB_RESULT"), str)
                         and item["JOB_RESULT"] in JOB_RESULTS else "unknown")
            job = decimal_field(item.get("JOB_ID"))
            if job is not None:
                event["systemd_job_id"] = job
        elif item.get("_UID") == "0" and item.get("_COMM") == "needrestart":
            # A root process name match is correlation, not proof of its
            # executable identity or evidence that it requested this stop.
            unit = item.get("_SYSTEMD_UNIT")
            event.update(history_source="needrestart", unit_transition="needrestart_observed",
                         source_unit=HISTORY_UNITS.get(unit, "unknown") if isinstance(unit, str) else "unknown")
        else:
            continue
        events.append(event)
    return events[-HISTORY_EVENTS:]


class GuestDiagnostics:
    def __init__(self, emit, runtime: Path = RUNTIME):
        self.emit = emit
        self.runtime = runtime
        self.enabled = False
        self.baseline: dict[str, int] = {}
        self.handlers: dict[int, object] = {}
        self.registered_ms: int | None = None

    def enable(self) -> bool:
        """Called only after the registration child and its _diag are gone."""
        if os.geteuid() != 0:
            return False
        try:
            metadata = self.runtime.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o077:
                return False
            baseline = oom_counters()
            registered_ms = uptime_ms()
            fd = os.open(self.runtime / MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump({"schema_version": 1, "baseline": baseline,
                           "registered_uptime_ms": registered_ms}, handle, separators=(",", ":"))
            self.baseline = baseline
            self.registered_ms = registered_ms
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
            deadline = time.monotonic() + 2
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
            history = []
            if reason == "unit_stop":
                values["history_status"] = "baseline_unavailable"
                if self.registered_ms is not None:
                    try:
                        data, values["history_status"] = journal_bytes(deadline)
                        history = classify_history(data, self.registered_ms, uptime_ms())
                    except Exception:
                        # Optional history must never suppress the original exit evidence.
                        values["history_status"] = "unavailable"
                        history = []
                values["history_event_count"] = len(history)
            self.emit("guest_diagnostic", **values)
            for event in history:
                self.emit("guest_diagnostic", **event)
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
        if (not isinstance(data, dict) or set(data) not in ({"schema_version", "baseline"},
                {"schema_version", "baseline", "registered_uptime_ms"})
                or type(data["schema_version"]) is not int or data["schema_version"] != 1):
            return None
        baseline = data["baseline"]
        if not isinstance(baseline, dict) or not set(baseline) <= {"oom_kill_count", "unit_oom_kill_count"}:
            return None
        if any(type(value) is not int or not 0 <= value <= COUNTER_LIMIT for value in baseline.values()):
            return None
        diagnostics = GuestDiagnostics(emit, runtime)
        registered_ms = data.get("registered_uptime_ms")
        if "registered_uptime_ms" in data and (type(registered_ms) is not int or not 0 <= registered_ms <= COUNTER_LIMIT):
            return None
        diagnostics.registered_ms = registered_ms
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
