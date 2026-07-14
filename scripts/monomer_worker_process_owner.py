#!/usr/bin/env python3
"""Own and terminate the pidfile-managed monomer-MD Worker safely.

The fallback Worker is started in a fresh session.  A plain PID is not enough
to identify it because Linux can reuse PIDs.  This helper records immutable
process identity plus the expected listener contract, then revalidates every
field before signalling the process group.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


MANIFEST_VERSION = 1
MAX_FILE_BYTES = 64 * 1024
DEFAULT_CAPTURE_TIMEOUT_SECONDS = 40.0
TERMINATION_GRACE_SECONDS = 10.0
KILL_OBSERVE_SECONDS = 1.0
POLL_SECONDS = 0.05


class OwnerError(RuntimeError):
    """A classified failure whose text contains no process command line."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    uid: int
    start_ticks: int
    process_group_id: int
    cwd: str
    command_sha256: str
    command: tuple[str, ...]
    state: str


def _read_proc_identity(pid: int) -> ProcessIdentity:
    proc = Path("/proc") / str(pid)
    try:
        metadata = proc.stat()
        raw_stat = (proc / "stat").read_text(encoding="utf-8")
        raw_command = (proc / "cmdline").read_bytes()
        cwd = os.path.realpath(proc / "cwd")
    except FileNotFoundError:
        raise OwnerError("process_missing") from None
    except OSError:
        raise OwnerError("process_inspection_failed") from None

    closing = raw_stat.rfind(")")
    if closing < 0:
        raise OwnerError("process_identity_invalid")
    fields = raw_stat[closing + 2 :].split()
    # fields starts at proc(5) field 3: state, ppid, pgrp, ... starttime.
    if len(fields) <= 19:
        raise OwnerError("process_identity_invalid")
    try:
        process_group_id = int(fields[2])
        start_ticks = int(fields[19])
    except ValueError:
        raise OwnerError("process_identity_invalid") from None
    command = tuple(
        item.decode("utf-8", errors="surrogateescape")
        for item in raw_command.split(b"\0")
        if item
    )
    if not command:
        raise OwnerError("process_identity_invalid")
    return ProcessIdentity(
        pid=pid,
        uid=metadata.st_uid,
        start_ticks=start_ticks,
        process_group_id=process_group_id,
        cwd=cwd,
        command_sha256=hashlib.sha256(raw_command).hexdigest(),
        command=command,
        state=fields[0],
    )


def _listener_contract(args: argparse.Namespace) -> dict[str, object]:
    if args.uds:
        if not os.path.isabs(args.uds):
            raise OwnerError("listener_contract_invalid")
        return {"kind": "uds", "path": os.path.realpath(args.uds)}
    if not args.host or args.port is None:
        raise OwnerError("listener_contract_missing")
    if not 1 <= args.port <= 65535:
        raise OwnerError("listener_contract_invalid")
    return {"host": args.host, "kind": "tcp", "port": args.port}


def _command_matches_worker(
    identity: ProcessIdentity, listener: dict[str, object]
) -> bool:
    command = identity.command
    try:
        module_index = command.index("-m")
    except ValueError:
        return False
    if module_index + 1 >= len(command) or command[module_index + 1] != "uvicorn":
        return False
    if "app.main:app" not in command:
        return False
    try:
        workers_index = command.index("--workers")
    except ValueError:
        return False
    if workers_index + 1 >= len(command) or command[workers_index + 1] != "1":
        return False
    if listener["kind"] == "uds":
        try:
            index = command.index("--uds")
        except ValueError:
            return False
        return (
            index + 1 < len(command)
            and os.path.realpath(command[index + 1]) == listener["path"]
        )
    try:
        host_index = command.index("--host")
        port_index = command.index("--port")
    except ValueError:
        return False
    return (
        host_index + 1 < len(command)
        and command[host_index + 1] == listener["host"]
        and port_index + 1 < len(command)
        and command[port_index + 1] == str(listener["port"])
    )


