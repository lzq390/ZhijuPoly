"""Host-only telemetry collector. No CUDA, Broker, application or database access."""
from __future__ import annotations

import argparse
import csv
from collections import deque
from datetime import datetime, timezone
import io
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

from .models import Gpu, HostInfo, validate_history, validate_snapshot


GPU_COMMAND = (
    "/usr/bin/nvidia-smi",
    "--query-gpu=index,uuid,name,utilization.gpu,memory.total,memory.used,temperature.gpu,power.draw",
    "--format=csv,noheader,nounits",
)
# An explicit environment prevents CUDA/MPS device remapping and LD_PRELOAD.
GPU_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
HOST_INFO_INTERVAL_SECONDS = 3600
HOST_INFO_FIELDS = ("cpu_model", "cpu_socket_count", "cpu_physical_core_count", "os_name", "kernel_release", "nvidia_driver_version")


def _host_text(path: Path, limit: int) -> str:
    # os-release is normally a trusted system symlink. Follow that fixed path,
    # but reject special files and bound reads (procfs reports a zero size).
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("host metadata must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("host metadata exceeds read limit")
        return raw.decode("utf-8")
    finally:
        os.close(descriptor)


def _host_label(value: str) -> str | None:
    value = value.strip()
    return value if 1 <= len(value) <= 160 and not re.search(r"[\x00-\x1f\x7f-\x9f]", value) else None


def parse_cpu_info(text: str) -> dict:
    fields = {"cpu_model": None, "cpu_socket_count": None, "cpu_physical_core_count": None}
    processors = []
    relevant = {"processor", "model name", "physical id", "core id"}
    for block in re.split(r"\n\s*\n", text.strip()):
        record, duplicate = {}, False
        for line in block.splitlines():
            key, separator, value = line.partition(":")
            key = key.strip()
            if separator and key in relevant:
                duplicate |= key in record
                record[key] = value.strip()
        if "processor" in record:
            record["duplicate"] = duplicate
            processors.append(record)
    if not processors:
        return fields
    models = {_host_label(record.get("model name", "")) for record in processors}
    if len(models) == 1:
        fields["cpu_model"] = models.pop()
    def identifier(value, maximum):
        return isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,5}", value) is not None and int(value) <= maximum
    if (any(record["duplicate"] or not identifier(record["processor"], 65535) for record in processors)
            or len({record["processor"] for record in processors}) != len(processors)):
        return fields
    if not all(identifier(record.get("physical id"), 1023) for record in processors):
        return fields
    fields["cpu_socket_count"] = len({record["physical id"] for record in processors})
    if all(identifier(record.get("core id"), 65535) for record in processors):
        # Core IDs are local to a socket; SMT siblings repeat the same pair.
        fields["cpu_physical_core_count"] = len({(record["physical id"], record["core id"]) for record in processors})
    return fields


