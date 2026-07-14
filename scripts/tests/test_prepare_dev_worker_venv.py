from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import prepare_dev_worker_venv  # noqa: E402
import release_controller  # noqa: E402


class PrepareDevWorkerVenvTests(unittest.TestCase):
    def _write_wheel(self, wheelhouse: Path) -> tuple[Path, str]:
        wheelhouse.mkdir(parents=True)
        wheel = wheelhouse / "fixture_pkg-1.0-py3-none-any.whl"
        distribution = "fixture_pkg-1.0.dist-info"
        records: dict[str, bytes] = {
            "fixture_pkg/__init__.py": b"VALUE = 'isolated'\n",
            f"{distribution}/METADATA": (
                b"Metadata-Version: 2.1\nName: fixture-pkg\nVersion: 1.0\n"
            ),
            f"{distribution}/WHEEL": (
                b"Wheel-Version: 1.0\nGenerator: nexpoly-test\n"
                b"Root-Is-Purelib: true\nTag: py3-none-any\n"
            ),
        }
        record_lines: list[str] = []
        for name, content in records.items():
            encoded = (
                base64.urlsafe_b64encode(hashlib.sha256(content).digest())
                .rstrip(b"=")
                .decode()
            )
            record_lines.append(f"{name},sha256={encoded},{len(content)}")
        record_name = f"{distribution}/RECORD"
        record_lines.append(f"{record_name},,")
        records[record_name] = ("\n".join(record_lines) + "\n").encode()
        with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in records.items():
                archive.writestr(name, content)
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        return wheel, digest

    def _repository_fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        repository = root / "repo"
        lock = repository / prepare_dev_worker_venv.LOCK_RELATIVE_PATH
        lock.parent.mkdir(parents=True)
        wheelhouse = root / "wheelhouse"
        _, digest = self._write_wheel(wheelhouse)
        lock.write_text(
            "fixture-pkg==1.0 \\\n"
            f"    --hash=sha256:{digest}\n",
            encoding="utf-8",
        )
        target = repository / prepare_dev_worker_venv.VENV_NAME
        return repository, target, lock, wheelhouse

    def test_prepare_and_verify_install_locked_distribution_locally(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository, target, lock, wheelhouse = self._repository_fixture(root)
            identity = release_controller.inspect_worker_base_python(sys.executable, None)
            prepared = prepare_dev_worker_venv.prepare_venv(
                repository,
                target,
                lock,
                root / "worker.pid",
                root / "worker.sock",
                sys.executable,
                identity["identity_sha256"],
                wheelhouse,
            )
            verified = prepare_dev_worker_venv.verify_venv(
                repository,
                target,
                lock,
                sys.executable,
                identity["identity_sha256"],
            )
            imported = subprocess.run(
                [
                    str(target / "bin/python"),
                    "-I",
                    "-c",
                    "import fixture_pkg; print(fixture_pkg.VALUE)",
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
            lock.write_text(lock.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
            with self.assertRaisesRegex(
                prepare_dev_worker_venv.DevWorkerVenvError,
                "lock digest has drifted",
            ):
                prepare_dev_worker_venv.verify_venv(
                    repository,
                    target,
                    lock,
                    sys.executable,
                    identity["identity_sha256"],
                )
        self.assertEqual(prepared, identity)
        self.assertEqual(verified, identity)
        self.assertEqual(imported.stdout.strip(), "isolated")

    def test_managed_target_venv_cannot_be_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / prepare_dev_worker_venv.VENV_NAME
            socket = root / "worker.sock"
            pid_file = root / "worker.pid"
            pid_file.write_text("42\n", encoding="ascii")
            command = root / "proc" / "42" / "cmdline"
            command.parent.mkdir(parents=True)
            command.write_bytes(
                b"\0".join(
                    item.encode()
                    for item in (
                        str(target / "bin/python"),
                        "-m",
                        "uvicorn",
                        "app.main:app",
                        "--uds",
                        str(socket),
                    )
                )
                + b"\0"
            )
            with self.assertRaisesRegex(
                prepare_dev_worker_venv.DevWorkerVenvError,
                "using the target venv",
            ):
                prepare_dev_worker_venv.assert_target_not_running(
                    pid_file,
                    socket,
                    target,
                    proc_root=root / "proc",
                )

    def test_managed_legacy_base_process_does_not_block_staging(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / prepare_dev_worker_venv.VENV_NAME
            socket = root / "worker.sock"
            pid_file = root / "worker.pid"
            pid_file.write_text("43\n", encoding="ascii")
            command = root / "proc" / "43" / "cmdline"
            command.parent.mkdir(parents=True)
            command.write_bytes(
                b"/frozen/base/bin/python\0-m\0uvicorn\0app.main:app\0--uds\0"
                + str(socket).encode()
                + b"\0"
            )
            prepare_dev_worker_venv.assert_target_not_running(
                pid_file,
                socket,
                target,
                proc_root=root / "proc",
            )

    def test_socket_without_managed_pid_blocks_staging(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / prepare_dev_worker_venv.VENV_NAME
            socket = root / "worker.sock"
            socket.touch()
            with self.assertRaisesRegex(
                prepare_dev_worker_venv.DevWorkerVenvError,
                "socket exists without a PID file",
            ):
                prepare_dev_worker_venv.assert_target_not_running(
                    root / "worker.pid",
                    socket,
                    target,
                )

    def test_failed_install_does_not_replace_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository, target, lock, wheelhouse = self._repository_fixture(root)
            target.mkdir()
            sentinel = target / "sentinel"
            sentinel.write_text("keep\n", encoding="utf-8")
            for wheel in wheelhouse.iterdir():
                wheel.unlink()
            identity = release_controller.inspect_worker_base_python(sys.executable, None)
            with self.assertRaises(subprocess.CalledProcessError):
                prepare_dev_worker_venv.prepare_venv(
                    repository,
                    target,
                    lock,
                    root / "worker.pid",
                    root / "worker.sock",
                    sys.executable,
                    identity["identity_sha256"],
                    wheelhouse,
                )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")
            leftovers = [
                path.name
                for path in repository.iterdir()
                if path.name.startswith(
                    (
                        f"{prepare_dev_worker_venv.VENV_NAME}.staging-",
                        f"{prepare_dev_worker_venv.VENV_NAME}.previous-",
                    )
                )
            ]
            self.assertEqual(leftovers, [])

    def test_layout_rejects_target_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository, _, lock, _ = self._repository_fixture(root)
            with self.assertRaisesRegex(
                prepare_dev_worker_venv.DevWorkerVenvError,
                "target must be",
            ):
                prepare_dev_worker_venv.validate_layout(
                    repository,
                    root / "outside",
                    lock,
                )


if __name__ == "__main__":
    unittest.main()
