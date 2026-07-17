from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/bridge_deploy_core.py"
SPEC = importlib.util.spec_from_file_location("bridge_deploy_core_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
BRIDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BRIDGE)


AUTHORITY_SHA = "a" * 40
AUTHORITY_TREE = "b" * 40
TARGET_SHA = "c" * 40
TARGET_TREE = "d" * 40
OPERATION_ID = "bridge-20260717-0001"
DIGEST_A = "sha256:" + "1" * 64
DIGEST_B = "sha256:" + "2" * 64
TARGET_MANIFEST_SHA256 = "sha256:" + "4" * 64
AUTHORITY_MANIFEST_SHA256 = "sha256:" + "6" * 64
TARGET_RECORDS = json.loads(
    (ROOT / "backend/migrations/postgres/manifest.json").read_text(
        encoding="utf-8"
    )
)["migrations"]
AUTHORITY_RECORDS = [*TARGET_RECORDS, BRIDGE.FINAL_MIGRATION_RECORD]


def policy() -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": 1,
        "mode": BRIDGE.BRIDGE_MODE,
        "authority_ref": BRIDGE.AUTHORITY_REF,
        "target_sha": TARGET_SHA,
        "target_tree": TARGET_TREE,
        "target_ref": f"refs/nexpoly/bridge-target/{TARGET_SHA}",
        "target_images": {
            "backend": f"{BRIDGE.IMAGE_ROOTS['backend']}@{DIGEST_A}",
            "web": f"{BRIDGE.IMAGE_ROOTS['web']}@{DIGEST_B}",
        },
        "asset_manifest_digest": "sha256:" + "3" * 64,
        "datasets_on_asset_change": [],
        "final_migration": dict(BRIDGE.FINAL_MIGRATION),
        "accepted_migration_ledgers": BRIDGE.expected_migration_registry(
            target_manifest_sha256=TARGET_MANIFEST_SHA256,
            target_records=TARGET_RECORDS,
            authority_manifest_sha256=AUTHORITY_MANIFEST_SHA256,
            authority_records=AUTHORITY_RECORDS,
        ),
        "required_ci_jobs": sorted(BRIDGE.REQUIRED_CI_JOBS),
        "policy_id": None,
    }
    identity = {key: value for key, value in document.items() if key != "policy_id"}
    document["policy_id"] = BRIDGE.canonical_json_digest(identity)
    return document


