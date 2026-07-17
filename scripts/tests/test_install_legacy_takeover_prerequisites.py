from __future__ import annotations

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/install_legacy_takeover_prerequisites.py"
SPEC = importlib.util.spec_from_file_location(
    "install_legacy_takeover_prerequisites_test",
    SCRIPT,
)
assert SPEC is not None and SPEC.loader is not None
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)

CONTRACT_SPEC = importlib.util.spec_from_file_location(
    "site_helper_contracts_installer_test",
    ROOT / "scripts/site_helper_contracts.py",
)
assert CONTRACT_SPEC is not None and CONTRACT_SPEC.loader is not None
CONTRACTS = importlib.util.module_from_spec(CONTRACT_SPEC)
CONTRACT_SPEC.loader.exec_module(CONTRACTS)

AUTHORITY_SHA = "1" * 40
AUTHORITY_TREE = "2" * 40


class InstallerFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="legacy-takeover-installer-"
        )
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.runtime = self.root / "runtime"
        self.staging = self.runtime / "bootstrap-input"
        self.state = self.runtime / "state"
        self.config = self.runtime / "config"
        self.staging.mkdir(parents=True, mode=0o700)
        self.state.mkdir(mode=0o700)
        self.config.mkdir(mode=0o700)
        os.chmod(self.runtime, 0o700)
        self.pgpass = (
            self.config / INSTALLER.MUTABLE_PGPASS_NAME
        )
        self.pgpass.write_text(
            "127.0.0.1:55432:nexpoly:nexpoly_mutable_audit:"
            "fixture-private-value\n",
            encoding="utf-8",
        )
        os.chmod(self.pgpass, 0o600)
        for name in self.site_names:
            path = self.staging / name
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(path, 0o700)
        self.classification = (
            self.staging / INSTALLER.CLASSIFICATION_NAME
        )
        self.classification.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "review_id": "review:fixture-20260717",
                    "paths": [{"path": "cache", "class": "runtime"}],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(self.classification, 0o600)
        self.readiness_calls = 0
        self.drift_at_call: int | None = None

    @property
    def site_names(self) -> list[str]:
        return sorted(
            set(CONTRACTS.HELPERS) - set(INSTALLER.REVIEWED_WRAPPERS)
        )

    def close(self) -> None:
        self.temporary.cleanup()

    def readiness(
        self,
        _root: Path,
        *,
        expected_sha: str,
    ) -> dict[str, object]:
        self.readiness_calls += 1
        tree = (
            "3" * 40
            if self.drift_at_call == self.readiness_calls
            else AUTHORITY_TREE
        )
        return {
            "ready": True,
            "source_sha": expected_sha,
            "source_tree": tree,
            "standalone_object_database": True,
            "dirty_entries": 0,
            "ignored_entries": 0,
            "unreachable_objects": 0,
        }

    @staticmethod
    def authority_reader(
        root: Path,
        _authority_sha: str,
        relative: str,
    ) -> bytes:
        return (root / relative).read_bytes()

    def install(
        self,
        *,
        apply: bool = True,
        ignored_paths: list[str] | None = None,
    ) -> dict[str, object]:
        return INSTALLER.install_prerequisites(
            source_root=ROOT,
            runtime_root=self.runtime,
            authority_sha=AUTHORITY_SHA,
            authority_tree=AUTHORITY_TREE,
            apply=apply,
            readiness=self.readiness,
            authority_reader=self.authority_reader,
            production_root=self.root / "not-production",
            ignored_paths=(
                ["cache"] if ignored_paths is None else ignored_paths
            ),
        )


