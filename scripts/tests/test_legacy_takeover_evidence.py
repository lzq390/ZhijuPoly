from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import unittest

from scripts.tests.test_install_legacy_takeover_prerequisites import (
    AUTHORITY_SHA,
    AUTHORITY_TREE,
    InstallerFixture,
)
from scripts.tests.test_legacy_takeover import Fixture, OPERATION_ID


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/legacy_takeover_evidence.py"
SPEC = importlib.util.spec_from_file_location(
    "legacy_takeover_evidence_test",
    SCRIPT,
)
assert SPEC is not None and SPEC.loader is not None
EVIDENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVIDENCE)


class LegacyTakeoverEvidenceTests(unittest.TestCase):
    def test_install_manifest_revalidates_every_installed_byte(self) -> None:
        fixture = InstallerFixture()
        self.addCleanup(fixture.close)
        fixture.install()
        manifest = EVIDENCE.validate_install_manifest(
            fixture.runtime,
            AUTHORITY_SHA,
            AUTHORITY_TREE,
        )
        self.assertEqual(manifest["authority_sha"], AUTHORITY_SHA)

        helper = fixture.runtime / "config/bootstrap-status"
        helper.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        os.chmod(helper, 0o700)
        with self.assertRaisesRegex(
            EVIDENCE.LegacyTakeoverEvidenceError,
            "installed file changed",
        ):
            EVIDENCE.validate_install_manifest(
                fixture.runtime,
                AUTHORITY_SHA,
                AUTHORITY_TREE,
            )

    def test_status_and_completed_binding_are_canonical(self) -> None:
        installer = InstallerFixture()
        takeover = Fixture()
        self.addCleanup(installer.close)
        self.addCleanup(takeover.close)
        installer.install()
        takeover.seal()
        takeover.controller.apply(OPERATION_ID)
        status = takeover.controller.status(OPERATION_ID)
        manifest = EVIDENCE.validate_install_manifest(
            installer.runtime,
            AUTHORITY_SHA,
            AUTHORITY_TREE,
        )
        status["classification_sha256"] = manifest[
            "classification_sha256"
        ]
        validated = EVIDENCE.validate_status_document(
            status,
            OPERATION_ID,
        )
        binding = EVIDENCE.validate_completed(
            installer.runtime,
            OPERATION_ID,
            AUTHORITY_SHA,
            AUTHORITY_TREE,
            expected_git_identity=status["git_identity"],
            status_document=validated,
        )
        self.assertEqual(
            binding["applied_record_sha256"],
            status["applied_record_sha256"],
        )
        unsigned = dict(binding)
        digest = unsigned.pop("binding_sha256")
        self.assertEqual(
            digest,
            EVIDENCE.sha256_bytes(
                EVIDENCE.canonical_json_bytes(unsigned)
            ),
        )
        mismatched_permission_authority = dict(validated)
        mismatched_permission_authority[
            "git_permission_takeover_sha256"
        ] = "sha256:" + "a" * 64
        mismatched_permission_authority[
            "git_permission_inventory_sha256"
        ] = "sha256:" + "b" * 64
        with self.assertRaisesRegex(
            EVIDENCE.LegacyTakeoverEvidenceError,
            "completed apply authority",
        ):
            EVIDENCE.validate_completed(
                installer.runtime,
                OPERATION_ID,
                AUTHORITY_SHA,
                AUTHORITY_TREE,
                status_document=mismatched_permission_authority,
            )

    def test_status_loader_uses_fixed_private_recovery_launcher(self) -> None:
        installer = InstallerFixture()
        takeover = Fixture()
        self.addCleanup(installer.close)
        self.addCleanup(takeover.close)
        installer.install()
        takeover.seal()
        takeover.controller.apply(OPERATION_ID)
        status = takeover.controller.status(OPERATION_ID)

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(
                command[0],
                str(
                    installer.runtime
                    / "legacy-takeover/bin/nexpoly-legacy-takeover"
                ),
            )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(status),
                stderr="",
            )

        self.assertEqual(
            EVIDENCE.load_status(
                installer.runtime,
                OPERATION_ID,
                runner=runner,
            ),
            status,
        )

    def test_restored_binding_requires_exact_terminal_restore(self) -> None:
        installer = InstallerFixture()
        takeover = Fixture()
        self.addCleanup(installer.close)
        self.addCleanup(takeover.close)
        installer.install()
        takeover.seal()
        takeover.controller.apply(OPERATION_ID)
        (takeover.runtime / "bin").mkdir(mode=0o700)
        replacement = takeover.runtime / "bin/candidate"
        replacement.write_text("candidate\n", encoding="utf-8")
        os.chmod(replacement, 0o700)
        os.chmod(takeover.repository / ".git", 0o755)
        control = EVIDENCE.snapshot_current_control_layout(
            takeover.runtime
        )
        permissions = EVIDENCE.snapshot_current_checkout_permissions(
            takeover.runtime,
            OPERATION_ID,
        )
        takeover.controller.restore(
            OPERATION_ID,
            expected_control_layout_sha256=control["sha256"],
            expected_checkout_permissions_sha256=permissions["sha256"],
        )
        status = takeover.controller.status(OPERATION_ID)
        manifest = EVIDENCE.validate_install_manifest(
            installer.runtime,
            AUTHORITY_SHA,
            AUTHORITY_TREE,
        )
        status["classification_sha256"] = manifest[
            "classification_sha256"
        ]
        binding = EVIDENCE.validate_restored(
            installer.runtime,
            OPERATION_ID,
            AUTHORITY_SHA,
            AUTHORITY_TREE,
            expected_git_identity=status["git_identity"],
            status_document=status,
        )
        self.assertEqual(
            binding["restored_terminal_sha256"],
            status["restored_terminal_sha256"],
        )

        incomplete = dict(status)
        incomplete["restore_phase"] = "runtime-restore-intent"
        with self.assertRaisesRegex(
            EVIDENCE.LegacyTakeoverEvidenceError,
            "terminal restore",
        ):
            EVIDENCE.validate_restored(
                installer.runtime,
                OPERATION_ID,
                AUTHORITY_SHA,
                AUTHORITY_TREE,
                status_document=incomplete,
            )

    def test_control_layout_snapshot_matches_takeover_algorithm(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        (fixture.runtime / "bin").mkdir(mode=0o700)
        path = fixture.runtime / "bin/controller"
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(path, 0o700)
        report = EVIDENCE.snapshot_current_control_layout(
            fixture.runtime
        )
        snapshot = fixture.controller._snapshot_control_layout()
        self.assertEqual(report["records"], snapshot)
        self.assertEqual(
            report["sha256"],
            fixture.controller._control_layout_digest(snapshot),
        )

    def test_checkout_permission_snapshot_uses_sealed_path_set(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        fixture.seal()
        fixture.controller.apply(OPERATION_ID)
        os.chmod(fixture.repository / ".git", 0o755)
        report = EVIDENCE.snapshot_current_checkout_permissions(
            fixture.runtime,
            OPERATION_ID,
        )
        state = fixture.controller._load(OPERATION_ID)
        current = fixture.controller._current_checkout_permissions(state)
        self.assertEqual(report["records"], current)
        self.assertEqual(
            report["sha256"],
            EVIDENCE.sha256_bytes(
                EVIDENCE.canonical_json_bytes(
                    {"schema_version": 1, "records": current}
                )
            ),
        )

    def test_fence_tampering_is_rejected(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        fixture.seal()
        fixture.controller.apply(OPERATION_ID)
        status = fixture.controller.status(OPERATION_ID)
        status["pre_stopped_fence"]["active_jobs_zero"][
            "active_total"
        ] = 1
        with self.assertRaisesRegex(
            EVIDENCE.LegacyTakeoverEvidenceError,
            "pre-stopped fence",
        ):
            EVIDENCE.validate_status_document(status, OPERATION_ID)


if __name__ == "__main__":
    unittest.main()
