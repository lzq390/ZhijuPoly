from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "production_readiness.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("production_readiness", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
READINESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(READINESS)

AUTHORITY_SHA = "a" * 40
AUTHORITY_TREE = "b" * 40
BRIDGE_SHA = "c" * 40
BRIDGE_TREE = "d" * 40
ASSET_DIGEST = "sha256:" + "4" * 64
TARGET_MANIFEST = "sha256:" + "5" * 64
AUTHORITY_MANIFEST = "sha256:" + "6" * 64
DESCRIPTOR_CI_SHA256 = "sha256:" + "7" * 64
PREFETCH_IMAGES_SHA256 = "sha256:" + "8" * 64
PREFETCH_WHEELS_SHA256 = "sha256:" + "9" * 64


def digest(character: str) -> str:
    return "sha256:" + character * 64


def seal(document: dict[str, object]) -> dict[str, object]:
    document["evidence_sha256"] = READINESS.canonical_json_digest(
        {
            key: document[key]
            for key in sorted(set(document) - {"evidence_sha256"})
        }
    )
    return document


def seal_top(document: dict[str, object]) -> dict[str, object]:
    document["evidence_sha256"] = READINESS.canonical_json_digest(
        {
            key: document[key]
            for key in sorted(
                READINESS.TOP_FIELDS - {"evidence_sha256"}
            )
        }
    )
    return document


def migration_contract() -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    target_records: list[dict[str, object]] = [
        {
            "version": "0011_monomer_md_demo_steps",
            "checksum": "1" * 64,
            "kind": "expand",
            "epoch": 1,
            "requires_contracts": [],
        },
        {
            **READINESS.bridge_deploy_core.CONTRACT_MIGRATION,
            "kind": "contract",
            "epoch": 1,
            "requires_contracts": [],
        },
    ]
    registry = READINESS.bridge_deploy_core.expected_migration_registry(
        target_manifest_sha256=TARGET_MANIFEST,
        target_records=target_records,
        authority_manifest_sha256=AUTHORITY_MANIFEST,
        authority_records=[
            *target_records,
            READINESS.bridge_deploy_core.FINAL_MIGRATION_RECORD,
        ],
    )
    return target_records, registry


def policy() -> dict[str, object]:
    _target_records, registry = migration_contract()
    value: dict[str, object] = {
        "schema_version": READINESS.bridge_deploy_core.POLICY_SCHEMA_VERSION,
        "mode": "first-governed-takeover",
        "authority_ref": "refs/heads/main",
        "target_sha": BRIDGE_SHA,
        "target_tree": BRIDGE_TREE,
        "target_ref": f"refs/nexpoly/bridge-target/{BRIDGE_SHA}",
        "target_images": {
            "backend": (
                "ghcr.io/lzq390/nexpoly-backend@sha256:" + "2" * 64
            ),
            "web": "ghcr.io/lzq390/nexpoly-web@sha256:" + "3" * 64,
        },
        "asset_manifest_digest": ASSET_DIGEST,
        "datasets_on_asset_change": [],
        "final_migration": dict(
            READINESS.bridge_deploy_core.FINAL_MIGRATION
        ),
        "accepted_migration_ledgers": registry,
        "external_database_audit": {
            **READINESS.bridge_deploy_core.EXTERNAL_DATABASE_AUDIT_POLICY,
            "media_authority_rules_sha256": digest("a"),
            "audit_role_sql_sha256": digest("b"),
        },
        "required_ci_jobs": sorted(
            READINESS.bridge_deploy_core.REQUIRED_CI_JOBS
        ),
    }
    value["policy_id"] = READINESS.canonical_json_digest(value)
    return value


def image(role: str, revision: str, character: str) -> dict[str, str]:
    root = READINESS.bridge_deploy_core.IMAGE_ROOTS[role]
    index = digest(character)
    return {
        "role": role,
        "digest_ref": f"{root}@{index}",
        "index_digest": index,
        "platform_digest": digest("7"),
        "image_id": digest("8"),
        "revision": revision,
        "source": READINESS.REPOSITORY_SOURCE_URL,
        "version": f"sha-{revision}",
    }


