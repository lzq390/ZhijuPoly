from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.ci import validate_dependency_locks as policy


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class DependencyLockPolicyTests(unittest.TestCase):
    def test_repository_dependency_policy(self) -> None:
        self.assertEqual(policy.validate(REPOSITORY_ROOT), [])

    def test_requirement_without_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lock = Path(raw) / "requirements.lock"
            lock.write_text("example==1.2.3\n", encoding="utf-8")
            self.assertIn("has no SHA256 hash", "\n".join(policy.validate_lock_hashes(lock)))

    def test_input_cannot_include_an_unreviewed_requirement_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            requirements = Path(raw) / "requirements.in"
            requirements.write_text("-r unreviewed.txt\nexample==1.2.3\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "directives are not allowed"):
                policy.input_versions(requirements)

    def test_system_lock_hash_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            backend = root / "backend"
            backend.mkdir()
            (backend / "requirements-system.in").write_text(
                "torch==2.6.0+cu118\ntorchvision==0.21.0+cu118\n",
                encoding="utf-8",
            )
            (backend / "requirements-system.lock").write_text(
                "torch==2.6.0+cu118 \\\n"
                "    --hash=sha256:" + "0" * 64 + "\n"
                "torchvision==0.21.0+cu118 \\\n"
                "    --hash=sha256:" + policy.SYSTEM_HASHES["torchvision"] + "\n",
                encoding="utf-8",
            )
            self.assertIn("reviewed CPython 3.11 Linux wheel hash", "\n".join(policy.validate_system_lock(root)))


if __name__ == "__main__":
    unittest.main()
