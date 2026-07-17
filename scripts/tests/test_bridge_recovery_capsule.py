from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def load(name: str, path: Path):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RECOVERY = load(
    "bridge_recovery_capsule_test",
    REPOSITORY_ROOT / "scripts/bridge_recovery_capsule.py",
)
LAUNCHER = load(
    "bridge_recovery_launcher_test",
    REPOSITORY_ROOT / "scripts/bridge_recovery_launcher.py",
)
BRIDGE = load(
    "bridge_recovery_core_test",
    REPOSITORY_ROOT / "scripts/bridge_deploy_core.py",
)
BRIDGE_TESTS = load(
    "bridge_recovery_core_fixtures",
    Path(__file__).with_name("test_bridge_deploy_core.py"),
)

OPERATION_ID = BRIDGE_TESTS.OPERATION_ID
TARGET_SHA = BRIDGE_TESTS.TARGET_SHA
TARGET_TREE = BRIDGE_TESTS.TARGET_TREE
AUTHORITY_SHA = BRIDGE_TESTS.AUTHORITY_SHA
AUTHORITY_TREE = BRIDGE_TESTS.AUTHORITY_TREE
CONTROL_RELEASE_ID = "7" * 64
RESTORED_TERMINAL = "sha256:" + "8" * 64


class CapsuleRecoveryTests(unittest.TestCase):
    def test_capsule_has_no_network_service_database_or_process_surface(
        self,
    ) -> None:
        source = (
            REPOSITORY_ROOT / "scripts/bridge_recovery_capsule.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            str(node.module).split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        self.assertTrue(
            imported.isdisjoint(
                {
                    "subprocess",
                    "socket",
                    "urllib",
                    "http",
                    "psycopg",
                    "sqlite3",
                }
            )
        )
        os_calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        }
        self.assertTrue(
            os_calls.isdisjoint(
                {
                    "execv",
                    "execve",
                    "execvp",
                    "execvpe",
                    "system",
                    "spawnl",
                    "spawnle",
                    "spawnlp",
                    "spawnlpe",
                    "spawnv",
                    "spawnve",
                    "spawnvp",
                    "spawnvpe",
                }
            )
        )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="bridge-recovery-capsule-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.runtime = Path(self.temporary.name) / "runtime"
        self.capsules = (
            self.runtime
            / "legacy-takeover/runtime/bridge-recovery-capsules"
        )
        for directory in (
            self.runtime,
            self.runtime / "legacy-takeover",
            self.runtime / "legacy-takeover/runtime",
            self.capsules,
            self.runtime / "state",
        ):
            directory.mkdir(mode=0o700)
            os.chmod(directory, 0o700)
        lock = self.runtime / "state/deploy.lock"
        lock.write_bytes(b"")
        os.chmod(lock, 0o600)
        self.policy = BRIDGE_TESTS.policy()
        reservation = BRIDGE.reserve_token(
            self.runtime / "state",
            operation_id=OPERATION_ID,
            policy_id=self.policy["policy_id"],
            token=b"bridge-recovery-capsule-token-01",
        )
        self.bridge = BRIDGE.build_bridge_descriptor(
            operation_id=OPERATION_ID,
            authority_sha=AUTHORITY_SHA,
            authority_tree=AUTHORITY_TREE,
            authority_control_release_id="6" * 64,
            ci_evidence={
                "head_sha": AUTHORITY_SHA,
                "conclusion": "success",
            },
            target_control_release_id=CONTROL_RELEASE_ID,
            policy=self.policy,
            token_id=reservation["token_id"],
            token_sha256=reservation["token_sha256"],
        )
        takeover = {
            "schema_version": 1,
            "operation_id": "takeover-capsule-fixture",
            "authority_sha": AUTHORITY_SHA,
            "authority_tree": AUTHORITY_TREE,
            "install_manifest_sha256": "sha256:" + "1" * 64,
            "classification_sha256": "sha256:" + "2" * 64,
            "runtime_identity_sha256": "sha256:" + "3" * 64,
            "git_identity": {
                "branch": "refs/heads/main",
                "head_sha": "9" * 40,
                "head_tree": "a" * 40,
                "local_main_sha": "9" * 40,
            },
            "pre_stopped_fence_sha256": "sha256:" + "4" * 64,
            "control_layout_sha256": "sha256:" + "5" * 64,
            "checkout_permissions_sha256": "sha256:" + "6" * 64,
            "applied_record_sha256": "sha256:" + "7" * 64,
        }
        takeover["binding_sha256"] = RECOVERY.canonical_json_digest(takeover)
        self.descriptor = {
            "schema_version": 3,
            "operation_id": OPERATION_ID,
            "previous_deployment": None,
            "repository": {"target_sha": TARGET_SHA},
            "controller": {
                "executor_control": {"release_id": CONTROL_RELEASE_ID},
                "executor_control_sha256": RECOVERY.canonical_json_digest(
                    {"release_id": CONTROL_RELEASE_ID}
                ),
            },
            "bridge": self.bridge,
            "legacy_takeover": takeover,
        }
        self.descriptor_payload = (
            RECOVERY.canonical_json_bytes(self.descriptor) + b"\n"
        )
        self.descriptor_digest = RECOVERY.sha256_bytes(
            self.descriptor_payload
        )
        BRIDGE.bind_token_descriptor(
            self.runtime / "state",
            operation_id=OPERATION_ID,
            policy_id=self.policy["policy_id"],
            descriptor_sha256=self.descriptor_digest,
        )
        sources = {
            name: (REPOSITORY_ROOT / "scripts" / name).read_bytes()
            for name in RECOVERY.CAPSULE_FILES
        }
        files = {
            name: {
                "sha256": RECOVERY.sha256_bytes(payload),
                "mode": "0700",
            }
            for name, payload in sources.items()
        }
        identity = {
            "schema_version": 1,
            "operation_id": OPERATION_ID,
            "authority_sha": AUTHORITY_SHA,
            "target_sha": TARGET_SHA,
            "descriptor_sha256": self.descriptor_digest,
            "control_release_id": CONTROL_RELEASE_ID,
            "takeover_operation_id": takeover["operation_id"],
            "files": files,
        }
        self.capsule_digest = RECOVERY.canonical_json_digest(identity)
        self.metadata = {
            **identity,
            "capsule_sha256": self.capsule_digest,
        }
        self.capsule = self.capsules / self.capsule_digest.removeprefix(
            "sha256:"
        )
        control = self.capsule / "control"
        control.mkdir(parents=True, mode=0o700)
        os.chmod(self.capsule, 0o700)
        os.chmod(control, 0o700)
        for name, payload in sources.items():
            path = control / name
            path.write_bytes(payload)
            os.chmod(path, 0o700)
        self.write_private(
            self.capsule / "descriptor.json", self.descriptor_payload
        )
        self.write_private(
            self.capsule / "capsule.json",
            RECOVERY.canonical_json_bytes(self.metadata) + b"\n",
        )
        self.binding = {
            "capsule_sha256": self.capsule_digest,
            "descriptor_sha256": self.descriptor_digest,
            "control_release_id": CONTROL_RELEASE_ID,
            "recovery_entry_sha256": files[
                "bridge_recovery_capsule.py"
            ]["sha256"],
        }
        self.marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": self.descriptor_digest,
            "executor_control": self.descriptor["controller"][
                "executor_control"
            ],
            "executor_control_sha256": self.descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "failed",
            "started_at": "2026-07-17T00:00:00Z",
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": False,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": False,
            "database_change_started": False,
            "takeover_restore_started": {
                "operation_id": takeover["operation_id"],
                "worker_unit_sha256": "sha256:" + "a" * 64,
                "control_layout_sha256": "sha256:" + "b" * 64,
                "checkout_permissions_sha256": "sha256:" + "c" * 64,
                "started_at": "2026-07-17T00:00:00Z",
            },
            "bridge_recovery_capsule": self.binding,
            "updated_at": "2026-07-17T00:00:00Z",
        }
        self.write_private(
            self.runtime / "state/deploy-in-progress.json",
            RECOVERY.canonical_json_bytes(self.marker) + b"\n",
        )
        self.restored = {
            **{
                name: takeover[name]
                for name in (
                    "operation_id",
                    "authority_sha",
                    "authority_tree",
                    "install_manifest_sha256",
                    "classification_sha256",
                    "runtime_identity_sha256",
                    "git_identity",
                    "pre_stopped_fence_sha256",
                    "control_layout_sha256",
                    "checkout_permissions_sha256",
                    "applied_record_sha256",
                )
            },
            "restored_terminal_sha256": RESTORED_TERMINAL,
        }
        self.arguments = SimpleNamespace(
            capsule_sha256=self.capsule_digest,
            authority_sha=AUTHORITY_SHA,
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
            descriptor_sha256=self.descriptor_digest,
            restored_terminal_sha256=RESTORED_TERMINAL,
        )

    @staticmethod
    def write_private(path: Path, payload: bytes) -> None:
        path.write_bytes(payload)
        os.chmod(path, 0o600)

    def modules(self):  # type: ignore[no-untyped-def]
        legacy = SimpleNamespace(
            load_status=lambda *_args, **_kwargs: {"fixture": True},
            validate_restored=lambda *_args, **_kwargs: dict(self.restored),
        )

        def loader(_name: str, path: Path):  # type: ignore[no-untyped-def]
            return BRIDGE if path.name == "bridge_deploy_core.py" else legacy

        return mock.patch.object(RECOVERY, "load_module", side_effect=loader)

    def constants(self):  # type: ignore[no-untyped-def]
        return (
            mock.patch.object(RECOVERY, "RUNTIME_ROOT", self.runtime),
            mock.patch.object(RECOVERY, "CAPSULES_ROOT", self.capsules),
        )

    def test_external_capsule_finalizes_without_live_controls_or_prepared_state(
        self,
    ) -> None:
        with self.constants()[0], self.constants()[1], self.modules():
            result = RECOVERY.finalize(self.arguments)
        self.assertEqual(result["token_status"], "retired-precommit")
        self.assertFalse(
            (self.runtime / "state/deploy-in-progress.json").exists()
        )
        token = BRIDGE.load_token_authority(self.runtime / "state")
        self.assertEqual(
            token["retirement"]["recovery_capsule_sha256"],
            self.capsule_digest,
        )
        terminal = (
            self.runtime
            / "legacy-takeover/runtime/pull-terminal"
            / OPERATION_ID
        )
        self.assertEqual(
            json.loads((terminal / "operation-state.json").read_text())[
                "outcome"
            ],
            "failed",
        )

    def test_marker_unlink_response_loss_replays_retired_generation(
        self,
    ) -> None:
        marker_path = self.runtime / "state/deploy-in-progress.json"
        original_unlink = Path.unlink

        def fail_marker_unlink(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            if path == marker_path:
                raise OSError("injected marker unlink failure")
            return original_unlink(path, *args, **kwargs)

        with (
            self.constants()[0],
            self.constants()[1],
            self.modules(),
            mock.patch.object(
                RECOVERY.Path,
                "unlink",
                side_effect=fail_marker_unlink,
                autospec=True,
            ),
            self.assertRaisesRegex(OSError, "marker unlink failure"),
        ):
            RECOVERY.finalize(self.arguments)
        self.assertTrue(marker_path.exists())
        self.assertEqual(
            BRIDGE.load_token_authority(self.runtime / "state")["status"],
            "retired-precommit",
        )
        with self.constants()[0], self.constants()[1], self.modules():
            result = RECOVERY.finalize(self.arguments)
        self.assertEqual(result["token_status"], "retired-precommit")
        self.assertFalse(marker_path.exists())

    def test_commit_intent_can_never_use_recovery_capsule(self) -> None:
        BRIDGE.begin_state_commit(
            self.runtime / "state",
            operation_id=OPERATION_ID,
            descriptor_sha256=self.descriptor_digest,
            candidate_state_sha256="sha256:" + "d" * 64,
        )
        with (
            self.constants()[0],
            self.constants()[1],
            self.modules(),
            self.assertRaisesRegex(
                RECOVERY.RecoveryError, "token authority differs"
            ),
        ):
            RECOVERY.finalize(self.arguments)
        self.assertTrue(
            (self.runtime / "state/deploy-in-progress.json").exists()
        )
        self.assertEqual(
            BRIDGE.load_token_authority(self.runtime / "state")["status"],
            "commit-intent",
        )

    def test_capsule_or_marker_tampering_fails_before_terminal_evidence(self) -> None:
        self.marker["bridge_recovery_capsule"]["capsule_sha256"] = (
            "sha256:" + "f" * 64
        )
        self.write_private(
            self.runtime / "state/deploy-in-progress.json",
            RECOVERY.canonical_json_bytes(self.marker) + b"\n",
        )
        with (
            self.constants()[0],
            self.constants()[1],
            self.modules(),
            self.assertRaisesRegex(
                RECOVERY.RecoveryError, "marker authority differs"
            ),
        ):
            RECOVERY.finalize(self.arguments)
        self.assertFalse(
            (
                self.runtime
                / "legacy-takeover/runtime/pull-terminal"
                / OPERATION_ID
            ).exists()
        )

    def test_source_pinned_launcher_executes_only_exact_b_entry(self) -> None:
        arguments = [
            "--capsule-sha256",
            self.capsule_digest,
            "--authority-sha",
            AUTHORITY_SHA,
            "--target-sha",
            TARGET_SHA,
            "--operation-id",
            OPERATION_ID,
            "--descriptor-sha256",
            self.descriptor_digest,
            "--restored-terminal-sha256",
            RESTORED_TERMINAL,
        ]
        with (
            mock.patch.object(LAUNCHER, "RUNTIME_ROOT", self.runtime),
            mock.patch.object(LAUNCHER, "CAPSULES_ROOT", self.capsules),
            mock.patch.object(
                LAUNCHER.os,
                "execve",
                side_effect=RuntimeError("exec captured"),
            ) as execute,
            self.assertRaisesRegex(RuntimeError, "exec captured"),
        ):
            LAUNCHER.main(arguments)
        command = execute.call_args.args[1]
        self.assertEqual(command[-len(arguments) :], arguments)
        self.assertEqual(
            Path(command[3]),
            self.capsule / "control/bridge_recovery_capsule.py",
        )


if __name__ == "__main__":
    unittest.main()