class BridgePolicyTests(unittest.TestCase):
    def test_policy_rejects_any_asset_driven_dataset_rebuild(self) -> None:
        document = policy()
        document["datasets_on_asset_change"] = ["database"]
        identity = {
            key: value for key, value in document.items() if key != "policy_id"
        }
        document["policy_id"] = BRIDGE.canonical_json_digest(identity)
        with self.assertRaisesRegex(
            BRIDGE.BridgeDeployError,
            "must not request",
        ):
            BRIDGE.validate_policy(document)

    def test_exact_policy_relation_and_descriptor_are_self_authenticating(self) -> None:
        document = policy()
        validated = BRIDGE.validate_policy(document)
        relation = BRIDGE.validate_relation(
            validated,
            authority_sha=AUTHORITY_SHA,
            authority_tree=AUTHORITY_TREE,
            remote_main=AUTHORITY_SHA,
            target_sha=TARGET_SHA,
            target_tree=TARGET_TREE,
            target_ref=f"refs/nexpoly/bridge-target/{TARGET_SHA}",
            is_ancestor=True,
        )
        self.assertEqual(relation["authority_sha"], AUTHORITY_SHA)
        descriptor = BRIDGE.build_bridge_descriptor(
            operation_id=OPERATION_ID,
            authority_sha=AUTHORITY_SHA,
            authority_tree=AUTHORITY_TREE,
            authority_control_release_id="7" * 64,
            ci_evidence={"head_sha": AUTHORITY_SHA, "conclusion": "success"},
            target_control_release_id="8" * 64,
            policy=validated,
            token_id="sha256:" + "9" * 64,
            token_sha256="sha256:" + "a" * 64,
        )
        self.assertEqual(
            BRIDGE.validate_bridge_descriptor(descriptor),
            descriptor,
        )

    def test_registry_is_derived_from_exact_b_and_unique_0013(self) -> None:
        document = policy()
        self.assertEqual(
            BRIDGE.validate_migration_registry(
                document,
                target_manifest_sha256=TARGET_MANIFEST_SHA256,
                target_records=TARGET_RECORDS,
                authority_manifest_sha256=AUTHORITY_MANIFEST_SHA256,
                authority_records=AUTHORITY_RECORDS,
            ),
            document["accepted_migration_ledgers"],
        )
        for label, authority_records in (
            (
                "wrong checksum",
                [
                    *TARGET_RECORDS,
                    {
                        **BRIDGE.FINAL_MIGRATION_RECORD,
                        "checksum": "f" * 64,
                    },
                ],
            ),
            (
                "future row",
                [
                    *AUTHORITY_RECORDS,
                    {
                        **BRIDGE.FINAL_MIGRATION_RECORD,
                        "version": "0014_future",
                    },
                ],
            ),
        ):
            with self.subTest(label=label), self.assertRaises(
                BRIDGE.BridgeDeployError
            ):
                BRIDGE.validate_migration_registry(
                    document,
                    target_manifest_sha256=TARGET_MANIFEST_SHA256,
                    target_records=TARGET_RECORDS,
                    authority_manifest_sha256=AUTHORITY_MANIFEST_SHA256,
                    authority_records=authority_records,
                )

    def test_registry_matches_only_exact_pre_post_0012_and_post_0013(self) -> None:
        registry = policy()["accepted_migration_ledgers"]
        assert isinstance(registry, list)
        for name, records in (
            ("pre-0012", TARGET_RECORDS[:-1]),
            ("post-0012", TARGET_RECORDS),
            ("post-0013", AUTHORITY_RECORDS),
        ):
            self.assertEqual(
                BRIDGE.match_migration_ledger(registry, records)["name"],
                name,
            )
        with self.assertRaises(BRIDGE.BridgeDeployError):
            BRIDGE.match_migration_ledger(
                registry,
                [
                    *TARGET_RECORDS,
                    {
                        **BRIDGE.FINAL_MIGRATION_RECORD,
                        "checksum": "e" * 64,
                    },
                ],
            )

    def test_policy_rejects_mutable_ref_tampering_and_missing_current_ci(self) -> None:
        document = policy()
        document["target_ref"] = "refs/tags/bridge"
        with self.assertRaisesRegex(BRIDGE.BridgeDeployError, "exact private ref"):
            BRIDGE.validate_policy(document)

        document = policy()
        document["required_ci_jobs"] = ["ci-gate"]
        identity = {
            key: value for key, value in document.items() if key != "policy_id"
        }
        document["policy_id"] = BRIDGE.canonical_json_digest(identity)
        with self.assertRaisesRegex(BRIDGE.BridgeDeployError, "CI jobs"):
            BRIDGE.validate_policy(document)

    def test_relation_rejects_stale_authority_arbitrary_target_and_nonancestor(
        self,
    ) -> None:
        values = {
            "policy": policy(),
            "authority_sha": AUTHORITY_SHA,
            "authority_tree": AUTHORITY_TREE,
            "remote_main": AUTHORITY_SHA,
            "target_sha": TARGET_SHA,
            "target_tree": TARGET_TREE,
            "target_ref": f"refs/nexpoly/bridge-target/{TARGET_SHA}",
            "is_ancestor": True,
        }
        for field, replacement in (
            ("remote_main", "e" * 40),
            ("target_sha", "f" * 40),
            ("target_ref", f"refs/nexpoly/bridge-target/{'f' * 40}"),
            ("is_ancestor", False),
        ):
            with self.subTest(field=field):
                changed = {**values, field: replacement}
                with self.assertRaisesRegex(
                    BRIDGE.BridgeDeployError,
                    "relation differs",
                ):
                    BRIDGE.validate_relation(**changed)

    def test_descriptor_rejects_authority_target_control_collapse(self) -> None:
        descriptor = BRIDGE.build_bridge_descriptor(
            operation_id=OPERATION_ID,
            authority_sha=AUTHORITY_SHA,
            authority_tree=AUTHORITY_TREE,
            authority_control_release_id="7" * 64,
            ci_evidence={"head_sha": AUTHORITY_SHA},
            target_control_release_id="8" * 64,
            policy=policy(),
            token_id="sha256:" + "9" * 64,
            token_sha256="sha256:" + "a" * 64,
        )
        descriptor["target"]["control_release_id"] = "7" * 64
        with self.assertRaisesRegex(BRIDGE.BridgeDeployError, "target differs"):
            BRIDGE.validate_bridge_descriptor(descriptor)


class BridgeTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="bridge-token-")
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "state"
        self.state.mkdir(mode=0o700)
        os.chmod(self.state, 0o700)
        self.descriptor_digest = "sha256:" + "b" * 64
        self.candidate_digest = "sha256:" + "c" * 64

    def prepare(self) -> dict[str, object]:
        return BRIDGE.prepare_token(
            self.state,
            operation_id=OPERATION_ID,
            policy_id=str(policy()["policy_id"]),
            descriptor_sha256=self.descriptor_digest,
            token=b"x" * 32,
        )

    def test_reservation_reuses_identity_and_binds_descriptor_exactly(self) -> None:
        reserved = BRIDGE.reserve_token(
            self.state,
            operation_id=OPERATION_ID,
            policy_id=str(policy()["policy_id"]),
            token=b"x" * 32,
        )
        self.assertEqual(reserved["status"], "reserved")
        self.assertIsNone(reserved["descriptor_sha256"])
        self.assertEqual(
            BRIDGE.reserve_token(
                self.state,
                operation_id=OPERATION_ID,
                policy_id=str(policy()["policy_id"]),
                token=b"y" * 32,
            ),
            reserved,
        )
        prepared = BRIDGE.bind_token_descriptor(
            self.state,
            operation_id=OPERATION_ID,
            policy_id=str(policy()["policy_id"]),
            descriptor_sha256=self.descriptor_digest,
        )
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(prepared["token_id"], reserved["token_id"])
        with self.assertRaisesRegex(
            BRIDGE.BridgeDeployError, "another descriptor"
        ):
            BRIDGE.bind_token_descriptor(
                self.state,
                operation_id=OPERATION_ID,
                policy_id=str(policy()["policy_id"]),
                descriptor_sha256="sha256:" + "d" * 64,
            )

    def test_single_global_token_is_idempotent_only_for_same_authority(self) -> None:
        first = self.prepare()
        self.assertEqual(first["status"], "prepared")
        self.assertEqual(self.prepare(), first)
        with self.assertRaisesRegex(BRIDGE.BridgeDeployError, "already owned"):
            BRIDGE.prepare_token(
                self.state,
                operation_id="bridge-20260717-0002",
                policy_id=str(policy()["policy_id"]),
                descriptor_sha256=self.descriptor_digest,
                token=b"y" * 32,
            )

    def test_crash_after_commit_intent_recovers_and_consumes_exact_state(self) -> None:
        self.prepare()
        original = BRIDGE._atomic_json

        def crash_after_publish(path, document):  # type: ignore[no-untyped-def]
            original(path, document)
            if document.get("status") == "commit-intent":
                raise RuntimeError("injected commit-intent crash")

        with (
            mock.patch.object(
                BRIDGE,
                "_atomic_json",
                side_effect=crash_after_publish,
            ),
            self.assertRaisesRegex(RuntimeError, "commit-intent crash"),
        ):
            BRIDGE.begin_state_commit(
                self.state,
                operation_id=OPERATION_ID,
                descriptor_sha256=self.descriptor_digest,
                candidate_state_sha256=self.candidate_digest,
            )
        record = BRIDGE._load_token(
            self.state / BRIDGE.TOKEN_RELATIVE_PATH.name
        )
        self.assertEqual(record["status"], "commit-intent")
        consumed = BRIDGE.reconcile_token(
            self.state,
            operation_id=OPERATION_ID,
            descriptor_sha256=self.descriptor_digest,
            observed_current_state_sha256=self.candidate_digest,
        )
        self.assertEqual(consumed["status"], "consumed")

    def test_precommit_retry_and_postcommit_lost_response_are_safe(self) -> None:
        self.prepare()
        intent = BRIDGE.begin_state_commit(
            self.state,
            operation_id=OPERATION_ID,
            descriptor_sha256=self.descriptor_digest,
            candidate_state_sha256=self.candidate_digest,
        )
        self.assertEqual(intent["status"], "commit-intent")
        self.assertEqual(
            BRIDGE.reconcile_token(
                self.state,
                operation_id=OPERATION_ID,
                descriptor_sha256=self.descriptor_digest,
                observed_current_state_sha256=None,
            )["status"],
            "commit-intent",
        )

        original = BRIDGE._atomic_json

        def lose_consumed_response(path, document):  # type: ignore[no-untyped-def]
            original(path, document)
            if document.get("status") == "consumed":
                raise RuntimeError("injected consumed response loss")

        with (
            mock.patch.object(
                BRIDGE,
                "_atomic_json",
                side_effect=lose_consumed_response,
            ),
            self.assertRaisesRegex(RuntimeError, "consumed response loss"),
        ):
            BRIDGE.consume_token(
                self.state,
                operation_id=OPERATION_ID,
                descriptor_sha256=self.descriptor_digest,
                candidate_state_sha256=self.candidate_digest,
            )
        recovered = BRIDGE.reconcile_token(
            self.state,
            operation_id=OPERATION_ID,
            descriptor_sha256=self.descriptor_digest,
            observed_current_state_sha256=self.candidate_digest,
        )
        self.assertEqual(recovered["status"], "consumed")

    def test_consumed_token_cannot_be_reused_or_detached_from_current_state(self) -> None:
        self.prepare()
        BRIDGE.begin_state_commit(
            self.state,
            operation_id=OPERATION_ID,
            descriptor_sha256=self.descriptor_digest,
            candidate_state_sha256=self.candidate_digest,
        )
        BRIDGE.consume_token(
            self.state,
            operation_id=OPERATION_ID,
            descriptor_sha256=self.descriptor_digest,
            candidate_state_sha256=self.candidate_digest,
        )
        with self.assertRaisesRegex(BRIDGE.BridgeDeployError, "lost its current state"):
            BRIDGE.reconcile_token(
                self.state,
                operation_id=OPERATION_ID,
                descriptor_sha256=self.descriptor_digest,
                observed_current_state_sha256=None,
            )
        with self.assertRaisesRegex(BRIDGE.BridgeDeployError, "already owned"):
            BRIDGE.prepare_token(
                self.state,
                operation_id="bridge-20260717-0002",
                policy_id=str(policy()["policy_id"]),
                descriptor_sha256="sha256:" + "d" * 64,
                token=b"z" * 32,
            )


if __name__ == "__main__":
    unittest.main()