def _os_release_value(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'") or "'" in value[1:-1]:
            return None
        decoded = value[1:-1]
    elif value.startswith('"'):
        if len(value) < 2 or not value.endswith('"'):
            return None
        decoded, index = "", 1
        while index < len(value) - 1:
            character = value[index]
            if character == "\\":
                index += 1
                if index >= len(value) - 1 or value[index] not in {'"', "\\", "$", "`"}:
                    return None
                decoded += value[index]
            elif character in {'"', "$", "`"}:
                # Never perform shell expansion or accept concatenated quotes.
                return None
            else:
                decoded += character
            index += 1
    else:
        if not re.fullmatch(r"[A-Za-z0-9._+/-]+", value):
            return None
        decoded = value
    return _host_label(decoded)


def parse_os_name(text: str) -> str | None:
    names, seen = {}, set()
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in {"PRETTY_NAME", "NAME", "VERSION"}:
            continue
        # A repeated field is ambiguous; do not silently choose one of them.
        names[key] = None if key in seen else _os_release_value(value)
        seen.add(key)
    if "PRETTY_NAME" in names:
        return names["PRETTY_NAME"]
    if names.get("NAME"):
        return _host_label(" ".join(filter(None, (names["NAME"], names.get("VERSION")))))
    return None


def parse_nvidia_driver(text: str) -> str | None:
    lines = [line for line in text.splitlines() if line.startswith("NVRM version:") and "NVIDIA" in line and "Kernel Module" in line]
    if len(lines) != 1:
        return None
    match = re.search(r"(?<![A-Za-z0-9_.])([0-9]+(?:\.[0-9]+)+)(?![A-Za-z0-9_.])", lines[0].split("Kernel Module", 1)[1])
    return match.group(1) if match and re.fullmatch(r"[0-9]{1,4}(?:\.[0-9]{1,4}){1,3}", match.group(1)) else None


def read_host_info(*, proc_root: Path = Path("/proc"), os_release_path: Path = Path("/etc/os-release")) -> dict:
    result = dict.fromkeys(HOST_INFO_FIELDS)
    try:
        result.update(parse_cpu_info(_host_text(proc_root / "cpuinfo", 4 * 1024 * 1024)))
    except (OSError, ValueError, UnicodeError):
        pass
    try:
        result["os_name"] = parse_os_name(_host_text(os_release_path, 32 * 1024))
    except (OSError, ValueError, UnicodeError):
        pass
    try:
        release = _host_text(proc_root / "sys/kernel/osrelease", 4096).strip()
        if len(release) <= 128 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+~-]*", release):
            result["kernel_release"] = release
    except (OSError, ValueError, UnicodeError):
        pass
    try:
        result["nvidia_driver_version"] = parse_nvidia_driver(_host_text(proc_root / "driver/nvidia/version", 32 * 1024))
    except (OSError, ValueError, UnicodeError):
        pass
    return HostInfo.model_validate(result).model_dump()


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def cpu_counters(text: str) -> tuple[int, ...]:
    line = next(line for line in text.splitlines() if line.startswith("cpu "))
    values = tuple(int(value) for value in line.split()[1:9])
    if len(values) < 4 or any(value < 0 for value in values):
        raise ValueError("invalid CPU counters")
    return values


def cpu_percent(previous: tuple[int, ...] | None, current: tuple[int, ...]) -> float | None:
    if previous is None or len(previous) != len(current) or any(b < a for a, b in zip(previous, current)):
        return None
    delta = [b - a for a, b in zip(previous, current)]
    total = sum(delta)
    if total <= 0:
        return None
    idle = delta[3] + (delta[4] if len(delta) > 4 else 0)
    return max(0.0, min(100.0, 100.0 * (total - idle) / total))


def memory_values(text: str) -> dict:
    fields = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] in {"MemTotal:", "MemAvailable:"} and parts[2] == "kB":
            fields[parts[0]] = int(parts[1]) * 1024
    total, available = fields["MemTotal:"], fields["MemAvailable:"]
    if total <= 0 or not 0 <= available <= total:
        raise ValueError("invalid memory counters")
    return {"total_bytes": total, "available_bytes": available, "used_bytes": total - available,
            "used_percent": 100.0 * (total - available) / total}


def _number(value: str, low: float, high: float) -> float | None:
    try:
        number = float(value.strip())
    except ValueError:
        return None
    return number if math.isfinite(number) and low <= number <= high else None


def parse_gpus(text: str) -> tuple[list[dict], bool]:
    devices, partial, seen = [], False, set()
    for fields in csv.reader(io.StringIO(text)):
        if not fields or all(not item.strip() for item in fields):
            continue
        fields = [item.strip() for item in fields]
        if len(fields) != 8:
            partial = True
            continue
        try:
            index = int(fields[0])
            total = _number(fields[4], 0, (2**63 - 1) / 1048576)
            used = _number(fields[5], 0, (2**63 - 1) / 1048576)
            item = Gpu(index=index, uuid=fields[1], name=fields[2],
                       utilization_percent=_number(fields[3], 0, 100),
                       memory_total_bytes=None if total is None else int(total * 1048576),
                       memory_used_bytes=None if used is None else int(used * 1048576),
                       temperature_celsius=_number(fields[6], -100, 250),
                       power_watts=_number(fields[7], 0, 10000)).model_dump()
            if item["uuid"] in seen or any(device["index"] == index for device in devices):
                raise ValueError("duplicate GPU identity")
            seen.add(item["uuid"])
            partial |= any(item[name] is None for name in ("utilization_percent", "memory_total_bytes", "memory_used_bytes", "temperature_celsius", "power_watts"))
            devices.append(item)
        except (ValueError, TypeError):
            partial = True
    return sorted(devices, key=lambda item: item["index"]), partial


def query_gpus() -> tuple[list[dict], bool]:
    completed = subprocess.run(GPU_COMMAND, cwd="/", env=GPU_ENVIRONMENT, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                               encoding="utf-8", timeout=3, check=True)
    if len(completed.stdout) > 128 * 1024:
        raise ValueError("GPU response exceeds limit")
    return parse_gpus(completed.stdout)