class LegacyTakeoverPrerequisiteInstallerTests(unittest.TestCase):
    def test_source_pinned_install_is_complete_and_idempotent(self) -> None:
        fixture = InstallerFixture()
        self.addCleanup(fixture.close)

        first = fixture.install()
        self.assertTrue(first["ready"])
        self.assertGreaterEqual(fixture.readiness_calls, 3)
        manifest = (
            fixture.runtime
            / "legacy-takeover/INSTALL-MANIFEST.json"
        )
        first_manifest = manifest.read_bytes()
        self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
        for name in INSTALLER.RECOVERY_FILES:
            installed = fixture.runtime / "legacy-takeover/bin" / name
            self.assertTrue(installed.is_file())
            self.assertEqual(installed.stat().st_mode & 0o777, 0o700)
        for name in CONTRACTS.HELPERS:
            installed = fixture.runtime / "config" / name
            self.assertTrue(installed.is_file())
            self.assertEqual(installed.stat().st_mode & 0o777, 0o700)

        second = fixture.install()
        self.assertTrue(second["ready"])
        self.assertEqual(manifest.read_bytes(), first_manifest)
        self.assertTrue(
            all(
                record["installed"] is False
                for record in second["installed"].values()
            )
        )
        self.assertFalse(second["install_manifest"]["installed"])

    def test_global_deploy_lock_rejects_without_install_side_effects(self) -> None:
        fixture = InstallerFixture()
        self.addCleanup(fixture.close)
        lock = fixture.state / "deploy.lock"
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with self.assertRaisesRegex(
            INSTALLER.PrerequisiteInstallError,
            "global deploy lock",
        ):
            fixture.install()
        self.assertEqual(
            {path.name for path in fixture.config.iterdir()},
            {INSTALLER.MUTABLE_PGPASS_NAME},
        )
        self.assertFalse((fixture.runtime / "legacy-takeover").exists())

    def test_source_drift_at_second_and_final_readiness_is_rejected(self) -> None:
        for drift_call in (2, 3):
            with self.subTest(drift_call=drift_call):
                fixture = InstallerFixture()
                try:
                    fixture.drift_at_call = drift_call
                    with self.assertRaisesRegex(
                        INSTALLER.PrerequisiteInstallError,
                        "source identity differs",
                    ):
                        fixture.install()
                finally:
                    fixture.close()

    def test_authority_blob_mismatch_is_rejected_before_install(self) -> None:
        fixture = InstallerFixture()
        self.addCleanup(fixture.close)

        def changed(
            root: Path,
            authority_sha: str,
            relative: str,
        ) -> bytes:
            payload = fixture.authority_reader(
                root,
                authority_sha,
                relative,
            )
            if relative == "scripts/legacy_takeover.py":
                return payload + b"\n# authority mismatch\n"
            return payload

        with self.assertRaisesRegex(
            INSTALLER.PrerequisiteInstallError,
            "differs from F authority blob",
        ):
            INSTALLER.install_prerequisites(
                source_root=ROOT,
                runtime_root=fixture.runtime,
                authority_sha=AUTHORITY_SHA,
                authority_tree=AUTHORITY_TREE,
                apply=True,
                readiness=fixture.readiness,
                authority_reader=changed,
                ignored_paths=["cache"],
            )
        self.assertEqual(
            {path.name for path in fixture.config.iterdir()},
            {INSTALLER.MUTABLE_PGPASS_NAME},
        )

    def test_fail_closed_template_and_classification_mismatch_are_rejected(
        self,
    ) -> None:
        fixture = InstallerFixture()
        self.addCleanup(fixture.close)
        template = fixture.staging / fixture.site_names[0]
        template.write_bytes(
            b"#!/bin/sh\n# SITE_IMPLEMENTATION_REQUIRED\nexit 2\n"
        )
        os.chmod(template, 0o700)
        with self.assertRaisesRegex(
            INSTALLER.PrerequisiteInstallError,
            "fail-closed template",
        ):
            fixture.install()
        self.assertEqual(
            {path.name for path in fixture.config.iterdir()},
            {INSTALLER.MUTABLE_PGPASS_NAME},
        )

        template.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(template, 0o700)
        with self.assertRaisesRegex(
            INSTALLER.PrerequisiteInstallError,
            "classification map failed exact validation",
        ):
            fixture.install(ignored_paths=["cache", "extra"])
        self.assertEqual(
            {path.name for path in fixture.config.iterdir()},
            {INSTALLER.MUTABLE_PGPASS_NAME},
        )

    def test_conflicting_installed_target_is_never_overwritten(self) -> None:
        fixture = InstallerFixture()
        self.addCleanup(fixture.close)
        fixture.install()
        target = fixture.runtime / "config/bootstrap-quiesce"
        target.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        os.chmod(target, 0o700)
        with self.assertRaisesRegex(
            INSTALLER.PrerequisiteInstallError,
            "conflicts",
        ):
            fixture.install()
        self.assertIn("exit 99", target.read_text(encoding="utf-8"))

    def test_pgpass_must_be_private_preprovisioned_and_not_template(
        self,
    ) -> None:
        fixture = InstallerFixture()
        self.addCleanup(fixture.close)
        fixture.pgpass.write_text(
            "127.0.0.1:55432:nexpoly:nexpoly_mutable_audit:"
            "<provision-owner-only-secret>\n",
            encoding="utf-8",
        )
        os.chmod(fixture.pgpass, 0o600)
        with self.assertRaisesRegex(
            INSTALLER.PrerequisiteInstallError,
            "identity or secret",
        ):
            fixture.install()
        self.assertFalse(
            (fixture.config / INSTALLER.MUTABLE_SERVICE_NAME).exists()
        )


if __name__ == "__main__":
    unittest.main()
