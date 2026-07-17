from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "bootstrap_release_root.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_release_root", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
BOOTSTRAP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BOOTSTRAP)


class BootstrapReleaseRootTests(unittest.TestCase):
    def test_retired_shim_always_fails_closed_without_creating_old_layout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nexpoly-retired-bootstrap-") as temporary:
            root = Path(temporary) / "production"
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                result = BOOTSTRAP.main(
                    [
                        "--apply",
                        "--production-root",
                        str(root),
                        "--confirm-production-root",
                        str(root),
                    ]
                )

            self.assertEqual(result, 2)
            self.assertIn("retired", error.getvalue())
            self.assertIn("./scripts/bootstrap_pull_deploy.py", error.getvalue())
            self.assertNotIn("python3 scripts/bootstrap_pull_deploy.py", error.getvalue())
            self.assertFalse(root.exists())

    def test_retired_shim_dry_run_is_also_fail_closed(self) -> None:
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            result = BOOTSTRAP.main([])

        self.assertEqual(result, 2)
        self.assertIn("nexpoly-runtime", error.getvalue())

    def test_bootstrap_hook_templates_are_secret_free_valid_shell(self) -> None:
        names = {
            "bootstrap-quiesce.example",
            "bootstrap-status.example",
            "bootstrap-resume-unchanged.example",
            "bootstrap-rollback.example",
        }
        actual = {
            path.name
            for path in (REPOSITORY_ROOT / "ops/config").glob("bootstrap-*.example")
        }
        self.assertEqual(actual, names)
        for name in sorted(names):
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
