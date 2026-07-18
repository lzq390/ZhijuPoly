from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "bootstrap_pull_deploy.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_pull_deploy", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
BOOTSTRAP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BOOTSTRAP)
SOURCE_SHA = "1" * 40
SOURCE_TREE = "2" * 40
TAKEOVER_OPERATION_ID = "takeover-fixture-0001"


def takeover_binding(
    operation_id: str = TAKEOVER_OPERATION_ID,
) -> dict[str, object]:
    binding: dict[str, object] = {
        "schema_version": 1,
        "operation_id": operation_id,
        "authority_sha": SOURCE_SHA,
        "authority_tree": SOURCE_TREE,
        "install_manifest_sha256": "sha256:" + "3" * 64,
        "classification_sha256": "sha256:" + "4" * 64,
        "runtime_identity_sha256": "sha256:" + "5" * 64,
        "git_identity": {
            "branch": "refs/heads/main",
            "head_sha": "0" * 40,
            "head_tree": "0" * 40,
            "local_main_sha": "0" * 40,
        },
        "pre_stopped_fence_sha256": "sha256:" + "6" * 64,
        "control_layout_sha256": "sha256:" + "7" * 64,
        "checkout_permissions_sha256": "sha256:" + "9" * 64,
        "applied_record_sha256": "sha256:" + "8" * 64,
    }
    binding["binding_sha256"] = BOOTSTRAP.digest(
        json.dumps(
            binding,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    )
    return binding


class BootstrapPullDeployTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="nexpoly-pull-bootstrap-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.production = self.root / "production"
        self.runtime = self.root / "runtime"
        self.production.mkdir(mode=0o775)
        subprocess.run(
            ["git", "init", "--initial-branch=main"],
            cwd=self.production,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tracked = self.production / "tracked" / "fixture.txt"
        tracked.parent.mkdir(mode=0o775)
        tracked.write_text("fixture\n", encoding="utf-8")
        os.chmod(tracked, 0o664)
        subprocess.run(["git", "add", "tracked/fixture.txt"], cwd=self.production, check=True)

    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        effective_arguments = list(arguments)
        if (
            "--check-source-readiness" not in effective_arguments
            and "--legacy-takeover-operation-id" not in effective_arguments
        ):
            effective_arguments.extend(
                [
                    "--legacy-takeover-operation-id",
                    TAKEOVER_OPERATION_ID,
                ]
            )
        # The pre-takeover installer owns creation of the shared lock.
        # Bootstrap must acquire it before making any runtime write.
        if (
            "--apply" in effective_arguments
            and "--check-source-readiness" not in effective_arguments
            and not (
            self.runtime.exists() or self.runtime.is_symlink()
            )
        ):
            state = self.runtime / "state"
            state.mkdir(parents=True, mode=0o700)
            os.chmod(self.runtime, 0o700)
            os.chmod(state, 0o700)
            lock = state / "deploy.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o600)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"NEXPOLY_ALLOW_TEST_ROOT": "1"}),
            mock.patch.object(
                BOOTSTRAP,
                "_completed_legacy_takeover",
                return_value=takeover_binding(),
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result = BOOTSTRAP.main(effective_arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def apply_arguments(self) -> list[str]:
        return [
            "--sha",
            SOURCE_SHA,
            "--apply",
            "--production-root",
            str(self.production),
            "--runtime-root",
            str(self.runtime),
            "--confirm-production-root",
            str(self.production.absolute()),
            "--confirm-runtime-root",
            str(self.runtime.absolute()),
            "--legacy-takeover-operation-id",
            TAKEOVER_OPERATION_ID,
            "--confirm-source-tree",
            SOURCE_TREE,
        ]

    def committed_private_repo(self, path: Path) -> Path:
        previous_umask = os.umask(0o077)
        try:
            path.mkdir(mode=0o700)
            subprocess.run(
                ["git", "init", "--initial-branch=main", "--quiet"],
                cwd=path,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Bootstrap Fixture"],
                cwd=path,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "bootstrap@example.invalid"],
                cwd=path,
                check=True,
            )
            (path / "control.txt").write_text("reviewed\n", encoding="utf-8")
            subprocess.run(["git", "add", "control.txt"], cwd=path, check=True)
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "fixture"],
                cwd=path,
                check=True,
            )
        finally:
            os.umask(previous_umask)
        return path

    def ready_private_repo(self, path: Path) -> Path:
        source = self.committed_private_repo(path)
        subprocess.run(
            [
                "git",
                "remote",
                "add",
                "origin",
                BOOTSTRAP.REPOSITORY_SSH_URL,
            ],
            cwd=source,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "update-ref",
                "refs/remotes/origin/main",
                "HEAD",
            ],
            cwd=source,
            check=True,
        )
        return source

    def test_dry_run_is_non_mutating_and_lists_external_layout(self) -> None:
        result, output, error = self.run_main(
            "--sha",
            SOURCE_SHA,
            "--production-root",
            str(self.production),
            "--runtime-root",
            str(self.runtime),
        )
        self.assertEqual(result, 0, error)
        document = json.loads(output)
        self.assertFalse(document["apply"])
        self.assertFalse(self.runtime.exists())
        self.assertIn(str(self.runtime / "state" / "worker-slots"), document["directories"])
        self.assertIn(str(self.runtime / "worker-venvs"), document["directories"])
        self.assertIn(str(self.runtime / "control-releases"), document["directories"])
        self.assertNotIn(str(self.runtime / "worker-venvs" / "md-a"), document["directories"])
        self.assertIn("change Git HEAD or fetch", document["excluded_actions"])
        self.assertEqual(document["delivery_gate"]["remote_main"], SOURCE_SHA)

    def test_requested_sha_and_source_tree_confirmation_are_fail_closed(self) -> None:
        result, _output, error = self.run_main(
            *self.apply_arguments()[:-1], "0" * 40
        )
        self.assertEqual(result, 2)
        self.assertIn("matching confirmations", error)
        arguments = self.apply_arguments()
        arguments[1] = "f" * 40
        result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2)
        self.assertIn("requested bootstrap SHA", error)

    def test_stable_wrappers_ignore_hostile_shell_startup_environment(self) -> None:
        hostile = self.root / "hostile"
        hostile.mkdir(mode=0o700)
        marker = hostile / "bash-env-executed"
        bash_env = hostile / "bash-env"
        bash_env.write_text(f"touch {marker}\n", encoding="utf-8")
        fake_bash = hostile / "bash"
        fake_bash.write_text(f"#!/bin/sh\ntouch {marker}\nexit 99\n", encoding="utf-8")
        os.chmod(fake_bash, 0o700)
        for name in (
            "nexpoly-pull-deploy",
            "nexpoly-production-readiness",
            "nexpoly-pull-contract-0012",
            "nexpoly-reconcile-production-0005-polytao-alias",
        ):
            wrapper = REPOSITORY_ROOT / "scripts" / name
            self.assertEqual(
                wrapper.read_text(encoding="utf-8").splitlines()[0],
                "#!/usr/bin/python3 -I",
            )
            completed = subprocess.run(
                [str(wrapper), "plan"],
                env={
                    "PATH": str(hostile),
                    "BASH_ENV": str(bash_env),
                    "ENV": str(bash_env),
                    "PYTHONPATH": str(hostile),
                    "PYTHONSTARTUP": str(bash_env),
                    "PYTHONUSERBASE": str(hostile),
                },
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(completed.returncode, 99)
            self.assertFalse(marker.exists())
            direct = subprocess.run(
                ["/usr/bin/python3", str(wrapper), "plan"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(direct.returncode, 0)
            self.assertIn("requires isolated Python startup", direct.stderr)

    def test_apply_installs_private_stable_controls_and_no_runtime_state(self) -> None:
        result, output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 0, error)
        document = json.loads(output)
        self.assertEqual(document["status"], "initialized")
        for relative, mode in BOOTSTRAP.DIRECTORIES.items():
            path = self.runtime / relative
            self.assertTrue(path.is_dir(), relative)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
        self.assertEqual(
            {entry.name for entry in (self.runtime / "bin").iterdir()},
            set(BOOTSTRAP.IMMUTABLE_FILES),
        )
        for name in BOOTSTRAP.IMMUTABLE_FILES:
            path = self.runtime / "bin" / name
            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        lock = self.runtime / "state" / "deploy.lock"
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
        self.assertFalse((self.runtime / "state" / "current-deployment.json").exists())
        self.assertFalse((self.runtime / "state" / "monomer-md-active-slot.json").exists())
        self.assertFalse((self.runtime / "state" / "deploy-in-progress.json").exists())
        active = json.loads(
            (self.runtime / "state/active-control.json").read_text(encoding="utf-8")
        )
        release = self.runtime / "control-releases" / active["release_id"]
        self.assertTrue(release.is_dir())
        self.assertTrue((release / "CONTROL-MANIFEST.json").is_file())
        self.assertTrue((release / "pull_deploy_controller.py").is_file())
        self.assertTrue(
            (release / "reconcile_production_0005_polytao_alias.py").is_file()
        )
        self.assertEqual(stat.S_IMODE(self.production.stat().st_mode) & 0o022, 0)
        self.assertEqual(stat.S_IMODE((self.production / ".git").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.production / ".git/config").stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE((self.production / "tracked/fixture.txt").stat().st_mode) & 0o022,
            0,
        )
        self.assertFalse((self.runtime / "worker-venvs/md-a").exists())
        bootstrap_record = self.runtime / "state/bootstrap-control.json"
        self.assertEqual(stat.S_IMODE(bootstrap_record.stat().st_mode), 0o600)
        self.assertEqual(
            json.loads(bootstrap_record.read_text(encoding="utf-8"))["source_sha"],
            SOURCE_SHA,
        )
        readiness = json.loads(
            bootstrap_record.read_text(encoding="utf-8")
        )["source_readiness"]
        self.assertTrue(readiness["ready"])
        self.assertTrue(readiness["owner_private"])
        self.assertEqual(readiness["source_tree"], SOURCE_TREE)
        authority = json.loads(
            bootstrap_record.read_text(encoding="utf-8")
        )
        self.assertEqual(authority["schema_version"], 2)
        self.assertEqual(
            authority["legacy_takeover"],
            takeover_binding(),
        )

    def test_takeover_binding_requires_exact_f_and_legacy_git_identity(
        self,
    ) -> None:
        observed: dict[str, object] = {}

        def validate_completed(
            runtime_root: Path,
            operation_id: str,
            authority_sha: str,
            authority_tree: str,
            *,
            expected_git_identity: dict[str, str],
        ) -> dict[str, object]:
            observed.update(
                {
                    "runtime_root": runtime_root,
                    "operation_id": operation_id,
                    "authority_sha": authority_sha,
                    "authority_tree": authority_tree,
                    "git_identity": expected_git_identity,
                }
            )
            return takeover_binding(operation_id)

        repository = BOOTSTRAP._production_repository_identity(
            self.production,
            SOURCE_SHA,
            allow_test=True,
        )
        with mock.patch.object(
            BOOTSTRAP,
            "_legacy_takeover_evidence",
            return_value=SimpleNamespace(
                validate_completed=validate_completed
            ),
        ):
            binding = BOOTSTRAP._completed_legacy_takeover(
                self.runtime,
                TAKEOVER_OPERATION_ID,
                source_sha=SOURCE_SHA,
                source_tree=SOURCE_TREE,
                production_repository=repository,
                allow_test=True,
            )
        self.assertEqual(binding, takeover_binding())
        self.assertEqual(
            observed,
            {
                "runtime_root": self.runtime.absolute(),
                "operation_id": TAKEOVER_OPERATION_ID,
                "authority_sha": SOURCE_SHA,
                "authority_tree": SOURCE_TREE,
                "git_identity": {
                    "branch": "refs/heads/main",
                    "head_sha": "0" * 40,
                    "head_tree": "0" * 40,
                    "local_main_sha": "0" * 40,
                },
            },
        )

    def test_shared_deploy_lock_blocks_before_every_runtime_write(self) -> None:
        state = self.runtime / "state"
        state.mkdir(parents=True, mode=0o700)
        os.chmod(self.runtime, 0o700)
        os.chmod(state, 0o700)
        lock = state / "deploy.lock"
        lock.write_bytes(b"pre-takeover-lock\n")
        os.chmod(lock, 0o600)

        def snapshot() -> list[tuple[str, int, bytes]]:
            records: list[tuple[str, int, bytes]] = []
            for path in sorted(self.runtime.rglob("*")):
                relative = path.relative_to(self.runtime).as_posix()
                mode = stat.S_IMODE(path.lstat().st_mode)
                records.append(
                    (
                        relative,
                        mode,
                        path.read_bytes() if path.is_file() else b"",
                    )
                )
            return records

        before = snapshot()
        with lock.open("r+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2)
        self.assertIn("another deployment holds deploy.lock", error)
        self.assertEqual(snapshot(), before)

    def test_apply_takes_over_exact_legacy_worker_unit_with_private_backup(self) -> None:
        unit = (
            self.root
            / "systemd/user/nexpoly-monomer-md-worker.service"
        )
        unit.parent.mkdir(parents=True, mode=0o700)
        unit.write_bytes(b"[Service]\nExecStart=/legacy\n")
        os.chmod(unit, 0o664)
        checksum = BOOTSTRAP.digest(unit.read_bytes())
        arguments = [
            *self.apply_arguments(),
            "--confirm-worker-unit-sha256",
            checksum,
        ]
        result, output, error = self.run_main(*arguments)
        self.assertEqual(result, 0, error)
        self.assertEqual(stat.S_IMODE(unit.stat().st_mode), 0o600)
        takeover = json.loads(output)["worker_unit_takeover"]
        self.assertEqual(takeover["status"], "completed")
        backup = Path(takeover["backup_path"])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_bytes(), unit.read_bytes())
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertTrue(backup.is_relative_to(self.runtime / "backups"))
        result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 0, error)

    def test_worker_unit_confirmation_and_mode_fail_before_runtime_write(self) -> None:
        unit = self.root / "systemd/user/nexpoly-monomer-md-worker.service"
        unit.parent.mkdir(parents=True, mode=0o700)
        unit.write_bytes(b"legacy unit\n")
        os.chmod(unit, 0o664)
        result, _output, error = self.run_main(
            *self.apply_arguments(),
            "--confirm-worker-unit-sha256",
            "sha256:" + "f" * 64,
        )
        self.assertEqual(result, 2)
        self.assertIn("explicit confirmation", error)
        self.assertFalse((self.runtime / "bin").exists())
        self.assertFalse((self.runtime / "state/bootstrap-control.json").exists())

        os.chmod(unit, 0o644)
        result, _output, error = self.run_main(
            *self.apply_arguments(),
            "--confirm-worker-unit-sha256",
            BOOTSTRAP.digest(unit.read_bytes()),
        )
        self.assertEqual(result, 2)
        self.assertIn("mode is not an allowed", error)
        self.assertFalse((self.runtime / "bin").exists())
        self.assertFalse((self.runtime / "state/bootstrap-control.json").exists())

    def test_worker_unit_takeover_crash_after_atomic_replace_is_resumable(self) -> None:
        unit = self.root / "systemd/user/nexpoly-monomer-md-worker.service"
        unit.parent.mkdir(parents=True, mode=0o700)
        unit.write_bytes(b"legacy unit\n")
        os.chmod(unit, 0o664)
        arguments = [
            *self.apply_arguments(),
            "--confirm-worker-unit-sha256",
            BOOTSTRAP.digest(unit.read_bytes()),
        ]
        with mock.patch.object(
            BOOTSTRAP,
            "_daemon_reload_worker_unit",
            side_effect=BOOTSTRAP.BootstrapError("injected reload crash"),
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2)
        self.assertIn("injected reload crash", error)
        self.assertEqual(stat.S_IMODE(unit.stat().st_mode), 0o600)
        self.assertTrue(
            (self.runtime / "audit/bootstrap-worker-unit/takeover-intent.json").is_file()
        )
        self.assertFalse(
            (self.runtime / "audit/bootstrap-worker-unit/takeover.json").exists()
        )
        result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 0, error)

    def test_worker_unit_pre_replace_crash_never_claims_legacy_inode_is_private(
        self,
    ) -> None:
        unit = self.root / "systemd/user/nexpoly-monomer-md-worker.service"
        unit.parent.mkdir(parents=True, mode=0o700)
        unit.write_bytes(b"legacy unit\n")
        os.chmod(unit, 0o664)
        checksum = BOOTSTRAP.digest(unit.read_bytes())
        writer = os.open(unit, os.O_RDWR)
        original_replace = BOOTSTRAP.os.replace

        def fail_unit_replace(source, destination):  # type: ignore[no-untyped-def]
            if Path(destination) == unit:
                raise OSError("injected unit publication crash")
            return original_replace(source, destination)

        try:
            with mock.patch.object(
                BOOTSTRAP.os, "replace", side_effect=fail_unit_replace
            ):
                result, _output, error = self.run_main(
                    *self.apply_arguments(),
                    "--confirm-worker-unit-sha256",
                    checksum,
                )
            self.assertEqual(result, 2)
            self.assertIn("unit publication crash", error)
            self.assertEqual(stat.S_IMODE(unit.stat().st_mode), 0o664)
            self.assertFalse(
                (self.runtime / "audit/bootstrap-worker-unit/takeover.json").exists()
            )
            os.lseek(writer, 0, os.SEEK_SET)
            os.write(writer, b"PWNED\n")
            os.ftruncate(writer, len(b"PWNED\n"))
            os.fsync(writer)
        finally:
            os.close(writer)
        self.assertEqual(unit.read_bytes(), b"PWNED\n")
        result, _output, error = self.run_main(
            *self.apply_arguments(),
            "--confirm-worker-unit-sha256",
            checksum,
        )
        self.assertEqual(result, 2)
        self.assertIn("explicit confirmation", error)

    def test_worker_unit_drift_after_atomic_takeover_fails_closed(self) -> None:
        unit = self.root / "systemd/user/nexpoly-monomer-md-worker.service"
        unit.parent.mkdir(parents=True, mode=0o700)
        unit.write_bytes(b"legacy unit\n")
        os.chmod(unit, 0o664)
        checksum = BOOTSTRAP.digest(unit.read_bytes())

        def mutate_after_reload(*, allow_test: bool) -> None:
            self.assertTrue(allow_test)
            unit.write_bytes(b"mutated after reload\n")
            os.chmod(unit, 0o600)

        with mock.patch.object(
            BOOTSTRAP,
            "_daemon_reload_worker_unit",
            side_effect=mutate_after_reload,
        ):
            result, _output, error = self.run_main(
                *self.apply_arguments(),
                "--confirm-worker-unit-sha256",
                checksum,
            )
        self.assertEqual(result, 2)
        self.assertIn("permission takeover did not verify", error)
        self.assertFalse(
            (self.runtime / "audit/bootstrap-worker-unit/takeover.json").exists()
        )

    def test_apply_is_idempotent_only_for_byte_identical_installed_controller(self) -> None:
        first, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(first, 0, error)
        second, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(second, 0, error)

        installed = self.runtime / "bin" / "control_runtime_selector.py"
        installed.write_text("tampered\n", encoding="utf-8")
        os.chmod(installed, 0o700)
        third, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(third, 2)
        self.assertIn("refusing to overwrite", error)

    def test_immutable_install_crash_never_publishes_a_partial_final_file(self) -> None:
        directory = self.root / "atomic-install"
        directory.mkdir(mode=0o700)
        target = directory / "router"
        with mock.patch.object(
            BOOTSTRAP.os,
            "link",
            side_effect=OSError("injected no-replace publication crash"),
        ):
            with self.assertRaisesRegex(OSError, "publication crash"):
                BOOTSTRAP._install_exact(target, b"reviewed payload\n", 0o700)
        self.assertFalse(target.exists())
        self.assertEqual(list(directory.iterdir()), [])
        self.assertEqual(
            BOOTSTRAP._install_exact(target, b"reviewed payload\n", 0o700),
            BOOTSTRAP.digest(b"reviewed payload\n"),
        )
        self.assertEqual(target.read_bytes(), b"reviewed payload\n")

    def test_apply_rejects_symlink_inside_production_git(self) -> None:
        target = self.root / "outside"
        target.write_text("outside\n", encoding="utf-8")
        link = self.production / ".git" / "unsafe"
        link.symlink_to(target)
        result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2)
        self.assertIn("unsafe production Git entry", error)

    def test_git_probe_disables_hostile_fsmonitor_and_pins_worktree(self) -> None:
        marker = self.root / "fsmonitor-executed"
        monitor = self.root / "hostile-fsmonitor"
        monitor.write_text(
            f"#!/bin/sh\ntouch {marker}\nexit 1\n", encoding="utf-8"
        )
        os.chmod(monitor, 0o700)
        outside = self.root / "outside-worktree"
        outside.mkdir(mode=0o700)
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.production / ".git"),
                "config",
                "core.fsmonitor",
                str(monitor),
            ],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.production / ".git"),
                "config",
                "core.worktree",
                str(outside),
            ],
            check=True,
        )
        result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 0, error)
        self.assertFalse(marker.exists())
        self.assertEqual(
            stat.S_IMODE((self.production / "tracked/fixture.txt").stat().st_mode)
            & 0o022,
            0,
        )

    def test_git_probe_rejects_executable_clean_filter_before_git_runs(self) -> None:
        marker = self.root / "clean-filter-executed"
        monitor = self.root / "hostile-clean-filter"
        monitor.write_text(
            f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8"
        )
        os.chmod(monitor, 0o700)
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.production / ".git"),
                "config",
                "filter.hostile.clean",
                str(monitor),
            ],
            check=True,
        )
        (self.production / ".gitattributes").write_text(
            "tracked/fixture.txt filter=hostile\n", encoding="utf-8"
        )
        os.chmod(self.production / ".gitattributes", 0o664)
        result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2)
        self.assertIn("unsupported section", error)
        self.assertFalse(marker.exists())
        self.assertFalse(any((self.runtime / "bin").iterdir()))
        self.assertFalse((self.runtime / "state/bootstrap-control.json").exists())

    def test_private_source_rejects_shared_clone_external_object_database(self) -> None:
        source = self.committed_private_repo(self.root / "shared-source")
        private_parent = self.root / "private-bootstrap-parent"
        private_parent.mkdir(mode=0o700)
        clone = private_parent / "clone"
        previous_umask = os.umask(0o077)
        try:
            subprocess.run(
                ["git", "clone", "--shared", "--quiet", str(source), str(clone)],
                check=True,
            )
        finally:
            os.umask(previous_umask)
        alternates = clone / ".git/objects/info/alternates"
        self.assertTrue(alternates.is_file())
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError, "forbidden external storage"
        ):
            BOOTSTRAP._assert_private_bootstrap_source(clone)

    def test_private_source_rejects_commondir_and_local_hardlink_clones(self) -> None:
        source = self.committed_private_repo(self.root / "local-source")
        private_parent = self.root / "private-clones"
        private_parent.mkdir(mode=0o700)
        previous_umask = os.umask(0o077)
        try:
            independent = private_parent / "independent"
            subprocess.run(
                ["git", "clone", "--no-local", "--quiet", str(source), str(independent)],
                check=True,
            )
            commondir = independent / ".git/commondir"
            commondir.write_text(str(source / ".git") + "\n", encoding="utf-8")
            os.chmod(commondir, 0o600)
            with self.assertRaisesRegex(
                BOOTSTRAP.BootstrapError, "forbidden external storage"
            ):
                BOOTSTRAP._assert_private_bootstrap_source(independent)

            local_clone = private_parent / "local-hardlinks"
            subprocess.run(
                ["git", "clone", "--local", "--quiet", str(source), str(local_clone)],
                check=True,
            )
        finally:
            os.umask(previous_umask)
        with self.assertRaisesRegex(BOOTSTRAP.BootstrapError, "hard-linked"):
            BOOTSTRAP._assert_private_bootstrap_source(local_clone)

    def test_source_readiness_accepts_only_exact_private_canonical_clone(self) -> None:
        source = self.ready_private_repo(self.root / "ready-source")
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        report = BOOTSTRAP.bootstrap_source_readiness(
            source,
            expected_sha=sha,
        )
        self.assertTrue(report["ready"])
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["source_sha"], sha)
        self.assertEqual(report["origin_fetch_urls"], [BOOTSTRAP.REPOSITORY_SSH_URL])
        self.assertEqual(report["origin_push_urls"], [BOOTSTRAP.REPOSITORY_SSH_URL])
        self.assertEqual(report["ignored_entries"], 0)
        self.assertEqual(report["unreachable_objects"], 0)
        self.assertEqual(report["replace_refs"], 0)
        self.assertEqual(report["special_index_entries"], 0)
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError, "commit identity"
        ):
            BOOTSTRAP.bootstrap_source_readiness(
                source,
                expected_sha="f" * 40,
            )

    def test_source_readiness_rejects_shallow_ignored_and_unreachable_objects(
        self,
    ) -> None:
        shallow = self.ready_private_repo(self.root / "shallow-source")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=shallow,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (shallow / ".git/shallow").write_text(head + "\n", encoding="ascii")
        os.chmod(shallow / ".git/shallow", 0o600)
        with self.assertRaisesRegex(BOOTSTRAP.BootstrapError, "must not be shallow"):
            BOOTSTRAP.bootstrap_source_readiness(shallow)

        ignored = self.ready_private_repo(self.root / "ignored-source")
        (ignored / ".gitignore").write_text("runtime-cache/\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore"], cwd=ignored, check=True)
        subprocess.run(
            ["git", "commit", "--quiet", "-m", "ignore fixture"],
            cwd=ignored,
            check=True,
        )
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
            cwd=ignored,
            check=True,
        )
        cache = ignored / "runtime-cache"
        cache.mkdir(mode=0o700)
        (cache / "value").write_text("ignored\n", encoding="utf-8")
        os.chmod(cache / "value", 0o600)
        with self.assertRaisesRegex(BOOTSTRAP.BootstrapError, "ignored paths"):
            BOOTSTRAP.bootstrap_source_readiness(ignored)

        dangling = self.ready_private_repo(self.root / "dangling-source")
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=dangling,
            input=b"unreviewed object\n",
            check=True,
            stdout=subprocess.PIPE,
        )
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError, "dangling or unreachable"
        ):
            BOOTSTRAP.bootstrap_source_readiness(dangling)

    def test_source_readiness_rejects_replace_refs_and_hidden_index_bits(
        self,
    ) -> None:
        replacement = self.ready_private_repo(self.root / "replace-source")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=replacement,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=replacement,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        alternate = subprocess.run(
            [
                "git",
                "commit-tree",
                tree,
                "-p",
                head,
                "-m",
                "replacement fixture",
            ],
            cwd=replacement,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "replace", head, alternate],
            cwd=replacement,
            check=True,
        )
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError,
            "replacement refs",
        ):
            BOOTSTRAP.bootstrap_source_readiness(replacement)

        hidden = self.ready_private_repo(self.root / "hidden-index-source")
        subprocess.run(
            ["git", "update-index", "--skip-worktree", "control.txt"],
            cwd=hidden,
            check=True,
        )
        (hidden / "control.txt").write_text("hidden drift\n", encoding="utf-8")
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError,
            "sparse or hidden",
        ):
            BOOTSTRAP.bootstrap_source_readiness(hidden)

        assumed = self.ready_private_repo(self.root / "assume-index-source")
        subprocess.run(
            ["git", "update-index", "--assume-unchanged", "control.txt"],
            cwd=assumed,
            check=True,
        )
        (assumed / "control.txt").write_text("assumed drift\n", encoding="utf-8")
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError,
            "sparse or hidden",
        ):
            BOOTSTRAP.bootstrap_source_readiness(assumed)

    def test_source_readiness_rejects_ambiguous_remote_urls(self) -> None:
        source = self.ready_private_repo(self.root / "multi-url-source")
        subprocess.run(
            [
                "git",
                "remote",
                "set-url",
                "--add",
                "origin",
                BOOTSTRAP.REPOSITORY_SSH_URL,
            ],
            cwd=source,
            check=True,
        )
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError,
            "one canonical",
        ):
            BOOTSTRAP.bootstrap_source_readiness(source)

    def test_source_readiness_rejects_worktree_and_group_writable_clone(self) -> None:
        source = self.ready_private_repo(self.root / "worktree-owner")
        worktree_parent = self.root / "private-worktrees"
        worktree_parent.mkdir(mode=0o700)
        linked = worktree_parent / "linked"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(linked), "HEAD"],
            cwd=source,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError, "standalone private clone"
        ):
            BOOTSTRAP.bootstrap_source_readiness(linked)

        writable = self.ready_private_repo(self.root / "writable-source")
        os.chmod(writable, 0o770)
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError, "standalone private clone"
        ):
            BOOTSTRAP.bootstrap_source_readiness(writable)

    def test_source_readiness_cli_is_read_only(self) -> None:
        source = self.ready_private_repo(self.root / "readiness-cli-source")
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        result, output, error = self.run_main(
            "--sha",
            sha,
            "--check-source-readiness",
            "--source-root",
            str(source),
        )
        self.assertEqual(result, 0, error)
        self.assertTrue(json.loads(output)["ready"])
        self.assertFalse(self.runtime.exists())
        result, _output, error = self.run_main(
            "--sha",
            sha,
            "--check-source-readiness",
            "--source-root",
            str(source),
            "--apply",
        )
        self.assertEqual(result, 2)
        self.assertIn("read-only", error)

    def test_strict_object_verification_rejects_hash_path_mismatch(self) -> None:
        source = self.committed_private_repo(self.root / "corrupt-source")
        blob = subprocess.run(
            ["git", "rev-parse", "HEAD:control.txt"],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        object_path = source / ".git/objects" / blob[:2] / blob[2:]
        os.chmod(object_path, 0o600)
        object_path.write_bytes(zlib.compress(b"blob 5\x00evil\n"))
        os.chmod(object_path, 0o400)
        BOOTSTRAP._assert_private_bootstrap_source(source)
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError, "failed strict verification"
        ):
            BOOTSTRAP._verify_git_object_database(source)

    def test_production_hardening_rejects_external_git_storage_before_git(self) -> None:
        marker = self.production / ".git/objects/info/alternates"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("/tmp/untrusted-objects\n", encoding="utf-8")
        os.chmod(marker, 0o600)
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError, "forbidden external storage"
        ):
            BOOTSTRAP._harden_checkout(self.production)

    def test_github_request_ignores_proxy_keylog_and_rejects_redirects(self) -> None:
        requested_url = f"{BOOTSTRAP.REPOSITORY_API_ROOT}/git/ref/heads/main"
        handlers: list[object] = []

        class Response:
            def __init__(self, url: str) -> None:
                self.url = url

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *_args):  # type: ignore[no-untyped-def]
                return False

            def geturl(self) -> str:
                return self.url

            @staticmethod
            def read(_limit: int) -> bytes:
                return b'{"ok":true}'

        class Opener:
            def __init__(self, response_url: str) -> None:
                self.response_url = response_url

            def open(self, _request, timeout):  # type: ignore[no-untyped-def]
                self.assert_timeout(timeout)
                return Response(self.response_url)

            @staticmethod
            def assert_timeout(timeout: int) -> None:
                if timeout != 30:
                    raise AssertionError(timeout)

        def build_opener(*values):  # type: ignore[no-untyped-def]
            handlers.extend(values)
            return Opener(requested_url)

        keylog = self.root / "tls-keys.log"
        with (
            mock.patch.dict(
                os.environ,
                {
                    "HTTPS_PROXY": "http://127.0.0.1:1",
                    "SSLKEYLOGFILE": str(keylog),
                },
            ),
            mock.patch.object(
                BOOTSTRAP.urllib.request,
                "build_opener",
                side_effect=build_opener,
            ),
        ):
            self.assertEqual(
                BOOTSTRAP._request_github_json(requested_url, "token"),
                {"ok": True},
            )
        proxy = next(
            value
            for value in handlers
            if isinstance(value, BOOTSTRAP.urllib.request.ProxyHandler)
        )
        https = next(
            value
            for value in handlers
            if isinstance(value, BOOTSTRAP.urllib.request.HTTPSHandler)
        )
        self.assertEqual(proxy.proxies, {})
        self.assertIsNone(https._context.keylog_filename)
        self.assertFalse(keylog.exists())

        with mock.patch.object(
            BOOTSTRAP.urllib.request,
            "build_opener",
            return_value=Opener("https://example.invalid/redirect"),
        ), self.assertRaisesRegex(BOOTSTRAP.BootstrapError, "redirected"):
            BOOTSTRAP._request_github_json(requested_url, "token")

    def test_sealed_delivery_gate_revalidates_exact_workflow_attempt(self) -> None:
        run_id = 77
        attempt = 1
        required = list(
            BOOTSTRAP._required_ci_jobs(
                source_sha=SOURCE_SHA,
                allow_test=True,
            )
        )
        self.assertEqual(
            required,
            [
                "Publish and smoke immutable main images",
                "bridge-validation",
                "ci-gate",
            ],
        )
        sealed = {
            "remote_main": SOURCE_SHA,
            "ci": {
                "workflow_run_id": run_id,
                "run_attempt": attempt,
                "head_sha": SOURCE_SHA,
                "head_branch": "main",
                "event": "push",
                "path": ".github/workflows/ci.yml",
                "conclusion": "success",
                "required_jobs": required,
            },
        }
        urls: list[str] = []

        def github(url: str, _token: str) -> dict[str, object]:
            urls.append(url)
            if url.endswith("/git/ref/heads/main"):
                return {
                    "ref": "refs/heads/main",
                    "object": {"type": "commit", "sha": SOURCE_SHA},
                }
            if url.endswith(f"/actions/runs/{run_id}/attempts/{attempt}"):
                return {
                    "id": run_id,
                    "run_attempt": attempt,
                    "head_sha": SOURCE_SHA,
                    "head_branch": "main",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                    "path": ".github/workflows/ci.yml",
                }
            if url.endswith(
                f"/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"
            ):
                return {
                    "jobs": [
                        {"name": name, "conclusion": "success"}
                        for name in required
                    ]
                }
            raise AssertionError(url)

        with (
            mock.patch.object(BOOTSTRAP, "_github_token", return_value="token"),
            mock.patch.object(
                BOOTSTRAP,
                "_required_ci_jobs",
                return_value=tuple(required),
            ),
            mock.patch.object(
                BOOTSTRAP, "_request_github_json", side_effect=github
            ),
        ):
            evidence = BOOTSTRAP._delivery_gate(
                self.production,
                self.runtime,
                SOURCE_SHA,
                allow_test=False,
                sealed=sealed,
            )
        self.assertEqual(evidence, sealed)
        self.assertTrue(any("/attempts/1" in value for value in urls))
        self.assertFalse(any("filter=latest" in value for value in urls))
        for missing in required:
            with self.subTest(missing=missing):
                def incomplete(
                    url: str,
                    token: str,
                    *,
                    omitted: str = missing,
                ) -> dict[str, object]:
                    document = github(url, token)
                    if url.endswith(
                        f"/actions/runs/{run_id}/attempts/{attempt}/jobs"
                        "?per_page=100"
                    ):
                        document = {
                            "jobs": [
                                {"name": name, "conclusion": "success"}
                                for name in required
                                if name != omitted
                            ]
                        }
                    return document

                with (
                    mock.patch.object(
                        BOOTSTRAP,
                        "_github_token",
                        return_value="token",
                    ),
                    mock.patch.object(
                        BOOTSTRAP,
                        "_required_ci_jobs",
                        return_value=tuple(required),
                    ),
                    mock.patch.object(
                        BOOTSTRAP,
                        "_request_github_json",
                        side_effect=incomplete,
                    ),
                    self.assertRaisesRegex(
                        BOOTSTRAP.BootstrapError,
                        "lacks required successful jobs",
                    ),
                ):
                    BOOTSTRAP._delivery_gate(
                        self.production,
                        self.runtime,
                        SOURCE_SHA,
                        allow_test=False,
                        sealed=sealed,
                    )

    def test_apply_rejects_symlink_runtime_root_before_chmod(self) -> None:
        target = self.root / "runtime-target"
        target.mkdir(mode=0o755)
        self.runtime.symlink_to(target, target_is_directory=True)
        before = stat.S_IMODE(target.stat().st_mode)
        result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2)
        self.assertIn("runtime root is unsafe", error)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), before)

    def test_apply_rejects_symlink_deploy_lock(self) -> None:
        state = self.runtime / "state"
        state.mkdir(parents=True, mode=0o700)
        outside = self.root / "outside-lock"
        outside.write_text("unchanged\n", encoding="utf-8")
        os.chmod(outside, 0o600)
        (state / "deploy.lock").symlink_to(outside)
        result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2)
        self.assertTrue(error)
        self.assertEqual(outside.read_text(encoding="utf-8"), "unchanged\n")
        self.assertFalse((self.runtime / "bin").exists())

    def test_content_release_is_never_overwritten_and_tampering_fails_closed(self) -> None:
        first, output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(first, 0, error)
        active = json.loads(output)["active_control"]
        controller = (
            self.runtime
            / "control-releases"
            / active["release_id"]
            / "pull_deploy_controller.py"
        )
        controller.write_text("tampered\n", encoding="utf-8")
        os.chmod(controller, 0o700)
        second, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(second, 2)
        self.assertIn("control release", error)

    def test_immutable_router_bytes_must_match_the_reviewed_git_object(self) -> None:
        current = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        with mock.patch.object(BOOTSTRAP, "_safe_source", return_value=b"tampered\n"):
            with self.assertRaisesRegex(BOOTSTRAP.BootstrapError, "reviewed Git object"):
                BOOTSTRAP._read_reviewed_source(
                    "README.md",
                    source_sha=current,
                    allow_test=False,
                )

    def test_locked_delivery_gate_drift_leaves_no_installed_authority(self) -> None:
        first = {
            "remote_main": SOURCE_SHA,
            "ci": {"head_sha": SOURCE_SHA, "conclusion": "success"},
        }
        second = {
            "remote_main": "f" * 40,
            "ci": {"head_sha": "f" * 40, "conclusion": "success"},
        }
        with mock.patch.object(
            BOOTSTRAP, "_delivery_gate", side_effect=[first, second]
        ):
            result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2)
        self.assertIn("delivery evidence changed", error)
        self.assertFalse((self.runtime / "state/active-control.json").exists())
        self.assertEqual(list((self.runtime / "bin").iterdir()), [])

    def test_partial_bootstrap_before_active_pointer_is_safely_resumable(self) -> None:
        first, output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(first, 0, error)
        release_id = json.loads(output)["active_control"]["release_id"]
        (self.runtime / "state/active-control.json").unlink()
        (self.runtime / "state/bootstrap-control.json").unlink()
        second, output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(second, 0, error)
        self.assertEqual(json.loads(output)["active_control"]["release_id"], release_id)

    def test_crash_after_prepared_authority_is_fail_closed_and_resumable(self) -> None:
        original = BOOTSTRAP._atomic_json
        injected = False

        def crash(path: Path, document: dict[str, object]) -> None:
            nonlocal injected
            original(path, document)
            if (
                not injected
                and path.name == "bootstrap-control.json"
                and document.get("status") == "prepared"
            ):
                injected = True
                raise BOOTSTRAP.BootstrapError("injected prepared crash")

        with mock.patch.object(BOOTSTRAP, "_atomic_json", side_effect=crash):
            result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2)
        self.assertIn("injected prepared crash", error)
        self.assertEqual(
            json.loads(
                (self.runtime / "state/bootstrap-control.json").read_text(
                    encoding="utf-8"
                )
            )["status"],
            "prepared",
        )
        self.assertFalse((self.runtime / "state/active-control.json").exists())
        result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 0, error)

    def test_crash_after_active_pointer_is_fail_closed_and_resumable(self) -> None:
        original = BOOTSTRAP._atomic_json
        injected = False

        def crash(path: Path, document: dict[str, object]) -> None:
            nonlocal injected
            original(path, document)
            if not injected and path.name == "active-control.json":
                injected = True
                raise BOOTSTRAP.BootstrapError("injected active crash")

        with mock.patch.object(BOOTSTRAP, "_atomic_json", side_effect=crash):
            result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2)
        self.assertIn("injected active crash", error)
        self.assertTrue((self.runtime / "state/active-control.json").is_file())
        self.assertEqual(
            json.loads(
                (self.runtime / "state/bootstrap-control.json").read_text(
                    encoding="utf-8"
                )
            )["status"],
            "prepared",
        )
        result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 0, error)

    def test_crash_after_completed_authority_is_idempotently_verified(self) -> None:
        original = BOOTSTRAP._atomic_json
        injected = False

        def crash(path: Path, document: dict[str, object]) -> None:
            nonlocal injected
            original(path, document)
            if (
                not injected
                and path.name == "bootstrap-control.json"
                and document.get("status") == "completed"
            ):
                injected = True
                raise BOOTSTRAP.BootstrapError("injected completed crash")

        with mock.patch.object(BOOTSTRAP, "_atomic_json", side_effect=crash):
            result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2)
        self.assertIn("injected completed crash", error)
        self.assertEqual(
            json.loads(
                (self.runtime / "state/bootstrap-control.json").read_text(
                    encoding="utf-8"
                )
            )["status"],
            "completed",
        )
        self.assertTrue((self.runtime / "state/active-control.json").is_file())
        result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 0, error)

    def test_test_mode_cannot_target_real_production_roots(self) -> None:
        result, _output, error = self.run_main(
            "--sha",
            SOURCE_SHA,
            "--apply",
            "--production-root",
            str(BOOTSTRAP.PRODUCTION_ROOT),
            "--runtime-root",
            str(BOOTSTRAP.RUNTIME_ROOT),
            "--confirm-production-root",
            str(BOOTSTRAP.PRODUCTION_ROOT),
            "--confirm-runtime-root",
            str(BOOTSTRAP.RUNTIME_ROOT),
            "--confirm-source-tree",
            SOURCE_TREE,
        )
        self.assertEqual(result, 2)
        self.assertIn("test mode is forbidden", error)

    def test_test_mode_rejects_real_root_subtrees_and_real_unit_derivation(self) -> None:
        result, _output, error = self.run_main(
            "--sha",
            SOURCE_SHA,
            "--production-root",
            str(self.production),
            "--runtime-root",
            str(BOOTSTRAP.RUNTIME_ROOT / "test-child"),
        )
        self.assertEqual(result, 2)
        self.assertIn("test mode is forbidden", error)
        self.assertFalse((BOOTSTRAP.RUNTIME_ROOT / "test-child").exists())

        crafted = BOOTSTRAP.WORKER_UNIT_PATH.parent.parent / "nexpoly-test"
        with mock.patch.object(
            BOOTSTRAP,
            "_worker_unit_path",
            return_value=BOOTSTRAP.WORKER_UNIT_PATH,
        ):
            result, _output, error = self.run_main(
                "--sha",
                SOURCE_SHA,
                "--production-root",
                str(crafted),
                "--runtime-root",
                str(self.runtime),
            )
        self.assertEqual(result, 2)
        self.assertIn("test mode is forbidden", error)

    def test_bootstrap_entrypoint_requires_isolated_fixed_python(self) -> None:
        self.assertEqual(
            SCRIPT.read_text(encoding="utf-8").splitlines()[0],
            "#!/usr/bin/python3 -I",
        )
        hostile = self.root / "hostile-python"
        hostile.mkdir(mode=0o700)
        marker = hostile / "sitecustomize-ran"
        (hostile / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
            encoding="utf-8",
        )
        direct = subprocess.run(
            ["/usr/bin/python3", str(SCRIPT), "--help"],
            env={"PATH": "/usr/bin:/bin"},
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(direct.returncode, 2)
        self.assertIn("isolated Python", direct.stderr)
        self.assertFalse(marker.exists())

        isolated = subprocess.run(
            [str(SCRIPT), "--help"],
            env={"PATH": str(hostile), "PYTHONPATH": str(hostile)},
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(isolated.returncode, 0, isolated.stderr)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
