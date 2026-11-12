"""Read verified GPU execution history and current activity without business imports."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import tempfile
import threading
import time
from uuid import uuid4

from .models import validate_snapshot
from .md_history import collect_md_history
from .task_history_models import GpuActivity, GpuTask, SOURCE_IDS, timestamp, validate_task_history


SOURCE_ROOTS = {
    "development_dft": Path("/data/lzq/gith/nexpoly-dev/.runtime/monomer-dft-worker-runs"),
    "production_dft": (Path("/data/lzq/gith/nexpoly-runtime/state/monomer-dft-worker-runs"),
                       Path("/data/lzq/gith/nexpoly-runtime/state/p9000-20261102r2/dft/jobs")),
}
HISTORY_INTERVAL_SECONDS = 60
ACTIVITY_INTERVAL_SECONDS = 5
LAST_GOOD_FILE = "gpu-tasks-last-good.json"
MAX_LAST_GOOD_BYTES = 2 * 1024 * 1024
MAX_JOURNAL_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_JOURNALS = 4096
MAX_DIRECTORY_ENTRIES = 8192
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _read_json(name, limit: int, *, directory: int | None = None, on_bytes=None):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink not in (0, 1) or before.st_uid != os.geteuid()
                or before.st_mode & 0o077 or before.st_size > limit):
            raise ValueError("unsafe history file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        if on_bytes is not None:
            on_bytes(len(raw))
        after = os.fstat(fd)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if len(raw) != before.st_size or len(raw) > limit or identity(before) != identity(after):
            raise ValueError("history file changed during read")
        def nonfinite(_value):
            raise ValueError("non-finite JSON value")
        return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=nonfinite), len(raw)
    finally:
        os.close(fd)


def _directory(name, *, parent: int | None = None) -> int:
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError("unsafe history directory")
    return fd


class ScanLimit(ValueError):
    pass


def _children(directory: int, budget: list[int]):
    with os.scandir(directory) as entries:
        for entry in entries:
            budget[0] += 1
            if budget[0] > MAX_DIRECTORY_ENTRIES:
                raise ScanLimit("directory scan limit")
            if entry.name.startswith("."):
                continue
            if entry.is_symlink():
                yield entry.name, False
            elif entry.is_dir(follow_symlinks=False):
                yield entry.name, True


def _task(payload, source_id: str, job_name: str, attempt_name: str, inventory: dict[str, int], now: float):
    if not isinstance(payload, dict) or not isinstance(payload.get("snapshot"), dict):
        raise ValueError("invalid journal envelope")
    snapshot = payload["snapshot"]
    if snapshot.get("status") in {"pending", "queued", "running", "failed", "cancel_requested", "cancelled"}:
        return None
    if snapshot.get("status") != "completed":
        raise ValueError("invalid journal status")
    result = snapshot.get("result")
    if not isinstance(result, dict):
        raise ValueError("completed journal has no result")
    if (payload.get("journal_schema_version") != 2 or snapshot.get("schema_version") != 2
            or result.get("schema_version") != 2 or payload.get("enqueue_sequence_source") != "backend"):
        return None
    provenance = result.get("provenance")
    if not isinstance(provenance, dict) or not provenance.get("gpu_uuid"):
        return None
    gpu_uuid = provenance["gpu_uuid"]
    if not isinstance(gpu_uuid, str) or gpu_uuid not in inventory:
        return None
    required = ("worker_instance_id", "lease_id", "broker_instance_id", "physical_gpu", "gpu_physical_device")
    if any(not isinstance(provenance.get(field), str) or not provenance[field] for field in required):
        return None
    if type(provenance.get("fencing_token")) is not int or provenance["fencing_token"] <= 0:
        return None
    if (provenance.get("execution_path") not in {"primary", "overflow"} or type(provenance.get("visible_gpu_count")) is not int or provenance["visible_gpu_count"] != 1
            or provenance.get("logical_device") != "cuda:0" or provenance.get("gpu_logical_device") != "cuda:0"):
        return None
    if (not isinstance(snapshot.get("job_id"), str) or not SAFE_IDENTIFIER.fullmatch(snapshot["job_id"])
            or snapshot["job_id"] != job_name or snapshot.get("attempt_token") != attempt_name
            or not isinstance(snapshot.get("worker_instance_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", snapshot["worker_instance_id"])
            or snapshot["worker_instance_id"] != provenance["worker_instance_id"]
            or type(snapshot.get("enqueue_sequence")) is not int or snapshot["enqueue_sequence"] <= 0
            or type(payload.get("enqueue_sequence")) is not int or payload["enqueue_sequence"] != snapshot["enqueue_sequence"]
            or provenance["physical_gpu"] != provenance["gpu_physical_device"]
            or not re.fullmatch(r"0|[1-9][0-9]{0,3}", provenance["physical_gpu"])
            or int(provenance["physical_gpu"]) > 1023):
        raise ValueError("inconsistent execution identity")
    created, started, finished = (timestamp(snapshot[field]) for field in ("created_at", "started_at", "finished_at"))
    if any(value.tzinfo is None or value.utcoffset() is None for value in (created, started, finished)):
        raise ValueError("execution timestamp lacks timezone")
    if not created <= started <= finished or finished.timestamp() > now:
        raise ValueError("invalid execution time")
    return GpuTask.model_validate({
        "id": hashlib.sha256(f"{source_id}:{snapshot['job_id']}".encode()).hexdigest()[:20],
        "gpu_uuid": gpu_uuid, "module": "monomer_dft", "environment": source_id.removesuffix("_dft"),
        "calculation_type": result.get("calculation_type"), "status": "completed",
        "started_at": snapshot["started_at"], "finished_at": snapshot["finished_at"],
        "duration_seconds": round((finished - started).total_seconds(), 3),
    }).model_dump()


def scan_source(source_id: str, root: Path, inventory: dict[str, int], clock=time.time):
    source = {"source_id": source_id, "status": "ready", "matched_tasks": 0, "excluded_tasks": 0, "error_code": None}
    tasks: dict[str, dict] = {}
    budget, journals, total_bytes = [0], 0, 0
    def incomplete(code: str):
        source["status"], source["error_code"] = "partial", code
    def count_bytes(size: int):
        nonlocal total_bytes
        total_bytes += size
        if total_bytes > MAX_SOURCE_BYTES:
            raise ScanLimit("source byte limit")
    try:
        root_fd = _directory(root)
    except (OSError, ValueError):
        source.update(status="unavailable", error_code="source_unavailable")
        return source, []
    try:
        for job_name, safe in _children(root_fd, budget):
            if not safe:
                incomplete("unsafe_source_path")
                continue
            try:
                job_fd = _directory(job_name, parent=root_fd)
            except (OSError, ValueError):
                incomplete("source_changed")
                continue
            try:
                for attempt_name, safe in _children(job_fd, budget):
                    if not safe:
                        incomplete("unsafe_source_path")
                        continue
                    journals += 1
                    if journals > MAX_JOURNALS or total_bytes >= MAX_SOURCE_BYTES:
                        raise ScanLimit("journal scan limit")
                    try:
                        attempt_fd = _directory(attempt_name, parent=job_fd)
                        try:
                            payload, _ = _read_json("journal.json", min(MAX_JOURNAL_BYTES, MAX_SOURCE_BYTES - total_bytes), directory=attempt_fd, on_bytes=count_bytes)
                        finally:
                            os.close(attempt_fd)
                        task = _task(payload, source_id, job_name, attempt_name, inventory, clock())
                        if task is None:
                            source["excluded_tasks"] += 1
                            continue
                        previous = tasks.get(task["id"])
                        if previous is not None:
                            source["excluded_tasks"] += 1
                        if previous is None or timestamp(task["finished_at"]) > timestamp(previous["finished_at"]):
                            tasks[task["id"]] = task
                    except ScanLimit:
                        raise
                    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
                        source["excluded_tasks"] += 1
                        incomplete("invalid_journal")
            finally:
                os.close(job_fd)
    except ScanLimit:
        incomplete("scan_limit")
    except OSError:
        incomplete("source_changed")
    finally:
        os.close(root_fd)
    source["matched_tasks"] = len(tasks)
    return source, list(tasks.values())


def atomic_json(path: Path, payload: dict):
    fd, temporary = tempfile.mkstemp(prefix=".gpu-tasks-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(payload, stream, separators=(",", ":"), allow_nan=False)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _source_group(source_id, roots, inventory, clock):
    """Several fixed retained cohorts can contain the same completed job."""
    if isinstance(roots, Path):
        return scan_source(source_id, roots, inventory, clock)
    parts, tasks = [], {}
    excluded = 0
    for root in roots:
        source, rows = scan_source(source_id, root, inventory, clock)
        parts.append(source); excluded += source["excluded_tasks"]
        for task in rows:
            previous = tasks.get(task["id"])
            if previous:
                excluded += 1
            if previous is None or timestamp(task["finished_at"]) > timestamp(previous["finished_at"]):
                tasks[task["id"]] = task
    status = "ready" if all(part["status"] == "ready" for part in parts) else "unavailable" if all(part["status"] == "unavailable" for part in parts) else "partial"
    error = next((part["error_code"] for part in parts if part["error_code"]), None)
    if len(tasks) > MAX_JOURNALS or excluded > 16384:
        status, error = "partial", "scan_limit"
    rows = sorted(tasks.values(), key=lambda task: timestamp(task["finished_at"]), reverse=True)[:MAX_JOURNALS]
    return {"source_id": source_id, "status": status, "matched_tasks": len(rows),
            "excluded_tasks": min(excluded, 16384), "error_code": error}, rows


def _unavailable_sources(code):
    return [{"source_id": name, "status": "unavailable", "matched_tasks": 0,
             "excluded_tasks": 0, "error_code": code} for name in SOURCE_IDS]


def _task_source(task):
    return task["environment"] + {"monomer_dft": "_dft", "monomer_md": "_md", "polymer_md": "_polymer_md"}[task["module"]]


def _activity(inventory, clock):
    from .gpu_activity import collect_activity
    return collect_activity(inventory, clock=clock)


class TaskHistoryCollector:
    def __init__(self, output_dir: Path, *, sources=None, clock=time.time, monotonic=time.monotonic,
                 md_reader=collect_md_history, activity_reader=_activity):
        self.output_dir, self.sources = output_dir, dict(SOURCE_ROOTS if sources is None else sources)
        if set(self.sources) != set(SOURCE_ROOTS):
            raise ValueError("both fixed source identities are required")
        self.clock, self.monotonic = clock, monotonic
        self.md_reader, self.activity_reader = md_reader, activity_reader
        self.started_epoch = clock()
        self.collector_id = str(uuid4())
        self._cache = None
        self._good_inventory: tuple[str, ...] | None = None
        self._last_good_history = None
        self._last_good_activity = None
        self._last_good_changed = False
        self._inventory_available = False

    def _discard_future_good(self, now):
        for name, time_key in (("_last_good_history", "history_captured_at"), ("_last_good_activity", "captured_at")):
            value = getattr(self, name)
            if value is not None and timestamp(value[time_key]).timestamp() > now:
                setattr(self, name, None)
                self._last_good_changed = True

    def _prepare_last_good(self, inventory, now):
        identity = tuple(sorted(inventory))
        if self._good_inventory is None:
            self._good_inventory = identity
            try:
                raw, _ = _read_json(self.output_dir / LAST_GOOD_FILE, MAX_LAST_GOOD_BYTES)
                saved = validate_task_history(raw).model_dump()
                if tuple(sorted(saved["gpu_uuids"])) != identity or timestamp(saved["captured_at"]).timestamp() > now:
                    raise ValueError("last-good inventory or clock does not match")
                # The public strict schema also fixes node_id. Only groups that
                # were independently complete are eligible, including [] results.
                if saved["status"] == "ready":
                    self._last_good_history = {key: deepcopy(saved[key]) for key in ("history_captured_at", "sources", "tasks")}
                if saved["activity"]["status"] == "ready":
                    self._last_good_activity = deepcopy(saved["activity"])
            except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
                pass
        elif self._good_inventory != identity:
            # A partial overlap is insufficient: replacing/removing any physical
            # device retires the entire last-good observation for this node.
            self._good_inventory = identity
            self._last_good_history = self._last_good_activity = None
            self._last_good_changed = True
        self._discard_future_good(now)

    def _with_last_good(self, payload):
        self._discard_future_good(timestamp(payload["captured_at"]).timestamp())
        if not self._inventory_available:
            return payload
        if payload["status"] == "ready":
            candidate = {key: deepcopy(payload[key]) for key in ("history_captured_at", "sources", "tasks")}
            if candidate != self._last_good_history:
                self._last_good_history = candidate
                self._last_good_changed = True
        elif self._last_good_history is not None:
            latest_sources = {source["source_id"]: source for source in payload["sources"]}
            payload.update(deepcopy(self._last_good_history))
            # Counts describe the displayed old group. Current failure codes
            # remain available, and even currently readable sources are partial
            # because this whole displayed history predates the latest attempt.
            for source in payload["sources"]:
                source.update(status="partial", error_code=latest_sources[source["source_id"]]["error_code"] or "cache_fallback")
            payload["status"] = "partial"
        if payload["activity"]["status"] == "ready":
            if payload["activity"] != self._last_good_activity:
                self._last_good_activity = deepcopy(payload["activity"])
                self._last_good_changed = True
        elif self._last_good_activity is not None:
            error = payload["activity"]["error_code"] or "cache_fallback"
            payload["activity"] = {**deepcopy(self._last_good_activity), "status": "partial", "error_code": error}
        return validate_task_history(payload).model_dump()

    def _persist_last_good(self, captured):
        if not self._last_good_changed or self._good_inventory is None:
            return
        history = self._last_good_history
        saved = {"schema_version": 1, "node_id": "local-server", "collector_id": self.collector_id,
                 "started_at": iso(self.started_epoch), "captured_at": captured, "coverage": "retained_verified_gpu_records",
                 "gpu_uuids": list(self._good_inventory or ()), "status": "ready" if history is not None else "unavailable",
                 "history_captured_at": history["history_captured_at"] if history is not None else captured,
                 "sources": history["sources"] if history is not None else _unavailable_sources("last_good_unavailable"),
                 "tasks": history["tasks"] if history is not None else [],
                 "activity": self._last_good_activity if self._last_good_activity is not None else
                    {"captured_at": captured, "status": "unavailable", "error_code": "last_good_unavailable", "items": []}}
        saved = validate_task_history(saved).model_dump()
        try:
            atomic_json(self.output_dir / LAST_GOOD_FILE, saved)
        except OSError:
            # A cache write failure must not stop publishing verified telemetry.
            # Keep dirty so the next collection retries this fixed private file.
            return
        self._last_good_changed = False

    def sample(self, *, force_history=False):
        inventory = {}
        self._inventory_available = False
        try:
            payload, _ = _read_json(self.output_dir / "latest.json", 256 * 1024)
            snapshot = validate_snapshot(payload)
            age = self.clock() - timestamp(snapshot.captured_at).timestamp()
            if not 0 <= age <= 60 or snapshot.components.gpus.status != "ok":
                raise ValueError("GPU inventory unavailable")
            inventory = {gpu.uuid: gpu.index for gpu in snapshot.gpus}
        except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
            sources, tasks = _unavailable_sources("gpu_inventory_unavailable"), []
            history_captured = self.clock()
            self._cache = None
            activity = {"captured_at": iso(self.clock()), "status": "unavailable", "error_code": "gpu_inventory_unavailable", "items": []}
        else:
            self._inventory_available = True
            self._prepare_last_good(inventory, self.clock())
            refresh = (force_history or self._cache is None or self._cache["inventory"] != inventory
                       or not 0 <= self.monotonic() - self._cache["monotonic"] < HISTORY_INTERVAL_SECONDS
                       or self.clock() < self._cache["captured"])
            if refresh:
                sources, tasks = [], []
                for source_id, roots in self.sources.items():
                    source, rows = _source_group(source_id, roots, inventory, self.clock)
                    sources.append(source); tasks.extend(rows)
                md_sources, md_tasks = self.md_reader(inventory, clock=self.clock)
                sources.extend(md_sources); tasks.extend(md_tasks)
                sources.sort(key=lambda source: SOURCE_IDS.index(source["source_id"]))
                self._cache = {"inventory": dict(inventory), "sources": sources, "tasks": tasks,
                               "captured": self.clock(), "monotonic": self.monotonic()}
            sources, tasks = deepcopy(self._cache["sources"]), deepcopy(self._cache["tasks"])
            history_captured = self._cache["captured"]
            try:
                activity = GpuActivity.model_validate(self.activity_reader(inventory, clock=self.clock)).model_dump()
                if any(item["gpu_uuid"] not in inventory for item in activity["items"]):
                    raise ValueError("activity inventory changed")
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
                activity = {"captured_at": iso(self.clock()), "status": "unavailable", "error_code": "activity_unavailable", "items": []}
        captured = self.clock()
        self.started_epoch = min(self.started_epoch, captured)
        # A wall-clock rollback while scanning must never publish future tasks.
        if history_captured > captured:
            for source in sources:
                if source["status"] != "unavailable":
                    source.update(status="partial", error_code="clock_changed")
        history_captured = min(history_captured, captured)
        rejected = Counter(_task_source(task) for task in tasks if timestamp(task["finished_at"]).timestamp() > history_captured)
        tasks = [task for task in tasks if timestamp(task["finished_at"]).timestamp() <= history_captured]
        for source in sources:
            count = rejected[source["source_id"]]
            if count:
                source.update(status="partial", error_code="clock_changed", matched_tasks=source["matched_tasks"] - count,
                              excluded_tasks=source["excluded_tasks"] + count)
        if timestamp(activity["captured_at"]).timestamp() > captured:
            activity = {"captured_at": iso(captured), "status": "unavailable", "error_code": "clock_changed", "items": []}
        counts = Counter()
        retained = []
        for task in sorted(tasks, key=lambda task: (timestamp(task["finished_at"]), task["id"]), reverse=True):
            key = task["gpu_uuid"]
            if counts[key] < 10:
                retained.append(task); counts[key] += 1
        status = "ready" if all(source["status"] == "ready" for source in sources) else "unavailable" if all(source["status"] == "unavailable" for source in sources) else "partial"
        payload = validate_task_history({"schema_version": 1, "node_id": "local-server", "collector_id": self.collector_id,
            "started_at": iso(self.started_epoch), "captured_at": iso(captured), "coverage": "retained_verified_gpu_records",
            "history_captured_at": iso(history_captured), "status": status, "gpu_uuids": list(inventory),
            "sources": sources, "tasks": retained, "activity": activity}).model_dump()
        return self._with_last_good(payload)

    def run(self, stop: threading.Event, *, once=False):
        self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        while not stop.is_set():
            started = self.monotonic()
            payload = self.sample()
            atomic_json(self.output_dir / "gpu-tasks.json", payload)
            self._persist_last_good(payload["captured_at"])
            if once:
                return
            stop.wait(max(0.05, ACTIVITY_INTERVAL_SECONDS - (self.monotonic() - started)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not args.output_dir.is_absolute():
        parser.error("--output-dir must be absolute")
    stop = threading.Event()
    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, lambda *_: stop.set())
    TaskHistoryCollector(args.output_dir).run(stop, once=args.once)


if __name__ == "__main__":
    main()
