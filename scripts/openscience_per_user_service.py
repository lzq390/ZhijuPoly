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
from openscience_per_user_dev import DEFAULT_READY_SECONDS, Registry, pilot_module
from openscience_m5_offline_file import web_cleanup


MANAGER = Path(__file__).with_name("openscience_per_user_dev.py")
WORKSPACE_KEYS = {"operation", "environment", "user_id"}
OPERATIONS = {"ensure", "status", "stop", "touch", "begin", "end", "check-write"}
MAX_REQUEST = 8192
# Life-cycle work is serialized in the registry; disk samples are serialized in
# the guest. Queue peers before forking, with a separate worker for each.
LIFECYCLE_WORKER = threading.Lock()
WRITE_CHECK_WORKER = threading.Lock()
CLEANUP_WORKERS = threading.BoundedSemaphore(2)


def ensure_timeout_seconds(root):
    # Admission, Docker setup and checked teardown share the original 120s allowance.
    return 150 + Registry(root).readiness_seconds() - DEFAULT_READY_SECONDS


def manager(root, *args):
    timeout = 150
    if args[0] == "ensure":
        # Leave the same budget for admission, Docker creation and checked teardown as a normal
        # start, in addition to the guest's explicitly configured readiness allowance.
        timeout = ensure_timeout_seconds(root)
    result = subprocess.run(
        [sys.executable, str(MANAGER), "--state-dir", str(root), *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode:
        try:
            message = json.loads(result.stderr)["error"]
        except (ValueError, KeyError, TypeError):
            message = "Workspace supervisor failed"
        raise RuntimeError(message)
    return json.loads(result.stdout)


def dispatch(root, request):
    if isinstance(request, dict) and request.get("operation") == "cleanup":
        if (request.keys() != WORKSPACE_KEYS | {"action", "data", "until_ms"}
                or request["environment"] not in ("dev-m1", "deploy-m6")):
            raise ValueError("cleanup_invalid")
        if not CLEANUP_WORKERS.acquire(blocking=False):
            raise ValueError("cleanup_busy")
        try:
            return web_cleanup(root, request["user_id"], request["action"], request["data"], request["until_ms"],
                               environment=request["environment"])
        finally:
            CLEANUP_WORKERS.release()
    if not isinstance(request, dict) or not all(isinstance(value, str) for value in request.values()):
        raise ValueError("Expected named string arguments")
    op = request.get("operation")
    if op not in OPERATIONS or request.keys() != (WORKSPACE_KEYS | ({"lease"} if op == "end" else set())):
        raise ValueError("Unknown workspace operation or arguments")
    args = ["recycle" if op == "stop" else op, "--environment", request["environment"], "--user-id", request["user_id"]]
    if op == "end":
        args.extend(["--lease", request["lease"]])
    with (WRITE_CHECK_WORKER if op == "check-write" else LIFECYCLE_WORKER):
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
        # A slow ensure must not hold the socket accept loop while another
        # running workspace samples the disk guard before a write.
        with socketserver.ThreadingUnixStreamServer(str(address), Handler) as server:
            address.chmod(0o600)
            server.registry = root
            server.timeout = 0.5
            stopping = threading.Event()
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, lambda *_: stopping.set())
            workers = [threading.Thread(target=reaper, args=(root, interval, stopping))]
            if Registry(root).data.get("candidate_owners"):
                host = pilot_module("candidate_water")
                workers.extend([threading.Thread(target=host.poll, args=(root, stopping)),
                                threading.Thread(target=host.water_monitor.water_alerts.serve,
                                                 args=(root / "host-watermark", host.water_monitor.HISTORY,
                                                       host.water_monitor.CADENCE_SECONDS), kwargs={"stop": stopping})])
            try:
                for worker in workers:
                    worker.start()
                print(f"M1 supervisor listening on {address}", flush=True)
                while not stopping.is_set():
                    server.handle_request()
            finally:
                stopping.set()
                for worker in workers:
                    if worker.ident is not None:
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
