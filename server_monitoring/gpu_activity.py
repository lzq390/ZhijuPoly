"""Observe GPU processes and fixed, local identity records without control APIs.

An allocated CUDA context is not evidence of a running business task. Only a
fresh, active execution lease with a live, registered workload can produce a
task row. Residency, production service identity, and MPS have separate labels.
No process arguments, PIDs, owner identities, or lease tokens leave this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import time


GPU_COMMAND = (
    "/usr/bin/nvidia-smi", "--query-compute-apps=gpu_uuid,pid,used_gpu_memory",
    "--format=csv,noheader,nounits",
)
DEV_SESSION_ROOT = Path("/data/lzq/gith/nexpoly-dev/.runtime/gpu-session")
PRODUCTION_GUARD = Path(
    "/data/lzq/gith/nexpoly-runtime/state/p9000-20261102r2/dft/gpu2-guard.json"
)
PRODUCTION_DFT_UNIT = "nexpoly-p9000-20261102r2-dft-worker.service"
PROC_ROOT = Path("/proc")
MAX_FILE_BYTES = 1024 * 1024
MAX_PROCESSES = 512
LEASE_MAX_AGE = 15.0
GUARD_MAX_AGE = 45.0
LABELS = frozenset({"DFT任务", "MD任务", "Polymer MD任务", "DFT服务", "平台服务", "NVIDIA MPS服务", "GPU进程"})
_UUID = re.compile(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_SESSION = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}\Z")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _epoch(value) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("invalid time")
    return float(value)


def _timestamp(value) -> float:
    if not isinstance(value, str):
        raise ValueError("invalid time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("time has no timezone")
    return parsed.timestamp()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _read_private_json(path: Path):
    # All callers supply a fixed path or a single validated run-directory name.
    if path.resolve(strict=True) != path:
        raise ValueError("linked state path")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_mode & 0o077 or before.st_nlink not in (0, 1)
                or not 0 < before.st_size <= MAX_FILE_BYTES):
            raise ValueError("unsafe state file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(fd)
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_size", "st_mtime_ns")
        if len(raw) != before.st_size or any(getattr(before, k) != getattr(after, k) for k in fields):
            raise ValueError("state changed during read")
        def nonfinite(_):
            raise ValueError("nonfinite state")
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=nonfinite)
        if not isinstance(value, dict):
            raise ValueError("invalid state")
        return value
    finally:
        os.close(fd)


def _proc_bytes(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("process field too large")
    return data


def _ticks(pid: int) -> int:
    if type(pid) is not int or pid <= 0:
        raise ValueError("invalid process")
    fields = _proc_bytes(PROC_ROOT / str(pid) / "stat", 4096).rsplit(b")", 1)[1].split()
    if fields[0] in (b"Z", b"X"):
        raise ValueError("process exited")
    return int(fields[19])


@dataclass(frozen=True)
class _Process:
    pid: int
    ticks: int
    started: float
    cgroup: str
    signature: str


def _process(pid: int, now: float) -> _Process:
    before = _ticks(pid)
    root = PROC_ROOT / str(pid)
    args = _proc_bytes(root / "cmdline", 16384).split(b"\0")
    cgroup = _proc_bytes(root / "cgroup", 16384).decode("utf-8")
    boot = next(int(line.split()[1]) for line in _proc_bytes(PROC_ROOT / "stat", 65536).splitlines()
                if line.startswith(b"btime "))
    started = boot + before / os.sysconf("SC_CLK_TCK")
    if not 0 < started <= now or _ticks(pid) != before:
        raise ValueError("process identity changed")
    signature = "other"
    modules = {args[i + 1] for i, value in enumerate(args[:-1]) if value == b"-m"}
    if b"workers.monomer_dft_worker.app.executor_process" in modules:
        signature = "monomer_dft"
    elif (b"app.main:app" in args or b"app.main:application" in args) and (
            b"uvicorn" in modules or (args and Path(os.fsdecode(args[0])).name == "uvicorn")
            or (len(args) > 1 and Path(os.fsdecode(args[0])).name in ("python", "python3", "python3.11")
                and Path(os.fsdecode(args[1])).name == "uvicorn")):
        signature = "platform"
    elif args and Path(os.fsdecode(args[0])).name == "nvidia-cuda-mps-server" and (
            root / "exe").resolve(strict=True).name == "nvidia-cuda-mps-server":
        signature = "nvidia_mps"
    return _Process(pid, before, started, cgroup, signature)


def _development_leases(now: float):
    controller = _read_private_json(DEV_SESSION_ROOT / "controller.json")
    if (type(controller.get("schema_version")) is not int or controller["schema_version"] != 1
            or type(controller.get("start_ticks")) is not int
            or _ticks(controller.get("pid")) != controller["start_ticks"]):
        raise ValueError("controller identity unavailable")
    run = Path(controller.get("run_directory", ""))
    if run.parent != DEV_SESSION_ROOT / "runs" or not _SESSION.fullmatch(run.name):
        raise ValueError("untrusted broker path")
    state = _read_private_json(run / "broker-state.json")
    if type(state.get("schema_version")) is not int or state["schema_version"] != 1:
        raise ValueError("unsupported broker state")
    leases = state.get("leases")
    if not isinstance(leases, list) or len(leases) > MAX_PROCESSES:
        raise ValueError("invalid broker leases")
    boot_id = _proc_bytes(PROC_ROOT / "sys/kernel/random/boot_id", 128).decode().strip()
    valid, partial = [], False
    for lease in leases:
        try:
            if not isinstance(lease, dict):
                raise ValueError("invalid lease")
            if lease.get("status") != "active":
                # Reserved/suspect/expired are not running-task evidence.
                continue
            heartbeat, created = _epoch(lease.get("heartbeat_at")), _epoch(lease.get("created_at"))
            if not 0 <= now - heartbeat <= LEASE_MAX_AGE or not 0 < created <= heartbeat:
                raise ValueError("stale lease")
            if (lease.get("owner_boot_id") != boot_id
                    or type(lease.get("owner_process_start_ticks")) is not int
                    or _ticks(lease.get("owner_pid")) != lease["owner_process_start_ticks"]
                    or type(lease.get("workload_process_start_ticks")) is not int
                    or _ticks(lease.get("workload_pid")) != lease["workload_process_start_ticks"]):
                raise ValueError("lease process changed")
            valid.append(lease)
        except (OSError, ValueError, TypeError, IndexError, KeyError, OverflowError):
            partial = True
    return valid, partial


def _production_guard(now: float):
    guard = _read_private_json(PRODUCTION_GUARD)
    if (type(guard.get("schema_version")) is not int or guard["schema_version"] != 1
            or guard.get("status") not in ("ready", "quarantined")
            or not 0 <= now - _timestamp(guard.get("observed_at")) <= GUARD_MAX_AGE
            or not isinstance(guard.get("allowed_processes"), list)
            or len(guard["allowed_processes"]) > MAX_PROCESSES
            or not isinstance(guard.get("authorities"), dict)):
        raise ValueError("production identity unavailable")
    return guard


def _lease_projection(process: _Process, gpu_uuid: str, gpu_index: int, leases):
    matches = [lease for lease in leases if lease.get("workload_pid") == process.pid
               and lease.get("workload_process_start_ticks") == process.ticks
               and lease.get("workload_cgroup") == process.cgroup.strip()
               and lease.get("gpu_uuid") == gpu_uuid and type(lease.get("gpu_index")) is int
               and lease["gpu_index"] == gpu_index]
    # An execution child and its residency parent share a process/memory row.
    # Prefer the verified execution so the process is never double-counted.
    matches.sort(key=lambda lease: lease.get("kind") != "execution")
    for lease in matches:
        environment = {"dev": "development", "prod": "production"}.get(lease.get("environment"))
        if environment is None:
            continue
        component, kind = lease.get("component"), lease.get("kind")
        request = lease.get("request_id", "")
        if kind == "execution" and isinstance(request, str):
            if component == "dft" and re.fullmatch(r"dft-[0-9a-f]{64}", request):
                return "task", "monomer_dft", environment, "DFT任务", "running", None
            if component == "md" and re.fullmatch(rf"md:{lease['environment']}:[0-9a-f]{{16}}", request):
                return "task", "monomer_md", environment, "MD任务", "running", None
        if kind == "residency" and component == "dft" and process.signature == "monomer_dft":
            return "service", "monomer_dft", environment, "DFT服务", "resident", _iso(process.started)
        if kind == "residency" and component == "backend" and process.signature == "platform":
            return "service", "platform", environment, "平台服务", "resident", _iso(process.started)
    return None


def _in_cgroup(raw: str, expected: str) -> bool:
    return any(path == expected or path.startswith(expected + "/")
               for line in raw.splitlines() if len(parts := line.split(":", 2)) == 3
               for path in (parts[2],))


def _guard_projection(process: _Process, gpu_uuid: str, gpu_index: int, guard):
    if guard is None:
        return None
    recorded_index = guard.get("gpu_index")
    if isinstance(recorded_index, str) and re.fullmatch(r"0|[1-9][0-9]*", recorded_index):
        recorded_index = int(recorded_index)
    if (guard.get("gpu_uuid") != gpu_uuid or type(recorded_index) is not int
            or recorded_index != gpu_index or process.started > _timestamp(guard["observed_at"])):
        return None
    authorities = guard["authorities"]
    for allowed in guard["allowed_processes"]:
        if (not isinstance(allowed, dict) or type(allowed.get("pid")) is not int
                or allowed["pid"] != process.pid or allowed.get("gpu_uuid") != gpu_uuid):
            continue
        authority = allowed.get("authority")
        if authority == "production-backend" and process.signature == "platform":
            container = authorities.get("backend_container")
            if isinstance(container, str) and re.fullmatch(r"[0-9a-f]{64}", container) and any(
                    part in (container, f"docker-{container}.scope") for line in process.cgroup.splitlines()
                    for part in line.split("/")):
                return "service", "platform", "production", "平台服务", "resident", _iso(process.started)
        if authority == PRODUCTION_DFT_UNIT and process.signature == "monomer_dft":
            units = authorities.get("systemd_user_units", {})
            root = units.get(PRODUCTION_DFT_UNIT) if isinstance(units, dict) else None
            if not isinstance(root, dict) or not isinstance(root.get("cgroup"), str):
                continue
            expected = root["cgroup"]
            if not expected.startswith("/") or expected.split("/")[-1] != PRODUCTION_DFT_UNIT:
                continue
            parent = _process(root.get("pid"), _timestamp(guard["observed_at"]))
            if _in_cgroup(parent.cgroup, expected) and _in_cgroup(process.cgroup, expected):
                return "service", "monomer_dft", "production", "DFT服务", "resident", _iso(process.started)
    return None


def collect_activity(inventory: dict[str, int], clock=time.time) -> dict:
    """Return only privacy-safe activity rows for the current UUID inventory.

    ``running`` describes an active, registered execution admission, not proof
    that a GPU kernel runs at this instant. Its start time stays null because a
    lease's created_at is allocation time, not actual task start time.
    """
    captured = _epoch(clock())
    result = {"captured_at": _iso(captured), "status": "ready", "error_code": None, "items": []}
    if (not isinstance(inventory, dict) or not inventory or len(inventory) > 64
            or any(not isinstance(uuid, str) or not _UUID.fullmatch(uuid)
                   or type(index) is not int or index < 0 for uuid, index in inventory.items())
            or len(set(inventory.values())) != len(inventory)):
        return {**result, "status": "unavailable", "error_code": "gpu_inventory_unavailable"}
    try:
        command = subprocess.run(GPU_COMMAND, cwd="/", env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 check=True, text=True, timeout=3)
        if len(command.stdout) > MAX_FILE_BYTES:
            raise ValueError("process query too large")
    except subprocess.TimeoutExpired:
        return {**result, "status": "unavailable", "error_code": "gpu_process_query_timeout"}
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return {**result, "status": "unavailable", "error_code": "gpu_process_query_failed"}

    observations, partial = {}, False
    for number, columns in enumerate(csv.reader(io.StringIO(command.stdout))):
        if not columns:
            continue
        if number >= MAX_PROCESSES:
            partial = True
            break
        try:
            if len(columns) != 3:
                raise ValueError("unsupported process fields")
            uuid, raw_pid, memory = (field.strip() for field in columns)
            if uuid not in inventory or not raw_pid.isascii() or not raw_pid.isdecimal() or int(raw_pid) <= 0:
                raise ValueError("unrecognized GPU process")
            used = None
            if memory.isascii() and memory.isdecimal():
                used = int(memory) * 1024 * 1024
                if used > 2 ** 50:
                    raise ValueError("invalid process memory")
            else:
                partial = True
            key = uuid, int(raw_pid)
            if key in observations:
                partial = True
                continue
            observations[key] = used
        except (ValueError, TypeError, OverflowError):
            partial = True

    leases, guard = [], None
    if observations:
        try:
            leases, incomplete = _development_leases(captured)
            partial |= incomplete
        except (OSError, ValueError, TypeError, IndexError, KeyError, OverflowError, RecursionError):
            partial = True
        try:
            guard = _production_guard(captured)
            partial |= guard.get("status") != "ready"
        except (OSError, ValueError, TypeError, IndexError, KeyError, OverflowError, RecursionError):
            partial = True
    for (gpu_uuid, pid), memory in observations.items():
        projection = None
        ticks = "unverified"
        try:
            process = _process(pid, captured)
            ticks = process.ticks
            projection = _lease_projection(process, gpu_uuid, inventory[gpu_uuid], leases)
            if projection is None:
                projection = _guard_projection(process, gpu_uuid, inventory[gpu_uuid], guard)
            if projection is None and process.signature == "nvidia_mps":
                projection = ("service", "nvidia_mps", "unknown", "NVIDIA MPS服务", "resident", _iso(process.started))
            if _ticks(pid) != ticks:
                raise ValueError("process identity changed")
        except (OSError, ValueError, TypeError, IndexError, KeyError, StopIteration, OverflowError):
            projection = None
            partial = True
        if projection is None:
            projection = ("process", "other", "unknown", "GPU进程", "observed", None)
        kind, module, environment, label, status, started = projection
        result["items"].append({
            "id": hashlib.sha256(f"{gpu_uuid}:{pid}:{ticks}".encode()).hexdigest()[:20],
            "gpu_uuid": gpu_uuid, "kind": kind, "module": module, "environment": environment,
            "label": label, "status": status, "memory_used_bytes": memory, "started_at": started,
        })
    result["items"].sort(key=lambda item: (inventory[item["gpu_uuid"]], item["kind"], item["id"]))
    if partial:
        result.update(status="partial", error_code="gpu_activity_incomplete")
    return result