def fixture(*, migration_state: str = "post-0012") -> dict[str, object]:
    bridge_policy = policy()
    target_records, registry = migration_contract()
    ledger_by_state: dict[str, list[dict[str, str]]] = {
        "pre-0012": [
            {
                "version": str(target_records[0]["version"]),
                "checksum": str(target_records[0]["checksum"]),
            }
        ],
        "post-0012": [
            {
                "version": str(record["version"]),
                "checksum": str(record["checksum"]),
            }
            for record in target_records
        ],
        "post-0013": [
            {
                "version": str(record["version"]),
                "checksum": str(record["checksum"]),
            }
            for record in [
                *target_records,
                READINESS.bridge_deploy_core.FINAL_MIGRATION_RECORD,
            ]
        ],
    }
    migration_row = next(
        record for record in registry if record["name"] == migration_state
    )
    policy_sha = READINESS.canonical_json_digest(bridge_policy)
    prefetch_identity = digest("9")
    takeover_binding = digest("a")
    builder = {
        "repository": READINESS.asset_release_contract.BUILD_SOURCE_REPOSITORY,
        "script_path": READINESS.asset_release_contract.BUILD_SOURCE_SCRIPT,
        "commit": "e" * 40,
        "tree": "f" * 40,
        "script_blob": "1" * 40,
    }
    builder_proof_identity: dict[str, object] = {
        "schema_version": 1,
        "bundle_sha256": digest("b"),
        "builder": builder,
        "target": {"sha": BRIDGE_SHA, "tree": BRIDGE_TREE},
        "authority": {"sha": AUTHORITY_SHA, "tree": AUTHORITY_TREE},
        "ancestry": {
            "builder_to_target": True,
            "target_to_authority": True,
            "builder_to_authority": True,
        },
        "network_used": False,
        "temporary_clone_fsck": True,
    }
    builder_proof = {
        **builder_proof_identity,
        "proof_sha256": READINESS.canonical_json_digest(
            builder_proof_identity
        ),
    }
    authority_images = {
        "backend": image("backend", AUTHORITY_SHA, "c"),
        "web": image("web", AUTHORITY_SHA, "d"),
    }
    bridge_images = {
        role: {
            **image(role, BRIDGE_SHA, "2" if role == "backend" else "3"),
            "digest_ref": str(bridge_policy["target_images"][role]),
            "index_digest": str(bridge_policy["target_images"][role]).split(
                "@", 1
            )[1],
        }
        for role in ("backend", "web")
    }
    postgres_audit = {
        major: {
            "digest_ref": reference,
            "index_digest": reference.split("@", 1)[1],
            "platform_digest": digest(
                {"14": "4", "15": "5", "16": "6", "18": "8"}[major]
            ),
            "image_id": digest(
                {"14": "a", "15": "b", "16": "c", "18": "d"}[major]
            ),
        }
        for major, reference in READINESS.POSTGRES_AUDIT_IMAGES.items()
    }
    asset = seal(
        {
            "schema_version": 2,
            "manifest_digest": ASSET_DIGEST,
            "manifest_sha256": ASSET_DIGEST,
            "predecessor_asset_digest": (
                READINESS.asset_release_contract.PREDECESSOR_ASSET_DIGEST
            ),
            "inventory_sha256": digest("d"),
            "file_count": 42,
            "asset_tree_digests": dict(
                READINESS.asset_release_contract.ASSET_TREE_DIGESTS
            ),
            "builder_source": builder,
            "builder_proof": builder_proof,
            "datasets_on_asset_change": [],
            "database_effect": "none",
            "live_pointer_start_sha256": digest("e"),
            "live_pointer_end_sha256": digest("e"),
            "release_validation_sha256": digest("f"),
            "prefetch_identity_sha256": digest("0"),
        }
    )
    document: dict[str, object] = {
        "schema_version": 1,
        "captured_at": "2026-07-17T01:02:03Z",
        "authority": {"sha": AUTHORITY_SHA, "tree": AUTHORITY_TREE},
        "bridge": {"sha": BRIDGE_SHA, "tree": BRIDGE_TREE},
        "git": seal(
            {
                "remote_main_before": AUTHORITY_SHA,
                "remote_main_after": AUTHORITY_SHA,
                "local": {
                    "branch": "main",
                    "source_root": "/data/lzq/gith/nexpoly-bootstrap-fixture",
                    "head_sha": AUTHORITY_SHA,
                    "head_tree": AUTHORITY_TREE,
                    "origin": READINESS.REPOSITORY_SSH_URL,
                    "remote_names": ["origin"],
                    "origin_fetch_urls": [READINESS.REPOSITORY_SSH_URL],
                    "origin_push_urls": [READINESS.REPOSITORY_SSH_URL],
                    "owner_private": True,
                    "standalone_object_database": True,
                    "shallow": False,
                    "dirty_entries": 0,
                    "ignored_entries": 0,
                    "unreachable_objects": 0,
                    "replace_refs": 0,
                    "special_index_entries": 0,
                    "sparse_index": False,
                    "group_or_world_writable": False,
                },
                "target_ref": f"refs/nexpoly/bridge-target/{BRIDGE_SHA}",
                "target_sha": BRIDGE_SHA,
                "target_tree": BRIDGE_TREE,
                "target_is_ancestor": True,
                "policy": bridge_policy,
                "policy_sha256": policy_sha,
            }
        ),
        "ci": seal(
            {
                "authority_sha": AUTHORITY_SHA,
                "descriptor_ci_sha256": DESCRIPTOR_CI_SHA256,
                "jobs": [
                    {
                        "name": name,
                        "conclusion": "success",
                        "head_sha": AUTHORITY_SHA,
                        "run_id": index + 1,
                        "attempt": 1,
                        "workflow_sha256": digest(str(index + 1)),
                    }
                    for index, name in enumerate(
                        sorted(
                            READINESS.bridge_deploy_core.REQUIRED_CI_JOBS
                        )
                    )
                ],
            }
        ),
        "oci": seal(
            {
                "authority_sha": AUTHORITY_SHA,
                "bridge_sha": BRIDGE_SHA,
                "authority_images": authority_images,
                "bridge_images": bridge_images,
                "postgres_audit": postgres_audit,
                "postgres_restore": dict(postgres_audit["16"]),
                "prefetch_images_sha256": PREFETCH_IMAGES_SHA256,
            }
        ),
        "asset": asset,
        "prepared": seal(
            {
                "operation_id": "bridge-operation-0001",
                "status": "ready",
                "descriptor_schema_version": 3,
                "descriptor_sha256": digest("1"),
                "ready_sha256": digest("2"),
                "authority_sha": AUTHORITY_SHA,
                "authority_tree": AUTHORITY_TREE,
                "target_sha": BRIDGE_SHA,
                "target_tree": BRIDGE_TREE,
                "policy_sha256": policy_sha,
                "prefetch_identity_sha256": prefetch_identity,
                "takeover_binding_sha256": takeover_binding,
                "bridge_token_sha256": digest("3"),
                "descriptor_ci_sha256": DESCRIPTOR_CI_SHA256,
                "control_handoff_sha256": digest("4"),
            }
        ),
        "prefetch": seal(
            {
                "operation_id": "prefetch-operation-0001",
                "status": "ready",
                "identity_sha256": prefetch_identity,
                "ready_sha256": digest("4"),
                "authority_sha": AUTHORITY_SHA,
                "authority_tree": AUTHORITY_TREE,
                "target_sha": BRIDGE_SHA,
                "target_tree": BRIDGE_TREE,
                "policy_sha256": policy_sha,
                "asset_manifest_digest": ASSET_DIGEST,
                "asset_evidence_sha256": asset[
                    "prefetch_identity_sha256"
                ],
                "source_readiness_sha256": digest("5"),
                "recovery_tools_sha256": digest("6"),
                "git_bundle_sha256": digest("7"),
                "images_sha256": PREFETCH_IMAGES_SHA256,
                "wheel_caches_sha256": PREFETCH_WHEELS_SHA256,
            }
        ),
        "helpers": seal(
            {
                "status": "ready",
                "installation_sha256": digest("8"),
                "required_helpers": sorted(
                    READINESS.site_helper_contracts.HELPERS
                ),
                "control_source_sha": AUTHORITY_SHA,
                "control_source_tree": AUTHORITY_TREE,
                "control_release_id": "1" * 64,
                "control_manifest_sha256": digest("2"),
                "entrypoint_sha256": digest("3"),
            }
        ),
        "takeover": seal(
            {
                "operation_id": "takeover-operation-0001",
                "status": "completed",
                "authority_sha": AUTHORITY_SHA,
                "authority_tree": AUTHORITY_TREE,
                "binding_sha256": takeover_binding,
                "bootstrap_control_sha256": digest("9"),
                "bootstrap_transaction_sha256": digest("a"),
                "bootstrap_transaction_identity_sha256": digest("b"),
            }
        ),
        "alias": seal(
            {
                "operation_id": "alias-operation-0001",
                "status": "completed",
                "completed_marker_sha256": digest("9"),
                "backup_sha256": digest("a"),
                "restore_audit_sha256": digest("b"),
                "postgres_system_identifier_sha256": digest("c"),
            }
        ),
        "external_media": seal(
            {
                "status": "ready",
                "captured_at": "2026-07-17T01:02:03Z",
                "audit_relative_path": (
                    READINESS.EXTERNAL_DATABASE_AUDIT_RELATIVE_PATH.as_posix()
                ),
                "audit_sha256": digest("d"),
                "validation_sha256": digest("e"),
                "media_authority_rules_sha256": digest("a"),
                "registry_sha256": digest("f"),
                "inventory_complete": True,
                "writable_target": {
                    "stack": "production",
                    "database": "nexpoly",
                },
                "requires_0014": False,
                "media_count": 2,
                "cas": None,
            }
        ),
        "postgres": seal(
            {
                "status": "ready",
                "container_id_sha256": digest("1"),
                "image_digest": digest("2"),
                "volume_identity_sha256": digest("3"),
                "system_identifier_sha256": digest("c"),
                "ledger_source_sha256": migration_row["ledger_sha256"],
                "running": True,
                "unchanged_from_alias": True,
                "read_only_probe": True,
            }
        ),
        "migrations": seal(
            {
                "records": ledger_by_state[migration_state],
                "ledger_sha256": migration_row["ledger_sha256"],
                "manifest_sha256": migration_row["manifest_sha256"],
                "registry_name": migration_state,
                "0012_applied": migration_state != "pre-0012",
                "0013_applied": migration_state == "post-0013",
            }
        ),
        "mutable_data": seal(
            {
                "operation_id": "bridge-operation-0001",
                "status": "ready",
                "captured_at": "2026-07-17T01:02:03Z",
                "audit_relative_path": (
                    READINESS.MUTABLE_DATA_AUDIT_RELATIVE_PATH.as_posix()
                ),
                "audit_sha256": digest("1"),
                "validation_sha256": digest("2"),
                "snapshot_sha256": digest("3"),
                "business_tables_sha256": digest("4"),
                "static_tables_sha256": digest("5"),
                "postgres_runtime_sha256": digest("6"),
                "system_identifier_sha256": digest("c"),
                "migration_ledger_sha256": migration_row["ledger_sha256"],
                "transaction_read_only": True,
            }
        ),
        "native_runtime": seal(
            {
                "status": "ready",
                "authority_sha": AUTHORITY_SHA,
                "python_version": "3.12.3",
                "uv_version": "0.11.21",
                "build_lock_sha256": digest("4"),
                "wheel_filename": "aimnet2calc-0.1-py3-none-any.whl",
                "wheel_sha256": digest("5"),
                "wheel_inventory_sha256": digest("6"),
                "record_sha256": digest("7"),
                "aimnet_source": {
                    "commit": "2" * 40,
                    "tree": "3" * 40,
                    "archive_sha256": digest("8"),
                },
                "model_registry_sha256": digest("9"),
                "models_sha256": digest("a"),
                "prefetch_wheel_caches_sha256": PREFETCH_WHEELS_SHA256,
                "gpu_acceptance": {
                    "status": "passed",
                    "authority_tree": AUTHORITY_TREE,
                    "image_digest": authority_images["backend"]["index_digest"],
                    "model_registry_sha256": digest("9"),
                    "gpus": [1, 3],
                    "production_gpu_2_touched": False,
                    "report_sha256": digest("b"),
                },
            }
        ),
        "capacity": seal(
            {
                "status": "sufficient",
                "disk_bytes_available": 1_000,
                "disk_bytes_required": 600,
                "memory_bytes_available": 800,
                "memory_bytes_required": 400,
                "wheel_cache_bytes": 100,
                "asset_release_bytes": 200,
                "backup_bytes_required": 300,
            }
        ),
        "conflicts": seal(
            {
                "deploy": [],
                "contract_0012": [],
                "alias": [],
                "takeover": [],
                "bridge": [],
                "prepared": [],
                "control_handoff": [],
            }
        ),
        "observation": seal(
            {
                "collector_sha256": digest("c"),
                "production_command_read_only": True,
                "git_fetch_used": False,
                "image_pull_used": False,
                "container_mutation_used": False,
                "service_mutation_used": False,
                "state_write_used": False,
                "database_transaction_read_only": True,
            }
        ),
    }
    return seal_top(document)


