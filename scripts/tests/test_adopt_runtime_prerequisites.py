from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock


SOURCE_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = SOURCE_ROOT / "scripts/adopt_runtime_prerequisites.py"
SPEC = importlib.util.spec_from_file_location(
    "nexpoly_adopt_runtime_prerequisites", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("cannot load prerequisite adopter")
ADOPTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADOPTER)


def _run(directory: Path, *arguments: str) -> str:
    result = subprocess.run(
        list(arguments),
        cwd=directory,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def _write_private(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)


def _inventory(root: Path) -> list[tuple[str, int, str]]:
    result: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            identity = "directory"
        elif stat.S_ISREG(metadata.st_mode):
            identity = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            identity = "special"
        result.append((relative, stat.S_IMODE(metadata.st_mode), identity))
    return result


class AdoptRuntimePrerequisiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.source = root / "source"
        self.runtime = root / "runtime"
        self.source.mkdir(mode=0o700)
        self.runtime.mkdir(mode=0o700)
        for relative in ("config", "state", "audit/adoption"):
            (self.runtime / relative).mkdir(mode=0o700, parents=True)
        _write_private(self.runtime / "state/deploy.lock", b"", 0o600)
        adopted = {"schema_version": 1, "status": "adopted"}
        _write_private(
            self.runtime / "state/adopted-deployment.json",
            json.dumps(adopted, sort_keys=True).encode() + b"\n",
            0o600,
        )
        _write_private(
            self.runtime / "state/bootstrap-control.json",
            json.dumps(
                {
                    "schema_version": 3,
                    "status": "completed",
                    "authority_kind": ADOPTER.ADOPTION_AUTHORITY_KIND,
                    "adopted_deployment": adopted,
                    "adopted_deployment_sha256": ADOPTER._canonical_digest(adopted),
                },
                sort_keys=True,
            ).encode()
            + b"\n",
            0o600,
        )
        self.pgpass_payload = b"127.0.0.1:55432:nexpoly:nexpoly_mutable_audit:secret\n"
        _write_private(
            self.runtime / "config/mutable-data-audit.pgpass",
            self.pgpass_payload,
            0o600,
        )

        for source_path, _name, mode, _classification in ADOPTER.TRACKED_INSTALLS:
            source = SOURCE_ROOT / source_path
            destination = self.source / source_path
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            destination.chmod(mode)
        bootstrap_source = SOURCE_ROOT / "scripts/bootstrap_pull_deploy.py"
        bootstrap_destination = self.source / "scripts/bootstrap_pull_deploy.py"
        bootstrap_destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(bootstrap_source, bootstrap_destination)
        bootstrap_destination.chmod(0o700)
        _run(self.source, "/usr/bin/git", "init", "--initial-branch=main")
        _run(self.source, "/usr/bin/git", "config", "user.name", "Prerequisite Test")
        _run(
            self.source,
            "/usr/bin/git",
            "config",
            "user.email",
            "prerequisite@example.invalid",
        )
        _run(self.source, "/usr/bin/git", "add", ".")
        _run(self.source, "/usr/bin/git", "commit", "-m", "fixture")
        _run(
            self.source,
            "/usr/bin/git",
            "remote",
            "add",
            "origin",
            ADOPTER.REPOSITORY_SSH_URL,
        )
        _run(
            self.source,
            "/usr/bin/git",
            "update-ref",
            "refs/remotes/origin/main",
            "HEAD",
        )
        for path in self.source.rglob("*"):
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o077)
        self.source.chmod(0o700)
        self.sha = _run(self.source, "/usr/bin/git", "rev-parse", "HEAD")
        self.operation_id = "adopt-prereq-test-0001"
        self.delivery_gate = {
            "remote_main": self.sha,
            "ci": {
                "workflow_run_id": 42,
                "run_attempt": 1,
                "head_sha": self.sha,
                "head_branch": "main",
                "event": "push",
                "path": ".github/workflows/ci.yml",
                "conclusion": "success",
                "required_jobs": ["fixture-gate"],
            },
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def installer(
        self,
        checkpoint=None,
        delivery_probe=None,
        source_readiness_probe=None,
    ):  # type: ignore[no-untyped-def]
        def default_delivery_probe(
            _source: Path,
            _runtime: Path,
            source_sha: str,
            sealed: dict[str, object] | None,
        ) -> dict[str, object]:
            self.assertEqual(source_sha, self.sha)
            if sealed is not None:
                self.assertEqual(sealed, self.delivery_gate)
            return json.loads(json.dumps(self.delivery_gate))

        return ADOPTER.PrerequisiteInstaller(
            self.source,
            self.runtime,
            checkpoint=checkpoint,
            delivery_gate_probe=delivery_probe or default_delivery_probe,
            source_readiness_probe=source_readiness_probe,
        )

    def advance_remote_tracking_ref(self) -> str:
        environment = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Prerequisite Test",
            "GIT_AUTHOR_EMAIL": "prerequisite@example.invalid",
            "GIT_COMMITTER_NAME": "Prerequisite Test",
            "GIT_COMMITTER_EMAIL": "prerequisite@example.invalid",
        }
        advanced = subprocess.run(
            [
                "/usr/bin/git",
                "commit-tree",
                f"{self.sha}^{{tree}}",
                "-p",
                self.sha,
                "-m",
                "advanced protected main",
            ],
            cwd=self.source,
            env=environment,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()
        _run(
            self.source,
            "/usr/bin/git",
            "update-ref",
            "refs/remotes/origin/main",
            advanced,
        )
        return advanced

    def test_plan_is_zero_write_and_deterministic(self) -> None:
        runtime_before = _inventory(self.runtime)
        source_before = _inventory(self.source)
        git_environments: list[dict[str, str]] = []
        original_run = subprocess.run

        def capture_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            command = args[0] if args else kwargs.get("args")
            environment = kwargs.get("env")
            if isinstance(command, list) and "/usr/bin/git" in command and environment:
                git_environments.append(dict(environment))
            return original_run(*args, **kwargs)

        with mock.patch.object(subprocess, "run", side_effect=capture_run):
            first = self.installer().plan(
                source_sha=self.sha, operation_id=self.operation_id
            )
            second = self.installer().plan(
                source_sha=self.sha, operation_id=self.operation_id
            )

        self.assertEqual(first, second)
        self.assertEqual(_inventory(self.runtime), runtime_before)
        self.assertEqual(_inventory(self.source), source_before)
        self.assertTrue(git_environments)
        self.assertTrue(
            all(environment.get("GIT_OPTIONAL_LOCKS") == "0" for environment in git_environments)
        )
        self.assertFalse(first["apply"])
        self.assertIs(first["logical_zero_write"], True)
        self.assertIsInstance(first["atime_zero_write"], bool)
        self.assertEqual(len(first["plan"]["files"]), 10)
        self.assertEqual(
            first["plan"]["preserved_pgpass"]["sha256"],
            "sha256:" + hashlib.sha256(self.pgpass_payload).hexdigest(),
        )
        self.assertNotIn("secret", json.dumps(first))
        self.assertEqual(first["plan"]["delivery_gate"], self.delivery_gate)
        self.assertEqual(first["plan"]["source_readiness"]["ready"], True)

    def test_plan_atime_claim_is_conservative_and_explicit(self) -> None:
        with mock.patch.object(
            ADOPTER, "_mount_suppresses_atime", side_effect=[True, False]
        ):
            unproven = self.installer().plan(
                source_sha=self.sha,
                operation_id=self.operation_id,
            )
        with mock.patch.object(
            ADOPTER, "_mount_suppresses_atime", return_value=True
        ):
            proven = self.installer().plan(
                source_sha=self.sha,
                operation_id=self.operation_id,
            )

        self.assertIs(unproven["logical_zero_write"], True)
        self.assertIs(unproven["atime_zero_write"], False)
        self.assertIs(proven["atime_zero_write"], True)
        self.assertEqual(unproven["plan"], proven["plan"])
        self.assertEqual(unproven["plan_sha256"], proven["plan_sha256"])

    def test_dirty_git_filter_is_rejected_before_any_filter_execution(self) -> None:
        marker = self.source / "filter-executed"
        attributes = self.source / ".gitattributes"
        attributes.write_text("ops/config/bootstrap-quiesce.example filter=evil\n")
        attributes.chmod(0o600)
        _run(
            self.source,
            "/usr/bin/git",
            "config",
            "filter.evil.clean",
            f"/usr/bin/touch {marker}",
        )

        with mock.patch.object(
            ADOPTER.subprocess,
            "run",
            side_effect=AssertionError("Git ran before the pure source gate"),
        ) as git_run:
            with self.assertRaisesRegex(
                ADOPTER.PrerequisiteError,
                "executable or unsupported Git policy|executable Git attribute",
            ):
                self.installer().plan(
                    source_sha=self.sha,
                    operation_id=self.operation_id,
                )

        git_run.assert_not_called()
        self.assertFalse(marker.exists())

    def test_source_policy_is_regated_before_delivery_contract_git(self) -> None:
        clean = self.installer().plan(
            source_sha=self.sha,
            operation_id=self.operation_id,
        )
        marker = self.source / "delivery-filter-executed"
        delivery_called = False

        def mutate_after_readiness(
            _source_root: Path,
            _source_sha: str,
        ) -> dict[str, object]:
            attributes = self.source / ".gitattributes"
            attributes.write_text(
                "ops/config/bootstrap-quiesce.example filter=delivery-evil\n"
            )
            attributes.chmod(0o600)
            _run(
                self.source,
                "/usr/bin/git",
                "config",
                "filter.delivery-evil.clean",
                f"/usr/bin/touch {marker}",
            )
            return json.loads(json.dumps(clean["plan"]["source_readiness"]))

        def forbidden_delivery(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            nonlocal delivery_called
            delivery_called = True
            return json.loads(json.dumps(self.delivery_gate))

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "executable or unsupported Git policy|executable Git attribute",
        ):
            self.installer(
                delivery_probe=forbidden_delivery,
                source_readiness_probe=mutate_after_readiness,
            ).plan(
                source_sha=self.sha,
                operation_id=self.operation_id,
            )

        self.assertFalse(delivery_called)
        self.assertFalse(marker.exists())

    def test_apply_is_create_only_idempotent_and_preserves_pgpass(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )
        applied = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        first_inventory = _inventory(self.runtime)
        replayed = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )

        self.assertEqual(replayed, applied)
        self.assertEqual(_inventory(self.runtime), first_inventory)
        self.assertEqual(
            (self.runtime / "config/mutable-data-audit.pgpass").read_bytes(),
            self.pgpass_payload,
        )
        self.assertEqual(applied["authority_kind"], ADOPTER.AUTHORITY_KIND)
        for record in applied["plan"]["files"]:
            target = Path(record["destination"])
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), int(record["mode"], 8))
            self.assertEqual(ADOPTER._file_digest(target), record["sha256"])
        self.assertEqual(
            self.installer().plan(
                source_sha=self.sha, operation_id=self.operation_id
            ),
            planned,
        )

    def test_locked_apply_recomputes_plan_after_target_race(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )
        first = planned["plan"]["files"][0]
        target = Path(first["destination"])

        def race(phase: str) -> None:
            if phase == "apply-lock-acquired":
                _write_private(
                    target,
                    (self.source / first["source_path"]).read_bytes(),
                    int(first["mode"], 8),
                )

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "plan changed before locked apply"
        ):
            self.installer(race).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        self.assertTrue(target.exists())
        self.assertFalse(
            (self.runtime / ADOPTER.TRANSACTION_DIRECTORY).exists()
        )

    def test_eexist_after_intent_never_acquires_or_aborts_foreign_target(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )
        first = planned["plan"]["files"][0]
        target = Path(first["destination"])
        raced = False

        def race(phase: str) -> None:
            nonlocal raced
            if phase == "install-intent:bootstrap-quiesce" and not raced:
                raced = True
                _write_private(
                    target,
                    (self.source / first["source_path"]).read_bytes(),
                    int(first["mode"], 8),
                )

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "without operation ownership"
        ):
            self.installer(race).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        foreign_identity = (target.stat().st_dev, target.stat().st_ino)
        aborted = self.installer().abort(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(aborted["status"], "aborted")
        self.assertEqual((target.stat().st_dev, target.stat().st_ino), foreign_identity)

    def test_distinct_operation_cannot_claim_completed_authority(self) -> None:
        other_operation = "adopt-prereq-test-0002"
        first = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )
        second = self.installer().plan(
            source_sha=self.sha, operation_id=other_operation
        )
        authority = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=first["plan_sha256"],
        )

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "another authority"
        ):
            self.installer().apply(
                source_sha=self.sha,
                operation_id=other_operation,
                confirm_plan_sha256=second["plan_sha256"],
            )
        self.assertEqual(
            json.loads((self.runtime / ADOPTER.AUTHORITY_PATH).read_text()), authority
        )

    def test_abort_removes_only_operation_created_exact_files(self) -> None:
        existing_record = ADOPTER.TRACKED_INSTALLS[4]
        source_path, name, mode, _classification = existing_record
        existing = self.runtime / "config" / name
        _write_private(existing, (self.source / source_path).read_bytes(), mode)
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )

        def crash(phase: str) -> None:
            if phase == "target-created:bootstrap-quiesce":
                raise RuntimeError("injected crash")

        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            self.installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        aborted = self.installer().abort(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )

        self.assertEqual(aborted["status"], "aborted")
        self.assertFalse((self.runtime / "config/bootstrap-quiesce").exists())
        self.assertTrue(existing.exists())
        self.assertEqual(
            (self.runtime / "config/mutable-data-audit.pgpass").read_bytes(),
            self.pgpass_payload,
        )

    def test_abort_refuses_cas_drift(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )

        def crash(phase: str) -> None:
            if phase == "target-created:bootstrap-quiesce":
                raise RuntimeError("injected crash")

        with self.assertRaises(RuntimeError):
            self.installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        target = self.runtime / "config/bootstrap-quiesce"
        target.write_bytes(b"drift\n")
        target.chmod(0o700)
        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "identity differs"
        ):
            self.installer().abort(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )

    def test_abort_quarantine_never_unlinks_substituted_inode(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )

        def crash(phase: str) -> None:
            if phase == "target-created:bootstrap-quiesce":
                raise RuntimeError("injected crash")

        with self.assertRaises(RuntimeError):
            self.installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        target = self.runtime / "config/bootstrap-quiesce"
        replacement = self.runtime / "config/.replacement"
        original_rename = ADOPTER._rename_noreplace
        substituted = False

        def substitute_then_rename(
            source_directory: int,
            source_name: str,
            target_directory: int,
            target_name: str,
        ) -> None:
            nonlocal substituted
            if source_name == "bootstrap-quiesce" and not substituted:
                substituted = True
                _write_private(replacement, target.read_bytes(), 0o700)
                os.replace(replacement, target)
            original_rename(
                source_directory,
                source_name,
                target_directory,
                target_name,
            )

        with mock.patch.object(
            ADOPTER, "_rename_noreplace", side_effect=substitute_then_rename
        ):
            with self.assertRaisesRegex(
                ADOPTER.PrerequisiteError, "raced during quarantine"
            ):
                self.installer().abort(
                    source_sha=self.sha,
                    operation_id=self.operation_id,
                    confirm_plan_sha256=planned["plan_sha256"],
                )
        self.assertTrue(substituted)
        self.assertTrue(target.is_file())
        self.assertEqual(
            ADOPTER._file_digest(target), planned["plan"]["files"][0]["sha256"]
        )

    def test_abort_refuses_completed_authority(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )
        self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        with self.assertRaisesRegex(ADOPTER.PrerequisiteError, "cannot be aborted"):
            self.installer().abort(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )

    def test_authority_commit_intent_is_replayed_and_cannot_abort(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )

        def crash(phase: str) -> None:
            if phase == "authority-commit-intent":
                raise RuntimeError("injected crash")

        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            self.installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        self.assertFalse((self.runtime / ADOPTER.AUTHORITY_PATH).exists())
        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "authority commit cannot be aborted"
        ):
            self.installer().abort(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )

        authority = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(authority["status"], "completed")
        self.assertEqual(
            json.loads((self.runtime / ADOPTER.AUTHORITY_PATH).read_text()),
            authority,
        )
        replayed = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(replayed, authority)

    def test_replay_uses_sealed_local_evidence_after_remote_main_advances(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )

        def crash(phase: str) -> None:
            if phase == "target-created:bootstrap-quiesce":
                raise RuntimeError("injected crash")

        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            self.installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        advanced = self.advance_remote_tracking_ref()
        self.assertNotEqual(advanced, self.sha)

        def forbidden_probe(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("durable replay called a mutable source probe")

        authority = self.installer(
            delivery_probe=forbidden_probe,
            source_readiness_probe=forbidden_probe,
        ).apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(authority["status"], "completed")
        self.assertEqual(authority["plan"]["delivery_gate"], self.delivery_gate)

    def test_abort_uses_sealed_local_evidence_after_remote_main_advances(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )

        def crash(phase: str) -> None:
            if phase == "target-created:bootstrap-quiesce":
                raise RuntimeError("injected crash")

        with self.assertRaises(RuntimeError):
            self.installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        self.advance_remote_tracking_ref()

        def forbidden_probe(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("durable abort called a mutable source probe")

        aborted = self.installer(
            delivery_probe=forbidden_probe,
            source_readiness_probe=forbidden_probe,
        ).abort(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(aborted["status"], "aborted")

    def test_partial_target_staging_write_is_recovered(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )

        def crash(phase: str) -> None:
            if phase == "install-intent:bootstrap-quiesce":
                raise RuntimeError("injected crash")

        with self.assertRaises(RuntimeError):
            self.installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        temporary = self.runtime / "config" / (
            f".adopt-prereq-{self.operation_id}-bootstrap-quiesce.tmp"
        )
        _write_private(temporary, b"partial staging write", 0o700)

        authority = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(authority["status"], "completed")
        self.assertFalse(temporary.exists())
        self.assertFalse(
            temporary.with_name(
                f".adopt-prereq-{self.operation_id}-bootstrap-quiesce.staging-quarantine"
            ).exists()
        )

    def test_transaction_temporary_quarantine_is_recovered(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )

        def crash(phase: str) -> None:
            if phase == "install-intent:bootstrap-quiesce":
                raise RuntimeError("injected crash")

        with self.assertRaises(RuntimeError):
            self.installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        transaction = self.runtime / ADOPTER.TRANSACTION_DIRECTORY / (
            f"{self.operation_id}.json"
        )
        temporary = transaction.with_name(f".{transaction.name}.tmp")
        quarantine = transaction.with_name(f".{transaction.name}.tmp.quarantine")
        _write_private(temporary, b"partial journal", 0o600)
        os.rename(temporary, quarantine)

        authority = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(authority["status"], "completed")
        self.assertFalse(temporary.exists())
        self.assertFalse(quarantine.exists())

    def test_staging_quarantine_sigkill_window_is_replayed(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )
        original_rename = ADOPTER._rename_noreplace
        crashed = False

        def rename_then_crash(
            source_directory: int,
            source_name: str,
            target_directory: int,
            target_name: str,
        ) -> None:
            nonlocal crashed
            original_rename(
                source_directory,
                source_name,
                target_directory,
                target_name,
            )
            if target_name.endswith("bootstrap-quiesce.staging-quarantine") and not crashed:
                crashed = True
                raise RuntimeError("injected sigkill window")

        with mock.patch.object(
            ADOPTER, "_rename_noreplace", side_effect=rename_then_crash
        ):
            with self.assertRaisesRegex(RuntimeError, "sigkill window"):
                self.installer().apply(
                    source_sha=self.sha,
                    operation_id=self.operation_id,
                    confirm_plan_sha256=planned["plan_sha256"],
                )
        quarantine = self.runtime / "config" / (
            f".adopt-prereq-{self.operation_id}-bootstrap-quiesce.staging-quarantine"
        )
        target = self.runtime / "config/bootstrap-quiesce"
        self.assertTrue(quarantine.exists())
        self.assertEqual(target.stat().st_nlink, 2)

        authority = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(authority["status"], "completed")
        self.assertFalse(quarantine.exists())
        self.assertEqual(target.stat().st_nlink, 1)

    def test_abort_target_quarantine_sigkill_window_is_replayed(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )

        def crash_after_ownership(phase: str) -> None:
            if phase == "ownership-recorded:bootstrap-quiesce":
                raise RuntimeError("injected apply crash")

        with self.assertRaises(RuntimeError):
            self.installer(crash_after_ownership).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        original_rename = ADOPTER._rename_noreplace
        crashed = False

        def rename_then_crash(
            source_directory: int,
            source_name: str,
            target_directory: int,
            target_name: str,
        ) -> None:
            nonlocal crashed
            original_rename(
                source_directory,
                source_name,
                target_directory,
                target_name,
            )
            if target_name.endswith("bootstrap-quiesce.abort-target") and not crashed:
                crashed = True
                raise RuntimeError("injected abort sigkill window")

        with mock.patch.object(
            ADOPTER, "_rename_noreplace", side_effect=rename_then_crash
        ):
            with self.assertRaisesRegex(RuntimeError, "abort sigkill window"):
                self.installer().abort(
                    source_sha=self.sha,
                    operation_id=self.operation_id,
                    confirm_plan_sha256=planned["plan_sha256"],
                )
        target = self.runtime / "config/bootstrap-quiesce"
        quarantine = self.runtime / "config" / (
            f".adopt-prereq-{self.operation_id}-bootstrap-quiesce.abort-target"
        )
        self.assertFalse(target.exists())
        self.assertTrue(quarantine.exists())

        aborted = self.installer().abort(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(aborted["status"], "aborted")
        self.assertFalse(target.exists())
        self.assertFalse(quarantine.exists())

    def test_authority_hardlink_sigkill_window_is_replayed(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )

        def crash(phase: str) -> None:
            if phase == "authority-linked":
                raise RuntimeError("injected authority link crash")

        with self.assertRaisesRegex(RuntimeError, "authority link crash"):
            self.installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        authority_path = self.runtime / ADOPTER.AUTHORITY_PATH
        staging = authority_path.with_name(
            f".{authority_path.name}.create-{self.operation_id}"
        )
        self.assertTrue(authority_path.exists())
        self.assertTrue(staging.exists())
        self.assertEqual(authority_path.stat().st_ino, staging.stat().st_ino)
        self.assertEqual(authority_path.stat().st_nlink, 2)

        authority = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(authority["status"], "completed")
        self.assertFalse(staging.exists())
        self.assertEqual(authority_path.stat().st_nlink, 1)

    def test_existing_exact_target_with_extra_hardlink_is_rejected(self) -> None:
        source_path, name, mode, _classification = ADOPTER.TRACKED_INSTALLS[0]
        target = self.runtime / "config" / name
        _write_private(target, (self.source / source_path).read_bytes(), mode)
        os.link(target, self.runtime / "config/.unowned-hardlink")

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "unsafe|identity differs"
        ):
            self.installer().plan(
                source_sha=self.sha,
                operation_id=self.operation_id,
            )

    def test_deploy_lock_symlink_and_hardlink_are_rejected(self) -> None:
        lock = self.runtime / "state/deploy.lock"
        backing = self.runtime / "state/.foreign-lock"
        _write_private(backing, b"", 0o600)
        lock.unlink()
        lock.symlink_to(backing)
        with self.assertRaises(ADOPTER.PrerequisiteError):
            self.installer().plan(
                source_sha=self.sha,
                operation_id=self.operation_id,
            )

        lock.unlink()
        os.link(backing, lock)
        with self.assertRaises(ADOPTER.PrerequisiteError):
            self.installer().plan(
                source_sha=self.sha,
                operation_id=self.operation_id,
            )

    def test_deploy_lock_path_swap_after_flock_is_rejected(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )
        lock = self.runtime / "state/deploy.lock"
        replacement = self.runtime / "state/.replacement-lock"

        def swap(phase: str) -> None:
            if phase == "apply-lock-acquired":
                _write_private(replacement, b"", 0o600)
                os.replace(replacement, lock)

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "deploy lock changed"
        ):
            self.installer(swap).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )

    def test_config_directory_path_swap_after_flock_is_rejected(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )
        config = self.runtime / "config"
        displaced = self.runtime / "config-displaced"

        def swap(phase: str) -> None:
            if phase == "apply-lock-acquired":
                os.rename(config, displaced)
                config.mkdir(mode=0o700)

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "config directory changed"
        ):
            self.installer(swap).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )


if __name__ == "__main__":
    unittest.main()
