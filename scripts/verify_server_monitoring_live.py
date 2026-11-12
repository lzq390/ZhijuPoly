#!/usr/bin/env python3
"""Verify a running monitor using an existing real login; never create fake sessions."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import math
import json
import os
import stat
from pathlib import Path
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from uuid import UUID


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def epoch(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Observation timestamps require a timezone")
    return parsed.timestamp()


def private_read(path: Path, maximum: int = 8 * 1024 * 1024) -> bytes:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > maximum):
        raise ValueError("Evidence must be a bounded private regular file owned by this user")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("Evidence file changed while opening")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(maximum + 1)
        after = os.fstat(descriptor)
        if (len(raw) > maximum or len(raw) != opened.st_size
                or (opened.st_mtime_ns, opened.st_ctime_ns, opened.st_size)
                != (after.st_mtime_ns, after.st_ctime_ns, after.st_size)):
            raise ValueError("Evidence exceeds size limit or changed while reading")
        return raw
    finally:
        os.close(descriptor)


def atomic_private_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def hardware_identity(snapshot: dict) -> dict:
    return {"collector_id": snapshot["collector_id"], "collector_started_at": snapshot["started_at"],
            "logical_cpu_count": snapshot["logical_cpu_count"], "memory_total_bytes": snapshot["memory"]["total_bytes"],
            "gpu_uuids": sorted(gpu["uuid"] for gpu in snapshot["gpus"])}


def read_events(path: Path) -> tuple[list[dict], str]:
    raw = private_read(path)
    if not raw or not raw.endswith(b"\n"):
        raise ValueError("Cannot resume an empty or partially written observation log")
    events = [json.loads(line) for line in raw.splitlines()]
    previous_time, previous_sequence = float("-inf"), 0
    for event in events:
        checked = epoch(event["checked_at"])
        if checked <= previous_time or event["status"] not in {"passed", "failed"}:
            raise ValueError("Existing observation timestamps or statuses are invalid")
        previous_time = checked
        if event["status"] == "passed":
            if (type(event["sequence"]) is not int or event["sequence"] <= previous_sequence
                    or not 0 <= event["age_seconds"] <= 15
                    or not 0 <= checked - epoch(event["captured_at"]) <= 20):
                raise ValueError("Existing passed observations do not contain fresh advancing samples")
            previous_sequence = event["sequence"]
    return events, hashlib.sha256(raw).hexdigest()


def observer_gaps(events: list[dict], interval: float) -> list[dict]:
    return [{"after_checked_at": left["checked_at"], "before_checked_at": right["checked_at"],
             "seconds_between_checks": round(epoch(right["checked_at"]) - epoch(left["checked_at"]), 3)}
            for left, right in zip(events, events[1:])
            if epoch(right["checked_at"]) - epoch(left["checked_at"]) > interval + 10]


def prepare_resume(args, snapshot: dict, user: str) -> tuple[list[dict], dict]:
    events, digest = read_events(args.output_dir / "samples.jsonl")
    if (args.output_dir / "result.json").exists():
        raise ValueError("A completed verification result cannot be resumed or overwritten")
    identity = hardware_identity(snapshot)
    passed = [event for event in events if event["status"] == "passed"]
    if not passed or passed[-1]["sequence"] >= snapshot["sequence"]:
        raise ValueError("The collector must advance beyond the existing observations")
    metadata_path = args.output_dir / "run.json"
    if metadata_path.exists():
        metadata = json.loads(private_read(metadata_path, 128 * 1024))
        if (metadata["output_dir"] != str(args.output_dir.resolve()) or metadata["identity"] != identity
                or metadata["base_url"] != args.base_url or metadata["user"] != user
                or metadata["interval_seconds"] != args.interval_seconds
                or metadata["duration_required_seconds"] != args.duration_seconds
                or metadata["started_at"] != events[0]["checked_at"]):
            raise ValueError("Resume must retain the exact output directory, collector, account and timing settings")
        if any(event.get("collector_id", identity["collector_id"]) != identity["collector_id"] for event in passed):
            raise ValueError("Existing samples identify another collector")
    else:
        # The original verifier did not persist identity/interval metadata. Its
        # migration requires an explicit trusted collector ID and verifies the
        # recorded five-second sequence timeline; it does not invent old IDs.
        expected = getattr(args, "expected_collector_id", None)
        if expected is None or str(UUID(expected)) != expected or expected != identity["collector_id"]:
            raise ValueError("Legacy resume requires --expected-collector-id with the current complete UUID")
        started = epoch(snapshot["started_at"])
        for event in passed:
            offset = epoch(event["captured_at"]) - started - (event["sequence"] - 1) * 5
            if not 0 <= offset <= 10 or epoch(event["captured_at"]) > epoch(snapshot["captured_at"]):
                raise ValueError("Legacy sample timestamps and sequence do not belong to the collector timeline")
        adjacent = [epoch(right["checked_at"]) - epoch(left["checked_at"]) for left, right in zip(events, events[1:])]
        if not adjacent or min(adjacent) < args.interval_seconds - 1 or not any(
                args.interval_seconds - 1 <= gap <= args.interval_seconds + 10 for gap in adjacent):
            raise ValueError("Legacy observation cadence does not match the declared polling interval")
        metadata = {"schema_version": 1, "output_dir": str(args.output_dir.resolve()),
                    "identity": identity, "base_url": args.base_url, "user": user,
                    "started_at": events[0]["checked_at"], "interval_seconds": args.interval_seconds,
                    "duration_required_seconds": args.duration_seconds,
                    "legacy_evidence": {"sha256": digest, "samples": len(events),
                        "binding": "explicit collector UUID and recorded sequence/capture timeline",
                        "old_samples_contain_collector_id": False}}
    if epoch(events[-1]["checked_at"]) >= time.time():
        raise ValueError("Cannot resume observations dated in the future")
    metadata.setdefault("resumes", []).append({"resumed_at": stamp(), "previous_checked_at": events[-1]["checked_at"],
                                                "prior_samples": len(events), "prior_log_sha256": digest})
    return events, metadata


def verify_history(history: dict, identity: dict, *, full_hour: bool) -> None:
    if (history["collector_id"] != identity["collector_id"]
            or history["started_at"] != identity["collector_started_at"]
            or history["freshness"] != "fresh" or history["interval_seconds"] != 30):
        raise ValueError("History identity, freshness or interval changed")
    points = history["points"]
    if len(points) > 120 or (full_hour and len(points) != 120):
        raise ValueError("One-hour history requires exactly 120 points")
    times = [epoch(point["timestamp"]) for point in points]
    if any(abs(right - left - 30) > 0.001 for left, right in zip(times, times[1:])):
        raise ValueError("Collector history has missing or irregular intervals")
    captured = epoch(history["captured_at"])
    if points and not 0 <= captured - times[-1] <= 35:
        raise ValueError("History does not reach the latest sampling window")
    if full_hour and not 3570 <= captured - times[0] <= 3605:
        raise ValueError("History does not cover the preceding hour")
    for point in points:
        if point["cpu_percent"] is None or point["memory_percent"] is None:
            raise ValueError("CPU or memory history contains an unavailable interval")
        if sorted(gpu["uuid"] for gpu in point["gpus"]) != identity["gpu_uuids"]:
            raise ValueError("GPU history has a missing or changed device")
        if any(gpu["utilization_percent"] is None or gpu["memory_used_bytes"] is None for gpu in point["gpus"]):
            raise ValueError("GPU history contains an unavailable interval")


def run(args) -> int:
    url = urlsplit(args.base_url)
    if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost"} or url.port not in {5319, 9003}
            or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment):
        raise ValueError("Live verification is restricted to local monitoring endpoints")
    private = json.loads(private_read(args.session_file, 128 * 1024))
    session = private["session"]
    if session.get("authenticated") is not True or session["user"]["username"] not in {"zhiju000", "zhiju001", "zhiju002"}:
        raise ValueError("An approved real development login is required")
    cookie = next(item["value"] for item in private["cookies"] if item["name"] == "nexpoly_dev_session")
    headers = {"Cookie": "nexpoly_dev_session=" + cookie, "X-Session-Context": session["session_id"]}
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def get(endpoint):
        request = urllib.request.Request(args.base_url.rstrip("/") + "/api/v1/server-monitoring/" + endpoint, headers=headers)
        try:
            with opener.open(request, timeout=8) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError("Oversized monitoring response")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{endpoint}: HTTP {exc.code}") from None

    if get("access").get("can_read") is not True:
        raise ValueError("Live monitoring access was not granted")
    if args.output_dir.is_symlink():
        raise ValueError("The observation directory cannot be a symbolic link")
    if not getattr(args, "resume", False):
        args.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = args.output_dir.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Resume requires the existing private output directory")
    lock = os.open(args.output_dir / ".verification.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return observe(args, get, session["user"]["username"])
    finally:
        os.close(lock)


def observe(args, get, user: str) -> int:
    samples = args.output_dir / "samples.jsonl"
    initial = get("snapshot")
    identity = hardware_identity(initial)
    if getattr(args, "resume", False):
        events, metadata = prepare_resume(args, initial, user)
        mode = os.O_APPEND
    else:
        if samples.exists() or (args.output_dir / "run.json").exists() or (args.output_dir / "result.json").exists():
            raise ValueError("Existing evidence will not be truncated; use --resume")
        events = []
        metadata = {"schema_version": 1, "output_dir": str(args.output_dir.resolve()), "identity": identity,
                    "base_url": args.base_url, "user": user, "started_at": None,
                    "interval_seconds": args.interval_seconds, "duration_required_seconds": args.duration_seconds}
        mode = os.O_EXCL
    descriptor = os.open(samples, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | mode, 0o600)
    if events:
        atomic_private_json(args.output_dir / "run.json", metadata)
    last_sequence = max((event.get("sequence", 0) for event in events), default=0)
    next_history, last_history = 0.0, None
    with os.fdopen(descriptor, "a", buffering=1) as stream:
        while True:
            iteration = time.monotonic()
            try:
                snapshot = initial if initial is not None else get("snapshot")
                initial = None
                if hardware_identity(snapshot) != identity:
                    raise ValueError("Collector or hardware identity changed")
                if snapshot["sequence"] <= last_sequence:
                    raise ValueError("Sampling did not advance")
                if snapshot["freshness"] != "fresh" or not all(value["status"] == "ok" for value in snapshot["components"].values()):
                    raise ValueError("Snapshot is stale or has a partial metric failure")
                last_sequence = snapshot["sequence"]
                if iteration >= next_history:
                    last_history = get("history")
                    verify_history(last_history, identity, full_hour=False)
                    next_history = iteration + 30
                event = {"checked_at": stamp(), "captured_at": snapshot["captured_at"],
                         "collector_id": identity["collector_id"], "sequence": last_sequence,
                         "age_seconds": snapshot["age_seconds"], "history_points": len((last_history or {}).get("points", [])),
                         "status": "passed"}
            except Exception as exc:
                event = {"checked_at": stamp(), "status": "failed", "error": str(exc)}
            events.append(event)
            stream.write(json.dumps(event) + "\n")
            os.fsync(stream.fileno())
            if metadata["started_at"] is None:
                metadata["started_at"] = event["checked_at"]
                atomic_private_json(args.output_dir / "run.json", metadata)
            if epoch(event["checked_at"]) - epoch(metadata["started_at"]) >= args.duration_seconds:
                break
            time.sleep(args.interval_seconds)
    errors = [event for event in events if event["status"] == "failed"]
    try:
        last_history = get("history")
        verify_history(last_history, identity, full_hour=args.duration_seconds >= 3600)
        atomic_private_json(args.output_dir / "final-history.json", last_history)
    except Exception as exc:
        errors.append({"error": str(exc)})
    gaps = observer_gaps(events, args.interval_seconds)
    report = {"started_at": metadata["started_at"], "completed_at": stamp(), "user": user,
              "duration_required_seconds": args.duration_seconds, "interval_seconds": args.interval_seconds,
              "elapsed_seconds": round(epoch(events[-1]["checked_at"]) - epoch(metadata["started_at"]), 3),
              **identity, "gpu_count": len(identity["gpu_uuids"]),
              "checks": sum(event["status"] == "passed" for event in events), "errors": errors,
              "max_age_seconds": max((event.get("age_seconds", 0) for event in events), default=0),
              "history_points": len((last_history or {}).get("points", [])),
              "observer_continuity": "interrupted" if gaps else "continuous", "observer_gaps": gaps,
              "collection_history_continuity": "failed" if errors else "passed",
              "status": "failed" if errors else "passed_with_observer_gap" if gaps else "passed"}
    atomic_private_json(args.output_dir / "result.json", report)
    print(json.dumps(report))
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:9003")
    parser.add_argument("--session-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=float, default=3600)
    parser.add_argument("--interval-seconds", type=float, default=15)
    parser.add_argument("--resume", action="store_true", help="Append to a private unfinished observation run; retain its real first timestamp")
    parser.add_argument("--expected-collector-id", help="Explicit trusted collector UUID required to migrate logs created before resume metadata existed")
    args = parser.parse_args()
    if not math.isfinite(args.duration_seconds) or args.duration_seconds < 0 or not 5 <= args.interval_seconds <= 30:
        parser.error("duration must be nonnegative and polling interval must be 5–30 seconds")
    if args.expected_collector_id and not args.resume:
        parser.error("--expected-collector-id requires --resume")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
