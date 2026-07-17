from __future__ import annotations

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/legacy_takeover.py"
SPEC = importlib.util.spec_from_file_location("legacy_takeover_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
TAKEOVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TAKEOVER)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64
DIGEST_E = "sha256:" + "e" * 64
DIGEST_F = "sha256:" + "f" * 64
OPERATION_ID = "takeover-fixture-0001"


class InjectedCrash(RuntimeError):
    pass


class Checkpoints:
    def __init__(self, crash_at: str | None = None):
        self.labels: list[str] = []
        self.crash_at = crash_at
        self.crashed = False

    def __call__(self, label: str) -> None:
        self.labels.append(label)
        if label == self.crash_at and not self.crashed:
            self.crashed = True
            raise InjectedCrash(label)


def runtime_status(
    worker_unit_path: Path,
    worker_unit_sha256: str,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "legacy_runtime_state": "open",
        "backend_image_id": DIGEST_A,
        "web_image_id": DIGEST_B,
        "worker_unit_sha256": worker_unit_sha256,
        "backend_container_id": "d" * 64,
        "web_container_id": "e" * 64,
        "backend_process_spec_sha256": DIGEST_D,
        "web_process_spec_sha256": DIGEST_E,
        "worker_unit_name": "nexpoly-monomer-md.service",
        "worker_unit_path": str(worker_unit_path),
        "worker_unit_mode": "0664",
        "worker_unit_uid": os.geteuid(),
        "worker_unit_gid": os.getegid(),
        "worker_manager_uid": os.geteuid(),
        "worker_manager_runtime_dir": f"/run/user/{os.geteuid()}",
        "worker_manager_environment_sha256": DIGEST_F,
        "postgres_container_id": "a" * 64,
        "postgres_image_id": DIGEST_C,
        "postgres_data_volume": "nexpoly_pg_data",
        "postgres_system_identifier": "7659245354718314530",
        "backend_pid": 123,
        "web_pid": 234,
        "backend_started_at": "2026-07-17T00:00:00Z",
        "web_started_at": "2026-07-17T00:00:01Z",
        "backend_restart_count": 0,
        "web_restart_count": 0,
        "worker_main_pid": 456,
        "worker_invocation_id": "fixture-worker",
        "worker_active_enter_monotonic": 789,
        "backend_healthy": True,
        "web_healthy": True,
        "worker_healthy": True,
        "ingress_open": True,
    }


