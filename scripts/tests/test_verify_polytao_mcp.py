from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1]
DEV_SPEC = importlib.util.spec_from_file_location("polytao_lock_dev", SCRIPTS / "polytao_mcp_dev.py")
dev = importlib.util.module_from_spec(DEV_SPEC)
DEV_SPEC.loader.exec_module(dev)
SPEC = importlib.util.spec_from_file_location("polytao_lock_verifier", SCRIPTS / "verify_polytao_mcp.py")
verifier = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, {"polytao_mcp_dev": dev}):
    SPEC.loader.exec_module(verifier)


class PolytaoAcceptanceLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root / ".runtime/polytao-mcp"
        for name, value in (("ROOT", self.root), ("RUNTIME", self.runtime)):
            replacement = patch.object(dev, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        dev.private_directory()

    def report_path(self, key):
        return self.runtime / ("acceptance-" + hashlib.sha256(key.encode()).hexdigest()[:20] + ".json")

    @contextmanager
    def rpc_fixture(self, *, on_confirm=None):
        calls = []
        mutex = threading.Lock()
        def client(*, ui=False):
            def rpc(method, params, request_id):
                if method == "initialize":
                    return {"protocolVersion": "2025-11-25"}
                name, arguments = params["name"], params["arguments"]
                with mutex:
                    calls.append(name)
                if name == "get_polytao_capabilities":
                    value = {"enabled": True, "available": True}
                elif name == "calculate_polytao_descriptors":
                    value = {"descriptors": {str(i): float(i) for i in range(15)}}
                elif name == "prepare_polytao_generation":
                    self.assertFalse(ui)
                    value = {"draft_id": "ptd_" + hashlib.sha256(arguments["conversation_id"].encode()).hexdigest()[:32],
                             "conversation_id": arguments["conversation_id"], "revision": 1,
                             "ready": True, "job_id": None}
                elif name == "confirm_polytao_generation":
                    self.assertTrue(ui)
                    if on_confirm:
                        on_confirm()
                    job = "job_" + arguments["draft_id"]
                    value = {"job_id": job, "submission": {"job_id": job, "status": "pending"}}
                else:
                    self.assertEqual(name, "get_polytao_job")
                    value = {"status": "completed", "requested_count": 3, "returned_count": 0,
                             "results": [], "next_offset": None}
                return {"structuredContent": {"ok": True, **value}}
            return rpc
        with patch.object(dev, "verify_project_ownership"), patch.object(dev, "verify_dev_target"), \
                patch.object(dev, "verify_session_file"), patch.object(dev, "rpc_client", side_effect=client), \
                patch.object(verifier, "runtime_metadata", return_value={}):
            yield calls

    def test_same_key_concurrency_fails_immediately_and_confirms_once(self):
        entered, release = threading.Event(), threading.Event()
        def confirm():
            entered.set()
            self.assertTrue(release.wait(5), "Test did not release the first confirmation")
        with self.rpc_fixture(on_confirm=confirm) as calls, ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(verifier.verify, "shared-key", polls=1)
            try:
                self.assertTrue(entered.wait(5), "First verification did not reach confirmation")
                with self.assertRaisesRegex(verifier.VerificationError, "already being verified"):
                    verifier.verify("shared-key", polls=1)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5)["status"], "passed")
            with self.assertRaisesRegex(verifier.VerificationError, "already has an acceptance report"):
                verifier.verify("shared-key", polls=1)
        self.assertEqual(calls.count("prepare_polytao_generation"), 1)
        self.assertEqual(calls.count("confirm_polytao_generation"), 1)
        self.assertTrue(json.loads(self.report_path("shared-key").read_text())["submit_attempted"])

    def test_different_keys_can_confirm_independently(self):
        barrier = threading.Barrier(2)
        with self.rpc_fixture(on_confirm=lambda: barrier.wait(timeout=5)) as calls, \
                ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(verifier.verify, "first-key", polls=1)
            second = executor.submit(verifier.verify, "second-key", polls=1)
            results = [first.result(timeout=10), second.result(timeout=10)]
        self.assertTrue(all(result["status"] == "passed" for result in results))
        self.assertNotEqual(results[0]["job_id"], results[1]["job_id"])
        self.assertEqual(calls.count("confirm_polytao_generation"), 2)

    def test_lock_is_private_retained_and_excludes_another_process(self):
        report = self.report_path("process-key")
        lock_path = report.with_name(report.name + ".lock")
        with verifier.report_lock(report.name):
            command = "import fcntl,os,sys; f=os.open(sys.argv[1],os.O_RDWR); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
            child = subprocess.run([sys.executable, "-c", command, str(lock_path)], capture_output=True, text=True)
            self.assertNotEqual(child.returncode, 0)
            self.assertIn("BlockingIOError", child.stderr)
        info = lock_path.stat()
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(info.st_uid, os.getuid())
        self.assertEqual(info.st_nlink, 1)
        with verifier.report_lock(report.name):
            self.assertEqual(lock_path.stat().st_ino, info.st_ino)

    def test_symlink_hardlink_mode_owner_and_nonregular_locks_fail_before_rpc(self):
        with self.rpc_fixture() as calls:
            for mutation in ("symlink", "hardlink", "mode", "owner", "fifo"):
                with self.subTest(mutation=mutation):
                    report = self.report_path(mutation)
                    lock_path = report.with_name(report.name + ".lock")
                    outside = self.root / (mutation + "-outside")
                    outside.write_text("preserved")
                    outside.chmod(0o600)
                    if mutation == "symlink":
                        lock_path.symlink_to(outside)
                    elif mutation == "hardlink":
                        os.link(outside, lock_path)
                    elif mutation == "fifo":
                        os.mkfifo(lock_path, 0o600)
                    else:
                        lock_path.write_text("")
                        lock_path.chmod(0o644 if mutation == "mode" else 0o600)
                    if mutation == "owner":
                        original_fstat = os.fstat
                        def foreign_owner(descriptor):
                            values = list(original_fstat(descriptor))
                            values[4] = os.getuid() + 1
                            return os.stat_result(values)
                        with patch.object(verifier.os, "fstat", side_effect=foreign_owner), \
                                self.assertRaisesRegex(verifier.VerificationError, "owner-only"):
                            verifier.verify(mutation, polls=1)
                    else:
                        with self.assertRaises((verifier.VerificationError, OSError)):
                            verifier.verify(mutation, polls=1)
                    self.assertFalse(report.exists())
                    self.assertEqual(outside.read_text(), "preserved")
        self.assertEqual(calls, [])

    def test_unknown_confirmation_releases_lock_but_retains_no_resubmit_marker(self):
        def lost_response():
            raise urllib.error.URLError("synthetic response loss")
        with self.rpc_fixture(on_confirm=lost_response) as calls:
            with self.assertRaises(urllib.error.URLError):
                verifier.verify("unknown-key", polls=1)
            with self.assertRaisesRegex(verifier.VerificationError, "already has an acceptance report"):
                verifier.verify("unknown-key", polls=1)
            result = verifier.verify("unknown-key", job_id="already-accepted-job", polls=1)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(calls.count("confirm_polytao_generation"), 1)


if __name__ == "__main__":
    unittest.main()
