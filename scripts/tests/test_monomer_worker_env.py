from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = REPOSITORY_ROOT / "scripts" / "monomer_worker_env.py"
SYSTEMD_UNIT = REPOSITORY_ROOT / "ops" / "systemd" / "nexpoly-monomer-md-worker.service"
SPEC = importlib.util.spec_from_file_location("monomer_worker_env", HELPER_PATH)
assert SPEC and SPEC.loader
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


class WorkerEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "worker.env"

    def write(self, value: str, *, mode: int = 0o600) -> None:
        self.path.write_text(value, encoding="utf-8")
        self.path.chmod(mode)

    def run_helper(self, *args: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("XDG_RUNTIME_DIR", None)
        environment.pop("DBUS_SESSION_BUS_ADDRESS", None)
        return subprocess.run(
            [sys.executable, str(HELPER_PATH), *args],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_validate_get_and_exec_keep_shell_metacharacters_literal(self) -> None:
        sentinel = self.root / "must-not-exist"
        literal = f"postgresql://user:$(touch {sentinel})`id`@127.0.0.1/db"
        self.write(
            f"APP_POSTGRES_DSN={literal}\n"
            "BYTEFF2_PYTHON=/opt/byteff2/bin/python\n"
            "BYTEFF2_OPENMM_DIR=/opt/byteff2/openmm\n"
        )

        self.assertEqual(self.run_helper("validate", str(self.path)).returncode, 0)
        fetched = self.run_helper("get", str(self.path), "APP_POSTGRES_DSN")
        executed = self.run_helper(
            "exec",
            str(self.path),
            "--",
            sys.executable,
            "-c",
            "import os; print(os.environ['APP_POSTGRES_DSN'])",
        )

        self.assertEqual(fetched.stdout, literal)
        self.assertEqual(executed.stdout.strip(), literal)
        self.assertFalse(sentinel.exists())

    def test_rejects_reserved_unknown_duplicate_and_nonliteral_entries(self) -> None:
        invalid = (
            "MONOMER_MD_REQUIRE_TRANSPORT_READY=true\n",
            "OPENMM_DIR=/native/openmm\n",
            "OPENMM_PLUGIN_DIR=/native/plugins\n",
            "LD_LIBRARY_PATH=/native/lib\n",
            "PATH=/tmp\n",
            "BYTEFF2_UNKNOWN=value\n",
            "BYTEFF2_ROOT=/one\nBYTEFF2_ROOT=/two\n",
            'BYTEFF2_ROOT="/quoted"\n',
            "BYTEFF2_ROOT=/continued\\path\n",
            " BYTEFF2_ROOT=/leading\n",
            "BYTEFF2_ROOT=/trailing \n",
            "export BYTEFF2_ROOT=/value\n",
            "BYTEFF2_ROOT=/value\x00suffix\n",
        )
        for value in invalid:
            with self.subTest(value=value):
                self.write(value)
                completed = self.run_helper("validate", str(self.path))
                self.assertNotEqual(completed.returncode, 0)
                self.assertNotIn(value.partition("=")[2].strip(), completed.stderr)

    def test_rejects_every_non_newline_control_separator_before_splitting(self) -> None:
        for separator in ("\v", "\f", "\x1c", "\x85", "\r"):
            with self.subTest(codepoint=ord(separator)):
                self.write(
                    "BYTEFF2_ROOT=/srv/byteff2"
                    f"{separator}MONOMER_MD_WORKER_MODE=real\n"
                )
                completed = self.run_helper("validate", str(self.path))
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("control character", completed.stderr)

    def test_requires_owner_only_regular_non_symlink_file(self) -> None:
        self.write("BYTEFF2_ROOT=/srv/byteff2\n", mode=0o644)
        mode = self.run_helper("validate", str(self.path))
        self.assertNotEqual(mode.returncode, 0)
        self.assertIn("0600", mode.stderr)

        target = self.root / "target.env"
        target.write_text("BYTEFF2_ROOT=/srv/byteff2\n", encoding="utf-8")
        target.chmod(0o600)
        self.path.unlink()
        self.path.symlink_to(target)
        symlink = self.run_helper("validate", str(self.path))
        self.assertNotEqual(symlink.returncode, 0)
        self.assertIn("non-symlink", symlink.stderr)

    def test_rejects_wrong_owner_and_non_regular_file(self) -> None:
        self.write("BYTEFF2_ROOT=/srv/byteff2\n")
        metadata = self.path.stat()
        wrong_owner = mock.Mock(
            st_mode=metadata.st_mode,
            st_uid=os.geteuid() + 1,
            st_size=metadata.st_size,
        )
        with (
            mock.patch.object(helper.os, "fstat", return_value=wrong_owner),
            self.assertRaisesRegex(helper.WorkerEnvError, "owned by uid"),
        ):
            helper.load_worker_env(self.path)

        directory = self.root / "worker-env-directory"
        directory.mkdir(mode=0o700)
        with self.assertRaisesRegex(helper.WorkerEnvError, "regular"):
            helper.load_worker_env(directory)

    def test_rejects_oversized_file(self) -> None:
        self.write("#" + "x" * helper.MAX_ENV_FILE_BYTES)
        completed = self.run_helper("validate", str(self.path))
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("exceeds", completed.stderr)

    def test_process_environment_scrubs_inherited_governed_values(self) -> None:
        values = {
            "BYTEFF2_PYTHON": "/opt/byteff2/bin/python",
            "BYTEFF2_ROOT": "/srv/byteff2",
            "MONOMER_MD_GPU_BROKER_ENABLED": "true",
        }
        inherited = {
            "PATH": "/untrusted/bin",
            "LD_LIBRARY_PATH": "/untrusted/lib",
            "LD_UNREVIEWED_FUTURE_KEY": "/untrusted/future",
            "OPENMM_DIR": "/untrusted/openmm",
            "MONOMER_MD_REQUIRE_TRANSPORT_READY": "false",
            "MONOMER_MD_CUDA_VISIBLE_DEVICES": "7",
            "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
            "CUDA_MPS_PIPE_DIRECTORY": "/untrusted/mps",
            "NVIDIA_VISIBLE_DEVICES": "all",
            "PYTHONUNREVIEWED_FUTURE_KEY": "unsafe",
            "PYTHONUSERBASE": "/untrusted/python-user-base",
            "PYTHONPATH": "/untrusted/python-path",
            "PIP_TARGET": "/untrusted/pip-target",
            "PIP_CONFIG_FILE": "/untrusted/pip.conf",
            "TORCH_HOME": "/untrusted/torch",
            "HF_HOME": "/untrusted/huggingface",
            "OMP_NUM_THREADS": "999",
            "BASH_FUNC_injected%%": "() { echo unsafe; }",
            "UNRELATED": "must-not-be-inherited",
            "HOME": "/safe/home",
            "LANG": "C.UTF-8",
        }

        environment = helper.build_worker_process_environment(
            values,
            inherited=inherited,
            overrides={"MONOMER_MD_CUDA_VISIBLE_DEVICES": "1"},
        )

        self.assertEqual(environment["MONOMER_MD_CUDA_VISIBLE_DEVICES"], "1")
        self.assertEqual(environment["MONOMER_MD_GPU_BROKER_ENABLED"], "true")
        self.assertEqual(environment["HOME"], "/safe/home")
        self.assertEqual(environment["LANG"], "C.UTF-8")
        self.assertEqual(environment["PYTHONNOUSERSITE"], "1")
        self.assertEqual(environment[helper.SANITIZED_MARKER], "1")
        self.assertEqual(
            environment["PATH"],
            "/opt/byteff2/bin:" + helper.SAFE_SYSTEM_PATH,
        )
        for key in (
            "LD_LIBRARY_PATH",
            "LD_UNREVIEWED_FUTURE_KEY",
            "OPENMM_DIR",
            "MONOMER_MD_REQUIRE_TRANSPORT_READY",
            "CUDA_DEVICE_ORDER",
            "CUDA_MPS_PIPE_DIRECTORY",
            "NVIDIA_VISIBLE_DEVICES",
            "PYTHONUNREVIEWED_FUTURE_KEY",
            "PYTHONUSERBASE",
            "PYTHONPATH",
            "PIP_TARGET",
            "PIP_CONFIG_FILE",
            "TORCH_HOME",
            "HF_HOME",
            "OMP_NUM_THREADS",
            "BASH_FUNC_injected%%",
            "UNRELATED",
        ):
            self.assertNotIn(key, environment)

    def test_systemd_user_bus_is_only_preserved_with_exact_runtime_identity(
        self,
    ) -> None:
        runtime = f"/run/user/{os.geteuid()}"
        environment = helper.build_worker_process_environment(
            {},
            inherited={
                "XDG_RUNTIME_DIR": runtime,
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
            },
        )
        self.assertEqual(environment["XDG_RUNTIME_DIR"], runtime)
        self.assertEqual(
            environment["DBUS_SESSION_BUS_ADDRESS"],
            f"unix:path={runtime}/bus",
        )

        for inherited in (
            {
                "XDG_RUNTIME_DIR": runtime,
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/attacker-bus",
            },
            {"XDG_RUNTIME_DIR": runtime},
            {"DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus"},
        ):
            with self.assertRaisesRegex(
                helper.WorkerEnvError,
                "manager environment identity is invalid",
            ):
                helper.build_worker_process_environment(
                    {},
                    inherited=inherited,
                )

    def test_cli_exec_sets_only_safe_summary_fields(self) -> None:
        self.write(
            "BYTEFF2_PYTHON=/opt/byteff2/bin/python\n"
            "MONOMER_MD_GPU_BROKER_SOCKET_PATH=/run/user/1001/gpu.sock\n"
        )
        completed = self.run_helper(
            "exec",
            str(self.path),
            "--",
            sys.executable,
            "-c",
            (
                "import json,os; print(json.dumps({"
                "'marker':os.environ.get('NEXPOLY_MONOMER_MD_ENV_SANITIZED'),"
                "'socket':os.environ.get('MONOMER_MD_GPU_BROKER_SOCKET_PATH')}))"
            ),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {"marker": "1", "socket": "/run/user/1001/gpu.sock"},
        )

    def test_systemd_uses_only_the_immutable_control_selector(self) -> None:
        unit = SYSTEMD_UNIT.read_text(encoding="utf-8")
        active_lines = [
            line
            for line in unit.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertFalse(any(line.startswith("EnvironmentFile=") for line in active_lines))
        exec_start = next(line for line in active_lines if line.startswith("ExecStart="))
        self.assertIn(
            "/usr/bin/python3 -I -B "
            "/data/lzq/gith/nexpoly-runtime/bin/control_runtime_selector.py "
            "run monomer-md",
            exec_start,
        )
        self.assertNotIn("monomer_worker_env.py", exec_start)
        self.assertNotIn("monomer_md_worker_launcher.py", exec_start)
        self.assertNotIn("/ops/current", exec_start)


if __name__ == "__main__":
    unittest.main()
