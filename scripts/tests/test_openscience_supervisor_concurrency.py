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
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import openscience_per_user_dev as manager
import openscience_per_user_service as service
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


class Started(threading.Thread):
    """Run one call in a thread and keep its result or exception."""

    def __init__(self, function, *args):
        super().__init__(daemon=True)
        self.function, self.args, self.result, self.error = function, args, None, None
        self.start()

    def run(self):
        try:
            self.result = self.function(*self.args)
        except Exception as error:  # Re-raised by the test that expects it.
            self.error = error


class ColdStartLockScopeTest(unittest.TestCase):
    """Owners share admission, but one cold start's readiness wait holds only its own workspace."""

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        registry = manager.Registry(self.root)
        registry.data = {"format": 1, "deployment": uuid4().hex, "image": "test-image", "workspaces": {},
                         "max_active": 2, "idle_seconds": 1800}
        registry.save()
        self.containers, self.created, self.removed, self.admissions = {}, [], [], []
        self.entered, self.release, self.fail_readiness = threading.Event(), threading.Event(), False
        test = self

        def resource(ws, role):
            if role == "volume":
                return {"name": ws.volume} if ws.row["initialized"] else None
            return test.containers.get((ws.row["id"], role))

        def prepare_volume(ws):
            ws.row["initialized"] = True

        def create_pair(ws):
            test.created.append(ws.user)
            for role in ("backend", "fixture"):
                test.containers[(ws.row["id"], role)] = {"state": "running", "id": ws.user + "-" + role}

        def ready(ws, fixture, backend):
            if ws.user == "test-user-a":
                test.entered.set()
                assert test.release.wait(5), "test did not release A's readiness probe"
                if test.fail_readiness:
                    ws.row["last_readiness_failure"] = {"probe_result": "URLError"}
                    raise manager.Problem("Workspace readiness failed: synthetic")

        def remove_pair(ws, **_):
            test.removed.append(ws.user)
            for role in ("backend", "fixture"):
                test.containers.pop((ws.row["id"], role), None)
            return True

        def state(ws, reason=None):
            running = (ws.row["id"], "backend") in test.containers
            return {"workspace": ws.row["id"], "generation": ws.row["generation"],
                    "state": "running" if running else "recycled"}

        def admit(registry, ws):
            test.admissions.append((ws.user, {key[0] for key in test.containers if key[1] == "backend"}))

        for name, value in (("verify_template_files", lambda ws: None), ("prepare_volume", prepare_volume),
                            ("verify_pair", lambda ws, *a, **k: None), ("resource", resource),
                            ("create_pair", create_pair), ("ready", ready), ("remove_pair", remove_pair),
                            ("state", state)):
            patcher = patch.object(manager.Workspace, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(manager.Registry, "admit", admit)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_command(self, command, user):
        return manager.execute(SimpleNamespace(state_dir=self.root, command=command,
                                               environment="dev-m1", user_id=user))

    def row(self, user):
        return manager.Registry(self.root).data["workspaces"]["dev-m1/" + user]

    def assert_lock(self, name, mode, available):
        with (self.root / name).open("a") as handle:
            try:
                fcntl.flock(handle, mode | fcntl.LOCK_NB)
            except BlockingIOError:
                self.assertFalse(available, name)
            else:
                self.assertTrue(available, name)
                fcntl.flock(handle, fcntl.LOCK_UN)

    def test_other_owner_starts_and_saves_while_a_cold_start_waits_for_readiness(self):
        a = Started(self.run_command, "ensure", "test-user-a")
        self.assertTrue(self.entered.wait(5))
        # A holds its own workspace and the shared configuration lock, not lifecycle.
        self.assert_lock("lifecycle.lock", fcntl.LOCK_EX, True)
        self.assert_lock("supervisor.lock", fcntl.LOCK_EX, False)
        with manager.workspace_lock(self.root, "dev-m1", "test-user-a", blocking=False) as owned:
            self.assertFalse(owned)
        b = Started(self.run_command, "ensure", "test-user-b")
        b.join(5)
        self.assertFalse(b.is_alive())
        self.assertIsNone(b.error)
        self.assertEqual(b.result["state"], "running")
        # B's admission charged A's created, still starting pair.
        a_id = self.row("test-user-a")["id"]
        self.assertIn(("test-user-b", {a_id}), self.admissions)
        touched = time.time()
        self.assertEqual(self.run_command("touch", "test-user-b")["state"], "running")
        # A second request for A queues behind A's start and then reuses it.
        again = Started(self.run_command, "ensure", "test-user-a")
        again.join(0.3)
        self.assertTrue(again.is_alive())
        self.assertTrue(a.is_alive())
        self.release.set()
        a.join(5)
        again.join(5)
        self.assertIsNone(a.error)
        self.assertIsNone(again.error)
        self.assertEqual((a.result["state"], a.result["generation"]), ("running", 1))
        self.assertEqual((again.result["state"], again.result["generation"]), ("running", 1))
        self.assertEqual(self.created, ["test-user-a", "test-user-b"])
        # A's result was applied to a fresh registry copy without losing B's writes.
        a_row, b_row = self.row("test-user-a"), self.row("test-user-b")
        self.assertEqual((a_row["generation"], a_row["failed_starts"], a_row["retry_after"]), (1, 0, 0))
        self.assertEqual((b_row["generation"], b_row["failed_starts"]), (1, 0))
        self.assertGreaterEqual(b_row["last_activity"], touched)

    def test_failed_readiness_is_recorded_after_relocking_and_keeps_other_owner_rows(self):
        self.fail_readiness = True
        a = Started(self.run_command, "ensure", "test-user-a")
        self.assertTrue(self.entered.wait(5))
        self.assertEqual(self.run_command("ensure", "test-user-b")["state"], "running")
        before = time.time()
        self.release.set()
        a.join(5)
        self.assertIsInstance(a.error, manager.Problem)
        self.assertIn("synthetic", str(a.error))
        self.assertEqual(self.removed, ["test-user-a"])
        a_row, b_row = self.row("test-user-a"), self.row("test-user-b")
        self.assertEqual((a_row["generation"], a_row["failed_starts"]), (1, 1))
        self.assertGreater(a_row["retry_after"], before)
        self.assertEqual(a_row["last_readiness_failure"], {"probe_result": "URLError"})
        self.assertEqual((b_row["generation"], b_row["failed_starts"]), (1, 0))
        self.assertEqual(self.run_command("status", "test-user-b")["state"], "running")

    def test_registry_writes_are_refused_while_the_lifecycle_lock_is_released(self):
        with manager.locked_registry(self.root, False, operation="ensure",
                                     workspace=("dev-m1", "test-user-a")) as registry:
            with registry.locks.lifecycle_released():
                with self.assertRaisesRegex(manager.Problem, "lifecycle lock"):
                    registry.save()
            registry.save()
        with self.assertRaisesRegex(manager.Problem, "lifecycle operations"):
            with manager.locked_registry(self.root, False, operation="set-capacity", workspace=("dev-m1", "x-y")):
                pass

    def test_reconciliation_never_reclaims_a_pair_whose_owner_holds_its_workspace(self):
        registry = manager.Registry(self.root)
        starting = registry.workspace("dev-m1", "test-user-b", create=True)
        candidate = registry.workspace("dev-m1", "test-user-a", create=True)
        self.containers[(starting.row["id"], "backend")] = {"state": "exited", "id": "b"}
        self.containers[(starting.row["id"], "fixture")] = {"state": "running", "id": "f"}
        residents = {starting.row["id"]}
        with patch.object(manager.Registry, "resident_ids", return_value=residents):
            with manager.workspace_lock(self.root, "dev-m1", "test-user-b") as owned:
                self.assertTrue(owned)
                self.assertEqual(registry.reconcile_residents(residents, candidate.row["id"]), residents)
            self.assertEqual(self.removed, [])
            registry.reconcile_residents(residents, candidate.row["id"])
        self.assertEqual(self.removed, ["test-user-b"])


class SupervisorWorkerTest(unittest.TestCase):
    def test_one_owner_cold_start_does_not_queue_another_owners_requests(self):
        started, release, calls = threading.Event(), threading.Event(), []

        def fake_manager(root, command, *args):
            user = args[args.index("--user-id") + 1]
            calls.append((command, user))
            if (command, user) == ("ensure", "test-user-b"):
                started.set()
                assert release.wait(5)
            return {"state": "running", "user": user}

        def request(operation, user):
            return service.dispatch(Path("/unused"), {"operation": operation, "environment": "dev-m1",
                                                       "user_id": user})

        with patch.object(service, "manager", fake_manager):
            cold = Started(request, "ensure", "test-user-b")
            self.assertTrue(started.wait(5))
            other = Started(request, "touch", "test-user-a")
            other.join(2)
            self.assertFalse(other.is_alive())
            self.assertEqual(other.result["user"], "test-user-a")
            same = Started(request, "status", "test-user-b")
            same.join(0.3)
            self.assertTrue(same.is_alive())
            release.set()
            for thread in (cold, same):
                thread.join(5)
                self.assertIsNone(thread.error)
        self.assertEqual(calls, [("ensure", "test-user-b"), ("touch", "test-user-a"), ("status", "test-user-b")])
        self.assertEqual(service._WORKSPACE_WORKERS, {})

