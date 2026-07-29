#!/usr/bin/env python3
"""Create and verify the owner-private development MD Worker process record."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import stat
import tempfile
from typing import Any


SCHEMA_VERSION = 1
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
INSTANCE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SESSION_RE = re.compile(r"^[0-9a-f]{32}$")
RECORD_KEYS = frozenset(
    {
        "schema_version",
        "pid",
        "start_ticks",
        "argv",
        "python",
        "socket",
        "source_sha",
        "source_tree",
        "worker_lock_sha256",
        "session_id",
        "worker_instance_id",
    }
)


class WorkerProcessRecordError(RuntimeError):
    """The managed process record or its live process identity is unsafe."""


def _safe_absolute(path: Path, name: str) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise WorkerProcessRecordError(f"{name} must be an absolute safe path")
    return path


def process_start_ticks(pid: int, *, proc_root: Path = Path("/proc")) -> int:
    try:
        payload = (proc_root / str(pid) / "stat").read_text(encoding="ascii")
    except OSError as exc:
        raise WorkerProcessRecordError("managed Worker process is unavailable") from exc
    close = payload.rfind(")")
    fields = payload[close + 2 :].split() if close >= 0 else []
    if len(fields) <= 19 or not fields[19].isdigit():
        raise WorkerProcessRecordError("managed Worker process start identity is invalid")
    return int(fields[19])


def process_argv(pid: int, *, proc_root: Path = Path("/proc")) -> list[str]:
    try:
        payload = (proc_root / str(pid) / "cmdline").read_bytes()
        argv = [part.decode("utf-8") for part in payload.split(b"\0") if part]
    except (OSError, UnicodeError) as exc:
        raise WorkerProcessRecordError("managed Worker command identity is unavailable") from exc
    if not argv:
        raise WorkerProcessRecordError("managed Worker command identity is empty")
    return argv


def _validate_record_shape(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != RECORD_KEYS:
        raise WorkerProcessRecordError("managed Worker process record has an invalid schema")
    pid = value.get("pid")
    start_ticks = value.get("start_ticks")
    argv = value.get("argv")
    instance = value.get("worker_instance_id")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise WorkerProcessRecordError("managed Worker process record version is unsupported")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise WorkerProcessRecordError("managed Worker PID is invalid")
    if isinstance(start_ticks, bool) or not isinstance(start_ticks, int) or start_ticks <= 0:
        raise WorkerProcessRecordError("managed Worker start ticks are invalid")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) or not item or "\0" in item for item in argv)
    ):
        raise WorkerProcessRecordError("managed Worker argv is invalid")
    for key in ("python", "socket"):
        raw = value.get(key)
        if not isinstance(raw, str):
            raise WorkerProcessRecordError(f"managed Worker {key} is invalid")
        _safe_absolute(Path(raw), f"managed Worker {key}")
    for key in ("source_sha", "source_tree"):
        raw = value.get(key)
        if not isinstance(raw, str) or SHA_RE.fullmatch(raw) is None:
            raise WorkerProcessRecordError(f"managed Worker {key} is invalid")
    digest = value.get("worker_lock_sha256")
    if not isinstance(digest, str) or DIGEST_RE.fullmatch(digest) is None:
        raise WorkerProcessRecordError("managed Worker lock digest is invalid")
    session_id = value.get("session_id")
    if not isinstance(session_id, str) or SESSION_RE.fullmatch(session_id) is None:
        raise WorkerProcessRecordError("managed Worker session identity is invalid")
    if instance is not None and (
        not isinstance(instance, str) or INSTANCE_RE.fullmatch(instance) is None
    ):
        raise WorkerProcessRecordError("managed Worker instance identity is invalid")
    return value


def load_record(path: Path) -> dict[str, Any]:
    _safe_absolute(path, "managed Worker process record")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise WorkerProcessRecordError("managed Worker process record is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > 32 * 1024
    ):
        raise WorkerProcessRecordError("managed Worker process record is unsafe")
    try:
        return _validate_record_shape(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerProcessRecordError("managed Worker process record is invalid") from exc


def _write_record(path: Path, record: dict[str, Any], *, replace: bool) -> None:
    _validate_record_shape(record)
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise WorkerProcessRecordError("managed Worker record directory is unsafe")
    if not replace and (path.exists() or path.is_symlink()):
        raise WorkerProcessRecordError("managed Worker process record already exists")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temp_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temp_path, path)
        directory_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def create_record(
    path: Path,
    *,
    pid: int,
    python: Path,
    socket: Path,
    source_sha: str,
    source_tree: str,
    worker_lock_sha256: str,
    session_id: str,
    expected_argv: list[str],
    proc_root: Path = Path("/proc"),
) -> dict[str, Any]:
    _safe_absolute(python, "managed Worker Python")
    _safe_absolute(socket, "managed Worker socket")
    live_argv = process_argv(pid, proc_root=proc_root)
    if live_argv != expected_argv:
        raise WorkerProcessRecordError("managed Worker launch command differs from the exact command")
    record = _validate_record_shape(
        {
            "schema_version": SCHEMA_VERSION,
            "pid": pid,
            "start_ticks": process_start_ticks(pid, proc_root=proc_root),
            "argv": live_argv,
            "python": str(python),
            "socket": str(socket),
            "source_sha": source_sha,
            "source_tree": source_tree,
            "worker_lock_sha256": worker_lock_sha256,
            "session_id": session_id,
            "worker_instance_id": None,
        }
    )
    _write_record(path, record, replace=False)
    return record


def verify_record(
    path: Path,
    *,
    python: Path,
    socket: Path,
    source_sha: str,
    source_tree: str,
    worker_lock_sha256: str,
    session_id: str,
    require_instance: bool = False,
    proc_root: Path = Path("/proc"),
) -> dict[str, Any]:
    record = load_record(path)
    expected = {
        "python": str(_safe_absolute(python, "managed Worker Python")),
        "socket": str(_safe_absolute(socket, "managed Worker socket")),
        "source_sha": source_sha,
        "source_tree": source_tree,
        "worker_lock_sha256": worker_lock_sha256,
        "session_id": session_id,
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise WorkerProcessRecordError("managed Worker process record differs from this source/runtime")
    pid = record["pid"]
    if process_start_ticks(pid, proc_root=proc_root) != record["start_ticks"]:
        raise WorkerProcessRecordError("managed Worker PID was reused")
    if process_argv(pid, proc_root=proc_root) != record["argv"]:
        raise WorkerProcessRecordError("managed Worker command changed")
    exact = [str(python), "-m", "uvicorn", "app.main:app", "--uds", str(socket)]
    if record["argv"] != exact or record["argv"][0] != record["python"]:
        raise WorkerProcessRecordError("managed Worker command is not the exact Uvicorn launch")
    if require_instance and record["worker_instance_id"] is None:
        raise WorkerProcessRecordError("managed Worker instance identity is not bound")
    return record


def collect_dead_record(
    path: Path,
    *,
    python: Path,
    socket: Path,
    source_sha: str,
    source_tree: str,
    worker_lock_sha256: str,
    session_id: str,
    proc_root: Path = Path("/proc"),
) -> dict[str, Any]:
    """Remove an exact managed record only after its recorded process is gone."""
    record = load_record(path)
    expected = {
        "python": str(_safe_absolute(python, "managed Worker Python")),
        "socket": str(_safe_absolute(socket, "managed Worker socket")),
        "source_sha": source_sha,
        "source_tree": source_tree,
        "worker_lock_sha256": worker_lock_sha256,
        "session_id": session_id,
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise WorkerProcessRecordError(
            "managed Worker process record differs from this source/runtime"
        )
    exact = [str(python), "-m", "uvicorn", "app.main:app", "--uds", str(socket)]
    if record["argv"] != exact or record["argv"][0] != record["python"]:
        raise WorkerProcessRecordError(
            "managed Worker command is not the exact Uvicorn launch"
        )

    process_dir = proc_root / str(record["pid"])
    if process_dir.exists():
        try:
            live_ticks = process_start_ticks(record["pid"], proc_root=proc_root)
        except WorkerProcessRecordError:
            if process_dir.exists():
                raise
        else:
            if live_ticks == record["start_ticks"]:
                try:
                    live_argv = process_argv(record["pid"], proc_root=proc_root)
                except WorkerProcessRecordError:
                    if process_dir.exists():
                        raise
                else:
                    if live_argv != record["argv"]:
                        raise WorkerProcessRecordError(
                            "managed Worker command changed while collecting its record"
                        )
                    raise WorkerProcessRecordError(
                        "managed Worker process is still running"
                    )

    if load_record(path) != record:
        raise WorkerProcessRecordError(
            "managed Worker process record changed while collecting it"
        )
    try:
        path.unlink()
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        raise WorkerProcessRecordError(
            "managed Worker dead process record could not be collected"
        ) from exc
    return record


def bind_instance(path: Path, instance_id: str, **verify_kwargs: Any) -> dict[str, Any]:
    if INSTANCE_RE.fullmatch(instance_id) is None:
        raise WorkerProcessRecordError("managed Worker instance identity is invalid")
    record = verify_record(path, **verify_kwargs)
    existing = record["worker_instance_id"]
    if existing not in (None, instance_id):
        raise WorkerProcessRecordError("managed Worker instance identity changed")
    updated = {**record, "worker_instance_id": instance_id}
    _write_record(path, updated, replace=True)
    return updated


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("create", "verify", "bind-instance", "terminate", "collect-dead"),
    )
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--worker-lock-sha256", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"))
    parser.add_argument("--pid", type=int)
    parser.add_argument("--instance-id")
    parser.add_argument("--require-instance", action="store_true")
    parser.add_argument("--expected-argv", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    common = {
        "python": args.python,
        "socket": args.socket,
        "source_sha": args.source_sha,
        "source_tree": args.source_tree,
        "worker_lock_sha256": args.worker_lock_sha256,
        "session_id": args.session_id,
        "proc_root": args.proc_root,
    }
    try:
        if args.command == "create":
            if args.pid is None or not args.expected_argv:
                raise WorkerProcessRecordError("create requires PID and exact expected argv")
            result = create_record(
                args.record,
                pid=args.pid,
                expected_argv=args.expected_argv,
                **common,
            )
        elif args.command == "verify":
            result = verify_record(
                args.record,
                require_instance=args.require_instance,
                **common,
            )
        elif args.command == "bind-instance":
            if args.instance_id is None:
                raise WorkerProcessRecordError("bind-instance requires an instance identity")
            result = bind_instance(
                args.record,
                args.instance_id,
                require_instance=False,
                **common,
            )
        elif args.command == "collect-dead":
            result = collect_dead_record(args.record, **common)
        else:
            result = verify_record(
                args.record,
                require_instance=args.require_instance,
                **common,
            )
            try:
                pid_descriptor = os.pidfd_open(result["pid"])
            except OSError as exc:
                raise WorkerProcessRecordError(
                    "managed Worker pidfd cannot be opened"
                ) from exc
            try:
                # Close the PID-reuse window between record validation and signal.
                verify_record(
                    args.record,
                    require_instance=args.require_instance,
                    **common,
                )
                signal.pidfd_send_signal(pid_descriptor, signal.SIGTERM)
            except OSError as exc:
                raise WorkerProcessRecordError(
                    "managed Worker could not be signalled through its pidfd"
                ) from exc
            finally:
                os.close(pid_descriptor)
    except WorkerProcessRecordError as exc:
        print(f"dev Worker process record error: {exc}", file=os.sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