def _expected_identity(
    pid: int, expected_cwd: str, listener: dict[str, object]
) -> ProcessIdentity:
    identity = _read_proc_identity(pid)
    if identity.uid != os.geteuid():
        raise OwnerError("process_uid_mismatch")
    if identity.process_group_id != pid:
        raise OwnerError("process_group_mismatch")
    if identity.cwd != os.path.realpath(expected_cwd):
        raise OwnerError("process_cwd_mismatch")
    if identity.state == "Z":
        raise OwnerError("process_missing")
    if not _command_matches_worker(identity, listener):
        raise OwnerError("process_command_mismatch")
    return identity


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        if temporary.exists():
            temporary.unlink()


def _secure_read(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise OwnerError("owner_file_missing") from None
    except OSError:
        raise OwnerError("owner_file_invalid") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OwnerError("owner_file_invalid")
        if metadata.st_uid != os.geteuid():
            raise OwnerError("owner_file_uid_mismatch")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise OwnerError("owner_file_mode_mismatch")
        if metadata.st_size > MAX_FILE_BYTES:
            raise OwnerError("owner_file_too_large")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(MAX_FILE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(payload) > MAX_FILE_BYTES:
        raise OwnerError("owner_file_too_large")
    return payload


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        manifest = json.loads(_secure_read(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise OwnerError("owner_manifest_invalid") from None
    required = {
        "capture_complete",
        "command_sha256",
        "cwd",
        "listener",
        "listener_identity",
        "pid",
        "process_group_id",
        "start_ticks",
        "uid",
        "version",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise OwnerError("owner_manifest_invalid")
    if manifest.get("version") != MANIFEST_VERSION:
        raise OwnerError("owner_manifest_invalid")
    if not isinstance(manifest.get("capture_complete"), bool):
        raise OwnerError("owner_manifest_invalid")
    integer_fields = ("pid", "process_group_id", "start_ticks")
    if any(
        not isinstance(manifest.get(field), int)
        or isinstance(manifest.get(field), bool)
        or int(manifest[field]) <= 0
        for field in integer_fields
    ):
        raise OwnerError("owner_manifest_invalid")
    if manifest.get("pid") != manifest.get("process_group_id"):
        raise OwnerError("owner_manifest_invalid")
    uid = manifest.get("uid")
    if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
        raise OwnerError("owner_manifest_invalid")
    if not isinstance(manifest.get("cwd"), str) or not os.path.isabs(
        str(manifest["cwd"])
    ):
        raise OwnerError("owner_manifest_invalid")
    command_hash = manifest.get("command_sha256")
    if (
        not isinstance(command_hash, str)
        or len(command_hash) != 64
        or any(character not in "0123456789abcdef" for character in command_hash)
    ):
        raise OwnerError("owner_manifest_invalid")
    listener = manifest.get("listener")
    listener_identity = manifest.get("listener_identity")
    if not isinstance(listener, dict) or listener.get("kind") not in {"tcp", "uds"}:
        raise OwnerError("owner_manifest_invalid")
    if listener.get("kind") == "uds":
        if set(listener) != {"kind", "path"} or not isinstance(
            listener.get("path"), str
        ):
            raise OwnerError("owner_manifest_invalid")
        if manifest["capture_complete"]:
            if not isinstance(listener_identity, dict) or set(listener_identity) != {
                "device",
                "inode",
                "uid",
            }:
                raise OwnerError("owner_manifest_invalid")
            if any(
                not isinstance(listener_identity.get(field), int)
                or isinstance(listener_identity.get(field), bool)
                or int(listener_identity[field]) < 0
                for field in ("device", "inode", "uid")
            ):
                raise OwnerError("owner_manifest_invalid")
        elif listener_identity is not None:
            raise OwnerError("owner_manifest_invalid")
    elif (
        set(listener) != {"host", "kind", "port"}
        or not isinstance(listener.get("host"), str)
        or not isinstance(listener.get("port"), int)
        or isinstance(listener.get("port"), bool)
        or not 1 <= int(listener["port"]) <= 65535
        or listener_identity is not None
    ):
        raise OwnerError("owner_manifest_invalid")
    return manifest


def _load_pid_file(path: Path) -> int:
    try:
        payload = _secure_read(path).decode("ascii").strip()
    except UnicodeDecodeError:
        raise OwnerError("pid_file_invalid") from None
    if not payload.isdigit() or int(payload) <= 0:
        raise OwnerError("pid_file_invalid")
    return int(payload)


def _manifest_matches_expected(
    manifest: dict[str, object], expected_cwd: str, listener: dict[str, object]
) -> None:
    if manifest.get("cwd") != os.path.realpath(expected_cwd):
        raise OwnerError("owner_cwd_mismatch")
    if manifest.get("listener") != listener:
        raise OwnerError("owner_listener_mismatch")


def _read_listener_identity(listener: dict[str, object]) -> dict[str, int] | None:
    if listener["kind"] != "uds":
        return None
    path = Path(str(listener["path"]))
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise OwnerError("worker_listener_missing") from None
    except OSError:
        raise OwnerError("worker_listener_inspection_failed") from None
    if not stat.S_ISSOCK(metadata.st_mode):
        raise OwnerError("worker_listener_invalid")
    if metadata.st_uid != os.geteuid():
        raise OwnerError("worker_listener_uid_mismatch")
    return {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "uid": metadata.st_uid,
    }


def _assert_listener_identity(
    listener: dict[str, object], captured: object
) -> None:
    if _read_listener_identity(listener) != captured:
        raise OwnerError("worker_listener_identity_changed")


def _identity_matches_manifest(
    identity: ProcessIdentity,
    manifest: dict[str, object],
    *,
    require_command_hash: bool = True,
) -> bool:
    return (
        identity.pid == manifest.get("pid")
        and identity.uid == manifest.get("uid")
        and identity.start_ticks == manifest.get("start_ticks")
        and identity.process_group_id == manifest.get("process_group_id")
        and identity.cwd == manifest.get("cwd")
        and (
            not require_command_hash
            or identity.command_sha256 == manifest.get("command_sha256")
        )
        and identity.state != "Z"
    )


def _group_has_live_members(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        entries = os.scandir("/proc")
    except OSError:
        return True
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                raw_stat = (Path("/proc") / entry.name / "stat").read_text(
                    encoding="utf-8"
                )
            except OSError:
                continue
            closing = raw_stat.rfind(")")
            fields = raw_stat[closing + 2 :].split() if closing >= 0 else []
            if len(fields) <= 2:
                continue
            try:
                member_group_id = int(fields[2])
            except ValueError:
                continue
            if member_group_id == process_group_id and fields[0] != "Z":
                return True
    return False


def _wait_for_group_exit(process_group_id: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while _group_has_live_members(process_group_id):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(POLL_SECONDS, remaining))
    return True


def _signal_if_identity_matches(
    pid: int,
    captured: ProcessIdentity,
    sig: signal.Signals,
    *,
    require_command_hash: bool,
) -> bool:
    try:
        current = _read_proc_identity(pid)
    except OwnerError as exc:
        if exc.category == "process_missing":
            return False
        raise
    matches = (
        current.uid == captured.uid
        and current.start_ticks == captured.start_ticks
        and current.process_group_id == captured.process_group_id
        and current.cwd == captured.cwd
        and current.state != "Z"
        and (
            not require_command_hash
            or current.command_sha256 == captured.command_sha256
        )
    )
    if not matches:
        raise OwnerError("process_identity_changed")
    try:
        os.killpg(captured.process_group_id, sig)
    except ProcessLookupError:
        return False
    return True


def _terminate_captured_process(
    captured: ProcessIdentity, *, require_command_hash: bool
) -> bool:
    signalled = _signal_if_identity_matches(
        captured.pid,
        captured,
        signal.SIGTERM,
        require_command_hash=require_command_hash,
    )
    if not signalled:
        if _group_has_live_members(captured.process_group_id):
            raise OwnerError("process_group_requires_manual_cleanup")
        return True
    if _wait_for_group_exit(captured.process_group_id, TERMINATION_GRACE_SECONDS):
        return True
    # Revalidate the leader immediately before escalation.  If it exited while
    # descendants remain, fail closed instead of risking a reused process group.
    signalled = _signal_if_identity_matches(
        captured.pid,
        captured,
        signal.SIGKILL,
        require_command_hash=require_command_hash,
    )
    if not signalled:
        raise OwnerError("process_group_requires_manual_cleanup")
    return _wait_for_group_exit(captured.process_group_id, KILL_OBSERVE_SECONDS)


def capture_owner(args: argparse.Namespace) -> dict[str, object]:
    if args.pid <= 0:
        raise OwnerError("pid_invalid")
    if not 0 < args.capture_timeout_seconds <= 600:
        raise OwnerError("capture_timeout_invalid")
    listener = _listener_contract(args)
    expected_cwd = os.path.realpath(args.expected_cwd)
    try:
        initial = _read_proc_identity(args.pid)
    except OwnerError:
        raise
    if (
        initial.uid != os.geteuid()
        or initial.process_group_id != args.pid
        or initial.cwd != expected_cwd
        or initial.state == "Z"
    ):
        raise OwnerError("new_process_identity_invalid")

    if args.manifest.exists() or args.manifest.is_symlink() or \
        args.pid_file.exists() or args.pid_file.is_symlink():
        try:
            _terminate_captured_process(initial, require_command_hash=False)
        except OwnerError:
            raise OwnerError("capture_cleanup_requires_manual_cleanup") from None
        raise OwnerError("owner_files_already_exist")

    provisional_manifest = {
        "capture_complete": False,
        "command_sha256": initial.command_sha256,
        "cwd": initial.cwd,
        "listener": listener,
        "listener_identity": None,
        "pid": initial.pid,
        "process_group_id": initial.process_group_id,
        "start_ticks": initial.start_ticks,
        "uid": initial.uid,
        "version": MANIFEST_VERSION,
    }
    owner_files_created = False
    try:
        _atomic_write(
            args.manifest,
            (
                json.dumps(
                    provisional_manifest, sort_keys=True, separators=(",", ":")
                )
                + "\n"
            ).encode(),
        )
        _atomic_write(args.pid_file, f"{initial.pid}\n".encode("ascii"))
        owner_files_created = True
    except Exception:
        try:
            cleaned = _terminate_captured_process(
                initial, require_command_hash=False
            )
        except OwnerError:
            if args.manifest.exists() and not args.pid_file.exists():
                try:
                    _atomic_write(
                        args.pid_file, f"{initial.pid}\n".encode("ascii")
                    )
                except Exception:
                    pass
            raise OwnerError("capture_cleanup_requires_manual_cleanup") from None
        if cleaned:
            args.pid_file.unlink(missing_ok=True)
            args.manifest.unlink(missing_ok=True)
        raise

    deadline = time.monotonic() + args.capture_timeout_seconds
    final: ProcessIdentity | None = None
    listener_identity: dict[str, int] | None = None
    try:
        while time.monotonic() <= deadline:
            current = _read_proc_identity(args.pid)
            if (
                current.uid != initial.uid
                or current.start_ticks != initial.start_ticks
                or current.process_group_id != initial.process_group_id
                or current.cwd != expected_cwd
            ):
                raise OwnerError("new_process_identity_changed")
            if _command_matches_worker(current, listener):
                try:
                    current_listener_identity = _read_listener_identity(listener)
                except OwnerError as exc:
                    if exc.category == "worker_listener_missing":
                        time.sleep(POLL_SECONDS)
                        continue
                    raise
                final = current
                listener_identity = current_listener_identity
                break
            time.sleep(POLL_SECONDS)
        if final is None:
            raise OwnerError("worker_command_capture_timeout")

        manifest = {
            "capture_complete": True,
            "command_sha256": final.command_sha256,
            "cwd": final.cwd,
            "listener": listener,
            "listener_identity": listener_identity,
            "pid": final.pid,
            "process_group_id": final.process_group_id,
            "start_ticks": final.start_ticks,
            "uid": final.uid,
            "version": MANIFEST_VERSION,
        }
        _atomic_write(
            args.manifest,
            (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        )
        return {"captured": True, "pid": final.pid}
    except Exception as original_error:
        try:
            cleaned = _terminate_captured_process(
                initial, require_command_hash=False
            )
        except OwnerError:
            raise OwnerError("capture_cleanup_requires_manual_cleanup") from None
        if not cleaned:
            raise OwnerError("capture_cleanup_requires_manual_cleanup")
        if owner_files_created:
            args.pid_file.unlink(missing_ok=True)
            args.manifest.unlink(missing_ok=True)
        raise original_error


def inspect_owner(args: argparse.Namespace) -> dict[str, object]:
    listener = _listener_contract(args)
    manifest_exists = args.manifest.exists() or args.manifest.is_symlink()
    pid_exists = args.pid_file.exists() or args.pid_file.is_symlink()
    if not manifest_exists and not pid_exists:
        return {"owned": False, "state": "absent"}
    if not manifest_exists and pid_exists:
        pid = _load_pid_file(args.pid_file)
        try:
            _read_proc_identity(pid)
        except OwnerError as exc:
            if exc.category == "process_missing":
                return {"owned": False, "state": "stale_pid"}
            raise
        raise OwnerError("legacy_live_pidfile_requires_manual_cleanup")
    if manifest_exists and not pid_exists:
        raise OwnerError("owner_pid_file_missing")

    manifest = _load_manifest(args.manifest)
    pid = _load_pid_file(args.pid_file)
    _manifest_matches_expected(manifest, args.expected_cwd, listener)
    if pid != manifest.get("pid"):
        raise OwnerError("owner_pid_mismatch")
    try:
        identity = _read_proc_identity(pid)
    except OwnerError as exc:
        if exc.category != "process_missing":
            raise
        if _group_has_live_members(int(manifest["process_group_id"])):
            raise OwnerError("process_group_requires_manual_cleanup")
        return {"owned": False, "state": "stale_owner"}
    capture_complete = bool(manifest["capture_complete"])
    if not _identity_matches_manifest(
        identity, manifest, require_command_hash=capture_complete
    ):
        raise OwnerError("owner_identity_mismatch")
    if capture_complete:
        if not _command_matches_worker(identity, listener):
            raise OwnerError("owner_command_mismatch")
        _assert_listener_identity(listener, manifest.get("listener_identity"))
    return {
        "owned": True,
        "pid": pid,
        "state": "valid" if capture_complete else "capture_incomplete",
    }


def terminate_owner(args: argparse.Namespace) -> dict[str, object]:
    inspection = inspect_owner(args)
    state = inspection["state"]
    if state == "absent":
        return {"stopped": False, "state": "absent"}
    if state == "stale_pid":
        args.pid_file.unlink()
        return {"stopped": False, "state": "stale_pid_removed"}
    if state == "stale_owner":
        args.pid_file.unlink()
        args.manifest.unlink()
        return {"stopped": False, "state": "stale_owner_removed"}

    manifest = _load_manifest(args.manifest)
    pid = _load_pid_file(args.pid_file)
    listener = _listener_contract(args)
    identity = _read_proc_identity(pid)
    capture_complete = bool(manifest["capture_complete"])
    if not _identity_matches_manifest(
        identity, manifest, require_command_hash=capture_complete
    ):
        raise OwnerError("owner_identity_mismatch")
    if capture_complete:
        if not _command_matches_worker(identity, listener):
            raise OwnerError("owner_command_mismatch")
        _assert_listener_identity(listener, manifest.get("listener_identity"))
    if not _terminate_captured_process(
        identity, require_command_hash=capture_complete
    ):
        raise OwnerError("process_group_did_not_exit")
    args.pid_file.unlink()
    args.manifest.unlink()
    return {"stopped": True, "state": "terminated"}


def _add_listener_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--uds")
    group.add_argument("--host")
    parser.add_argument("--port", type=int)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture")
    capture.add_argument("--manifest", required=True, type=Path)
    capture.add_argument("--pid-file", required=True, type=Path)
    capture.add_argument("--pid", required=True, type=int)
    capture.add_argument(
        "--capture-timeout-seconds",
        default=DEFAULT_CAPTURE_TIMEOUT_SECONDS,
        type=float,
    )
    capture.add_argument("--expected-cwd", required=True)
    _add_listener_arguments(capture)

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--manifest", required=True, type=Path)
    inspect.add_argument("--pid-file", required=True, type=Path)
    inspect.add_argument("--expected-cwd", required=True)
    _add_listener_arguments(inspect)

    terminate = subparsers.add_parser("terminate")
    terminate.add_argument("--manifest", required=True, type=Path)
    terminate.add_argument("--pid-file", required=True, type=Path)
    terminate.add_argument("--expected-cwd", required=True)
    _add_listener_arguments(terminate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "capture":
            result = capture_owner(args)
        elif args.command == "inspect":
            result = inspect_owner(args)
        else:
            result = terminate_owner(args)
    except OwnerError as exc:
        print(
            json.dumps(
                {"error_category": exc.category, "ok": False},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    except Exception:
        print('{"error_category":"owner_helper_failed","ok":false}')
        return 1
    result["ok"] = True
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
