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


def _make_tree_private(root: Path) -> None:
    for path in root.rglob("*"):
        if not path.is_symlink():
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o077)
    root.chmod(0o700)


class AdoptRuntimePrerequisiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.source = root / "source"
        self.runtime = root / "runtime"
        self.source.mkdir(mode=0o700)
        self.runtime.mkdir(mode=0o700)
        runtime_directories = (
            self.runtime,
            self.runtime / "config",
            self.runtime / "state",
            self.runtime / "audit",
            self.runtime / "audit/adoption",
        )
        for directory in runtime_directories[1:]:
            directory.mkdir(mode=0o700)
        for directory in runtime_directories:
            directory.chmod(0o700)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
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
        _make_tree_private(self.source)
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

    def permission_installer(
        self,
        checkpoint=None,
    ):  # type: ignore[no-untyped-def]
        if not hasattr(self, "production"):
            self.production = Path(self.temporary.name) / "production"
            self.production.mkdir(mode=0o755)
            (self.production / "tracked.txt").write_text("adopted production\n")
            _run(
                self.production,
                "/usr/bin/git",
                "init",
                "--initial-branch=main",
            )
            _run(
                self.production,
                "/usr/bin/git",
                "config",
                "user.name",
                "Permission Test",
            )
            _run(
                self.production,
                "/usr/bin/git",
                "config",
                "user.email",
                "permission@example.invalid",
            )
            _run(self.production, "/usr/bin/git", "add", ".")
            _run(
                self.production,
                "/usr/bin/git",
                "commit",
                "-m",
                "adopted production",
            )
            self.production_sha = _run(
                self.production,
                "/usr/bin/git",
                "rev-parse",
                "HEAD",
            )
            self.production_tree = _run(
                self.production,
                "/usr/bin/git",
                "rev-parse",
                "HEAD^{tree}",
            )
            for path in sorted(
                (self.production / ".git").rglob("*"),
                reverse=True,
            ):
                if path.is_dir():
                    path.chmod(0o755)
                elif path.is_file():
                    path.chmod(0o644)
            (self.production / ".git").chmod(0o755)
            (self.production / "tracked.txt").chmod(0o644)
            self.production.chmod(0o755)

            trust_source = SOURCE_ROOT / "scripts/git_source_trust.py"
            trust_target = self.source / "scripts/git_source_trust.py"
            shutil.copyfile(trust_source, trust_target)
            trust_target.chmod(0o700)
            _run(
                self.source,
                "/usr/bin/git",
                "add",
                "scripts/git_source_trust.py",
            )
            _run(
                self.source,
                "/usr/bin/git",
                "commit",
                "-m",
                "add permission trust policy",
            )
            self.sha = _run(
                self.source, "/usr/bin/git", "rev-parse", "HEAD"
            )
            _run(
                self.source,
                "/usr/bin/git",
                "update-ref",
                "refs/remotes/origin/main",
                self.sha,
            )
            _make_tree_private(self.source)
            self.delivery_gate["remote_main"] = self.sha
            self.delivery_gate["ci"]["head_sha"] = self.sha

            adopted = {
                "schema_version": 1,
                "status": "adopted",
                "authority_kind": ADOPTER.ADOPTION_AUTHORITY_KIND,
                "source_sha": self.production_sha,
                "source_tree": self.production_tree,
            }
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
                        "adopted_deployment_sha256": (
                            ADOPTER._canonical_digest(adopted)
                        ),
                    },
                    sort_keys=True,
                ).encode()
                + b"\n",
                0o600,
            )
            base_plan = self.installer().plan(
                source_sha=self.sha,
                operation_id=self.operation_id,
            )
            self.installer().apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=base_plan["plan_sha256"],
            )
            self.permission_operation_id = (
                "adopt-git-permission-test-0001"
            )
        return ADOPTER.PermissionHardeningInstaller(
            self.source,
            self.runtime,
            production_root=self.production,
            checkpoint=checkpoint,
            delivery_gate_probe=self.installer().delivery_gate_probe,
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
        _make_tree_private(self.source)
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

    def test_permission_plan_is_zero_write_and_apply_binds_marker(self) -> None:
        installer = self.permission_installer()
        production_before = _inventory(self.production)
        runtime_before = _inventory(self.runtime)
        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
        )

        self.assertEqual(_inventory(self.production), production_before)
        self.assertEqual(_inventory(self.runtime), runtime_before)
        self.assertTrue(planned["logical_zero_write"])
        self.assertEqual(
            planned["permission_impact_sha256"],
            planned["plan"]["permission_impact_sha256"],
        )
        self.assertEqual(
            planned["plan"]["production_source"],
            {
                "source_sha": self.production_sha,
                "source_tree": self.production_tree,
            },
        )
        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "impact confirmation differs",
        ):
            installer.apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256="sha256:" + "0" * 64,
            )
        self.assertFalse(installer.permission_marker_path.exists())

        authority = installer.apply(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_permission_impact_sha256=planned[
                "permission_impact_sha256"
            ],
        )
        marker = ADOPTER.GIT_SOURCE_TRUST.verify_repository_permission_takeover(
            self.production,
            installer.permission_marker_path,
        )
        self.assertEqual(authority["status"], "completed")
        self.assertEqual(
            authority["authority_kind"],
            ADOPTER.PERMISSION_AUTHORITY_KIND,
        )
        self.assertEqual(
            authority["permission_marker_sha256"],
            ADOPTER._file_digest(installer.permission_marker_path, mode=0o600),
        )
        self.assertEqual(
            authority["permission_evidence_sha256"],
            marker["evidence_sha256"],
        )
        self.assertEqual(
            authority["permission_inventory_sha256"],
            marker["inventory_sha256"],
        )
        self.assertEqual(stat.S_IMODE(self.production.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE(
                (self.production / "tracked.txt").stat().st_mode
            ),
            0o644,
        )
        self.assertEqual(
            installer.apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            ),
            authority,
        )

    def test_permission_change_intent_without_marker_replays_forward(self) -> None:
        planned = self.permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
        )

        def crash(phase: str) -> None:
            if phase == "permission-change-intent":
                raise RuntimeError("crash before first marker")

        with self.assertRaisesRegex(RuntimeError, "before first marker"):
            self.permission_installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            )
        self.assertFalse(
            self.permission_installer().permission_marker_path.exists()
        )
        self.assertEqual(
            self.permission_installer().plan(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
            ),
            planned,
        )
        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "forward-only",
        ):
            self.permission_installer().abort(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            )
        completed = self.permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_permission_impact_sha256=planned[
                "permission_impact_sha256"
            ],
        )
        self.assertEqual(completed["status"], "completed")

    def test_permission_abort_is_allowed_only_before_change_intent(self) -> None:
        planned = self.permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
        )

        def crash(phase: str) -> None:
            if phase == "permission-intent":
                raise RuntimeError("crash at abortable intent")

        with self.assertRaisesRegex(RuntimeError, "abortable intent"):
            self.permission_installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            )
        aborted = self.permission_installer().abort(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_permission_impact_sha256=planned[
                "permission_impact_sha256"
            ],
        )
        self.assertEqual(aborted["status"], "aborted")
        self.assertFalse(
            self.permission_installer().permission_marker_path.exists()
        )
        self.assertEqual(stat.S_IMODE(self.production.stat().st_mode), 0o755)

    def test_permission_authority_link_crash_replays_create_only(self) -> None:
        planned = self.permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
        )
        crashed = False

        def crash(phase: str) -> None:
            nonlocal crashed
            if phase == "authority-linked" and not crashed:
                crashed = True
                raise RuntimeError("crash after permission authority link")

        with self.assertRaisesRegex(RuntimeError, "after permission authority"):
            self.permission_installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            )
        authority_path = (
            self.runtime / ADOPTER.PERMISSION_AUTHORITY_PATH
        )
        self.assertTrue(authority_path.exists())
        self.assertEqual(authority_path.stat().st_nlink, 2)

        completed = self.permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_permission_impact_sha256=planned[
                "permission_impact_sha256"
            ],
        )
        self.assertTrue(crashed)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(authority_path.stat().st_nlink, 1)

    def test_create_owned_json_once_honors_large_explicit_limit(self) -> None:
        directory = self.runtime / "state"
        directory_fd = ADOPTER._open_private_directory(directory)
        try:
            ADOPTER._create_owned_json_once_at(
                directory_fd,
                "large-authority.json",
                {"payload": "x" * (9 * 1024 * 1024)},
                operation_id=self.operation_id,
                checkpoint=lambda _phase: None,
                maximum_bytes=10 * 1024 * 1024,
            )
        finally:
            os.close(directory_fd)
        authority = directory / "large-authority.json"
        self.assertGreater(authority.stat().st_size, 8 * 1024 * 1024)
        self.assertEqual(authority.stat().st_nlink, 1)

    def test_create_owned_json_once_rejects_oversize_before_staging(self) -> None:
        directory = self.runtime / "state"
        directory_fd = ADOPTER._open_private_directory(directory)
        try:
            with self.assertRaisesRegex(
                ADOPTER.PrerequisiteError,
                "authority is oversized",
            ):
                ADOPTER._create_owned_json_once_at(
                    directory_fd,
                    "oversized-authority.json",
                    {"payload": "x" * 2048},
                    operation_id=self.operation_id,
                    checkpoint=lambda _phase: None,
                    maximum_bytes=1024,
                )
        finally:
            os.close(directory_fd)
        target = directory / "oversized-authority.json"
        staging = directory / (
            f".{target.name}.create-{self.operation_id}"
        )
        quarantine = staging.with_name(staging.name + ".quarantine")
        self.assertFalse(target.exists())
        self.assertFalse(staging.exists())
        self.assertFalse(quarantine.exists())

    def test_permission_state_path_swap_fails_closed_and_same_op_recovers(
        self,
    ) -> None:
        installer = self.permission_installer()
        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
        )
        state = self.runtime / "state"
        displaced = self.runtime / "state-permission-displaced"
        replacement = self.runtime / "state-permission-replacement"
        replacement.mkdir(mode=0o700)
        swapped = False

        def swap(phase: str) -> None:
            nonlocal swapped
            if phase == "permission:captured" and not swapped:
                swapped = True
                os.rename(state, displaced)
                os.rename(replacement, state)

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "hardening did not complete|pinned prerequisite state",
        ):
            self.permission_installer(swap).apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            )
        self.assertTrue(swapped)
        self.assertFalse(
            (self.runtime / ADOPTER.PERMISSION_AUTHORITY_PATH).exists()
        )

        os.rename(state, replacement)
        os.rename(displaced, state)
        completed = self.permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_permission_impact_sha256=planned[
                "permission_impact_sha256"
            ],
        )
        self.assertEqual(completed["status"], "completed")

    def test_permission_production_root_swap_before_publish_recovers(
        self,
    ) -> None:
        installer = self.permission_installer()
        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
        )
        displaced = self.production.parent / "production-permission-displaced"
        replacement = self.production.parent / "production-permission-replacement"
        shutil.copytree(self.production, replacement, copy_function=shutil.copy2)
        swapped = False

        def swap(phase: str) -> None:
            nonlocal swapped
            if phase == "permission-authority-commit-intent" and not swapped:
                swapped = True
                os.rename(self.production, displaced)
                os.rename(replacement, self.production)

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "pinned production permission authority changed",
        ):
            self.permission_installer(swap).apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            )
        self.assertTrue(swapped)
        self.assertFalse(
            (self.runtime / ADOPTER.PERMISSION_AUTHORITY_PATH).exists()
        )

        os.rename(self.production, replacement)
        os.rename(displaced, self.production)
        completed = self.permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_permission_impact_sha256=planned[
                "permission_impact_sha256"
            ],
        )
        self.assertEqual(completed["status"], "completed")

    def test_permission_base_authority_atomic_swap_is_rejected(self) -> None:
        installer = self.permission_installer()
        base_path = self.runtime / ADOPTER.AUTHORITY_PATH
        original_reader = installer._read_adoption_permission_authorities
        original_inode = base_path.stat().st_ino
        replaced = False

        def read_then_replace():  # type: ignore[no-untyped-def]
            nonlocal replaced
            observed = original_reader()
            if not replaced:
                replacement = json.loads(base_path.read_text(encoding="utf-8"))
                replacement["completed_at"] = "2099-01-01T00:00:00Z"
                staging = base_path.parent / ".base-authority-replacement"
                _write_private(
                    staging,
                    json.dumps(replacement, sort_keys=True).encode() + b"\n",
                    0o600,
                )
                os.replace(staging, base_path)
                replaced = True
            return observed

        with mock.patch.object(
            installer,
            "_read_adoption_permission_authorities",
            side_effect=read_then_replace,
        ), self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "authorities changed while validating",
        ):
            installer.plan(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
            )
        self.assertTrue(replaced)
        self.assertNotEqual(base_path.stat().st_ino, original_inode)

    def test_permission_base_authority_in_place_write_is_rejected(self) -> None:
        installer = self.permission_installer()
        base_path = self.runtime / ADOPTER.AUTHORITY_PATH
        original_inode = base_path.stat().st_ino
        original_reader = ADOPTER._descriptor_bytes
        rewritten = False

        def rewrite_open_inode(
            descriptor: int,
            *,
            maximum_bytes: int,
        ) -> bytes:
            nonlocal rewritten
            payload = original_reader(
                descriptor,
                maximum_bytes=maximum_bytes,
            )
            try:
                opened_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            except OSError:
                opened_path = Path("")
            if opened_path == base_path and not rewritten:
                with base_path.open("r+b", buffering=0) as stream:
                    stream.seek(0)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                rewritten = True
            return payload

        with mock.patch.object(
            ADOPTER,
            "_descriptor_bytes",
            side_effect=rewrite_open_inode,
        ), self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "changed while reading",
        ):
            installer.plan(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
            )
        self.assertTrue(rewritten)
        self.assertEqual(base_path.stat().st_ino, original_inode)

    def test_permission_plan_is_restricted_to_raw_manual_adoption(self) -> None:
        installer = self.permission_installer()
        _write_private(
            self.runtime / "state/current-deployment.json",
            b"{}\n",
            0o600,
        )
        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "restricted to raw manual adoption",
        ):
            installer.plan(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
            )
        self.assertFalse(installer.permission_marker_path.exists())

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
