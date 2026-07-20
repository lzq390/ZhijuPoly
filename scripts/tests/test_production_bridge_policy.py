from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/validate_production_bridge_policy.py"
POLICY = ROOT / "ops/config/production-bridge-policy.json"
SPEC = importlib.util.spec_from_file_location(
    "validate_production_bridge_policy_test",
    SCRIPT,
)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)

ARBITRARY_ANCESTOR_SHA = "d91f17006669c98064e1a3ad7c7616866ebee54c"
ARBITRARY_ANCESTOR_TREE = "2ce9c9b0440170689f32988686fc34cf97cdfdca"


def policy_document() -> dict[str, object]:
    document = json.loads(POLICY.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def reseal(document: dict[str, object]) -> None:
    document["policy_id"] = VALIDATOR.bridge_core.canonical_json_digest(
        {
            key: value
            for key, value in document.items()
            if key != "policy_id"
        }
    )


def payload(document: dict[str, object]) -> bytes:
    return (
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def clean_head_binding_snapshot() -> tuple[
    dict[str, dict[str, str]],
    dict[str, bytes],
]:
    policy_payload = POLICY.read_bytes()
    return (
        {
            "policy": {
                "path": "ops/config/production-bridge-policy.json",
                "mode": "100644",
                "blob": "a" * 40,
                "sha256": VALIDATOR._sha256(policy_payload),
            }
        },
        {"policy": policy_payload},
    )


def git(repository: Path, *arguments: str) -> bytes:
    return subprocess.run(
        [VALIDATOR.GIT_BINARY, "-C", str(repository), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


class ProductionBridgePolicyPinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = policy_document()

    def validate(self, document: dict[str, object]) -> dict[str, object]:
        return VALIDATOR.validate_policy_payload(payload(document))

    def test_tracked_policy_cross_binds_dynamic_f_and_frozen_b(self) -> None:
        with mock.patch.object(
            VALIDATOR,
            "_snapshot_head_bound_inputs",
            return_value=clean_head_binding_snapshot(),
        ):
            evidence = VALIDATOR.validate_tracked_policy()
        self.assertEqual(evidence["target"]["sha"], VALIDATOR.TARGET_SHA)
        self.assertEqual(evidence["target"]["tree"], VALIDATOR.TARGET_TREE)
        self.assertEqual(
            evidence["target"]["bridge_core_blob"],
            VALIDATOR.TARGET_CORE_BLOB,
        )
        self.assertEqual(
            evidence["target"]["required_ci_jobs"],
            [
                "Publish and smoke immutable main images",
                "bridge-validation",
                "ci-gate",
            ],
        )
        self.assertEqual(
            evidence["authority"]["required_ci_jobs"],
            VALIDATOR.REQUIRED_CI_JOBS,
        )
        self.assertEqual(
            set(evidence["authority"]["required_ci_jobs"])
            - set(evidence["target"]["required_ci_jobs"]),
            {"exact-B bridge compatibility"},
        )
        self.assertNotEqual(
            evidence["authority"]["sha"],
            evidence["target"]["sha"],
        )
        self.assertEqual(
            evidence["authority"]["identity_source"],
            "current-HEAD-not-policy-self-reference",
        )
        self.assertEqual(
            evidence["asset"]["manifest_sha256"],
            VALIDATOR.ASSET_MANIFEST_SHA256,
        )
        self.assertEqual(
            evidence["asset"]["predecessor_manifest_sha256"],
            VALIDATOR.PREDECESSOR_ASSET_MANIFEST_SHA256,
        )
        self.assertEqual(
            [
                record["name"]
                for record in evidence["migrations"]["accepted_ledgers"]
            ],
            ["pre-0012", "post-0012", "post-0013"],
        )
        self.assertEqual(
            evidence["external_database_audit"][
                "media_authority_rules_sha256"
            ],
            VALIDATOR.MEDIA_AUTHORITY_RULES_SHA256,
        )
        self.assertEqual(
            evidence["external_database_audit"]["audit_role_sql_sha256"],
            VALIDATOR.AUDIT_ROLE_SQL_SHA256,
        )

    def test_arbitrary_full_historical_target_is_rejected_after_reseal(
        self,
    ) -> None:
        changed = copy.deepcopy(self.document)
        changed.update(
            {
                "target_sha": ARBITRARY_ANCESTOR_SHA,
                "target_tree": ARBITRARY_ANCESTOR_TREE,
                "target_ref": (
                    "refs/nexpoly/bridge-target/" + ARBITRARY_ANCESTOR_SHA
                ),
            }
        )
        reseal(changed)
        VALIDATOR.bridge_core.validate_policy(changed)
        with self.assertRaisesRegex(
            VALIDATOR.ProductionBridgePolicyError,
            "exact reviewed",
        ):
            self.validate(changed)

    def test_every_repository_pin_rejects_resealed_drift(self) -> None:
        mutations = (
            (
                "target tree",
                lambda value: value.__setitem__("target_tree", "f" * 40),
            ),
            (
                "backend image",
                lambda value: value["target_images"].__setitem__(
                    "backend",
                    "ghcr.io/lzq390/nexpoly-backend@sha256:" + "1" * 64,
                ),
            ),
            (
                "web image",
                lambda value: value["target_images"].__setitem__(
                    "web",
                    "ghcr.io/lzq390/nexpoly-web@sha256:" + "2" * 64,
                ),
            ),
            (
                "asset",
                lambda value: value.__setitem__(
                    "asset_manifest_digest",
                    "sha256:" + "3" * 64,
                ),
            ),
            (
                "final checksum",
                lambda value: value["final_migration"].__setitem__(
                    "checksum",
                    "4" * 64,
                ),
            ),
            (
                "ledger",
                lambda value: value["accepted_migration_ledgers"][0].__setitem__(
                    "ledger_sha256",
                    "sha256:" + "6" * 64,
                ),
            ),
            (
                "media authority",
                lambda value: value["external_database_audit"].__setitem__(
                    "media_authority_rules_sha256",
                    "sha256:" + "7" * 64,
                ),
            ),
            (
                "audit role",
                lambda value: value["external_database_audit"].__setitem__(
                    "audit_role_sql_sha256",
                    "sha256:" + "8" * 64,
                ),
            ),
            (
                "extra CI job",
                lambda value: value.__setitem__(
                    "required_ci_jobs",
                    sorted([*value["required_ci_jobs"], "unexpected-job"]),
                ),
            ),
            (
                "missing exact-B CI job",
                lambda value: value.__setitem__(
                    "required_ci_jobs",
                    [
                        job
                        for job in value["required_ci_jobs"]
                        if job != "exact-B bridge compatibility"
                    ],
                ),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                changed = copy.deepcopy(self.document)
                mutate(changed)
                reseal(changed)
                with self.assertRaises(VALIDATOR.ProductionBridgePolicyError):
                    self.validate(changed)

    def test_mutable_refs_short_shas_and_self_references_are_rejected(
        self,
    ) -> None:
        mutations = (
            (
                "tag",
                lambda value: value.__setitem__(
                    "target_ref",
                    "refs/tags/bridge-b",
                ),
            ),
            (
                "branch",
                lambda value: value.__setitem__(
                    "target_ref",
                    "refs/heads/bridge-b",
                ),
            ),
            (
                "short SHA",
                lambda value: value.__setitem__(
                    "target_sha",
                    VALIDATOR.TARGET_SHA[:12],
                ),
            ),
            (
                "authority SHA",
                lambda value: value.__setitem__("authority_sha", "a" * 40),
            ),
            (
                "authority tree",
                lambda value: value.__setitem__("authority_tree", "b" * 40),
            ),
            (
                "predecessor field",
                lambda value: value.__setitem__(
                    "predecessor_asset_manifest_digest",
                    VALIDATOR.PREDECESSOR_ASSET_MANIFEST_SHA256,
                ),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                changed = copy.deepcopy(self.document)
                mutate(changed)
                reseal(changed)
                with self.assertRaises(VALIDATOR.ProductionBridgePolicyError):
                    self.validate(changed)

    def test_stale_policy_identity_is_rejected(self) -> None:
        changed = copy.deepcopy(self.document)
        changed["target_tree"] = "e" * 40
        with self.assertRaisesRegex(
            VALIDATOR.ProductionBridgePolicyError,
            "identity differs",
        ):
            self.validate(changed)

    def test_authority_inputs_are_tracked_exact_regular_files(self) -> None:
        VALIDATOR._verify_tracked_external_authority()
        with tempfile.TemporaryDirectory() as temporary:
            changed = Path(temporary) / "changed.json"
            changed.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(
                VALIDATOR,
                "MEDIA_AUTHORITY_RULES_PATH",
                changed,
            ), self.assertRaisesRegex(
                VALIDATOR.ProductionBridgePolicyError,
                "authority rules",
            ):
                VALIDATOR._verify_tracked_external_authority()

            target = Path(temporary) / "target.sql"
            target.write_bytes(
                VALIDATOR.AUDIT_ROLE_SQL_PATH.read_bytes()
            )
            link = Path(temporary) / "role.sql"
            link.symlink_to(target)
            with mock.patch.object(
                VALIDATOR,
                "AUDIT_ROLE_SQL_PATH",
                link,
            ), self.assertRaisesRegex(
                VALIDATOR.ProductionBridgePolicyError,
                "stable bounded",
            ):
                VALIDATOR._verify_tracked_external_authority()

    def test_validator_has_no_runtime_pin_override(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("argparse", source)
        self.assertNotIn("os.environ", source)
        self.assertNotIn("media_registry_sha256", source)


class ProductionBridgeHeadBindingTests(unittest.TestCase):
    def make_repository(
        self,
        root: Path,
        relative_path: str,
        content: bytes,
        *,
        executable: bool = False,
    ) -> str:
        git(root, "init", "--quiet")
        git(root, "config", "user.name", "Bridge Validator Test")
        git(root, "config", "user.email", "bridge-validator@example.invalid")
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if executable:
            path.chmod(0o755)
        git(root, "add", "--", relative_path)
        git(root, "commit", "--quiet", "-m", "authority input")
        return git(root, "rev-parse", "HEAD").decode("ascii").strip()

    def test_snapshot_requires_exact_tracked_head_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            authority_sha = self.make_repository(
                root,
                "policy.json",
                b'{"schema_version":1}\n',
            )
            with mock.patch.object(
                VALIDATOR,
                "HEAD_BOUND_INPUTS",
                {"policy": "policy.json"},
            ):
                bindings, payloads = VALIDATOR._snapshot_head_bound_inputs(
                    root,
                    authority_sha,
                )
                self.assertEqual(
                    payloads["policy"],
                    b'{"schema_version":1}\n',
                )
                self.assertEqual(bindings["policy"]["mode"], "100644")

                (root / "policy.json").write_bytes(b'{"schema_version":2}\n')
                with self.assertRaisesRegex(
                    VALIDATOR.ProductionBridgePolicyError,
                    "working-tree bytes differ",
                ):
                    VALIDATOR._snapshot_head_bound_inputs(root, authority_sha)

    def test_snapshot_rejects_untracked_or_mode_drifted_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            authority_sha = self.make_repository(
                root,
                "authority.sh",
                b"#!/bin/sh\nexit 0\n",
                executable=True,
            )
            (root / "authority.sh").chmod(0o644)
            with mock.patch.object(
                VALIDATOR,
                "HEAD_BOUND_INPUTS",
                {"sample": "authority.sh"},
            ), self.assertRaisesRegex(
                VALIDATOR.ProductionBridgePolicyError,
                "working-tree mode differs",
            ):
                VALIDATOR._snapshot_head_bound_inputs(root, authority_sha)

            (root / "untracked.json").write_text("{}\n", encoding="utf-8")
            with mock.patch.object(
                VALIDATOR,
                "HEAD_BOUND_INPUTS",
                {"sample": "untracked.json"},
            ), self.assertRaisesRegex(
                VALIDATOR.ProductionBridgePolicyError,
                "not tracked exactly once",
            ):
                VALIDATOR._snapshot_head_bound_inputs(root, authority_sha)

    def test_snapshot_binds_the_loaded_core_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            authority_sha = self.make_repository(
                root,
                "bridge_core.py",
                b"POLICY_SCHEMA_VERSION = 2\n",
            )
            with mock.patch.object(
                VALIDATOR,
                "HEAD_BOUND_INPUTS",
                {"bridge_core": "bridge_core.py"},
            ), mock.patch.object(
                VALIDATOR,
                "_LOADED_BRIDGE_CORE_SOURCE",
                b"POLICY_SCHEMA_VERSION = 3\n",
            ), self.assertRaisesRegex(
                VALIDATOR.ProductionBridgePolicyError,
                "loaded bridge core differs",
            ):
                VALIDATOR._snapshot_head_bound_inputs(root, authority_sha)

    def test_formal_validation_rechecks_inputs_and_head(self) -> None:
        before_bindings, before_payloads = clean_head_binding_snapshot()
        after_bindings = copy.deepcopy(before_bindings)
        after_bindings["policy"]["blob"] = "b" * 40
        with mock.patch.object(
            VALIDATOR,
            "_snapshot_head_bound_inputs",
            side_effect=[
                (before_bindings, before_payloads),
                (after_bindings, before_payloads),
            ],
        ), self.assertRaisesRegex(
            VALIDATOR.ProductionBridgePolicyError,
            "inputs changed during",
        ):
            VALIDATOR.validate_tracked_policy()


class ProductionBridgeReadinessTests(unittest.TestCase):
    def test_live_entrypoint_reports_ready_for_tracked_policy(self) -> None:
        with mock.patch.object(
            VALIDATOR,
            "_snapshot_head_bound_inputs",
            return_value=clean_head_binding_snapshot(),
        ):
            status = VALIDATOR.readiness_status()
            self.assertEqual(status["status"], "ready")
            self.assertIs(status["ready"], True)
            self.assertEqual(status["blockers"], [])

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = VALIDATOR.main()
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()), status)

    def test_live_entrypoint_fails_closed_on_policy_error(self) -> None:
        with mock.patch.object(
            VALIDATOR,
            "validate_tracked_policy",
            side_effect=VALIDATOR.ProductionBridgePolicyError("invalid"),
        ):
            status = VALIDATOR.readiness_status()
        self.assertEqual(
            status,
            {
                "schema_version": 1,
                "ready": False,
                "status": "not_ready",
                "blockers": [
                    {
                        "code": "production_bridge_policy_invalid",
                        "detail": "invalid",
                    }
                ],
            },
        )

    def test_live_entrypoint_fails_closed_on_head_binding_error(self) -> None:
        with mock.patch.object(
            VALIDATOR,
            "_snapshot_head_bound_inputs",
            side_effect=VALIDATOR.ProductionBridgePolicyError(
                "policy working-tree bytes differ from authority HEAD"
            ),
        ):
            status = VALIDATOR.readiness_status()
        self.assertIs(status["ready"], False)
        self.assertEqual(status["status"], "not_ready")
        self.assertEqual(
            status["blockers"][0],
            {
                "code": "production_bridge_policy_invalid",
                "detail": (
                    "policy working-tree bytes differ from authority HEAD"
                ),
            },
        )


if __name__ == "__main__":
    unittest.main()