def atomic_json(path: Path, payload: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".monitor-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(payload, stream, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _mean(values):
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else None


class HistoryRing:
    def __init__(self):
        self.points = deque(maxlen=120)
        self.bucket: int | None = None
        self.samples: deque[dict] = deque(maxlen=64)

    def observe(self, snapshot: dict, epoch: float) -> bool:
        bucket = int(epoch // 30)
        changed = False
        if self.bucket is not None and bucket < self.bucket:
            # Never join two clock eras into a falsely continuous trend.
            self.points.clear()
            self.samples.clear()
            changed = True
            self.bucket = None
        if self.bucket is not None and bucket > self.bucket:
            self.points.append(self._aggregate(self.bucket, self.samples))
            for missing in range(max(self.bucket + 1, bucket - 120), bucket):
                self.points.append(self._aggregate(missing, []))
            self.samples.clear()
            changed = True
        self.bucket = bucket
        self.samples.append(snapshot)
        cutoff = epoch - 3600
        while self.points and datetime.fromisoformat(self.points[0]["timestamp"].replace("Z", "+00:00")).timestamp() < cutoff:
            self.points.popleft()
        return changed

    @staticmethod
    def _aggregate(bucket: int, samples) -> dict:
        # UUID, rather than a mutable device index, identifies each series.
        uuids = sorted({gpu["uuid"] for sample in samples for gpu in sample["gpus"]})
        return {
            "timestamp": iso((bucket + 1) * 30),
            "cpu_percent": _mean((sample["cpu"] or {}).get("utilization_percent") for sample in samples),
            "memory_percent": _mean((sample["memory"] or {}).get("used_percent") for sample in samples),
            "gpus": [{"uuid": uuid,
                      "utilization_percent": _mean(gpu["utilization_percent"] for sample in samples for gpu in sample["gpus"] if gpu["uuid"] == uuid),
                      "memory_used_bytes": _mean(gpu["memory_used_bytes"] for sample in samples for gpu in sample["gpus"] if gpu["uuid"] == uuid)} for uuid in uuids],
        }


class Collector:
    def __init__(self, output_dir: Path, *, proc_root: Path = Path("/proc"),
                 disks=(("/", "系统盘"), ("/data", "数据盘")), gpu_query=query_gpus,
                 clock=time.time, monotonic=time.monotonic, host_info_reader=None):
        self.output_dir, self.proc_root, self.disks = output_dir, proc_root, disks
        self.gpu_query, self.clock, self.monotonic = gpu_query, clock, monotonic
        self.host_info_reader = host_info_reader or (lambda: read_host_info(proc_root=self.proc_root))
        self._host_info = None
        self._host_info_at: float | None = None
        self.started_epoch = clock()
        self.last_wall_epoch = self.started_epoch
        self.started_at = iso(self.started_epoch)
        self.collector_id = str(uuid4())
        self.hostname = socket.gethostname()[:253] or "local-server"
        self.previous_cpu = None
        self.previous_cpu_at: float | None = None
        self.last_success = {name: None for name in ("cpu", "memory", "disks", "gpus")}
        self.sequence = 0
        self.history = HistoryRing()

    def _observe_time(self, epoch: float) -> bool:
        rolled_back = epoch < self.last_wall_epoch
        if rolled_back:
            # The process identity/sequence stay stable. Start a new observation
            # window without retaining success timestamps from the clock's future.
            self.started_epoch = min(self.started_epoch, epoch)
            self.started_at = iso(self.started_epoch)
            self.last_success = {name: None for name in self.last_success}
            self.history = HistoryRing()
        self.last_wall_epoch = epoch
        return rolled_back

    def _component(self, name: str, status: str, captured: str, error: str | None = None) -> dict:
        if status == "ok":
            self.last_success[name] = captured
        return {"status": status, "last_success_at": self.last_success[name], "error_code": error}

    def sample(self) -> tuple[dict, dict]:
        values, states = {}, {}
        host_info_at = self.monotonic()
        if self._host_info_at is None or not 0 <= host_info_at - self._host_info_at < HOST_INFO_INTERVAL_SECONDS:
            try:
                self._host_info = HostInfo.model_validate(self.host_info_reader()).model_dump()
            except (OSError, ValueError, TypeError):
                self._host_info = dict.fromkeys(HOST_INFO_FIELDS)
            self._host_info_at = self.monotonic()
        values["host_info"] = self._host_info
        if self._observe_time(self.clock()):
            self.previous_cpu = None
            self.previous_cpu_at = None
        try:
            cpu_at = self.monotonic()
            current = cpu_counters((self.proc_root / "stat").read_text())
            if self.previous_cpu_at is not None and not 0 <= cpu_at - self.previous_cpu_at <= 15:
                self.previous_cpu = None
            value = cpu_percent(self.previous_cpu, current)
            self.previous_cpu = current
            self.previous_cpu_at = cpu_at
            values["cpu"] = {"utilization_percent": value}
            states["cpu"] = ("warming", "cpu_baseline") if value is None else ("ok", None)
        except (OSError, ValueError, StopIteration):
            self.previous_cpu = None
            self.previous_cpu_at = None
            values["cpu"], states["cpu"] = None, ("unavailable", "cpu_read_failed")
        try:
            values["memory"] = memory_values((self.proc_root / "meminfo").read_text())
            states["memory"] = ("ok", None)
        except (OSError, ValueError, KeyError):
            values["memory"], states["memory"] = None, ("unavailable", "memory_read_failed")
        values["disks"] = []
        for mount, label in self.disks:
            item = {"mount_point": mount, "label": label, "total_bytes": None, "used_bytes": None,
                    "available_bytes": None, "used_percent": None, "status": "unavailable"}
            try:
                stat = os.statvfs(mount)
                total, free, available = stat.f_blocks * stat.f_frsize, stat.f_bfree * stat.f_frsize, stat.f_bavail * stat.f_frsize
                if total <= 0 or not 0 <= available <= free <= total:
                    raise ValueError("invalid filesystem counters")
                used = total - free
                item.update(total_bytes=total, used_bytes=used, available_bytes=available,
                            used_percent=100.0 * used / (used + available) if used + available else 0.0, status="ok")
            except (OSError, ValueError):
                pass
            values["disks"].append(item)
        states["disks"] = ("ok", None) if all(item["status"] == "ok" for item in values["disks"]) else ("unavailable", "disk_read_failed")
        try:
            values["gpus"], partial = self.gpu_query()
            states["gpus"] = ("unavailable", "gpu_metrics_partial") if partial else ("ok", None)
        except (OSError, ValueError, subprocess.SubprocessError):
            values["gpus"], states["gpus"] = [], ("unavailable", "gpu_query_failed")
        try:
            values["load_average"] = list(os.getloadavg())
        except OSError:
            values["load_average"] = None
        try:
            values["uptime_seconds"] = _number((self.proc_root / "uptime").read_text().split()[0], 0, 1e18)
        except (OSError, IndexError):
            values["uptime_seconds"] = None
        epoch = self.clock()
        if self._observe_time(epoch) and values["cpu"] is not None:
            # A rollback during this sample also invalidates its interval. Keep
            # the just-read counters as the baseline for the next five seconds.
            values["cpu"] = {"utilization_percent": None}
            states["cpu"] = ("warming", "cpu_baseline")
        captured = iso(epoch)
        self.sequence += 1
        envelope = {"schema_version": 1, "node_id": "local-server", "collector_id": self.collector_id,
                    "started_at": self.started_at, "captured_at": captured}
        snapshot = validate_snapshot({**envelope, "hostname": self.hostname, "sequence": self.sequence,
                    "logical_cpu_count": os.cpu_count() or 1, **values,
                    "components": {name: self._component(name, status, captured, error) for name, (status, error) in states.items()}}).model_dump()
        self.history.observe(snapshot, epoch)
        history = validate_history({**envelope, "interval_seconds": 30, "points": list(self.history.points)}).model_dump()
        return snapshot, history

    def run(self, stop: threading.Event, *, interval: float = 5.0, once: bool = False):
        self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # This process owns the history epoch. Existing history is never restored.
        while not stop.is_set():
            started = self.monotonic()
            snapshot, history = self.sample()
            atomic_json(self.output_dir / "latest.json", snapshot)
            atomic_json(self.output_dir / "history.json", history)
            if once:
                return
            stop.wait(max(0.05, interval - (self.monotonic() - started)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    stop = threading.Event()
    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, lambda *_: stop.set())
    Collector(args.output_dir).run(stop, once=args.once)


if __name__ == "__main__":
    main()
