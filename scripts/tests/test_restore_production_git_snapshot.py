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
SCRIPT = ROOT / "scripts/restore_production_git_snapshot.py"
SPEC = importlib.util.spec_from_file_location("production_git_restore_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
RESTORE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RESTORE)
SNAPSHOT = RESTORE.snapshot


class InjectedCrash(RuntimeError):
    pass


class RestoreFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.seed = self.root / "seed"
        self.source = self.root / "source"
        self.production = self.root / "production"
        self.runtime = self.root / "runtime"
        self.seed.mkdir(mode=0o700)
        self._git(self.seed, "init", "-b", "main")
        self._git(self.seed, "config", "user.email", "restore@example.invalid")
        self._git(self.seed, "config", "user.name", "Restore Test")
        (self.seed / ".gitignore").write_text(
            "ignored-target.txt\n",
            encoding="utf-8",
        )
        (self.seed / "state.txt").write_text(
            "production baseline\n",
            encoding="utf-8",
        )
        self._git(self.seed, "add", ".gitignore", "state.txt")
        self._git(self.seed, "commit", "-m", "baseline")
        self.production_sha = self._git(self.seed, "rev-parse", "HEAD")
        self.production_tree = self._git(self.seed, "rev-parse", "HEAD^{tree}")
        (self.seed / "state.txt").write_text("reviewed target\n", encoding="utf-8")
        (self.seed / "target.txt").write_text("target closure\n", encoding="utf-8")
        (self.seed / "ignored-target.txt").write_text(
            "tracked target content\n",
            encoding="utf-8",
        )
        self._git(self.seed, "add", "state.txt", "target.txt")
        self._git(self.seed, "add", "-f", "ignored-target.txt")
        self._git(self.seed, "commit", "-m", "target")
        self.target_sha = self._git(self.seed, "rev-parse", "HEAD")
        self.target_tree = self._git(self.seed, "rev-parse", "HEAD^{tree}")
        self._clone(self.seed, self.source)
        self._clone(self.seed, self.production)
        self.production.chmod(0o700)
        self._git(self.production, "checkout", "-B", "main", self.production_sha)
        self.runtime.mkdir(mode=0o700)
        (self.runtime / "state").mkdir(mode=0o700)
        self._make_git_private(self.production / ".git")
        self.snapshot_operation = "snapshot-git-20260819t120000z"
        self.restore_operation = "restore-git-20260819t130000z"
        self.deploy_operation = "deploy-20260819t125500z"
        snapshot_manager = SNAPSHOT.ProductionGitSnapshotManager(
            self.source,
            self.production,
            self.runtime,
            allow_test=True,
            delivery_gate_probe=self._delivery,
        )
        planned = snapshot_manager.plan(
            target_sha=self.target_sha,
            operation_id=self.snapshot_operation,
        )
        with mock.patch.object(
            SNAPSHOT,
            "_fiemap_has_shared_extents",
            return_value=False,
        ):
            self.authority = snapshot_manager.apply(
                target_sha=self.target_sha,
                operation_id=self.snapshot_operation,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_snapshot_impact_sha256=planned[
                    "snapshot_impact_sha256"
                ],
            )
        _authority, self.authority_digest = SNAPSHOT.verify_completed_snapshot(
            self.runtime,
            production_root=self.production,
            full=True,
        )

    def close(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _clone(source: Path, destination: Path) -> None:
        subprocess.run(
            ["/usr/bin/git", "clone", "--no-local", str(source), str(destination)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

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
        for path in sorted(
            git_dir.rglob("*"),
            key=lambda value: len(value.parts),
            reverse=True,
        ):
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

    def manager(self, *, checkpoint=None):  # type: ignore[no-untyped-def]
        return RESTORE.ProductionGitRestoreManager(
            self.production,
            self.runtime,
            allow_test=True,
            checkpoint=checkpoint,
        )

    def mutate_git_only(self) -> None:
        self._git(
            self.production,
            "update-ref",
            "refs/nexpoly/prepared/interrupted",
            self.target_sha,
        )
        self._make_git_private(self.production / ".git")

    def switch_to_target(self) -> None:
        self._git(self.production, "reset", "--hard", self.target_sha)
        self._make_git_private(self.production / ".git")

    def plan(self, manager=None):  # type: ignore[no-untyped-def]
        manager = manager or self.manager()
        return manager.plan(
            restore_operation_id=self.restore_operation,
            abandoned_deploy_operation_id=self.deploy_operation,
            terminal_decision="restore-before-new-operation",
        )

    def apply(self, manager, planned):  # type: ignore[no-untyped-def]
        return manager.apply(
            restore_operation_id=self.restore_operation,
            abandoned_deploy_operation_id=self.deploy_operation,
            terminal_decision="restore-before-new-operation",
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_snapshot_authority_sha256=self.authority_digest,
        )


class ProductionGitRestoreTests(unittest.TestCase):
    def test_plan_is_zero_write_and_current_state_is_terminal(self) -> None:
        fixture = RestoreFixture()
        try:
            fixture.mutate_git_only()
            before = sorted(
                path.relative_to(fixture.root).as_posix()
                for path in fixture.root.rglob("*")
            )
            planned = fixture.plan()
            after = sorted(
                path.relative_to(fixture.root).as_posix()
                for path in fixture.root.rglob("*")
            )
            self.assertEqual(before, after)
            self.assertTrue(planned["logical_zero_write"])
            self.assertFalse(planned["plan"]["materialize_worktree"])

            current = fixture.runtime / "state/current-deployment.json"
            SNAPSHOT._atomic_private_json(current, {"committed": True})
            with self.assertRaisesRegex(RESTORE.RestoreError, "forbidden"):
                fixture.manager().plan(
                    restore_operation_id="restore-git-20260819t140000z",
                    abandoned_deploy_operation_id=fixture.deploy_operation,
                    terminal_decision="restore-before-new-operation",
                )
        finally:
            fixture.close()

    def test_whole_directory_restore_archives_mutated_git(self) -> None:
        fixture = RestoreFixture()
        try:
            fixture.mutate_git_only()
            original_identity = RESTORE._directory_identity(
                fixture.production / ".git"
            )
            planned = fixture.plan()
            with mock.patch.object(
                SNAPSHOT,
                "_fiemap_has_shared_extents",
                return_value=False,
            ):
                journal = fixture.apply(fixture.manager(), planned)
            archive = (
                Path(planned["plan"]["archive_root"]) / "displaced-live.git"
            )
            self.assertEqual(journal["phase"], "completed")
            self.assertEqual(journal["exchange_count"], 1)
            self.assertEqual(RESTORE._directory_identity(archive), original_identity)
            self.assertFalse(Path(planned["plan"]["staging_one"]).exists())
            self.assertEqual(
                fixture._git(fixture.production, "rev-parse", "HEAD"),
                fixture.production_sha,
            )
            live = SNAPSHOT.scan_git_directory(fixture.production / ".git")
            golden = SNAPSHOT.scan_git_directory(Path(fixture.authority["backup_git_dir"]))
            self.assertEqual(live, golden)
        finally:
            fixture.close()

    def test_clean_target_worktree_is_materialized_without_mutating_golden(self) -> None:
        fixture = RestoreFixture()
        try:
            fixture.switch_to_target()
            planned = fixture.plan()
            self.assertTrue(planned["plan"]["materialize_worktree"])
            self.assertEqual(
                planned["plan"]["worktree_cleanup_paths"],
                ["ignored-target.txt", "target.txt"],
            )
            with mock.patch.object(
                SNAPSHOT,
                "_fiemap_has_shared_extents",
                return_value=False,
            ):
                fixture.apply(fixture.manager(), planned)
            self.assertEqual(
                fixture._git(fixture.production, "rev-parse", "HEAD"),
                fixture.production_sha,
            )
            self.assertEqual(
                (fixture.production / "state.txt").read_text(encoding="utf-8"),
                "production baseline\n",
            )
            self.assertFalse((fixture.production / "target.txt").exists())
            self.assertFalse((fixture.production / "ignored-target.txt").exists())
            self.assertEqual(
                SNAPSHOT.scan_git_directory(fixture.production / ".git"),
                SNAPSHOT.scan_git_directory(Path(fixture.authority["backup_git_dir"])),
            )
        finally:
            fixture.close()

    def test_crash_windows_resume_without_reversing_exchange(self) -> None:
        checkpoints = (
            "restore-intent",
            "restore-before-manifest-sealed",
            "restore-first-staging-copied",
            "restore-first-exchange-intent",
            "restore-first-exchange",
            "restore-archive-intent",
            "restore-displaced-archived",
            "restore-completed",
        )
        for checkpoint_name in checkpoints:
            with self.subTest(checkpoint=checkpoint_name):
                fixture = RestoreFixture()
                try:
                    fixture.mutate_git_only()
                    fired = False

                    def checkpoint(name: str) -> None:
                        nonlocal fired
                        if name == checkpoint_name and not fired:
                            fired = True
                            raise InjectedCrash(name)

                    manager = fixture.manager(checkpoint=checkpoint)
                    planned = fixture.plan(manager)
                    with mock.patch.object(
                        SNAPSHOT,
                        "_fiemap_has_shared_extents",
                        return_value=False,
                    ):
                        with self.assertRaises(InjectedCrash):
                            fixture.apply(manager, planned)
                        journal = fixture.apply(fixture.manager(), planned)
                    self.assertEqual(journal["phase"], "completed")
                    self.assertEqual(journal["exchange_count"], 1)
                    self.assertEqual(
                        fixture._git(fixture.production, "rev-parse", "HEAD"),
                        fixture.production_sha,
                    )
                finally:
                    fixture.close()

    def test_materialization_replay_uses_sealed_cleanup_inventory(self) -> None:
        fixture = RestoreFixture()
        try:
            fixture.switch_to_target()
            fired = False

            def checkpoint(name: str) -> None:
                nonlocal fired
                if name == "restore-displaced-reset" and not fired:
                    fired = True
                    raise InjectedCrash(name)

            manager = fixture.manager(checkpoint=checkpoint)
            planned = fixture.plan(manager)
            with mock.patch.object(
                SNAPSHOT,
                "_fiemap_has_shared_extents",
                return_value=False,
            ):
                with self.assertRaises(InjectedCrash):
                    fixture.apply(manager, planned)
                (fixture.production / "target.txt").write_text(
                    "interrupted residue\n",
                    encoding="utf-8",
                )
                journal = fixture.apply(fixture.manager(), planned)
            self.assertEqual(journal["phase"], "completed")
            self.assertFalse((fixture.production / "target.txt").exists())
        finally:
            fixture.close()

    def test_plan_rejects_preplanted_restore_namespace(self) -> None:
        fixture = RestoreFixture()
        try:
            fixture.mutate_git_only()
            staging = (
                fixture.production
                / f".git.restore-{fixture.restore_operation}-one"
            )
            staging.mkdir(mode=0o700)
            with self.assertRaisesRegex(RESTORE.RestoreError, "occupied"):
                fixture.plan()
        finally:
            fixture.close()

    def test_replay_rejects_confirmation_drift(self) -> None:
        fixture = RestoreFixture()
        try:
            fixture.mutate_git_only()
            fired = False

            def checkpoint(name: str) -> None:
                nonlocal fired
                if name == "restore-intent" and not fired:
                    fired = True
                    raise InjectedCrash(name)

            manager = fixture.manager(checkpoint=checkpoint)
            planned = fixture.plan(manager)
            with self.assertRaises(InjectedCrash):
                fixture.apply(manager, planned)
            with self.assertRaisesRegex(RESTORE.RestoreError, "confirmations differ"):
                fixture.manager().apply(
                    restore_operation_id=fixture.restore_operation,
                    abandoned_deploy_operation_id=fixture.deploy_operation,
                    terminal_decision="abandon-operation-to-predecessor",
                    confirm_plan_sha256=planned["plan_sha256"],
                    confirm_snapshot_authority_sha256=fixture.authority_digest,
                )
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main()