class FakeSystem:
    def __init__(
        self,
        repository: Path,
        runtime_root: Path,
        classified_paths: list[str],
        worker_unit_path: Path,
    ):
        self.repository = repository
        self.runtime_root = runtime_root
        self.classified_paths = classified_paths
        self.origin = TAKEOVER.REPOSITORY_HTTPS_URL
        self.worker_unit_path = worker_unit_path
        self.status_document = runtime_status(
            worker_unit_path,
            TAKEOVER.sha256_file(worker_unit_path),
        )
        self.original_status = dict(self.status_document)
        self.backend_running = True
        self.web_running = True
        self.worker_running = True
        self.dirty = False
        self.head_sha = "1" * 40
        self.head_tree = "2" * 40
        self.local_main_sha = self.head_sha
        self.branch = "refs/heads/main"
        self.actions: list[str] = []

    def origin_urls(self) -> tuple[list[str], list[str]]:
        return [self.origin], [self.origin]

    def switch_origin(self, expected: str, target: str) -> None:
        if self.origin not in {expected, target}:
            raise TAKEOVER.LegacyTakeoverError("origin CAS failed")
        self.origin = target
        self.actions.append(f"origin:{target}")

    def ignored_paths(self) -> list[str]:
        return sorted(
            (
                value
                for value in self.classified_paths
                if (self.repository / value).exists()
                or (self.repository / value).is_symlink()
            ),
            key=os.fsencode,
        )

    def worktree_clean(self) -> bool:
        return not self.dirty

    def git_identity(self) -> dict[str, str]:
        return {
            "branch": self.branch,
            "head_sha": self.head_sha,
            "head_tree": self.head_tree,
            "local_main_sha": self.local_main_sha,
        }

    def helper_report(self) -> dict[str, object]:
        return TAKEOVER.SITE_HELPERS.inspect_helper_installation(
            self.runtime_root
        )

    def legacy_status(self) -> dict[str, object]:
        return dict(self.status_document)

    def drain(self) -> dict[str, object]:
        self.status_document["legacy_runtime_state"] = "isolated"
        self.status_document["web_healthy"] = False
        self.status_document["ingress_open"] = False
        self.status_document["web_pid"] = None
        self.status_document["web_started_at"] = None
        self.status_document["web_restart_count"] = None
        self.web_running = False
        self.actions.append("drain")
        jobs = {
            name: 0 for name in TAKEOVER.SITE_HELPERS.ACTIVE_JOB_FIELDS_V2
        }
        return {
            "active_jobs_schema_version": 2,
            "ingress_isolated": True,
            "active_jobs": jobs,
            "active_total": 0,
        }

    def stop_container(
        self,
        role: str,
        runtime: dict[str, object],
    ) -> None:
        self.assert_exact_runtime(runtime)
        if role == "web":
            self.web_running = False
        elif role == "backend":
            self.backend_running = False
        else:
            raise AssertionError(role)
        self.actions.append(f"stop:{role}")

    def stop_worker(self, runtime: dict[str, object]) -> None:
        self.assert_exact_runtime(runtime)
        self.worker_running = False
        self.status_document["legacy_runtime_state"] = "stopped"
        for name in (
            "backend_pid",
            "web_pid",
            "backend_started_at",
            "web_started_at",
            "backend_restart_count",
            "web_restart_count",
            "worker_main_pid",
            "worker_invocation_id",
            "worker_active_enter_monotonic",
        ):
            self.status_document[name] = None
        for name in (
            "backend_healthy",
            "web_healthy",
            "worker_healthy",
            "ingress_open",
        ):
            self.status_document[name] = False
        self.actions.append("stop:worker")

    def assert_runtime_stopped(
        self,
        runtime: dict[str, object],
    ) -> dict[str, object]:
        self.assert_exact_runtime(runtime)
        if self.backend_running or self.web_running or self.worker_running:
            raise TAKEOVER.LegacyTakeoverError(
                "sealed runtime restarted during takeover"
            )
        return {
            "schema_version": 1,
            "readers_stopped": True,
            "postgres_running_untouched": True,
            "backend_container_id": runtime["backend_container_id"],
            "backend_image_id": runtime["backend_image_id"],
            "web_container_id": runtime["web_container_id"],
            "web_image_id": runtime["web_image_id"],
            "worker_unit_name": runtime["worker_unit_name"],
            "worker_unit_sha256": runtime["worker_unit_sha256"],
            "worker_manager_uid": runtime["worker_manager_uid"],
            "postgres_container_id": runtime["postgres_container_id"],
            "postgres_image_id": runtime["postgres_image_id"],
            "postgres_data_volume": runtime["postgres_data_volume"],
            "postgres_system_identifier": runtime[
                "postgres_system_identifier"
            ],
        }

    def assert_exact_runtime(self, runtime: dict[str, object]) -> None:
        for name in (
            "backend_container_id",
            "web_container_id",
            "backend_image_id",
            "web_image_id",
            "backend_process_spec_sha256",
            "web_process_spec_sha256",
            "worker_unit_name",
            "worker_unit_sha256",
            "worker_unit_path",
            "worker_unit_mode",
            "worker_unit_uid",
            "worker_unit_gid",
            "worker_manager_uid",
            "worker_manager_runtime_dir",
            "worker_manager_environment_sha256",
            "postgres_container_id",
            "postgres_image_id",
            "postgres_data_volume",
            "postgres_system_identifier",
            "worker_main_pid",
            "worker_invocation_id",
        ):
            if runtime[name] != self.original_status[name]:
                raise AssertionError(name)

    def restore_runtime(
        self,
        runtime: dict[str, object],
    ) -> dict[str, object]:
        self.assert_exact_runtime(runtime)
        self.backend_running = True
        self.web_running = True
        self.worker_running = True
        self.status_document = runtime_status(
            self.worker_unit_path,
            TAKEOVER.sha256_file(self.worker_unit_path),
        )
        self.actions.append("restore-runtime")
        evidence = {
            key: value
            for key, value in self.status_document.items()
            if key not in {"legacy_runtime_state", "ingress_open"}
        }
        evidence.update(
            {
                "legacy_runtime_restored": True,
                "backend_healthy": True,
                "web_healthy": True,
                "worker_healthy": True,
                "ingress_restored": True,
            }
        )
        return evidence

    def reload_worker_manager(self, runtime: dict[str, object]) -> None:
        self.assert_exact_runtime(runtime)
        if TAKEOVER.sha256_file(self.worker_unit_path) != runtime[
            "worker_unit_sha256"
        ]:
            raise TAKEOVER.LegacyTakeoverError(
                "Worker unit was not restored"
            )
        self.actions.append("worker-daemon-reload")


