from __future__ import annotations

import json
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from scripts.dev_gpu_operator import DevGpuOperator, prepare_runtime


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OPERATOR_SCRIPT = REPOSITORY_ROOT / "scripts" / "dev_gpu_operator.py"
SOURCE_SHA = "a" * 40
SOURCE_TREE = "b" * 40


class DevGpuOperatorTests(unittest.TestCase):
    def test_expired_queue_fails_without_starting_the_gpu_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            prepare_runtime(repository)
            operator = DevGpuOperator(
                repository=repository,
                source_sha=SOURCE_SHA,
                source_tree=SOURCE_TREE,
                python_executable=sys.executable,
            )
            operation = operator._new_operation("queued", "waiting")
            operator._write_operation(operation)
            operator._run_recovery(operation["operation_id"], time.monotonic() - 1)

            assert operator._operation is not None
            self.assertEqual(operator._operation["phase"], "failed")
            self.assertIn("30 分钟", operator._operation["message"])

    def test_double_recover_uses_one_operation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            prepare_runtime(repository)
            operator = DevGpuOperator(
                repository=repository,
                source_sha=SOURCE_SHA,
                source_tree=SOURCE_TREE,
                python_executable=sys.executable,
            )
            operator._controller_status = lambda: {  # type: ignore[method-assign]
                "schema_version": 1,
                "status": "contaminated",
                "gpu_index": 1,
            }
            operator._validate_candidate = lambda: None  # type: ignore[method-assign]
            started = threading.Event()
            release = threading.Event()

            def fake_recovery(operation_id: str, _deadline: float) -> None:
                started.set()
                release.wait(timeout=5)
                operator._finish_operation(operation_id, "ready", "ready")

            operator._run_recovery = fake_recovery  # type: ignore[method-assign]

            first = operator.recover()
            self.assertTrue(started.wait(timeout=2))
            second = operator.recover()
            release.set()
            assert operator._operation_thread is not None
            operator._operation_thread.join(timeout=2)

            self.assertEqual(first["phase"], "queued")
            self.assertEqual(second["phase"], "queued")
            self.assertEqual(first["operation_id"], second["operation_id"])

    def test_active_operation_status_does_not_spawn_controller_helper(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            prepare_runtime(repository)
            operator = DevGpuOperator(
                repository=repository,
                source_sha=SOURCE_SHA,
                source_tree=SOURCE_TREE,
                python_executable=sys.executable,
            )
            operation = operator._new_operation("starting", "starting")
            operator._write_operation(operation)
            release = threading.Event()
            operation_thread = threading.Thread(
                target=release.wait,
                kwargs={"timeout": 5},
            )
            operator._operation_thread = operation_thread
            operator._controller_status = lambda: self.fail(  # type: ignore[method-assign]
                "active status polling spawned a controller helper"
            )
            operation_thread.start()
            try:
                status = operator.status()
                duplicate = operator.recover()
            finally:
                release.set()
                operation_thread.join(timeout=2)

            self.assertEqual(status["phase"], "starting")
            self.assertEqual(status["controller_status"], "stopped")
            self.assertEqual(duplicate["operation_id"], operation["operation_id"])

    def test_private_socket_protocol_reports_status_and_shuts_down(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            scripts = repository / "scripts"
            scripts.mkdir()
            controller = scripts / "dev_gpu_session.py"
            controller.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "print(json.dumps({'schema_version': 1, 'status': 'stopped', 'gpu_index': 1}))\n",
                encoding="utf-8",
            )
            prepare_runtime(repository)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(OPERATOR_SCRIPT),
                    "serve",
                    "--repository",
                    str(repository),
                    "--source-sha",
                    SOURCE_SHA,
                    "--source-tree",
                    SOURCE_TREE,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            socket_path = repository / ".runtime" / "gpu-operator-client" / "operator.sock"
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not socket_path.exists():
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                self.assertIsNone(process.poll(), "GPU operator exited during startup")
                metadata = socket_path.lstat()
                self.assertTrue(stat.S_ISSOCK(metadata.st_mode))
                self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
                self.assertEqual(
                    stat.S_IMODE(socket_path.parent.lstat().st_mode),
                    0o700,
                )

                status_result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        str(OPERATOR_SCRIPT),
                        "request",
                        "--socket",
                        str(socket_path),
                        "--command",
                        "status",
                    ],
                    check=True,
                    text=True,
                    capture_output=True,
                )
                status = json.loads(status_result.stdout)
                self.assertEqual(status["phase"], "stopped")
                self.assertEqual(status["source_sha"], SOURCE_SHA)

                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(str(socket_path))
                    client.sendall(b'{"schema_version":1,"command":"down"}\n')
                    invalid = json.loads(client.recv(4096))
                self.assertFalse(invalid["ok"])

                subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        str(OPERATOR_SCRIPT),
                        "request",
                        "--socket",
                        str(socket_path),
                        "--command",
                        "shutdown",
                    ],
                    check=True,
                    text=True,
                    capture_output=True,
                )
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertFalse(socket_path.exists())
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

    def test_delivery_has_no_docker_socket_or_arbitrary_command_surface(self) -> None:
        source = OPERATOR_SCRIPT.read_text(encoding="utf-8")
        overlay = (
            REPOSITORY_ROOT / "docker-compose.dev-gpu-launcher.yml"
        ).read_text(encoding="utf-8")

        self.assertNotIn("/var/run/docker.sock", overlay)
        self.assertNotIn("shell=True", source)
        self.assertIn('"gpu-session-up"', source)
        self.assertEqual(source.count("QUEUE_TIMEOUT_SECONDS = 30 * 60"), 1)
        self.assertNotIn('"down"', source[source.index("def parse_args"):])
        self.assertNotIn('"--porcelain"', source)
        self.assertNotIn("未提交改动", source)


if __name__ == "__main__":
    unittest.main()
