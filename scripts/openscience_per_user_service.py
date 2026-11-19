"""Serve the M1 development supervisor on a private Unix socket and reap idle instances."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socketserver
import stat
import subprocess
import sys
import threading


MANAGER = Path(__file__).with_name("openscience_per_user_dev.py")
WORKSPACE_KEYS = {"operation", "environment", "user_id"}
OPERATIONS = {"ensure", "status", "stop", "touch", "begin", "end"}
MAX_REQUEST = 2048


def manager(root, *args):
    result = subprocess.run(
        [sys.executable, str(MANAGER), "--state-dir", str(root), *args],
        capture_output=True, text=True, timeout=150,
    )
    if result.returncode:
        try:
            message = json.loads(result.stderr)["error"]
        except (ValueError, KeyError, TypeError):
            message = "Workspace supervisor failed"
        raise RuntimeError(message)
    return json.loads(result.stdout)


def dispatch(root, request):
    if not isinstance(request, dict) or not all(isinstance(value, str) for value in request.values()):
        raise ValueError("Expected named string arguments")
    op = request.get("operation")
    if op not in OPERATIONS or request.keys() != (WORKSPACE_KEYS | ({"lease"} if op == "end" else set())):
        raise ValueError("Unknown workspace operation or arguments")
    args = ["recycle" if op == "stop" else op, "--environment", request["environment"], "--user-id", request["user_id"]]
    if op == "end":
        args.extend(["--lease", request["lease"]])
    return manager(root, *args)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            line = self.rfile.readline(MAX_REQUEST + 1)
            if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                raise ValueError("Expected one bounded newline-terminated request")
            reply = {"ok": True, "result": dispatch(self.server.registry, json.loads(line))}
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            reply = {"ok": False, "error": str(error)}
        try:
            self.wfile.write(json.dumps(reply).encode() + b"\n")
        except OSError:
            pass  # The client may disconnect while its idempotent ensure is in progress.


def sweep(root):
    for row in manager(root, "list")["workspaces"]:
        if row["state"] != "running":
            continue
        try:
            result = manager(root, "reap", "--environment", row["environment"], "--user-id", row["user_id"])
            if result.get("reason") == "activity_unknown":
                print(f"Workspace {row['workspace']} retained: activity unknown", file=sys.stderr, flush=True)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            print(f"Workspace {row['workspace']} retained: {error}", file=sys.stderr, flush=True)


def reaper(root, interval, stopping):
    while not stopping.wait(interval):
        try:
            sweep(root)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            print(f"Workspace sweep failed: {error}", file=sys.stderr, flush=True)


def serve(root, interval):
    # The supervisor must already own the private registry. Only one process
    # may own (and replace a stale socket at) this path.
    if root.is_symlink() or root.stat().st_uid != os.getuid() or stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise ValueError("The initialized registry must be private to this Unix user")
    manager(root, "list")
    address = root / "supervisor.sock"
    with (root / "service.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if address.is_socket():
            address.unlink()
        if address.exists() or address.is_symlink():
            raise ValueError("Unexpected file at supervisor socket path")
        with socketserver.UnixStreamServer(str(address), Handler) as server:
            address.chmod(0o600)
            server.registry = root
            server.timeout = 0.5
            stopping = threading.Event()
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, lambda *_: stopping.set())
            worker = threading.Thread(target=reaper, args=(root, interval, stopping))
            try:
                worker.start()
                print(f"M1 supervisor listening on {address}", flush=True)
                while not stopping.is_set():
                    server.handle_request()
            finally:
                stopping.set()
                worker.join()
                address.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--sweep-seconds", type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.sweep_seconds <= 3600:
        parser.error("Expected a sweep interval of 1–3600 seconds")
    os.umask(0o077)
    serve(args.state_dir, args.sweep_seconds)


if __name__ == "__main__":
    main()
