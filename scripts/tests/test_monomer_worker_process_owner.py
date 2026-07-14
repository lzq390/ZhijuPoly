from __future__ import annotations

import importlib.util
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
OWNER_HELPER = REPO_ROOT / "scripts" / "monomer_worker_process_owner.py"
HOST = "127.0.0.1"
PORT = 18002


def _load_owner_helper_module():
    spec = importlib.util.spec_from_file_location(
        "monomer_worker_process_owner_under_test", OWNER_HELPER
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Worker owner helper")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


OWNER_MODULE = _load_owner_helper_module()


class MonomerWorkerProcessOwnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.worker_cwd = self.root / "worker"
        self.worker_cwd.mkdir()
        self.manifest = self.root / "worker.owner.json"
        self.pid_file = self.root / "worker.pid"
        self.fake_worker = self.worker_cwd / "fake_worker.py"
        self.fake_worker.write_text(
            """
import os
import subprocess
import socket
import sys
import time

child_pid_file = os.environ.get("FAKE_WORKER_CHILD_PID_FILE")
if child_pid_file:
    child_code = "import time; time.sleep(600)"
    if os.environ.get("FAKE_WORKER_CHILD_IGNORE_TERM") == "1":
        child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"
    child = subprocess.Popen(
        [sys.executable, "-c", child_code],
        close_fds=True,
    )
    with open(child_pid_file, "w", encoding="ascii") as stream:
        stream.write(f"{child.pid}\\n")
        stream.flush()
        os.fsync(stream.fileno())

uds_path = os.environ.get("FAKE_WORKER_UDS_PATH")
if uds_path:
    time.sleep(float(os.environ.get("FAKE_WORKER_UDS_DELAY", "0")))
    try:
        os.unlink(uds_path)
    except FileNotFoundError:
        pass
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(uds_path)
    listener.listen(1)

leader_exit_delay = os.environ.get("FAKE_WORKER_LEADER_EXIT_DELAY")
if leader_exit_delay:
    time.sleep(float(leader_exit_delay))
    raise SystemExit(0)

while True:
    time.sleep(60)
""".lstrip(),
            encoding="utf-8",
        )
        self.processes: list[subprocess.Popen[bytes]] = []

    def tearDown(self) -> None:
        for process in reversed(self.processes):
            if process.poll() is None:
                try:
                    if os.getpgid(process.pid) == process.pid:
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self.temporary.cleanup()

    def spawn_worker(
        self,
        *,
        with_child: bool = False,
        child_ignores_term: bool = False,
        uds: Path | None = None,
        uds_delay: float = 0,
        create_uds: bool = True,
        leader_exit_delay: float | None = None,
    ) -> tuple[subprocess.Popen[bytes], Path | None]:
        environment = os.environ.copy()
        child_pid_file = self.root / f"child-{len(self.processes)}.pid"
        if with_child:
            environment["FAKE_WORKER_CHILD_PID_FILE"] = str(child_pid_file)
        if child_ignores_term:
            environment["FAKE_WORKER_CHILD_IGNORE_TERM"] = "1"
        if uds is not None and create_uds:
            environment["FAKE_WORKER_UDS_PATH"] = str(uds)
            environment["FAKE_WORKER_UDS_DELAY"] = str(uds_delay)
        if leader_exit_delay is not None:
            environment["FAKE_WORKER_LEADER_EXIT_DELAY"] = str(leader_exit_delay)
        listener_arguments = (
            ["--uds", str(uds)]
            if uds is not None
            else ["--host", HOST, "--port", str(PORT)]
        )
        process = subprocess.Popen(
            [
                sys.executable,
                str(self.fake_worker),
                "-m",
                "uvicorn",
                "app.main:app",
                "--workers",
                "1",
                *listener_arguments,
            ],
            cwd=self.worker_cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.processes.append(process)
        return process, child_pid_file if with_child else None

    def helper(
        self,
        command: str,
        *,
        manifest: Path | None = None,
        pid_file: Path | None = None,
        pid: int | None = None,
        uds: Path | None = None,
        capture_timeout: float | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        arguments = [
            sys.executable,
            str(OWNER_HELPER),
            command,
            "--manifest",
            str(manifest or self.manifest),
            "--pid-file",
            str(pid_file or self.pid_file),
        ]
        if pid is not None:
            arguments.extend(["--pid", str(pid)])
        if capture_timeout is not None:
            arguments.extend(
                ["--capture-timeout-seconds", str(capture_timeout)]
            )
        arguments.extend(
            [
                "--expected-cwd",
                str(self.worker_cwd),
            ]
        )
        if uds is not None:
            arguments.extend(["--uds", str(uds)])
        else:
            arguments.extend(["--host", HOST, "--port", str(PORT)])
        completed = subprocess.run(
            arguments,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        self.assertTrue(completed.stdout.strip(), completed.stderr)
        return completed, json.loads(completed.stdout)

    def capture(self, worker: subprocess.Popen[bytes]) -> dict[str, object]:
        completed, payload = self.helper("capture", pid=worker.pid)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(payload, {"captured": True, "ok": True, "pid": worker.pid})
        self.assertEqual(stat.S_IMODE(self.manifest.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.pid_file.stat().st_mode), 0o600)
        return payload

    def terminate(self) -> dict[str, object]:
        completed, payload = self.helper("terminate")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return payload

    def assert_live(self, process: subprocess.Popen[bytes]) -> None:
        self.assertIsNone(process.poll(), f"process {process.pid} was unexpectedly killed")
        os.kill(process.pid, 0)

    def assert_pid_not_live(self, pid: int) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                raw_stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
            except FileNotFoundError:
                return
            closing = raw_stat.rfind(")")
            fields = raw_stat[closing + 2 :].split() if closing >= 0 else []
            if fields and fields[0] == "Z":
                return
            time.sleep(0.05)
        self.fail(f"process {pid} remained live")

    def test_capture_and_terminate_kills_entire_disposable_process_group(self) -> None:
        worker, child_pid_file = self.spawn_worker(with_child=True)
        assert child_pid_file is not None
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(child_pid_file.exists(), "fake Worker did not create its child")
        child_pid = int(child_pid_file.read_text(encoding="ascii"))
        self.assertEqual(os.getpgid(worker.pid), worker.pid)
        self.assertEqual(os.getpgid(child_pid), worker.pid)

        self.capture(worker)
        result = self.terminate()

        self.assertEqual(result, {"ok": True, "state": "terminated", "stopped": True})
        worker.wait(timeout=5)
        self.assert_pid_not_live(child_pid)
        self.assertFalse(self.manifest.exists())
        self.assertFalse(self.pid_file.exists())

    def test_changed_start_ticks_fails_closed_without_killing_worker(self) -> None:
        worker, _ = self.spawn_worker()
        self.capture(worker)
        original = json.loads(self.manifest.read_text(encoding="utf-8"))
        changed = dict(original)
        changed["start_ticks"] = int(changed["start_ticks"]) + 1
        self.manifest.write_text(
            json.dumps(changed, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        self.manifest.chmod(0o600)

        completed, payload = self.helper("terminate")

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(payload["error_category"], "owner_identity_mismatch")
        self.assert_live(worker)

        self.manifest.write_text(
            json.dumps(original, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        self.manifest.chmod(0o600)
        self.terminate()

    def test_unrelated_pid_file_fails_closed_without_killing_either_process(self) -> None:
        worker, _ = self.spawn_worker()
        unrelated, _ = self.spawn_worker()
        self.capture(worker)
        self.pid_file.write_text(f"{unrelated.pid}\n", encoding="ascii")
        self.pid_file.chmod(0o600)

        completed, payload = self.helper("terminate")

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(payload["error_category"], "owner_pid_mismatch")
        self.assert_live(worker)
        self.assert_live(unrelated)

        self.pid_file.write_text(f"{worker.pid}\n", encoding="ascii")
        self.pid_file.chmod(0o600)
        self.terminate()

    def test_legacy_live_pid_file_fails_closed_without_killing_process(self) -> None:
        worker, _ = self.spawn_worker()
        self.pid_file.write_text(f"{worker.pid}\n", encoding="ascii")
        self.pid_file.chmod(0o600)

        inspected, inspect_payload = self.helper("inspect")
        completed, payload = self.helper("terminate")

        self.assertNotEqual(inspected.returncode, 0)
        self.assertEqual(
            inspect_payload["error_category"],
            "legacy_live_pidfile_requires_manual_cleanup",
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            payload["error_category"], "legacy_live_pidfile_requires_manual_cleanup"
        )
        self.assert_live(worker)
        self.assertTrue(self.pid_file.exists())

    def test_missing_leader_with_live_group_keeps_owner_files(self) -> None:
        worker, child_pid_file = self.spawn_worker(
            with_child=True, child_ignores_term=True
        )
        assert child_pid_file is not None
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        child_pid = int(child_pid_file.read_text(encoding="ascii"))
        self.capture(worker)
        os.kill(worker.pid, signal.SIGKILL)
        worker.wait(timeout=5)

        completed, payload = self.helper("terminate")

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            payload["error_category"], "process_group_requires_manual_cleanup"
        )
        self.assertTrue(self.manifest.exists())
        self.assertTrue(self.pid_file.exists())
        os.killpg(worker.pid, signal.SIGKILL)
        self.assert_pid_not_live(child_pid)

    def test_uds_capture_waits_for_listener_and_binds_inode(self) -> None:
        uds = self.root / "worker.sock"
        worker, _ = self.spawn_worker(uds=uds, uds_delay=0.3)
        started = time.monotonic()

        completed, payload = self.helper(
            "capture", pid=worker.pid, uds=uds, capture_timeout=2
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertTrue(manifest["capture_complete"])
        self.assertEqual(manifest["listener_identity"]["inode"], uds.lstat().st_ino)
        terminated, result = self.helper("terminate", uds=uds)
        self.assertEqual(terminated.returncode, 0, terminated.stdout + terminated.stderr)
        self.assertTrue(result["stopped"])

    def test_capture_failure_retains_provisional_owner_for_live_descendant(self) -> None:
        uds = self.root / "never-created.sock"
        worker, child_pid_file = self.spawn_worker(
            with_child=True,
            child_ignores_term=True,
            uds=uds,
            create_uds=False,
            leader_exit_delay=0.3,
        )
        assert child_pid_file is not None
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        child_pid = int(child_pid_file.read_text(encoding="ascii"))

        completed, payload = self.helper(
            "capture", pid=worker.pid, uds=uds, capture_timeout=2
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            payload["error_category"], "capture_cleanup_requires_manual_cleanup"
        )
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertFalse(manifest["capture_complete"])
        self.assertTrue(self.pid_file.exists())
        os.killpg(worker.pid, signal.SIGKILL)
        self.assert_pid_not_live(child_pid)
        stale, result = self.helper("terminate", uds=uds)
        self.assertEqual(stale.returncode, 0, stale.stdout + stale.stderr)
        self.assertEqual(result["state"], "stale_owner_removed")

    def test_stale_legacy_pid_file_is_removed_without_signalling(self) -> None:
        stale_pid = 2_147_483_647
        self.assertFalse((Path("/proc") / str(stale_pid)).exists())
        self.pid_file.write_text(f"{stale_pid}\n", encoding="ascii")
        self.pid_file.chmod(0o600)

        result = self.terminate()

        self.assertEqual(
            result, {"ok": True, "state": "stale_pid_removed", "stopped": False}
        )
        self.assertFalse(self.pid_file.exists())

    def test_manifest_mode_mismatch_fails_closed(self) -> None:
        worker, _ = self.spawn_worker()
        self.capture(worker)
        self.manifest.chmod(0o640)

        completed, payload = self.helper("terminate")

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(payload["error_category"], "owner_file_mode_mismatch")
        self.assert_live(worker)

        self.manifest.chmod(0o600)
        self.terminate()

    def test_manifest_symlink_fails_closed(self) -> None:
        worker, _ = self.spawn_worker()
        self.capture(worker)
        real_manifest = self.root / "real-owner.json"
        self.manifest.rename(real_manifest)
        self.manifest.symlink_to(real_manifest)

        completed, payload = self.helper("terminate")

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(payload["error_category"], "owner_file_invalid")
        self.assert_live(worker)

        self.manifest.unlink()
        real_manifest.rename(self.manifest)
        self.terminate()

    def test_owner_uid_mismatch_is_rejected_before_payload_is_used(self) -> None:
        owner_file = self.root / "wrong-owner.json"
        owner_file.write_text("{}\n", encoding="utf-8")
        owner_file.chmod(0o600)
        real_fstat = OWNER_MODULE.os.fstat

        def fstat_with_other_owner(descriptor: int):
            metadata = real_fstat(descriptor)
            fields = list(metadata)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)

        with mock.patch.object(OWNER_MODULE.os, "fstat", side_effect=fstat_with_other_owner):
            with self.assertRaises(OWNER_MODULE.OwnerError) as raised:
                OWNER_MODULE._secure_read(owner_file)

        self.assertEqual(raised.exception.category, "owner_file_uid_mismatch")


if __name__ == "__main__":
    unittest.main()
