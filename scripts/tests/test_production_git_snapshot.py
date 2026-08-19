from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/production_git_snapshot.py"
SPEC = importlib.util.spec_from_file_location("production_git_snapshot_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
SNAPSHOT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SNAPSHOT)


class InjectedCrash(RuntimeError):
    pass


class ProductionGitSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.seed = self.root / "seed"
        self.source = self.root / "source"
        self.production = self.root / "production"
        self.runtime = self.root / "runtime"
        self.seed.mkdir(mode=0o700)
        self._git(self.seed, "init", "-b", "main")
        self._git(self.seed, "config", "user.email", "snapshot@example.invalid")
        self._git(self.seed, "config", "user.name", "Snapshot Test")
        (self.seed / "state.txt").write_text("production baseline\n", encoding="utf-8")
        self._git(self.seed, "add", "state.txt")
        self._git(self.seed, "commit", "-m", "baseline")
        self.production_sha = self._git(self.seed, "rev-parse", "HEAD")
        self.production_tree = self._git(self.seed, "rev-parse", "HEAD^{tree}")
        (self.seed / "state.txt").write_text("reviewed target\n", encoding="utf-8")
        (self.seed / "target.txt").write_text("target closure\n", encoding="utf-8")
        self._git(self.seed, "add", "state.txt", "target.txt")
        self._git(self.seed, "commit", "-m", "target")
        self.target_sha = self._git(self.seed, "rev-parse", "HEAD")
        self.target_tree = self._git(self.seed, "rev-parse", "HEAD^{tree}")
        subprocess.run(
            ["/usr/bin/git", "clone", "--no-local", str(self.seed), str(self.source)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        subprocess.run(
            [
                "/usr/bin/git",
                "clone",
                "--no-local",
                str(self.seed),
                str(self.production),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._git(self.production, "checkout", "-B", "main", self.production_sha)
        self.runtime.mkdir(mode=0o700)
        (self.runtime / "state").mkdir(mode=0o700)
        self._make_git_private(self.production / ".git")
        self.operation_id = "snapshot-git-20260819t120000z"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _git(repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ["/usr/bin/git", "-C", str(repository), *arguments],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()

    @staticmethod
    def _make_git_private(git_dir: Path) -> None:
        for path in sorted(git_dir.rglob("*"), key=lambda value: len(value.parts), reverse=True):
            if path.is_symlink():
                continue
            path.chmod(0o700 if path.is_dir() else 0o600)
        git_dir.chmod(0o700)

    def _delivery(self, target_sha: str) -> dict[str, object]:
        return {
            "remote_main": target_sha,
            "ci": {
                "workflow_run_id": 12345,
                "run_attempt": 1,
                "head_sha": target_sha,
                "head_branch": "main",
                "event": "push",
                "path": ".github/workflows/ci.yml",
                "conclusion": "success",
                "required_jobs": ["Release and deployment script tests"],
            },
        }

    def _manager(self, *, checkpoint=None):  # type: ignore[no-untyped-def]
        return SNAPSHOT.ProductionGitSnapshotManager(
            self.source,
            self.production,
            self.runtime,
            allow_test=True,
            delivery_gate_probe=self._delivery,
            checkpoint=checkpoint,
        )

    def _plan(self, manager=None):  # type: ignore[no-untyped-def]
        manager = manager or self._manager()
        return manager.plan(
            target_sha=self.target_sha,
            operation_id=self.operation_id,
        )

    def _apply(self, manager, planned):  # type: ignore[no-untyped-def]
        return manager.apply(
            target_sha=self.target_sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_snapshot_impact_sha256=planned[
                "snapshot_impact_sha256"
            ],
        )

    def test_plan_is_logically_zero_write_and_seals_double_scan(self) -> None:
        before = sorted(path.relative_to(self.runtime).as_posix() for path in self.runtime.rglob("*"))
        planned = self._plan()
        after = sorted(path.relative_to(self.runtime).as_posix() for path in self.runtime.rglob("*"))

        self.assertEqual(before, after)
        self.assertTrue(planned["logical_zero_write"])
        self.assertFalse(planned["atime_zero_write"])
        self.assertEqual(planned["plan"]["production_source_sha"], self.production_sha)
        self.assertEqual(planned["plan"]["production_source_tree"], self.production_tree)
        self.assertEqual(planned["plan"]["target_source_sha"], self.target_sha)
        self.assertEqual(planned["plan"]["target_source_tree"], self.target_tree)
        self.assertGreater(planned["plan"]["manifest_summary"]["file_count"], 0)

    def test_apply_copies_independent_tree_and_publishes_authority(self) -> None:
        manager = self._manager()
        planned = self._plan(manager)
        authority = self._apply(manager, planned)
        verified, authority_digest = SNAPSHOT.verify_completed_snapshot(
            self.runtime,
            production_root=self.production,
            full=True,
        )

        self.assertEqual(verified, authority)
        self.assertRegex(authority_digest, r"^sha256:[0-9a-f]{64}$")
        source_head = (self.production / ".git/HEAD").stat()
        copied_head = Path(authority["backup_git_dir"]).joinpath("HEAD").stat()
        self.assertNotEqual(
            (source_head.st_dev, source_head.st_ino),
            (copied_head.st_dev, copied_head.st_ino),
        )
        self.assertEqual(copied_head.st_nlink, 1)
        journal = json.loads(
            (self.runtime / "state/production-git-snapshot-transactions" / f"{self.operation_id}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(journal["phase"], "completed")
        self.assertEqual(journal["completed_at"], authority["completed_at"])

    def test_verifier_rejects_snapshot_content_drift(self) -> None:
        manager = self._manager()
        planned = self._plan(manager)
        with mock.patch.object(SNAPSHOT, "_fiemap_has_shared_extents", return_value=False):
            authority = self._apply(manager, planned)
        copied_head = Path(authority["backup_git_dir"]) / "HEAD"
        copied_head.write_text("ref: refs/heads/foreign\n", encoding="utf-8")
        copied_head.chmod(0o600)

        with self.assertRaisesRegex(SNAPSHOT.SnapshotError, "snapshot changed"):
            SNAPSHOT.verify_completed_snapshot(
                self.runtime,
                production_root=self.production,
                full=True,
            )

    def test_scan_rejects_symlink_and_hard_link(self) -> None:
        symlink = self.production / ".git/foreign-link"
        symlink.symlink_to("HEAD")
        with self.assertRaisesRegex(SNAPSHOT.SnapshotError, "special entry"):
            SNAPSHOT.scan_git_directory(self.production / ".git")
        symlink.unlink()

        hardlink = self.production / ".git/foreign-hardlink"
        os.link(self.production / ".git/HEAD", hardlink)
        with self.assertRaisesRegex(SNAPSHOT.SnapshotError, "identity is unsafe"):
            SNAPSHOT.scan_git_directory(self.production / ".git")

    def test_copy_rejects_reported_shared_extents(self) -> None:
        manager = self._manager()
        planned = self._plan(manager)
        with mock.patch.object(
            SNAPSHOT,
            "_fiemap_has_shared_extents",
            return_value=True,
        ):
            with self.assertRaisesRegex(
                SNAPSHOT.SnapshotError,
                "linked or shared",
            ):
                self._apply(manager, planned)

    def test_plan_rejects_preplanted_operation_namespace(self) -> None:
        backup_root = self.runtime / "backups/production-git"
        backup_root.mkdir(mode=0o700, parents=True)
        (backup_root / self.operation_id).mkdir(mode=0o700)

        with self.assertRaisesRegex(SNAPSHOT.SnapshotError, "namespace is occupied"):
            self._plan()

    def test_apply_resumes_after_completed_copy(self) -> None:
        fired = False

        def checkpoint(name: str) -> None:
            nonlocal fired
            if name == "snapshot-copy-complete" and not fired:
                fired = True
                raise InjectedCrash(name)

        manager = self._manager(checkpoint=checkpoint)
        planned = self._plan(manager)
        with mock.patch.object(SNAPSHOT, "_fiemap_has_shared_extents", return_value=False):
            with self.assertRaises(InjectedCrash):
                self._apply(manager, planned)
            authority = self._apply(manager, planned)
            verified, _digest = SNAPSHOT.verify_completed_snapshot(
                self.runtime,
                production_root=self.production,
                full=True,
            )
        self.assertEqual(verified, authority)

    def test_apply_finishes_journal_after_authority_response_loss(self) -> None:
        fired = False

        def checkpoint(name: str) -> None:
            nonlocal fired
            if name == "snapshot-authority-published" and not fired:
                fired = True
                raise InjectedCrash(name)

        manager = self._manager(checkpoint=checkpoint)
        planned = self._plan(manager)
        with mock.patch.object(SNAPSHOT, "_fiemap_has_shared_extents", return_value=False):
            with self.assertRaises(InjectedCrash):
                self._apply(manager, planned)
            authority = self._apply(manager, planned)
        journal = json.loads(
            (self.runtime / "state/production-git-snapshot-transactions" / f"{self.operation_id}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(journal["phase"], "completed")
        self.assertEqual(journal["completed_at"], authority["completed_at"])

    def test_apply_recovers_exact_linked_authority_publication(self) -> None:
        manager = self._manager()
        planned = self._plan(manager)
        with mock.patch.object(SNAPSHOT, "_fiemap_has_shared_extents", return_value=False):
            authority = self._apply(manager, planned)
            final = self.runtime / "state/production-git-snapshot.json"
            staging = self.runtime / "state" / (
                ".production-git-snapshot.json.create-" + self.operation_id
            )
            os.link(final, staging)
            self.assertEqual(final.stat().st_nlink, 2)
            recovered = self._apply(manager, planned)

        self.assertEqual(recovered, authority)
        self.assertFalse(staging.exists())
        self.assertEqual(final.stat().st_nlink, 1)


if __name__ == "__main__":
    unittest.main()
