from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "pull_deploy_controller.py"
SPEC = importlib.util.spec_from_file_location(
    "pull_deploy_controller_transition_integration", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
CONTROLLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROLLER)


class ProductionRepositoryTransitionIntegrationTests(unittest.TestCase):
    """Exercise the deployed predecessor's real fetch/update-ref sequence."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="nexpoly-production-transition-"
        )
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.seed = self.root / "seed"
        self.seed.mkdir(mode=0o700)
        self.production = self.root / "production"
        self.production.mkdir(mode=0o700)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.remote = self.root / "remote.git"

        self.environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.home),
            "LANG": "C",
            "LC_ALL": "C",
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            # A file URL forces a real object transfer rather than local
            # hardlinks, preserving the standalone/nlink=1 trust contract.
            "GIT_ALLOW_PROTOCOL": "file",
        }

        self._git(self.root, "init", "--bare", "-b", "main", self.remote)
        self._git(self.seed, "init", "-b", "main")
        self._git(self.seed, "config", "user.name", "Transition Fixture")
        self._git(
            self.seed,
            "config",
            "user.email",
            "transition@example.invalid",
        )
        self._git(self.seed, "remote", "add", "origin", str(self.remote))

        self._write_seed("state.txt", "production B\n")
        self._git(self.seed, "add", "state.txt")
        self._git(self.seed, "commit", "-m", "production B")
        self.production_sha = self._git(self.seed, "rev-parse", "HEAD")
        self.production_tree = self._git(
            self.seed, "rev-parse", "HEAD^{tree}"
        )
        self._git(self.seed, "push", "origin", "main")

        self._git(self.production, "init", "-b", "main")
        self.remote_url = self.remote.resolve().as_uri()
        self._old_fetch("refs/remotes/origin/main")
        self._git(
            self.production,
            "reset",
            "--hard",
            "refs/remotes/origin/main",
        )

        self._write_seed("state.txt", "predecessor authority A\n")
        self._git(self.seed, "add", "state.txt")
        self._git(self.seed, "commit", "-m", "predecessor authority A")
        self.predecessor_sha = self._git(self.seed, "rev-parse", "HEAD")
        self._git(self.seed, "push", "origin", "main")
        self._old_fetch(CONTROLLER.DEPLOY_REMOTE_REF)

        # This deliberately unreachable blob proves the materialized object
        # policy preserves baseline-only objects instead of comparing only the
        # target's reachable closure.
        self.baseline_only_oid = self._git(
            self.production,
            "hash-object",
            "-w",
            "--stdin",
            input_text="baseline-only transition evidence\n",
        )

        self._write_seed("state.txt", "successor authority T\n")
        self._write_seed("successor.txt", "content-addressed target\n")
        self._git(self.seed, "add", "state.txt", "successor.txt")
        self._git(self.seed, "commit", "-m", "successor authority T")
        self.target_sha = self._git(self.seed, "rev-parse", "HEAD")
        self.target_tree = self._git(self.seed, "rev-parse", "HEAD^{tree}")
        self._git(self.seed, "push", "origin", "main")

        self.controller = CONTROLLER.PullDeployController(
            self.production, self.runtime
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(
        self,
        repository: Path,
        *arguments: object,
        input_text: str | None = None,
    ) -> str:
        completed = subprocess.run(
            ["/usr/bin/git", *(str(argument) for argument in arguments)],
            cwd=repository,
            env=self.environment,
            input=input_text,
            stdin=None if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            timeout=120,
            umask=0o077,
        )
        return completed.stdout.strip()

    def _old_fetch(self, destination: str) -> None:
        # Keep this argument sequence aligned with the deployed predecessor.
        self._git(
            self.production,
            "fetch",
            "--no-tags",
            "--prune",
            self.remote_url,
            f"+refs/heads/main:{destination}",
        )

    def _write_seed(self, relative: str, payload: str) -> None:
        path = self.seed / relative
        path.write_text(payload, encoding="utf-8")
        path.chmod(0o600)

    def _snapshot(
        self,
    ) -> tuple[
        dict[str, object],
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
        dict[str, bytes],
        list[dict[str, object]],
    ]:
        return self.controller._production_repository_transition_snapshot(
            source_sha=self.production_sha,
            source_tree=self.production_tree,
        )

    @staticmethod
    def _by_name(records: list[dict[str, object]]) -> dict[str, str]:
        return {
            str(record["name"]): str(record["object_sha"])
            for record in records
        }

    def _semantic_inventory(self, repository: Path) -> list[dict[str, object]]:
        output = self._git(
            repository,
            "cat-file",
            "--batch-all-objects",
            "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        )
        records = []
        for line in output.splitlines():
            oid, object_type, raw_size = line.split(" ")
            records.append(
                {"oid": oid, "type": object_type, "size": int(raw_size)}
            )
        return sorted(records, key=lambda record: str(record["oid"]))

    def _target_closure(self) -> list[dict[str, object]]:
        all_objects = {
            str(record["oid"]): record
            for record in self._semantic_inventory(self.seed)
        }
        object_ids = set(
            self._git(
                self.seed,
                "rev-list",
                "--objects",
                "--no-object-names",
                self.target_sha,
            ).splitlines()
        )
        return [all_objects[oid] for oid in sorted(object_ids)]

    def _transition(
        self,
        evidence: dict[str, object],
        logical: list[dict[str, object]],
        raw: list[dict[str, object]],
        baseline_objects: list[dict[str, object]],
        baseline_auxiliary: list[dict[str, object]],
    ) -> tuple[
        dict[str, object],
        list[dict[str, object]],
        list[dict[str, object]],
    ]:
        target_objects = self._target_closure()
        target_oids = {str(record["oid"]) for record in target_objects}
        baseline_only = [
            record
            for record in baseline_objects
            if str(record["oid"]) not in target_oids
        ]
        expected_objects = sorted(
            [*baseline_only, *target_objects],
            key=lambda record: str(record["oid"]),
        )
        stable = self.controller._production_repository_stable_projection(
            evidence
        )
        transition = {
            "schema_version": 1,
            "policy": CONTROLLER.PRODUCTION_REPOSITORY_TRANSITION_POLICY,
            "source": {
                "sha": self.production_sha,
                "tree": self.production_tree,
            },
            "target": {"sha": self.target_sha, "tree": self.target_tree},
            "baseline_evidence_sha256": evidence["evidence_sha256"],
            "stable_projection": stable,
            "stable_projection_sha256": CONTROLLER.canonical_json_digest(
                stable
            ),
            "logical_refs": logical,
            "logical_refs_sha256": CONTROLLER.canonical_json_digest(logical),
            "raw_ref_inventory": raw,
            "raw_ref_inventory_sha256": CONTROLLER.canonical_json_digest(raw),
            "baseline_auxiliary_inventory": baseline_auxiliary,
            "baseline_auxiliary_inventory_sha256": (
                CONTROLLER.canonical_json_digest(baseline_auxiliary)
            ),
            "baseline_semantic_object_count": len(baseline_objects),
            "baseline_semantic_objects_sha256": (
                CONTROLLER.canonical_json_digest(baseline_objects)
            ),
            "baseline_only_object_count": len(baseline_only),
            "baseline_only_objects_sha256": (
                CONTROLLER.canonical_json_digest(baseline_only)
            ),
            "target_reachable_object_count": len(target_objects),
            "target_reachable_objects_sha256": (
                CONTROLLER.canonical_json_digest(target_objects)
            ),
            "expected_materialized_object_count": len(expected_objects),
            "expected_materialized_objects_sha256": (
                CONTROLLER.canonical_json_digest(expected_objects)
            ),
            "mutable_refs": {
                "deploy_remote": CONTROLLER.DEPLOY_REMOTE_REF,
                "prepared_prefix": CONTROLLER.PREPARED_REF_PREFIX,
            },
            "storage_policy": {
                "standalone": True,
                "promisor": False,
                "alternates": False,
                "replace_refs": 0,
            },
            "auxiliary_policy": CONTROLLER.GIT_AUXILIARY_POLICY,
            "object_storage_policy": CONTROLLER.GIT_OBJECT_STORAGE_POLICY,
            "object_materialization_policy": (
                "strict-fsck-owner-private-content-addressed-target-closure-v1"
            ),
        }
        validated = CONTROLLER.validate_production_repository_transition(
            transition,
            production_root=self.production,
            production_sha=self.production_sha,
            production_tree=self.production_tree,
            target_sha=self.target_sha,
            target_tree=self.target_tree,
            baseline_trust_sha256=str(evidence["evidence_sha256"]),
        )
        return validated, target_objects, expected_objects

    def _assert_fetch_head(self, expected_sha: str) -> bytes:
        path = self.production / ".git/FETCH_HEAD"
        metadata = path.lstat()
        self.assertTrue(stat.S_ISREG(metadata.st_mode))
        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
        self.assertEqual(metadata.st_nlink, 1)
        payload = path.read_bytes()
        lines = payload.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        fields = lines[0].split("\t")
        self.assertEqual(fields[:2], [expected_sha, ""])
        self.assertEqual(len(fields), 3)
        self.assertEqual(
            # FETCH_HEAD records Git's display form and strips a trailing
            # ".git" from the fetched repository name.
            fields[2],
            f"branch 'main' of {self.remote_url.removesuffix('.git')}",
        )
        return payload

    @staticmethod
    def _reflog_transitions(payload: bytes) -> list[tuple[str, str, str]]:
        transitions = []
        for line in payload.decode("utf-8").splitlines():
            identity, message = line.split("\t", 1)
            old_sha, new_sha, _actor = identity.split(" ", 2)
            transitions.append((old_sha, new_sha, message))
        return transitions

    def _verify_auxiliary(
        self,
        transition: dict[str, object],
        observed: list[dict[str, object]],
        payloads: dict[str, bytes],
        *,
        materialized: bool,
        prepared_operation_id: str | None,
    ) -> None:
        # Production remains pinned to the SSH authority.  The integration
        # fixture patches only the two display strings emitted by its local
        # file transport; all structural and exact-byte checks remain active.
        fetch_description = (
            f"branch 'main' of {self.remote_url.removesuffix('.git')}"
        )
        deploy_message = (
            f"fetch --no-tags --prune {self.remote_url} "
            f"+refs/heads/main:{CONTROLLER.DEPLOY_REMOTE_REF}: fast-forward"
        )
        with (
            mock.patch.object(
                CONTROLLER,
                "GIT_FETCH_HEAD_DESCRIPTION",
                fetch_description,
            ),
            mock.patch.object(
                CONTROLLER,
                "GIT_DEPLOY_REFLOG_MESSAGE",
                deploy_message,
            ),
        ):
            self.controller._verify_transition_auxiliary(
                transition,
                observed,
                payloads,
                materialized=materialized,
                target_sha=self.target_sha,
                prepared_operation_id=prepared_operation_id,
            )

    def test_predecessor_fetch_materializes_b_to_p_then_f_and_new_p(
        self,
    ) -> None:
        (
            baseline_evidence,
            baseline_logical,
            baseline_raw,
            baseline_objects,
            baseline_auxiliary,
            baseline_auxiliary_payloads,
            baseline_object_storage,
        ) = self._snapshot()
        transition, target_objects, expected_objects = self._transition(
            baseline_evidence,
            baseline_logical,
            baseline_raw,
            baseline_objects,
            baseline_auxiliary,
        )
        baseline_refs = self._by_name(baseline_logical)
        self.assertEqual(baseline_refs["refs/heads/main"], self.production_sha)
        self.assertEqual(
            baseline_refs[CONTROLLER.DEPLOY_REMOTE_REF], self.predecessor_sha
        )
        self.assertFalse(
            any(
                name.startswith(CONTROLLER.PREPARED_REF_PREFIX)
                for name in baseline_refs
            )
        )
        self.assertIn(
            self.baseline_only_oid,
            {str(record["oid"]) for record in baseline_objects},
        )
        self.assertEqual(
            transition["baseline_only_object_count"], 1
        )
        self.controller._verify_transition_raw_refs(
            transition,
            baseline_raw,
            materialized=False,
            target_sha=self.target_sha,
            prepared_operation_id=None,
        )
        self._verify_auxiliary(
            transition,
            baseline_auxiliary,
            baseline_auxiliary_payloads,
            materialized=False,
            prepared_operation_id=None,
        )

        deploy_reflog = (
            self.production
            / ".git/logs/refs/remotes/nexpoly-deploy/main"
        )
        self._assert_fetch_head(self.predecessor_sha)
        self.assertEqual(
            stat.S_IMODE(deploy_reflog.lstat().st_mode), 0o600
        )
        baseline_deploy_reflog = deploy_reflog.read_bytes()
        self.assertEqual(
            self._reflog_transitions(baseline_deploy_reflog),
            [
                (
                    "0" * 40,
                    self.predecessor_sha,
                    (
                        f"fetch --no-tags --prune {self.remote_url} "
                        f"+refs/heads/main:{CONTROLLER.DEPLOY_REMOTE_REF}: "
                        "storing head"
                    ),
                )
            ],
        )

        first_operation = "deploy-transition-op-0001"
        first_prepared_ref = (
            f"{CONTROLLER.PREPARED_REF_PREFIX}{first_operation}"
        )
        self._old_fetch(CONTROLLER.DEPLOY_REMOTE_REF)
        self._git(
            self.production,
            "update-ref",
            first_prepared_ref,
            self.target_sha,
        )

        (
            p_evidence,
            p_logical,
            p_raw,
            p_objects,
            p_auxiliary,
            p_auxiliary_payloads,
            p_object_storage,
        ) = self._snapshot()
        self.assertEqual(
            self.controller._production_repository_stable_projection(
                p_evidence
            ),
            transition["stable_projection"],
        )
        self.assertEqual(
            p_logical,
            self.controller._transition_expected_logical_refs(
                transition,
                materialized=True,
                target_sha=self.target_sha,
                prepared_operation_id=first_operation,
            ),
        )
        self.controller._verify_transition_raw_refs(
            transition,
            p_raw,
            materialized=True,
            target_sha=self.target_sha,
            prepared_operation_id=first_operation,
        )
        self._verify_auxiliary(
            transition,
            p_auxiliary,
            p_auxiliary_payloads,
            materialized=True,
            prepared_operation_id=first_operation,
        )
        deploy_log_path = f"logs/{CONTROLLER.DEPLOY_REMOTE_REF}"
        tampered_payloads = dict(p_auxiliary_payloads)
        tampered_deploy_log = bytearray(tampered_payloads[deploy_log_path])
        tampered_deploy_log[0] = ord("f")
        tampered_payloads[deploy_log_path] = bytes(tampered_deploy_log)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "deployment remote reflog lost its baseline",
        ):
            self._verify_auxiliary(
                transition,
                p_auxiliary,
                tampered_payloads,
                materialized=True,
                prepared_operation_id=first_operation,
            )
        self.assertEqual(p_objects, expected_objects)
        self.assertNotEqual(p_object_storage, baseline_object_storage)
        self.assertEqual(
            self.controller._target_reachable_object_evidence(
                self.target_sha, self.target_tree, p_objects
            ),
            (
                len(target_objects),
                CONTROLLER.canonical_json_digest(target_objects),
            ),
        )
        fetch_head_at_p = self._assert_fetch_head(self.target_sha)
        deploy_reflog_at_p = deploy_reflog.read_bytes()
        self.assertEqual(
            self._reflog_transitions(deploy_reflog_at_p),
            [
                *self._reflog_transitions(baseline_deploy_reflog),
                (
                    self.predecessor_sha,
                    self.target_sha,
                    (
                        f"fetch --no-tags --prune {self.remote_url} "
                        f"+refs/heads/main:{CONTROLLER.DEPLOY_REMOTE_REF}: "
                        "fast-forward"
                    ),
                ),
            ],
        )
        # Plain update-ref does not opt custom namespaces into reflogs.
        first_prepared_reflog = (
            self.production / ".git/logs" / first_prepared_ref
        )
        self.assertFalse(first_prepared_reflog.exists())

        self._git(
            self.production,
            "update-ref",
            "--no-deref",
            "-d",
            first_prepared_ref,
            self.target_sha,
        )
        (
            f_evidence,
            f_logical,
            f_raw,
            f_objects,
            f_auxiliary,
            f_auxiliary_payloads,
            f_object_storage,
        ) = self._snapshot()
        self.assertEqual(
            self.controller._production_repository_stable_projection(
                f_evidence
            ),
            transition["stable_projection"],
        )
        self.assertEqual(
            f_logical,
            self.controller._transition_expected_logical_refs(
                transition,
                materialized=True,
                target_sha=self.target_sha,
                prepared_operation_id=None,
            ),
        )
        self.controller._verify_transition_raw_refs(
            transition,
            f_raw,
            materialized=True,
            target_sha=self.target_sha,
            prepared_operation_id=None,
        )
        self._verify_auxiliary(
            transition,
            f_auxiliary,
            f_auxiliary_payloads,
            materialized=True,
            prepared_operation_id=None,
        )
        self.assertEqual(f_objects, expected_objects)
        self.assertEqual(f_auxiliary, p_auxiliary)
        self.assertEqual(f_auxiliary_payloads, p_auxiliary_payloads)
        self.assertEqual(f_object_storage, p_object_storage)
        self.assertEqual(self._assert_fetch_head(self.target_sha), fetch_head_at_p)
        self.assertEqual(deploy_reflog.read_bytes(), deploy_reflog_at_p)
        self.assertFalse((self.production / ".git" / first_prepared_ref).exists())
        self.assertFalse(first_prepared_reflog.exists())

        # Record the files-backend behavior explicitly: deleting the final
        # loose ref removes refs/nexpoly/prepared, but leaves the first custom
        # namespace directory present and empty.
        prepared_directory = self.production / ".git/refs/nexpoly/prepared"
        namespace_directory = self.production / ".git/refs/nexpoly"
        self.assertFalse(prepared_directory.exists())
        self.assertTrue(namespace_directory.is_dir())
        self.assertEqual(list(namespace_directory.iterdir()), [])
        self.assertEqual(stat.S_IMODE(namespace_directory.stat().st_mode), 0o700)

        second_operation = "deploy-transition-op-0002"
        second_prepared_ref = (
            f"{CONTROLLER.PREPARED_REF_PREFIX}{second_operation}"
        )
        self._old_fetch(CONTROLLER.DEPLOY_REMOTE_REF)
        self._git(
            self.production,
            "update-ref",
            second_prepared_ref,
            self.target_sha,
        )
        (
            p2_evidence,
            p2_logical,
            p2_raw,
            p2_objects,
            p2_auxiliary,
            p2_auxiliary_payloads,
            p2_object_storage,
        ) = self._snapshot()
        self.assertEqual(
            self.controller._production_repository_stable_projection(
                p2_evidence
            ),
            transition["stable_projection"],
        )
        self.assertEqual(
            p2_logical,
            self.controller._transition_expected_logical_refs(
                transition,
                materialized=True,
                target_sha=self.target_sha,
                prepared_operation_id=second_operation,
            ),
        )
        self.controller._verify_transition_raw_refs(
            transition,
            p2_raw,
            materialized=True,
            target_sha=self.target_sha,
            prepared_operation_id=second_operation,
        )
        self._verify_auxiliary(
            transition,
            p2_auxiliary,
            p2_auxiliary_payloads,
            materialized=True,
            prepared_operation_id=second_operation,
        )
        self.assertEqual(p2_objects, expected_objects)
        self.assertEqual(p2_auxiliary, p_auxiliary)
        self.assertEqual(p2_auxiliary_payloads, p_auxiliary_payloads)
        self.assertEqual(p2_object_storage, p_object_storage)
        self.assertEqual(self._assert_fetch_head(self.target_sha), fetch_head_at_p)
        # A same-SHA fetch rewrites FETCH_HEAD but does not move the tracking
        # ref, so it adds no deploy reflog entry.
        self.assertEqual(deploy_reflog.read_bytes(), deploy_reflog_at_p)
        self.assertFalse(
            (self.production / ".git" / first_prepared_ref).exists()
        )
        self.assertTrue((self.production / ".git" / second_prepared_ref).is_file())
        self.assertFalse(
            (self.production / ".git/logs" / second_prepared_ref).exists()
        )
        self.assertEqual(stat.S_IMODE(prepared_directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(namespace_directory.stat().st_mode), 0o700)

    def test_snapshot_rejects_lock_tmp_pack_and_keep_residue(self) -> None:
        lock_path = self.production / ".git/FETCH_HEAD.lock"
        lock_path.write_bytes(b"interrupted fetch lock\n")
        lock_path.chmod(0o600)
        try:
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "transaction lock remains: FETCH_HEAD.lock",
            ):
                self.controller._production_git_auxiliary_inventory()
            with self.assertRaises(CONTROLLER.PullDeployError):
                self._snapshot()
        finally:
            # Fixture cleanup only.  This is deliberately not evidence of a
            # governed product recovery command for interrupted Git residue.
            lock_path.unlink()
        self._snapshot()

        tmp_pack = self.production / ".git/objects/pack/tmp_pack_successor"
        tmp_pack.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp_pack.write_bytes(b"interrupted pack transfer\n")
        tmp_pack.chmod(0o600)
        try:
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "object file is not canonical: pack/tmp_pack_successor",
            ):
                self.controller._production_git_object_storage_inventory()
            with self.assertRaises(CONTROLLER.PullDeployError):
                self._snapshot()
        finally:
            # Fixture cleanup only; production currently has no equivalent
            # deploy-lock/journal/CAS residue recovery transaction.
            tmp_pack.unlink()
        self._snapshot()

        keep = (
            self.production
            / ".git/objects/pack"
            / f"pack-{'a' * 40}.keep"
        )
        keep.write_bytes(b"interrupted pack keep marker\n")
        keep.chmod(0o600)
        try:
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                rf"object file is not canonical: pack/{keep.name}",
            ):
                self.controller._production_git_object_storage_inventory()
            with self.assertRaises(CONTROLLER.PullDeployError):
                self._snapshot()
        finally:
            keep.unlink()
        recovered = self._snapshot()
        self.assertEqual(
            self._by_name(recovered[1])["refs/heads/main"],
            self.production_sha,
        )


if __name__ == "__main__":
    unittest.main()