class Fixture:
    PATHS = [
        {"path": "runtime-cache", "class": "runtime"},
        {"path": "secret.env", "class": "secret"},
        {"path": "asset-current", "class": "asset"},
    ]

    def __init__(self, checkpoint: Checkpoints | None = None):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="legacy-takeover-test-"
        )
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.repository = self.root / "repo"
        self.runtime = self.root / "runtime"
        self.config = self.runtime / "config"
        self.repository.mkdir(mode=0o700)
        (self.repository / ".git").mkdir(mode=0o700)
        self.config.mkdir(parents=True, mode=0o700)
        os.chmod(self.runtime, 0o700)
        for name in TAKEOVER.SITE_HELPERS.HELPERS:
            helper = self.config / name
            helper.write_text(f"#!/bin/sh\n# {name}\n", encoding="utf-8")
            os.chmod(helper, 0o700)
        self.worker_unit = self.runtime / "nexpoly-monomer-md.service"
        self.worker_unit.write_text(
            "[Service]\nExecStart=/usr/bin/true\n",
            encoding="utf-8",
        )
        os.chmod(self.worker_unit, 0o664)

        cache = self.repository / "runtime-cache"
        cache.mkdir(mode=0o750)
        (cache / "journal.json").write_text('{"job":1}\n', encoding="utf-8")
        os.chmod(cache / "journal.json", 0o640)
        secret = self.repository / "secret.env"
        secret.write_text("TOKEN=fixture-not-a-secret\n", encoding="utf-8")
        os.chmod(secret, 0o600)
        target = self.repository / "asset-release"
        target.mkdir(mode=0o755)
        (target / "manifest").write_text("fixture\n", encoding="utf-8")
        (self.repository / "asset-current").symlink_to("asset-release")

        classification = {
            "schema_version": 1,
            "review_id": "review:fixture-20260717",
            "paths": self.PATHS,
        }
        self.classification = (
            self.config / "legacy-takeover-classification.json"
        )
        self.classification.write_text(
            json.dumps(classification, sort_keys=True),
            encoding="utf-8",
        )
        os.chmod(self.classification, 0o600)
        self.classification_digest = TAKEOVER.sha256_file(self.classification)
        self.external = {
            category: self.root / "external" / category
            for category in TAKEOVER.EXTERNAL_ROOTS
        }
        self.system = FakeSystem(
            self.repository,
            self.runtime,
            [record["path"] for record in self.PATHS],
            self.worker_unit,
        )
        self.checkpoint = checkpoint or Checkpoints()
        self.controller = TAKEOVER.LegacyTakeover(
            repository=self.repository,
            runtime_root=self.runtime,
            external_roots=self.external,
            system=self.system,
            checkpoint=self.checkpoint,
        )

    def close(self) -> None:
        self.temporary.cleanup()

    def recreate_controller(
        self,
        checkpoint: Checkpoints | None = None,
    ) -> TAKEOVER.LegacyTakeover:
        return TAKEOVER.LegacyTakeover(
            repository=self.repository,
            runtime_root=self.runtime,
            external_roots=self.external,
            system=self.system,
            checkpoint=checkpoint,
        )

    def seal(self) -> dict[str, object]:
        return self.controller.seal(
            OPERATION_ID,
            self.classification_digest,
        )

    def assert_applied(self) -> None:
        self.assertEqual(self.system.origin, TAKEOVER.REPOSITORY_SSH_URL)
        self.assertFalse(self.system.ignored_paths())
        self.assertFalse(self.system.backend_running)
        self.assertFalse(self.system.web_running)
        self.assertFalse(self.system.worker_running)
        for record in self.PATHS:
            source = self.repository / record["path"]
            destination = (
                self.external[record["class"]]
                / OPERATION_ID
                / record["path"]
            )
            self.assertFalse(source.exists() or source.is_symlink())
            self.assertTrue(destination.exists() or destination.is_symlink())

    def assert_restored(self) -> None:
        self.assertEqual(self.system.origin, TAKEOVER.REPOSITORY_HTTPS_URL)
        self.assertEqual(
            self.system.ignored_paths(),
            sorted((record["path"] for record in self.PATHS), key=os.fsencode),
        )
        self.assertTrue(self.system.backend_running)
        self.assertTrue(self.system.web_running)
        self.assertTrue(self.system.worker_running)
        for record in self.PATHS:
            source = self.repository / record["path"]
            destination = (
                self.external[record["class"]]
                / OPERATION_ID
                / record["path"]
            )
            self.assertTrue(source.exists() or source.is_symlink())
            self.assertFalse(destination.exists() or destination.is_symlink())

    # unittest assertions are delegated so fixture validation reads naturally.
    def assertEqual(self, first: object, second: object) -> None:
        if first != second:
            raise AssertionError(f"{first!r} != {second!r}")

    def assertTrue(self, value: object) -> None:
        if not value:
            raise AssertionError(f"expected truthy value, received {value!r}")

    def assertFalse(self, value: object) -> None:
        if value:
            raise AssertionError(f"expected falsey value, received {value!r}")


