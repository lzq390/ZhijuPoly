from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import stat
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "bootstrap_release_root.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_release_root", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
BOOTSTRAP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BOOTSTRAP)


class BootstrapReleaseRootTests(unittest.TestCase):
    def test_layout_has_only_minimal_state_and_no_release_inventory(self) -> None:
        self.assertIn("ops/state", BOOTSTRAP.DIRECTORIES)
        self.assertNotIn("ops/state/releases", BOOTSTRAP.DIRECTORIES)
        self.assertEqual(BOOTSTRAP.DIRECTORIES["ops/state"], 0o700)

    def test_dry_run_reports_no_runtime_mutation(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = BOOTSTRAP.main(["--production-root", "/tmp/nexpoly-plan"])
        self.assertEqual(result, 0)
        document = json.loads(output.getvalue())
        self.assertFalse(document["apply"])
        self.assertNotIn("/tmp/nexpoly-plan/ops/state/releases", document["directories"])
        self.assertIn("change running services", document["excluded_actions"])

    def test_apply_creates_private_directories_and_lock_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nexpoly-bootstrap-root-") as temporary:
            root = Path(temporary).resolve()
            output = io.StringIO()
            with (
                mock.patch.object(BOOTSTRAP, "PRODUCTION_ROOT", root),
                contextlib.redirect_stdout(output),
            ):
                result = BOOTSTRAP.main(
                    [
                        "--apply",
                        "--production-root",
                        str(root),
                        "--confirm-production-root",
                        str(root),
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "initialized")
            for relative, mode in BOOTSTRAP.DIRECTORIES.items():
                path = root / relative
                self.assertTrue(path.is_dir())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
            lock = root / "ops/state/deploy.lock"
            self.assertTrue(lock.is_file())
            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
            self.assertFalse((root / "ops/state/releases").exists())
            self.assertFalse((root / "ops/current").exists())

    def test_bootstrap_hook_templates_are_secret_free_valid_shell(self) -> None:
        for name in ("bootstrap-quiesce.example", "bootstrap-rollback.example"):
            path = REPOSITORY_ROOT / "ops/config" / name
            result = subprocess.run(
                ["bash", "-n", str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE_KEY", text)
            self.assertNotIn("PASSWORD=", text)


if __name__ == "__main__":
    unittest.main()