def reseal_section(document: dict[str, object], name: str) -> None:
    seal(document[name])  # type: ignore[arg-type]
    seal_top(document)


class ProductionReadinessTests(unittest.TestCase):
    def validate(
        self, document: dict[str, object]
    ) -> dict[str, object]:
        return READINESS.validate_evidence(
            document,
            expected_authority=AUTHORITY_SHA,
            expected_bridge=BRIDGE_SHA,
            enforce_freshness=False,
            now=dt.datetime(2026, 7, 17, 1, 3, tzinfo=dt.timezone.utc),
        )

    def _live_helper_inputs(
        self,
        runtime: Path,
        *,
        collector_sha256: str,
        observation_sha256: str,
    ) -> tuple[
        dict[str, object],
        dict[str, object],
        mock.Mock,
        Path,
    ]:
        report = {
            "schema_version": 1,
            "ready": True,
            "runtime_root": str(runtime),
            "executed_helpers": False,
            "helpers": {
                name: {
                    **contract,
                    "path": str(runtime / "config" / name),
                    "sha256": (
                        collector_sha256
                        if name == "production-readiness-collector"
                        else digest("e")
                    ),
                    "mode": "0700",
                }
                for name, contract in sorted(
                    READINESS.site_helper_contracts.HELPERS.items()
                )
            },
        }
        control_root = runtime / "control"
        control_root.mkdir(parents=True)
        manifest_path = control_root / "control-release.json"
        entrypoint_path = control_root / "production_readiness.py"
        manifest_path.write_bytes(b"sealed control manifest\n")
        entrypoint_path.write_bytes(b"sealed readiness entrypoint\n")

        document = fixture()
        document["helpers"]["installation_sha256"] = (  # type: ignore[index]
            READINESS.canonical_json_digest(report)
        )
        document["helpers"]["control_manifest_sha256"] = (  # type: ignore[index]
            READINESS.sha256_file(manifest_path)
        )
        document["helpers"]["entrypoint_sha256"] = (  # type: ignore[index]
            READINESS.sha256_file(entrypoint_path)
        )
        document["observation"]["collector_sha256"] = (  # type: ignore[index]
            observation_sha256
        )
        reseal_section(document, "helpers")
        reseal_section(document, "observation")
        validated = self.validate(document)

        release_id = "1" * 64
        selector = mock.Mock()
        selector.CONTROL_MANIFEST_NAME = manifest_path.name
        selector.load_active_control.return_value = (
            {
                "source_sha": AUTHORITY_SHA,
                "source_tree": AUTHORITY_TREE,
                "release_id": release_id,
            },
            {
                "source_sha": AUTHORITY_SHA,
                "source_tree": AUTHORITY_TREE,
                "release_id": release_id,
                "entrypoints": {
                    "production-readiness": {
                        "kind": "python",
                        "file": entrypoint_path.name,
                    }
                },
            },
            control_root,
        )
        return validated, report, selector, control_root

    def test_valid_fixture_is_ready_and_output_is_sanitized(self) -> None:
        validated = self.validate(fixture())
        output = READINESS.readiness_output(validated)
        self.assertTrue(output["ready"])
        self.assertEqual(output["migration"]["state"], "post-0012")
        self.assertEqual(
            set(output["checks"]),
            set(READINESS.SECTION_NAMES),
        )
        encoded = json.dumps(output)
        self.assertNotIn("container_id\"", encoded)
        self.assertNotIn("system_identifier\"", encoded)
        self.assertNotIn("database_system_identifier", encoded)
        self.assertNotIn("operation-0001", encoded)

    def test_all_frozen_migration_states_are_accepted(self) -> None:
        for state in ("pre-0012", "post-0012", "post-0013"):
            with self.subTest(state=state):
                validated = self.validate(fixture(migration_state=state))
                self.assertEqual(validated["migration"]["name"], state)

    def test_unknown_field_is_rejected_even_when_top_is_resealed(self) -> None:
        document = fixture()
        document["git"]["unexpected"] = True  # type: ignore[index]
        reseal_section(document, "git")
        with self.assertRaisesRegex(
            READINESS.ProductionReadinessError, "invalid shape"
        ):
            self.validate(document)

    def test_remote_main_cas_drift_is_rejected(self) -> None:
        document = fixture()
        document["git"]["remote_main_after"] = "0" * 40  # type: ignore[index]
        reseal_section(document, "git")
        with self.assertRaisesRegex(
            READINESS.ProductionReadinessError, "not trusted"
        ):
            self.validate(document)

    def test_oci_index_and_local_image_id_are_distinct_and_validated(self) -> None:
        document = fixture()
        backend = document["oci"]["authority_images"]["backend"]  # type: ignore[index]
        backend["index_digest"] = backend["image_id"]
        reseal_section(document, "oci")
        with self.assertRaisesRegex(
            READINESS.ProductionReadinessError, "authority differs"
        ):
            self.validate(document)

    def test_postgres_audit_map_and_pg16_restore_identity_are_distinct(
        self,
    ) -> None:
        document = fixture()
        document["oci"]["postgres_audit"].pop("18")  # type: ignore[index]
        reseal_section(document, "oci")
        with self.assertRaisesRegex(
            READINESS.ProductionReadinessError,
            "PostgreSQL audit images",
        ):
            self.validate(document)

        document = fixture()
        document["oci"]["postgres_restore"]["image_id"] = digest("f")  # type: ignore[index]
        reseal_section(document, "oci")
        with self.assertRaisesRegex(
            READINESS.ProductionReadinessError,
            "exact PG16 audit image",
        ):
            self.validate(document)

    def test_asset_pointer_or_database_effect_change_is_rejected(self) -> None:
        for key, value in (
            ("live_pointer_end_sha256", digest("0")),
            ("datasets_on_asset_change", ["online_knowledge"]),
        ):
            document = fixture()
            document["asset"][key] = value  # type: ignore[index]
            reseal_section(document, "asset")
            with self.subTest(key=key), self.assertRaises(
                READINESS.ProductionReadinessError
            ):
                self.validate(document)

    def test_postgres_alias_identity_mismatch_is_rejected(self) -> None:
        document = fixture()
        document["postgres"]["system_identifier_sha256"] = digest("0")  # type: ignore[index]
        reseal_section(document, "postgres")
        with self.assertRaisesRegex(
            READINESS.ProductionReadinessError, "not preserved"
        ):
            self.validate(document)

    def test_mutable_data_must_match_pg_and_ledger(self) -> None:
        for field in (
            "system_identifier_sha256",
            "migration_ledger_sha256",
        ):
            document = fixture()
            document["mutable_data"][field] = digest("0")  # type: ignore[index]
            reseal_section(document, "mutable_data")
            with self.subTest(field=field), self.assertRaisesRegex(
                READINESS.ProductionReadinessError,
                "mutable-data seal",
            ):
                self.validate(document)

    def test_prefetch_ci_oci_and_wheel_cross_bindings_are_required(self) -> None:
        mutations = (
            ("ci", "descriptor_ci_sha256", digest("0")),
            ("oci", "prefetch_images_sha256", digest("0")),
            (
                "native_runtime",
                "prefetch_wheel_caches_sha256",
                digest("0"),
            ),
        )
        for section, field, value in mutations:
            document = fixture()
            document[section][field] = value  # type: ignore[index]
            reseal_section(document, section)
            with self.subTest(section=section), self.assertRaisesRegex(
                READINESS.ProductionReadinessError,
                "prepared dependencies",
            ):
                self.validate(document)

    def test_wrong_0013_checksum_is_rejected(self) -> None:
        document = fixture(migration_state="post-0013")
        document["migrations"]["records"][-1]["checksum"] = "0" * 64  # type: ignore[index]
        reseal_section(document, "migrations")
        with self.assertRaisesRegex(
            READINESS.ProductionReadinessError, "outside B/F"
        ):
            self.validate(document)

    def test_gpu2_contact_is_rejected(self) -> None:
        document = fixture()
        acceptance = document["native_runtime"]["gpu_acceptance"]  # type: ignore[index]
        acceptance["production_gpu_2_touched"] = True
        reseal_section(document, "native_runtime")
        with self.assertRaisesRegex(
            READINESS.ProductionReadinessError, "GPU acceptance"
        ):
            self.validate(document)

    def test_any_conflict_marker_is_rejected(self) -> None:
        document = fixture()
        document["conflicts"]["deploy"] = ["state/deploy-in-progress.json"]  # type: ignore[index]
        reseal_section(document, "conflicts")
        with self.assertRaisesRegex(
            READINESS.ProductionReadinessError, "conflict marker"
        ):
            self.validate(document)

    def test_optional_media_cas_is_strict_when_present(self) -> None:
        document = fixture()
        cas = {
            "schema_version": 1,
            "status": "ready",
            "registry_sha256": digest("f"),
            "manifest_sha256": digest("1"),
            "inventory_sha256": digest("2"),
            "media_count": 2,
        }
        cas["evidence_sha256"] = READINESS.canonical_json_digest(cas)
        document["external_media"]["cas"] = cas  # type: ignore[index]
        reseal_section(document, "external_media")
        validated = self.validate(document)
        self.assertEqual(
            READINESS.readiness_output(validated)["external_media"][
                "cas_status"
            ],
            "ready",
        )
        document["external_media"]["cas"]["unexpected"] = True  # type: ignore[index]
        reseal_section(document, "external_media")
        with self.assertRaisesRegex(
            READINESS.ProductionReadinessError, "invalid shape"
        ):
            self.validate(document)

    def test_offline_cli_success_and_failure_are_machine_readable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="readiness-fixture-") as raw:
            path = Path(raw) / "evidence.json"
            path.write_text(json.dumps(fixture()), encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                result = READINESS.main(
                    [
                        "--authority",
                        AUTHORITY_SHA,
                        "--bridge",
                        BRIDGE_SHA,
                        "--offline-fixture",
                        str(path),
                    ]
                )
            self.assertEqual(result, 0, stderr.getvalue())
            self.assertTrue(json.loads(stdout.getvalue())["ready"])

            broken = fixture()
            broken["conflicts"]["bridge"] = ["foreign-token"]  # type: ignore[index]
            reseal_section(broken, "conflicts")
            path.write_text(json.dumps(broken), encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                result = READINESS.main(
                    [
                        "--authority",
                        AUTHORITY_SHA,
                        "--bridge",
                        BRIDGE_SHA,
                        "--offline-fixture",
                        str(path),
                    ]
                )
            error = json.loads(stderr.getvalue())
            self.assertEqual(result, 2)
            self.assertFalse(error["ready"])
            self.assertEqual(error["error"]["code"], "evidence_rejected")
            self.assertNotIn("foreign-token", stderr.getvalue())

    def test_live_mode_requires_private_evidence_and_calls_read_only_binding(self) -> None:
        with tempfile.TemporaryDirectory(prefix="readiness-live-") as raw:
            runtime = Path(raw)
            os.chmod(runtime, 0o700)
            evidence = runtime / READINESS.EVIDENCE_RELATIVE_PATH
            evidence.parent.mkdir(parents=True, mode=0o700)
            os.chmod(runtime / "audit", 0o700)
            os.chmod(evidence.parent, 0o700)
            evidence.write_text(json.dumps(fixture()), encoding="utf-8")
            os.chmod(evidence, 0o600)
            stdout = io.StringIO()
            with (
                mock.patch.dict(
                    os.environ, {"NEXPOLY_ALLOW_TEST_ROOT": "1"}
                ),
                mock.patch.object(
                    READINESS,
                    "validate_live_bindings",
                ) as live,
                mock.patch.object(
                    READINESS.dt,
                    "datetime",
                    wraps=dt.datetime,
                ) as datetime_mock,
                contextlib.redirect_stdout(stdout),
            ):
                datetime_mock.now.return_value = dt.datetime(
                    2026, 7, 17, 1, 3, tzinfo=dt.timezone.utc
                )
                result = READINESS.main(
                    [
                        "--authority",
                        AUTHORITY_SHA,
                        "--bridge",
                        BRIDGE_SHA,
                        "--runtime-root",
                        str(runtime),
                    ]
                )
            self.assertEqual(result, 0)
            live.assert_called_once()

    def test_live_helper_inventory_binds_exact_collector_bytes(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="readiness-live-helper-"
        ) as raw:
            runtime = Path(raw)
            collector_sha256 = digest("c")
            validated, report, selector, control_root = (
                self._live_helper_inputs(
                    runtime,
                    collector_sha256=collector_sha256,
                    observation_sha256=collector_sha256,
                )
            )
            with (
                mock.patch.object(
                    READINESS.site_helper_contracts,
                    "inspect_helper_installation",
                    return_value=report,
                ),
                mock.patch.object(
                    READINESS,
                    "_load_python_module",
                    return_value=selector,
                ),
                mock.patch.dict(
                    os.environ,
                    {
                        "NEXPOLY_ACTIVE_CONTROL_ROOT": str(control_root),
                        "NEXPOLY_ACTIVE_CONTROL_RELEASE_ID": "1" * 64,
                    },
                ),
            ):
                READINESS._validate_live_helpers(runtime, validated)

    def test_live_collector_tamper_and_digest_mismatch_are_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="readiness-live-helper-tamper-"
        ) as raw:
            root = Path(raw)
            original_sha256 = digest("c")
            validated, report, _selector, _control_root = (
                self._live_helper_inputs(
                    root / "original",
                    collector_sha256=original_sha256,
                    observation_sha256=original_sha256,
                )
            )
            tampered = json.loads(json.dumps(report))
            tampered["helpers"]["production-readiness-collector"][  # type: ignore[index]
                "sha256"
            ] = digest("d")
            with (
                mock.patch.object(
                    READINESS.site_helper_contracts,
                    "inspect_helper_installation",
                    return_value=tampered,
                ),
                self.assertRaisesRegex(
                    READINESS.ProductionReadinessError,
                    "installed site helpers changed",
                ),
            ):
                READINESS._validate_live_helpers(
                    root / "original",
                    validated,
                )

            resealed, tampered_report, _selector, _control_root = (
                self._live_helper_inputs(
                    root / "resealed",
                    collector_sha256=digest("d"),
                    observation_sha256=original_sha256,
                )
            )
            with (
                mock.patch.object(
                    READINESS.site_helper_contracts,
                    "inspect_helper_installation",
                    return_value=tampered_report,
                ),
                self.assertRaisesRegex(
                    READINESS.ProductionReadinessError,
                    "collector differs",
                ),
            ):
                READINESS._validate_live_helpers(
                    root / "resealed",
                    resealed,
                )

    def test_live_conflict_scan_is_read_only_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="readiness-conflict-") as raw:
            runtime = Path(raw)
            marker = runtime / "state/deploy-in-progress.json"
            marker.parent.mkdir()
            marker.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(
                READINESS.ProductionReadinessError, "conflict marker"
            ):
                READINESS._validate_live_conflicts(runtime)
            self.assertEqual(marker.read_text(encoding="utf-8"), "{}")

    def test_live_external_media_binds_current_authority_and_registry_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="readiness-live-media-"
        ) as raw:
            runtime = Path(raw)
            config = runtime / "config"
            audit_root = runtime / "audit"
            config.mkdir(mode=0o700)
            audit_root.mkdir(mode=0o700)
            rules_payload = b'{"schema_version":1}\n'
            rules = config / "postgres-media-authority-rules.json"
            rules.write_bytes(rules_payload)
            os.chmod(rules, 0o600)
            rules_digest = READINESS.sha256_bytes(rules_payload)
            registry_document = {
                "media_authority_rules_sha256": rules_digest,
            }
            registry_payload = json.dumps(
                registry_document,
                sort_keys=True,
                separators=(",", ":"),
            ).encode() + b"\n"
            registry = config / "postgres-media-registry.json"
            registry.write_bytes(registry_payload)
            os.chmod(registry, 0o600)
            audit_document = {"fixture": True}
            audit_payload = json.dumps(
                audit_document,
                sort_keys=True,
                separators=(",", ":"),
            ).encode() + b"\n"
            audit = (
                runtime
                / READINESS.EXTERNAL_DATABASE_AUDIT_RELATIVE_PATH
            )
            audit.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(audit.parent, 0o700)
            audit.write_bytes(audit_payload)
            os.chmod(audit, 0o600)
            normalized = {
                "media": [{}, {}],
                "media_registry": {
                    "captured_at": "2026-07-17T01:02:03Z",
                },
                "requires_0014": False,
            }
            section = {
                "audit_relative_path": (
                    READINESS.EXTERNAL_DATABASE_AUDIT_RELATIVE_PATH.as_posix()
                ),
                "audit_sha256": READINESS.sha256_bytes(audit_payload),
                "validation_sha256": (
                    READINESS.canonical_json_digest(normalized)
                ),
                "media_authority_rules_sha256": rules_digest,
                "registry_sha256": READINESS.sha256_bytes(
                    registry_payload
                ),
                "media_count": 2,
                "captured_at": "2026-07-17T01:02:03Z",
            }
            validated = {"sections": {"external_media": section}}
            with mock.patch.object(
                READINESS.site_helper_contracts,
                "validate_external_database_audit",
                return_value=normalized,
            ):
                READINESS._validate_live_external_media(
                    runtime,
                    validated,
                )
                rules.write_bytes(b'{"schema_version":2}\n')
                os.chmod(rules, 0o600)
                with self.assertRaisesRegex(
                    READINESS.ProductionReadinessError,
                    "authority or registry changed",
                ):
                    READINESS._validate_live_external_media(
                        runtime,
                        validated,
                    )

    def test_live_prepared_requires_exact_selector_handoff_inventory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="readiness-live-handoff-"
        ) as raw:
            runtime = Path(raw)
            operation_id = "bridge-operation-0001"
            operation = runtime / "state/prepared" / operation_id
            handoff_root = runtime / "state/control-handoffs"
            operation.mkdir(parents=True, mode=0o700)
            handoff_root.mkdir(parents=True, mode=0o700)
            os.chmod(runtime / "state", 0o700)
            os.chmod(runtime / "state/prepared", 0o700)
            previous = {"release_id": "a" * 64}
            executor = {"release_id": "b" * 64}
            prefetch = {
                "operation_id": "prefetch-operation-0001",
                "identity_sha256": digest("1"),
            }
            takeover = {"binding_sha256": digest("2")}
            normalized = {
                "schema_version": 3,
                "operation_id": operation_id,
                "bridge": {
                    "authority": {
                        "sha": AUTHORITY_SHA,
                        "tree": AUTHORITY_TREE,
                    },
                    "target": {
                        "sha": BRIDGE_SHA,
                        "tree": BRIDGE_TREE,
                        "exact_ref": (
                            f"refs/nexpoly/bridge-target/{BRIDGE_SHA}"
                        ),
                    },
                    "policy": {"policy_id": digest("3")},
                    "policy_sha256": digest("4"),
                },
                "prefetch": prefetch,
                "legacy_takeover": takeover,
                "ci": {"fixture": True},
                "controller": {
                    "previous_active_control": previous,
                    "executor_control": executor,
                    "executor_control_sha256": (
                        READINESS.canonical_json_digest(executor)
                    ),
                },
                "monomer_md": {"slot_record_sha256": digest("5")},
            }
            descriptor_payload = b'{"fixture":"descriptor"}\n'
            descriptor_path = operation / "descriptor.json"
            descriptor_path.write_bytes(descriptor_payload)
            os.chmod(descriptor_path, 0o600)
            descriptor_sha256 = READINESS.sha256_bytes(
                descriptor_payload
            )
            ready = {
                "schema_version": 1,
                "status": "ready",
                "operation_id": operation_id,
                "source_sha": BRIDGE_SHA,
                "descriptor_sha256": descriptor_sha256,
                "executor_control": executor,
                "executor_control_sha256": (
                    READINESS.canonical_json_digest(executor)
                ),
                "slot_record_sha256": digest("5"),
                "prepared_at": "2026-07-17T01:02:03Z",
            }
            ready_payload = (
                READINESS.canonical_json_bytes(ready) + b"\n"
            )
            ready_path = operation / "ready.json"
            ready_path.write_bytes(ready_payload)
            os.chmod(ready_path, 0o600)
            handoff = {
                "schema_version": 2,
                "protocol_version": 1,
                "operation_id": operation_id,
                "authority_sha": AUTHORITY_SHA,
                "authority_tree": AUTHORITY_TREE,
                "target_sha": BRIDGE_SHA,
                "target_tree": BRIDGE_TREE,
                "target_ref": (
                    f"refs/nexpoly/bridge-target/{BRIDGE_SHA}"
                ),
                "policy_id": digest("3"),
                "policy_sha256": digest("4"),
                "prefetch_operation_id": prefetch["operation_id"],
                "prefetch": prefetch,
                "legacy_takeover": takeover,
                "previous_active_control": previous,
                "previous_active_control_sha256": (
                    READINESS.canonical_json_digest(previous)
                ),
                "executor_control": executor,
                "executor_control_sha256": (
                    READINESS.canonical_json_digest(executor)
                ),
                "created_at": "2026-07-17T01:02:03Z",
            }
            handoff_payload = (
                READINESS.canonical_json_bytes(handoff) + b"\n"
            )
            handoff_path = handoff_root / f"{operation_id}.json"
            handoff_path.write_bytes(handoff_payload)
            os.chmod(handoff_path, 0o600)
            section = {
                "operation_id": operation_id,
                "descriptor_sha256": descriptor_sha256,
                "ready_sha256": READINESS.sha256_bytes(ready_payload),
                "policy_sha256": digest("4"),
                "prefetch_identity_sha256": digest("1"),
                "takeover_binding_sha256": digest("2"),
                "bridge_token_sha256": digest("6"),
                "descriptor_ci_sha256": (
                    READINESS.canonical_json_digest({"fixture": True})
                ),
                "control_handoff_sha256": (
                    READINESS.sha256_bytes(handoff_payload)
                ),
            }
            validated = {
                "authority": {"sha": AUTHORITY_SHA, "tree": AUTHORITY_TREE},
                "bridge": {"sha": BRIDGE_SHA, "tree": BRIDGE_TREE},
                "policy": {"policy_id": digest("3")},
                "sections": {"prepared": section},
            }
            pull_module = mock.Mock()
            pull_module.validate_descriptor.return_value = normalized
            pull_module._control_runtime.PROTOCOL_VERSION = 1
            token = {
                "operation_id": operation_id,
                "policy_id": digest("3"),
                "descriptor_sha256": descriptor_sha256,
                "status": "prepared",
            }
            with (
                mock.patch.object(
                    READINESS,
                    "_load_sibling",
                    return_value=pull_module,
                ),
                mock.patch.object(
                    READINESS.bridge_deploy_core,
                    "load_token_authority",
                    return_value=token,
                ),
                mock.patch.object(
                    READINESS.bridge_deploy_core,
                    "token_record_digest",
                    return_value=digest("6"),
                ),
            ):
                READINESS._validate_live_prepared(runtime, validated)
                extra = handoff_root / "foreign.json"
                extra.write_bytes(b"{}\n")
                os.chmod(extra, 0o600)
                with self.assertRaisesRegex(
                    READINESS.ProductionReadinessError,
                    "handoff inventory",
                ):
                    READINESS._validate_live_prepared(runtime, validated)

    def test_live_takeover_requires_completed_bootstrap_child_transaction(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="readiness-bootstrap-child-"
        ) as raw:
            runtime = Path(raw)
            operation_id = "takeover-operation-0001"
            state = runtime / "state"
            children = (
                state / "legacy-takeover/bootstrap-children"
            )
            children.mkdir(parents=True, mode=0o700)
            os.chmod(runtime, 0o700)
            os.chmod(state, 0o700)
            os.chmod(state / "legacy-takeover", 0o700)
            binding = {
                "operation_id": operation_id,
                "binding_sha256": digest("1"),
            }
            active = {
                "source_sha": AUTHORITY_SHA,
                "source_tree": AUTHORITY_TREE,
            }
            active_payload = (
                READINESS.canonical_json_bytes(active) + b"\n"
            )
            active_path = state / "active-control.json"
            active_path.write_bytes(active_payload)
            os.chmod(active_path, 0o600)
            active_sha256 = READINESS.sha256_bytes(active_payload)
            bootstrap = {
                "schema_version": 2,
                "status": "completed",
                "source_sha": AUTHORITY_SHA,
                "source_tree": AUTHORITY_TREE,
                "legacy_takeover": binding,
                "active_control": active,
            }
            bootstrap_payload = (
                READINESS.canonical_json_bytes(bootstrap) + b"\n"
            )
            bootstrap_path = state / "bootstrap-control.json"
            bootstrap_path.write_bytes(bootstrap_payload)
            os.chmod(bootstrap_path, 0o600)
            bootstrap_sha256 = READINESS.sha256_bytes(
                bootstrap_payload
            )
            identity = {"legacy_takeover": binding}
            identity_sha256 = READINESS.canonical_json_digest(identity)
            child = {
                "status": "completed",
                "phase": "completed",
                "operation_id": operation_id,
                "source_sha": AUTHORITY_SHA,
                "source_tree": AUTHORITY_TREE,
                "identity": identity,
                "identity_sha256": identity_sha256,
                "step_evidence": {
                    "authority_commit": {
                        "bootstrap_control_sha256": bootstrap_sha256,
                        "active_control_sha256": active_sha256,
                        "active_control": active,
                    }
                },
            }
            child_payload = (
                READINESS.canonical_json_bytes(child) + b"\n"
            )
            child_name = f"{operation_id}-{AUTHORITY_SHA}.json"
            child_path = children / child_name
            child_path.write_bytes(child_payload)
            os.chmod(child_path, 0o600)
            section = {
                "operation_id": operation_id,
                "binding_sha256": digest("1"),
                "bootstrap_control_sha256": bootstrap_sha256,
                "bootstrap_transaction_sha256": (
                    READINESS.sha256_bytes(child_payload)
                ),
                "bootstrap_transaction_identity_sha256": (
                    identity_sha256
                ),
            }
            validated = {
                "authority": {
                    "sha": AUTHORITY_SHA,
                    "tree": AUTHORITY_TREE,
                },
                "sections": {"takeover": section},
            }
            legacy_module = mock.Mock()
            legacy_module.validate_completed.return_value = binding
            bootstrap_module = mock.Mock()
            bootstrap_module._validate_bootstrap_transaction.side_effect = (
                lambda document, **_kwargs: document
            )

            def sibling(_name: str, filename: str):
                if filename == "legacy_takeover_evidence.py":
                    return legacy_module
                if filename == "bootstrap_pull_deploy.py":
                    return bootstrap_module
                raise AssertionError(filename)

            with mock.patch.object(
                READINESS,
                "_load_sibling",
                side_effect=sibling,
            ):
                READINESS._validate_live_takeover(runtime, validated)
                extra = children / "foreign.json"
                extra.write_bytes(b"{}\n")
                os.chmod(extra, 0o600)
                with self.assertRaisesRegex(
                    READINESS.ProductionReadinessError,
                    "child transaction inventory",
                ):
                    READINESS._validate_live_takeover(
                        runtime,
                        validated,
                    )

    def test_live_evidence_must_be_fresh_and_mode_0600(self) -> None:
        with self.assertRaisesRegex(
            READINESS.ProductionReadinessError, "stale"
        ):
            READINESS.validate_evidence(
                fixture(),
                expected_authority=AUTHORITY_SHA,
                expected_bridge=BRIDGE_SHA,
                enforce_freshness=True,
                now=dt.datetime(
                    2026, 7, 17, 2, 0, tzinfo=dt.timezone.utc
                ),
            )
        stale_section = fixture()
        stale_section["external_media"]["captured_at"] = (  # type: ignore[index]
            "2026-07-17T00:00:00Z"
        )
        reseal_section(stale_section, "external_media")
        with self.assertRaisesRegex(
            READINESS.ProductionReadinessError, "external media evidence is stale"
        ):
            self.validate(stale_section)
        with tempfile.TemporaryDirectory(prefix="readiness-mode-") as raw:
            path = Path(raw) / "evidence.json"
            path.write_text(json.dumps(fixture()), encoding="utf-8")
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(
                READINESS.ProductionReadinessError, "unsafe"
            ):
                READINESS._read_json_file(path, private=True)

    def test_cli_parse_errors_are_sanitized_json(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = READINESS.main(["--unknown"])
        error = json.loads(stderr.getvalue())
        self.assertEqual(result, 2)
        self.assertEqual(error["error"]["code"], "evidence_rejected")
        self.assertNotIn("usage:", stderr.getvalue())

    def test_output_schema_is_closed(self) -> None:
        schema = READINESS.output_json_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["properties"]["checks"]["required"]),
            set(READINESS.SECTION_NAMES),
        )
        self.assertFalse(
            schema["properties"]["external_media"]["additionalProperties"]
        )

    def test_controller_loads_siblings_under_isolated_python(self) -> None:
        completed = subprocess.run(
            [
                "/usr/bin/python3",
                "-I",
                "-B",
                str(SCRIPT),
                "--print-output-schema",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertFalse(json.loads(completed.stdout)["additionalProperties"])
        self.assertEqual(completed.stderr, "")


if __name__ == "__main__":
    unittest.main()
