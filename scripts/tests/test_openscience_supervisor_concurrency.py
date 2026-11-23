"""Concurrency at the private supervisor socket and its cross-process locks."""

from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import openscience_per_user_dev as manager
from openscience_m5_native_storage import ssh_config_text


def rpc(address, operation, owner):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(5)
        client.connect(str(address))
        req = {"operation": operation, "environment": "dev-m1", "user_id": owner}
        client.sendall(json.dumps(req).encode() + b"\n")
        with client.makefile("rb") as reply:
            return json.loads(reply.readline())


def install_test_guard(registry):
    root = registry.root
    ssh_config = root / "ssh.conf"
    ssh_config.touch(mode=0o600)
    ssh_config.write_text(ssh_config_text(root, 39001))
    (root / "vm.json").write_text(json.dumps({"format": "nexpoly-m5-isolated-browser-v1", "ssh_port": 39001}))
    nfs = manager.pilot_module("quota_nfs")
    bindings, endpoints = {}, {}
    for letter in "ab":
        user = "test-user-" + letter
        ws = registry.workspace("dev-m1", user)
        ws.row["initialized"] = True
        slot = "src-" + letter
        key = "dev-m1/" + user
        bindings[key] = {"user_id": user, "environment": "dev-m1", "uid": ws.row["uid"],
                         "workspace": ws.row["id"], "filesystem_uuid": str(uuid4()),
                         "mount": nfs.GUEST_ROOT + "/" + slot, "generation": 0}
        endpoints[key] = {"slot": slot, "volume": "test-" + slot, "ssh_config": str(ssh_config),
                          "ssh_sha256": hashlib.sha256(ssh_config.read_bytes()).hexdigest()}
    claim = nfs.claim_record("src", *nfs.ownership(registry), *(b["filesystem_uuid"] for b in bindings.values()))
    policy = {"filesystem_uuid": str(uuid4()), "low_bytes": 100, "recover_bytes": 160}
    registry.data.update(format=5, quota_storage=bindings, nfs_endpoints=endpoints, native_claim=claim,
                         disk_watermark=policy)
    registry.save()
    return policy


class SupervisorConcurrencyTest(unittest.TestCase):
    def test_hot_write_check_completes_before_another_users_cold_probe_returns(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            registry = manager.Registry(root)
            registry.data = {"format": 1, "deployment": uuid4().hex, "image": "test-image", "workspaces": {}}
            registry.workspace("dev-m1", "test-user-a", create=True)
            cold = registry.workspace("dev-m1", "test-user-b", create=True)
            for name in manager.FIXTURE_HASHES:
                (root / name).write_bytes((manager.FIXTURE / name).read_bytes())
            # Use the normal service, manager subprocesses and CLI locks.
            # Stub Docker to hold B's start and SSH to report guest disk state.
            docker = root / "docker"
            docker.write_text("""#!/bin/sh
if [ "$1" = container ] && [ "$2" = inspect ]; then
    if [ "$5" = "$SLOW_BACKEND" ] && [ -e "$TEST_STATE/hold" ]; then
        touch "$TEST_STATE/probing"
        while [ -e "$TEST_STATE/hold" ]; do sleep 0.02; done
        echo 'end of test probe' >&2
    else
        echo 'No such container' >&2
    fi
    exit 1
fi
exit 2
""")
            docker.chmod(0o700)
            ssh = root / "ssh"
            ssh.write_text('#!/bin/sh\ncase "$*" in *water_monitor.py\\ sample*) cat "$TEST_STATE/sample";; *) exit 2;; esac\n')
            ssh.chmod(0o700)
            env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "TEST_STATE": str(root),
                   "DOCKER_HOST": "unix:///unused", "DOCKER_CONTEXT": "", "SLOW_BACKEND": cold.backend}
            address = root / "supervisor.sock"
            command = [sys.executable, str(SCRIPTS / "openscience_per_user_service.py"), "--state-dir", str(root)]
            with subprocess.Popen(command, env=env, stdout=subprocess.PIPE, text=True) as server:
                try:
                    self.assertTrue(select.select([server.stdout], [], [], 5)[0])
                    self.assertIn("listening", server.stdout.readline())
                    with manager.locked_registry(root, False) as config:
                        policy = install_test_guard(config)
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        (root / "hold").touch()
                        slow = pool.submit(rpc, address, "ensure", cold.user)
                        try:
                            deadline = time.monotonic() + 3
                            while not (root / "probing").exists() and time.monotonic() < deadline:
                                time.sleep(0.02)
                            self.assertTrue((root / "probing").exists())
                            sample = root / "sample"
                            for state in ("normal", "low", "unavailable", "normal"):
                                sample.write_text(json.dumps({**policy, "configured": True, "state": state,
                                                              "write_allowed": state == "normal"}))
                                result = rpc(address, "check-write", "test-user-a")
                                if state == "normal":
                                    self.assertEqual(result, {"ok": True, "result": {"configured": True,
                                                                                    "write_allowed": True}})
                                else:
                                    reason = "low_watermark" if state == "low" else "monitor_unavailable"
                                    self.assertEqual(result, {"ok": False, "error": "workspace_disk_" + reason})
                            self.assertFalse(rpc(address, "check-write", "unknown-user")["ok"])
                            self.assertFalse(slow.done())
                        finally:
                            (root / "hold").unlink()
                        self.assertIn("end of test probe", slow.result()["error"])
                finally:
                    server.terminate()
                    server.wait(timeout=5)

    def test_lifecycle_stays_exclusive_and_configuration_excludes_write_checks(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with manager.locked_registry(root, False, operation="ensure"):
                with (root / "lifecycle.lock").open("a") as serial:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(serial, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with (root / "supervisor.lock").open("a") as config:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(config, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with manager.locked_registry(root, False, operation="set-disk-watermark"):
                with (root / "supervisor.lock").open("a") as admission:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(admission, fcntl.LOCK_SH | fcntl.LOCK_NB)