class LegacyTakeoverTests(unittest.TestCase):
    def labels_for_apply(self) -> list[str]:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        fixture.seal()
        fixture.checkpoint.labels.clear()
        fixture.controller.apply(OPERATION_ID)
        return list(fixture.checkpoint.labels)

    def labels_for_restore(self) -> list[str]:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        fixture.seal()
        fixture.controller.apply(OPERATION_ID)
        fixture.checkpoint.labels.clear()
        fixture.controller.restore(OPERATION_ID)
        return list(fixture.checkpoint.labels)

    def test_full_takeover_and_restore_preserve_exact_path_seals(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        sealed = fixture.seal()
        original_seals = {
            move["path"]: move["seal"] for move in sealed["moves"]
        }
        applied = fixture.controller.apply(OPERATION_ID)
        self.assertEqual(applied["apply_phase"], "complete")
        fixture.assert_applied()
        restored = fixture.controller.restore(OPERATION_ID)
        self.assertEqual(restored["restore_phase"], "restored")
        fixture.assert_restored()
        for relative, seal in original_seals.items():
            TAKEOVER.verify_path_seal(fixture.repository / relative, seal)

    def test_seal_lost_response_is_idempotent(self) -> None:
        checkpoints = Checkpoints("sealed")
        fixture = Fixture(checkpoints)
        self.addCleanup(fixture.close)
        with self.assertRaises(InjectedCrash):
            fixture.seal()
        resumed = fixture.recreate_controller().seal(
            OPERATION_ID,
            fixture.classification_digest,
        )
        self.assertEqual(resumed["apply_phase"], "sealed")

    def test_every_apply_boundary_resumes_after_lost_response(self) -> None:
        labels = self.labels_for_apply()
        self.assertGreater(len(labels), 20)
        for label in labels:
            with self.subTest(label=label):
                checkpoints = Checkpoints(label)
                fixture = Fixture(checkpoints)
                try:
                    fixture.seal()
                    with self.assertRaises(InjectedCrash):
                        fixture.controller.apply(OPERATION_ID)
                    resumed = fixture.recreate_controller()
                    state = resumed.apply(OPERATION_ID)
                    self.assertEqual(state["apply_phase"], "complete")
                    fixture.assert_applied()
                finally:
                    fixture.close()

    def test_restore_is_available_from_every_interrupted_apply_boundary(self) -> None:
        for label in self.labels_for_apply():
            with self.subTest(label=label):
                checkpoints = Checkpoints(label)
                fixture = Fixture(checkpoints)
                try:
                    fixture.seal()
                    with self.assertRaises(InjectedCrash):
                        fixture.controller.apply(OPERATION_ID)
                    restored = fixture.recreate_controller().restore(OPERATION_ID)
                    self.assertEqual(restored["restore_phase"], "restored")
                    fixture.assert_restored()
                finally:
                    fixture.close()

    def test_every_restore_boundary_resumes_after_lost_response(self) -> None:
        labels = self.labels_for_restore()
        self.assertGreater(len(labels), 15)
        for label in labels:
            with self.subTest(label=label):
                checkpoints = Checkpoints(label)
                fixture = Fixture()
                try:
                    fixture.seal()
                    fixture.controller.apply(OPERATION_ID)
                    controller = fixture.recreate_controller(checkpoints)
                    with self.assertRaises(InjectedCrash):
                        controller.restore(OPERATION_ID)
                    state = fixture.recreate_controller().restore(OPERATION_ID)
                    self.assertEqual(state["restore_phase"], "restored")
                    fixture.assert_restored()
                finally:
                    fixture.close()

    def test_classification_rejects_missing_extra_duplicate_and_overlap(self) -> None:
        base = {
            "schema_version": 1,
            "review_id": "review:fixture-20260717",
            "paths": [
                {"path": "cache", "class": "runtime"},
                {"path": "secret.env", "class": "secret"},
            ],
        }
        with self.assertRaisesRegex(
            TAKEOVER.LegacyTakeoverError,
            "cover ignored paths exactly",
        ):
            TAKEOVER.validate_classification(base, ignored_paths=["cache"])
        duplicate = dict(base)
        duplicate["paths"] = [
            {"path": "cache", "class": "runtime"},
            {"path": "cache", "class": "asset"},
        ]
        with self.assertRaisesRegex(
            TAKEOVER.LegacyTakeoverError,
            "more than once",
        ):
            TAKEOVER.validate_classification(
                duplicate,
                ignored_paths=["cache"],
            )
        overlap = dict(base)
        overlap["paths"] = [
            {"path": "cache", "class": "runtime"},
            {"path": "cache/nested", "class": "asset"},
        ]
        with self.assertRaisesRegex(TAKEOVER.LegacyTakeoverError, "overlap"):
            TAKEOVER.validate_classification(
                overlap,
                ignored_paths=["cache", "cache/nested"],
            )

    def test_seal_rejects_origin_runtime_and_classification_drift(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        fixture.system.origin = "https://example.invalid/repository.git"
        with self.assertRaisesRegex(
            TAKEOVER.LegacyTakeoverError,
            "origin identity",
        ):
            fixture.seal()

        fixture.system.origin = TAKEOVER.REPOSITORY_HTTPS_URL
        fixture.system.status_document["web_container_id"] = "f" * 64
        # A changed but internally valid runtime can be sealed; a later change cannot.
        fixture.seal()
        fixture.system.status_document["web_pid"] = 999
        with self.assertRaisesRegex(
            TAKEOVER.LegacyTakeoverError,
            "runtime process identity changed|runtime changed",
        ):
            fixture.controller.apply(OPERATION_ID)

    def test_apply_fails_closed_on_helper_or_source_tampering(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        fixture.seal()
        helper = fixture.config / "bootstrap-status"
        helper.write_text("#!/bin/sh\n# changed\n", encoding="utf-8")
        os.chmod(helper, 0o700)
        with self.assertRaisesRegex(
            TAKEOVER.LegacyTakeoverError,
            "helper installation changed",
        ):
            fixture.controller.apply(OPERATION_ID)

        helper.write_text(
            "#!/bin/sh\n# bootstrap-status\n",
            encoding="utf-8",
        )
        os.chmod(helper, 0o700)
        source = fixture.repository / "secret.env"
        source.write_text("TOKEN=changed\n", encoding="utf-8")
        os.chmod(source, 0o600)
        with self.assertRaisesRegex(
            TAKEOVER.LegacyTakeoverError,
            "no longer matches",
        ):
            fixture.controller.apply(OPERATION_ID)

    def test_externalization_rechecks_stopped_runtime_and_https_origin(self) -> None:
        checkpoints = Checkpoints("externalizing")
        fixture = Fixture(checkpoints)
        self.addCleanup(fixture.close)
        fixture.seal()
        with self.assertRaises(InjectedCrash):
            fixture.controller.apply(OPERATION_ID)
        fixture.system.backend_running = True
        with self.assertRaisesRegex(
            TAKEOVER.LegacyTakeoverError,
            "restarted",
        ):
            fixture.recreate_controller().apply(OPERATION_ID)

        fixture.system.backend_running = False
        fixture.system.origin = TAKEOVER.REPOSITORY_SSH_URL
        with self.assertRaisesRegex(
            TAKEOVER.LegacyTakeoverError,
            "origin failed takeover CAS",
        ):
            fixture.recreate_controller().apply(OPERATION_ID)

    def test_operation_and_private_map_are_cas_bound(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        fixture.seal()
        with self.assertRaisesRegex(
            TAKEOVER.LegacyTakeoverError,
            "private file is unavailable",
        ):
            fixture.controller.apply("takeover-another-0002")
        with self.assertRaisesRegex(
            TAKEOVER.LegacyTakeoverError,
            "classification changed",
        ):
            fixture.controller.seal(OPERATION_ID, "sha256:" + "f" * 64)

        fixture.classification.write_text("{}\n", encoding="utf-8")
        os.chmod(fixture.classification, 0o644)
        with self.assertRaisesRegex(
            TAKEOVER.LegacyTakeoverError,
            "private file is unsafe",
        ):
            fixture.controller.apply(OPERATION_ID)

    def test_seal_and_every_action_bind_clean_main_head_and_tree(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        fixture.system.dirty = True
        with self.assertRaisesRegex(
            TAKEOVER.LegacyTakeoverError,
            "tracked or non-ignored",
        ):
            fixture.seal()

        fixture.system.dirty = False
        fixture.seal()
        fixture.system.head_tree = "3" * 40
        with self.assertRaisesRegex(
            TAKEOVER.LegacyTakeoverError,
            "HEAD/tree/main drifted",
        ):
            fixture.controller.apply(OPERATION_ID)

    def test_phase_checkpoint_drift_is_caught_before_destructive_action(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        fixture.seal()

        def drift(label: str) -> None:
            if label == "web-stop-intent":
                fixture.system.head_sha = "4" * 40
                fixture.system.local_main_sha = "4" * 40

        fixture.controller.checkpoint = drift
        with self.assertRaisesRegex(
            TAKEOVER.LegacyTakeoverError,
            "HEAD/tree/main drifted",
        ):
            fixture.controller.apply(OPERATION_ID)
        self.assertNotIn("stop:web", fixture.system.actions)
        self.assertTrue(fixture.system.backend_running)
        self.assertTrue(fixture.system.worker_running)

    def test_execution_flock_rejects_concurrent_same_operation(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        fixture.seal()
        contender = fixture.recreate_controller()
        with fixture.controller._execution_lock():
            with self.assertRaisesRegex(
                TAKEOVER.LegacyTakeoverError,
                "another process holds",
            ):
                contender.apply(OPERATION_ID)

    def test_global_deploy_flock_rejects_before_takeover_side_effects(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        lock_path = fixture.runtime / "state/deploy.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with self.assertRaisesRegex(
            TAKEOVER.LegacyTakeoverError,
            "global deploy lock",
        ):
            fixture.seal()
        self.assertFalse(
            fixture.controller._state_path(OPERATION_ID).exists()
        )
        self.assertEqual(fixture.system.actions, [])

    def test_inherited_parent_lock_fd_is_authenticated_across_process(
        self,
    ) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        lock_path = fixture.runtime / "state/deploy.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        locked = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, locked)
        fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
        unlocked = os.open(lock_path, os.O_RDWR)
        self.addCleanup(os.close, unlocked)
        code = """
import importlib.util
from pathlib import Path
import sys
spec = importlib.util.spec_from_file_location("takeover_child", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
controller = module.LegacyTakeover(
    repository=Path(sys.argv[4]),
    runtime_root=Path(sys.argv[5]),
    external_roots={
        name: Path(sys.argv[6]) / name
        for name in module.EXTERNAL_ROOTS
    },
    system=object(),
)
with controller._execution_lock(parent_deploy_lock_fd=int(sys.argv[2])):
    pass
print("authenticated")
"""

        accepted = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                code,
                str(SCRIPT),
                str(locked),
                str(lock_path),
                str(fixture.repository),
                str(fixture.runtime),
                str(fixture.root / "handoff-external"),
            ],
            pass_fds=(locked,),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(accepted.stdout.strip(), "authenticated")

        rejected = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                code,
                str(SCRIPT),
                str(unlocked),
                str(lock_path),
                str(fixture.repository),
                str(fixture.runtime),
                str(fixture.root / "handoff-external"),
            ],
            pass_fds=(unlocked,),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("not the locked inherited description", rejected.stderr)

    def test_atomic_json_write_crash_preserves_prior_record_and_retries(
        self,
    ) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        parent = fixture.runtime / "state/atomic-fixture"
        parent.mkdir(parents=True, mode=0o700)
        target = parent / "state.json"
        TAKEOVER._atomic_json(target, {"generation": 1})

        with mock.patch.object(
            TAKEOVER.os,
            "fsync",
            side_effect=OSError("injected fsync failure"),
        ):
            with self.assertRaisesRegex(
                TAKEOVER.LegacyTakeoverError,
                "cannot persist",
            ):
                TAKEOVER._atomic_json(target, {"generation": 2})
        self.assertEqual(
            json.loads(target.read_text(encoding="utf-8")),
            {"generation": 1},
        )
        self.assertEqual(list(parent.glob(".state.json.tmp-*")), [])
        TAKEOVER._atomic_json(target, {"generation": 2})
        self.assertEqual(
            json.loads(target.read_text(encoding="utf-8")),
            {"generation": 2},
        )

    def test_exclusive_json_write_crash_never_publishes_partial_record(
        self,
    ) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        parent = fixture.runtime / "state/exclusive-fixture"
        parent.mkdir(parents=True, mode=0o700)
        target = parent / "active.json"

        with mock.patch.object(
            TAKEOVER.os,
            "fsync",
            side_effect=OSError("injected fsync failure"),
        ):
            with self.assertRaisesRegex(
                TAKEOVER.LegacyTakeoverError,
                "cannot create exclusive",
            ):
                TAKEOVER._create_json_exclusive(
                    target,
                    {"operation_id": OPERATION_ID},
                )
        self.assertFalse(target.exists())
        self.assertEqual(list(parent.glob(".active.json.create-*")), [])

        stale = parent / ".active.json.create-stale"
        stale.write_bytes(b'{"partial":')
        os.chmod(stale, 0o600)
        TAKEOVER._create_json_exclusive(
            target,
            {"operation_id": OPERATION_ID},
        )
        self.assertFalse(stale.exists())
        self.assertEqual(
            json.loads(target.read_text(encoding="utf-8")),
            {"operation_id": OPERATION_ID},
        )

    def test_terminal_archive_allows_a_later_distinct_operation(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        fixture.seal()
        fixture.controller.apply(OPERATION_ID)
        fixture.controller.restore(OPERATION_ID)
        later = "takeover-fixture-0002"
        state = fixture.controller.seal(
            later,
            fixture.classification_digest,
        )
        self.assertEqual(state["operation_id"], later)
        self.assertTrue(
            fixture.controller._state_path(OPERATION_ID).exists()
        )
        self.assertTrue(fixture.controller._state_path(later).exists())

    def test_pre_stopped_fence_is_stable_and_contains_pg_zero_jobs(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        fixture.seal()
        state = fixture.controller.apply(OPERATION_ID)
        status = fixture.controller.status(OPERATION_ID)
        fence = status["pre_stopped_fence"]
        self.assertEqual(
            TAKEOVER.sha256_bytes(TAKEOVER.canonical_json_bytes(fence)),
            status["pre_stopped_fence_sha256"],
        )
        self.assertEqual(fence["active_jobs_zero"]["active_total"], 0)
        self.assertTrue(
            fence["runtime_fence"]["postgres_running_untouched"]
        )
        self.assertEqual(
            fence["runtime_fence"]["postgres_container_id"],
            state["runtime"]["postgres_container_id"],
        )
        self.assertIsNotNone(status["applied_record_sha256"])

    def test_restore_cas_restores_bootstrap_replaced_worker_unit(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        original = fixture.seal()["worker_unit_seal"]
        fixture.controller.apply(OPERATION_ID)
        fixture.worker_unit.write_text(
            "[Service]\nExecStart=/usr/bin/false\n",
            encoding="utf-8",
        )
        os.chmod(fixture.worker_unit, 0o600)
        replacement = TAKEOVER.sha256_file(fixture.worker_unit)
        fixture.controller.restore(
            OPERATION_ID,
            expected_worker_unit_sha256=replacement,
        )
        TAKEOVER.verify_path_seal(fixture.worker_unit, original)
        self.assertIn("worker-daemon-reload", fixture.system.actions)

    def test_restore_cas_restores_prior_bootstrap_control_layout(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        state_root = fixture.runtime / "state"
        state_root.mkdir(mode=0o700)
        old_bin = fixture.runtime / "bin"
        old_bin.mkdir(mode=0o700)
        (old_bin / "nexpoly-pull-deploy").write_text(
            "#!/bin/sh\nexit 0\n",
            encoding="utf-8",
        )
        os.chmod(old_bin / "nexpoly-pull-deploy", 0o700)
        old_releases = fixture.runtime / "control-releases"
        (old_releases / "old").mkdir(parents=True, mode=0o700)
        (old_releases / "old/manifest.json").write_text(
            '{"release":"old"}\n',
            encoding="utf-8",
        )
        os.chmod(old_releases / "old/manifest.json", 0o600)
        for name in ("active-control.json", "bootstrap-control.json"):
            path = state_root / name
            path.write_text('{"release":"old"}\n', encoding="utf-8")
            os.chmod(path, 0o600)

        sealed = fixture.seal()
        original = {
            record["relative_path"]: record["seal"]
            for record in sealed["control_layout"]
            if record["present"]
        }
        fixture.controller.apply(OPERATION_ID)

        TAKEOVER._remove_tree(old_bin)
        old_bin.mkdir(mode=0o700)
        (old_bin / "nexpoly-pull-deploy").write_text(
            "#!/bin/sh\nexit 42\n",
            encoding="utf-8",
        )
        os.chmod(old_bin / "nexpoly-pull-deploy", 0o700)
        TAKEOVER._remove_tree(old_releases)
        (old_releases / "new").mkdir(parents=True, mode=0o700)
        (old_releases / "new/manifest.json").write_text(
            '{"release":"new"}\n',
            encoding="utf-8",
        )
        os.chmod(old_releases / "new/manifest.json", 0o600)
        for name in ("active-control.json", "bootstrap-control.json"):
            path = state_root / name
            path.write_text('{"release":"new"}\n', encoding="utf-8")
            os.chmod(path, 0o600)
        audit = fixture.runtime / "audit/bootstrap-worker-unit"
        audit.mkdir(parents=True, mode=0o700)
        for name in ("takeover-intent.json", "takeover.json"):
            path = audit / name
            path.write_text('{"release":"new"}\n', encoding="utf-8")
            os.chmod(path, 0o600)
        backup = fixture.runtime / "backups/bootstrap-worker-unit"
        backup.mkdir(parents=True, mode=0o700)
        (backup / "unit.service").write_text(
            "[Service]\nExecStart=/usr/bin/false\n",
            encoding="utf-8",
        )
        os.chmod(backup / "unit.service", 0o600)
        replacement = fixture.controller._snapshot_control_layout()
        replacement_digest = fixture.controller._control_layout_digest(
            replacement
        )

        with self.assertRaisesRegex(
            TAKEOVER.LegacyTakeoverError,
            "no parent CAS digest",
        ):
            fixture.controller.restore(OPERATION_ID)
        restored = fixture.controller.restore(
            OPERATION_ID,
            expected_control_layout_sha256=replacement_digest,
        )
        self.assertEqual(restored["restore_phase"], "restored")
        fixture.assert_restored()
        for relative, seal in original.items():
            TAKEOVER.verify_path_seal(fixture.runtime / relative, seal)
        for relative in (
            "audit/bootstrap-worker-unit/takeover-intent.json",
            "audit/bootstrap-worker-unit/takeover.json",
            "backups/bootstrap-worker-unit",
        ):
            path = fixture.runtime / relative
            self.assertFalse(path.exists() or path.is_symlink())
        replacement_by_path = {
            record["relative_path"]: record for record in replacement
        }
        for index, relative in (
            (TAKEOVER.CONTROL_LAYOUT_RELATIVE_PATHS.index("audit"), "audit"),
            (
                TAKEOVER.CONTROL_LAYOUT_RELATIVE_PATHS.index("backups"),
                "backups",
            ),
        ):
            archive = (
                fixture.external["runtime"]
                / ".takeover-preserved-control"
                / OPERATION_ID
                / str(index)
            )
            TAKEOVER.verify_path_seal(
                archive,
                replacement_by_path[relative]["seal"],
            )
        status = fixture.controller.status(OPERATION_ID)
        self.assertEqual(
            status["control_layout_replacement_sha256"],
            replacement_digest,
        )

    def test_preserved_control_archive_rejects_tampering_on_resume(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        fixture.seal()
        fixture.controller.apply(OPERATION_ID)
        audit = fixture.runtime / "audit"
        audit.mkdir(mode=0o700)
        evidence = audit / "success.json"
        evidence.write_text('{"status":"success"}\n', encoding="utf-8")
        os.chmod(evidence, 0o600)
        replacement = fixture.controller._snapshot_control_layout()
        replacement_digest = fixture.controller._control_layout_digest(
            replacement
        )
        audit_index = TAKEOVER.CONTROL_LAYOUT_RELATIVE_PATHS.index("audit")
        checkpoints = Checkpoints(
            f"restore:control-layout-{audit_index}:archived"
        )
        with self.assertRaises(InjectedCrash):
            fixture.recreate_controller(checkpoints).restore(
                OPERATION_ID,
                expected_control_layout_sha256=replacement_digest,
            )
        archive = (
            fixture.external["runtime"]
            / ".takeover-preserved-control"
            / OPERATION_ID
            / str(audit_index)
        )
        archived_evidence = archive / "success.json"
        archived_evidence.write_text(
            '{"status":"tampered"}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            TAKEOVER.LegacyTakeoverError,
            "seal",
        ):
            fixture.recreate_controller().restore(
                OPERATION_ID,
                expected_control_layout_sha256=replacement_digest,
            )

    def test_restore_removes_every_bootstrap_created_runtime_tree(
        self,
    ) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        sealed = fixture.seal()
        fixture.controller.apply(OPERATION_ID)

        absent = [
            record["relative_path"]
            for record in sealed["control_layout"]
            if not record["present"]
        ]
        for relative in absent:
            path = fixture.runtime / relative
            if path.suffix == ".json":
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                path.write_text('{"candidate":true}\n', encoding="utf-8")
                os.chmod(path, 0o600)
            else:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                evidence = path / "candidate-evidence"
                evidence.write_text("candidate\n", encoding="utf-8")
                os.chmod(evidence, 0o600)
        replacement = fixture.controller._snapshot_control_layout()
        replacement_digest = fixture.controller._control_layout_digest(
            replacement
        )
        fixture.controller.restore(
            OPERATION_ID,
            expected_control_layout_sha256=replacement_digest,
        )
        for relative in absent:
            path = fixture.runtime / relative
            self.assertFalse(
                path.exists() or path.is_symlink(),
                relative,
            )

    def test_control_layout_covers_all_bootstrap_owned_directories(
        self,
    ) -> None:
        bootstrap_script = ROOT / "scripts/bootstrap_pull_deploy.py"
        bootstrap_spec = importlib.util.spec_from_file_location(
            "bootstrap_layout_coverage_test",
            bootstrap_script,
        )
        assert bootstrap_spec is not None and bootstrap_spec.loader is not None
        bootstrap = importlib.util.module_from_spec(bootstrap_spec)
        bootstrap_spec.loader.exec_module(bootstrap)
        roots = set(TAKEOVER.CONTROL_LAYOUT_RELATIVE_PATHS)
        for relative in bootstrap.DIRECTORIES:
            if relative in {"config", "state"}:
                continue
            self.assertTrue(
                any(
                    relative == root
                    or relative.startswith(root + "/")
                    for root in roots
                ),
                relative,
            )
        for left in roots:
            for right in roots - {left}:
                self.assertFalse(
                    right.startswith(left + "/"),
                    (left, right),
                )

    def test_restore_cas_restores_recursive_checkout_permissions(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        sealed = fixture.seal()
        original_digest = sealed["checkout_permissions_sha256"]
        original = {
            record["path"]: (
                record["mode"],
                record["uid"],
                record["gid"],
            )
            for record in sealed["checkout_permissions"]
        }
        fixture.controller.apply(OPERATION_ID)
        os.chmod(fixture.repository, 0o755)
        os.chmod(fixture.repository / ".git", 0o755)
        os.chmod(fixture.repository / "asset-release", 0o700)
        os.chmod(fixture.repository / "asset-release/manifest", 0o600)
        replacement = fixture.controller._current_checkout_permissions(
            fixture.controller._load(OPERATION_ID)
        )
        replacement_digest = TAKEOVER.checkout_permissions_digest(
            replacement
        )
        self.assertNotEqual(replacement_digest, original_digest)

        with self.assertRaisesRegex(
            TAKEOVER.LegacyTakeoverError,
            "no parent CAS digest",
        ):
            fixture.controller.restore(OPERATION_ID)
        restored = fixture.controller.restore(
            OPERATION_ID,
            expected_checkout_permissions_sha256=replacement_digest,
        )
        self.assertEqual(restored["restore_phase"], "restored")
        for relative, expected in original.items():
            path = (
                fixture.repository
                if relative == "."
                else fixture.repository / relative
            )
            metadata = path.lstat()
            self.assertEqual(
                (
                    f"{metadata.st_mode & 0o7777:04o}",
                    metadata.st_uid,
                    metadata.st_gid,
                ),
                expected,
            )
        self.assertEqual(
            fixture.controller.status(OPERATION_ID)[
                "checkout_permissions_replacement_sha256"
            ],
            replacement_digest,
        )

    def test_cross_bootstrap_git_drift_requires_parent_cas_before_restore(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        sealed = fixture.seal()["git_identity"]
        fixture.controller.apply(OPERATION_ID)
        fixture.system.head_sha = "7" * 40
        fixture.system.local_main_sha = "7" * 40
        fixture.system.head_tree = "8" * 40
        with self.assertRaisesRegex(
            TAKEOVER.LegacyTakeoverError,
            "HEAD/tree/main drifted",
        ):
            fixture.controller.restore(OPERATION_ID)
        fixture.system.head_sha = sealed["head_sha"]
        fixture.system.local_main_sha = sealed["local_main_sha"]
        fixture.system.head_tree = sealed["head_tree"]
        restored = fixture.controller.restore(OPERATION_ID)
        self.assertEqual(restored["restore_phase"], "restored")
        fixture.assert_restored()

    def test_intact_recursive_trash_resumes_apply_and_restore(self) -> None:
        checkpoints = Checkpoints("move-1:source-detached")
        fixture = Fixture(checkpoints)
        self.addCleanup(fixture.close)
        fixture.seal()
        with self.assertRaises(InjectedCrash):
            fixture.controller.apply(OPERATION_ID)
        fixture.recreate_controller().apply(OPERATION_ID)
        fixture.assert_applied()

        restore_checkpoints = Checkpoints(
            "restore-move-1:destination-detached"
        )
        with self.assertRaises(InjectedCrash):
            fixture.recreate_controller(restore_checkpoints).restore(
                OPERATION_ID
            )
        fixture.recreate_controller().restore(OPERATION_ID)
        fixture.assert_restored()

    def test_detached_trash_tampering_is_rejected_before_recursive_delete(
        self,
    ) -> None:
        checkpoints = Checkpoints("move-1:source-detached")
        fixture = Fixture(checkpoints)
        self.addCleanup(fixture.close)
        fixture.seal()
        with self.assertRaises(InjectedCrash):
            fixture.controller.apply(OPERATION_ID)
        state = fixture.controller._load(OPERATION_ID)
        trash = Path(state["moves"][1]["source_trash"])
        (trash / "journal.json").write_text(
            '{"tampered":true}\n',
            encoding="utf-8",
        )
        os.chmod(trash / "journal.json", 0o640)
        with self.assertRaisesRegex(
            TAKEOVER.LegacyTakeoverError,
            "not a sealed subset",
        ):
            fixture.recreate_controller().apply(OPERATION_ID)
        self.assertTrue(trash.exists())

    def test_partial_recursive_trash_is_a_safe_resumable_subset(self) -> None:
        checkpoints = Checkpoints("move-1:source-detached")
        fixture = Fixture(checkpoints)
        self.addCleanup(fixture.close)
        fixture.seal()
        with self.assertRaises(InjectedCrash):
            fixture.controller.apply(OPERATION_ID)
        state = fixture.controller._load(OPERATION_ID)
        trash = Path(state["moves"][1]["source_trash"])
        (trash / "journal.json").unlink()
        fixture.recreate_controller().apply(OPERATION_ID)
        fixture.assert_applied()

        checkpoints = Checkpoints(
            "restore-move-1:destination-detached"
        )
        with self.assertRaises(InjectedCrash):
            fixture.recreate_controller(checkpoints).restore(OPERATION_ID)
        state = fixture.controller._load(OPERATION_ID)
        trash = Path(state["moves"][1]["destination_trash"])
        (trash / "journal.json").unlink()
        fixture.recreate_controller().restore(OPERATION_ID)
        fixture.assert_restored()


if __name__ == "__main__":
    unittest.main()
