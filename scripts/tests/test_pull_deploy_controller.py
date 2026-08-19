from __future__ import annotations

import importlib.util
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts.tests.bridge_manifest_fixtures import (
    B_MANIFEST_PAYLOAD,
    B_MANIFEST_RECORDS,
    B_MANIFEST_SHA256,
    F_MANIFEST_RECORDS,
    F_MANIFEST_SHA256,
)
from scripts.tests.mutable_audit_role_fixtures import role_security_evidence
from scripts.tests.test_postgres_media_evidence import (
    external_inventory_fixture as external_inventory_v3_fixture,
    role_security_fields as external_role_security_fields,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "pull_deploy_controller.py"
SPEC = importlib.util.spec_from_file_location("pull_deploy_controller", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CONTROLLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROLLER)


PREVIOUS_SHA = "1" * 40
PREVIOUS_TREE = "2" * 40
TARGET_SHA = "3" * 40
TARGET_TREE = "4" * 40
OPERATION_ID = "deploy-20260716-0001"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
B_MANIFEST_DIGEST = B_MANIFEST_SHA256
F_MANIFEST_DIGEST = F_MANIFEST_SHA256


def write_private(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    path.write_text(payload, encoding="utf-8")
    os.chmod(path, 0o600)


def seed_complete_private_git_layout(production: Path) -> None:
    """Create the minimum owner-private files-backend write domain."""

    git_dir = production / ".git"
    for relative in (
        "refs/heads",
        "refs/remotes/nexpoly-deploy",
        "objects",
        "logs/refs/remotes/nexpoly-deploy",
    ):
        (git_dir / relative).mkdir(
            parents=True,
            exist_ok=True,
            mode=0o700,
        )
    for path in (git_dir, *git_dir.rglob("*")):
        if path.is_dir():
            path.chmod(0o700)
    write_private(git_dir / "HEAD", "ref: refs/heads/main\n")
    write_private(git_dir / "config", "[core]\n")
    write_private(git_dir / "index", "fixture-index\n")
    write_private(git_dir / "refs/heads/main", PREVIOUS_SHA + "\n")
    write_private(
        git_dir / "refs/remotes/nexpoly-deploy/main",
        TARGET_SHA + "\n",
    )
    write_private(
        git_dir / "logs/refs/remotes/nexpoly-deploy/main",
        "fixture-deploy-reflog\n",
    )


def v4_recovery_marker(marker: dict[str, object]) -> dict[str, object]:
    """Upgrade legacy hand-written recovery fixtures to descriptor-v4 effects."""

    upgraded = dict(marker)
    upgraded["schema_version"] = CONTROLLER.MARKER_SCHEMA_VERSION
    upgraded.setdefault(
        "worker_env_switched", bool(upgraded.get("source_switched"))
    )
    upgraded.setdefault(
        "dft_runtime_switched", bool(upgraded.get("source_switched"))
    )
    upgraded.setdefault(
        "dft_unit_switched", bool(upgraded.get("unit_switched"))
    )
    upgraded.setdefault("dft_guard_scheduling_stopped", False)
    if upgraded.get("action") == "deploy":
        operation_id = str(upgraded["operation_id"])
        upgraded.setdefault(
            "postgres_rehearsal",
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "target_sha": upgraded["source_sha"],
                "descriptor_sha256": upgraded["descriptor_sha256"],
                "path": (
                    "/var/lib/nexpoly-fixture/audit/deployment-rehearsals/"
                    f"{operation_id}/report.json"
                ),
                "file_sha256": "sha256:" + "a" * 64,
                "report_sha256": "sha256:" + "b" * 64,
                "completed_at": "2026-01-01T00:00:00Z",
                "dump_sha256": "sha256:" + "c" * 64,
                "journal_head_sha256": "sha256:" + "d" * 64,
            },
        )
    return upgraded


def image_record(role: str, sha: str = TARGET_SHA) -> dict[str, str]:
    root = CONTROLLER.BACKEND_TAG_ROOT if role == "backend" else CONTROLLER.WEB_TAG_ROOT
    return {
        "tag": f"{root}:sha-{sha}",
        "digest_ref": f"{root}@{DIGEST_A if role == 'backend' else DIGEST_B}",
        "image_id": "sha256:" + ("c" if role == "backend" else "d") * 64,
        "revision": sha,
        "source": CONTROLLER.SOURCE_URL,
        "version": f"sha-{sha}",
    }


def git_permission_marker_fixture(
    repository: Path,
    marker_path: Path,
    *,
    phase: str = "hardened",
) -> dict[str, object]:
    records: list[dict[str, object]] = [
        {
            "path": ".",
            "type": "directory",
            "mode": "0755",
            "target_mode": "0700",
            "uid": os.geteuid(),
            "gid": os.getegid(),
            "device": 1,
            "inode": 1,
            "nlink": None,
            "size": None,
            "content_sha256": None,
            "mutable": False,
        },
        {
            "path": ".git",
            "type": "directory",
            "mode": "0700",
            "target_mode": "0700",
            "uid": os.geteuid(),
            "gid": os.getegid(),
            "device": 1,
            "inode": 2,
            "nlink": None,
            "size": None,
            "content_sha256": None,
            "mutable": False,
        },
        {
            "path": ".git/config",
            "type": "file",
            "mode": "0600",
            "target_mode": "0600",
            "uid": os.geteuid(),
            "gid": os.getegid(),
            "device": 1,
            "inode": 3,
            "nlink": 1,
            "size": 1,
            "content_sha256": "sha256:" + "1" * 64,
            "mutable": True,
        },
        {
            "path": ".git/objects",
            "type": "directory",
            "mode": "0700",
            "target_mode": "0700",
            "uid": os.geteuid(),
            "gid": os.getegid(),
            "device": 1,
            "inode": 4,
            "nlink": None,
            "size": None,
            "content_sha256": None,
            "mutable": False,
        },
    ]
    trust = CONTROLLER._git_source_trust
    marker: dict[str, object] = {
        "schema_version": trust.PERMISSION_SCHEMA_VERSION,
        "policy": trust.PERMISSION_POLICY_NAME,
        "repository": str(repository.absolute()),
        "marker_path": str(marker_path.absolute()),
        "phase": phase,
        "generation": 9 if phase == "hardened" else 1,
        "records": records,
        "inventory_sha256": trust.sha256_bytes(
            trust.canonical_json_bytes(records)
        ),
        "original_permissions_sha256": trust.sha256_bytes(
            trust.canonical_json_bytes(
                trust._permission_identity(records, hardened=False)
            )
        ),
        "hardened_permissions_sha256": trust.sha256_bytes(
            trust.canonical_json_bytes(
                trust._permission_identity(records, hardened=True)
            )
        ),
    }
    marker["evidence_sha256"] = trust._permission_document_digest(marker)
    return trust.validate_permission_takeover_evidence(
        marker,
        repository=repository,
        marker_path=marker_path,
        allowed_phases={phase},
    )


def adopted_git_permission_takeover_fixture(
    repository: Path,
    runtime: Path,
    *,
    source_sha: str = TARGET_SHA,
    source_tree: str = TARGET_TREE,
    production_source_sha: str = PREVIOUS_SHA,
    production_source_tree: str = PREVIOUS_TREE,
    adopted_deployment_sha256: str = "sha256:" + "2" * 64,
) -> dict[str, object]:
    marker_path = CONTROLLER._git_source_trust.permission_takeover_marker_path(
        runtime
    )
    marker = git_permission_marker_fixture(
        repository, marker_path, phase="hardened"
    )
    authority = {
        "authority_kind": CONTROLLER.ADOPTED_GIT_PERMISSION_AUTHORITY_KIND,
        "operation_id": "adopt-git-permission-fixture-001",
        "source_sha": source_sha,
        "source_tree": source_tree,
        "production_source_sha": production_source_sha,
        "production_source_tree": production_source_tree,
        "adopted_deployment_sha256": adopted_deployment_sha256,
        "bootstrap_control_sha256": "sha256:" + "3" * 64,
        "adopted_prerequisites_sha256": "sha256:" + "4" * 64,
        "plan_sha256": "sha256:" + "5" * 64,
        "permission_impact_sha256": "sha256:" + "6" * 64,
        "permission_marker_sha256": "sha256:" + "7" * 64,
        "permission_evidence_sha256": marker["evidence_sha256"],
        "permission_inventory_sha256": marker["inventory_sha256"],
        "original_permissions_sha256": marker[
            "original_permissions_sha256"
        ],
        "hardened_permissions_sha256": marker[
            "hardened_permissions_sha256"
        ],
        "completed_at": "2026-08-14T00:00:00Z",
    }
    combined: dict[str, object] = {
        "schema_version": 1,
        "authority_kind": CONTROLLER.ADOPTED_GIT_PERMISSION_AUTHORITY_KIND,
        "authority_file_sha256": "sha256:" + "8" * 64,
        "authority": authority,
        "hardened_marker": marker,
    }
    combined["identity_sha256"] = CONTROLLER.canonical_json_digest(combined)
    return CONTROLLER.validate_adopted_git_permission_takeover(combined)


def production_repository_transition_fixture(
    production_root: Path,
    *,
    predecessor_sha: str,
    production_sha: str,
    production_tree: str,
    target_sha: str,
    target_tree: str,
    baseline_trust_sha256: str,
) -> dict[str, object]:
    stable = {
        "schema_version": 1,
        "policy": "nexpoly-production-repository-stable-projection-v1",
        "repository_root": str(production_root),
        "git_dir": str(production_root / ".git"),
        "object_dir": str(production_root / ".git/objects"),
        "index_path": str(production_root / ".git/index"),
        "source": {
            "sha": production_sha,
            "tree": production_tree,
            "branch": "refs/heads/main",
            "origin": None,
        },
        "git_binary": "/usr/bin/git",
        "local_config": [],
        "head": {"kind": "symbolic", "target": "refs/heads/main"},
        "index": {"version": 2, "entries": 1},
        "forbidden_markers_absent": True,
        "execution_environment": {},
    }
    logical_refs = [
        {
            "name": "refs/heads/main",
            "object_sha": production_sha,
            "object_type": "commit",
            "symbolic_target": None,
        },
        {
            "name": CONTROLLER.DEPLOY_REMOTE_REF,
            "object_sha": predecessor_sha,
            "object_type": "commit",
            "symbolic_target": None,
        },
    ]
    raw_refs = [{"path": "refs", "kind": "directory", "mode": "0700"}]
    target_objects = [
        {"oid": target_sha, "type": "commit", "size": 123}
    ]
    transition = {
        "schema_version": 1,
        "policy": CONTROLLER.PRODUCTION_REPOSITORY_TRANSITION_POLICY,
        "source": {"sha": production_sha, "tree": production_tree},
        "target": {"sha": target_sha, "tree": target_tree},
        "baseline_evidence_sha256": baseline_trust_sha256,
        "stable_projection": stable,
        "stable_projection_sha256": CONTROLLER.canonical_json_digest(stable),
        "logical_refs": logical_refs,
        "logical_refs_sha256": CONTROLLER.canonical_json_digest(
            logical_refs
        ),
        "raw_ref_inventory": raw_refs,
        "raw_ref_inventory_sha256": CONTROLLER.canonical_json_digest(
            raw_refs
        ),
        "baseline_auxiliary_inventory": [],
        "baseline_auxiliary_inventory_sha256": (
            CONTROLLER.canonical_json_digest([])
        ),
        "baseline_semantic_object_count": 0,
        "baseline_semantic_objects_sha256": (
            CONTROLLER.canonical_json_digest([])
        ),
        "baseline_only_object_count": 0,
        "baseline_only_objects_sha256": (
            CONTROLLER.canonical_json_digest([])
        ),
        "target_reachable_object_count": 1,
        "target_reachable_objects_sha256": CONTROLLER.canonical_json_digest(
            target_objects
        ),
        "expected_materialized_object_count": 1,
        "expected_materialized_objects_sha256": (
            CONTROLLER.canonical_json_digest(target_objects)
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
    return CONTROLLER.validate_production_repository_transition(
        transition,
        production_root=production_root,
        production_sha=production_sha,
        production_tree=production_tree,
        target_sha=target_sha,
        target_tree=target_tree,
        baseline_trust_sha256=baseline_trust_sha256,
    )


def production_repository_materialization_fixture(
    *,
    phase: str,
    operation_id: str | None = None,
    transition: dict[str, object] | None = None,
) -> dict[str, object]:
    materialized = phase in {"materialized", "prepared"}
    if transition is None:
        target_objects = [
            {"oid": TARGET_SHA, "type": "commit", "size": 123}
        ]
        transition = {
            "baseline_semantic_object_count": 0,
            "baseline_semantic_objects_sha256": (
                CONTROLLER.canonical_json_digest([])
            ),
            "target_reachable_object_count": 1,
            "target_reachable_objects_sha256": (
                CONTROLLER.canonical_json_digest(target_objects)
            ),
            "expected_materialized_object_count": 1,
            "expected_materialized_objects_sha256": (
                CONTROLLER.canonical_json_digest(target_objects)
            ),
        }
    return CONTROLLER.validate_production_repository_materialization(
        {
            "schema_version": 1,
            "phase": phase,
            "full_trust_sha256": "sha256:" + "a" * 64,
            "object_inventory_sha256": "sha256:" + "b" * 64,
            "object_count": 10,
            "object_total_size": 4096,
            "semantic_object_count": transition[
                "expected_materialized_object_count"
                if materialized
                else "baseline_semantic_object_count"
            ],
            "semantic_object_inventory_sha256": (
                transition[
                    "expected_materialized_objects_sha256"
                    if materialized
                    else "baseline_semantic_objects_sha256"
                ]
            ),
            "logical_refs_sha256": "sha256:" + "c" * 64,
            "raw_ref_inventory_sha256": "sha256:" + "d" * 64,
            "auxiliary_inventory_sha256": "sha256:" + "1" * 64,
            "object_storage_inventory_sha256": "sha256:" + "2" * 64,
            "target_reachable_object_count": (
                transition["target_reachable_object_count"]
                if materialized
                else None
            ),
            "target_reachable_objects_sha256": (
                transition["target_reachable_objects_sha256"]
                if materialized
                else None
            ),
            "operation_id": operation_id if phase == "prepared" else None,
        }
    )


def adopted_git_permission_source_successor_fixture(
    git_permission_takeover: dict[str, object],
    *,
    target_sha: str = TARGET_SHA,
    target_tree: str = TARGET_TREE,
    repository_root: Path = REPOSITORY_ROOT,
    production_root: Path = REPOSITORY_ROOT,
    delivery_gate: dict[str, object] | None = None,
    files: list[dict[str, object]] | None = None,
    file_payloads: dict[str, bytes] | None = None,
    production_repository_transition: dict[str, object] | None = None,
) -> dict[str, object]:
    root = git_permission_takeover["authority"]
    if files is None:
        files = []
        for index, path in enumerate(
            CONTROLLER.ADOPTED_UNIT_PERMISSION_SUCCESSOR_V2_FILES,
            start=1,
        ):
            mode = (
                "100644"
                if path
                == "ops/config/mutable-data-audit.pg_service.conf.example"
                else "100755"
            )
            target = {
                "object_type": "blob",
                "mode": mode,
                "blob_sha": f"{index % 16:x}" * 40,
                "sha256": (
                    CONTROLLER.sha256_bytes(file_payloads[path])
                    if file_payloads is not None
                    else CONTROLLER.sha256_file(repository_root / path)
                ),
            }
            if (
                path
                in CONTROLLER.ADOPTED_GIT_PERMISSION_SOURCE_SUCCESSOR_ALLOWED_CHANGED_FILES
            ):
                predecessor = {
                    **target,
                    "blob_sha": f"{(index + 8) % 16:x}" * 40,
                    "sha256": "sha256:" + f"{(index + 8) % 16:x}" * 64,
                }
                relation = "changed"
            else:
                predecessor = dict(target)
                relation = "byte-identical"
            files.append(
                {
                    "path": path,
                    "relation": relation,
                    "predecessor": predecessor,
                    "target": target,
                }
            )
    if delivery_gate is None:
        delivery_gate = {
            "remote_main": target_sha,
            "ci": {
                "workflow_run_id": 42,
                "run_attempt": 1,
                "head_sha": target_sha,
                "head_branch": "main",
                "event": "push",
                "path": ".github/workflows/ci.yml",
                "conclusion": "success",
                "required_jobs": sorted(
                    CONTROLLER._bridge_core.REQUIRED_CI_JOBS
                ),
            },
        }
    changed = list(
        CONTROLLER.ADOPTED_GIT_PERMISSION_SOURCE_SUCCESSOR_ALLOWED_CHANGED_FILES
    )
    source_trust_sha256 = "sha256:" + "3" * 64
    if production_repository_transition is None:
        production_repository_transition = (
            production_repository_transition_fixture(
                production_root,
                predecessor_sha=root["source_sha"],
                production_sha=root["production_source_sha"],
                production_tree=root["production_source_tree"],
                target_sha=target_sha,
                target_tree=target_tree,
                baseline_trust_sha256=source_trust_sha256,
            )
        )
    binding: dict[str, object] = {
        "schema_version": 2,
        "authority_kind": (
            CONTROLLER.ADOPTED_GIT_PERMISSION_SOURCE_SUCCESSOR_AUTHORITY_KIND
        ),
        "operation_id": "adopt-git-successor-fixture-001",
        "predecessor_authority_sha256": git_permission_takeover[
            "authority_file_sha256"
        ],
        "predecessor_source_sha": root["source_sha"],
        "predecessor_source_tree": root["source_tree"],
        "predecessor_marker_sha256": root["permission_marker_sha256"],
        "target_source_sha": target_sha,
        "target_source_tree": target_tree,
        "production_source_sha": root["production_source_sha"],
        "production_source_tree": root["production_source_tree"],
        "adopted_deployment_sha256": root["adopted_deployment_sha256"],
        "bootstrap_control_sha256": root["bootstrap_control_sha256"],
        "adopted_prerequisites_sha256": root[
            "adopted_prerequisites_sha256"
        ],
        "snapshot_authority_sha256": "sha256:" + "5" * 64,
        "plan_sha256": "sha256:" + "1" * 64,
        "source_successor_impact_sha256": "sha256:" + "2" * 64,
        "source_trust_sha256": source_trust_sha256,
        "production_repository_transition": (
            production_repository_transition
        ),
        "production_repository_transition_sha256": (
            CONTROLLER.canonical_json_digest(
                production_repository_transition
            )
        ),
        "delivery_gate": delivery_gate,
        "delivery_gate_sha256": CONTROLLER.canonical_json_digest(
            delivery_gate
        ),
        "fixed_files": files,
        "fixed_files_sha256": CONTROLLER.canonical_json_digest(files),
        "changed_files": changed,
        "changed_files_sha256": CONTROLLER.canonical_json_digest(changed),
        "completed_at": "2026-08-14T00:00:00Z",
        "authority_file_sha256": "sha256:" + "4" * 64,
    }
    binding["identity_sha256"] = CONTROLLER.canonical_json_digest(binding)
    return CONTROLLER.validate_adopted_git_permission_source_successor_binding(
        binding
    )


def adopted_unit_permission_binding_fixture(
    git_permission_takeover: dict[str, object],
    *,
    source_successor_takeover: dict[str, object] | None = None,
    source_sha: str = TARGET_SHA,
    source_tree: str = TARGET_TREE,
    adopted_deployment_sha256: str = "sha256:" + "2" * 64,
    repository_root: Path = REPOSITORY_ROOT,
    file_digests: dict[str, str] | None = None,
) -> dict[str, object]:
    git_authority = git_permission_takeover["authority"]
    predecessor_sha = str(git_authority["source_sha"])
    predecessor_tree = str(git_authority["source_tree"])
    if source_successor_takeover is None:
        files = [
            {
                "path": path,
                "sha256": (
                    file_digests[path]
                    if file_digests is not None
                    else CONTROLLER.sha256_file(repository_root / path)
                ),
            }
            for path in CONTROLLER.ADOPTED_UNIT_PERMISSION_SUCCESSOR_FILES
        ]
        successor: dict[str, object] = {
            "schema_version": 1,
            "policy": CONTROLLER.ADOPTED_UNIT_PERMISSION_SUCCESSOR_POLICY,
            "mode": (
                "exact-source"
                if predecessor_sha == source_sha
                and predecessor_tree == source_tree
                else "ancestor-byte-identical"
            ),
            "authority": {
                "source_sha": predecessor_sha,
                "source_tree": predecessor_tree,
                "raw_sha256": git_permission_takeover[
                    "authority_file_sha256"
                ],
            },
            "target": {
                "source_sha": source_sha,
                "source_tree": source_tree,
            },
            "files": files,
            "files_sha256": CONTROLLER.canonical_json_digest(files),
        }
    else:
        files = source_successor_takeover["fixed_files"]
        successor = {
            "schema_version": 2,
            "policy": CONTROLLER.ADOPTED_UNIT_PERMISSION_SUCCESSOR_V2_POLICY,
            "mode": "protected-main-ci-exact-target",
            "root_authority": {
                "source_sha": predecessor_sha,
                "source_tree": predecessor_tree,
                "raw_sha256": git_permission_takeover[
                    "authority_file_sha256"
                ],
            },
            "source_successor_authority": source_successor_takeover,
            "target": {
                "source_sha": source_sha,
                "source_tree": source_tree,
            },
            "files": files,
            "files_sha256": CONTROLLER.canonical_json_digest(files),
        }
    successor["identity_sha256"] = CONTROLLER.canonical_json_digest(
        successor
    )
    binding: dict[str, object] = {
        "authority_kind": (
            CONTROLLER.ADOPTED_UNIT_PERMISSION_AUTHORITY_KIND
        ),
        "operation_id": "adopt-unit-permission-fixture-001",
        "source_sha": source_sha,
        "source_tree": source_tree,
        "production_source_sha": git_authority["production_source_sha"],
        "production_source_tree": git_authority[
            "production_source_tree"
        ],
        "adopted_deployment_sha256": adopted_deployment_sha256,
        "bootstrap_control_sha256": git_authority[
            "bootstrap_control_sha256"
        ],
        "adopted_prerequisites_sha256": git_authority[
            "adopted_prerequisites_sha256"
        ],
        "adopted_git_permissions_sha256": git_permission_takeover[
            "authority_file_sha256"
        ],
        "adopted_git_permission_source_sha": predecessor_sha,
        "adopted_git_permission_source_tree": predecessor_tree,
        "plan_sha256": "sha256:" + "9" * 64,
        "unit_permission_impact_sha256": "sha256:" + "a" * 64,
        "original_units_sha256": "sha256:" + "b" * 64,
        "hardened_units_sha256": "sha256:" + "c" * 64,
        "backup_sha256": "sha256:" + "d" * 64,
        "backup_content_sha256": "sha256:" + "f" * 64,
        "completed_at": "2026-08-14T00:00:00Z",
        "authority_file_sha256": "sha256:" + "e" * 64,
        "git_permission_successor": successor,
    }
    if source_successor_takeover is not None:
        binding.update(
            {
                "schema_version": 2,
                "adopted_git_permission_source_successor_sha256": (
                    source_successor_takeover["authority_file_sha256"]
                ),
            }
        )
    binding["identity_sha256"] = CONTROLLER.canonical_json_digest(binding)
    return CONTROLLER.validate_adopted_unit_permission_binding(binding)


def mutable_data_evidence(
    *,
    ledger_length: int = 11,
    operation_id: str = OPERATION_ID,
) -> dict[str, object]:
    def table_record(
        schema: str,
        table: str,
        index: int,
        *,
        present: bool = True,
        rows: int | None = None,
    ) -> dict[str, object]:
        return {
            "schema": schema,
            "table": table,
            "state": "present" if present else "absent",
            "row_count": (index + 1 if rows is None else rows) if present else None,
            "schema_sha256": (
                "sha256:" + f"{(index + 1) % 16:x}" * 64
                if present
                else None
            ),
            "content_sha256": (
                "sha256:" + f"{(index + 9) % 16:x}" * 64
                if present
                else None
            ),
        }

    dft_ready = ledger_length >= 13
    md_queue_ready = ledger_length >= 14
    property_filter_ready = ledger_length >= 15
    contract_applied = ledger_length >= 12
    controls_ready = ledger_length >= 10
    business_tables = [
        table_record(schema, table, index)
        for index, (schema, table) in enumerate(
            CONTROLLER._site_helper_contracts.BUSINESS_MUTABLE_TABLES
        )
    ]
    business_tables.extend(
        table_record(
            schema,
            table,
            index + len(business_tables),
            present=dft_ready,
            rows=0,
        )
        for index, (schema, table) in enumerate(
            CONTROLLER._site_helper_contracts.POST_0013_BUSINESS_MUTABLE_TABLES
        )
    )
    if dft_ready:
        for record in business_tables:
            relation = (record["schema"], record["table"])
            expected_schema = (
                CONTROLLER._site_helper_contracts
                .MONOMER_DFT_TABLE_SCHEMA_SHA256.get(relation)
            )
            if expected_schema is not None:
                record["schema_sha256"] = expected_schema
                record["content_sha256"] = (
                    CONTROLLER._site_helper_contracts
                    .EMPTY_POSTGRES_COPY_SHA256
                )
    if md_queue_ready:
        md_jobs = next(
            record
            for record in business_tables
            if (record["schema"], record["table"])
            == ("md", "monomer_md_jobs")
        )
        md_jobs["schema_sha256"] = "sha256:" + "e" * 64
    static_tables = []
    for index, (schema, table) in enumerate(
        CONTROLLER._site_helper_contracts.STATIC_IMPORT_TABLES
    ):
        is_property_filter_snapshot = (
            schema,
            table,
        ) == ("governance", "property_filter_options_snapshots")
        static_tables.append(
            table_record(
                schema,
                table,
                index + 8,
                present=(
                    property_filter_ready
                    if is_property_filter_snapshot
                    else True
                ),
                rows=(1 if is_property_filter_snapshot else None),
            )
        )
    deployment_table = table_record(
        "governance",
        "deployment_control",
        7,
        present=controls_ready,
        rows=1,
    )
    analytics_table = table_record(
        "governance",
        "database_analytics_snapshots",
        8,
        present=controls_ready,
        rows=0,
    )
    sequences: list[dict[str, object]] = []
    for index, ((schema, sequence, _owned_by), owner) in enumerate(
        zip(
            CONTROLLER._site_helper_contracts.DATA_SEQUENCES,
            CONTROLLER._site_helper_contracts.DATA_SEQUENCE_OWNERSHIP,
            strict=True,
        )
    ):
        is_dft_sequence = schema == "monomer_dft"
        is_md_queue_sequence = (
            schema == "md"
            and sequence == "monomer_md_queue_sequence_seq"
        )
        present = (
            not is_dft_sequence
            and not is_md_queue_sequence
            or is_dft_sequence
            and dft_ready
            or is_md_queue_sequence
            and md_queue_ready
        )
        sequences.append(
            {
                "schema": schema,
                "sequence": sequence,
                "ownership": (
                    {
                        "schema": owner[0],
                        "table": owner[1],
                        "column": owner[2],
                        "ordinal": owner[3],
                        "deptype": owner[4],
                    }
                    if present
                    else None
                ),
                "state": "present" if present else "absent",
                "data_type": "bigint" if present else None,
                "start_value": 1 if present else None,
                "min_value": 1 if present else None,
                "max_value": 9223372036854775807 if present else None,
                "increment_by": 1 if present else None,
                "cache_size": 1 if present else None,
                "cycle": False if present else None,
                "last_value": 1 if present else None,
                "is_called": (
                    False
                    if present
                    and (is_dft_sequence or is_md_queue_sequence)
                    else (True if present else None)
                ),
            }
        )
    identity = {
        "operation_id": operation_id,
        "database": "nexpoly",
        "database_system_identifier": "7659245354718314530",
        "connection": {
            "service": CONTROLLER.MUTABLE_DATA_SERVICE,
            "host": CONTROLLER.MUTABLE_DATA_HOST,
            "port": CONTROLLER.MUTABLE_DATA_PORT,
            "database": CONTROLLER.MUTABLE_DATA_DATABASE,
            "user": CONTROLLER.MUTABLE_DATA_USER,
        },
        "postgres_runtime": {
            "container_id": "a" * 64,
            "image_id": "sha256:" + "b" * 64,
            "configured_image": "postgres:16-alpine",
            "data_volume": {
                "type": "volume",
                "name": "nexpoly_postgres_data",
                "source": (
                    "/var/lib/docker/volumes/nexpoly_postgres_data/_data"
                ),
                "destination": "/var/lib/postgresql/data",
                "driver": "local",
                "read_write": True,
            },
            "host_endpoint": {
                "host": CONTROLLER.MUTABLE_DATA_HOST,
                "port": CONTROLLER.MUTABLE_DATA_PORT,
                "container_port": 5432,
                "protocol": "tcp",
            },
            "system_identifier": "7659245354718314530",
        },
        "role_security": role_security_evidence(
            CONTROLLER._site_helper_contracts
        ),
        "digest_algorithm": "sha256-postgres-jsonb-copy-v4",
        "migration_ledger": [
            {"version": version, "checksum": checksum}
            for version, checksum in (
                CONTROLLER._site_helper_contracts.CANONICAL_MIGRATION_LEDGER[
                    :ledger_length
                ]
            )
        ],
        "business_tables": business_tables,
        "governed_controls": {
            "deployment_control": {
                "table": deployment_table,
                "row": (
                    {
                        "control_key": "production",
                        "drain_enabled": True,
                        "reason": f"pull deployment {operation_id}",
                        "release_sha": TARGET_SHA,
                        "activated_at": "2026-07-17T00:00:00Z",
                        "activated_by": "pull-deploy-controller",
                        "updated_at": "2026-07-17T00:00:00Z",
                    }
                    if controls_ready
                    else None
                ),
            },
            "database_analytics_snapshots": {
                "table": analytics_table,
                "entries": [],
            },
        },
        "static_tables": static_tables,
        "migration_exception": table_record(
            "generation",
            "polytao_jobs",
            23,
            present=not contract_applied,
            rows=9,
        ),
        "migration_exception_archive_evidence": (
            None
            if contract_applied
            else {
                "schema_version": 2,
                "row_count": 9,
                "status_counts": {"completed": 9},
                "rows_sha256": "1" * 64,
                "schema_sha256": "2" * 64,
                "structure_counts": {
                    "columns": 1,
                    "indexes": 1,
                    "constraints": 1,
                    "triggers": 0,
                },
            }
        ),
        "sequences": sequences,
        "bridge_projection": {
            "schema": "md",
            "table": "monomer_md_jobs",
            "projection": "pre-0009-row-json-v1",
            "state": "present",
            "row_count": next(
                record["row_count"]
                for record in business_tables
                if record["schema"] == "md"
                and record["table"] == "monomer_md_jobs"
            ),
            "content_sha256": "sha256:" + "f" * 64,
            "lease_columns": {
                "state": "present" if ledger_length >= 9 else "absent",
                "non_null_counts": {
                    "worker_instance_id": (
                        0 if ledger_length >= 9 else None
                    ),
                    "heartbeat_at": 0 if ledger_length >= 9 else None,
                    "lease_expires_at": (
                        0 if ledger_length >= 9 else None
                    ),
                },
            },
        },
    }
    return {
        "schema_version": 7,
        **identity,
        "transaction_isolation": "repeatable read",
        "transaction_read_only": True,
        "transaction_deferrable": True,
        "snapshot_sha256": CONTROLLER.canonical_json_digest(identity),
        "captured_at": "2026-07-17T00:00:00Z",
    }


def reseal_mutable_data_evidence(
    document: dict[str, object],
) -> dict[str, object]:
    identity_fields = (
        "operation_id",
        "database",
        "database_system_identifier",
        "connection",
        "postgres_runtime",
        "role_security",
        "digest_algorithm",
        "migration_ledger",
        "business_tables",
        "governed_controls",
        "static_tables",
        "migration_exception",
        "migration_exception_archive_evidence",
        "sequences",
        "bridge_projection",
    )
    document["snapshot_sha256"] = CONTROLLER.canonical_json_digest(
        {name: document[name] for name in identity_fields}
    )
    return document


def _rewrite_v3_media_ledger(
    snapshot: dict[str, object],
    *,
    database: str,
    ledger: list[dict[str, str]],
    legacy_relation_present: bool,
) -> None:
    record = next(
        value
        for value in snapshot["media"]
        if value.get("database") == database
        and value.get("record_type") == "nexpoly-db"
        and value.get("disposition") != "retained-private-isolated"
    )
    analysis, migration_0013, requires_0014 = (
        CONTROLLER._site_helper_contracts._external_media_ledger_v2(
            ledger,
            legacy_relation_present=legacy_relation_present,
            isolated=False,
        )
    )
    ledger_digest = CONTROLLER.canonical_json_digest(ledger)
    ledger_relation = record["ledger_relation"]
    ledger_relation.update(
        {
            "state": "present",
            "row_count": len(ledger),
            "content_sha256": ledger_digest,
        }
    )
    legacy_relation = record["legacy_relation"]
    if legacy_relation_present:
        if legacy_relation.get("schema_authority") is None:
            raise AssertionError(
                "fixture cannot recreate a dropped legacy schema authority"
            )
        legacy_relation.update(
            {
                "state": "present",
                "row_count": 9,
            }
        )
    else:
        legacy_relation.update(
            {
                "state": "absent",
                "row_count": None,
                "schema_sha256": None,
                "schema_authority": None,
                "content_sha256": None,
            }
        )
    generation_schema = json.loads(
        json.dumps(record["generation_schema"])
    )
    if not legacy_relation_present:
        generation_schema = {
            "state": "absent",
            "schema_sha256": None,
            "schema_authority": None,
        }
    database_record = next(
        value
        for value in record["databases"]
        if value["name"] == database
    )
    previous_database_audit = database_record["audit"]
    primary = {
        "database_identity": record["database_identity"],
        "database_identity_sha256": record[
            "database_identity_sha256"
        ],
        "current_user": record["current_user"],
        "transaction_read_only": record["transaction_read_only"],
        "server_startup": json.loads(
            json.dumps(record["server_startup"])
        ),
        "role_superuser": record["role_superuser"],
        "role_create_db": record["role_create_db"],
        "role_create_role": record["role_create_role"],
        "role_replication": record["role_replication"],
        "role_bypass_rls": record["role_bypass_rls"],
        "role_inherit": record["role_inherit"],
        "role_can_login": record["role_can_login"],
        **external_role_security_fields(
            database,
            superuser=False,
            ledger_present=True,
            legacy_present=legacy_relation_present,
        ),
        "ledger": ledger,
        "ledger_sha256": ledger_digest,
        "ledger_relation": ledger_relation,
        "ledger_analysis": analysis,
        "legacy_relation_present": legacy_relation_present,
        "generation_schema": generation_schema,
        "legacy_relation": legacy_relation,
        "migration_0013": migration_0013,
    }
    record.update(primary)
    database_record["audit"] = {
        **json.loads(json.dumps(primary)),
        "role_contract_marker": previous_database_audit[
            "role_contract_marker"
        ],
        "role_contract_sha256": previous_database_audit[
            "role_contract_sha256"
        ],
        "requires_0014": requires_0014,
    }
    record["source_content_sha256"] = (
        CONTROLLER.canonical_json_digest(
            {
                "database_inventory": record["database_inventory"],
                "databases": record["databases"],
            }
        )
    )
    for projection in snapshot["databases"]:
        if projection["database"] == database:
            projection.update(
                {
                    "ledger": ledger,
                    "ledger_sha256": ledger_digest,
                    "legacy_relation_present": legacy_relation_present,
                }
            )
    snapshot["requires_0014"] = any(
        value["audit"].get("requires_0014", False)
        for medium in snapshot["media"]
        if medium.get("record_type") == "nexpoly-db"
        for value in medium["databases"]
    )


def _reseal_v3_media_runtime(
    snapshot: dict[str, object],
    *,
    captured_at: str,
) -> None:
    snapshot["media_registry"]["captured_at"] = captured_at
    for record in snapshot["media"]:
        audit = record["audit"]
        audit["audited_at"] = captured_at
        audit["postgres_uid"] = 70
        audit["postgres_gid"] = 70
        audit.pop("evidence_sha256", None)
        audit["evidence_sha256"] = CONTROLLER.canonical_json_digest(
            record
        )


def external_database_audit_binding(
    runtime: Path,
    *,
    captured_at: str = "2026-07-17T00:00:00Z",
) -> dict[str, object]:
    helper_path = (
        runtime / "bin" / CONTROLLER.EXTERNAL_DATABASE_AUDIT_HELPER
    )
    authority_rules_path = (
        runtime
        / "config"
        / CONTROLLER.EXTERNAL_DATABASE_MEDIA_AUTHORITY_RULES
    )
    registry_path = (
        runtime
        / "config"
        / CONTROLLER.EXTERNAL_DATABASE_MEDIA_REGISTRY
    )
    helper_sha256 = (
        CONTROLLER.sha256_file(helper_path)
        if helper_path.exists()
        else "sha256:" + "8" * 64
    )
    authority_rules_sha256 = (
        CONTROLLER.sha256_file(authority_rules_path)
        if authority_rules_path.exists()
        else "sha256:" + "4" * 64
    )
    registry_sha256 = (
        CONTROLLER.sha256_file(registry_path)
        if registry_path.exists()
        else "sha256:" + "5" * 64
    )
    ledger = [
        {"version": version, "checksum": checksum}
        for version, checksum in (
            CONTROLLER._site_helper_contracts.CANONICAL_MIGRATION_LEDGER
        )
    ]
    through_0011 = [
        row
        for row in ledger
        if row["version"] <= "0011_monomer_md_demo_steps"
    ]
    through_0012 = [
        row
        for row in ledger
        if row["version"] <= "0012_drop_polytao_jobs"
    ]
    through_0008 = [
        row
        for row in ledger
        if row["version"] <= "0008_polytao_backend_runtime"
    ]
    snapshot = external_inventory_v3_fixture(
        dev_ledger=through_0012,
        # The reusable builder uses this ledger for both production and
        # health; rewrite health below while retaining production at 0011.
        health_ledger=through_0011,
        registry_digest=registry_sha256,
        dev_user="nexpoly_dev_auditor",
        health_user="nexpoly_health_auditor",
    )
    snapshot["media_registry"].pop("sha256", None)
    snapshot["media_registry"]["media_authority_rules_sha256"] = (
        authority_rules_sha256
    )
    snapshot["media_registry"]["runtime_registry_sha256"] = (
        registry_sha256
    )
    _rewrite_v3_media_ledger(
        snapshot,
        database="nexpoly_md_health_opt",
        ledger=through_0008,
        legacy_relation_present=True,
    )
    _reseal_v3_media_runtime(snapshot, captured_at=captured_at)
    active, control_manifest, control_root = (
        CONTROLLER._control_runtime.load_active_control(runtime)
    )
    role_sql_path = (
        control_root / CONTROLLER.EXTERNAL_DATABASE_AUDIT_ROLE_SQL
    )
    role_sql_sha256 = CONTROLLER.sha256_file(role_sql_path)
    helper_control = {
        "release_id": active["release_id"],
        "source_sha": control_manifest["source_sha"],
        "source_tree": control_manifest["source_tree"],
        "manifest_sha256": CONTROLLER.sha256_file(
            control_root
            / CONTROLLER._control_runtime.CONTROL_MANIFEST_NAME
        ),
        "launcher_sha256": CONTROLLER.sha256_file(
            control_root / "postgres_media_launcher.py"
        ),
        "implementation_sha256": CONTROLLER.sha256_file(
            control_root / "postgres_media_evidence.py"
        ),
        "authority_rules_sha256": authority_rules_sha256,
        "role_sql_sha256": role_sql_sha256,
    }
    binding: dict[str, object] = {
        "schema_version": 2,
        "helper": {
            "path": str(helper_path),
            "sha256": helper_sha256,
            "mode": "0700",
        },
        "helper_control": helper_control,
        "authority_rules": {
            "path": str(authority_rules_path),
            "sha256": authority_rules_sha256,
            "mode": "0600",
        },
        "role_sql": {
            "path": str(role_sql_path),
            "sha256": role_sql_sha256,
            "mode": "0700",
            "control_release_id": active["release_id"],
            "source_sha": control_manifest["source_sha"],
            "source_tree": control_manifest["source_tree"],
        },
        "role_provisioning": (
            CONTROLLER.external_database_role_provisioning(
                snapshot,
                role_sql_sha256=role_sql_sha256,
            )
        ),
        "registry": {
            "path": str(registry_path),
            "sha256": registry_sha256,
            "mode": "0600",
            "authority_rules_sha256": authority_rules_sha256,
        },
        "expected_users": {
            "nexpoly_dev": "nexpoly_dev_auditor",
            "nexpoly_md_health_opt": "nexpoly_health_auditor",
        },
        "snapshot": snapshot,
        "snapshot_sha256": CONTROLLER.canonical_json_digest(snapshot),
        "state_sha256": CONTROLLER.canonical_json_digest(
            CONTROLLER.external_database_audit_state(snapshot)
        ),
        "identity_sha256": None,
    }
    binding["identity_sha256"] = CONTROLLER.canonical_json_digest(
        {
            key: value
            for key, value in binding.items()
            if key != "identity_sha256"
        }
    )
    return binding


def reseal_external_database_audit_binding(
    binding: dict[str, object],
) -> dict[str, object]:
    snapshot = binding["snapshot"]
    binding["role_provisioning"] = (
        CONTROLLER.external_database_role_provisioning(
            snapshot,
            role_sql_sha256=binding["role_sql"]["sha256"],
        )
    )
    binding["snapshot_sha256"] = CONTROLLER.canonical_json_digest(snapshot)
    binding["state_sha256"] = CONTROLLER.canonical_json_digest(
        CONTROLLER.external_database_audit_state(snapshot)
    )
    binding["identity_sha256"] = CONTROLLER.canonical_json_digest(
        {
            key: value
            for key, value in binding.items()
            if key != "identity_sha256"
        }
    )
    return binding


def external_database_binding_state(
    binding: dict[str, object],
    *,
    production_ledger: list[dict[str, str]],
    legacy_relation_present: bool,
    captured_at: str,
) -> dict[str, object]:
    changed = json.loads(json.dumps(binding))
    snapshot = changed["snapshot"]
    _rewrite_v3_media_ledger(
        snapshot,
        database="nexpoly",
        ledger=production_ledger,
        legacy_relation_present=legacy_relation_present,
    )
    _reseal_v3_media_runtime(snapshot, captured_at=captured_at)
    return reseal_external_database_audit_binding(changed)


def seed_completed_alias_gate(
    runtime: Path, manifest: dict[str, object], control_root: Path
) -> None:
    selector = CONTROLLER._control_runtime
    operation_id = "alias-0005-fixture"
    audit_dir = runtime / selector.ALIAS_AUDIT_ROOT_RELATIVE / operation_id
    backup_dir = runtime / selector.ALIAS_BACKUP_ROOT_RELATIVE / operation_id
    for directory in (audit_dir, backup_dir):
        directory.mkdir(parents=True, mode=0o700)
        os.chmod(directory, 0o700)
    dump = backup_dir / "nexpoly-before.dump"
    write_private(dump, "fixture database dump\n")
    dump_sha = selector.sha256_file(dump).removeprefix("sha256:")
    write_private(backup_dir / "nexpoly-before.dump.sha256", dump_sha + "\n")
    restore_list = audit_dir / "pg-restore.list"
    write_private(
        restore_list,
        "TABLE DATA generation polytao_jobs\n"
        "TABLE DATA governance schema_migrations\n",
    )
    def ledger_rows(pairs: list[tuple[str, str]]) -> list[dict[str, str]]:
        return [
            {
                "version": version,
                "checksum": checksum,
                "applied_at": (
                    selector.ALIAS_APPLIED_AT
                    if version == selector.ALIAS_VERSION
                    else f"2026-07-08T02:{index:02d}:00.000000Z"
                ),
            }
            for index, (version, checksum) in enumerate(pairs)
        ]

    archive = {
        "row_count": 12,
        "status_counts": {"completed": 8, "failed": 4},
        "rows_sha256": "c" * 64,
        "schema_sha256": selector.ALIAS_EXPECTED_SCHEMA_SHA256,
        "structure_counts": selector.ALIAS_EXPECTED_STRUCTURE_COUNTS,
    }
    relation = {
        "kind": "r",
        "persistence": "p",
        "is_partition": False,
        "row_security": False,
        "force_row_security": False,
        "owner": "polyprop",
        "parents": 0,
        "children": 0,
    }
    before = {
        "database": "nexpoly",
        "current_user": "polyprop",
        "database_owner": "polyprop",
        "server_version_num": 160014,
        "in_recovery": False,
        "system_identifier": selector.ALIAS_SYSTEM_IDENTIFIER,
        "ledger": ledger_rows(selector.ALIAS_PRE_LEDGER),
        "archive": archive,
        "ledger_schema_sha256": selector.ALIAS_EXPECTED_LEDGER_SCHEMA_SHA256,
        "ledger_structure_counts": selector.ALIAS_EXPECTED_LEDGER_STRUCTURE_COUNTS,
        "polytao_relation": relation,
        "ledger_relation": relation,
    }
    after = {
        **before,
        "ledger": [
            row
            for row in before["ledger"]
            if row["version"] != selector.ALIAS_VERSION
        ],
    }
    restored = {
        **before,
        "database": "nexpoly_alias_restore",
        "current_user": "postgres",
        "database_owner": "postgres",
        "system_identifier": "123456789",
        "polytao_relation": {**relation, "owner": "postgres"},
        "ledger_relation": {**relation, "owner": "postgres"},
    }
    entrypoint = manifest["entrypoints"]["reconcile-production-0005-alias"]
    control = {
        "release_id": manifest["release_id"],
        "source_sha": manifest["source_sha"],
        "source_tree": manifest["source_tree"],
        "manifest_sha256": selector.sha256_file(
            control_root / selector.CONTROL_MANIFEST_NAME
        ).removeprefix("sha256:"),
        "script_sha256": selector.sha256_file(
            control_root / entrypoint["file"]
        ).removeprefix("sha256:"),
    }
    identity = {
        "operation_id": operation_id,
        "control": control,
        "legacy_source": {"sha": PREVIOUS_SHA, "tree": PREVIOUS_TREE},
        "binaries_sha256": {"/fixture/bin": "b" * 64},
        "database_endpoint": selector.ALIAS_DATABASE_ENDPOINT,
        "database_system_identifier": selector.ALIAS_SYSTEM_IDENTIFIER,
        "restore_image": {
            "digest_ref": selector.ALIAS_RESTORE_IMAGE,
            "image_id": "sha256:" + "d" * 64,
        },
        "alias": {
            "version": selector.ALIAS_VERSION,
            "checksum": selector.ALIAS_CHECKSUM,
            "applied_at": selector.ALIAS_APPLIED_AT,
        },
    }
    backup = {
        "dump_path": str(dump),
        "dump_sha256": dump_sha,
        "dump_size": dump.stat().st_size,
        "restore_list_sha256": selector.sha256_file(restore_list).removeprefix(
            "sha256:"
        ),
    }
    restore = {
        "image": {
            "digest_ref": selector.ALIAS_RESTORE_IMAGE,
            "image_id": "sha256:" + "d" * 64,
        },
        "container_name": "nexpoly-alias-restore-fixture",
        "network_mode": "none",
        "dump_sha256": dump_sha,
        "archive": before["archive"],
        "ledger_schema_sha256": before["ledger_schema_sha256"],
        "database_inventory": restored,
        "verified_at": "2026-07-17T00:00:00Z",
    }
    CONTROLLER.atomic_json(audit_dir / "isolated-postgres16-restore.json", restore)
    CONTROLLER.atomic_json(audit_dir / "database-after.json", after)
    external_transition_path = (
        audit_dir / "external-database-alias-transition.json"
    )
    CONTROLLER.atomic_json(
        external_transition_path,
        {"schema_version": 1, "fixture": True},
    )
    external_transition = {
        "path": str(external_transition_path),
        "sha256": selector.sha256_file(external_transition_path),
        "identity_sha256": "sha256:" + "1" * 64,
        "before_state_sha256": "sha256:" + "2" * 64,
        "after_state_sha256": "sha256:" + "3" * 64,
        "descriptor_sha256": "sha256:" + "4" * 64,
        "operation_id": operation_id,
        "kind": "alias-0005-reconciliation",
    }
    files = selector._alias_evidence_files(audit_dir, backup_dir)
    completed_at = "2026-07-17T00:00:01Z"
    runtime_stop_fence = {"fixture": True}
    audit = {
        "schema_version": 1,
        "operation_id": operation_id,
        "outcome": "completed",
        "identity": identity,
        "database_before": before,
        "database_after": after,
        "database_backup": backup,
        "isolated_restore": restore,
        "runtime_stop_fence": runtime_stop_fence,
        "runtime_stop_fence_sha256": selector.canonical_json_digest(
            runtime_stop_fence
        ).removeprefix("sha256:"),
        "external_database_alias_transition": external_transition,
        "binaries": {"/fixture/bin": {"sha256": "b" * 64}},
        "files": files,
        "completed_at": completed_at,
    }
    audit_path = audit_dir / "AUDIT-MANIFEST.json"
    CONTROLLER.atomic_json(audit_path, audit)
    marker = {
        "schema_version": 1,
        "action": selector.ALIAS_ACTION,
        "phase": "completed",
        "identity": identity,
        "operation_directories": {
            "audit": str(audit_dir),
            "backup": str(backup_dir),
        },
        "started_at": "2026-07-17T00:00:00Z",
        "updated_at": completed_at,
        "runtime_stop_fence": runtime_stop_fence,
        "before": before,
        "database_backup": backup,
        "restore_container": {"name": "fixture"},
        "isolated_restore": restore,
        "mutation_intent": {
            "database_system_identifier": selector.ALIAS_SYSTEM_IDENTIFIER,
            "alias": identity["alias"],
            "pre_ledger": before["ledger"],
            "archive": before["archive"],
            "dump_sha256": dump_sha,
            "restore_dump_sha256": dump_sha,
        },
        "after": after,
        "external_database_alias_transition": external_transition,
        "audit_manifest_sha256": selector.sha256_file(audit_path).removeprefix(
            "sha256:"
        ),
        "completed_at": completed_at,
    }
    marker_path = runtime / selector.ALIAS_MARKER_RELATIVE
    marker_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(marker_path.parent, 0o700)
    CONTROLLER.atomic_json(marker_path, marker)


class ControllerSiblingValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def make_directory(path: Path, mode: int) -> None:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)

    @staticmethod
    def make_file(path: Path, mode: int) -> None:
        path.write_text("# test control payload\n", encoding="utf-8")
        os.chmod(path, mode)

    def validate(
        self,
        controller: Path,
        *,
        environment: dict[str, str] | None = None,
    ) -> Path:
        return CONTROLLER._validate_executable_sibling(
            controller,
            "worker_slot_runtime.py",
            runtime_root=self.runtime,
            environment=environment or {},
        )

    def test_source_checkout_accepts_mixed_git_materialization_modes(
        self,
    ) -> None:
        scripts = self.root / "checkout" / "scripts"
        self.make_directory(scripts, 0o755)
        controller = scripts / "pull_deploy_controller.py"
        sibling = scripts / "worker_slot_runtime.py"
        self.make_file(controller, 0o700)
        self.make_file(sibling, 0o755)

        self.assertEqual(self.validate(controller), sibling)

    def test_private_source_checkout_keeps_executable_cohort_private(
        self,
    ) -> None:
        scripts = self.root / "private-checkout" / "scripts"
        self.make_directory(scripts, 0o700)
        controller = scripts / "pull_deploy_controller.py"
        sibling = scripts / "worker_slot_runtime.py"
        self.make_file(controller, 0o700)
        self.make_file(sibling, 0o755)

        with self.assertRaisesRegex(RuntimeError, "sibling is unsafe"):
            self.validate(controller)
        os.chmod(sibling, 0o700)
        self.assertEqual(self.validate(controller), sibling)

    def test_source_checkout_rejects_writable_or_linked_sibling(self) -> None:
        scripts = self.root / "unsafe-checkout" / "scripts"
        self.make_directory(scripts, 0o755)
        controller = scripts / "pull_deploy_controller.py"
        sibling = scripts / "worker_slot_runtime.py"
        self.make_file(controller, 0o755)
        self.make_file(sibling, 0o775)

        with self.assertRaisesRegex(RuntimeError, "sibling is unsafe"):
            self.validate(controller)

        sibling.unlink()
        target = self.root / "external-worker-slot.py"
        self.make_file(target, 0o755)
        sibling.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "sibling is unsafe"):
            self.validate(controller)

    def test_stable_install_requires_exact_private_modes(self) -> None:
        binary_directory = self.runtime / "bin"
        self.make_directory(self.runtime, 0o700)
        self.make_directory(binary_directory, 0o700)
        controller = binary_directory / "pull_deploy_controller.py"
        sibling = binary_directory / "worker_slot_runtime.py"
        self.make_file(controller, 0o700)
        self.make_file(sibling, 0o700)

        self.assertEqual(self.validate(controller), sibling)
        for path in (self.runtime, binary_directory, controller, sibling):
            with self.subTest(path=path):
                os.chmod(path, 0o755)
                with self.assertRaisesRegex(RuntimeError, "sibling is unsafe"):
                    self.validate(controller)
                os.chmod(path, 0o700)

    def test_control_release_requires_selector_binding_and_private_modes(
        self,
    ) -> None:
        release_id = "a" * 64
        releases = self.runtime / "control-releases"
        release = releases / release_id
        self.make_directory(self.runtime, 0o700)
        self.make_directory(releases, 0o700)
        self.make_directory(release, 0o700)
        controller = release / "pull_deploy_controller.py"
        sibling = release / "worker_slot_runtime.py"
        self.make_file(controller, 0o700)
        self.make_file(sibling, 0o700)
        environment = {
            "NEXPOLY_ACTIVE_CONTROL_ROOT": str(release),
            "NEXPOLY_ACTIVE_CONTROL_RELEASE_ID": release_id,
        }

        self.assertEqual(
            self.validate(controller, environment=environment),
            sibling,
        )
        for invalid_environment in (
            {},
            {
                **environment,
                "NEXPOLY_ACTIVE_CONTROL_ROOT": str(releases / ("b" * 64)),
            },
            {
                **environment,
                "NEXPOLY_ACTIVE_CONTROL_RELEASE_ID": "b" * 64,
            },
        ):
            with self.subTest(environment=invalid_environment):
                with self.assertRaisesRegex(RuntimeError, "selector binding"):
                    self.validate(
                        controller,
                        environment=invalid_environment,
                    )

        for path in (self.runtime, releases, release, controller, sibling):
            with self.subTest(path=path):
                os.chmod(path, 0o755)
                with self.assertRaisesRegex(RuntimeError, "sibling is unsafe"):
                    self.validate(controller, environment=environment)
                os.chmod(path, 0o700)

    def test_unrecognized_runtime_locations_do_not_fall_back_to_source(
        self,
    ) -> None:
        self.make_directory(self.runtime, 0o700)
        locations = (
            self.runtime / "control-releases" / "not-a-release",
            self.runtime / "checkout" / "scripts",
        )
        for location in locations:
            with self.subTest(location=location):
                self.make_directory(location, 0o700)
                controller = location / "pull_deploy_controller.py"
                sibling = location / "worker_slot_runtime.py"
                self.make_file(controller, 0o700)
                self.make_file(sibling, 0o700)
                with self.assertRaisesRegex(RuntimeError, "runtime location"):
                    self.validate(controller)


class AdoptedPrerequisitePrivateSourceTests(unittest.TestCase):
    def test_direct_plan_uses_private_complete_target_clone_for_relation(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="nexpoly-prerequisite-source-"
        ) as temporary:
            root = Path(temporary)
            os.chmod(root, 0o700)
            source = root / "source"
            source.mkdir(mode=0o700)

            def git(*arguments: str) -> str:
                result = subprocess.run(
                    ["/usr/bin/git", *arguments],
                    cwd=source,
                    env={
                        "PATH": "/usr/bin:/bin",
                        "HOME": str(root),
                        "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_CONFIG_GLOBAL": os.devnull,
                    },
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                return result.stdout.strip()

            git("init", "--initial-branch=main")
            git("config", "user.name", "Prerequisite Test")
            git("config", "user.email", "prerequisite@example.invalid")
            git("remote", "add", "origin", CONTROLLER.REPOSITORY_SSH_URL)
            controller_path = source / "scripts/pull_deploy_controller.py"
            controller_path.parent.mkdir(parents=True, mode=0o700)
            controller_path.write_text(
                "# exact target controller fixture\n", encoding="utf-8"
            )
            os.chmod(controller_path, 0o700)
            for relative in (
                "scripts/bootstrap_pull_deploy.py",
                "scripts/git_source_trust.py",
            ):
                path = source / relative
                path.write_text(
                    f"# exact successor fixture: {relative}\n",
                    encoding="utf-8",
                )
                os.chmod(path, 0o700)
            sealed: dict[str, str] = {}
            for index, (
                source_path,
                _name,
                _mode,
                _classification,
                _evidence_key,
            ) in enumerate(CONTROLLER.ADOPTED_PREREQUISITE_FILES):
                path = source / source_path
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(path.parent, 0o700)
                payload = f"sealed prerequisite {index}\n".encode("utf-8")
                path.write_bytes(payload)
                os.chmod(path, 0o700)
                sealed[source_path] = CONTROLLER.sha256_bytes(payload)
            git("add", "--all")
            git("commit", "-m", "authority")
            authority_sha = git("rev-parse", "HEAD")
            authority_tree = git("rev-parse", "HEAD^{tree}")
            successor = source / "SUCCESSOR"
            successor.write_text("verifier fix only\n", encoding="utf-8")
            os.chmod(successor, 0o700)
            git("add", "SUCCESSOR")
            git("commit", "-m", "successor")
            target_sha = git("rev-parse", "HEAD")
            target_tree = git("rev-parse", "HEAD^{tree}")
            git("update-ref", "refs/remotes/origin/main", target_sha)

            for directory, _names, files in os.walk(source):
                current = Path(directory)
                os.chmod(current, 0o700)
                for name in files:
                    path = current / name
                    os.chmod(
                        path,
                        0o700 if not path.is_relative_to(source / ".git") else 0o600,
                    )
            # Worktree modes are part of this repository's committed identity.
            for path in source.rglob("*"):
                if path.is_file() and not path.is_relative_to(source / ".git"):
                    os.chmod(path, 0o700)

            readiness = {
                "source_sha": authority_sha,
                "source_tree": authority_tree,
            }
            delivery = {
                "remote_main": authority_sha,
                "ci": {"head_sha": authority_sha, "conclusion": "success"},
            }
            plan = {
                "source_readiness_sha256": CONTROLLER.canonical_json_digest(
                    readiness
                ),
                "delivery_gate_sha256": CONTROLLER.canonical_json_digest(
                    delivery
                ),
                "files": [
                    {
                        "source_path": source_path,
                        "name": name,
                        "sha256": sealed[source_path],
                    }
                    for source_path, name, _mode, _classification, _evidence_key in (
                        CONTROLLER.ADOPTED_PREREQUISITE_FILES
                    )
                ],
            }
            authority = {
                "source_sha": authority_sha,
                "source_tree": authority_tree,
                "plan_sha256": CONTROLLER.canonical_json_digest(plan),
                "adopted_deployment_sha256": "sha256:" + "a" * 64,
                "plan": plan,
            }
            controller = object.__new__(CONTROLLER.PullDeployController)
            controller.runtime_root = root / "runtime"
            git_permission = adopted_git_permission_takeover_fixture(
                source,
                controller.runtime_root,
                source_sha=target_sha,
                source_tree=target_tree,
                production_source_sha=authority_sha,
                production_source_tree=authority_tree,
                adopted_deployment_sha256=str(
                    authority["adopted_deployment_sha256"]
                ),
            )
            controller._git_permission_takeover = (  # type: ignore[method-assign]
                lambda: git_permission
            )
            controller._git_permission_source_successor_takeover = (  # type: ignore[method-assign]
                lambda **_kwargs: None
            )
            controller._unit_permission_takeover = (  # type: ignore[method-assign]
                lambda **_kwargs: adopted_unit_permission_binding_fixture(
                    git_permission,
                    source_sha=target_sha,
                    source_tree=target_tree,
                    adopted_deployment_sha256=str(
                        authority["adopted_deployment_sha256"]
                    ),
                    repository_root=source,
                )
            )
            controller._unit_permission_transaction_digests = (  # type: ignore[method-assign]
                mock.Mock(
                    return_value=(
                        "sha256:" + "d" * 64,
                        "sha256:" + "c" * 64,
                    )
                )
            )
            with mock.patch.object(
                CONTROLLER, "__file__", str(controller_path)
            ):
                binding = controller._plan_adopted_prerequisite_target_binding(
                    authority,
                    target_sha=target_sha,
                )
            self.assertEqual(binding["mode"], "ancestor-byte-identical")
            self.assertEqual(
                binding["target"],
                {"source_sha": target_sha, "source_tree": target_tree},
            )
            self.assertEqual(
                binding["target_source_trust_sha256"],
                CONTROLLER.canonical_json_digest(
                    binding["target_source_trust"]
                ),
            )
            self.assertEqual(
                controller._unit_permission_transaction_digests.call_count,
                2,
            )


class GitRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(
        self, command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[0] != "git":
            raise AssertionError(command)
        index = 1
        while index + 1 < len(command) and command[index] == "-c":
            index += 2
        arguments = command[index:]
        output = ""
        returncode = 0
        if arguments == ["symbolic-ref", "--short", "HEAD"]:
            output = "main\n"
        elif arguments == ["status", "--porcelain=v1", "--untracked-files=all"]:
            output = ""
        elif arguments == ["ls-files", "-z", "--cached"]:
            output = ""
        elif arguments == [
            "ls-files",
            "-z",
            "--others",
            "--ignored",
            "--exclude-standard",
        ]:
            output = ""
        elif arguments == ["remote", "get-url", "origin"]:
            output = CONTROLLER.REPOSITORY_HTTPS_URL + "\n"
        elif arguments == ["rev-parse", "HEAD"]:
            output = PREVIOUS_SHA + "\n"
        elif arguments == ["rev-parse", "HEAD^{tree}"]:
            output = PREVIOUS_TREE + "\n"
        elif arguments == [
            "ls-remote",
            "--exit-code",
            CONTROLLER.REPOSITORY_SSH_URL,
            "refs/heads/main",
        ]:
            output = f"{TARGET_SHA}\trefs/heads/main\n"
        else:
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(command, returncode, output, "")

    def request_json(self, _url: str, _token: str) -> dict[str, object]:
        raise AssertionError("unexpected network request")


class AdoptedUnitRunner:
    def __init__(self, units: list[dict[str, object]]) -> None:
        self.units = {str(record["name"]): record for record in units}

    def run(
        self, command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[:3] != ["systemctl", "--user", "show"]:
            raise AssertionError(command)
        record = self.units[command[3]]
        state = dict(record["systemd_state"])
        process = dict(record["process_identity"])
        values = {
            **state,
            "MainPID": str(process["main_pid"]),
            "InvocationID": str(process["invocation_id"]),
        }
        return subprocess.CompletedProcess(
            command,
            0,
            "".join(f"{key}={value}\n" for key, value in values.items()),
            "",
        )

    def request_json(self, _url: str, _token: str) -> dict[str, object]:
        raise AssertionError("unexpected network request")


class GithubRunner:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def run(
        self, *_args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError("unexpected subprocess")

    def request_json(self, url: str, _token: str) -> dict[str, object]:
        self.urls.append(url)
        if "/jobs?" in url:
            return {
                "jobs": [
                    {"name": name, "conclusion": "success"}
                    for name in sorted(
                        CONTROLLER._bridge_core.REQUIRED_CI_JOBS
                    )
                ]
            }
        return {
            "workflow_runs": [
                {
                    "id": 42,
                    "run_attempt": 2,
                    "head_sha": TARGET_SHA,
                    "head_branch": "main",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                    "path": ".github/workflows/ci.yml",
                }
            ]
        }


class ImageRunner:
    def __init__(self, *, wrong_revision: bool = False) -> None:
        self.commands: list[list[str]] = []
        self.wrong_revision = wrong_revision

    def run(
        self, command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            tag = command[3]
            root = tag.split(":sha-", 1)[0].split("@", 1)[0]
            sha = TARGET_SHA if not self.wrong_revision else PREVIOUS_SHA
            output = json.dumps(
                [
                    {
                        "Id": "sha256:" + "9" * 64,
                        "RepoDigests": [f"{root}@{DIGEST_A}"],
                        "Config": {
                            "Labels": {
                                "org.opencontainers.image.revision": sha,
                                "org.opencontainers.image.source": CONTROLLER.SOURCE_URL,
                                "org.opencontainers.image.version": f"sha-{TARGET_SHA}",
                            }
                        },
                    }
                ]
            )
        else:
            output = ""
        return subprocess.CompletedProcess(command, 0, output, "")

    def request_json(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("unexpected request")


class FakeLifecycle:
    def __init__(
        self, *, fail_at: str | None = None, admission_open: bool = False
    ) -> None:
        self.events: list[str] = []
        self.fail_at = fail_at
        self.admission_open = admission_open
        self.recovery_fence: dict[str, object] = {"fixture_instance": "instance-1"}
        self.runtime_repository: dict[str, object] = {
            "sha": TARGET_SHA,
            "tree": TARGET_TREE,
            "origin": CONTROLLER.REPOSITORY_SSH_URL,
        }
        self.runtime_instance = "fixture-runtime-1"
        self.runtime_state = "live"

    def _event(self, name: str) -> None:
        self.events.append(name)
        if self.fail_at == name:
            raise CONTROLLER.PullDeployError(f"injected {name} failure")

    def postgres_runtime_identity(
        self, _controller: object, _descriptor: object
    ) -> dict[str, object]:
        runtime = dict(mutable_data_evidence()["postgres_runtime"])
        return {
            "schema_version": 1,
            **runtime,
            "captured_at": CONTROLLER.utc_now(),
        }

    def drain(self, _controller: object, _descriptor: object) -> dict[str, object]:
        self._event("drain")
        self.admission_open = False
        return {"active_total": 0}

    def ensure_candidate_drained(
        self, _controller: object, _descriptor: object
    ) -> dict[str, object]:
        self._event("ensure-candidate-drained")
        self.admission_open = False
        return {"active_total": 0}

    def backup(
        self, controller: object, descriptor: dict[str, object]
    ) -> dict[str, object]:
        self._event("backup")
        backup_root = Path(getattr(controller, "backups_dir"))
        directory = backup_root / str(descriptor["operation_id"])
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        dump = directory / "database.dump"
        if not dump.exists():
            dump.write_bytes(b"fixture database dump\n")
            os.chmod(dump, 0o600)
        digest = CONTROLLER.sha256_file(dump)
        return {
            "path": str(dump),
            "sha256": digest,
            "restore_verification": {
                "schema_version": 1,
                "restored": True,
                "postgres_major": 16,
                "image": CONTROLLER.POSTGRES16_IMAGE,
                "dump_sha256": digest,
                "source_mutable_data_identity_sha256": (
                    CONTROLLER.canonical_json_digest(
                        CONTROLLER.mutable_data_identity(
                            mutable_data_evidence(
                                operation_id=str(
                                    descriptor["operation_id"]
                                )
                            )
                        )
                    )
                ),
                "ledger": [
                    {
                        "version": record["version"],
                        "checksum": record["checksum"],
                    }
                    for record in B_MANIFEST_RECORDS[:11]
                ],
            },
        }

    def backup_rollback(
        self,
        controller: object,
        descriptor: dict[str, object],
        backup_operation_id: str,
    ) -> dict[str, object]:
        projected = dict(descriptor)
        projected["operation_id"] = backup_operation_id
        backup = self.backup(controller, projected)
        marker = CONTROLLER.load_private_json(
            Path(getattr(controller, "marker_path"))
        )
        before = marker["mutable_data_before"]
        backup["restore_verification"][
            "source_mutable_data_identity_sha256"
        ] = CONTROLLER.canonical_json_digest(
            CONTROLLER.mutable_data_identity(before)
        )
        return backup

    def stop(self, _controller: object, _descriptor: object) -> None:
        self._event("stop")
        self.runtime_state = "stopped"

    def run_acceptance_probes(
        self,
        _controller: object,
        _descriptor: object,
        _authority_path: Path,
    ) -> None:
        self._event("acceptance-probes")

    def cleanup_acceptance_probe_proxy(
        self,
        _controller: object,
        _descriptor: object,
    ) -> None:
        return None

    @staticmethod
    def _guard_evidence(
        descriptor: dict[str, object], *, status: str
    ) -> dict[str, object]:
        controls = descriptor["monomer_dft"]["guard"]
        service = json.loads(json.dumps(controls["service"]))
        timer = json.loads(json.dumps(controls["timer"]))
        service["systemd_state"]["ActiveState"] = "inactive"
        service["systemd_state"]["SubState"] = "dead"
        service["main_pid"] = 0
        if status == "stopped":
            timer["systemd_state"]["ActiveState"] = "inactive"
            timer["systemd_state"]["SubState"] = "dead"
            observation = None
        else:
            active = controls["timer_policy"]["active"]
            timer["systemd_state"]["ActiveState"] = (
                "active" if active else "inactive"
            )
            timer["systemd_state"]["SubState"] = (
                "waiting" if active else "dead"
            )
            observation = {
                "status": "quarantined",
                "contention": True,
                "observed_at": CONTROLLER.utc_now(),
            }
        timer["main_pid"] = 0
        return {
            "schema_version": 1,
            "status": status,
            "service": service,
            "timer": timer,
            "observation": observation,
            "recorded_at": CONTROLLER.utc_now(),
        }

    def stop_dft_guard(
        self,
        _controller: object,
        descriptor: dict[str, object],
        *,
        allow_already_stopped: bool,
    ) -> dict[str, object]:
        del allow_already_stopped
        return self._guard_evidence(descriptor, status="stopped")

    def restore_dft_guard(
        self,
        _controller: object,
        descriptor: dict[str, object],
    ) -> dict[str, object]:
        return self._guard_evidence(descriptor, status="restored")

    def restore_database(
        self, _controller: object, _descriptor: object, backup: dict[str, object]
    ) -> dict[str, object]:
        self._event("restore_database")
        return {
            "restored": True,
            "dump_sha256": backup["sha256"],
            "ledger": backup["restore_verification"].get("ledger", []),
        }

    def migrate(self, _controller: object, _descriptor: object) -> dict[str, object]:
        self._event("migrate")
        return {
            "newly_applied": [
                record["version"] for record in B_MANIFEST_RECORDS[:11]
            ],
            "ledger": json.loads(json.dumps(B_MANIFEST_RECORDS[:11])),
        }

    def start(self, _controller: object, _descriptor: object) -> None:
        self._event("start")
        self.runtime_state = "live"
        self.admission_open = False

    def ensure_acceptance_ingress_isolated(
        self, _controller: object, _descriptor: object
    ) -> None:
        self.admission_open = False

    def verification(self) -> dict[str, object]:
        return {
            "health": "ok",
            "runtime_identity": {
                "repository": json.loads(
                    json.dumps(self.runtime_repository)
                ),
                "worker_instance_id": self.runtime_instance,
            },
            "recovery_fence": dict(self.recovery_fence),
        }

    def _capture_runtime_repository(self, controller: object) -> None:
        self.runtime_repository = json.loads(
            json.dumps(
                getattr(controller, "repository_identity")(
                    require_ssh_origin=True
                )
            )
        )

    def verify(self, _controller: object, _descriptor: object) -> dict[str, object]:
        self._event("verify")
        self._capture_runtime_repository(_controller)
        return self.verification()

    def verify_acceptance_stability(
        self, _controller: object, _descriptor: object
    ) -> dict[str, object]:
        self._event("verify-acceptance-stability")
        self._capture_runtime_repository(_controller)
        return self.verification()

    def resume(
        self,
        _controller: object,
        _descriptor: object,
        expected_verification: object,
    ) -> None:
        self._event("resume")
        if expected_verification != self.verification():
            raise CONTROLLER.PullDeployError(
                "resumed runtime instance differs from committed verification"
            )
        self.admission_open = True

    def resume_unchanged(
        self,
        _controller: object,
        _descriptor: object,
        persist_verification,  # type: ignore[no-untyped-def]
        expected_verification=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self._event("resume-unchanged")
        if (
            expected_verification is not None
            and expected_verification != self.verification()
        ):
            raise CONTROLLER.PullDeployError(
                "unchanged runtime differs from committed verification"
            )
        persist_verification(self.verification())
        self.admission_open = True

    def resume_bootstrap_unchanged(
        self, _controller: object, _descriptor: object
    ) -> None:
        self._event("resume-bootstrap-unchanged")
        self.admission_open = True

    def bootstrap_can_resume_unchanged(
        self, _controller: object, _descriptor: object
    ) -> bool:
        self._event("bootstrap-admission-status")
        return self.admission_open

    def admission_is_open(self, _controller: object, _descriptor: object) -> bool:
        self._event("admission-status")
        return self.admission_open

    def verify_open_runtime(
        self,
        _controller: object,
        _descriptor: object,
        expected_verification: object,
    ) -> None:
        self._event("verify-open")
        if expected_verification != self.verification():
            raise CONTROLLER.PullDeployError(
                "open runtime instance differs from committed verification"
            )

    def prepare_recovery_runtime(
        self,
        _controller: object,
        _descriptor: object,
        expected_verification: object,
        *,
        allow_unfenced: bool,
        allow_partial_stop: bool = False,
    ) -> dict[str, object]:
        self._event("recovery-isolate")
        if self.runtime_state != "live":
            if self.runtime_state not in {
                "partial",
                "stopped",
                "failed",
                "activating",
                "deactivating",
            }:
                raise CONTROLLER.PullDeployError(
                    "runtime process state is invalid during recovery"
                )
            if allow_partial_stop:
                prior_state = self.runtime_state
                self._event(f"recovery-{prior_state}-stop")
                self.stop(_controller, _descriptor)
                return {
                    "runtime_state": "stopped",
                    "ingress_isolated": True,
                    "partial_runtime_converged": True,
                    "postgres_runtime_fence": self.postgres_runtime_identity(
                        _controller, _descriptor
                    ),
                }
            if self.runtime_state != "stopped":
                raise CONTROLLER.PullDeployError(
                    "runtime is partially stopped during recovery"
                )
            self._event("recovery-stopped-prove")
            return {
                "runtime_state": "stopped",
                "ingress_isolated": True,
                "postgres_runtime_fence": self.postgres_runtime_identity(
                    _controller, _descriptor
                ),
            }
        if expected_verification is not None:
            if expected_verification != self.verification():
                raise CONTROLLER.PullDeployError(
                    "recovery runtime instance differs from committed verification"
                )
        elif not allow_unfenced:
            raise CONTROLLER.PullDeployError(
                "runtime recovery lacks committed verification evidence"
            )
        self._event("recovery-redrain")
        self.admission_open = False
        return {
            "runtime_state": "drained",
            "ingress_isolated": True,
            "drain": {"active_total": 0},
            "verification": self.verification(),
        }


class LostResumeLifecycle(FakeLifecycle):
    def __init__(self) -> None:
        super().__init__()
        self.lose_next_resume = False

    def resume(
        self,
        controller: object,
        descriptor: object,
        expected_verification: object,
    ) -> None:
        marker = CONTROLLER.load_private_json(getattr(controller, "marker_path"))
        if marker.get("verification") != expected_verification:
            raise AssertionError("runtime fence was not durable before resume")
        super().resume(controller, descriptor, expected_verification)
        if self.lose_next_resume:
            self.lose_next_resume = False
            raise CONTROLLER.PullDeployError(
                "injected lost response after admission commit"
            )


class LostUnchangedResumeLifecycle(FakeLifecycle):
    def __init__(self) -> None:
        super().__init__()
        self.lose_next_unchanged_resume = False

    def resume_unchanged(
        self,
        controller: object,
        descriptor: object,
        persist_verification,  # type: ignore[no-untyped-def]
        expected_verification=None,  # type: ignore[no-untyped-def]
    ) -> None:
        super().resume_unchanged(
            controller,
            descriptor,
            persist_verification,
            expected_verification,
        )
        marker = CONTROLLER.load_private_json(getattr(controller, "marker_path"))
        if marker.get("verification") != self.verification():
            raise AssertionError(
                "unchanged runtime fence was not durable before resume"
            )
        if self.lose_next_unchanged_resume:
            self.lose_next_unchanged_resume = False
            raise CONTROLLER.PullDeployError("injected lost unchanged-resume response")


class FixtureController(CONTROLLER.PullDeployController):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.source_sha = PREVIOUS_SHA
        self.source_tree = PREVIOUS_TREE
        self.rollback_called = False
        self.prerequisite_is_ancestor = True
        if self.active_control_path.exists():
            return
        with self.deployment_lock():
            candidate = super().prepare_control_release(
                operation_id="bootstrap-controls-fixture",
                target_sha=TARGET_SHA,
                target_tree=TARGET_TREE,
            )
        manifest, _root = CONTROLLER._control_runtime.load_control_release(
            self.runtime_root, candidate["release_id"]
        )
        active = {
            "schema_version": CONTROLLER._control_runtime.ACTIVE_CONTROL_SCHEMA_VERSION,
            "protocol_version": CONTROLLER._control_runtime.PROTOCOL_VERSION,
            "component": "deployment-controls",
            "generation": 1,
            "release_id": candidate["release_id"],
            "source_sha": TARGET_SHA,
            "source_tree": TARGET_TREE,
            "manifest_sha256": CONTROLLER.sha256_file(
                _root / CONTROLLER._control_runtime.CONTROL_MANIFEST_NAME
            ),
            "operation_id": "bootstrap-controls-fixture",
            "previous_release_id": None,
            "activated_at": CONTROLLER.utc_now(),
        }
        CONTROLLER.atomic_json(self.active_control_path, active)
        source_readiness = {
            "schema_version": 2,
            "ready": True,
            "source_root": str(
                self.runtime_root / "fixture-bootstrap-source"
            ),
            "source_sha": candidate["source_sha"],
            "source_tree": candidate["source_tree"],
            "branch": "main",
            "origin": CONTROLLER.REPOSITORY_SSH_URL,
            "remote_names": ["origin"],
            "origin_fetch_urls": [CONTROLLER.REPOSITORY_SSH_URL],
            "origin_push_urls": [CONTROLLER.REPOSITORY_SSH_URL],
            "origin_main_sha": candidate["source_sha"],
            "standalone_object_database": True,
            "shallow": False,
            "dirty_entries": 0,
            "ignored_entries": 0,
            "unreachable_objects": 0,
            "replace_refs": 0,
            "special_index_entries": 0,
            "sparse_index": False,
            "owner_private": True,
            "group_or_world_writable": False,
        }
        takeover = {
            "schema_version": 1,
            "operation_id": "takeover-pull-fixture",
            "authority_sha": candidate["source_sha"],
            "authority_tree": candidate["source_tree"],
            "install_manifest_sha256": "sha256:" + "3" * 64,
            "classification_sha256": "sha256:" + "4" * 64,
            "runtime_identity_sha256": "sha256:" + "5" * 64,
            "git_identity": {
                "branch": "refs/heads/main",
                "head_sha": PREVIOUS_SHA,
                "head_tree": PREVIOUS_TREE,
                "local_main_sha": PREVIOUS_SHA,
            },
            "pre_stopped_fence_sha256": "sha256:" + "6" * 64,
            "control_layout_sha256": "sha256:" + "7" * 64,
            "checkout_permissions_sha256": "sha256:" + "8" * 64,
            "applied_record_sha256": "sha256:" + "9" * 64,
        }
        takeover["binding_sha256"] = CONTROLLER.canonical_json_digest(
            takeover
        )
        CONTROLLER.atomic_json(
            self.state_dir / "bootstrap-control.json",
            {
                "schema_version": 2,
                "status": "completed",
                "source_sha": candidate["source_sha"],
                "source_tree": candidate["source_tree"],
                "source_readiness": source_readiness,
                "source_readiness_sha256": (
                    CONTROLLER.canonical_json_digest(source_readiness)
                ),
                "legacy_takeover": takeover,
                "delivery_gate": {
                    "remote_main": candidate["source_sha"],
                    "ci": {
                        "workflow_run_id": 42,
                        "run_attempt": 1,
                        "head_sha": candidate["source_sha"],
                        "head_branch": "main",
                        "event": "push",
                        "path": ".github/workflows/ci.yml",
                        "conclusion": "success",
                        "required_jobs": sorted(
                            CONTROLLER._bridge_core.REQUIRED_CI_JOBS
                        ),
                    },
                },
                "production_repository": {"fixture": True},
                "immutable_files": {
                    name: CONTROLLER.sha256_file(self.bin_dir / name)
                    for name in CONTROLLER.STABLE_HELPER_FILES
                },
                "worker_unit_takeover": {"fixture": True},
                "candidate_control": candidate,
                "active_control": active,
            },
        )
        seed_completed_alias_gate(self.runtime_root, manifest, _root)

    def apply_staged(self, **arguments):  # type: ignore[no-untyped-def]
        return super().apply(**arguments)

    def _load_postgres_rehearsal_report(
        self,
        _descriptor: dict[str, object],
        _descriptor_digest: str,
    ) -> dict[str, str]:
        operation_id = str(_descriptor["operation_id"])
        return {
            "schema_version": 1,
            "operation_id": operation_id,
            "target_sha": str(_descriptor["repository"]["target_sha"]),
            "descriptor_sha256": _descriptor_digest,
            "path": (
                "/var/lib/nexpoly-fixture/audit/deployment-rehearsals/"
                f"{operation_id}/report.json"
            ),
            "file_sha256": "sha256:" + "a" * 64,
            "report_sha256": "sha256:" + "b" * 64,
            "completed_at": "2026-01-01T00:00:00Z",
            "dump_sha256": "sha256:" + "c" * 64,
            "journal_head_sha256": "sha256:" + "d" * 64,
        }

    def apply(self, **arguments):  # type: ignore[no-untyped-def]
        """Preserve legacy one-call fixture semantics outside staged tests."""

        state = self.apply_staged(**arguments)
        if self.marker_path.exists():
            marker = CONTROLLER.load_private_json(self.marker_path)
            if marker.get("phase") == "awaiting-acceptance":
                marker["acceptance_started_at"] = "2026-01-01T00:00:00Z"
                marker["acceptance_not_before"] = "2026-01-01T00:15:00Z"
                CONTROLLER.atomic_json(self.marker_path, marker)
                probe_report = {
                    "report_sha256": "sha256:" + "1" * 64,
                    "file_sha256": "sha256:" + "2" * 64,
                    "authority_sha256": "sha256:" + "3" * 64,
                    "finished_at": None,
                }

                def load_probe_report(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                    if probe_report["finished_at"] is None:
                        probe_report["finished_at"] = CONTROLLER.utc_now()
                    return dict(probe_report)

                with mock.patch.object(
                    self,
                    "_load_acceptance_probe_report",
                    side_effect=load_probe_report,
                ), mock.patch.object(
                    self.lifecycle,
                    "run_acceptance_probes",
                    return_value=None,
                ):
                    observing = super().accept(
                        target_sha=arguments["target_sha"],
                        operation_id=arguments["operation_id"],
                    )
                    if observing.get("status") != "maintenance-observation":
                        raise AssertionError("fixture did not enter acceptance hold")
                    real_datetime = CONTROLLER.dt.datetime

                    class FutureDateTime(real_datetime):
                        @classmethod
                        def now(cls, tz=None):  # type: ignore[no-untyped-def]
                            return real_datetime.now(tz) + CONTROLLER.dt.timedelta(
                                seconds=CONTROLLER.ACCEPTANCE_HOLD_SECONDS + 1
                            )

                    with mock.patch.object(
                        CONTROLLER.dt, "datetime", FutureDateTime
                    ):
                        return super().accept(
                            target_sha=arguments["target_sha"],
                            operation_id=arguments["operation_id"],
                        )
        return state

    def _git_show(self, _target_sha: str, relative: str) -> bytes:
        return (REPOSITORY_ROOT / relative).read_bytes()

    def _git_permission_takeover(self) -> dict[str, object] | None:
        fixture = getattr(self, "_fixture_git_permission_takeover", None)
        if fixture is not None:
            return json.loads(json.dumps(fixture))
        return super()._git_permission_takeover()

    def _git_permission_source_successor_takeover(  # type: ignore[no-untyped-def]
        self,
        **_kwargs,
    ):
        fixture = getattr(
            self, "_fixture_git_permission_source_successor_takeover", None
        )
        if fixture is not None:
            root = _kwargs.get("git_permission_takeover")
            if (
                isinstance(root, dict)
                and fixture["predecessor_authority_sha256"]
                != root["authority_file_sha256"]
            ):
                fixture = adopted_git_permission_source_successor_fixture(
                    root,
                    target_sha=str(fixture["target_source_sha"]),
                    target_tree=str(fixture["target_source_tree"]),
                    delivery_gate=fixture["delivery_gate"],
                    files=fixture["fixed_files"],
                    production_repository_transition=fixture[
                        "production_repository_transition"
                    ],
                )
                self._fixture_git_permission_source_successor_takeover = fixture
            return json.loads(json.dumps(fixture))
        return super()._git_permission_source_successor_takeover(**_kwargs)

    def _unit_permission_takeover(self, **_kwargs):  # type: ignore[no-untyped-def]
        fixture = getattr(self, "_fixture_unit_permission_takeover", None)
        if fixture is not None:
            git_permission = _kwargs.get("git_permission_takeover")
            source_successor = _kwargs.get(
                "git_permission_source_successor_takeover"
            )
            fixture_successor = fixture["git_permission_successor"]
            if (
                isinstance(git_permission, dict)
                and fixture["adopted_git_permissions_sha256"]
                != git_permission["authority_file_sha256"]
                or isinstance(source_successor, dict)
                and (
                    fixture_successor.get("schema_version") != 2
                    or fixture_successor.get(
                        "source_successor_authority"
                    )
                    != source_successor
                )
            ):
                fixture = adopted_unit_permission_binding_fixture(
                    git_permission,
                    source_successor_takeover=source_successor,
                    source_sha=fixture["source_sha"],
                    source_tree=fixture["source_tree"],
                    adopted_deployment_sha256=fixture[
                        "adopted_deployment_sha256"
                    ],
                    file_digests={
                        path: CONTROLLER.sha256_bytes(
                            self._git_show(fixture["source_sha"], path)
                        )
                        for path in CONTROLLER.ADOPTED_UNIT_PERMISSION_SUCCESSOR_FILES
                    },
                )
                self._fixture_unit_permission_takeover = fixture
            return json.loads(json.dumps(fixture))
        return super()._unit_permission_takeover(**_kwargs)

    def _bootstrap_router_successor_takeover(  # type: ignore[no-untyped-def]
        self,
        *,
        source_successor,
        unit_permission,
    ):
        if hasattr(self, "_fixture_bootstrap_router_successor"):
            fixture = self._fixture_bootstrap_router_successor
            return (
                json.loads(json.dumps(fixture))
                if fixture is not None
                else None
            )
        if source_successor is None or unit_permission is None:
            return None
        return {
            "target_source_sha": source_successor["target_source_sha"],
            "target_source_tree": source_successor["target_source_tree"],
            "production_git_snapshot_authority_sha256": source_successor[
                "snapshot_authority_sha256"
            ],
            "source_successor_authority_sha256": source_successor[
                "authority_file_sha256"
            ],
            "unit_permission_authority_sha256": unit_permission[
                "authority_file_sha256"
            ],
            "bootstrap_router_intent_sha256": "sha256:" + "6" * 64,
            "bootstrap_router_authority_sha256": "sha256:" + "7" * 64,
        }

    def _production_source_trust_digest(
        self,
        *,
        source_sha: str,
        source_tree: str,
    ) -> str:
        fixture = getattr(
            self, "_fixture_git_permission_source_successor_takeover", None
        )
        if fixture is not None and (
            fixture["production_source_sha"] == source_sha
            and fixture["production_source_tree"] == source_tree
        ):
            return str(
                getattr(
                    self,
                    "_fixture_production_source_trust_sha256",
                    fixture["source_trust_sha256"],
                )
            )
        return super()._production_source_trust_digest(
            source_sha=source_sha,
            source_tree=source_tree,
        )

    def _source_successor_completed_journal_digest(  # type: ignore[override]
        self,
        source_successor: dict[str, object],
    ) -> str:
        journal = (
            self.runtime_root
            / CONTROLLER.ADOPTED_GIT_PERMISSION_SOURCE_SUCCESSOR_TRANSACTION_DIRECTORY
            / f"{source_successor['operation_id']}.json"
        )
        if journal.exists() or journal.is_symlink():
            return super()._source_successor_completed_journal_digest(
                source_successor
            )
        fixture = getattr(
            self,
            "_fixture_source_successor_completed_journal_sha256",
            "sha256:" + "e" * 64,
        )
        return str(fixture)

    def _unit_permission_transaction_digests(  # type: ignore[override]
        self,
        unit_permission: dict[str, object],
    ) -> tuple[str, str]:
        transaction_root = (
            self.runtime_root
            / CONTROLLER.ADOPTED_UNIT_PERMISSION_TRANSACTION_DIRECTORY
        )
        if transaction_root.exists() or transaction_root.is_symlink():
            return super()._unit_permission_transaction_digests(
                unit_permission
            )
        return (
            str(
                getattr(
                    self,
                    "_fixture_unit_permission_completed_journal_sha256",
                    "sha256:" + "d" * 64,
                )
            ),
            str(
                getattr(
                    self,
                    "_fixture_unit_permission_transaction_inventory_sha256",
                    "sha256:" + "c" * 64,
                )
            ),
        )

    def _current_adoption_successor_lineage(  # type: ignore[override]
        self,
        descriptor: dict[str, object],
    ) -> dict[str, object] | None:
        previous = descriptor.get("previous_deployment")
        binding = descriptor.get("adopted_prerequisite_target_binding")
        successor_binding = (
            binding
            if isinstance(binding, dict)
            and binding.get("schema_version") in {4, 5}
            else None
        )
        previous_lineage = (
            previous.get("adoption_successor_lineage")
            if isinstance(previous, dict)
            and isinstance(previous.get("adoption_successor_lineage"), dict)
            else None
        )
        if previous_lineage is not None and not (
            previous_lineage.get("schema_version")
            < (3 if successor_binding.get("schema_version") == 5 else 2)
            if successor_binding is not None
            else False
        ):
            return json.loads(json.dumps(previous_lineage))
        if successor_binding is None:
            return None
        source_successor = successor_binding[
            "git_permission_source_successor_authority"
        ]
        unit = successor_binding["unit_permission_authority"]
        lineage = {
            "schema_version": (
                3 if successor_binding["schema_version"] == 5 else 2
            ),
            "source_successor_authority_sha256": source_successor[
                "authority_file_sha256"
            ],
            "source_successor_completed_journal_sha256": (
                successor_binding[
                    "source_successor_completed_journal_sha256"
                ]
            ),
            "unit_permission_authority_sha256": unit[
                "authority_file_sha256"
            ],
            "unit_permission_completed_journal_sha256": successor_binding[
                "unit_permission_completed_journal_sha256"
            ],
            "unit_permission_transaction_inventory_sha256": successor_binding[
                "unit_permission_transaction_inventory_sha256"
            ],
        }
        if successor_binding["schema_version"] == 5:
            lineage.update(
                {
                    field: successor_binding[field]
                    for field in (
                        "production_git_snapshot_authority_sha256",
                        "bootstrap_router_intent_sha256",
                        "bootstrap_router_authority_sha256",
                    )
                }
            )
        return lineage

    def repository_identity(
        self, *, require_ssh_origin: bool = False
    ) -> dict[str, str]:
        return {
            "sha": self.source_sha,
            "tree": self.source_tree,
            "origin": (
                CONTROLLER.REPOSITORY_SSH_URL
                if require_ssh_origin
                else CONTROLLER.REPOSITORY_HTTPS_URL
            ),
        }

    def remote_main(self) -> str:
        return TARGET_SHA

    def fetch_target(self, target_sha: str, _operation_id: str) -> str:
        if target_sha != TARGET_SHA:
            raise AssertionError(target_sha)
        return TARGET_TREE

    def _fixture_adopted_prerequisite_target_binding(
        self,
        authority: dict[str, object],
        *,
        target_sha: str,
        target_tree: str,
        materialization_phase: str = "baseline",
        materialization_operation_id: str | None = None,
    ) -> dict[str, object]:
        source_sha = str(authority["source_sha"])
        authority_digests = {
            source_path: CONTROLLER.sha256_bytes(
                self._git_show(source_sha, source_path)
            )
            for source_path, _name, _mode, _classification, _evidence_key in (
                CONTROLLER.ADOPTED_PREREQUISITE_FILES
            )
        }
        target_digests = {
            source_path: CONTROLLER.sha256_bytes(
                self._git_show(target_sha, source_path)
            )
            for source_path, _name, _mode, _classification, _evidence_key in (
                CONTROLLER.ADOPTED_PREREQUISITE_FILES
            )
        }
        git_permission = self._git_permission_takeover()
        source_successor = self._git_permission_source_successor_takeover(
            git_permission_takeover=git_permission,
        )
        if source_successor is not None:
            if source_successor["delivery_gate"] != {
                "remote_main": target_sha,
                "ci": self.ci_evidence(target_sha),
            }:
                raise CONTROLLER.PullDeployError(
                    "target CI differs from source-successor delivery authority"
                )
            if source_successor[
                "source_trust_sha256"
            ] != self._production_source_trust_digest(
                source_sha=source_successor["production_source_sha"],
                source_tree=source_successor["production_source_tree"],
            ):
                raise CONTROLLER.PullDeployError(
                    "production source trust differs from source-successor authority"
                )
        unit_permission = self._unit_permission_takeover(
            git_permission_takeover=git_permission,
            git_permission_source_successor_takeover=source_successor,
            verify_live=True,
        )
        unit_authority_digests = None
        unit_target_digests = None
        unit_is_ancestor = None
        unit_journal_digest = None
        unit_inventory_digest = None
        if unit_permission is not None:
            unit_successor = unit_permission["git_permission_successor"]
            unit_root = (
                unit_successor["root_authority"]
                if unit_successor["schema_version"] == 2
                else unit_successor["authority"]
            )
            unit_source = unit_root["source_sha"]
            unit_paths = (
                CONTROLLER.ADOPTED_UNIT_PERMISSION_SUCCESSOR_V2_FILES
                if unit_successor["schema_version"] == 2
                else CONTROLLER.ADOPTED_UNIT_PERMISSION_SUCCESSOR_FILES
            )
            unit_authority_digests = {
                path: CONTROLLER.sha256_bytes(
                    self._git_show(unit_source, path)
                )
                for path in unit_paths
            }
            unit_target_digests = {
                path: CONTROLLER.sha256_bytes(
                    self._git_show(target_sha, path)
                )
                for path in unit_paths
            }
            unit_is_ancestor = self.prerequisite_is_ancestor
            if unit_successor["schema_version"] == 2:
                (
                    unit_journal_digest,
                    unit_inventory_digest,
                ) = self._unit_permission_transaction_digests(
                    unit_permission
                )
        source_predecessor_identities = None
        source_target_identities = None
        source_is_ancestor = None
        if source_successor is not None:
            source_predecessor_identities = {
                record["path"]: record["predecessor"]
                for record in source_successor["fixed_files"]
            }
            source_target_identities = {
                record["path"]: record["target"]
                for record in source_successor["fixed_files"]
            }
            source_is_ancestor = self.prerequisite_is_ancestor
        bootstrap_router_successor = (
            self._bootstrap_router_successor_takeover(
                source_successor=source_successor,
                unit_permission=unit_permission,
            )
            if source_successor is not None
            else None
        )
        if (
            source_successor is not None
            and source_successor.get("schema_version") == 2
            and bootstrap_router_successor is None
        ):
            raise CONTROLLER.PullDeployError(
                "first source-successor deployment lacks bootstrap-router authority"
            )
        return self._build_adopted_prerequisite_target_binding(
            authority,
            target_sha=target_sha,
            target_tree=target_tree,
            is_ancestor=self.prerequisite_is_ancestor,
            authority_file_digests=authority_digests,
            target_file_digests=target_digests,
            git_permission_takeover=git_permission,
            git_permission_source_successor_takeover=source_successor,
            source_successor_completed_journal_sha256=(
                self._source_successor_completed_journal_digest(
                    source_successor
                )
                if source_successor is not None
                else None
            ),
            production_repository_materialization=(
                production_repository_materialization_fixture(
                    phase=materialization_phase,
                    operation_id=materialization_operation_id,
                    transition=source_successor[
                        "production_repository_transition"
                    ],
                )
                if source_successor is not None
                else None
            ),
            source_successor_is_ancestor=source_is_ancestor,
            source_successor_predecessor_file_identities=(
                source_predecessor_identities
            ),
            source_successor_target_file_identities=(
                source_target_identities
            ),
            unit_permission_takeover=unit_permission,
            unit_permission_completed_journal_sha256=(
                unit_journal_digest
            ),
            unit_permission_transaction_inventory_sha256=(
                unit_inventory_digest
            ),
            bootstrap_router_successor=bootstrap_router_successor,
            unit_permission_is_ancestor=unit_is_ancestor,
            unit_permission_authority_file_digests=(
                unit_authority_digests
            ),
            unit_permission_target_file_digests=unit_target_digests,
        )

    def _plan_adopted_prerequisite_target_binding(
        self,
        authority: dict[str, object],
        *,
        target_sha: str,
    ) -> dict[str, object]:
        return self._fixture_adopted_prerequisite_target_binding(
            authority,
            target_sha=target_sha,
            target_tree=TARGET_TREE,
        )

    def _prepared_adopted_prerequisite_target_binding(
        self,
        authority: dict[str, object],
        *,
        target_sha: str,
        target_tree: str,
        operation_id: str,
    ) -> dict[str, object]:
        return self._fixture_adopted_prerequisite_target_binding(
            authority,
            target_sha=target_sha,
            target_tree=target_tree,
            materialization_phase="prepared",
            materialization_operation_id=operation_id,
        )

    def ci_evidence(self, target_sha: str) -> dict[str, object]:
        return {
            "workflow_run_id": 42,
            "run_attempt": 1,
            "head_sha": target_sha,
            "head_branch": "main",
            "event": "push",
            "path": ".github/workflows/ci.yml",
            "conclusion": "success",
            "required_jobs": sorted(
                CONTROLLER._bridge_core.REQUIRED_CI_JOBS
            ),
        }

    def image_evidence(self, role: str, target_sha: str) -> dict[str, str]:
        return image_record(role, target_sha)

    def _revalidate_materialized_images(
        self,
        _images: object,
        *,
        source_sha: str,
        pull: bool,
    ) -> None:
        del source_sha, pull

    def postgres_restore_image_evidence(self) -> dict[str, str]:
        return {
            "digest_ref": CONTROLLER.POSTGRES16_IMAGE,
            "image_id": "sha256:" + "5" * 64,
        }

    def controller_digest(self) -> str:
        return CONTROLLER.sha256_file(SCRIPT)

    def stable_helper_evidence(self) -> dict[str, str]:
        return {name: "sha256:" + "d" * 64 for name in CONTROLLER.STABLE_HELPER_FILES}

    def validate_installed_controls_against_target(self, _target_sha: str) -> None:
        return

    def production_deploy_values(self, *, check_free_space: bool) -> dict[str, str]:
        return {
            "fixture": str(check_free_space),
            "NEXPOLY_BOOTSTRAP_LEGACY_RUNTIME_SHA256": "sha256:" + "e" * 64,
        }

    def external_database_audit_evidence(
        self,
        policy: dict[str, object],
        *,
        lightweight_revalidation: bool = False,
    ) -> dict[str, object]:
        del lightweight_revalidation
        return CONTROLLER.validate_external_database_audit_binding(
            external_database_audit_binding(self.runtime_root),
            expected_policy=policy,
        )

    def _capture_mutable_data(
        self, descriptor: dict[str, object]
    ) -> dict[str, object]:
        evidence = mutable_data_evidence(
            operation_id=str(descriptor["operation_id"])
        )
        evidence["captured_at"] = CONTROLLER.utc_now()
        return evidence

    def asset_evidence(self, expected_digest: str) -> dict[str, object]:
        target = self.runtime_root / "fixture-assets" / expected_digest.split(":", 1)[1]
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target.parent, 0o700)
        os.chmod(target, 0o700)
        return {
            "pointer_path": str(self.state_dir / "current-assets"),
            "root": str(target),
            "manifest_sha256": expected_digest,
            "schema_version": 2,
            "byteff2_commit": "a" * 40,
            "inventory_sha256": "sha256:" + "b" * 64,
            "previous": None,
        }

    def prepare_worker_controls(
        self,
        *,
        operation_id: str,
        target_sha: str,
        executor_control: dict[str, object],
    ) -> dict[str, object]:
        operation = self.prepared_root / operation_id
        candidate = operation / CONTROLLER.MONOMER_MD_UNIT_NAME
        candidate.write_text("fixture unit\n", encoding="utf-8")
        os.chmod(candidate, 0o600)
        target = self.runtime_root / "config" / CONTROLLER.MONOMER_MD_UNIT_NAME
        worker_env = self.runtime_root / "config/worker.env"
        if not worker_env.exists():
            write_private(
                worker_env,
                "MONOMER_MD_WORKER_MODE=real\n"
                "MONOMER_MD_MAX_ACTIVE_JOBS=1\n",
            )
        previous_worker_env = {
            "path": str(worker_env),
            "sha256": CONTROLLER.sha256_file(worker_env),
            "byteff2_python": "/opt/byteff2/bin/python",
            "byteff2_openmm_dir": "/opt/byteff2/openmm",
            "gmx_sha256": "sha256:" + "c" * 64,
        }
        candidate_worker_env = operation / "worker.env.candidate"
        candidate_worker_env.write_bytes(
            CONTROLLER.PullDeployController._md_worker_env_candidate_payload(
                worker_env.read_bytes()
            )
        )
        os.chmod(candidate_worker_env, 0o600)
        previous_worker_env_backup = operation / "worker.env.previous"
        previous_worker_env_backup.write_bytes(worker_env.read_bytes())
        os.chmod(previous_worker_env_backup, 0o600)
        return {
            "worker_env": {
                "target": {
                    **previous_worker_env,
                    "sha256": CONTROLLER.sha256_file(candidate_worker_env),
                },
                "candidate_path": str(candidate_worker_env),
                "previous": previous_worker_env,
                "previous_backup_path": str(previous_worker_env_backup),
            },
            "systemd_unit": {
                "source_path": CONTROLLER.MONOMER_MD_UNIT_SOURCE,
                "candidate_path": str(candidate),
                "target_path": str(target),
                "sha256": CONTROLLER.sha256_file(candidate),
                "previous_present": False,
                "previous_sha256": None,
                "previous_backup_path": None,
                "previous_unit_state": {
                    "LoadState": "not-found",
                    "FragmentPath": "",
                    "DropInPaths": "",
                    "NeedDaemonReload": "no",
                    "UnitFileState": "",
                },
                "control_release_id": executor_control["release_id"],
                "launcher_sha256": "sha256:" + "a" * 64,
            },
        }

    def prepare_dft_controls(
        self,
        *,
        operation_id: str,
        previous_sha: str,
        target_sha: str,
        target_tree: str,
        executor_control: dict[str, object],
    ) -> dict[str, object]:
        del previous_sha
        operation = self.prepared_root / operation_id
        release_root = self.venv_root / "dft" / target_sha
        release_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(release_root.parent, 0o700)
        os.chmod(release_root, 0o700)
        runtime_manifest = release_root / "runtime.json"
        if not runtime_manifest.exists():
            write_private(runtime_manifest, '{"fixture":true}\n')
        runtime = {
            "root": str(release_root),
            "runtime_manifest_path": str(runtime_manifest),
            "runtime_manifest_sha256": CONTROLLER.sha256_file(runtime_manifest),
            "release_sha": target_sha,
            "source_tree": target_tree,
            "python": str(release_root / "venv/bin/python"),
            "requirements_lock_sha256": "sha256:" + "1" * 64,
            "aimnet_source_lock_sha256": "sha256:" + "2" * 64,
            "models": {
                name: "sha256:" + f"{index:x}" * 64
                for index, name in enumerate(
                    sorted(CONTROLLER.MONOMER_DFT_MODEL_ALIASES), start=3
                )
            },
            "runtime_inventory_sha256": "sha256:" + "9" * 64,
            "prepared_operation_id": operation_id,
            "prepared_at": CONTROLLER.utc_now(),
        }
        env_target = self.runtime_root / CONTROLLER.MONOMER_DFT_RUNTIME_ENV
        previous_present = env_target.exists()
        previous_sha256 = (
            CONTROLLER.sha256_file(env_target) if previous_present else None
        )
        previous_backup_path = None
        if previous_present:
            backup = operation / "previous-monomer-dft-runtime.env"
            backup.write_bytes(env_target.read_bytes())
            os.chmod(backup, 0o600)
            previous_backup_path = str(backup)
        env_values = {
            "MONOMER_DFT_RELEASE_SHA": target_sha,
            "MONOMER_DFT_RUNTIME_CONTRACT_SHA256": runtime[
                "runtime_manifest_sha256"
            ],
            "MONOMER_DFT_RUNTIME_INVENTORY_SHA256": runtime[
                "runtime_inventory_sha256"
            ],
            "MONOMER_DFT_PYTHON": runtime["python"],
            "AIMNET_CACHE_DIR": str(release_root / "aimnet-cache"),
            "WARP_CACHE_PATH": str(
                self.state_dir / "monomer-dft-warp-cache" / target_sha
            ),
            "NEXPOLY_DFT_GPU_GUARD_MODE": "observe",
        }
        env_candidate = operation / "monomer-dft-runtime.env"
        write_private(
            env_candidate,
            "".join(f"{key}={value}\n" for key, value in env_values.items()),
        )
        unit_candidate = operation / CONTROLLER.MONOMER_DFT_UNIT_NAME
        write_private(unit_candidate, "fixture DFT unit\n")
        unit_target = self.runtime_root / "config" / CONTROLLER.MONOMER_DFT_UNIT_NAME
        unit_previous_present = unit_target.exists()
        unit_previous_sha256 = (
            CONTROLLER.sha256_file(unit_target)
            if unit_previous_present
            else None
        )
        unit_previous_backup_path = None
        if unit_previous_present:
            backup = operation / f"previous-{CONTROLLER.MONOMER_DFT_UNIT_NAME}"
            backup.write_bytes(unit_target.read_bytes())
            os.chmod(backup, 0o600)
            unit_previous_backup_path = str(backup)
        guard_units: dict[str, dict[str, object]] = {}
        for name, unit_file_state, active_state, sub_state in (
            (
                CONTROLLER.MONOMER_DFT_GUARD_SERVICE_NAME,
                "static",
                "inactive",
                "dead",
            ),
            (
                CONTROLLER.MONOMER_DFT_GUARD_TIMER_NAME,
                "enabled",
                "active",
                "waiting",
            ),
        ):
            target = self.runtime_root / "config" / name
            if not target.exists():
                write_private(target, f"fixture {name}\n")
            guard_units[
                "service"
                if name == CONTROLLER.MONOMER_DFT_GUARD_SERVICE_NAME
                else "timer"
            ] = {
                "name": name,
                "target_path": str(target),
                "sha256": CONTROLLER.sha256_file(target),
                "systemd_state": {
                    "LoadState": "loaded",
                    "FragmentPath": str(target),
                    "DropInPaths": "",
                    "NeedDaemonReload": "no",
                    "UnitFileState": unit_file_state,
                    "ActiveState": active_state,
                    "SubState": sub_state,
                },
                "main_pid": 0,
                "invocation_id": "",
            }
        return CONTROLLER.validate_dft_descriptor(
            {
                "runtime": runtime,
                "runtime_env": {
                    "target": {
                        "path": str(env_target),
                        "sha256": CONTROLLER.sha256_file(env_candidate),
                        "values": env_values,
                    },
                    "candidate_path": str(env_candidate),
                    "previous_present": previous_present,
                    "previous_sha256": previous_sha256,
                    "previous_backup_path": previous_backup_path,
                },
                "systemd_unit": {
                    "source_path": CONTROLLER.MONOMER_DFT_UNIT_SOURCE,
                    "candidate_path": str(unit_candidate),
                    "target_path": str(unit_target),
                    "sha256": CONTROLLER.sha256_file(unit_candidate),
                    "previous_present": unit_previous_present,
                    "previous_sha256": unit_previous_sha256,
                    "previous_backup_path": unit_previous_backup_path,
                    "previous_systemd_state": {
                        "LoadState": "loaded" if unit_previous_present else "not-found",
                        "FragmentPath": str(unit_target) if unit_previous_present else "",
                        "DropInPaths": "",
                        "NeedDaemonReload": "no",
                        "UnitFileState": "enabled" if unit_previous_present else "",
                        "ActiveState": "active" if unit_previous_present else "inactive",
                        "SubState": "running" if unit_previous_present else "dead",
                    },
                    "control_release_id": executor_control["release_id"],
                    "launcher_path": str(
                        self.control_releases_dir
                        / str(executor_control["release_id"])
                        / "worker_slot_runtime.py"
                    ),
                    "launcher_sha256": "sha256:" + "a" * 64,
                },
                "gpu": {
                    "index": CONTROLLER.MONOMER_DFT_GPU_INDEX,
                    "uuid": CONTROLLER.MONOMER_DFT_GPU_UUID,
                    "guard_mode": "observe",
                    "guard_state_path": str(CONTROLLER.MONOMER_DFT_GUARD_STATE),
                    "guard_schema_version": 1,
                },
                "guard": {
                    **guard_units,
                    "timer_policy": {"enabled": True, "active": True},
                    "git_units": {
                        "service": {
                            "source_path": CONTROLLER.MONOMER_DFT_GUARD_SERVICE_SOURCE,
                            "sha256": guard_units["service"]["sha256"],
                        },
                        "timer": {
                            "source_path": CONTROLLER.MONOMER_DFT_GUARD_TIMER_SOURCE,
                            "sha256": guard_units["timer"]["sha256"],
                        },
                    },
                },
            },
            operation_id=operation_id,
        )

    def _revalidate_worker_controls(
        self,
        _descriptor: object,
        *,
        allow_target_environment: bool = False,
    ) -> None:
        del allow_target_environment
        return

    def _revalidate_dft_controls(self, _descriptor: object) -> None:
        return

    def _install_candidate_worker_unit(self, _descriptor: object) -> None:
        return

    def _restore_previous_worker_unit(self, _descriptor: object) -> None:
        return

    def _install_candidate_dft_unit(self, _descriptor: object) -> None:
        return

    def _restore_previous_dft_unit(self, _descriptor: object) -> None:
        return

    def _current_dft_projection(
        self,
        descriptor: dict[str, object],
        _marker: dict[str, object],
    ) -> dict[str, object]:
        dft = descriptor["monomer_dft"]
        unit = dft["systemd_unit"]
        return CONTROLLER.validate_dft_current_projection(
            {
                "runtime": dft["runtime"],
                "runtime_env": dft["runtime_env"]["target"],
                "systemd_unit": {
                    "target_path": unit["target_path"],
                    "sha256": unit["sha256"],
                    "systemd_state": {
                        "LoadState": "loaded",
                        "FragmentPath": unit["target_path"],
                        "DropInPaths": "",
                        "NeedDaemonReload": "no",
                        "UnitFileState": "enabled",
                        "ActiveState": "active",
                        "SubState": "running",
                    },
                    "process_identity": {
                        "main_pid": 456,
                        "invocation_id": "fixture-dft-invocation",
                    },
                    "control_release_id": unit["control_release_id"],
                    "launcher_path": unit["launcher_path"],
                    "launcher_sha256": unit["launcher_sha256"],
                },
                "gpu": dft["gpu"],
            }
        )

    def _source_evidence(self, _target_sha: str):  # type: ignore[no-untyped-def]
        return (
            {
                "sha256": DIGEST_A,
                "schema_version": 2,
                "asset_manifest_digest": (
                    CONTROLLER.SCHEMA_V2_ASSET_MANIFEST_DIGEST
                ),
                "predecessor_asset_manifest_digest": (
                    CONTROLLER.SCHEMA_V2_PREDECESSOR_ASSET_MANIFEST_DIGEST
                ),
                "changed_asset_trees": ["byteff2"],
                "datasets_on_asset_change": [],
            },
            {
                "sha256": DIGEST_B,
                "schema_version": 2,
                "records": json.loads(json.dumps(B_MANIFEST_RECORDS[:11])),
            },
            {
                "sha256": "sha256:" + "6" * 64,
                "files": {
                    "docker-compose.yml": DIGEST_A,
                    "docker-compose.prod.yml": DIGEST_B,
                },
            },
            b"fixture==1.0 --hash=sha256:" + b"1" * 64 + b"\n",
        )

    def prepare_md_slot(
        self,
        *,
        operation_id: str,
        target_sha: str,
        target_tree: str,
        lock_payload: bytes,
    ) -> dict[str, object]:
        slot = self.choose_inactive_slot()
        self._remove_owned_slot(slot, operation_id)
        venv = self.venv_root / f"md-{slot}" / "venv"
        (venv / "bin").mkdir(parents=True, mode=0o700)
        for path in (venv.parent, venv, venv / "bin"):
            os.chmod(path, 0o700)
        (venv / "bin/python").write_text("fixture\n", encoding="utf-8")
        record = {
            "schema_version": CONTROLLER.SLOT_RECORD_SCHEMA_VERSION,
            "component": "monomer-md",
            "status": "ready",
            "slot": slot,
            "source_sha": target_sha,
            "source_tree": target_tree,
            "worker_lock_sha256": CONTROLLER.sha256_bytes(lock_payload),
            "requirements_sha256": CONTROLLER.sha256_bytes(lock_payload),
            "wheel_cache_key": "sha256:" + "7" * 64,
            "wheel_inventory_sha256": "sha256:" + "8" * 64,
            "venv_prefix": str(venv.resolve()),
            "venv_inventory_sha256": CONTROLLER.worker_directory_inventory_digest(venv),
            "base_python_configured_path": sys.executable,
            "base_python_identity_sha256": "sha256:" + "9" * 64,
            "prepared_operation_id": operation_id,
            "prepared_at": CONTROLLER.utc_now(),
        }
        CONTROLLER.validate_slot_record(record, slot)
        CONTROLLER.atomic_json(self.slots_state_dir / f"md-{slot}.json", record)
        return record

    def _revalidate_pre_switch(self, descriptor: dict[str, object]) -> None:
        if self.production_config_evidence(check_free_space=True) != descriptor.get(
            "production_config"
        ):
            raise CONTROLLER.PullDeployError(
                "production configuration changed after prepare"
            )
        repository = descriptor["repository"]
        assert isinstance(repository, dict)
        if self.source_sha != repository["previous_sha"]:
            raise CONTROLLER.PullDeployError("fixture source changed")

    def _refresh_dft_guard_source_switch_fence(
        self,
        marker: dict[str, object],
        descriptor: dict[str, object],
    ) -> None:
        if (
            descriptor.get("schema_version")
            != CONTROLLER.DESCRIPTOR_SCHEMA_VERSION
        ):
            return
        self._stop_dft_guard_scheduling(marker, descriptor)
        evidence = marker["dft_guard_stop_evidence"]
        marker["dft_guard_source_switch_fence"] = {
            "guard_stop_evidence_sha256": CONTROLLER.canonical_json_digest(
                evidence
            ),
            "checked_at": CONTROLLER.utc_now(),
        }
        marker["updated_at"] = CONTROLLER.utc_now()
        self._write_marker(marker)

    def _switch_source(self, _descriptor: dict[str, object]) -> None:
        self.source_sha = TARGET_SHA
        self.source_tree = TARGET_TREE

    def _restore_source(self, _descriptor: dict[str, object]) -> None:
        previous = _descriptor.get("previous_deployment")
        if isinstance(previous, dict):
            self.source_sha = str(previous["source_sha"])
            self.source_tree = str(previous["source_tree"])
        else:
            self.source_sha = PREVIOUS_SHA
            self.source_tree = PREVIOUS_TREE

    def _rollback_failed_attempt(self, _descriptor: object, _marker: object) -> None:
        self.rollback_called = True
        self.source_sha = PREVIOUS_SHA
        self.source_tree = PREVIOUS_TREE


class PullDeployTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = mock.patch.dict(os.environ, {"NEXPOLY_ALLOW_TEST_ROOT": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix="nexpoly-pull-controller-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.production = self.root / "production"
        self.runtime = self.root / "runtime"
        self.production.mkdir(mode=0o755)
        (self.production / ".git").mkdir(mode=0o700)
        for relative in (
            "bin",
            "config",
            "config/docker",
            "state",
            "state/prepared",
            "state/worker-slots",
            "state/control-handoffs",
            "audit",
            "backups",
            "wheel-cache",
            "worker-venvs",
            "control-releases",
        ):
            path = self.runtime / relative
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        # The first nested mkdir creates the runtime root using the process
        # umask.  Keep this fixture deterministic when a test is run alone
        # under the CI default umask instead of relying on an earlier main().
        os.chmod(self.runtime, 0o700)
        lock = self.runtime / "state/deploy.lock"
        lock.write_text("", encoding="utf-8")
        os.chmod(lock, 0o600)
        registry_fixture_payload = '{"schema_version":1,"media":[]}\n'
        registry_fixture_sha256 = CONTROLLER.sha256_bytes(
            registry_fixture_payload.encode("utf-8")
        )
        authority_rules_fixture_payload = (
            Path(__file__).resolve().parents[2]
            / "ops/config/postgres-media-authority-rules.json"
        ).read_text(encoding="utf-8")
        authority_rules_fixture_sha256 = CONTROLLER.sha256_bytes(
            authority_rules_fixture_payload.encode("utf-8")
        )
        for name, content in (
            ("git-deploy-key", "fixture-key\n"),
            ("known_hosts", "github.com ssh-ed25519 fixture\n"),
            ("github-api-token", "fixture-token\n"),
            (
                "deploy.env",
                "\n".join(
                    (
                        f"NEXPOLY_RUNTIME_ROOT={self.runtime}",
                        f"NEXPOLY_APP_ENV_FILE={self.runtime / 'config/app.env'}",
                        f"NEXPOLY_ASSET_ROOT={self.runtime / 'state/current-assets'}",
                        "NEXPOLY_POSTGRES_USER=fixture_user",
                        "NEXPOLY_POSTGRES_PASSWORD=fixture-secret-0123456789",
                        "NEXPOLY_POSTGRES_DB=nexpoly",
                        "NEXPOLY_POSTGRES_PORT=55432",
                        "APP_POSTGRES_DSN=postgresql://fixture_user:fixture-secret-0123456789@lab-postgres:5432/nexpoly",
                        "PI_POSTGRES_DSN=postgresql://fixture_user:fixture-secret-0123456789@lab-postgres:5432/nexpoly",
                        "LAB_DATA_POSTGRES_DSN=postgresql://fixture_user:fixture-secret-0123456789@lab-postgres:5432/nexpoly",
                        "POLYTAO_ENABLED=true",
                        "MONOMER_MD_REQUIRE_TRANSPORT_READY=true",
                        "NEXPOLY_HEALTH_URLS=http://127.0.0.1:9000/health",
                        "NEXPOLY_MIN_FREE_BYTES=1073741824",
                        "NEXPOLY_POSTGRES_RESTORE_TMPFS_BYTES=8589934592",
                        (
                            "NEXPOLY_CONTRACT_0012_EXTERNAL_DATABASE_AUDIT_COMMAND="
                            + str(
                                self.runtime
                                / "bin"
                                / CONTROLLER.EXTERNAL_DATABASE_AUDIT_HELPER
                            )
                        ),
                        "NEXPOLY_CONTRACT_0012_DEV_AUDIT_USER=nexpoly_dev_auditor",
                        "NEXPOLY_CONTRACT_0012_MD_HEALTH_AUDIT_USER=nexpoly_health_auditor",
                        (
                            "NEXPOLY_CONTRACT_0012_MEDIA_AUTHORITY_RULES_SHA256="
                            + authority_rules_fixture_sha256
                        ),
                        f"NEXPOLY_WORKER_BASE_PYTHON={sys.executable}",
                        "",
                    )
                ),
            ),
            ("app.env", "ONLINE_KNOWLEDGE_API_KEY=fixture\n"),
        ):
            write_private(self.runtime / "config" / name, content)
        for name in CONTROLLER.STABLE_HELPER_FILES:
            helper = self.runtime / "bin" / name
            if name == CONTROLLER.EXTERNAL_DATABASE_AUDIT_HELPER:
                helper.write_bytes(
                    (
                        Path(__file__).resolve().parents[2]
                        / "scripts"
                        / CONTROLLER.EXTERNAL_DATABASE_AUDIT_HELPER
                    ).read_bytes()
                )
            else:
                helper.write_text(
                    f"fixture {name}\n",
                    encoding="utf-8",
                )
            os.chmod(helper, 0o700)
        for name in (
            "bootstrap-quiesce",
            "bootstrap-status",
            "bootstrap-resume-unchanged",
            "bootstrap-rollback",
            "bootstrap-active-jobs-probe",
            "bootstrap-legacy-runtime-status",
            "bootstrap-legacy-runtime-resume-unchanged",
            "bootstrap-legacy-runtime-restore",
            CONTROLLER.MUTABLE_DATA_AUDIT_HELPER,
        ):
            hook = self.runtime / "config" / name
            hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(hook, 0o700)
        write_private(
            self.runtime
            / "config"
            / CONTROLLER.EXTERNAL_DATABASE_MEDIA_AUTHORITY_RULES,
            authority_rules_fixture_payload,
        )
        write_private(
            self.runtime
            / "config"
            / CONTROLLER.EXTERNAL_DATABASE_MEDIA_REGISTRY,
            registry_fixture_payload,
        )
        write_private(
            self.runtime / "config" / CONTROLLER.MUTABLE_DATA_SERVICE_CONFIG,
            (
                "[nexpoly-mutable-audit]\n"
                "host=127.0.0.1\n"
                "port=55432\n"
                "dbname=nexpoly\n"
                "user=nexpoly_mutable_audit\n"
                "sslmode=disable\n"
                f"passfile={self.runtime / 'config' / CONTROLLER.MUTABLE_DATA_PGPASS}\n"
            ),
        )
        write_private(
            self.runtime / "config" / CONTROLLER.MUTABLE_DATA_PGPASS,
            "127.0.0.1:55432:nexpoly:nexpoly_mutable_audit:fixture\n",
        )
        write_private(
            self.runtime / "config/docker/config.json",
            json.dumps(
                {
                    "auths": {
                        "ghcr.io": {
                            "auth": base64.b64encode(b"fixture:read-only-token").decode(
                                "ascii"
                            )
                        }
                    }
                }
            ),
        )
        # Bootstrap normally installs the target control authority before the
        # first governed runtime takeover.  Seed that exact condition for both
        # base-controller plan tests and mutating fixture tests.
        FixtureController(
            self.production,
            self.runtime,
            runner=GitRunner(),
            lifecycle=FakeLifecycle(),
            apply=True,
        )

    def controller(
        self,
        *,
        runner: object | None = None,
        lifecycle: FakeLifecycle | None = None,
    ) -> FixtureController:
        return FixtureController(
            self.production,
            self.runtime,
            runner=runner or GitRunner(),
            lifecycle=lifecycle or FakeLifecycle(),
            apply=True,
        )


class RepositoryAndEvidenceTests(PullDeployTestCase):
    def test_bridge_media_authority_rules_are_exact_f_object_bytes(
        self,
    ) -> None:
        controller = self.controller()
        payload = b'{"schema_version":1,"rules":"fixture"}\n'
        role_sql = b"-- reviewed role SQL fixture\n"
        policy = {
            "external_database_audit": {
                **CONTROLLER._bridge_core.EXTERNAL_DATABASE_AUDIT_POLICY,
                "media_authority_rules_sha256": (
                    CONTROLLER.sha256_bytes(payload)
                ),
                "audit_role_sql_sha256": CONTROLLER.sha256_bytes(
                    role_sql
                ),
            }
        }
        with mock.patch.object(
            controller,
            "_git_show",
            side_effect=lambda _sha, relative: (
                payload
                if relative
                == CONTROLLER._bridge_core.MEDIA_AUTHORITY_RULES_RELATIVE_PATH
                else role_sql
            ),
        ) as git_show:
            self.assertEqual(
                controller._verify_bridge_media_authority_rules(
                    TARGET_SHA,
                    policy,
                ),
                CONTROLLER.sha256_bytes(payload),
            )
        self.assertEqual(
            git_show.call_args_list,
            [
                mock.call(
                    TARGET_SHA,
                    CONTROLLER._bridge_core.MEDIA_AUTHORITY_RULES_RELATIVE_PATH,
                ),
                mock.call(
                    TARGET_SHA,
                    CONTROLLER._bridge_core.MEDIA_AUDIT_ROLE_SQL_RELATIVE_PATH,
                ),
            ],
        )

        with (
            mock.patch.object(
                controller,
                "_git_show",
                return_value=b'{"schema_version":1,"rules":"drift"}\n',
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "differ from F policy",
            ),
        ):
            controller._verify_bridge_media_authority_rules(
                TARGET_SHA,
                policy,
            )

    def test_trusted_git_uses_fixed_binary_despite_shadowed_path(self) -> None:
        controller = self.controller()

        class RecordingRunner:
            def __init__(self) -> None:
                self.commands: list[list[str]] = []

            def run(
                self, command: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                self.commands.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")

        runner = RecordingRunner()
        controller.runner = runner
        with (
            mock.patch.object(
                controller,
                "_git_trust_preflight",
                return_value={"trusted": True},
            ),
            mock.patch.object(
                controller,
                "_clean_environment",
                return_value={"PATH": "/tmp/fake-git:/usr/local/bin:/usr/bin"},
            ),
        ):
            controller._git("status", "--porcelain=v1")
        self.assertEqual(len(runner.commands), 1)
        self.assertEqual(runner.commands[0][0], "/usr/bin/git")

    def test_stopped_bridge_guard_rejects_every_network_fetch_path(
        self,
    ) -> None:
        bundle = self.runtime / "sealed.bundle"
        write_private(bundle, "fixture bundle\n")
        guarded = CONTROLLER.OfflineBridgeRunner(GitRunner())

        for command in (
            [
                "git",
                "ls-remote",
                CONTROLLER.REPOSITORY_SSH_URL,
                "refs/heads/main",
            ],
            ["git", "fetch", CONTROLLER.REPOSITORY_SSH_URL],
            ["docker", "pull", "ghcr.io/example/image@sha256:" + "1" * 64],
            ["python3", "-m", "pip", "download", "fixture"],
            ["python3", "-m", "pip", "install", "fixture"],
            ["ssh", "production.example"],
            ["curl", "https://api.github.com/repos/example/project"],
        ):
            with self.subTest(command=command):
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "offline bridge revalidation",
                ):
                    guarded.run(command)

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "GitHub API",
        ):
            guarded.request_json("https://api.github.com", "token")
        CONTROLLER.OfflineBridgeRunner._validate_command(
            [
                "git",
                "-c",
                "credential.helper=",
                "fetch",
                "--no-tags",
                str(bundle),
                "+refs/heads/main:refs/nexpoly/prefetch/test/authority",
            ]
        )
        CONTROLLER.OfflineBridgeRunner._validate_command(
            ["docker", "image", "inspect", CONTROLLER.POSTGRES16_IMAGE]
        )
        CONTROLLER.OfflineBridgeRunner._validate_command(
            [
                "python3",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(self.runtime / "wheel-cache"),
                "fixture",
            ]
        )

    def test_git_and_control_mutations_require_deploy_lock(
        self,
    ) -> None:
        controller = self.controller()
        with mock.patch.object(controller, "_git") as git_call:
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "lacks deploy.lock ownership",
            ):
                CONTROLLER.PullDeployController.fetch_target(
                    controller, TARGET_SHA, OPERATION_ID
                )
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "lacks deploy.lock ownership",
            ):
                controller.materialize_prefetched_bridge_relation(
                    {},
                    create_target_ref=True,
                )
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "lacks deploy.lock ownership",
            ):
                controller.bridge_policy_relation(
                    TARGET_SHA,
                    create_target_ref=False,
                    fetch_authority=True,
                )
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "lacks deploy.lock ownership",
            ):
                controller.prepare_control_release(
                    operation_id=OPERATION_ID,
                    target_sha=TARGET_SHA,
                    target_tree=TARGET_TREE,
                )
        git_call.assert_not_called()

    def test_first_source_successor_target_mismatch_rejects_before_git(
        self,
    ) -> None:
        controller = object.__new__(CONTROLLER.PullDeployController)
        controller.adopted_git_permission_source_successor_path = (
            self.runtime / "successor-authority.json"
        )
        controller.current_state_path = self.runtime / "missing-current.json"
        controller._require_deploy_lock_for_staging = mock.Mock(
            return_value=None
        )
        controller._git_permission_takeover = mock.Mock(
            return_value={"authority": "fixture"}
        )
        controller._git_permission_source_successor_takeover = mock.Mock(
            return_value={
                "target_source_sha": TARGET_SHA,
                "target_source_tree": TARGET_TREE,
            }
        )
        controller._git = mock.Mock(
            side_effect=AssertionError(
                "Git was invoked for an unauthorized successor target"
            )
        )

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "target differs from its sealed authority",
        ):
            CONTROLLER.PullDeployController.fetch_target(
                controller,
                "5" * 40,
                OPERATION_ID,
            )
        controller._git.assert_not_called()

    def test_current_state_disables_one_time_source_successor_transition(
        self,
    ) -> None:
        controller = object.__new__(CONTROLLER.PullDeployController)
        controller.adopted_git_permission_source_successor_path = (
            self.runtime / "successor-authority.json"
        )
        controller.current_state_path = self.runtime / "current.json"
        write_private(controller.current_state_path, "fixture-current\n")
        controller._git_permission_takeover = mock.Mock(
            return_value={"authority": "fixture"}
        )
        controller._git_permission_source_successor_takeover = mock.Mock(
            return_value={
                "target_source_sha": TARGET_SHA,
                "target_source_tree": TARGET_TREE,
            }
        )

        self.assertIsNone(
            controller._source_successor_transition_for_target(TARGET_SHA)
        )

    def test_fetch_target_bounds_successor_reproof_by_bfp_state(self) -> None:
        cases = (
            (
                "baseline",
                ["baseline", "materialized", "prepared"],
                ["fetch-before", "fetch-after", "ref-after"],
                ["fetch", "update-ref"],
            ),
            (
                "materialized",
                ["materialized", "prepared"],
                ["fetch-before", "ref-after"],
                ["update-ref"],
            ),
            (
                "prepared",
                ["prepared"],
                ["fetch-before"],
                [],
            ),
        )
        for label, states, proof_phases, expected_mutations in cases:
            with self.subTest(state=label):
                controller = self.controller()
                proofs = mock.Mock(
                    side_effect=[{"phase": state} for state in states]
                )
                mutations = mock.Mock(return_value=None)
                observed_ref = mock.Mock(return_value=None)
                git_calls: list[tuple[str, ...]] = []

                def git(*arguments: str, **_kwargs: object) -> object:
                    git_calls.append(arguments)
                    if arguments[0] in {"fetch", "update-ref"}:
                        return SimpleNamespace(stdout="", returncode=0)
                    if arguments[:2] == (
                        "rev-parse",
                        "refs/remotes/nexpoly-deploy/main",
                    ):
                        return SimpleNamespace(
                            stdout=TARGET_SHA + "\n",
                            returncode=0,
                        )
                    if arguments[:2] == ("cat-file", "-t"):
                        return SimpleNamespace(
                            stdout="commit\n",
                            returncode=0,
                        )
                    if arguments[:2] == (
                        "rev-parse",
                        f"{TARGET_SHA}^{{tree}}",
                    ):
                        return SimpleNamespace(
                            stdout=TARGET_TREE + "\n",
                            returncode=0,
                        )
                    if arguments[0] == "merge-base":
                        return SimpleNamespace(stdout="", returncode=0)
                    raise AssertionError(arguments)

                controller._assert_source_successor_repository_state = (  # type: ignore[method-assign]
                    proofs
                )
                controller._assert_production_git_mutation_preconditions = (  # type: ignore[method-assign]
                    mutations
                )
                controller._observe_prepare_abort_prepared_ref = (  # type: ignore[method-assign]
                    observed_ref
                )
                controller.remote_main = mock.Mock(  # type: ignore[method-assign]
                    return_value=TARGET_SHA
                )
                controller._git = mock.Mock(  # type: ignore[method-assign]
                    side_effect=git
                )

                with controller.deployment_lock():
                    observed_tree = (
                        CONTROLLER.PullDeployController.fetch_target(
                            controller,
                            TARGET_SHA,
                            OPERATION_ID,
                        )
                    )

                self.assertEqual(observed_tree, TARGET_TREE)
                self.assertEqual(
                    [call.kwargs["phase"] for call in proofs.call_args_list],
                    proof_phases,
                )
                self.assertEqual(
                    [
                        arguments[0]
                        for arguments in git_calls
                        if arguments[0] in {"fetch", "update-ref"}
                    ],
                    expected_mutations,
                )
                self.assertEqual(
                    mutations.call_count,
                    len(expected_mutations),
                )
                self.assertEqual(
                    observed_ref.call_count,
                    0 if label == "prepared" else 1,
                )

    def test_fetch_rejects_git_write_domain_symlinks_before_git(self) -> None:
        vectors = (
            ("deploy-reflog", ".git/logs/refs/remotes/nexpoly-deploy"),
            ("refs", ".git/refs"),
            ("objects", ".git/objects"),
        )
        for label, victim_relative in vectors:
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                dir=self.root,
                prefix=f"write-domain-{label}-",
            ) as temporary:
                repository = Path(temporary) / "production"
                git_dir = repository / ".git"
                for relative in (
                    "refs/heads",
                    "refs/remotes/nexpoly-deploy",
                    "objects",
                    "logs/refs/remotes/nexpoly-deploy",
                ):
                    (git_dir / relative).mkdir(
                        parents=True,
                        exist_ok=True,
                        mode=0o700,
                    )
                for path in (git_dir, *git_dir.rglob("*")):
                    if path.is_dir():
                        path.chmod(0o700)
                write_private(
                    git_dir / "refs/heads/main",
                    PREVIOUS_SHA + "\n",
                )
                write_private(
                    git_dir / "refs/remotes/nexpoly-deploy/main",
                    TARGET_SHA + "\n",
                )
                write_private(
                    git_dir / "logs/refs/remotes/nexpoly-deploy/main",
                    "local-reflog\n",
                )

                external = Path(temporary) / f"external-{label}"
                external.mkdir(mode=0o700)
                sentinel = external / (
                    "main" if label == "deploy-reflog" else "sentinel"
                )
                write_private(sentinel, f"external-{label}-sentinel\n")
                before = sentinel.read_bytes()
                victim = repository / victim_relative
                if label == "deploy-reflog":
                    shutil.rmtree(victim)
                else:
                    victim.rename(Path(temporary) / f"held-{label}")
                victim.symlink_to(external, target_is_directory=True)

                controller = object.__new__(
                    CONTROLLER.PullDeployController
                )
                controller.production_root = repository
                controller._require_deploy_lock_for_staging = mock.Mock(
                    return_value=None
                )
                controller._git = mock.Mock(
                    side_effect=AssertionError(
                        "Git was invoked before write-domain validation"
                    )
                )
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "cannot be opened without links",
                ):
                    CONTROLLER.PullDeployController.fetch_target(
                        controller,
                        TARGET_SHA,
                        OPERATION_ID,
                    )
                controller._git.assert_not_called()
                self.assertEqual(sentinel.read_bytes(), before)

    def test_fetch_rechecks_write_domain_immediately_before_update_ref(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            dir=self.root,
            prefix="write-domain-update-ref-race-",
        ) as temporary:
            repository = Path(temporary) / "production"
            git_dir = repository / ".git"
            for relative in (
                "refs/heads",
                "refs/remotes/nexpoly-deploy",
                "objects",
                "logs/refs/remotes/nexpoly-deploy",
            ):
                (git_dir / relative).mkdir(
                    parents=True,
                    exist_ok=True,
                    mode=0o700,
                )
            for path in (git_dir, *git_dir.rglob("*")):
                if path.is_dir():
                    path.chmod(0o700)
            write_private(
                git_dir / "refs/heads/main",
                PREVIOUS_SHA + "\n",
            )
            write_private(
                git_dir / "refs/remotes/nexpoly-deploy/main",
                TARGET_SHA + "\n",
            )
            write_private(
                git_dir / "logs/refs/remotes/nexpoly-deploy/main",
                "local-reflog\n",
            )
            external = Path(temporary) / "external-nexpoly-refs"
            (external / "prepared").mkdir(
                parents=True,
                mode=0o700,
            )
            sentinel = external / "prepared" / OPERATION_ID
            write_private(sentinel, "external-prepared-ref-sentinel\n")
            before = sentinel.read_bytes()
            git_calls: list[tuple[str, ...]] = []

            def git(*arguments: str, **_kwargs: object) -> object:
                git_calls.append(arguments)
                if arguments[0] == "fetch":
                    return SimpleNamespace(stdout="", returncode=0)
                if arguments[:2] == (
                    "rev-parse",
                    "refs/remotes/nexpoly-deploy/main",
                ):
                    return SimpleNamespace(
                        stdout=TARGET_SHA + "\n",
                        returncode=0,
                    )
                if arguments[0] == "ls-remote":
                    return SimpleNamespace(
                        stdout=(
                            TARGET_SHA + "\trefs/heads/main\n"
                        ),
                        returncode=0,
                    )
                if arguments[:2] == ("cat-file", "-t"):
                    return SimpleNamespace(stdout="commit\n", returncode=0)
                if arguments[:2] == ("rev-parse", f"{TARGET_SHA}^{{tree}}"):
                    return SimpleNamespace(
                        stdout=TARGET_TREE + "\n",
                        returncode=0,
                    )
                if arguments[0] == "merge-base":
                    (git_dir / "refs/nexpoly").symlink_to(
                        external,
                        target_is_directory=True,
                    )
                    return SimpleNamespace(stdout="", returncode=0)
                raise AssertionError(arguments)

            controller = object.__new__(CONTROLLER.PullDeployController)
            controller.production_root = repository
            controller._require_deploy_lock_for_staging = mock.Mock(
                return_value=None
            )
            controller._observe_prepare_abort_prepared_ref = mock.Mock(
                return_value=None
            )
            controller._git = mock.Mock(side_effect=git)
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "cannot be opened without links",
            ):
                CONTROLLER.PullDeployController.fetch_target(
                    controller,
                    TARGET_SHA,
                    OPERATION_ID,
                )
            self.assertFalse(
                any(call and call[0] == "update-ref" for call in git_calls)
            )
            self.assertEqual(sentinel.read_bytes(), before)

    def test_source_switch_rejects_external_ref_symlink_before_update(
        self,
    ) -> None:
        controller = self.controller()
        seed_complete_private_git_layout(controller.production_root)
        external = self.root / "external-previous-ref"
        external.mkdir(mode=0o700)
        sentinel = external / "previous"
        write_private(sentinel, "external-previous-ref-sentinel\n")
        before = sentinel.read_bytes()
        (controller.production_root / ".git/refs/nexpoly").symlink_to(
            external,
            target_is_directory=True,
        )
        descriptor = {
            "schema_version": CONTROLLER.DESCRIPTOR_SCHEMA_VERSION,
            "operation_id": OPERATION_ID,
            "repository": {
                "previous_sha": PREVIOUS_SHA,
                "previous_tree": PREVIOUS_TREE,
                "target_sha": TARGET_SHA,
                "target_tree": TARGET_TREE,
            },
        }
        git_call = mock.Mock(
            return_value=subprocess.CompletedProcess([], 1, "", "")
        )
        controller._git = git_call  # type: ignore[method-assign]
        with (
            mock.patch.object(
                controller,
                "_assert_descriptor_source_successor_pre_switch_state",
                return_value=None,
            ),
            controller.deployment_lock(),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "cannot be opened without links",
            ),
        ):
            CONTROLLER.PullDeployController._switch_source(
                controller,
                descriptor,
            )
        self.assertFalse(
            any(
                call.args and call.args[0] == "update-ref"
                for call in git_call.call_args_list
            )
        )
        self.assertEqual(sentinel.read_bytes(), before)

    def test_source_restore_rejects_external_head_ref_before_reset(
        self,
    ) -> None:
        controller = self.controller()
        seed_complete_private_git_layout(controller.production_root)
        heads = controller.production_root / ".git/refs/heads"
        heads.rename(self.root / "held-heads")
        external = self.root / "external-heads"
        external.mkdir(mode=0o700)
        sentinel = external / "main"
        write_private(sentinel, "external-main-ref-sentinel\n")
        before = sentinel.read_bytes()
        heads.symlink_to(external, target_is_directory=True)
        controller._git = mock.Mock(  # type: ignore[method-assign]
            side_effect=AssertionError(
                "reset ran before write-domain validation"
            )
        )
        descriptor = {
            "repository": {
                "previous_sha": PREVIOUS_SHA,
                "previous_tree": PREVIOUS_TREE,
            }
        }
        with (
            controller.deployment_lock(),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "cannot be opened without links",
            ),
        ):
            CONTROLLER.PullDeployController._restore_source(
                controller,
                descriptor,
            )
        controller._git.assert_not_called()
        self.assertEqual(sentinel.read_bytes(), before)

    def test_bridge_token_requires_locked_current_remote_main(
        self,
    ) -> None:
        controller = self.controller()
        token_path = (
            controller.state_dir
            / CONTROLLER._bridge_core.TOKEN_RELATIVE_PATH
        )
        controller.remote_main = lambda: "5" * 40  # type: ignore[method-assign]
        with (
            controller.deployment_lock(),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "changed before descriptor publication",
            ),
        ):
            controller._reserve_current_bridge_token(
                authority_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
                policy_id="sha256:" + "a" * 64,
            )
        self.assertFalse(token_path.exists())
        self.assertFalse(token_path.is_symlink())

        controller.remote_main = lambda: TARGET_SHA  # type: ignore[method-assign]
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lacks deploy.lock ownership",
        ):
            controller._reserve_current_bridge_token(
                authority_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
                policy_id="sha256:" + "a" * 64,
            )
        self.assertFalse(token_path.exists())
        self.assertFalse(token_path.is_symlink())

    def test_bridge_plan_cas_rechecks_remote_main_before_publication(
        self,
    ) -> None:
        controller = self.controller()
        authority_sha = TARGET_SHA
        authority_tree = TARGET_TREE
        target_sha = "5" * 40
        target_tree = "6" * 40
        ready = {
            "source": {
                "authority": {
                    "sha": authority_sha,
                    "tree": authority_tree,
                },
                "target": {"sha": target_sha, "tree": target_tree},
            },
            "policy": {
                "target_sha": target_sha,
                "target_tree": target_tree,
                "target_ref": f"refs/nexpoly/bridge-target/{target_sha}",
                "policy_id": "sha256:" + "a" * 64,
            },
        }
        active_control = {
            "source_sha": authority_sha,
            "source_tree": authority_tree,
        }
        current = {
            "sha": PREVIOUS_SHA,
            "tree": PREVIOUS_TREE,
            "origin": CONTROLLER.REPOSITORY_SSH_URL,
        }
        with (
            mock.patch.object(
                controller,
                "remote_main",
                side_effect=[authority_sha, "7" * 40],
            ) as remote_main,
            mock.patch.object(
                controller,
                "production_config_evidence",
                return_value={"fixture": True},
            ),
            mock.patch.object(
                controller,
                "stable_helper_evidence",
                return_value={"fixture": True},
            ),
            mock.patch.object(
                controller,
                "active_control_evidence",
                return_value=active_control,
            ),
            mock.patch.object(
                controller,
                "maintenance_prefetch_evidence",
                return_value=(ready, {"fixture": True}),
            ),
            mock.patch.object(
                controller,
                "repository_identity",
                return_value=current,
            ),
            mock.patch.object(
                controller,
                "completed_legacy_takeover_evidence",
                return_value={"fixture": True},
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "changed while planning",
            ),
        ):
            controller.bridge_plan(
                authority_sha=authority_sha,
                operation_id=OPERATION_ID,
                prefetch_operation_id="prefetch-20260718-0001",
            )
        self.assertEqual(remote_main.call_count, 2)

    def test_prelock_bridge_handoff_validation_is_read_only(
        self,
    ) -> None:
        controller = self.controller()
        authority_sha = TARGET_SHA
        authority_tree = TARGET_TREE
        target_sha = "5" * 40
        target_tree = "6" * 40
        previous_control = controller.active_control_evidence()
        candidate = {
            "operation_id": OPERATION_ID,
            "source_sha": target_sha,
            "source_tree": target_tree,
        }
        prefetch = {"fixture": "prefetch-binding"}
        takeover = {"fixture": "takeover-binding"}
        record = {
            "schema_version": 2,
            "protocol_version": (
                CONTROLLER._control_runtime.PROTOCOL_VERSION
            ),
            "operation_id": OPERATION_ID,
            "authority_sha": authority_sha,
            "authority_tree": authority_tree,
            "target_sha": target_sha,
            "target_tree": target_tree,
            "target_ref": f"refs/nexpoly/bridge-target/{target_sha}",
            "policy_id": "sha256:" + "a" * 64,
            "policy_sha256": "sha256:" + "b" * 64,
            "prefetch_operation_id": "prefetch-20260718-0001",
            "prefetch": prefetch,
            "legacy_takeover": takeover,
            "previous_active_control": previous_control,
            "previous_active_control_sha256": (
                CONTROLLER.canonical_json_digest(previous_control)
            ),
            "executor_control": candidate,
            "executor_control_sha256": (
                CONTROLLER.canonical_json_digest(candidate)
            ),
            "created_at": CONTROLLER.utc_now(),
        }
        handoff = controller.control_handoffs_dir / f"{OPERATION_ID}.json"
        CONTROLLER.atomic_json(handoff, record)
        environment = {
            "NEXPOLY_PREPARE_HANDOFF_OPERATION": OPERATION_ID,
            "NEXPOLY_PREPARE_HANDOFF_SHA256": (
                CONTROLLER.sha256_file(handoff)
            ),
            "NEXPOLY_BRIDGE_AUTHORITY_SHA": authority_sha,
            "NEXPOLY_PREFETCH_OPERATION_ID": (
                "prefetch-20260718-0001"
            ),
        }
        manifest = {
            "compatibility": {
                "descriptor_schema_versions": [
                    CONTROLLER.BRIDGE_DESCRIPTOR_SCHEMA_VERSION
                ]
            }
        }
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(
                controller,
                "maintenance_prefetch_evidence",
                return_value=({}, prefetch),
            ),
            mock.patch.object(
                controller,
                "repository_identity",
                return_value={"fixture": True},
            ),
            mock.patch.object(
                controller,
                "completed_legacy_takeover_evidence",
                return_value=takeover,
            ),
            mock.patch.object(
                controller,
                "materialize_prefetched_bridge_relation",
                side_effect=AssertionError("pre-lock materialize"),
            ) as materialize,
            mock.patch.object(
                CONTROLLER._control_runtime,
                "load_candidate_control",
                return_value=(candidate, manifest, SCRIPT.parent),
            ),
        ):
            observed, relation = (
                controller._validate_bridge_prepare_handoff(
                    authority_sha=authority_sha,
                    operation_id=OPERATION_ID,
                    prefetch_operation_id="prefetch-20260718-0001",
                    materialize=False,
                )
            )
        self.assertEqual(observed, record)
        self.assertIsNone(relation)
        materialize.assert_not_called()

    def test_bridge_ci_comes_only_from_sealed_bootstrap_publication(
        self,
    ) -> None:
        controller = self.controller()
        expected = controller.bootstrap_ci_evidence(
            authority_sha=TARGET_SHA,
            required_jobs=CONTROLLER._bridge_core.REQUIRED_CI_JOBS,
        )
        self.assertEqual(expected["workflow_run_id"], 42)
        self.assertEqual(expected["head_sha"], TARGET_SHA)

        path = controller.state_dir / "bootstrap-control.json"
        changed = CONTROLLER.load_private_json(path)
        changed["delivery_gate"]["ci"]["required_jobs"] = ["ci-gate"]
        CONTROLLER.atomic_json(path, changed)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "exact required F CI jobs",
        ):
            controller.bootstrap_ci_evidence(
                authority_sha=TARGET_SHA,
                required_jobs=CONTROLLER._bridge_core.REQUIRED_CI_JOBS,
            )

    def test_ambient_test_mode_cannot_authorize_production_roots(self) -> None:
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "forbidden for production paths"
        ):
            CONTROLLER.PullDeployController(
                CONTROLLER.PRODUCTION_ROOT,
                self.runtime,
                runner=GitRunner(),
                apply=True,
            )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "forbidden for production paths"
        ):
            CONTROLLER.PullDeployController(
                self.production,
                CONTROLLER.RUNTIME_ROOT,
                runner=GitRunner(),
                apply=True,
            )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "forbidden for production paths"
        ):
            CONTROLLER.clean_control_environment(CONTROLLER.RUNTIME_ROOT)

    def test_plan_is_read_only_and_requires_requested_sha_to_equal_remote_main(
        self,
    ) -> None:
        runner = GitRunner()
        controller = CONTROLLER.PullDeployController(
            self.production, self.runtime, runner=runner, apply=False
        )
        plan = controller.plan(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self.assertEqual(plan["target_sha"], TARGET_SHA)
        self.assertFalse(plan["service_mutation"])
        self.assertFalse((self.runtime / "state/deploy-in-progress.json").exists())
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "not current remote main"
        ):
            controller.plan(target_sha="5" * 40, operation_id=OPERATION_ID)

    def test_ci_gate_binds_successful_main_push_and_all_required_jobs(self) -> None:
        runner = GithubRunner()
        controller = CONTROLLER.PullDeployController(
            self.production, self.runtime, runner=runner, apply=False
        )
        evidence = controller.ci_evidence(TARGET_SHA)
        self.assertEqual(evidence["head_sha"], TARGET_SHA)
        self.assertEqual(evidence["conclusion"], "success")
        self.assertEqual(len(runner.urls), 2)

    def test_ci_gate_rejects_each_missing_contract_job(self) -> None:
        for missing in sorted(CONTROLLER._bridge_core.REQUIRED_CI_JOBS):
            with self.subTest(missing=missing):
                runner = GithubRunner()
                original = runner.request_json

                def incomplete(
                    url: str,
                    token: str,
                    *,
                    omitted: str = missing,
                ):  # type: ignore[no-untyped-def]
                    payload = original(url, token)
                    if "/jobs?" in url:
                        return {
                            "jobs": [
                                {"name": name, "conclusion": "success"}
                                for name in sorted(
                                    CONTROLLER._bridge_core.REQUIRED_CI_JOBS
                                )
                                if name != omitted
                            ]
                        }
                    return payload

                runner.request_json = incomplete  # type: ignore[method-assign]
                controller = CONTROLLER.PullDeployController(
                    self.production,
                    self.runtime,
                    runner=runner,
                    apply=False,
                )
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "lacks a successful",
                ):
                    controller.ci_evidence(TARGET_SHA)

    def test_image_gate_resolves_digest_and_rejects_wrong_revision(self) -> None:
        runner = ImageRunner()
        controller = CONTROLLER.PullDeployController(
            self.production, self.runtime, runner=runner, apply=False
        )
        evidence = controller.image_evidence("backend", TARGET_SHA)
        self.assertEqual(
            evidence["digest_ref"], f"{CONTROLLER.BACKEND_TAG_ROOT}@{DIGEST_A}"
        )
        self.assertEqual(
            runner.commands[-2], ["docker", "pull", evidence["digest_ref"]]
        )
        self.assertEqual(
            runner.commands[-1],
            ["docker", "image", "inspect", evidence["digest_ref"]],
        )

        controller.runner = ImageRunner(wrong_revision=True)
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "OCI identity"):
            controller.image_evidence("backend", TARGET_SHA)


class EphemeralContainerOwnershipTests(unittest.TestCase):
    @staticmethod
    def web_record(operation_id: str) -> dict[str, object]:
        name = f"nexpoly-web-smoke-{operation_id}-{TARGET_SHA[:12]}"
        return {
            "Id": "1" * 64,
            "Name": f"/{name}",
            "Config": {
                "Image": f"example.invalid/web@{DIGEST_A}",
                "Labels": {"com.nexpoly.deploy-operation": operation_id},
                "Env": [],
            },
            "HostConfig": {
                "NetworkMode": "none",
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                "AutoRemove": False,
                "Privileged": False,
                "PublishAllPorts": False,
            },
            "NetworkSettings": {
                "Ports": {"80/tcp": None},
                "Networks": {
                    "none": {
                        "Gateway": "",
                        "IPAddress": "",
                        "IPPrefixLen": 0,
                        "IPv6Gateway": "",
                        "GlobalIPv6Address": "",
                        "GlobalIPv6PrefixLen": 0,
                    }
                },
            },
            "Mounts": [],
        }

    def test_web_run_and_remove_unknown_commit_are_proven_by_inspection(self) -> None:
        record = self.web_record(OPERATION_ID)
        commands: list[list[str]] = []
        inspect_count = 0

        def run(command, **_kwargs):  # type: ignore[no-untyped-def]
            nonlocal inspect_count
            commands.append(command)
            if command[:3] == ["docker", "container", "inspect"]:
                inspect_count += 1
                if inspect_count == 1:
                    return SimpleNamespace(returncode=1, stdout="")
                if inspect_count == 2:
                    return SimpleNamespace(returncode=0, stdout=json.dumps([record]))
                return SimpleNamespace(returncode=1, stdout="")
            if command[:2] == ["docker", "run"]:
                raise OSError("response lost after committed run")
            if command[:3] == ["docker", "exec", record["Name"][1:]]:
                if command[-1] == "http://127.0.0.1/":
                    return SimpleNamespace(
                        returncode=0,
                        stdout='<script src="/assets/app-abcdef12.js"></script>',
                    )
                return SimpleNamespace(returncode=0, stdout=b"asset")
            if command[:3] == ["docker", "rm", "--force"]:
                raise OSError("response lost after committed removal")
            if command[:4] == ["docker", "container", "ls", "--all"]:
                return SimpleNamespace(returncode=0, stdout="")
            raise AssertionError(command)

        controller = SimpleNamespace(
            runner=SimpleNamespace(run=run),
            control_environment=lambda: {},
        )
        descriptor = {
            "operation_id": OPERATION_ID,
            "repository": {"target_sha": TARGET_SHA},
            "images": {"web": {"digest_ref": f"example.invalid/web@{DIGEST_A}"}},
        }
        evidence = CONTROLLER.SystemLifecycle()._verify_web_image(
            controller, descriptor
        )
        self.assertEqual(evidence["image"], descriptor["images"]["web"]["digest_ref"])
        self.assertEqual(inspect_count, 2)

    def test_same_sha_different_operation_and_extra_resources_are_foreign(self) -> None:
        record = self.web_record("deploy-20260716-another-operation")
        record["Name"] = f"/nexpoly-web-smoke-{OPERATION_ID}-{TARGET_SHA[:12]}"
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "operation"):
            CONTROLLER.SystemLifecycle._validate_isolated_container(
                record,
                name=f"nexpoly-web-smoke-{OPERATION_ID}-{TARGET_SHA[:12]}",
                image=f"example.invalid/web@{DIGEST_A}",
                operation_label="com.nexpoly.deploy-operation",
                operation_id=OPERATION_ID,
                tmpfs_capacity=None,
            )
        record = self.web_record(OPERATION_ID)
        record["HostConfig"]["PortBindings"] = {"80/tcp": [{"HostPort": "8080"}]}
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "host resources"):
            CONTROLLER.SystemLifecycle._validate_isolated_container(
                record,
                name=f"nexpoly-web-smoke-{OPERATION_ID}-{TARGET_SHA[:12]}",
                image=f"example.invalid/web@{DIGEST_A}",
                operation_label="com.nexpoly.deploy-operation",
                operation_id=OPERATION_ID,
                tmpfs_capacity=None,
            )


class PostgresRuntimeFencingTests(unittest.TestCase):
    def test_docker_exec_requires_an_exact_container_id(self) -> None:
        container_id = "a" * 64
        self.assertEqual(
            CONTROLLER.SystemLifecycle._docker_exec(
                container_id,
                "pg_restore",
                interactive=True,
            ),
            [
                "docker",
                "exec",
                "--interactive",
                container_id,
                "pg_restore",
            ],
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "exact container ID",
        ):
            CONTROLLER.SystemLifecycle._docker_exec(
                "lab-postgres",
                "dropdb",
            )

    class Runner:
        def __init__(self, *, replace_on_up: bool = False) -> None:
            self.container_id = "a" * 64
            self.replace_on_up = replace_on_up
            self.commands: list[list[str]] = []

        def _container(self) -> dict[str, object]:
            return {
                "Id": self.container_id,
                "Image": "sha256:" + "b" * 64,
                "Config": {
                    "Image": "postgres:16-alpine",
                    "Labels": {
                        "com.docker.compose.project": "nexpoly",
                        "com.docker.compose.service": "lab-postgres",
                    },
                },
                "State": {"Running": True},
                "NetworkSettings": {
                    "Ports": {
                        "5432/tcp": [
                            {"HostIp": "127.0.0.1", "HostPort": "55432"}
                        ]
                    }
                },
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": "nexpoly_postgres_data",
                        "Source": "/var/lib/docker/volumes/nexpoly_postgres_data/_data",
                        "Destination": "/var/lib/postgresql/data",
                        "Driver": "local",
                        "RW": True,
                    }
                ],
            }

        def run(
            self, command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            self.commands.append(command)
            output = ""
            returncode = 0
            if (
                command[0:3] == ["docker", "container", "inspect"]
                and len(command) == 4
            ):
                output = json.dumps([self._container()])
            elif command[:3] == ["docker", "inspect", "--format"]:
                output = "true\n"
            elif command[:2] == ["docker", "compose"]:
                if "ps" in command and command[-1] == "lab-postgres":
                    output = self.container_id + "\n"
                elif "ps" in command:
                    output = ""
                elif any("pg_control_system()" in value for value in command):
                    output = "7659245354718314530\n"
                elif "up" in command and self.replace_on_up:
                    self.container_id = "c" * 64
            elif command[:3] == ["systemctl", "--user", "is-active"]:
                output = "inactive\n"
                returncode = 3
            elif command[:3] == ["systemctl", "--user", "show"]:
                output = "ActiveState=inactive\nMainPID=0\n"
            elif command[:2] == ["systemctl", "is-active"]:
                output = "inactive\n"
                returncode = 3
            return subprocess.CompletedProcess(command, returncode, output, "")

        def request_json(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            raise AssertionError("unexpected network request")

    class Lifecycle(CONTROLLER.SystemLifecycle):
        def _drain_started_runtime(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {"fixture": True}

        @staticmethod
        def _assert_no_checkout_readers(_production_root: Path) -> None:
            return

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="postgres-fence-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.production = self.root / "production"
        self.runtime = self.root / "runtime"
        self.config = self.runtime / "config"
        self.state = self.runtime / "state"
        for path in (self.production, self.runtime, self.config, self.state):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        self.descriptor = {
            "images": {
                "backend": {"digest_ref": "example.invalid/backend@" + DIGEST_A},
                "web": {"digest_ref": "example.invalid/web@" + DIGEST_B},
            }
        }

    def controller(self, runner: object) -> object:
        marker_path = self.state / "deploy-in-progress.json"
        return SimpleNamespace(
            runner=runner,
            production_root=self.production,
            runtime_root=self.runtime,
            config_dir=self.config,
            state_dir=self.state,
            marker_path=marker_path,
            control_environment=lambda: {},
            production_deploy_values=lambda **_kwargs: {
                "NEXPOLY_POSTGRES_USER": "nexpoly",
                "NEXPOLY_POSTGRES_DB": "nexpoly",
            },
        )

    def test_stop_and_start_preserve_exact_container_volume_and_system_id(self) -> None:
        runner = self.Runner()
        controller = self.controller(runner)
        lifecycle = self.Lifecycle()
        fence = lifecycle.stop(controller, self.descriptor)
        self.assertEqual(fence["container_id"], "a" * 64)
        self.assertEqual(fence["data_volume"]["name"], "nexpoly_postgres_data")
        self.assertEqual(fence["system_identifier"], "7659245354718314530")
        CONTROLLER.atomic_json(
            controller.marker_path,
            {
                "runtime_stopped": True,
                "postgres_runtime_fence": fence,
            },
        )
        lifecycle.start(controller, self.descriptor)
        up = next(command for command in runner.commands if "up" in command)
        self.assertIn("--no-deps", up)
        self.assertIn("backend", up)
        self.assertNotIn("lab-postgres", up)
        self.assertFalse(
            any(
                "stop" in command and "lab-postgres" in command
                for command in runner.commands
            )
        )

    def test_start_fails_closed_if_compose_replaces_postgres(self) -> None:
        runner = self.Runner(replace_on_up=True)
        controller = self.controller(runner)
        lifecycle = self.Lifecycle()
        fence = lifecycle.postgres_runtime_identity(controller, self.descriptor)
        CONTROLLER.atomic_json(
            controller.marker_path,
            {
                "runtime_stopped": True,
                "postgres_runtime_fence": fence,
            },
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "changed during application start",
        ):
            lifecycle.start(controller, self.descriptor)

    def test_fence_rejects_bind_or_writable_identity_drift(self) -> None:
        fence = {
            "schema_version": 1,
            "container_id": "a" * 64,
            "image_id": "sha256:" + "b" * 64,
            "configured_image": "postgres:16-alpine",
            "data_volume": {
                "type": "bind",
                "name": "foreign",
                "source": "/tmp/foreign",
                "destination": "/var/lib/postgresql/data",
                "driver": "local",
                "read_write": True,
            },
            "host_endpoint": {
                "host": "127.0.0.1",
                "port": 55432,
                "container_port": 5432,
                "protocol": "tcp",
            },
            "system_identifier": "7659245354718314530",
            "captured_at": CONTROLLER.utc_now(),
        }
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "data-volume fence",
        ):
            CONTROLLER.validate_postgres_runtime_fence(fence)


class SlotAndDescriptorTests(PullDeployTestCase):
    def test_bridge_prepare_consumes_complete_dual_postgres_prefetch_contract(
        self,
    ) -> None:
        local_ids = {
            major: "sha256:" + f"{index:x}" * 64
            for index, major in enumerate(
                sorted(CONTROLLER._prefetch_evidence.POSTGRES_AUDIT_IMAGES),
                start=5,
            )
        }

        def postgres_record(major: str) -> dict[str, object]:
            reference = (
                CONTROLLER._prefetch_evidence.POSTGRES_AUDIT_IMAGES[major]
            )
            return {
                "digest_ref": reference,
                "oci_reference_digest": reference.split("@", 1)[1],
                "local_image_id": local_ids[major],
                "repo_digests": [
                    CONTROLLER._prefetch_evidence.canonical_repo_digest(
                        reference
                    )
                ],
                "revision": None,
                "source": None,
                "version": None,
            }

        postgres_audit = {
            major: postgres_record(major)
            for major in sorted(
                CONTROLLER._prefetch_evidence.POSTGRES_AUDIT_IMAGES
            )
        }
        ready = {
            "images": {
                "postgres_audit": postgres_audit,
                "postgres_restore": dict(postgres_audit["16"]),
            }
        }

        class RestoreRunner:
            def run(
                self,
                command: list[str],
                **_kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                if command != [
                    "docker",
                    "image",
                    "inspect",
                    CONTROLLER.POSTGRES16_IMAGE,
                ]:
                    raise AssertionError(command)
                record = postgres_audit["16"]
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        [
                            {
                                "Id": record["local_image_id"],
                                "RepoDigests": record["repo_digests"],
                                "Config": {"Labels": {}},
                            }
                        ]
                    ),
                    "",
                )

        controller = self.controller()
        controller.runner = RestoreRunner()
        self.assertEqual(
            controller.prefetched_postgres_restore_image(ready),
            {
                "digest_ref": CONTROLLER.POSTGRES16_IMAGE,
                "image_id": local_ids["16"],
            },
        )

        incomplete = json.loads(json.dumps(ready))
        incomplete["images"]["postgres_audit"].pop("18")
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "audit image set is incomplete",
        ):
            controller.prefetched_postgres_restore_image(incomplete)

        divergent = json.loads(json.dumps(ready))
        divergent["images"]["postgres_restore"]["local_image_id"] = (
            "sha256:" + "f" * 64
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "not the exact PostgreSQL 16 audit image",
        ):
            controller.prefetched_postgres_restore_image(divergent)

    def test_prepare_resumes_existing_descriptor_without_rebuilding_it(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        _operation, descriptor_path, ready_path = (
            controller._operation_paths(OPERATION_ID)
        )
        descriptor_payload = descriptor_path.read_bytes()
        ready_path.unlink()
        result = controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(descriptor_path.read_bytes(), descriptor_payload)
        descriptor = CONTROLLER.validate_descriptor(
            CONTROLLER.load_private_json(descriptor_path)
        )
        controller._validate_ready(
            CONTROLLER.load_private_json(ready_path),
            descriptor,
            descriptor_path,
        )

    def test_bridge_descriptor_cannot_be_resumed_in_ordinary_mode(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        controller.remote_main = lambda: descriptor["bridge"]["authority"][  # type: ignore[method-assign]
            "sha"
        ]
        _operation, descriptor_path, ready_path = (
            controller._operation_paths(OPERATION_ID)
        )
        ordinary_ready = CONTROLLER.load_private_json(ready_path)
        CONTROLLER.atomic_json(descriptor_path, descriptor)
        ready_path.unlink()
        CONTROLLER.fsync_directory(ready_path.parent)
        operation_tree_before = {
            path.relative_to(descriptor_path.parent).as_posix(): path.read_bytes()
            for path in descriptor_path.parent.rglob("*")
            if path.is_file() and not path.is_symlink()
        }

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "original ordinary or bridge command mode",
        ):
            controller.prepare(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )
        self.assertFalse(ready_path.exists())
        self.assertFalse(ready_path.is_symlink())
        self.assertEqual(
            {
                path.relative_to(descriptor_path.parent).as_posix(): path.read_bytes()
                for path in descriptor_path.parent.rglob("*")
                if path.is_file() and not path.is_symlink()
            },
            operation_tree_before,
        )

        poisoned_ready = {
            **ordinary_ready,
            "descriptor_sha256": CONTROLLER.sha256_file(
                descriptor_path
            ),
        }
        CONTROLLER.atomic_json(ready_path, poisoned_ready)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "original ordinary or bridge command mode",
        ):
            controller.prepare(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )

    def test_ordinary_descriptor_rejects_bridge_before_materialization(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        operation, _descriptor_path, _ready_path = (
            controller._operation_paths(OPERATION_ID)
        )
        operation_tree_before = {
            path.relative_to(operation).as_posix(): path.read_bytes()
            for path in operation.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        token_path = controller.state_dir / "bridge-takeover-token.json"
        token_before = (
            token_path.read_bytes()
            if token_path.exists() and not token_path.is_symlink()
            else None
        )
        prefetch_ready = {
            "source": {"target": {"sha": TARGET_SHA}},
        }
        controller.maintenance_prefetch_evidence = mock.Mock(  # type: ignore[method-assign]
            return_value=(prefetch_ready, {"fixture": True})
        )
        controller.materialize_prefetched_bridge_relation = mock.Mock(  # type: ignore[method-assign]
            side_effect=AssertionError("mode fence ran after materialization")
        )

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "original ordinary or bridge command mode",
        ):
            controller.prepare(
                target_sha=None,
                operation_id=OPERATION_ID,
                bridge_authority_sha="5" * 40,
                prefetch_operation_id="prefetch-fixture-mode-fence",
            )

        controller.materialize_prefetched_bridge_relation.assert_not_called()
        self.assertEqual(
            {
                path.relative_to(operation).as_posix(): path.read_bytes()
                for path in operation.rglob("*")
                if path.is_file() and not path.is_symlink()
            },
            operation_tree_before,
        )
        self.assertEqual(
            (
                token_path.read_bytes()
                if token_path.exists() and not token_path.is_symlink()
                else None
            ),
            token_before,
        )

    def test_existing_bridge_ready_rebinds_reserved_token(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        controller.remote_main = lambda: descriptor["bridge"]["authority"][  # type: ignore[method-assign]
            "sha"
        ]
        _operation, descriptor_path, _ready_path = (
            controller._operation_paths(OPERATION_ID)
        )
        policy_id = descriptor["bridge"]["policy"]["policy_id"]
        with controller.deployment_lock():
            token = CONTROLLER._bridge_core.reserve_token(
                controller.state_dir,
                operation_id=OPERATION_ID,
                policy_id=policy_id,
            )
            descriptor["bridge"]["token"] = {
                "token_id": token["token_id"],
                "token_sha256": token["token_sha256"],
            }
            # A response-lost reservation is the timestamp authority too;
            # production prepare copies this value into the descriptor.
            descriptor["prepared_at"] = token["prepared_at"]
            CONTROLLER.atomic_json(descriptor_path, descriptor)
            bound = controller._bind_bridge_descriptor_token(
                descriptor,
                descriptor_path,
            )
        self.assertEqual(bound["status"], "prepared")
        self.assertEqual(
            bound["descriptor_sha256"],
            CONTROLLER.sha256_file(descriptor_path),
        )
        self.assertEqual(
            CONTROLLER._bridge_core.load_token_authority(
                controller.state_dir
            ),
            bound,
        )

    def test_descriptor_first_bridge_publishes_prepared_token_directly(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        controller.remote_main = lambda: descriptor["bridge"]["authority"][  # type: ignore[method-assign]
            "sha"
        ]
        _operation, descriptor_path, ready_path = (
            controller._operation_paths(OPERATION_ID)
        )
        ready_path.unlink()
        CONTROLLER.atomic_json(descriptor_path, descriptor)
        token_path = (
            controller.state_dir
            / CONTROLLER._bridge_core.TOKEN_RELATIVE_PATH
        )
        self.assertFalse(token_path.exists())
        with controller.deployment_lock():
            prepared = controller._bind_bridge_descriptor_token(
                descriptor,
                descriptor_path,
            )
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(
            prepared["descriptor_sha256"],
            CONTROLLER.sha256_file(descriptor_path),
        )
        self.assertEqual(
            prepared["token_id"],
            descriptor["bridge"]["token"]["token_id"],
        )

    def test_lost_prepared_token_write_response_resumes_without_remote_f(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        controller.remote_main = lambda: descriptor["bridge"]["authority"][  # type: ignore[method-assign]
            "sha"
        ]
        _operation, descriptor_path, ready_path = (
            controller._operation_paths(OPERATION_ID)
        )
        ready_path.unlink()
        CONTROLLER.atomic_json(descriptor_path, descriptor)
        original = CONTROLLER._bridge_core._atomic_json
        lost = False

        def lose_response(path, value):  # type: ignore[no-untyped-def]
            nonlocal lost
            result = original(path, value)
            if (
                not lost
                and Path(path).name
                == CONTROLLER._bridge_core.TOKEN_RELATIVE_PATH.name
                and value.get("status") == "prepared"
            ):
                lost = True
                raise OSError("injected token write response loss")
            return result

        with (
            controller.deployment_lock(),
            mock.patch.object(
                CONTROLLER._bridge_core,
                "_atomic_json",
                side_effect=lose_response,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "could not bind descriptor",
            ),
        ):
            controller._bind_bridge_descriptor_token(
                descriptor,
                descriptor_path,
            )
        durable = CONTROLLER._bridge_core.load_token_authority(
            controller.state_dir
        )
        self.assertEqual(durable["status"], "prepared")
        controller.remote_main = lambda: "f" * 40  # type: ignore[method-assign]
        with controller.deployment_lock():
            resumed = controller._bind_bridge_descriptor_token(
                descriptor,
                descriptor_path,
            )
        self.assertEqual(resumed, durable)

    def test_descriptor_only_remote_drift_cannot_create_first_token(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        _operation, descriptor_path, ready_path = (
            controller._operation_paths(OPERATION_ID)
        )
        ready_path.unlink()
        CONTROLLER.atomic_json(descriptor_path, descriptor)
        controller.remote_main = lambda: "f" * 40  # type: ignore[method-assign]
        token_path = (
            controller.state_dir
            / CONTROLLER._bridge_core.TOKEN_RELATIVE_PATH
        )

        with (
            controller.deployment_lock(),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "changed before prepared token publication",
            ),
        ):
            controller._bind_bridge_descriptor_token(
                descriptor,
                descriptor_path,
            )

        self.assertFalse(token_path.exists())
        self.assertFalse(ready_path.exists())

    def test_legacy_reserved_plan_replays_after_remote_drift(
        self,
    ) -> None:
        controller = self.controller()
        authority_sha = "5" * 40
        policy_id = "sha256:" + "a" * 64
        with controller.deployment_lock():
            reserved = CONTROLLER._bridge_core.reserve_token(
                controller.state_dir,
                operation_id=OPERATION_ID,
                policy_id=policy_id,
                token=b"x" * 32,
            )
        controller.remote_main = lambda: "f" * 40  # type: ignore[method-assign]
        with controller.deployment_lock():
            plan = controller._plan_current_bridge_token(
                authority_sha=authority_sha,
                operation_id=OPERATION_ID,
                policy_id=policy_id,
            )
        self.assertEqual(plan["token_id"], reserved["token_id"])
        self.assertEqual(
            plan["token_sha256"],
            reserved["token_sha256"],
        )
        self.assertEqual(plan["prepared_at"], reserved["prepared_at"])

    def test_token_plan_is_memory_only_until_descriptor_publication(
        self,
    ) -> None:
        controller = self.controller()
        authority_sha = "5" * 40
        controller.remote_main = lambda: authority_sha  # type: ignore[method-assign]
        token_path = (
            controller.state_dir
            / CONTROLLER._bridge_core.TOKEN_RELATIVE_PATH
        )
        with controller.deployment_lock():
            plan = controller._plan_current_bridge_token(
                authority_sha=authority_sha,
                operation_id=OPERATION_ID,
                policy_id="sha256:" + "a" * 64,
            )
        self.assertRegex(plan["token_id"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(plan["token_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertFalse(token_path.exists())
        self.assertFalse(token_path.is_symlink())

    def test_pull_uses_shared_strict_schema_v2_asset_contract(self) -> None:
        root = (
            CONTROLLER.ASSET_RELEASES_ROOT
            / CONTROLLER.SCHEMA_V2_ASSET_MANIFEST_DIGEST.removeprefix(
                "sha256:"
            )
        )
        evidence = {
            "root": str(root),
            "manifest_sha256": CONTROLLER.SCHEMA_V2_ASSET_MANIFEST_DIGEST,
            "schema_version": 2,
            "byteff2_commit": "8" * 40,
            "inventory_sha256": "sha256:" + "9" * 64,
        }
        with mock.patch.object(
            CONTROLLER._asset_release_contract,
            "validate_schema_v2_release",
            return_value=evidence,
        ) as validator:
            self.assertEqual(
                CONTROLLER.inspect_asset_release(
                    root,
                    CONTROLLER.SCHEMA_V2_ASSET_MANIFEST_DIGEST,
                ),
                evidence,
            )
        validator.assert_called_once_with(
            root,
            expected_digest=CONTROLLER.SCHEMA_V2_ASSET_MANIFEST_DIGEST,
            releases_root=CONTROLLER.ASSET_RELEASES_ROOT,
        )

        with (
            mock.patch.object(
                CONTROLLER._asset_release_contract,
                "validate_schema_v2_release",
                side_effect=CONTROLLER._asset_release_contract.AssetContractError(
                    "tampered predecessor"
                ),
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "strict external schema-v2",
            ),
        ):
            CONTROLLER.inspect_asset_release(
                root,
                CONTROLLER.SCHEMA_V2_ASSET_MANIFEST_DIGEST,
            )

    def test_alias_gate_allows_preparation_only_before_reconciliation_starts(
        self,
    ) -> None:
        controller = self.controller()
        marker_path = (
            controller.runtime_root
            / CONTROLLER._control_runtime.ALIAS_MARKER_RELATIVE
        )
        completed = CONTROLLER.load_private_json(marker_path)
        marker_path.unlink()
        CONTROLLER.fsync_directory(marker_path.parent)

        controller._require_no_contract_maintenance(
            require_alias_completed=False
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "reconciliation is required"
        ):
            controller._require_no_contract_maintenance()

        CONTROLLER.atomic_json(marker_path, {**completed, "phase": "planned"})
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "must recover first"
        ):
            controller._require_no_contract_maintenance(
                require_alias_completed=False
            )

    def bridge_descriptor(
        self, controller: FixtureController
    ) -> dict[str, object]:
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready_path = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        authority_sha = "5" * 40
        authority_tree = "6" * 40
        previous_control = dict(
            descriptor["controller"]["previous_active_control"]
        )
        previous_control.update(
            {
                "release_id": "7" * 64,
                "source_sha": authority_sha,
                "source_tree": authority_tree,
            }
        )
        descriptor["controller"]["previous_active_control"] = previous_control
        descriptor["controller"][
            "previous_active_control_sha256"
        ] = CONTROLLER.canonical_json_digest(previous_control)
        required_jobs = sorted(CONTROLLER._bridge_core.REQUIRED_CI_JOBS)
        descriptor["ci"] = {
            "workflow_run_id": 99,
            "run_attempt": 1,
            "head_sha": authority_sha,
            "head_branch": "main",
            "event": "push",
            "path": ".github/workflows/ci.yml",
            "conclusion": "success",
            "required_jobs": required_jobs,
        }
        external_database_audit = external_database_audit_binding(
            controller.runtime_root
        )
        policy = {
            "schema_version": (
                CONTROLLER._bridge_core.POLICY_SCHEMA_VERSION
            ),
            "mode": CONTROLLER._bridge_core.BRIDGE_MODE,
            "authority_ref": CONTROLLER._bridge_core.AUTHORITY_REF,
            "target_sha": TARGET_SHA,
            "target_tree": TARGET_TREE,
            "target_ref": f"refs/nexpoly/bridge-target/{TARGET_SHA}",
            "target_images": {
                role: descriptor["images"][role]["digest_ref"]
                for role in ("backend", "web")
            },
            "asset_manifest_digest": descriptor["release_input"][
                "asset_manifest_digest"
            ],
            "datasets_on_asset_change": [],
            "final_migration": dict(CONTROLLER._bridge_core.FINAL_MIGRATION),
            "accepted_migration_ledgers": (
                CONTROLLER._bridge_core.expected_migration_registry(
                    target_manifest_sha256=B_MANIFEST_DIGEST,
                    target_records=B_MANIFEST_RECORDS,
                    authority_manifest_sha256=F_MANIFEST_DIGEST,
                    authority_records=F_MANIFEST_RECORDS,
                )
            ),
            "external_database_audit": {
                **CONTROLLER._bridge_core.EXTERNAL_DATABASE_AUDIT_POLICY,
                "media_authority_rules_sha256": external_database_audit[
                    "authority_rules"
                ]["sha256"],
                "audit_role_sql_sha256": external_database_audit[
                    "role_sql"
                ]["sha256"],
            },
            "required_ci_jobs": required_jobs,
            "policy_id": None,
        }
        policy["policy_id"] = CONTROLLER._bridge_core.canonical_json_digest(
            {key: value for key, value in policy.items() if key != "policy_id"}
        )
        descriptor["migrations"] = {
            "sha256": B_MANIFEST_DIGEST,
            "schema_version": 2,
            "records": json.loads(json.dumps(B_MANIFEST_RECORDS)),
        }
        descriptor["monomer_md"]["worker_env"] = descriptor["monomer_md"][
            "worker_env"
        ]["previous"]
        descriptor.pop("monomer_dft")
        descriptor.pop("adopted_deployment")
        descriptor.pop("adopted_deployment_sha256")
        descriptor["schema_version"] = CONTROLLER.BRIDGE_DESCRIPTOR_SCHEMA_VERSION
        descriptor["bridge"] = CONTROLLER._bridge_core.build_bridge_descriptor(
            operation_id=OPERATION_ID,
            authority_sha=authority_sha,
            authority_tree=authority_tree,
            authority_control_release_id=previous_control["release_id"],
            ci_evidence=descriptor["ci"],
            target_control_release_id=descriptor["controller"][
                "executor_control"
            ]["release_id"],
            policy=policy,
            token_id="sha256:" + "8" * 64,
            token_sha256="sha256:" + "9" * 64,
        )
        takeover = {
            "schema_version": 1,
            "operation_id": "takeover-fixture-operation",
            "authority_sha": authority_sha,
            "authority_tree": authority_tree,
            "install_manifest_sha256": "sha256:" + "a" * 64,
            "classification_sha256": "sha256:" + "b" * 64,
            "runtime_identity_sha256": "sha256:" + "c" * 64,
            "git_identity": {
                "branch": "refs/heads/main",
                "head_sha": descriptor["repository"]["previous_sha"],
                "head_tree": descriptor["repository"]["previous_tree"],
                "local_main_sha": descriptor["repository"]["previous_sha"],
            },
            "pre_stopped_fence_sha256": "sha256:" + "d" * 64,
            "control_layout_sha256": "sha256:" + "e" * 64,
            "checkout_permissions_sha256": "sha256:" + "f" * 64,
            "applied_record_sha256": "sha256:" + "1" * 64,
        }
        takeover["binding_sha256"] = CONTROLLER.canonical_json_digest(
            takeover
        )
        descriptor["legacy_takeover"] = takeover
        prefetch = {
            "schema_version": 2,
            "operation_id": "prefetch-fixture-operation",
            "ready_path": str(
                controller.runtime_root
                / "prefetch/prefetch-fixture-operation/ready.json"
            ),
            "ready_sha256": "sha256:" + "2" * 64,
            "identity_sha256": "sha256:" + "3" * 64,
            "source": {
                "authority": {
                    "sha": authority_sha,
                    "tree": authority_tree,
                },
                "target": {
                    "sha": TARGET_SHA,
                    "tree": TARGET_TREE,
                },
            },
            "source_readiness_sha256": "sha256:" + "4" * 64,
            "controller_sha256": "sha256:" + "b" * 64,
            "policy_sha256": descriptor["bridge"]["policy_sha256"],
            "docker_config_path": str(
                controller.runtime_root / "config/docker"
            ),
            "git_bundle_sha256": "sha256:" + "6" * 64,
            "images_sha256": "sha256:" + "7" * 64,
            "wheel_caches_sha256": "sha256:" + "8" * 64,
            "asset_manifest_sha256": descriptor["release_input"][
                "asset_manifest_digest"
            ],
            "asset_inventory_sha256": "sha256:" + "9" * 64,
            "asset_contract_sha256": "sha256:" + "c" * 64,
            "asset_builder_proof_sha256": "sha256:" + "d" * 64,
            "asset_predecessor_inventory_sha256": "sha256:" + "e" * 64,
            "live_asset_pointer_sha256": "sha256:" + "f" * 64,
            "recovery_tools_sha256": "sha256:" + "a" * 64,
            "created_at": "2026-07-17T00:00:00Z",
        }
        prefetch["binding_sha256"] = CONTROLLER.canonical_json_digest(
            prefetch
        )
        descriptor["prefetch"] = prefetch
        descriptor["external_database_audit"] = external_database_audit
        return descriptor

    def bind_bridge_token(
        self,
        controller: FixtureController,
        descriptor: dict[str, object],
    ) -> str:
        bridge = descriptor["bridge"]
        token = CONTROLLER._bridge_core.reserve_token(
            controller.state_dir,
            operation_id=OPERATION_ID,
            policy_id=bridge["policy"]["policy_id"],
            token=b"bridge-token-fixture-entropy-0001",
        )
        bridge["token"] = {
            "token_id": token["token_id"],
            "token_sha256": token["token_sha256"],
        }
        CONTROLLER.validate_descriptor(descriptor)
        descriptor_digest = CONTROLLER.sha256_bytes(
            CONTROLLER.canonical_json_bytes(descriptor) + b"\n"
        )
        CONTROLLER._bridge_core.bind_token_descriptor(
            controller.state_dir,
            operation_id=OPERATION_ID,
            policy_id=bridge["policy"]["policy_id"],
            descriptor_sha256=descriptor_digest,
        )
        return descriptor_digest

    def bind_bridge_recovery_capsule(
        self,
        controller: FixtureController,
        descriptor: dict[str, object],
        descriptor_digest: str,
        marker: dict[str, object],
    ) -> dict[str, object]:
        with controller.deployment_lock():
            capsule = controller._prepare_bridge_recovery_capsule(
                descriptor, descriptor_digest
            )
        marker["bridge_recovery_capsule"] = (
            controller._bridge_recovery_capsule_binding(capsule)
        )
        return capsule

    def test_bridge_recovery_capsule_publish_is_crash_idempotent_and_tamper_evident(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        descriptor_digest = self.bind_bridge_token(controller, descriptor)
        original_rename = CONTROLLER.os.rename

        def publish_then_lose_response(source, target):  # type: ignore[no-untyped-def]
            original_rename(source, target)
            raise RuntimeError("injected capsule rename response loss")

        with (
            mock.patch.object(
                CONTROLLER.os,
                "rename",
                side_effect=publish_then_lose_response,
            ),
            self.assertRaisesRegex(RuntimeError, "rename response loss"),
            controller.deployment_lock(),
        ):
            controller._prepare_bridge_recovery_capsule(
                descriptor, descriptor_digest
            )
        with controller.deployment_lock():
            capsule = controller._prepare_bridge_recovery_capsule(
                descriptor, descriptor_digest
            )
        observed, observed_descriptor = (
            controller._load_bridge_recovery_capsule(
                capsule["capsule_sha256"]
            )
        )
        self.assertEqual(observed, capsule)
        self.assertEqual(observed_descriptor, descriptor)

        root = (
            controller.runtime_root
            / CONTROLLER.BRIDGE_RECOVERY_CAPSULE_ROOT_RELATIVE
            / capsule["capsule_sha256"].removeprefix("sha256:")
        )
        entry = root / "control/bridge_recovery_capsule.py"
        entry.write_bytes(entry.read_bytes() + b"# tampered\n")
        os.chmod(entry, 0o700)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "file changed"
        ):
            controller._load_bridge_recovery_capsule(
                capsule["capsule_sha256"]
            )

    def test_v3_descriptor_binds_f_authority_exact_b_and_empty_datasets(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        self.assertEqual(CONTROLLER.validate_descriptor(descriptor), descriptor)

        mutations = (
            (
                "authority control",
                lambda value: value["bridge"]["authority"].__setitem__(
                    "control_release_id", "a" * 64
                ),
            ),
            (
                "authority CI",
                lambda value: value["ci"].__setitem__("workflow_run_id", 100),
            ),
            (
                "target ref",
                lambda value: value["bridge"]["target"].__setitem__(
                    "exact_ref", f"refs/nexpoly/bridge-target/{'a' * 40}"
                ),
            ),
            (
                "dataset rebuild",
                lambda value: value["release_input"][
                    "datasets_on_asset_change"
                ].append("online"),
            ),
            (
                "external registry",
                lambda value: value["external_database_audit"][
                    "registry"
                ].__setitem__("sha256", "sha256:" + "f" * 64),
            ),
            (
                "external snapshot",
                lambda value: value["external_database_audit"].__setitem__(
                    "snapshot_sha256", "sha256:" + "f" * 64
                ),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                changed = json.loads(json.dumps(descriptor))
                mutate(changed)
                with self.assertRaises(CONTROLLER.PullDeployError):
                    CONTROLLER.validate_descriptor(changed)

    def test_alias_bridge_projection_is_content_addressed_and_tamper_evident(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        _operation, descriptor_path, ready_path = (
            controller._operation_paths(OPERATION_ID)
        )
        CONTROLLER.atomic_json(descriptor_path, descriptor)
        ready = CONTROLLER.load_private_json(ready_path)
        ready["descriptor_sha256"] = CONTROLLER.sha256_file(
            descriptor_path
        )
        CONTROLLER.atomic_json(ready_path, ready)

        authority = CONTROLLER.alias_bridge_authority_projection(
            descriptor,
            descriptor_path=descriptor_path,
            ready_path=ready_path,
        )
        self.assertEqual(
            CONTROLLER.validate_alias_bridge_authority(
                authority,
                descriptor=descriptor,
                descriptor_path=descriptor_path,
                ready_path=ready_path,
            ),
            authority,
        )
        changed = json.loads(json.dumps(authority))
        changed["target"]["sha"] = "f" * 40
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "another bridge",
        ):
            CONTROLLER.validate_alias_bridge_authority(
                changed,
                descriptor=descriptor,
                descriptor_path=descriptor_path,
                ready_path=ready_path,
            )

    def test_alias_bridge_projection_accepts_only_proven_restored_successor(
        self,
    ) -> None:
        controller = self.controller()
        original = self.bridge_descriptor(controller)
        original_digest = self.bind_bridge_token(controller, original)
        _operation, original_path, original_ready_path = (
            controller._operation_paths(OPERATION_ID)
        )
        CONTROLLER.atomic_json(original_path, original)
        self.assertEqual(
            CONTROLLER.sha256_file(original_path),
            original_digest,
        )
        original_ready = CONTROLLER.load_private_json(
            original_ready_path
        )
        original_ready["descriptor_sha256"] = original_digest
        CONTROLLER.atomic_json(original_ready_path, original_ready)
        alias_authority = (
            CONTROLLER.alias_bridge_authority_projection(
                original,
                descriptor_path=original_path,
                ready_path=original_ready_path,
            )
        )

        retired = CONTROLLER._bridge_core.retire_precommit_token(
            controller.state_dir,
            operation_id=OPERATION_ID,
            descriptor_sha256=original_digest,
            operation_state_sha256="sha256:" + "1" * 64,
            terminal_audit_sha256="sha256:" + "2" * 64,
            restored_terminal_sha256="sha256:" + "3" * 64,
            recovery_capsule_sha256="sha256:" + "4" * 64,
        )
        successor_operation = "bridge-20260717-successor"
        successor_token = CONTROLLER._bridge_core.reserve_token(
            controller.state_dir,
            operation_id=successor_operation,
            policy_id=original["bridge"]["policy"]["policy_id"],
            token=b"successor-token-fixture-entropy-01",
            predecessor_retirement=(
                CONTROLLER._bridge_core.retirement_reuse_authority(
                    retired
                )
            ),
        )
        successor = json.loads(json.dumps(original))
        successor["operation_id"] = successor_operation
        successor["prepared_at"] = successor_token["prepared_at"]
        executor = successor["controller"]["executor_control"]
        executor["operation_id"] = successor_operation
        successor["controller"][
            "executor_control_sha256"
        ] = CONTROLLER.canonical_json_digest(executor)
        slot = successor["monomer_md"]["slot_record"]
        slot["prepared_operation_id"] = successor_operation
        successor["monomer_md"][
            "slot_record_sha256"
        ] = CONTROLLER.worker_record_digest(slot)
        bridge = original["bridge"]
        successor["bridge"] = (
            CONTROLLER._bridge_core.build_bridge_descriptor(
                operation_id=successor_operation,
                authority_sha=bridge["authority"]["sha"],
                authority_tree=bridge["authority"]["tree"],
                authority_control_release_id=bridge["authority"][
                    "control_release_id"
                ],
                ci_evidence=successor["ci"],
                target_control_release_id=bridge["target"][
                    "control_release_id"
                ],
                policy=bridge["policy"],
                token_id=successor_token["token_id"],
                token_sha256=successor_token["token_sha256"],
            )
        )
        CONTROLLER.validate_descriptor(successor)
        successor_root, successor_path, successor_ready_path = (
            controller._operation_paths(successor_operation)
        )
        successor_root.mkdir(mode=0o700)
        CONTROLLER.atomic_json(successor_path, successor)
        successor_digest = CONTROLLER.sha256_file(successor_path)
        CONTROLLER._bridge_core.bind_token_descriptor(
            controller.state_dir,
            operation_id=successor_operation,
            policy_id=bridge["policy"]["policy_id"],
            descriptor_sha256=successor_digest,
        )
        successor_ready = {
            "schema_version": 1,
            "status": "ready",
            "operation_id": successor_operation,
            "source_sha": successor["repository"]["target_sha"],
            "descriptor_sha256": successor_digest,
            "executor_control": executor,
            "executor_control_sha256": successor["controller"][
                "executor_control_sha256"
            ],
            "slot_record_sha256": successor["monomer_md"][
                "slot_record_sha256"
            ],
            "prepared_at": "2026-07-17T00:00:01Z",
        }
        CONTROLLER.atomic_json(successor_ready_path, successor_ready)
        current_token = CONTROLLER._bridge_core.load_token_authority(
            controller.state_dir
        )
        self.assertEqual(
            CONTROLLER.validate_alias_bridge_authority(
                alias_authority,
                descriptor=successor,
                descriptor_path=successor_path,
                ready_path=successor_ready_path,
                state_root=controller.state_dir,
                current_token=current_token,
            ),
            alias_authority,
        )

    def test_external_database_cas_allows_only_fresh_timestamp_changes(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        observed = json.loads(
            json.dumps(descriptor["external_database_audit"])
        )
        observed["snapshot"]["media_registry"]["captured_at"] = (
            "2026-07-17T00:01:00Z"
        )
        observed["snapshot"]["media"][0]["audit"]["audited_at"] = (
            "2026-07-17T00:01:00Z"
        )
        observed["snapshot"]["media_registry"][
            "docker_inventory_sha256"
        ] = "sha256:" + "1" * 64
        observed["snapshot"]["media_registry"][
            "backup_inventory_sha256"
        ] = "sha256:" + "2" * 64
        observed["snapshot"]["media_registry"][
            "discovery_state_sha256_before"
        ] = "sha256:" + "3" * 64
        observed["snapshot"]["media_registry"][
            "discovery_state_sha256_after"
        ] = "sha256:" + "3" * 64
        observed["snapshot"]["media_registry"][
            "scanned_container_ids"
        ] = sorted(
            [
                *observed["snapshot"]["media_registry"][
                    "scanned_container_ids"
                ],
                "f" * 64,
            ]
        )
        reseal_external_database_audit_binding(observed)
        with mock.patch.object(
            controller,
            "external_database_audit_evidence",
            return_value=observed,
        ):
            self.assertEqual(
                controller._revalidate_external_database_audit(descriptor),
                observed,
            )

        changed = json.loads(json.dumps(observed))
        changed["snapshot"]["media"][0]["source_content_sha256"] = (
            "sha256:" + "e" * 64
        )
        reseal_external_database_audit_binding(changed)
        with (
            mock.patch.object(
                controller,
                "external_database_audit_evidence",
                return_value=changed,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "media changed",
            ),
        ):
            controller._revalidate_external_database_audit(descriptor)

    def test_external_database_semantic_state_excludes_fresh_self_seals(
        self,
    ) -> None:
        controller = self.controller()
        before = external_database_audit_binding(
            controller.runtime_root
        )
        ledger = next(
            record["ledger"]
            for record in before["snapshot"]["media"]
            if record["disposition"] == "writable-target"
        )
        after = external_database_binding_state(
            before,
            production_ledger=ledger,
            legacy_relation_present=True,
            captured_at="2026-07-17T00:02:00Z",
        )
        self.assertNotEqual(
            before["identity_sha256"],
            after["identity_sha256"],
        )
        self.assertEqual(
            before["state_sha256"],
            after["state_sha256"],
        )
        changed = json.loads(json.dumps(after))
        medium = changed["snapshot"]["media"][0]
        medium["source_identity_before"]["attached"][0][
            "container_started_at"
        ] = "2026-07-17T00:03:00.000000000Z"
        medium["source_identity_after"] = json.loads(
            json.dumps(medium["source_identity_before"])
        )
        medium["audit"].pop("evidence_sha256")
        medium["audit"]["evidence_sha256"] = (
            CONTROLLER.canonical_json_digest(medium)
        )
        reseal_external_database_audit_binding(changed)
        validated = CONTROLLER.validate_external_database_audit_binding(
            changed
        )
        self.assertNotEqual(
            before["state_sha256"],
            validated["state_sha256"],
        )

    def test_external_database_semantic_state_normalizes_backup_scratch_identity(
        self,
    ) -> None:
        first = {
            "media_registry": {
                "schema_version": 2,
                "sha256": "sha256:" + "1" * 64,
                "discovery_boundary_sha256": "sha256:" + "2" * 64,
                "expected_media_ids": ["postgres-backup:/private/db.dump"],
                "discovered_media_ids": ["postgres-backup:/private/db.dump"],
                "captured_at": "2026-07-17T00:00:00Z",
                "discovery_state_sha256_before": "sha256:" + "3" * 64,
                "discovery_state_sha256_after": "sha256:" + "3" * 64,
                "docker_inventory_sha256": "sha256:" + "4" * 64,
                "backup_inventory_sha256": "sha256:" + "5" * 64,
                "scanned_volume_names": [],
                "scanned_bind_sources": [],
                "scanned_container_ids": [],
            },
            "media": [
                {
                    "database_identity": {
                        "database": "nexpoly",
                        "system_identifier": "111",
                        "system_identifier_scope": "isolated-restore-cluster",
                        "database_oid": "16384",
                        "database_owner": "postgres",
                        "encoding": "UTF8",
                        "collate": "C",
                        "ctype": "C",
                        "server_version_num": 160004,
                    },
                    "database_identity_sha256": "sha256:" + "6" * 64,
                    "source_content_sha256": "sha256:" + "7" * 64,
                    "audit": {
                        "method": "isolated-backup-restore-read-only",
                        "audited_at": "2026-07-17T00:00:00Z",
                        "evidence_sha256": "sha256:" + "8" * 64,
                    },
                }
            ],
        }
        second = json.loads(json.dumps(first))
        second["media"][0]["database_identity"].update(
            {
                "system_identifier": "222",
                "database_oid": "24576",
                "server_version_num": 160005,
            }
        )
        second["media"][0]["database_identity_sha256"] = (
            "sha256:" + "9" * 64
        )
        self.assertEqual(
            CONTROLLER.external_database_audit_state(first),
            CONTROLLER.external_database_audit_state(second),
        )

    def test_alias_and_bridge_external_database_pairs_form_exact_chain(
        self,
    ) -> None:
        controller = self.controller()
        canonical = [
            {"version": version, "checksum": checksum}
            for version, checksum in (
                CONTROLLER._site_helper_contracts.CANONICAL_MIGRATION_LEDGER
            )
        ]
        through_0008 = [
            row
            for row in canonical
            if row["version"] <= "0008_polytao_backend_runtime"
        ]
        through_0011 = [
            row
            for row in canonical
            if row["version"] <= "0011_monomer_md_demo_steps"
        ]
        alias_row = {
            "version": (
                CONTROLLER._site_helper_contracts
                .LEGACY_0005_ALIAS_VERSION
            ),
            "checksum": (
                CONTROLLER._site_helper_contracts
                .LEGACY_0005_ALIAS_CHECKSUM
            ),
        }
        seed = external_database_audit_binding(
            controller.runtime_root
        )
        pre_alias = external_database_binding_state(
            seed,
            production_ledger=sorted(
                [*through_0008, alias_row],
                key=lambda row: row["version"],
            ),
            legacy_relation_present=True,
            captured_at="2026-07-17T00:01:00Z",
        )
        post_alias = external_database_binding_state(
            pre_alias,
            production_ledger=through_0008,
            legacy_relation_present=True,
            captured_at="2026-07-17T00:02:00Z",
        )
        descriptor_sha256 = "sha256:" + "d" * 64
        alias_pair = CONTROLLER.build_external_database_alias_pair(
            pre_alias,
            post_alias,
            operation_id="alias-0005-chain-test",
            descriptor_sha256=descriptor_sha256,
        )
        self.assertEqual(
            CONTROLLER.validate_external_database_alias_pair(
                alias_pair,
                before_binding=pre_alias,
            ),
            alias_pair,
        )
        post_bridge = external_database_binding_state(
            post_alias,
            production_ledger=through_0011,
            legacy_relation_present=True,
            captured_at="2026-07-17T00:03:00Z",
        )
        bridge_pair = CONTROLLER.build_external_database_bridge_pair(
            post_alias,
            post_bridge,
            operation_id=OPERATION_ID,
            descriptor_sha256=descriptor_sha256,
        )
        self.assertEqual(
            bridge_pair["transition"]["added"],
            through_0011[len(through_0008) :],
        )
        volatile_post_bridge = json.loads(json.dumps(post_bridge))
        registry = volatile_post_bridge["snapshot"]["media_registry"]
        registry["docker_inventory_sha256"] = "sha256:" + "0" * 64
        registry["backup_inventory_sha256"] = "sha256:" + "1" * 64
        registry["scanned_container_ids"] = sorted(
            [*registry["scanned_container_ids"], "f" * 64]
        )
        reseal_external_database_audit_binding(volatile_post_bridge)
        volatile_pair = CONTROLLER.build_external_database_bridge_pair(
            post_alias,
            volatile_post_bridge,
            operation_id=OPERATION_ID,
            descriptor_sha256=descriptor_sha256,
        )
        self.assertEqual(
            volatile_pair["after_binding"]["state_sha256"],
            bridge_pair["after_binding"]["state_sha256"],
        )
        changed = json.loads(json.dumps(post_alias))
        dormant = next(
            record
            for record in changed["snapshot"]["media"]
            if record["database"] == "nexpoly_dev"
        )
        dormant["source_identity_before"]["inspect_sha256"] = (
            "sha256:" + "0" * 64
        )
        dormant["source_identity_after"] = json.loads(
            json.dumps(dormant["source_identity_before"])
        )
        dormant["audit"].pop("evidence_sha256")
        dormant["audit"]["evidence_sha256"] = (
            CONTROLLER.canonical_json_digest(dormant)
        )
        reseal_external_database_audit_binding(changed)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "read-only external media",
        ):
            CONTROLLER.build_external_database_bridge_pair(
                post_alias,
                changed,
                operation_id=OPERATION_ID,
                descriptor_sha256=descriptor_sha256,
            )

    def test_external_database_chain_folds_exact_0012_and_0013_endpoints(
        self,
    ) -> None:
        controller = self.controller()
        canonical = [
            {"version": version, "checksum": checksum}
            for version, checksum in (
                CONTROLLER._site_helper_contracts.CANONICAL_MIGRATION_LEDGER
            )
        ]
        through_0012 = canonical[:12]
        through_0013 = canonical[:13]
        bridge = external_database_audit_binding(
            controller.runtime_root
        )
        post_contract = external_database_binding_state(
            bridge,
            production_ledger=through_0012,
            legacy_relation_present=False,
            captured_at="2026-07-17T00:04:00Z",
        )
        contract_pair = (
            CONTROLLER.build_external_database_contract_pair(
                bridge,
                post_contract["snapshot"],
                operation_id="contract-0012-chain-test",
            )
        )
        self.assertEqual(
            CONTROLLER.external_database_endpoint(
                bridge,
                contract_pair=contract_pair,
            )["state_sha256"],
            post_contract["state_sha256"],
        )

        post_final = external_database_binding_state(
            post_contract,
            production_ledger=through_0013,
            legacy_relation_present=False,
            captured_at="2026-07-17T00:05:00Z",
        )
        descriptor_sha256 = "sha256:" + "d" * 64
        final_pair = CONTROLLER.build_external_database_final_pair(
            post_contract,
            post_final,
            operation_id="deploy-final-0013-chain-test",
            descriptor_sha256=descriptor_sha256,
        )
        self.assertEqual(
            CONTROLLER.validate_external_database_final_pair(
                final_pair,
                before_binding=post_contract,
            ),
            final_pair,
        )
        self.assertEqual(
            final_pair["transition"]["added"],
            through_0013[len(through_0012) :],
        )
        self.assertEqual(
            CONTROLLER.external_database_endpoint(
                bridge,
                contract_pair=contract_pair,
                final_pair=final_pair,
            )["state_sha256"],
            post_final["state_sha256"],
        )

        wrong_checksum = json.loads(json.dumps(post_final))
        writable = next(
            record
            for record in wrong_checksum["snapshot"]["media"]
            if record["disposition"] == "writable-target"
        )
        writable["ledger"][-1]["checksum"] = "f" * 64
        writable["ledger_sha256"] = CONTROLLER.canonical_json_digest(
            writable["ledger"]
        )
        writable["ledger_relation"]["content_sha256"] = writable[
            "ledger_sha256"
        ]
        writable["migration_0013"] = {
            "state": "superseded",
            "checksum": "f" * 64,
        }
        writable["audit"].pop("evidence_sha256")
        writable["audit"]["evidence_sha256"] = (
            CONTROLLER.canonical_json_digest(writable)
        )
        reseal_external_database_audit_binding(wrong_checksum)
        with self.assertRaises(CONTROLLER.PullDeployError):
            CONTROLLER.build_external_database_final_pair(
                post_contract,
                wrong_checksum,
                operation_id="deploy-final-0013-chain-test",
                descriptor_sha256=descriptor_sha256,
            )

    def test_external_database_revalidation_allows_only_exact_0014_expansion(
        self,
    ) -> None:
        controller = self.controller()
        canonical = [
            {"version": version, "checksum": checksum}
            for version, checksum in (
                CONTROLLER._site_helper_contracts.CANONICAL_MIGRATION_LEDGER
            )
        ]
        post_0013 = external_database_binding_state(
            external_database_audit_binding(controller.runtime_root),
            production_ledger=canonical[:13],
            legacy_relation_present=False,
            captured_at="2026-07-17T00:05:00Z",
        )
        post_0014 = external_database_binding_state(
            post_0013,
            production_ledger=canonical[:14],
            legacy_relation_present=False,
            captured_at="2026-07-17T00:06:00Z",
        )
        policy = {
            **CONTROLLER._bridge_core.EXTERNAL_DATABASE_AUDIT_POLICY,
            "media_authority_rules_sha256": post_0013[
                "authority_rules"
            ]["sha256"],
            "audit_role_sql_sha256": post_0013["role_sql"]["sha256"],
        }

        with mock.patch.object(
            controller,
            "external_database_audit_evidence",
            return_value=post_0014,
        ):
            self.assertEqual(
                controller._revalidate_external_database_binding(
                    post_0013,
                    policy=policy,
                    allow_queue_expansion=True,
                ),
                post_0014,
            )
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "changed after its sealed transition",
            ):
                controller._revalidate_external_database_binding(
                    post_0013,
                    policy=policy,
                )

    def test_bridge_prepare_binds_static_rules_dynamic_registry_and_fresh_audit(
        self,
    ) -> None:
        controller = self.controller()
        expected = external_database_audit_binding(
            controller.runtime_root,
            captured_at=CONTROLLER.utc_now(),
        )
        runner = mock.Mock()
        runner.run.return_value = subprocess.CompletedProcess(
            [expected["helper"]["path"]],
            0,
            json.dumps(expected["snapshot"]),
            "",
        )
        controller.runner = runner
        values = {
            CONTROLLER.CONTRACT_0012_EXTERNAL_AUDIT_COMMAND: expected[
                "helper"
            ]["path"],
            CONTROLLER.CONTRACT_0012_MEDIA_AUTHORITY_RULES_DIGEST: expected[
                "authority_rules"
            ]["sha256"],
            CONTROLLER.CONTRACT_0012_EXTERNAL_AUDIT_USERS[
                "nexpoly_dev"
            ]: expected["expected_users"]["nexpoly_dev"],
            CONTROLLER.CONTRACT_0012_EXTERNAL_AUDIT_USERS[
                "nexpoly_md_health_opt"
            ]: expected["expected_users"]["nexpoly_md_health_opt"],
        }
        policy = {
            **CONTROLLER._bridge_core.EXTERNAL_DATABASE_AUDIT_POLICY,
            "media_authority_rules_sha256": expected[
                "authority_rules"
            ]["sha256"],
            "audit_role_sql_sha256": expected["role_sql"]["sha256"],
        }
        with mock.patch.object(
            controller,
            "production_deploy_values",
            return_value=values,
        ):
            observed = (
                CONTROLLER.PullDeployController.external_database_audit_evidence(
                    controller,
                    policy,
                )
            )
        self.assertEqual(observed, expected)
        runner.run.assert_called_once()
        self.assertEqual(
            len(runner.run.call_args.args[0]),
            1,
        )
        self.assertRegex(
            runner.run.call_args.args[0][0],
            r"^/proc/self/fd/[0-9]+$",
        )
        self.assertEqual(
            runner.run.call_args.kwargs["pass_fds"],
            (
                int(
                    runner.run.call_args.args[0][0].rsplit("/", 1)[1]
                ),
            ),
        )
        self.assertEqual(
            observed["registry"]["authority_rules_sha256"],
            observed["authority_rules"]["sha256"],
        )
        self.assertEqual(
            observed["snapshot"]["media_registry"][
                "runtime_registry_sha256"
            ],
            observed["registry"]["sha256"],
        )
        self.assertNotIn(
            "runtime_registry_sha256",
            policy,
        )

        refreshed = external_database_audit_binding(
            controller.runtime_root,
            captured_at=CONTROLLER.utc_now(),
        )
        runner.reset_mock()
        runner.run.return_value = subprocess.CompletedProcess(
            [refreshed["helper"]["path"], "revalidate"],
            0,
            json.dumps(refreshed["snapshot"]),
            "",
        )
        with mock.patch.object(
            controller,
            "production_deploy_values",
            return_value=values,
        ):
            lightweight = (
                CONTROLLER.PullDeployController.external_database_audit_evidence(
                    controller,
                    policy,
                    lightweight_revalidation=True,
                )
            )
        self.assertEqual(lightweight, refreshed)
        self.assertEqual(
            runner.run.call_args.args[0][1:],
            ["revalidate"],
        )
        self.assertRegex(
            runner.run.call_args.args[0][0],
            r"^/proc/self/fd/[0-9]+$",
        )

        stale = external_database_audit_binding(
            controller.runtime_root,
            captured_at="2026-07-16T00:00:00Z",
        )
        runner.run.return_value = subprocess.CompletedProcess(
            [stale["helper"]["path"]],
            0,
            json.dumps(stale["snapshot"]),
            "",
        )
        with (
            mock.patch.object(
                controller,
                "production_deploy_values",
                return_value=values,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "not captured by this invocation",
            ),
        ):
            CONTROLLER.PullDeployController.external_database_audit_evidence(
                controller,
                policy,
            )

        missing_user_values = dict(values)
        missing_user_values.pop(
            CONTROLLER.CONTRACT_0012_EXTERNAL_AUDIT_USERS[
                "nexpoly_dev"
            ]
        )
        with (
            mock.patch.object(
                controller,
                "production_deploy_values",
                return_value=missing_user_values,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "user is missing or invalid",
            ),
        ):
            CONTROLLER.PullDeployController.external_database_audit_evidence(
                controller,
                policy,
            )

        with (
            mock.patch.object(
                controller,
                "production_deploy_values",
                return_value={
                    **values,
                    CONTROLLER.CONTRACT_0012_MEDIA_AUTHORITY_RULES_DIGEST: (
                        "sha256:" + "f" * 64
                    ),
                },
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "differ from deploy.env or F policy",
            ),
        ):
            CONTROLLER.PullDeployController.external_database_audit_evidence(
                controller,
                {
                    **policy,
                    "media_authority_rules_sha256": (
                        "sha256:" + "f" * 64
                    ),
                },
            )

    def test_external_database_dual_digests_are_policy_and_cas_bound(
        self,
    ) -> None:
        controller = self.controller()
        expected = external_database_audit_binding(
            controller.runtime_root
        )
        policy = {
            **CONTROLLER._bridge_core.EXTERNAL_DATABASE_AUDIT_POLICY,
            "media_authority_rules_sha256": expected[
                "authority_rules"
            ]["sha256"],
            "audit_role_sql_sha256": expected["role_sql"]["sha256"],
        }
        CONTROLLER.validate_external_database_audit_binding(
            expected,
            expected_policy=policy,
        )

        changed_authority = json.loads(json.dumps(expected))
        replacement_authority = "sha256:" + "e" * 64
        changed_authority["authority_rules"]["sha256"] = (
            replacement_authority
        )
        changed_authority["helper_control"][
            "authority_rules_sha256"
        ] = replacement_authority
        changed_authority["registry"]["authority_rules_sha256"] = (
            replacement_authority
        )
        changed_authority["snapshot"]["media_registry"][
            "media_authority_rules_sha256"
        ] = replacement_authority
        reseal_external_database_audit_binding(changed_authority)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "differs from F policy",
        ):
            CONTROLLER.validate_external_database_audit_binding(
                changed_authority,
                expected_policy=policy,
            )

        changed_registry = json.loads(json.dumps(expected))
        replacement_registry = "sha256:" + "d" * 64
        changed_registry["registry"]["sha256"] = replacement_registry
        changed_registry["snapshot"]["media_registry"][
            "runtime_registry_sha256"
        ] = replacement_registry
        reseal_external_database_audit_binding(changed_registry)
        CONTROLLER.validate_external_database_audit_binding(
            changed_registry,
            expected_policy=policy,
        )
        with (
            mock.patch.object(
                controller,
                "external_database_audit_evidence",
                return_value=changed_registry,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "media changed",
            ),
        ):
            controller._revalidate_external_database_binding(
                expected,
                policy=policy,
            )

    def test_bridge_ledger_registry_is_consumed_by_runtime_validation(self) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        accepted = descriptor["bridge"]["policy"]["accepted_migration_ledgers"]
        manifest = descriptor["migrations"]["records"]
        for name, rows in (
            ("pre-0012", manifest[:-1]),
            ("post-0012", manifest),
            (
                "post-0013",
                [*manifest, CONTROLLER._bridge_core.DFT_MIGRATION_RECORD],
            ),
            (
                "post-0014",
                [
                    *manifest,
                    CONTROLLER._bridge_core.DFT_MIGRATION_RECORD,
                    CONTROLLER._bridge_core.QUEUE_MIGRATION_RECORD,
                ],
            ),
            ("post-0015", F_MANIFEST_RECORDS),
        ):
            history = CONTROLLER.canonical_ledger_history(
                [
                    {
                        "version": record["version"],
                        "checksum": record["checksum"],
                    }
                    for record in rows
                ],
                manifest,
                accepted_ledgers=accepted,
                require_registry_match=True,
            )
            self.assertEqual(history, rows)
            compatibility = CONTROLLER.build_migration_compatibility_state(
                descriptor["bridge"]["policy"],
                code_manifest_sha256=(
                    F_MANIFEST_DIGEST
                    if name in {"post-0013", "post-0014", "post-0015"}
                    else B_MANIFEST_DIGEST
                ),
                migrations=history,
            )
            self.assertEqual(compatibility["ledger_state"]["name"], name)

        for rows in (
            [
                *manifest,
                {
                    **CONTROLLER._bridge_core.FINAL_MIGRATION,
                    "checksum": "f" * 64,
                },
            ],
            [
                *manifest,
                CONTROLLER._bridge_core.FINAL_MIGRATION,
                {"version": "0014_future", "checksum": "e" * 64},
            ],
        ):
            with self.assertRaises(CONTROLLER.PullDeployError):
                CONTROLLER.canonical_ledger_history(
                    rows,
                    manifest,
                    accepted_ledgers=accepted,
                    require_registry_match=True,
                )

    def test_b_state_can_truthfully_record_f_0013_ledger(self) -> None:
        descriptor = self.bridge_descriptor(self.controller())
        migrations = json.loads(
            json.dumps(
                [
                    *descriptor["migrations"]["records"],
                    CONTROLLER._bridge_core.DFT_MIGRATION_RECORD,
                ]
            )
        )
        compatibility = CONTROLLER.build_migration_compatibility_state(
            descriptor["bridge"]["policy"],
            code_manifest_sha256=B_MANIFEST_DIGEST,
            migrations=migrations,
        )
        self.assertEqual(
            compatibility["code_manifest_sha256"],
            B_MANIFEST_DIGEST,
        )
        self.assertEqual(
            compatibility["ledger_manifest_sha256"],
            descriptor["bridge"]["policy"]["accepted_migration_ledgers"][-1][
                "manifest_sha256"
            ],
        )
        self.assertEqual(
            compatibility["ledger_state"]["name"],
            "post-0013",
        )

    def test_bridge_state_cannot_delete_authority_and_downgrade_to_0011(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        state = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        _operation_root, descriptor_path, ready_path = (
            controller._operation_paths(OPERATION_ID)
        )
        CONTROLLER.atomic_json(descriptor_path, descriptor)
        descriptor_digest = CONTROLLER.sha256_file(descriptor_path)
        ready = CONTROLLER.load_private_json(ready_path)
        ready["descriptor_sha256"] = descriptor_digest
        CONTROLLER.atomic_json(ready_path, ready)

        state["descriptor_sha256"] = descriptor_digest
        state["schema_version"] = CONTROLLER.LEGACY_CURRENT_STATE_SCHEMA_VERSION
        state["monomer_md_worker_env"] = descriptor["monomer_md"]["worker_env"]
        for field in (
            "authority_kind",
            "adoption_evidence",
            "adoption_evidence_sha256",
            "adopted_deployment_sha256",
            "monomer_dft",
            "postgres_rehearsal",
        ):
            state.pop(field)
        state["migrations"] = json.loads(
            json.dumps(descriptor["migrations"]["records"])
        )
        state["migration_compatibility"] = (
            CONTROLLER.build_migration_compatibility_state(
                descriptor["bridge"]["policy"],
                code_manifest_sha256=B_MANIFEST_DIGEST,
                migrations=state["migrations"],
            )
        )
        state["external_database_audit"] = (
            external_database_audit_binding(controller.runtime_root)
        )
        state["external_database_transition_chain"] = (
            CONTROLLER.build_external_database_transition_chain(
                alias_reference={
                    "path": "/runtime/audit/alias-transition.json",
                    "sha256": "sha256:" + "1" * 64,
                    "identity_sha256": "sha256:" + "2" * 64,
                    "before_state_sha256": "sha256:" + "3" * 64,
                    "after_state_sha256": "sha256:" + "4" * 64,
                    "descriptor_sha256": "sha256:" + "5" * 64,
                    "operation_id": "alias-0005-fixture",
                    "kind": "alias-0005-reconciliation",
                },
                bridge_reference={
                    "path": "/runtime/audit/bridge-transition.json",
                    "sha256": "sha256:" + "6" * 64,
                    "identity_sha256": "sha256:" + "7" * 64,
                    "before_state_sha256": "sha256:" + "4" * 64,
                    "after_state_sha256": state[
                        "external_database_audit"
                    ]["state_sha256"],
                    "descriptor_sha256": descriptor_digest,
                    "operation_id": OPERATION_ID,
                    "kind": "bridge-expand-to-0011",
                },
                active_binding=state["external_database_audit"],
            )
        )
        controller._validate_state_source_descriptor(state)

        downgraded = json.loads(json.dumps(state))
        downgraded["migration_compatibility"] = None
        downgraded["migrations"] = downgraded["migrations"][:11]
        for field in (
            "contract_external_database_audit",
            "final_external_database_audit",
        ):
            downgraded.pop(field, None)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "compatibility presence differs",
        ):
            controller._validate_state_source_descriptor(downgraded)

        retained_compatibility = json.loads(json.dumps(state))
        retained_compatibility["migrations"] = (
            retained_compatibility["migrations"][:11]
        )
        retained_compatibility["migration_compatibility"] = (
            CONTROLLER.build_migration_compatibility_state(
                descriptor["bridge"]["policy"],
                code_manifest_sha256=B_MANIFEST_DIGEST,
                migrations=retained_compatibility["migrations"],
            )
        )
        retained_compatibility.pop("external_database_audit")
        retained_compatibility.pop("external_database_transition_chain")
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lacks its external database authority",
        ):
            controller._validate_state_source_descriptor(
                retained_compatibility
            )

    def test_ordinary_successor_exactly_inherits_all_governance_authority(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        controller.apply(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        successor_operation = "deploy-governance-successor"
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=successor_operation,
        )
        successor = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=successor_operation,
        )
        controller._validate_state_source_descriptor(successor)

        mutations = {
            "approved_contracts": [{"foreign": True}],
            "migration_epoch_barrier": {"foreign": True},
            "schema_compatibility_floor": {"foreign": True},
            "last_contract_operation": "contract-foreign-0012",
            "contract_mutable_data_audit": {"foreign": True},
            "contract_external_database_audit": {"foreign": True},
        }
        for field, replacement in mutations.items():
            with self.subTest(field=field):
                changed = json.loads(json.dumps(successor))
                changed[field] = replacement
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    f"governance field {field}",
                ):
                    controller._validate_state_source_descriptor(
                        changed
                    )

        for field in (
            "external_database_audit",
            "external_database_transition_chain",
        ):
            with self.subTest(field=field):
                changed = json.loads(json.dumps(successor))
                changed[field] = {"foreign": True}
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "external database authority",
                ):
                    controller._validate_state_source_descriptor(
                        changed
                    )

        changed = json.loads(json.dumps(successor))
        changed["final_mutable_data_audit"] = {"foreign": True}
        changed["final_external_database_audit"] = {"foreign": True}
        with self.assertRaises(CONTROLLER.PullDeployError):
            controller._validate_state_source_descriptor(changed)

    def test_bootstrap_state_rejects_unproven_final_0013_authority(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        state = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        state["final_mutable_data_audit"] = {"foreign": True}
        state["final_external_database_audit"] = {"foreign": True}

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "unproven final 0013 authority",
        ):
            controller._validate_state_source_descriptor(state)

    def test_mutable_online_tables_are_sealed_before_and_after_apply(self) -> None:
        before = mutable_data_evidence()
        pair = CONTROLLER.build_mutable_data_pair(
            before,
            json.loads(json.dumps(before)),
        )
        self.assertEqual(pair["before"], pair["after"])

        for label, field, replacement in (
            ("row count", "row_count", 18),
            ("content", "content_sha256", "sha256:" + "f" * 64),
            ("schema", "schema_sha256", "sha256:" + "e" * 64),
        ):
            with self.subTest(label=label):
                after = json.loads(json.dumps(before))
                after["business_tables"][0][field] = replacement
                reseal_mutable_data_evidence(after)
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "mutable business table changed",
                ):
                    CONTROLLER.build_mutable_data_pair(before, after)

    def test_bridge_mutable_pair_proves_exact_0008_to_0011_expansion(
        self,
    ) -> None:
        before = mutable_data_evidence(ledger_length=8)
        after = mutable_data_evidence(ledger_length=11)
        restored_pair = CONTROLLER.build_mutable_data_pair(
            before,
            json.loads(json.dumps(before)),
        )
        self.assertEqual(
            restored_pair["transition"]["control"]["mode"],
            "pre-0010-unchanged",
        )
        before_md = next(
            record
            for record in before["business_tables"]
            if record["schema"] == "md"
            and record["table"] == "monomer_md_jobs"
        )
        after_md = next(
            record
            for record in after["business_tables"]
            if record["schema"] == "md"
            and record["table"] == "monomer_md_jobs"
        )
        before_md["schema_sha256"] = (
            CONTROLLER.BRIDGE_MUTABLE_MD_SCHEMA_SHA256_BEFORE
        )
        after_md["schema_sha256"] = (
            CONTROLLER.BRIDGE_MUTABLE_MD_SCHEMA_SHA256_AFTER
        )
        after_md["content_sha256"] = "sha256:" + "0" * 64
        after_control = after["governed_controls"]["deployment_control"]
        after_control["table"]["schema_sha256"] = (
            CONTROLLER.BRIDGE_MUTABLE_DEPLOYMENT_CONTROL_SCHEMA_SHA256
        )
        after_control["row"].update(
            {
                "reason": f"post-canary drain {OPERATION_ID}",
                "activated_by": "pull-deploy-controller",
                "release_sha": TARGET_SHA,
            }
        )
        after_analytics = after["governed_controls"][
            "database_analytics_snapshots"
        ]
        after_analytics["table"]["schema_sha256"] = (
            CONTROLLER.BRIDGE_MUTABLE_ANALYTICS_SCHEMA_SHA256
        )
        reseal_mutable_data_evidence(before)
        reseal_mutable_data_evidence(after)
        descriptor_digest = "sha256:" + "9" * 64
        pair = CONTROLLER.build_bridge_mutable_data_pair(
            before,
            after,
            descriptor_sha256=descriptor_digest,
        )
        self.assertEqual(
            pair["transition"]["kind"],
            "bridge-expand-to-0011",
        )
        self.assertEqual(
            pair["transition"]["descriptor_sha256"],
            descriptor_digest,
        )

        controller = self.controller()
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        state = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        state["descriptor_sha256"] = descriptor_digest
        state["postgres_rehearsal"]["descriptor_sha256"] = descriptor_digest
        state["migrations"] = json.loads(
            json.dumps(B_MANIFEST_RECORDS[:11])
        )
        state["mutable_data_audit"] = pair
        state["database_backup"]["mutable_data_before_sha256"] = (
            pair["identity_sha256"]
        )
        accepted_ledgers = (
            CONTROLLER._bridge_core.expected_migration_registry(
                target_manifest_sha256=B_MANIFEST_DIGEST,
                target_records=B_MANIFEST_RECORDS,
                authority_manifest_sha256=F_MANIFEST_DIGEST,
                authority_records=F_MANIFEST_RECORDS,
            )
        )
        state["migration_compatibility"] = (
            CONTROLLER.build_migration_compatibility_state(
                {
                    "policy_id": "sha256:" + "8" * 64,
                    "accepted_migration_ledgers": accepted_ledgers,
                },
                code_manifest_sha256=B_MANIFEST_DIGEST,
                migrations=state["migrations"],
            )
        )
        state["external_database_audit"] = (
            external_database_audit_binding(controller.runtime_root)
        )
        state["external_database_transition_chain"] = (
            CONTROLLER.build_external_database_transition_chain(
                alias_reference={
                    "path": "/runtime/audit/alias-transition.json",
                    "sha256": "sha256:" + "1" * 64,
                    "identity_sha256": "sha256:" + "2" * 64,
                    "before_state_sha256": "sha256:" + "3" * 64,
                    "after_state_sha256": "sha256:" + "4" * 64,
                    "descriptor_sha256": "sha256:" + "5" * 64,
                    "operation_id": "alias-0005-fixture",
                    "kind": "alias-0005-reconciliation",
                },
                bridge_reference={
                    "path": "/runtime/audit/bridge-transition.json",
                    "sha256": "sha256:" + "6" * 64,
                    "identity_sha256": "sha256:" + "7" * 64,
                    "before_state_sha256": "sha256:" + "4" * 64,
                    "after_state_sha256": state[
                        "external_database_audit"
                    ]["state_sha256"],
                    "descriptor_sha256": descriptor_digest,
                    "operation_id": OPERATION_ID,
                    "kind": "bridge-expand-to-0011",
                },
                active_binding=state["external_database_audit"],
            )
        )
        CONTROLLER.validate_current_deployment_state(state)

        changed = json.loads(json.dumps(after))
        changed["bridge_projection"]["content_sha256"] = (
            "sha256:" + "1" * 64
        )
        reseal_mutable_data_evidence(changed)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "changed existing business rows",
        ):
            CONTROLLER.build_bridge_mutable_data_pair(
                before,
                changed,
                descriptor_sha256=descriptor_digest,
            )

        leased = json.loads(json.dumps(after))
        leased["bridge_projection"]["lease_columns"][
            "non_null_counts"
        ]["worker_instance_id"] = 1
        reseal_mutable_data_evidence(leased)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lease columns changed existing business rows",
        ):
            CONTROLLER.build_bridge_mutable_data_pair(
                before,
                leased,
                descriptor_sha256=descriptor_digest,
            )

        partial_control = json.loads(json.dumps(before))
        partial_control["governed_controls"]["deployment_control"] = (
            json.loads(
                json.dumps(
                    after["governed_controls"]["deployment_control"]
                )
            )
        )
        reseal_mutable_data_evidence(partial_control)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "mutable-data audit evidence is invalid",
        ):
            CONTROLLER.validate_mutable_data_evidence(partial_control)

    def test_mutable_data_allows_only_exact_0012_exception(self) -> None:
        before = mutable_data_evidence(ledger_length=11)
        before_control = before["governed_controls"]["deployment_control"]
        before_control["row"].update(
            {
                "reason": f"0012 maintenance {OPERATION_ID}",
                "activated_by": "pull-contract-0012",
            }
        )
        reseal_mutable_data_evidence(before)
        after = mutable_data_evidence(ledger_length=12)
        after_control = after["governed_controls"]["deployment_control"]
        after_control["row"].update(
            {
                "reason": f"0012 maintenance {OPERATION_ID}",
                "activated_by": "pull-contract-0012",
                "updated_at": "2026-07-17T00:01:00Z",
            }
        )
        after_control["table"]["content_sha256"] = "sha256:" + "e" * 64
        reseal_mutable_data_evidence(after)

        pair = CONTROLLER.build_mutable_data_pair(before, after)

        self.assertEqual(pair["transition"]["kind"], "contract-0012")
        self.assertEqual(
            pair["transition"]["polytao_exception"]["row_count"],
            before["migration_exception"]["row_count"],
        )

        changed = json.loads(json.dumps(after))
        changed["business_tables"][0]["content_sha256"] = "sha256:" + "f" * 64
        reseal_mutable_data_evidence(changed)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "mutable business table changed",
        ):
            CONTROLLER.build_mutable_data_pair(before, changed)

    def test_mutable_data_allows_only_pristine_0013_expansion(self) -> None:
        before = mutable_data_evidence(ledger_length=12)
        after = mutable_data_evidence(ledger_length=13)

        pair = CONTROLLER.build_mutable_data_pair(before, after)

        self.assertEqual(pair["transition"]["kind"], "expand-0013")
        self.assertEqual(
            pair["transition"]["dft_relations"],
            sorted(CONTROLLER.MUTABLE_DATA_BUSINESS_TABLES[-3:]),
        )

        nonempty = json.loads(json.dumps(after))
        nonempty["business_tables"][-1]["row_count"] = 1
        reseal_mutable_data_evidence(nonempty)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "empty DFT business relation",
        ):
            CONTROLLER.build_mutable_data_pair(before, nonempty)

        advanced = json.loads(json.dumps(after))
        advanced["sequences"][-1]["is_called"] = True
        reseal_mutable_data_evidence(advanced)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "DFT creation evidence is not pristine",
        ):
            CONTROLLER.build_mutable_data_pair(before, advanced)

    def test_mutable_data_allows_only_pristine_0014_expansion(self) -> None:
        before = mutable_data_evidence(ledger_length=13)
        after = mutable_data_evidence(ledger_length=14)

        pair = CONTROLLER.build_mutable_data_pair(before, after)

        self.assertEqual(pair["transition"]["kind"], "expand-0014")
        md_before = next(
            record
            for record in before["business_tables"]
            if (record["schema"], record["table"])
            == ("md", "monomer_md_jobs")
        )
        md_after = next(
            record
            for record in after["business_tables"]
            if (record["schema"], record["table"])
            == ("md", "monomer_md_jobs")
        )
        self.assertEqual(md_before["row_count"], md_after["row_count"])
        self.assertNotEqual(
            md_before["schema_sha256"],
            md_after["schema_sha256"],
        )

        advanced = json.loads(json.dumps(after))
        queue_sequence = next(
            record
            for record in advanced["sequences"]
            if (record["schema"], record["sequence"])
            == ("md", "monomer_md_queue_sequence_seq")
        )
        queue_sequence["is_called"] = True
        reseal_mutable_data_evidence(advanced)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "queue sequence is not pristine",
        ):
            CONTROLLER.build_mutable_data_pair(before, advanced)

        changed_rows = json.loads(json.dumps(after))
        md_jobs = next(
            record
            for record in changed_rows["business_tables"]
            if (record["schema"], record["table"])
            == ("md", "monomer_md_jobs")
        )
        md_jobs["row_count"] += 1
        changed_rows["bridge_projection"]["row_count"] += 1
        reseal_mutable_data_evidence(changed_rows)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "preserve the MD job table",
        ):
            CONTROLLER.build_mutable_data_pair(before, changed_rows)

    def test_current_state_uses_ordered_ledger_for_0014_queue_audit(self) -> None:
        controller = self.controller()
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        state = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        state["migrations"] = json.loads(json.dumps(F_MANIFEST_RECORDS[:14]))
        state["queue_mutable_data_audit"] = CONTROLLER.build_mutable_data_pair(
            mutable_data_evidence(ledger_length=13),
            mutable_data_evidence(ledger_length=14),
        )

        # This deliberately omits the separate post-0011 compatibility and
        # external-database authorities.  The validator must first consume the
        # ordered 0014 ledger prefix and then reject that later omission with a
        # domain error, rather than crashing by calling ``index`` on a set.
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "post-0011 migration history lacks compatibility",
        ):
            CONTROLLER.validate_current_deployment_state(state)

    def test_mutable_data_allows_only_pristine_0015_expansion(self) -> None:
        before = mutable_data_evidence(ledger_length=14)
        after = mutable_data_evidence(ledger_length=15)

        pair = CONTROLLER.build_mutable_data_pair(before, after)

        self.assertEqual(pair["transition"]["kind"], "expand-0015")
        snapshot = next(
            record
            for record in after["static_tables"]
            if (record["schema"], record["table"])
            == ("governance", "property_filter_options_snapshots")
        )
        self.assertEqual(snapshot["row_count"], 1)

        noncanonical = json.loads(json.dumps(after))
        snapshot = next(
            record
            for record in noncanonical["static_tables"]
            if (record["schema"], record["table"])
            == ("governance", "property_filter_options_snapshots")
        )
        snapshot["row_count"] = 2
        reseal_mutable_data_evidence(noncanonical)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "one property-filter catalog snapshot",
        ):
            CONTROLLER.build_mutable_data_pair(before, noncanonical)

    def test_mutable_data_allows_exact_post_0013_to_0015_expansion(self) -> None:
        before = mutable_data_evidence(ledger_length=13)
        after = mutable_data_evidence(ledger_length=15)

        pair = CONTROLLER.build_mutable_data_pair(before, after)

        self.assertEqual(pair["transition"]["kind"], "expand-0014-0015")
        self.assertIsNone(pair["transition"]["migration"])
        self.assertEqual(
            pair["transition"]["migrations"],
            after["migration_ledger"][-2:],
        )
        self.assertEqual(
            [record["version"] for record in pair["transition"]["migrations"]],
            [
                "0014_monomer_md_task_queue_cancel",
                "0015_property_filter_performance",
            ],
        )

        # A durable pair can be reopened after a lost controller response; the
        # validator must derive the same exact composite authority from the two
        # sealed snapshots rather than trusting the recorded transition.
        recovered = json.loads(json.dumps(pair))
        self.assertEqual(CONTROLLER.validate_mutable_data_pair(recovered), pair)

    def test_mutable_data_rejects_nonpristine_post_0013_to_0015_expansion(
        self,
    ) -> None:
        before = mutable_data_evidence(ledger_length=13)
        pristine = mutable_data_evidence(ledger_length=15)

        mutations = (
            (
                "md-rows",
                "preserve the MD job table",
                lambda evidence: (
                    next(
                        record
                        for record in evidence["business_tables"]
                        if (record["schema"], record["table"])
                        == ("md", "monomer_md_jobs")
                    ).update(row_count=1),
                    evidence["bridge_projection"].update(row_count=1),
                ),
            ),
            (
                "queue-sequence",
                "queue sequence is not pristine",
                lambda evidence: next(
                    record
                    for record in evidence["sequences"]
                    if (record["schema"], record["sequence"])
                    == ("md", "monomer_md_queue_sequence_seq")
                ).update(is_called=True),
            ),
            (
                "property-snapshot",
                "one property-filter catalog snapshot",
                lambda evidence: next(
                    record
                    for record in evidence["static_tables"]
                    if (record["schema"], record["table"])
                    == ("governance", "property_filter_options_snapshots")
                ).update(row_count=2),
            ),
            (
                "property-record-count",
                "static import tables changed",
                lambda evidence: next(
                    record
                    for record in evidence["static_tables"]
                    if (record["schema"], record["table"])
                    == ("core", "polymer_property_filter_records")
                ).update(row_count=615_160),
            ),
            (
                "analytics-snapshot",
                "analytics snapshots changed",
                lambda evidence: evidence["governed_controls"][
                    "database_analytics_snapshots"
                ]["table"].update(content_sha256="sha256:" + "f" * 64),
            ),
        )
        for label, message, mutate in mutations:
            with self.subTest(label=label):
                changed = json.loads(json.dumps(pristine))
                mutate(changed)
                reseal_mutable_data_evidence(changed)
                with self.assertRaisesRegex(CONTROLLER.PullDeployError, message):
                    CONTROLLER.build_mutable_data_pair(before, changed)

    def test_mutable_data_composite_transition_rejects_partial_crash_snapshot(
        self,
    ) -> None:
        before = mutable_data_evidence(ledger_length=13)
        complete = mutable_data_evidence(ledger_length=15)
        pair = CONTROLLER.build_mutable_data_pair(before, complete)

        # If execution stopped after 0014, an already-recorded composite pair
        # must not be reusable. Revalidation derives expand-0014 from the
        # partial after-snapshot and rejects the stale two-step assertion.
        partial = json.loads(json.dumps(pair))
        partial["after"] = mutable_data_evidence(ledger_length=14)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "mutable-data transition evidence differs",
        ):
            CONTROLLER.validate_mutable_data_pair(partial)

    def test_mutable_data_rejects_static_control_and_analytics_drift(self) -> None:
        before = mutable_data_evidence()
        mutations = (
            (
                "static",
                "static import tables changed",
                lambda value: value["static_tables"][0].update(
                    content_sha256="sha256:" + "f" * 64
                ),
            ),
            (
                "control",
                "current operation",
                lambda value: value["governed_controls"][
                    "deployment_control"
                ]["row"].update(reason="pull deployment another-operation"),
            ),
            (
                "analytics",
                "analytics snapshots changed",
                lambda value: value["governed_controls"][
                    "database_analytics_snapshots"
                ]["table"].update(content_sha256="sha256:" + "f" * 64),
            ),
        )
        for label, message, mutate in mutations:
            with self.subTest(label=label):
                after = json.loads(json.dumps(before))
                mutate(after)
                reseal_mutable_data_evidence(after)
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    message,
                ):
                    CONTROLLER.build_mutable_data_pair(before, after)

    def test_mutable_helper_is_descriptor_bound_and_asset_rebuild_is_empty(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        self.assertEqual(
            descriptor["release_input"]["datasets_on_asset_change"],
            [],
        )
        self.assertEqual(
            descriptor["mutable_data"]["helper_sha256"],
            descriptor["production_config"][
                "deployment_mutable_data_audit_sha256"
            ],
        )
        descriptor["mutable_data"]["helper_sha256"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "mutable-data helper differs",
        ):
            CONTROLLER.validate_descriptor(descriptor)

        pgpass = controller.config_dir / CONTROLLER.MUTABLE_DATA_PGPASS
        pgpass.write_text(
            (
                "127.0.0.1:55432:nexpoly:"
                "nexpoly_mutable_audit:changed\n"
            ),
            encoding="utf-8",
        )
        os.chmod(pgpass, 0o600)
        self.assertNotEqual(
            controller.mutable_data_contract(),
            descriptor["mutable_data"],
        )
        pgpass.unlink()
        pgpass.symlink_to(controller.config_dir / "app.env")
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "dependency is unsafe",
        ):
            controller.mutable_data_contract()

    def test_mutable_connection_rejects_indirection_and_wrong_audit_identity(
        self,
    ) -> None:
        passfile = Path("/private/mutable-data-audit.pgpass")
        canonical = (
            "[nexpoly-mutable-audit]\n"
            "host=127.0.0.1\n"
            "port=55432\n"
            "dbname=nexpoly\n"
            "user=nexpoly_mutable_audit\n"
            "sslmode=disable\n"
            f"passfile={passfile}\n"
        ).encode()
        pgpass = (
            b"127.0.0.1:55432:nexpoly:nexpoly_mutable_audit:secret\\:value\n"
        )
        self.assertEqual(
            CONTROLLER.validate_mutable_data_connection_inputs(
                canonical,
                pgpass,
                expected_passfile=passfile,
            )["user"],
            "nexpoly_mutable_audit",
        )
        for service in (
            canonical + b"include=/tmp/redirect.conf\n",
            canonical.replace(b"host=127.0.0.1", b"host=localhost"),
            canonical.replace(
                b"passfile=/private/mutable-data-audit.pgpass",
                b"servicefile=/tmp/other.conf",
            ),
        ):
            with self.subTest(service=service):
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "one exact loopback audit endpoint",
                ):
                    CONTROLLER.validate_mutable_data_connection_inputs(
                        service,
                        pgpass,
                        expected_passfile=passfile,
                    )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "does not match",
        ):
            CONTROLLER.validate_mutable_data_connection_inputs(
                canonical,
                b"127.0.0.1:55432:nexpoly:postgres:secret\n",
                expected_passfile=passfile,
            )

    def test_same_system_identifier_clone_cannot_satisfy_mutable_audit(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        expected = FakeLifecycle().postgres_runtime_identity(
            controller, descriptor
        )
        CONTROLLER.atomic_json(
            controller.marker_path,
            {
                "postgres_runtime_fence": expected,
            },
        )

        class CloneLifecycle(FakeLifecycle):
            def __init__(self) -> None:
                super().__init__()
                self.captures = 0

            def postgres_runtime_identity(
                self, target_controller: object, target_descriptor: object
            ) -> dict[str, object]:
                result = super().postgres_runtime_identity(
                    target_controller, target_descriptor
                )
                self.captures += 1
                if self.captures == 2:
                    result["container_id"] = "c" * 64
                return result

        controller.lifecycle = CloneLifecycle()
        with mock.patch.object(
            controller.runner,
            "run",
            return_value=subprocess.CompletedProcess(
                ["deployment-mutable-data-audit"],
                0,
                json.dumps(mutable_data_evidence()),
                "",
            ),
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "exact PostgreSQL container",
        ):
            CONTROLLER.PullDeployController._capture_mutable_data(
                controller,
                descriptor,
            )

    def test_bridge_source_switch_uses_exact_policy_ref_not_remote_main(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        commands: list[tuple[object, ...]] = []

        def fake_git(*arguments, **_kwargs):  # type: ignore[no-untyped-def]
            commands.append(arguments)
            if arguments[:3] == ("show-ref", "--verify", "--hash"):
                return subprocess.CompletedProcess([], 1, "", "")
            return subprocess.CompletedProcess([], 0, "", "")

        controller._git = fake_git  # type: ignore[method-assign]
        controller.repository_identity = lambda **_kwargs: {  # type: ignore[method-assign]
            "sha": TARGET_SHA,
            "tree": TARGET_TREE,
            "origin": CONTROLLER.REPOSITORY_SSH_URL,
        }
        with controller.deployment_lock():
            CONTROLLER.PullDeployController._switch_source(
                controller,
                descriptor,
            )
        merge = next(command for command in commands if command[0] == "merge")
        self.assertEqual(
            merge,
            ("merge", "--ff-only", f"refs/nexpoly/bridge-target/{TARGET_SHA}"),
        )

    def test_first_bridge_restore_inherits_lock_and_seals_every_cas(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        sealed = descriptor["legacy_takeover"]
        unit = descriptor["monomer_md"]["systemd_unit"]
        target_unit = Path(unit["target_path"])
        target_unit.write_bytes(Path(unit["candidate_path"]).read_bytes())
        os.chmod(target_unit, 0o600)
        status = {
            "apply_phase": "complete",
            "restore_phase": None,
            "active": False,
            "classification_sha256": sealed["classification_sha256"],
            "runtime_identity_sha256": sealed["runtime_identity_sha256"],
            "git_identity": sealed["git_identity"],
            "pre_stopped_fence_sha256": sealed[
                "pre_stopped_fence_sha256"
            ],
            "pre_stopped_fence": {
                "worker_unit_seal_sha256": "sha256:" + "2" * 64,
            },
            "control_layout_sha256": sealed[
                "control_layout_sha256"
            ],
            "checkout_permissions_sha256": sealed[
                "checkout_permissions_sha256"
            ],
            "applied_record_sha256": sealed[
                "applied_record_sha256"
            ],
        }
        restored = {
            **{
                key: sealed[key]
                for key in (
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
            "control_layout_replacement_sha256": "sha256:" + "3" * 64,
            "checkout_permissions_replacement_sha256": "sha256:" + "4" * 64,
            "restored_terminal_sha256": "sha256:" + "5" * 64,
            "binding_sha256": "sha256:" + "6" * 64,
        }
        marker = {
            "descriptor_sha256": CONTROLLER.sha256_bytes(
                CONTROLLER.canonical_json_bytes(descriptor) + b"\n"
            ),
            "database_change_started": False,
            "source_switched": True,
            "slot_switched": False,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": False,
            "runtime_stopped": True,
            "updated_at": CONTROLLER.utc_now(),
        }
        captured: dict[str, object] = {}

        def invoke(
            command: list[str],
            *,
            deploy_lock_fd: int,
        ) -> subprocess.CompletedProcess[str]:
            captured["command"] = command
            captured["fd"] = deploy_lock_fd
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"fixture": "terminal"}),
                "",
            )

        controller._held_deploy_lock_fd = 91
        controller._run_legacy_takeover_restore = invoke  # type: ignore[method-assign]
        with (
            mock.patch.object(
                controller,
                "_probe_restored_legacy_takeover",
                side_effect=[None, restored],
            ),
            mock.patch.object(
                CONTROLLER._legacy_takeover_evidence,
                "validate_install_manifest",
                return_value={"fixture": True},
            ),
            mock.patch.object(
                CONTROLLER._legacy_takeover_evidence,
                "load_status",
                return_value=status,
            ),
            mock.patch.object(
                CONTROLLER._legacy_takeover_evidence,
                "snapshot_current_control_layout",
                return_value={"sha256": "sha256:" + "3" * 64},
            ),
            mock.patch.object(
                CONTROLLER._legacy_takeover_evidence,
                "snapshot_current_checkout_permissions",
                return_value={"sha256": "sha256:" + "4" * 64},
            ),
            mock.patch.object(
                CONTROLLER._legacy_takeover_evidence,
                "validate_status_document",
                return_value={"fixture": "terminal"},
            ),
        ):
            result = controller._restore_legacy_takeover(
                descriptor,
                marker,
            )
        controller._held_deploy_lock_fd = None

        self.assertEqual(result, restored)
        self.assertEqual(captured["fd"], 91)
        command = captured["command"]
        self.assertEqual(command[-2:], ["--parent-deploy-lock-fd", "91"])
        self.assertEqual(
            marker["takeover_restore_started"],
            {
                "operation_id": sealed["operation_id"],
                "worker_unit_sha256": descriptor["monomer_md"][
                    "systemd_unit"
                ]["sha256"],
                "control_layout_sha256": "sha256:" + "3" * 64,
                "checkout_permissions_sha256": "sha256:" + "4" * 64,
                "started_at": marker["takeover_restore_started"][
                    "started_at"
                ],
            },
        )
        self.assertEqual(
            marker["takeover_restored_terminal_sha256"],
            restored["restored_terminal_sha256"],
        )
        self.assertFalse(marker["runtime_stopped"])

    def test_takeover_subprocess_receives_only_inherited_lock_fd(
        self,
    ) -> None:
        controller = self.controller()
        completed = subprocess.CompletedProcess(
            ["restore"],
            0,
            "{}",
            "",
        )
        with mock.patch.object(
            CONTROLLER.subprocess,
            "run",
            return_value=completed,
        ) as run:
            self.assertEqual(
                controller._run_legacy_takeover_restore(
                    ["/private/nexpoly-legacy-takeover", "restore"],
                    deploy_lock_fd=17,
                ),
                completed,
            )
        self.assertEqual(run.call_args.kwargs["pass_fds"], (17,))
        self.assertEqual(run.call_args.kwargs["env"]["PATH"], CONTROLLER.SAFE_PATH)
        self.assertNotIn("SSH_AUTH_SOCK", run.call_args.kwargs["env"])

    def test_lost_takeover_restore_response_precedes_git_reconciliation(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        descriptor_digest = self.bind_bridge_token(controller, descriptor)
        restored = {
            "restored_terminal_sha256": "sha256:" + "7" * 64,
        }
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": descriptor_digest,
            "executor_control": descriptor["controller"][
                "executor_control"
            ],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "failed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": False,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": False,
            "database_change_started": False,
        }
        self.bind_bridge_recovery_capsule(
            controller, descriptor, descriptor_digest, marker
        )
        CONTROLLER.atomic_json(controller.marker_path, marker)
        with (
            mock.patch.object(
                controller,
                "_load_prepared",
                return_value=(descriptor, descriptor_digest),
            ),
            mock.patch.object(
                controller,
                "_probe_restored_legacy_takeover",
                return_value=restored,
            ),
            mock.patch.object(
                controller,
                "_reconcile_effect_commit_windows",
                side_effect=AssertionError(
                    "Git reconciliation must not run after restore"
                ),
            ),
        ):
            with controller.deployment_lock():
                self.assertIsNone(controller.recover_interrupted())
        self.assertFalse(controller.marker_path.exists())
        terminal_dir = (
            controller.runtime_root
            / "legacy-takeover"
            / "runtime"
            / "pull-terminal"
            / OPERATION_ID
        )
        outcome = CONTROLLER.load_private_json(
            terminal_dir / "operation-state.json"
        )
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(
            controller._load_operation_state(OPERATION_ID),
            outcome,
        )
        self.assertFalse(
            (controller.audit_dir / OPERATION_ID).exists()
        )
        token = CONTROLLER._bridge_core.load_token_authority(
            controller.state_dir
        )
        self.assertEqual(token["status"], "retired-precommit")
        self.assertEqual(
            token["retirement"]["restored_terminal_sha256"],
            restored["restored_terminal_sha256"],
        )

    def test_failed_first_bridge_recovery_crash_matrix_preserves_order(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        descriptor_digest = self.bind_bridge_token(controller, descriptor)
        restored = {
            "restored_terminal_sha256": "sha256:" + "7" * 64,
        }
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": descriptor_digest,
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "failed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": False,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": False,
            "database_change_started": False,
        }
        self.bind_bridge_recovery_capsule(
            controller, descriptor, descriptor_digest, marker
        )
        CONTROLLER.atomic_json(controller.marker_path, marker)
        terminal_dir = (
            controller.runtime_root
            / "legacy-takeover"
            / "runtime"
            / "pull-terminal"
            / OPERATION_ID
        )
        audit_path = terminal_dir / "recovered-takeover-restore.json"
        state_path = terminal_dir / "operation-state.json"

        def recover_with_published_crash(method_name: str) -> None:
            original = getattr(controller, method_name)

            def publish_then_crash(*args, **kwargs):  # type: ignore[no-untyped-def]
                original(*args, **kwargs)
                raise RuntimeError(f"injected {method_name} response loss")

            with (
                mock.patch.object(
                    controller,
                    "_load_prepared",
                    return_value=(descriptor, descriptor_digest),
                ),
                mock.patch.object(
                    controller,
                    "_probe_restored_legacy_takeover",
                    return_value=restored,
                ),
                mock.patch.object(
                    controller,
                    method_name,
                    side_effect=publish_then_crash,
                ),
                self.assertRaisesRegex(RuntimeError, "response loss"),
                controller.deployment_lock(),
            ):
                controller.recover_interrupted()

        recover_with_published_crash("_finalize_restored_legacy_takeover")
        self.assertTrue(controller.marker_path.exists())
        self.assertFalse(audit_path.exists())
        self.assertFalse(state_path.exists())
        self.assertEqual(
            CONTROLLER._bridge_core.load_token_authority(
                controller.state_dir
            )["status"],
            "prepared",
        )

        recover_with_published_crash("_audit_attempt")
        self.assertTrue(audit_path.exists())
        self.assertFalse(state_path.exists())
        self.assertEqual(
            CONTROLLER._bridge_core.load_token_authority(
                controller.state_dir
            )["status"],
            "prepared",
        )

        recover_with_published_crash("_record_operation_outcome")
        self.assertTrue(state_path.exists())
        self.assertEqual(
            CONTROLLER._bridge_core.load_token_authority(
                controller.state_dir
            )["status"],
            "prepared",
        )

        recover_with_published_crash("_retire_failed_first_bridge_token")
        self.assertTrue(controller.marker_path.exists())
        self.assertEqual(
            CONTROLLER._bridge_core.load_token_authority(
                controller.state_dir
            )["status"],
            "retired-precommit",
        )

        with (
            mock.patch.object(
                controller,
                "_load_prepared",
                return_value=(descriptor, descriptor_digest),
            ),
            mock.patch.object(
                controller,
                "_probe_restored_legacy_takeover",
                return_value=restored,
            ),
            controller.deployment_lock(),
        ):
            self.assertIsNone(controller.recover_interrupted())
        self.assertFalse(controller.marker_path.exists())
        self.assertEqual(
            CONTROLLER._bridge_core.load_token_authority(
                controller.state_dir
            )["status"],
            "retired-precommit",
        )

    def test_restored_bridge_recovery_bypasses_only_checkout_permissions(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        descriptor_digest = self.bind_bridge_token(controller, descriptor)
        restored_terminal = "sha256:" + "7" * 64
        restored = {"restored_terminal_sha256": restored_terminal}
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": descriptor_digest,
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "failed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": False,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": False,
            "database_change_started": False,
        }
        capsule = self.bind_bridge_recovery_capsule(
            controller, descriptor, descriptor_digest, marker
        )
        CONTROLLER.atomic_json(controller.marker_path, marker)
        os.chmod(controller.production_root, 0o775)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "owner-controlled"
        ):
            controller.ensure_roots(mutating=True)

        with (
            mock.patch.object(
                controller,
                "_load_prepared",
                return_value=(descriptor, descriptor_digest),
            ),
            mock.patch.object(
                controller,
                "_probe_restored_legacy_takeover",
                return_value=restored,
            ),
        ):
            result = controller.recover_restored_first_bridge(
                authority_sha=descriptor["bridge"]["authority"]["sha"],
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
                capsule_sha256=capsule["capsule_sha256"],
                descriptor_sha256=descriptor_digest,
                restored_terminal_sha256=restored_terminal,
            )
        self.assertTrue(result["apply"])
        self.assertEqual(result["token_status"], "retired-precommit")
        self.assertFalse(controller.marker_path.exists())

    def test_restored_bridge_recovery_rejects_wrong_content_address(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        descriptor_digest = self.bind_bridge_token(controller, descriptor)
        restored_terminal = "sha256:" + "7" * 64
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": descriptor_digest,
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "failed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": False,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": False,
            "database_change_started": False,
        }
        capsule = self.bind_bridge_recovery_capsule(
            controller, descriptor, descriptor_digest, marker
        )
        CONTROLLER.atomic_json(controller.marker_path, marker)
        os.chmod(controller.production_root, 0o775)
        with (
            mock.patch.object(
                controller,
                "_load_prepared",
                return_value=(descriptor, descriptor_digest),
            ),
            mock.patch.object(
                controller,
                "_probe_restored_legacy_takeover",
                return_value={
                    "restored_terminal_sha256": restored_terminal
                },
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError, "terminal identity differs"
            ),
        ):
            controller.recover_restored_first_bridge(
                authority_sha=descriptor["bridge"]["authority"]["sha"],
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
                capsule_sha256=capsule["capsule_sha256"],
                descriptor_sha256=descriptor_digest,
                restored_terminal_sha256="sha256:" + "8" * 64,
            )
        self.assertTrue(controller.marker_path.exists())
        self.assertEqual(
            CONTROLLER._bridge_core.load_token_authority(
                controller.state_dir
            )["status"],
            "prepared",
        )

    def test_successor_generation_requires_untampered_failed_restore_chain(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        descriptor_digest = self.bind_bridge_token(controller, descriptor)
        restored = {
            "restored_terminal_sha256": "sha256:" + "7" * 64,
        }
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": descriptor_digest,
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "failed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": False,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": False,
            "database_change_started": False,
        }
        self.bind_bridge_recovery_capsule(
            controller, descriptor, descriptor_digest, marker
        )
        CONTROLLER.atomic_json(controller.marker_path, marker)
        with (
            mock.patch.object(
                controller,
                "_load_prepared",
                return_value=(descriptor, descriptor_digest),
            ),
            mock.patch.object(
                controller,
                "_probe_restored_legacy_takeover",
                return_value=restored,
            ),
            controller.deployment_lock(),
        ):
            controller.recover_interrupted()

        terminal_dir = (
            controller.runtime_root
            / "legacy-takeover"
            / "runtime"
            / "pull-terminal"
            / OPERATION_ID
        )
        audit_path = terminal_dir / "recovered-takeover-restore.json"
        original_audit = audit_path.read_bytes()
        operation, _descriptor_path, _ready_path = (
            controller._operation_paths(OPERATION_ID)
        )
        shutil.rmtree(operation)
        with (
            mock.patch.object(
                controller,
                "_probe_restored_legacy_takeover",
                return_value=restored,
            ),
        ):
            successor_authority = (
                controller._bridge_token_successor_authority()
            )
        self.assertIsNotNone(successor_authority)

        audit_path.write_bytes(original_audit.replace(b"recovered", b"tampered!", 1))
        os.chmod(audit_path, 0o600)
        with (
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError, "terminal audit authority differs"
            ),
        ):
            controller._bridge_token_successor_authority()
        audit_path.write_bytes(original_audit)
        os.chmod(audit_path, 0o600)

        successor = CONTROLLER._bridge_core.reserve_token(
            controller.state_dir,
            operation_id="deploy-20260716-0002",
            policy_id=descriptor["bridge"]["policy"]["policy_id"],
            token=b"bridge-token-fixture-entropy-0002",
            predecessor_retirement=successor_authority,
        )
        self.assertEqual(successor["generation"], 2)
        self.assertEqual(successor["status"], "reserved")

    def test_first_bridge_pre_stopped_rollback_never_drains_again(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        descriptor = self.bridge_descriptor(controller)
        marker = {
            "database_change_started": False,
            "runtime_stopped": True,
            "source_switched": False,
            "slot_switched": False,
            "control_switched": False,
            "unit_switched": False,
            "asset_switched": False,
            "updated_at": CONTROLLER.utc_now(),
        }
        with (
            mock.patch.object(
                controller,
                "_probe_restored_legacy_takeover",
                return_value=None,
            ),
            mock.patch.object(
                controller,
                "_reconcile_effect_commit_windows",
            ),
            mock.patch.object(
                controller,
                "_restore_previous_asset_pointer",
            ),
            mock.patch.object(
                controller,
                "_restore_legacy_takeover",
            ) as restore,
        ):
            controller._rollback_first_bridge(descriptor, marker)
        restore.assert_called_once_with(descriptor, marker)
        self.assertEqual(lifecycle.events, [])

    def test_bridge_commit_intent_crash_finishes_exact_current_state(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        # First build a structurally valid governed state using the existing
        # v2 fixture lifecycle; bridge recovery only changes its descriptor
        # authority before committing it through the v3 token.
        state = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        token = CONTROLLER._bridge_core.reserve_token(
            controller.state_dir,
            operation_id=OPERATION_ID,
            policy_id=descriptor["bridge"]["policy"]["policy_id"],
            token=b"bridge-token-fixture-entropy-0001",
        )
        descriptor["bridge"]["token"] = {
            "token_id": token["token_id"],
            "token_sha256": token["token_sha256"],
        }
        CONTROLLER.validate_descriptor(descriptor)
        descriptor_digest = CONTROLLER.sha256_bytes(
            CONTROLLER.canonical_json_bytes(descriptor) + b"\n"
        )
        CONTROLLER._bridge_core.bind_token_descriptor(
            controller.state_dir,
            operation_id=OPERATION_ID,
            policy_id=descriptor["bridge"]["policy"]["policy_id"],
            descriptor_sha256=descriptor_digest,
        )
        state["descriptor_sha256"] = descriptor_digest
        state["postgres_rehearsal"]["descriptor_sha256"] = descriptor_digest
        state["external_database_audit"] = descriptor[
            "external_database_audit"
        ]
        state["external_database_transition_chain"] = (
            CONTROLLER.build_external_database_transition_chain(
                alias_reference={
                    "path": "/runtime/audit/alias-transition.json",
                    "sha256": "sha256:" + "1" * 64,
                    "identity_sha256": "sha256:" + "2" * 64,
                    "before_state_sha256": "sha256:" + "3" * 64,
                    "after_state_sha256": "sha256:" + "4" * 64,
                    "descriptor_sha256": "sha256:" + "5" * 64,
                    "operation_id": "alias-0005-fixture",
                    "kind": "alias-0005-reconciliation",
                },
                bridge_reference={
                    "path": "/runtime/audit/bridge-transition.json",
                    "sha256": "sha256:" + "6" * 64,
                    "identity_sha256": "sha256:" + "7" * 64,
                    "before_state_sha256": "sha256:" + "4" * 64,
                    "after_state_sha256": state[
                        "external_database_audit"
                    ]["state_sha256"],
                    "descriptor_sha256": descriptor_digest,
                    "operation_id": OPERATION_ID,
                    "kind": "bridge-expand-to-0011",
                },
                active_binding=state["external_database_audit"],
            )
        )
        CONTROLLER.validate_current_deployment_state(state)
        candidate_digest = CONTROLLER.sha256_bytes(
            CONTROLLER.canonical_json_bytes(state) + b"\n"
        )
        CONTROLLER._bridge_core.begin_state_commit(
            controller.state_dir,
            operation_id=OPERATION_ID,
            descriptor_sha256=descriptor_digest,
            candidate_state_sha256=candidate_digest,
        )
        controller.current_state_path.unlink()
        marker = {
            "phase": "state-commit-started",
            "candidate_state": state,
            "candidate_state_sha256": candidate_digest,
        }
        with (
            mock.patch.object(
                controller,
                "_validate_external_database_state_provenance",
                side_effect=CONTROLLER.validate_current_deployment_state,
            ),
            mock.patch.object(
                controller,
                "_revalidate_bridge_candidate_database_state",
            ) as initial_revalidation,
            mock.patch.object(
                controller,
                "_consume_bridge_token",
                side_effect=OSError("lost token-consume response"),
            ),
            self.assertRaisesRegex(OSError, "lost token-consume response"),
        ):
            controller._candidate_current_state(
                descriptor,
                descriptor_digest,
                marker,
            )
        initial_revalidation.assert_called_once_with(
            descriptor, state, include_mutable=True
        )
        self.assertEqual(
            CONTROLLER._bridge_core.load_token_authority(
                controller.state_dir
            )["status"],
            "commit-intent",
        )
        self.assertEqual(
            CONTROLLER.sha256_file(controller.current_state_path),
            candidate_digest,
        )

        with (
            mock.patch.object(
                controller,
                "_validate_external_database_state_provenance",
                side_effect=CONTROLLER.validate_current_deployment_state,
            ),
            mock.patch.object(
                controller,
                "_revalidate_bridge_candidate_database_state",
                side_effect=CONTROLLER.PullDeployError(
                    "candidate mutable data changed before commit"
                ),
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "mutable data changed",
            ),
        ):
            controller._candidate_current_state(
                descriptor,
                descriptor_digest,
                marker,
            )
        self.assertEqual(
            CONTROLLER._bridge_core.load_token_authority(
                controller.state_dir
            )["status"],
            "commit-intent",
        )

        with (
            mock.patch.object(
                controller,
                "_validate_external_database_state_provenance",
                side_effect=CONTROLLER.validate_current_deployment_state,
            ),
            mock.patch.object(
                controller,
                "_revalidate_bridge_candidate_database_state",
            ) as revalidate_database,
        ):
            recovered = controller._candidate_current_state(
                descriptor,
                descriptor_digest,
                marker,
            )
        revalidate_database.assert_called_once_with(
            descriptor, state, include_mutable=True
        )
        self.assertEqual(recovered, state)
        self.assertEqual(
            recovered["external_database_audit"]["identity_sha256"],
            descriptor["external_database_audit"]["identity_sha256"],
        )
        self.assertEqual(
            CONTROLLER.sha256_file(controller.current_state_path),
            candidate_digest,
        )
        self.assertEqual(
            CONTROLLER._bridge_core.load_token_authority(
                controller.state_dir
            )["status"],
            "consumed",
        )

    def test_pending_contract_marker_blocks_every_code_deployment_command(self) -> None:
        controller = self.controller()
        CONTROLLER.atomic_json(
            controller.contract_marker_path,
            {"operation_id": "contract-0012-20260716"},
        )
        actions = (
            lambda: controller.plan(target_sha=TARGET_SHA, operation_id=OPERATION_ID),
            lambda: controller.prepare(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            ),
            lambda: controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID),
            lambda: controller.rollback(operation_id=OPERATION_ID),
        )
        for action in actions:
            with self.subTest(action=action):
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError, "0012 maintenance"
                ):
                    action()

    def test_active_controller_seals_once_then_hands_prepare_to_target_controller(
        self,
    ) -> None:
        controller = self.controller()
        captured: list[tuple[str, list[str], dict[str, str]]] = []

        def capture(path, argv, environment):  # type: ignore[no-untyped-def]
            captured.append((path, argv, environment))
            raise RuntimeError("exec captured")

        controller.control_environment = lambda: {}  # type: ignore[method-assign]
        with mock.patch.object(CONTROLLER.os, "execve", capture):
            with self.assertRaisesRegex(RuntimeError, "exec captured"):
                controller._handoff_prepare_to_target_controller(
                    target_sha=TARGET_SHA, operation_id=OPERATION_ID
                )
        handoff_path = controller.control_handoffs_dir / f"{OPERATION_ID}.json"
        first = CONTROLLER.load_private_json(handoff_path)
        first_digest = CONTROLLER.sha256_file(handoff_path)
        with mock.patch.object(CONTROLLER.os, "execve", capture):
            with self.assertRaisesRegex(RuntimeError, "exec captured"):
                controller._handoff_prepare_to_target_controller(
                    target_sha=TARGET_SHA, operation_id=OPERATION_ID
                )
        self.assertEqual(CONTROLLER.load_private_json(handoff_path), first)
        self.assertEqual(CONTROLLER.sha256_file(handoff_path), first_digest)
        self.assertEqual(len(captured), 2)
        for _path, argv, environment in captured:
            self.assertEqual(argv[:3], ["/usr/bin/python3", "-I", "-B"])
            self.assertEqual(
                Path(argv[3]).parent.name,
                first["executor_control"]["release_id"],
            )
            self.assertEqual(
                environment["NEXPOLY_PREPARE_HANDOFF_SHA256"], first_digest
            )

    def test_control_pointer_accepts_only_sealed_previous_or_candidate(self) -> None:
        controller = self.controller()
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.validate_descriptor(
            CONTROLLER.load_private_json(descriptor_path)
        )
        previous = controller.active_control_evidence()
        candidate = controller._activate_control(descriptor)
        self.assertEqual(candidate["generation"], previous["generation"] + 1)
        controller._restore_previous_control(descriptor)
        self.assertEqual(controller.active_control_evidence(), previous)
        foreign = dict(previous)
        foreign["operation_id"] = "foreign-controls-0001"
        CONTROLLER.atomic_json(controller.active_control_path, foreign)
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "neither sealed"):
            controller._activate_control(descriptor)

    def test_slot_recycling_detects_venv_python_via_bounded_proc_cmdline(self) -> None:
        controller = self.controller()
        slot_root = controller.venv_root / "md-a"
        binary = slot_root / "venv/bin/python"
        binary.parent.mkdir(parents=True, mode=0o700)
        binary.symlink_to(Path(sys.executable).resolve())
        process = subprocess.Popen(
            [str(binary), "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "inactive slot is still used",
            ):
                controller._assert_slot_not_running(slot_root)
        finally:
            process.terminate()
            process.wait(timeout=10)

    def test_prepare_operation_owner_is_published_atomically_and_recovers_staging(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        staging = operation.parent / f".{operation.name}.preparing"
        staging.mkdir(mode=0o700)
        with controller.deployment_lock():
            attempt = controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        self.assertEqual(attempt, 1)
        self.assertFalse(staging.exists())
        owner = CONTROLLER.load_private_json(
            operation / "prepare-owner.json"
        )
        self.assertEqual(owner["operation_id"], OPERATION_ID)

        crash_operation_id = "deploy-prepare-quarantine-crash"
        crash_operation, _descriptor, _ready = (
            controller._operation_paths(crash_operation_id)
        )
        crash_staging = (
            crash_operation.parent
            / f".{crash_operation.name}.preparing"
        )
        crash_staging.mkdir(mode=0o700)
        with (
            controller.deployment_lock(),
            mock.patch.object(
                CONTROLLER.shutil,
                "rmtree",
                side_effect=OSError("injected quarantine crash"),
            ),
            self.assertRaisesRegex(OSError, "quarantine crash"),
        ):
            controller._open_prepare_operation(
                crash_operation,
                operation_id=crash_operation_id,
                target_sha=TARGET_SHA,
            )
        self.assertTrue(
            (
                crash_staging.parent
                / f"{crash_staging.name}.discard"
            ).exists()
        )
        with controller.deployment_lock():
            self.assertEqual(
                controller._open_prepare_operation(
                    crash_operation,
                    operation_id=crash_operation_id,
                    target_sha=TARGET_SHA,
                ),
                1,
            )

        other_operation = "deploy-prepare-foreign-owner"
        foreign, _descriptor, _ready = controller._operation_paths(
            other_operation
        )
        foreign_staging = (
            foreign.parent / f".{foreign.name}.preparing"
        )
        foreign_staging.mkdir(mode=0o700)
        CONTROLLER.atomic_json(
            foreign_staging / "prepare-owner.json",
            {
                "schema_version": 1,
                "operation_id": "deploy-foreign-owner",
                "target_sha": TARGET_SHA,
                "controller_sha256": controller.controller_digest(),
                "created_at": CONTROLLER.utc_now(),
            },
        )
        with (
            controller.deployment_lock(),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "belongs to another operation",
            ),
        ):
            controller._open_prepare_operation(
                foreign,
                operation_id=other_operation,
                target_sha=TARGET_SHA,
            )

    def test_prepare_operation_accepts_only_exact_lost_rename_response(
        self,
    ) -> None:
        controller = self.controller()
        operation_id = "deploy-prepare-lost-rename"
        operation, _descriptor, _ready = controller._operation_paths(
            operation_id
        )
        original = CONTROLLER.rename_directory_noreplace
        lost = False

        def lose_response(source, target):  # type: ignore[no-untyped-def]
            nonlocal lost
            result = original(source, target)
            if target == operation and not lost:
                lost = True
                raise OSError("injected lost prepare rename response")
            return result

        with (
            controller.deployment_lock(),
            mock.patch.object(
                CONTROLLER,
                "rename_directory_noreplace",
                side_effect=lose_response,
            ),
        ):
            attempt = controller._open_prepare_operation(
                operation,
                operation_id=operation_id,
                target_sha=TARGET_SHA,
            )
        self.assertTrue(lost)
        self.assertEqual(attempt, 1)
        self.assertEqual(
            CONTROLLER.load_private_json(
                operation / "prepare-owner.json"
            )["operation_id"],
            operation_id,
        )

    def test_real_md_staging_recovers_ownerless_dirs_and_lost_cache_rename(
        self,
    ) -> None:
        identity_digest = "sha256:" + "9" * 64
        deploy_env = self.runtime / "config/deploy.env"
        write_private(
            deploy_env,
            deploy_env.read_text(encoding="utf-8")
            + (
                "NEXPOLY_WORKER_BASE_PYTHON_IDENTITY_SHA256="
                + identity_digest
                + "\n"
            ),
        )

        class WheelRunner:
            def run(
                self, command: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                if "download" in command:
                    destination = Path(command[command.index("--dest") + 1])
                    wheel = destination / "fixture-1.0-py3-none-any.whl"
                    wheel.write_bytes(b"sealed wheel\n")
                    os.chmod(wheel, 0o600)
                elif "venv" in command:
                    venv = Path(command[-1])
                    (venv / "bin").mkdir(parents=True, mode=0o700)
                    python = venv / "bin/python"
                    python.write_bytes(b"fixture python\n")
                    os.chmod(python, 0o700)
                return subprocess.CompletedProcess(command, 0, "", "")

        controller = self.controller(runner=WheelRunner())
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        lock_payload = (
            b"fixture==1.0 --hash=sha256:" + b"1" * 64 + b"\n"
        )
        lock_sha = CONTROLLER.sha256_bytes(lock_payload)
        cache_key = CONTROLLER.sha256_bytes(
            CONTROLLER.canonical_json_bytes(
                {
                    "worker_lock_sha256": lock_sha,
                    "base_python_identity_sha256": identity_digest,
                    "platform": sys.platform,
                }
            )
        )
        cache = controller.wheel_cache_dir / cache_key
        wheel_staging = (
            controller.wheel_cache_dir
            / f".{cache_key}.staging-{OPERATION_ID}"
        )
        wheel_staging.mkdir(mode=0o700)
        slot_staging = (
            controller.venv_root
            / f".md-a.preparing-{OPERATION_ID}"
        )
        slot_staging.mkdir(mode=0o700)
        original = CONTROLLER.rename_directory_noreplace
        lost = False

        def lose_cache_response(source, target):  # type: ignore[no-untyped-def]
            nonlocal lost
            result = original(source, target)
            if target == cache and not lost:
                lost = True
                raise OSError("injected lost wheel rename response")
            return result

        identity = {
            "resolved_path": str(Path(sys.executable).resolve()),
            "identity_sha256": identity_digest,
        }
        with (
            controller.deployment_lock(),
            mock.patch.object(
                controller,
                "_base_python_identity",
                return_value=identity,
            ),
            mock.patch.object(
                CONTROLLER,
                "shared_inspect_base_python_identity",
                return_value=identity,
            ),
            mock.patch.object(
                CONTROLLER,
                "rename_directory_noreplace",
                side_effect=lose_cache_response,
            ),
        ):
            record = (
                CONTROLLER.PullDeployController.prepare_md_slot(
                    controller,
                    operation_id=OPERATION_ID,
                    target_sha=TARGET_SHA,
                    target_tree=TARGET_TREE,
                    lock_payload=lock_payload,
                )
            )
        self.assertTrue(lost)
        self.assertTrue((cache / "READY.json").is_file())
        self.assertFalse(wheel_staging.exists())
        self.assertFalse(slot_staging.exists())
        self.assertFalse(
            (
                controller.venv_root
                / f"md-{record['slot']}"
                / ".preparing.json"
            ).exists()
        )
        self.assertEqual(
            record["wheel_inventory_sha256"],
            CONTROLLER.directory_inventory_digest(cache),
        )

    def test_real_md_staging_rejects_foreign_wheel_and_slot_owners(
        self,
    ) -> None:
        identity_digest = "sha256:" + "9" * 64
        deploy_env = self.runtime / "config/deploy.env"
        write_private(
            deploy_env,
            deploy_env.read_text(encoding="utf-8")
            + (
                "NEXPOLY_WORKER_BASE_PYTHON_IDENTITY_SHA256="
                + identity_digest
                + "\n"
            ),
        )
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        lock_payload = (
            b"fixture==1.0 --hash=sha256:" + b"1" * 64 + b"\n"
        )
        lock_sha = CONTROLLER.sha256_bytes(lock_payload)
        cache_key = CONTROLLER.sha256_bytes(
            CONTROLLER.canonical_json_bytes(
                {
                    "worker_lock_sha256": lock_sha,
                    "base_python_identity_sha256": identity_digest,
                    "platform": sys.platform,
                }
            )
        )
        wheel = (
            controller.wheel_cache_dir
            / f".{cache_key}.staging-{OPERATION_ID}"
        )
        wheel.mkdir(mode=0o700)
        CONTROLLER.atomic_json(
            wheel / ".owner.json",
            {
                "schema_version": 1,
                "operation_id": "deploy-foreign-owner",
                "wheel_cache_key": cache_key,
                "worker_lock_sha256": lock_sha,
                "base_python_identity_sha256": identity_digest,
            },
        )
        identity = {
            "resolved_path": str(Path(sys.executable).resolve()),
            "identity_sha256": identity_digest,
        }
        with (
            controller.deployment_lock(),
            mock.patch.object(
                controller,
                "_base_python_identity",
                return_value=identity,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "another operation",
            ),
        ):
            CONTROLLER.PullDeployController.prepare_md_slot(
                controller,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
                target_tree=TARGET_TREE,
                lock_payload=lock_payload,
            )

        shutil.rmtree(wheel)
        cache = controller.wheel_cache_dir / cache_key
        cache.mkdir(mode=0o700)
        owner = CONTROLLER.PullDeployController._wheel_cache_owner(
            operation_id=OPERATION_ID,
            wheel_cache_key=cache_key,
            worker_lock_sha256=lock_sha,
            base_python_identity_sha256=identity_digest,
        )
        CONTROLLER.atomic_json(cache / ".owner.json", owner)
        payload_path = cache / "fixture-1.0-py3-none-any.whl"
        payload_path.write_bytes(b"sealed wheel\n")
        os.chmod(payload_path, 0o600)
        payload = CONTROLLER.wheel_payload_inventory(cache)
        CONTROLLER.atomic_json(
            cache / "READY.json",
            {
                "schema_version": 1,
                "status": "ready",
                "operation_id": OPERATION_ID,
                "wheel_cache_key": cache_key,
                "worker_lock_sha256": lock_sha,
                "base_python_identity_sha256": identity_digest,
                "payload_file_count": len(payload["files"]),
                "payload_inventory_sha256": payload[
                    "inventory_sha256"
                ],
                "ready_at": CONTROLLER.utc_now(),
            },
        )
        slot_staging = (
            controller.venv_root
            / f".md-a.preparing-{OPERATION_ID}"
        )
        slot_staging.mkdir(mode=0o700)
        CONTROLLER.atomic_json(
            slot_staging / ".preparing.json",
            {
                "schema_version": 1,
                "operation_id": "deploy-foreign-owner",
                "slot": "a",
            },
        )
        with (
            controller.deployment_lock(),
            mock.patch.object(
                controller,
                "_base_python_identity",
                return_value=identity,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "slot staging belongs to another operation",
            ),
        ):
            CONTROLLER.PullDeployController.prepare_md_slot(
                controller,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
                target_tree=TARGET_TREE,
                lock_payload=lock_payload,
            )

    def test_prepare_abort_archives_operation_and_releases_inactive_slot(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        record = controller.prepare_md_slot(
            operation_id=OPERATION_ID,
            target_sha=TARGET_SHA,
            target_tree=TARGET_TREE,
            lock_payload=b"failed-prepare-lock\n",
        )
        os.chmod(controller.venv_root / f"md-{record['slot']}", 0o700)

        aborted = controller.abort_prepare(operation_id=OPERATION_ID)
        archive = Path(aborted["archive_path"])
        self.assertEqual(aborted["status"], "aborted")
        self.assertFalse(operation.exists())
        self.assertTrue(archive.is_dir())
        self.assertFalse(
            (controller.venv_root / f"md-{record['slot']}").exists()
        )
        self.assertFalse(
            (
                controller.slots_state_dir / f"md-{record['slot']}.json"
            ).exists()
        )
        journal = CONTROLLER.load_private_json(
            controller.prepare_aborts_dir / f"{OPERATION_ID}.json"
        )
        self.assertEqual(journal["phase"], "completed")
        self.assertEqual(
            journal["archive_inventory_sha256"],
            CONTROLLER.directory_inventory_digest(archive),
        )
        self.assertEqual(
            controller.abort_prepare(operation_id=OPERATION_ID)["status"],
            "already-aborted",
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "durable prepare-abort",
        ):
            controller._assert_operation_not_terminal(
                OPERATION_ID,
                action="prepare",
            )

        successor = "deploy-20260716-successor"
        successor_operation, _descriptor, _ready = controller._operation_paths(
            successor
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                successor_operation,
                operation_id=successor,
                target_sha=TARGET_SHA,
            )
        successor_record = controller.prepare_md_slot(
            operation_id=successor,
            target_sha=TARGET_SHA,
            target_tree=TARGET_TREE,
            lock_payload=b"successor-lock\n",
        )
        self.assertEqual(successor_record["slot"], record["slot"])

    def test_prepare_abort_recovers_slot_and_archive_response_loss(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        record = controller.prepare_md_slot(
            operation_id=OPERATION_ID,
            target_sha=TARGET_SHA,
            target_tree=TARGET_TREE,
            lock_payload=b"response-loss-lock\n",
        )
        os.chmod(controller.venv_root / f"md-{record['slot']}", 0o700)
        original_quarantine = (
            CONTROLLER._prepare_abort_renameat2_noreplace
        )
        lost_archive_response = False

        def lose_archive_response(
            source_parent_descriptor: int,
            source_name: str,
            target_parent_descriptor: int,
            target_name: str,
        ) -> None:
            nonlocal lost_archive_response
            original_quarantine(
                source_parent_descriptor,
                source_name,
                target_parent_descriptor,
                target_name,
            )
            if not lost_archive_response:
                lost_archive_response = True
                raise OSError("injected lost archive response")

        with mock.patch.object(
            CONTROLLER,
            "_prepare_abort_renameat2_noreplace",
            side_effect=lose_archive_response,
        ):
            result = controller.abort_prepare(operation_id=OPERATION_ID)
        self.assertTrue(lost_archive_response)
        self.assertEqual(result["status"], "aborted")
        self.assertFalse(operation.exists())
        self.assertTrue(Path(result["archive_path"]).is_dir())
        self.assertEqual(
            CONTROLLER.load_private_json(
                controller.prepare_aborts_dir / f"{OPERATION_ID}.json"
            )["phase"],
            "completed",
        )

    def test_prepare_abort_reseals_visible_archive_intent_before_replay(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )

        original_advance = controller._advance_prepare_abort

        def stop_before_archive_intent(
            journal: dict[str, object],
            phase: str,
            **updates: object,
        ) -> dict[str, object]:
            if phase == "operation-archive-intent":
                raise OSError("stop with durable slots-cleaned")
            return original_advance(journal, phase, **updates)

        with mock.patch.object(
            controller,
            "_advance_prepare_abort",
            side_effect=stop_before_archive_intent,
        ), self.assertRaisesRegex(OSError, "durable slots-cleaned"):
            controller.abort_prepare(operation_id=OPERATION_ID)

        journal_path = (
            controller.prepare_aborts_dir / f"{OPERATION_ID}.json"
        )
        durable = CONTROLLER.load_private_json(journal_path)
        self.assertEqual(durable["phase"], "slots-cleaned")
        visible = controller._validate_prepare_abort_journal(
            {**durable, "phase": "operation-archive-intent"},
            OPERATION_ID,
        )

        original_load = controller._load_prepare_abort_journal
        returned_visible = False

        def visible_parent_fsync_lost(
            operation_id: str,
        ) -> dict[str, object] | None:
            nonlocal returned_visible
            if not returned_visible:
                returned_visible = True
                return json.loads(json.dumps(visible))
            return original_load(operation_id)

        def second_power_loss(
            journal: dict[str, object],
            phase: str,
            **updates: object,
        ) -> dict[str, object]:
            if phase == "completed":
                raise OSError("second power loss before completed journal")
            return original_advance(journal, phase, **updates)

        with (
            mock.patch.object(
                controller,
                "_load_prepare_abort_journal",
                side_effect=visible_parent_fsync_lost,
            ),
            mock.patch.object(
                controller,
                "_advance_prepare_abort",
                side_effect=second_power_loss,
            ),
            self.assertRaisesRegex(OSError, "second power loss"),
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)

        self.assertTrue(returned_visible)
        self.assertFalse(operation.exists())
        self.assertEqual(
            CONTROLLER.load_private_json(journal_path)["phase"],
            "operation-archive-intent",
        )
        recovered = controller.abort_prepare(operation_id=OPERATION_ID)
        self.assertEqual(recovered["status"], "aborted")
        self.assertEqual(
            CONTROLLER.load_private_json(journal_path)["phase"],
            "completed",
        )

    def test_prepare_abort_reseals_visible_completed_terminal(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        controller.abort_prepare(operation_id=OPERATION_ID)
        journal_path = (
            controller.prepare_aborts_dir / f"{OPERATION_ID}.json"
        )
        completed = CONTROLLER.load_private_json(journal_path)
        durable_old = controller._validate_prepare_abort_journal(
            {
                **completed,
                "status": "aborting",
                "phase": "operation-archive-intent",
                "archive_inventory_sha256": None,
                "completed_at": None,
            },
            OPERATION_ID,
        )
        CONTROLLER.atomic_json(journal_path, durable_old)

        original_load = controller._load_prepare_abort_journal
        returned_visible = False

        def visible_completed(
            operation_id: str,
        ) -> dict[str, object] | None:
            nonlocal returned_visible
            if not returned_visible:
                returned_visible = True
                return json.loads(json.dumps(completed))
            return original_load(operation_id)

        original_reseal = controller._reseal_prepare_abort_journal
        second_loss = False

        def reseal_completed_then_lose_parent_response(
            journal: dict[str, object],
        ) -> dict[str, object]:
            nonlocal second_loss
            resealed = original_reseal(journal)
            if not second_loss:
                second_loss = True
                # Model the power-loss outcome in which the completed rename
                # was visible to this process but the older parent namespace
                # remains durable.
                CONTROLLER.atomic_json(journal_path, durable_old)
                raise OSError("second power loss inside completed reseal")
            return resealed

        with (
            mock.patch.object(
                controller,
                "_load_prepare_abort_journal",
                side_effect=visible_completed,
            ),
            mock.patch.object(
                controller,
                "_reseal_prepare_abort_journal",
                side_effect=reseal_completed_then_lose_parent_response,
            ),
            self.assertRaisesRegex(OSError, "second power loss"),
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)

        self.assertTrue(returned_visible)
        self.assertTrue(second_loss)
        self.assertEqual(
            CONTROLLER.load_private_json(journal_path),
            durable_old,
        )

        replay_completed_at = "2099-12-31T23:59:59Z"
        with mock.patch.object(
            CONTROLLER,
            "utc_now",
            return_value=replay_completed_at,
        ):
            replay = controller.abort_prepare(operation_id=OPERATION_ID)
        self.assertEqual(replay["status"], "aborted")
        resealed = CONTROLLER.load_private_json(journal_path)
        self.assertEqual(
            resealed,
            {**completed, "completed_at": replay_completed_at},
        )
        self.assertEqual(resealed["phase"], "completed")

    def test_prepare_abort_target_only_archive_reseals_move_namespaces(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        with mock.patch.object(
            controller,
            "_reconcile_prepare_abort_archive",
            side_effect=OSError("stop before archive move"),
        ), self.assertRaisesRegex(OSError, "before archive move"):
            controller.abort_prepare(operation_id=OPERATION_ID)

        journal = controller._load_prepare_abort_journal(OPERATION_ID)
        self.assertEqual(journal["phase"], "operation-archive-intent")
        archive = controller._ensure_prepare_abort_archive(journal)
        archived_operation = archive / "operation"
        os.rename(operation, archived_operation)

        original_fsync = CONTROLLER.os.fsync
        fsynced: set[tuple[int, int]] = set()
        expected_source_parent = (
            operation.parent.lstat().st_dev,
            operation.parent.lstat().st_ino,
        )
        expected_target_parent = (
            archive.lstat().st_dev,
            archive.lstat().st_ino,
        )
        original_inventory = CONTROLLER.directory_inventory_digest
        sealed_inventories: list[Path] = []

        def record_fsync(descriptor: int) -> None:
            metadata = os.fstat(descriptor)
            fsynced.add((metadata.st_dev, metadata.st_ino))
            original_fsync(descriptor)

        def record_inventory(
            path: Path,
            *,
            fsync_root: bool = False,
            **kwargs: object,
        ) -> str:
            if fsync_root:
                sealed_inventories.append(path)
            return original_inventory(
                path,
                fsync_root=fsync_root,
                **kwargs,
            )

        original_advance = controller._advance_prepare_abort

        def lose_completed_journal(
            current: dict[str, object],
            phase: str,
            **updates: object,
        ) -> dict[str, object]:
            if phase == "completed":
                self.assertIn(expected_source_parent, fsynced)
                self.assertIn(expected_target_parent, fsynced)
                self.assertIn(archived_operation, sealed_inventories)
                raise OSError("second loss before completed journal")
            return original_advance(current, phase, **updates)

        with (
            mock.patch.object(
                CONTROLLER.os,
                "fsync",
                side_effect=record_fsync,
            ),
            mock.patch.object(
                CONTROLLER,
                "directory_inventory_digest",
                side_effect=record_inventory,
            ),
            mock.patch.object(
                controller,
                "_advance_prepare_abort",
                side_effect=lose_completed_journal,
            ),
            self.assertRaisesRegex(OSError, "second loss"),
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)

        self.assertFalse(operation.exists())
        self.assertTrue(archived_operation.is_dir())
        self.assertEqual(
            controller._load_prepare_abort_journal(OPERATION_ID)["phase"],
            "operation-archive-intent",
        )
        self.assertEqual(
            controller.abort_prepare(operation_id=OPERATION_ID)["status"],
            "aborted",
        )

    def test_prepare_abort_reseals_pruned_dft_cache_ancestor_on_replay(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        owner = {
            "schema_version": 1,
            "operation_id": OPERATION_ID,
            "release_sha": TARGET_SHA,
            "source_tree": TARGET_TREE,
        }
        dft_root = controller.venv_root / "dft"
        cache = (
            dft_root
            / ".build-cache"
            / TARGET_SHA
            / OPERATION_ID
        )
        cache.mkdir(parents=True, mode=0o700)
        for parent in (
            dft_root,
            dft_root / ".build-cache",
            cache.parent,
            cache,
        ):
            os.chmod(parent, 0o700)
        CONTROLLER.atomic_json(
            cache / "owner.json",
            {
                **owner,
                "requirements_lock_sha256": "sha256:" + "1" * 64,
            },
        )
        write_private(cache / "partial-wheel", "partial\n")

        original_advance = controller._advance_prepare_abort

        def prune_then_lose_phase(
            journal: dict[str, object],
            phase: str,
            **updates: object,
        ) -> dict[str, object]:
            if phase == "slots-cleaned":
                cache.parent.rmdir()
                cache.parent.parent.rmdir()
                raise OSError("loss after cache parent pruning")
            return original_advance(journal, phase, **updates)

        with (
            mock.patch.object(
                controller,
                "_advance_prepare_abort",
                side_effect=prune_then_lose_phase,
            ),
            self.assertRaisesRegex(OSError, "cache parent pruning"),
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)
        self.assertFalse(cache.parent.exists())
        self.assertEqual(
            controller._load_prepare_abort_journal(OPERATION_ID)["phase"],
            "slot-cleanup-intent",
        )

        fsynced: set[tuple[int, int]] = set()
        expected_existing_ancestor = (
            dft_root.lstat().st_dev,
            dft_root.lstat().st_ino,
        )
        original_fsync = CONTROLLER.os.fsync

        def record_fsync(descriptor: int) -> None:
            metadata = os.fstat(descriptor)
            fsynced.add((metadata.st_dev, metadata.st_ino))
            original_fsync(descriptor)

        def lose_replayed_phase(
            journal: dict[str, object],
            phase: str,
            **updates: object,
        ) -> dict[str, object]:
            if phase == "slots-cleaned":
                self.assertIn(expected_existing_ancestor, fsynced)
                raise OSError("second loss before slots-cleaned journal")
            return original_advance(journal, phase, **updates)

        with (
            mock.patch.object(
                CONTROLLER.os,
                "fsync",
                side_effect=record_fsync,
            ),
            mock.patch.object(
                controller,
                "_advance_prepare_abort",
                side_effect=lose_replayed_phase,
            ),
            self.assertRaisesRegex(OSError, "second loss"),
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)

        self.assertEqual(
            controller._load_prepare_abort_journal(OPERATION_ID)["phase"],
            "slot-cleanup-intent",
        )
        self.assertEqual(
            controller.abort_prepare(operation_id=OPERATION_ID)["status"],
            "aborted",
        )

    def test_prepare_abort_archive_file_replay_rejects_fsync_path_swap(
        self,
    ) -> None:
        controller = self.controller()
        with controller.deployment_lock():
            controller._ensure_prepare_abort_roots()
        archive = controller.prepare_abort_archives_dir / "swap-fixture"
        archive.mkdir(mode=0o700)
        target = archive / "record.json"
        replacement = archive / ".replacement"
        write_private(target, "sealed payload\n")
        write_private(replacement, "replacement payload\n")
        expected = CONTROLLER.sha256_file(target)
        source = controller.control_handoffs_dir / "missing.json"
        real_fsync = os.fsync
        swapped = False

        def swap_before_fsync(descriptor: int) -> None:
            nonlocal swapped
            if not swapped:
                os.replace(replacement, target)
                swapped = True
            real_fsync(descriptor)

        with (
            mock.patch.object(
                CONTROLLER.os,
                "fsync",
                side_effect=swap_before_fsync,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "changed while flushing",
            ),
        ):
            with controller.deployment_lock():
                controller._archive_prepare_abort_file(
                    source=source,
                    target=target,
                    expected_sha256=expected,
                    label="swap fixture",
                )

        self.assertTrue(swapped)
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            "replacement payload\n",
        )

    def test_prepare_abort_directory_source_swap_fails_before_move(
        self,
    ) -> None:
        controller = self.controller()
        with controller.deployment_lock():
            controller._ensure_prepare_abort_roots()
        archive = controller.prepare_abort_archives_dir / "dir-swap-fixture"
        archive.mkdir(mode=0o700)
        source = controller.prepared_root / "held-directory"
        displaced = controller.prepared_root / ".held-directory-original"
        rogue = controller.prepared_root / ".held-directory-rogue"
        source.mkdir(mode=0o700)
        rogue.mkdir(mode=0o700)
        write_private(source / "payload", "sealed directory\n")
        write_private(rogue / "payload", "rogue directory\n")
        target = archive / "directory"
        expected = CONTROLLER.directory_inventory_digest(source)
        original_inventory = CONTROLLER.directory_inventory_digest
        swapped = False

        def inventory_then_swap(path: Path, **kwargs: object) -> str:
            nonlocal swapped
            digest = original_inventory(path, **kwargs)
            if (
                path == source
                and kwargs.get("root_descriptor") is not None
                and not swapped
            ):
                os.rename(source, displaced)
                os.rename(rogue, source)
                swapped = True
            return digest

        with (
            controller.deployment_lock(),
            mock.patch.object(
                CONTROLLER,
                "directory_inventory_digest",
                side_effect=inventory_then_swap,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "changed before archive move",
            ),
        ):
            controller._archive_prepare_abort_directory(
                source=source,
                target=target,
                expected_inventory_sha256=expected,
                label="held directory",
            )

        self.assertTrue(swapped)
        self.assertFalse(target.exists())
        self.assertEqual(
            CONTROLLER.directory_inventory_digest(displaced),
            expected,
        )
        self.assertEqual(
            (source / "payload").read_text(encoding="utf-8"),
            "rogue directory\n",
        )

    def test_prepare_abort_file_source_swap_fails_before_move(
        self,
    ) -> None:
        controller = self.controller()
        with controller.deployment_lock():
            controller._ensure_prepare_abort_roots()
        archive = controller.prepare_abort_archives_dir / "file-swap-fixture"
        archive.mkdir(mode=0o700)
        source = controller.control_handoffs_dir / "held-file.json"
        displaced = source.with_name(".held-file-original")
        rogue = source.with_name(".held-file-rogue")
        write_private(source, "sealed file\n")
        write_private(rogue, "rogue file\n")
        target = archive / "record.json"
        expected = CONTROLLER.sha256_file(source)
        original_validate = controller._validate_prepare_abort_held_file
        swapped = False

        def validate_then_swap(
            parent_descriptor: int,
            name: str,
            descriptor: int,
            expected_sha256: str,
            *,
            label: str,
        ) -> os.stat_result:
            nonlocal swapped
            sealed = original_validate(
                parent_descriptor,
                name,
                descriptor,
                expected_sha256,
                label=label,
            )
            if name == source.name and not swapped:
                os.rename(
                    source.name,
                    displaced.name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
                os.rename(
                    rogue.name,
                    source.name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
                swapped = True
            return sealed

        with (
            controller.deployment_lock(),
            mock.patch.object(
                controller,
                "_validate_prepare_abort_held_file",
                side_effect=validate_then_swap,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "changed before archive move",
            ),
        ):
            controller._archive_prepare_abort_file(
                source=source,
                target=target,
                expected_sha256=expected,
                label="held file",
            )

        self.assertTrue(swapped)
        self.assertFalse(target.exists())
        self.assertEqual(CONTROLLER.sha256_file(displaced), expected)
        self.assertEqual(source.read_text(encoding="utf-8"), "rogue file\n")

    def test_prepare_abort_archive_parent_fsync_uses_pinned_inode(
        self,
    ) -> None:
        controller = self.controller()
        with controller.deployment_lock():
            controller._ensure_prepare_abort_roots()
        parent = controller.prepare_abort_archives_dir / "pinned-parent"
        moved = controller.prepare_abort_archives_dir / "pinned-parent-held"
        parent.mkdir(mode=0o700)
        expected = (parent.lstat().st_dev, parent.lstat().st_ino)
        original_fsync = CONTROLLER.os.fsync
        swapped = False
        fsynced: list[tuple[int, int]] = []

        def swap_path_then_fsync(descriptor: int) -> None:
            nonlocal swapped
            metadata = os.fstat(descriptor)
            identity = (metadata.st_dev, metadata.st_ino)
            if identity == expected and not swapped:
                os.rename(parent, moved)
                parent.mkdir(mode=0o700)
                swapped = True
            fsynced.append(identity)
            original_fsync(descriptor)

        with (
            controller.deployment_lock(),
            mock.patch.object(
                CONTROLLER.os,
                "fsync",
                side_effect=swap_path_then_fsync,
            ),
        ):
            controller._fsync_prepare_abort_archive_namespace(parent)

        self.assertTrue(swapped)
        self.assertIn(expected, fsynced)
        self.assertEqual((moved.lstat().st_dev, moved.lstat().st_ino), expected)
        self.assertNotEqual(
            (parent.lstat().st_dev, parent.lstat().st_ino),
            expected,
        )

    def test_prepare_abort_retains_safe_journal_residue_for_successor(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
            controller._ensure_prepare_abort_roots()

        residues = (
            ("create", b"", False),
            ("write", b'{"partial":', False),
            ("file-fsync", b'{"partial":"complete"}\n', True),
            ("replace-before", b'{"phase":"intent"}\n', True),
        )
        residue_paths: list[Path] = []
        for index, (
            _fault,
            payload,
            durable_file,
        ) in enumerate(residues, 1):
            path = controller.prepare_aborts_dir / (
                f".{OPERATION_ID}.json.{index:032x}.tmp"
            )
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                if payload:
                    self.assertEqual(os.write(descriptor, payload), len(payload))
                if durable_file:
                    os.fsync(descriptor)
            finally:
                os.close(descriptor)
            residue_paths.append(path)

        aborted = controller.abort_prepare(operation_id=OPERATION_ID)
        self.assertEqual(aborted["status"], "aborted")
        self.assertTrue(all(path.is_file() for path in residue_paths))

        successor = "deploy-20260716-journal-residue-successor"
        prepared = controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=successor,
        )
        self.assertEqual(prepared["status"], "ready")
        self.assertTrue(all(path.is_file() for path in residue_paths))

    def test_prepare_abort_bounds_safe_atomic_journal_residues(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
            controller._ensure_prepare_abort_roots()
        for index in range(
            CONTROLLER.PREPARE_ABORT_JOURNAL_STAGING_MAX_FILES + 1
        ):
            residue = controller.prepare_aborts_dir / (
                f".{OPERATION_ID}.json.{index:032x}.tmp"
            )
            write_private(residue, "residue\n")

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "too many prepare-abort journal staging residues",
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)

        self.assertTrue(operation.is_dir())

    def test_prepare_abort_rejects_unsafe_atomic_journal_residue(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
            controller._ensure_prepare_abort_roots()
        residue = controller.prepare_aborts_dir / (
            f".{OPERATION_ID}.json.{'a' * 32}.tmp"
        )
        residue.symlink_to(operation / "prepare-owner.json")

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "journal staging is unavailable",
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)

        self.assertTrue(residue.is_symlink())
        self.assertFalse(
            (controller.prepare_aborts_dir / f"{OPERATION_ID}.json").exists()
        )

    def test_prepare_abort_rejects_unknown_journal_directory_entry(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
            controller._ensure_prepare_abort_roots()
        unknown = controller.prepare_aborts_dir / ".foreign.tmp"
        write_private(unknown, "foreign\n")

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "journal directory contains an unknown entry",
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)

        self.assertTrue(unknown.is_file())
        self.assertFalse(
            (controller.prepare_aborts_dir / f"{OPERATION_ID}.json").exists()
        )

    def test_prepare_abort_rejects_operation_bound_bridge_token_without_mutation(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        before = CONTROLLER.directory_inventory_digest(operation)
        policy_id = "sha256:" + "a" * 64
        CONTROLLER._bridge_core.reserve_token(
            controller.state_dir,
            operation_id=OPERATION_ID,
            policy_id=policy_id,
            token=b"t" * 32,
        )
        CONTROLLER._bridge_core.bind_token_descriptor(
            controller.state_dir,
            operation_id=OPERATION_ID,
            policy_id=policy_id,
            descriptor_sha256="sha256:" + "b" * 64,
        )
        token_before = (
            controller.bridge_token_path.read_bytes()
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "first-bridge recovery",
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)
        self.assertEqual(
            CONTROLLER.directory_inventory_digest(operation),
            before,
        )
        self.assertEqual(
            controller.bridge_token_path.read_bytes(),
            token_before,
        )
        self.assertFalse(
            (
                controller.prepare_aborts_dir / f"{OPERATION_ID}.json"
            ).exists()
        )

    def test_prepare_abort_rejects_active_owned_slot_without_intent(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        record = controller.prepare_md_slot(
            operation_id=OPERATION_ID,
            target_sha=TARGET_SHA,
            target_tree=TARGET_TREE,
            lock_payload=b"active-owned-slot\n",
        )
        os.chmod(controller.venv_root / f"md-{record['slot']}", 0o700)
        CONTROLLER.atomic_json(
            controller.active_slot_path,
            {
                "schema_version": CONTROLLER.ACTIVE_SLOT_SCHEMA_VERSION,
                "component": "monomer-md",
                "slot": record["slot"],
                "source_sha": record["source_sha"],
                "source_tree": record["source_tree"],
                "worker_lock_sha256": record["worker_lock_sha256"],
                "slot_record_sha256": CONTROLLER.worker_record_digest(record),
                "operation_id": OPERATION_ID,
                "activated_at": CONTROLLER.utc_now(),
            },
        )
        before = CONTROLLER.directory_inventory_digest(operation)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "active Worker slot",
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)
        self.assertEqual(
            CONTROLLER.directory_inventory_digest(operation),
            before,
        )
        self.assertFalse(controller.prepare_aborts_dir.exists())

    def test_prepare_abort_rejects_foreign_root_owner_before_intent(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        record = controller.prepare_md_slot(
            operation_id=OPERATION_ID,
            target_sha=TARGET_SHA,
            target_tree=TARGET_TREE,
            lock_payload=b"foreign-root-owner\n",
        )
        root = controller.venv_root / f"md-{record['slot']}"
        foreign_operation = "deploy-20260719-foreign-slot"
        CONTROLLER.atomic_json(
            root / ".preparing.json",
            {
                "schema_version": 1,
                "operation_id": foreign_operation,
                "slot": record["slot"],
            },
        )
        before = CONTROLLER.directory_inventory_digest(root)

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "belongs to another operation",
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)

        self.assertEqual(
            CONTROLLER.directory_inventory_digest(root),
            before,
        )
        self.assertEqual(
            CONTROLLER.load_private_json(root / ".preparing.json")[
                "operation_id"
            ],
            foreign_operation,
        )
        self.assertTrue(
            (
                controller.slots_state_dir / f"md-{record['slot']}.json"
            ).is_file()
        )
        self.assertFalse(controller.prepare_aborts_dir.exists())

    def test_prepare_abort_resume_rechecks_running_slot_before_rename(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        record = controller.prepare_md_slot(
            operation_id=OPERATION_ID,
            target_sha=TARGET_SHA,
            target_tree=TARGET_TREE,
            lock_payload=b"running-slot-resume\n",
        )
        root = controller.venv_root / f"md-{record['slot']}"
        binary = root / "venv/bin/python"
        binary.unlink()
        binary.symlink_to(Path(sys.executable).resolve())

        with (
            mock.patch.object(
                controller,
                "_reconcile_prepare_abort_staging",
                side_effect=OSError("injected abort interruption"),
            ),
            self.assertRaisesRegex(OSError, "abort interruption"),
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)
        journal_path = (
            controller.prepare_aborts_dir / f"{OPERATION_ID}.json"
        )
        self.assertEqual(
            CONTROLLER.load_private_json(journal_path)["phase"],
            "slot-cleanup-intent",
        )

        process = subprocess.Popen(
            [str(binary), "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "inactive slot is still used",
            ):
                controller.abort_prepare(operation_id=OPERATION_ID)
            self.assertTrue(root.is_dir())
            self.assertTrue(
                (
                    controller.slots_state_dir
                    / f"md-{record['slot']}.json"
                ).is_file()
            )
            self.assertEqual(
                CONTROLLER.load_private_json(journal_path)["phase"],
                "slot-cleanup-intent",
            )
        finally:
            process.terminate()
            process.wait(timeout=10)

        result = controller.abort_prepare(operation_id=OPERATION_ID)
        self.assertEqual(result["status"], "aborted")
        self.assertFalse(root.exists())

    def test_prepare_abort_current_state_cas_blocks_resume_after_intent(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        with (
            mock.patch.object(
                controller,
                "_reconcile_prepare_abort_staging",
                side_effect=OSError("injected abort interruption"),
            ),
            self.assertRaisesRegex(OSError, "abort interruption"),
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)
        journal = CONTROLLER.load_private_json(
            controller.prepare_aborts_dir / f"{OPERATION_ID}.json"
        )
        self.assertEqual(journal["phase"], "slot-cleanup-intent")
        CONTROLLER.atomic_json(
            controller.current_state_path,
            {"external": "unauthorized state transition"},
        )
        before = CONTROLLER.directory_inventory_digest(operation)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "current state sha256 CAS changed",
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)
        self.assertEqual(
            CONTROLLER.directory_inventory_digest(operation),
            before,
        )

    def test_prepare_abort_validates_selector_handoff_controller_owner(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            candidate = controller.prepare_control_release(
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
                target_tree=TARGET_TREE,
            )
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        previous_active = controller.active_control_evidence()
        handoff = {
            "schema_version": 1,
            "protocol_version": CONTROLLER._control_runtime.PROTOCOL_VERSION,
            "operation_id": OPERATION_ID,
            "target_sha": TARGET_SHA,
            "target_tree": TARGET_TREE,
            "previous_active_control": previous_active,
            "previous_active_control_sha256": (
                CONTROLLER.canonical_json_digest(previous_active)
            ),
            "executor_control": candidate,
            "executor_control_sha256": (
                CONTROLLER.canonical_json_digest(candidate)
            ),
            "created_at": CONTROLLER.utc_now(),
        }
        handoff_path = (
            controller.control_handoffs_dir / f"{OPERATION_ID}.json"
        )
        CONTROLLER.atomic_json(handoff_path, handoff)
        handoff_sha256 = CONTROLLER.sha256_file(handoff_path)
        result = controller.abort_prepare(operation_id=OPERATION_ID)
        journal = CONTROLLER.load_private_json(
            controller.prepare_aborts_dir / f"{OPERATION_ID}.json"
        )
        self.assertEqual(result["status"], "aborted")
        self.assertEqual(
            journal["control_handoff_sha256"],
            handoff_sha256,
        )
        self.assertFalse(handoff_path.exists())
        self.assertEqual(
            CONTROLLER.sha256_file(
                Path(result["archive_path"]) / "control-handoff.json"
            ),
            handoff_sha256,
        )
        self.assertEqual(journal["control_handoff_schema_version"], 1)
        self.assertEqual(
            journal["executor_control_sha256"],
            handoff["executor_control_sha256"],
        )

    def test_active_b_aborts_distinct_f_controller_with_same_prepare_abi(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        original_git_show = controller._git_show
        target_tree = "e" * 40
        target_payload = (
            original_git_show(TARGET_SHA, "scripts/pull_deploy_controller.py")
            + b"\n# distinct target-controller fixture\n"
        )
        target_controller_sha256 = CONTROLLER.sha256_bytes(target_payload)

        def target_git_show(_sha: str, relative: str) -> bytes:
            if relative == "scripts/pull_deploy_controller.py":
                return target_payload
            return original_git_show(TARGET_SHA, relative)

        with (
            controller.deployment_lock(),
            mock.patch.object(
                controller,
                "_git_show",
                side_effect=target_git_show,
            ),
        ):
            candidate = controller.prepare_control_release(
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
                target_tree=target_tree,
            )
            with mock.patch.object(
                controller,
                "controller_digest",
                return_value=target_controller_sha256,
            ):
                controller._open_prepare_operation(
                    operation,
                    operation_id=OPERATION_ID,
                    target_sha=TARGET_SHA,
                )
        self.assertNotEqual(
            target_controller_sha256,
            controller.controller_digest(),
        )
        previous_active = controller.active_control_evidence()
        handoff = {
            "schema_version": 1,
            "protocol_version": CONTROLLER._control_runtime.PROTOCOL_VERSION,
            "operation_id": OPERATION_ID,
            "target_sha": TARGET_SHA,
            "target_tree": target_tree,
            "previous_active_control": previous_active,
            "previous_active_control_sha256": (
                CONTROLLER.canonical_json_digest(previous_active)
            ),
            "executor_control": candidate,
            "executor_control_sha256": (
                CONTROLLER.canonical_json_digest(candidate)
            ),
            "created_at": CONTROLLER.utc_now(),
        }
        handoff_path = (
            controller.control_handoffs_dir / f"{OPERATION_ID}.json"
        )
        CONTROLLER.atomic_json(handoff_path, handoff)

        result = controller.abort_prepare(operation_id=OPERATION_ID)
        journal = CONTROLLER.load_private_json(
            controller.prepare_aborts_dir / f"{OPERATION_ID}.json"
        )
        self.assertEqual(result["status"], "aborted")
        self.assertEqual(
            journal["prepare_owner"]["controller_sha256"],
            target_controller_sha256,
        )
        self.assertEqual(journal["target_tree"], target_tree)

    def test_selector_subprocess_active_b2_aborts_partial_f_revision(
        self,
    ) -> None:
        controller = self.controller()
        revision_repo = self.root / "prepare-abort-revisions"
        revision_repo.mkdir(mode=0o700)

        def git(*arguments: str, text: bool = True) -> str | bytes:
            environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Prepare ABI Fixture",
                "GIT_AUTHOR_EMAIL": "prepare-abi@example.invalid",
                "GIT_COMMITTER_NAME": "Prepare ABI Fixture",
                "GIT_COMMITTER_EMAIL": "prepare-abi@example.invalid",
            }
            completed = subprocess.run(
                ["git", "-C", str(revision_repo), *arguments],
                env=environment,
                check=True,
                capture_output=True,
                text=text,
            )
            return completed.stdout

        git("init", "--quiet")
        source_manifest_path = REPOSITORY_ROOT / CONTROLLER.CONTROL_SOURCE_MANIFEST
        source_manifest = json.loads(
            source_manifest_path.read_text(encoding="utf-8")
        )
        for source in source_manifest["files"]:
            relative = Path(source["source"])
            destination = revision_repo / relative
            destination.parent.mkdir(
                parents=True,
                exist_ok=True,
                mode=0o700,
            )
            destination.write_bytes((REPOSITORY_ROOT / relative).read_bytes())
        manifest_destination = (
            revision_repo / CONTROLLER.CONTROL_SOURCE_MANIFEST
        )
        manifest_destination.parent.mkdir(
            parents=True,
            exist_ok=True,
            mode=0o700,
        )
        manifest_destination.write_bytes(source_manifest_path.read_bytes())

        controller_source_path = (
            revision_repo / "scripts/pull_deploy_controller.py"
        )
        b2_source = controller_source_path.read_text(encoding="utf-8")
        b2_source = b2_source.replace(
            "/data/lzq/gith/nexpoly-runtime",
            str(self.runtime),
        ).replace(
            "/data/lzq/gith/nexpoly",
            str(self.production),
        )
        test_root_override = (
            "\n\ndef _prepare_abi_fixture_test_root_mode("
            "*, runtime_root: Path, production_root: Path | None = None"
            ") -> bool:\n"
            "    del runtime_root, production_root\n"
            "    return True\n\n"
            "test_root_mode = _prepare_abi_fixture_test_root_mode\n"
        )
        test_root_marker = "\n\ndef clean_control_environment("
        self.assertIn(test_root_marker, b2_source)
        b2_source = b2_source.replace(
            test_root_marker,
            test_root_override + test_root_marker,
            1,
        )
        controller_source_path.write_text(b2_source, encoding="utf-8")
        git("add", ".")
        git("commit", "--quiet", "-m", "test: freeze B2 prepare ABI")
        b2_sha = str(git("rev-parse", "HEAD")).strip()
        b2_tree = str(git("rev-parse", "HEAD^{tree}")).strip()

        f_source = controller_source_path.read_text(encoding="utf-8")
        validation_point = (
            "        self._require_deploy_lock_for_staging()\n"
            '        owner_path = operation / "prepare-owner.json"\n'
        )
        self.assertIn(validation_point, f_source)
        f_source = f_source.replace(
            validation_point,
            (
                "        self._require_deploy_lock_for_staging()\n"
                "        target_sha = require_sha(\n"
                '            target_sha, "prepare ABI F target SHA"\n'
                "        )\n"
                '        owner_path = operation / "prepare-owner.json"\n'
            ),
            1,
        )
        controller_source_path.write_text(f_source, encoding="utf-8")
        git("add", "scripts/pull_deploy_controller.py")
        git("commit", "--quiet", "-m", "test: advance to F prepare producer")
        f_sha = str(git("rev-parse", "HEAD")).strip()
        f_tree = str(git("rev-parse", "HEAD^{tree}")).strip()
        self.assertNotEqual(b2_sha, f_sha)
        self.assertNotEqual(b2_tree, f_tree)

        def git_payload(revision: str, relative: str) -> bytes:
            payload = git("show", f"{revision}:{relative}", text=False)
            self.assertIsInstance(payload, bytes)
            return payload

        def build_release(
            revision: str,
            tree: str,
        ) -> tuple[dict[str, object], Path]:
            parsed_source = CONTROLLER._control_runtime.parse_source_manifest(
                git_payload(
                    revision,
                    CONTROLLER.CONTROL_SOURCE_MANIFEST,
                )
            )
            payloads = {
                source["name"]: git_payload(revision, source["source"])
                for source in parsed_source["files"]
            }
            identities = {
                name: {
                    "sha256": CONTROLLER.sha256_bytes(payload),
                    "size": len(payload),
                    "mode": 0o700,
                }
                for name, payload in payloads.items()
            }
            identity = {
                "schema_version": (
                    CONTROLLER._control_runtime.CONTROL_MANIFEST_SCHEMA_VERSION
                ),
                "protocol_version": CONTROLLER._control_runtime.PROTOCOL_VERSION,
                "source_sha": revision,
                "source_tree": tree,
                "compatibility": parsed_source["compatibility"],
                "entrypoints": parsed_source["entrypoints"],
                "files": identities,
            }
            release_id = CONTROLLER._control_runtime.release_identity(identity)
            manifest = {**identity, "release_id": release_id}
            release = controller.control_releases_dir / release_id
            release.mkdir(mode=0o700)
            for name, payload in payloads.items():
                path = release / name
                path.write_bytes(payload)
                os.chmod(path, 0o700)
            CONTROLLER.atomic_bytes(
                release / CONTROLLER._control_runtime.CONTROL_MANIFEST_NAME,
                CONTROLLER.canonical_json_bytes(manifest) + b"\n",
                mode=0o600,
            )
            observed, observed_root = (
                CONTROLLER._control_runtime.load_control_release(
                    self.runtime,
                    release_id,
                )
            )
            self.assertEqual(observed, manifest)
            self.assertEqual(observed_root, release)
            return manifest, release

        b2_manifest, b2_root = build_release(b2_sha, b2_tree)
        f_manifest, f_root = build_release(f_sha, f_tree)
        self.assertNotEqual(
            b2_manifest["files"]["pull_deploy_controller.py"]["sha256"],
            f_manifest["files"]["pull_deploy_controller.py"]["sha256"],
        )

        selector_path = (
            controller.bin_dir / "control_runtime_selector.py"
        )
        selector_path.write_bytes(
            (
                REPOSITORY_ROOT
                / "scripts/control_runtime_selector.py"
            ).read_bytes()
        )
        os.chmod(selector_path, 0o700)
        bootstrap_path = controller.state_dir / "bootstrap-control.json"
        bootstrap = CONTROLLER.load_private_json(bootstrap_path)
        bootstrap["immutable_files"][
            "control_runtime_selector.py"
        ] = CONTROLLER.sha256_file(selector_path)
        CONTROLLER.atomic_json(bootstrap_path, bootstrap)

        previous_active = controller.active_control_evidence()
        b2_active = {
            "schema_version": (
                CONTROLLER._control_runtime.ACTIVE_CONTROL_SCHEMA_VERSION
            ),
            "protocol_version": CONTROLLER._control_runtime.PROTOCOL_VERSION,
            "component": "deployment-controls",
            "generation": previous_active["generation"] + 1,
            "release_id": b2_manifest["release_id"],
            "source_sha": b2_sha,
            "source_tree": b2_tree,
            "manifest_sha256": CONTROLLER.sha256_file(
                b2_root
                / CONTROLLER._control_runtime.CONTROL_MANIFEST_NAME
            ),
            "operation_id": "bootstrap-controls-b2-abi",
            "previous_release_id": previous_active["release_id"],
            "activated_at": CONTROLLER.utc_now(),
        }
        CONTROLLER.atomic_json(controller.active_control_path, b2_active)
        self.assertEqual(
            CONTROLLER._control_runtime.load_active_control(self.runtime)[0],
            b2_active,
        )

        producer = (
            "import importlib.util, pathlib, sys\n"
            "controller_path = pathlib.Path(sys.argv[1])\n"
            "spec = importlib.util.spec_from_file_location("
            "'prepare_abi_f_controller', controller_path)\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "production = pathlib.Path(sys.argv[2])\n"
            "runtime = pathlib.Path(sys.argv[3])\n"
            "operation_id = sys.argv[4]\n"
            "target_sha = sys.argv[5]\n"
            "controller = module.PullDeployController("
            "production, runtime, apply=True)\n"
            "operation, _descriptor, _ready = "
            "controller._operation_paths(operation_id)\n"
            "with controller.deployment_lock():\n"
            "    controller._open_prepare_operation(\n"
            "        operation,\n"
            "        operation_id=operation_id,\n"
            "        target_sha=target_sha,\n"
            "    )\n"
        )
        produced = subprocess.run(
            [
                "/usr/bin/python3",
                "-I",
                "-B",
                "-c",
                producer,
                str(f_root / "pull_deploy_controller.py"),
                str(self.production),
                str(self.runtime),
                OPERATION_ID,
                f_sha,
            ],
            env={
                **os.environ,
                "NEXPOLY_ACTIVE_CONTROL_ROOT": str(f_root),
                "NEXPOLY_ACTIVE_CONTROL_RELEASE_ID": str(
                    f_manifest["release_id"]
                ),
            },
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(produced.returncode, 0, produced.stderr)
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        owner = CONTROLLER.load_private_json(
            operation / "prepare-owner.json"
        )
        self.assertEqual(owner["target_sha"], f_sha)
        self.assertEqual(
            owner["controller_sha256"],
            f_manifest["files"]["pull_deploy_controller.py"]["sha256"],
        )

        candidate = {
            "schema_version": (
                CONTROLLER._control_runtime.CONTROL_CANDIDATE_SCHEMA_VERSION
            ),
            "protocol_version": CONTROLLER._control_runtime.PROTOCOL_VERSION,
            "component": "deployment-controls",
            "release_id": f_manifest["release_id"],
            "source_sha": f_sha,
            "source_tree": f_tree,
            "manifest_sha256": CONTROLLER.sha256_file(
                f_root
                / CONTROLLER._control_runtime.CONTROL_MANIFEST_NAME
            ),
            "operation_id": OPERATION_ID,
            "prepared_at": CONTROLLER.utc_now(),
        }
        handoff = {
            "schema_version": 1,
            "protocol_version": CONTROLLER._control_runtime.PROTOCOL_VERSION,
            "operation_id": OPERATION_ID,
            "target_sha": f_sha,
            "target_tree": f_tree,
            "previous_active_control": b2_active,
            "previous_active_control_sha256": (
                CONTROLLER.canonical_json_digest(b2_active)
            ),
            "executor_control": candidate,
            "executor_control_sha256": (
                CONTROLLER.canonical_json_digest(candidate)
            ),
            "created_at": CONTROLLER.utc_now(),
        }
        handoff_path = (
            controller.control_handoffs_dir / f"{OPERATION_ID}.json"
        )
        CONTROLLER.atomic_json(handoff_path, handoff)

        aborted = subprocess.run(
            [
                "/usr/bin/python3",
                "-I",
                "-B",
                str(selector_path),
                "run",
                "deploy",
                "prepare-abort",
                "--operation-id",
                OPERATION_ID,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(aborted.returncode, 0, aborted.stderr)
        result = json.loads(aborted.stdout)
        self.assertEqual(result["status"], "aborted")
        archive = Path(result["archive_path"])
        self.assertFalse(operation.exists())
        self.assertFalse(handoff_path.exists())
        self.assertTrue((archive / "operation").is_dir())
        self.assertTrue((archive / "control-handoff.json").is_file())
        self.assertEqual(
            CONTROLLER._control_runtime.load_active_control(self.runtime)[0],
            b2_active,
        )

    def test_target_controls_without_prepare_abort_abi_are_rejected(
        self,
    ) -> None:
        controller = self.controller()
        source = json.loads(
            (
                REPOSITORY_ROOT / CONTROLLER.CONTROL_SOURCE_MANIFEST
            ).read_text(encoding="utf-8")
        )
        del source["compatibility"]["prepare_abort_abi_versions"]
        payload = CONTROLLER.canonical_json_bytes(source) + b"\n"
        with (
            controller.deployment_lock(),
            mock.patch.object(
                controller,
                "_git_show",
                return_value=payload,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "target control release manifest is invalid",
            ),
        ):
            controller.prepare_control_release(
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
                target_tree=TARGET_TREE,
            )

    def test_prepare_abort_archives_partial_wheel_staging_and_unblocks_key(
        self,
    ) -> None:
        identity_digest = "sha256:" + "9" * 64
        deploy_env = self.runtime / "config/deploy.env"
        write_private(
            deploy_env,
            deploy_env.read_text(encoding="utf-8")
            + (
                "NEXPOLY_WORKER_BASE_PYTHON_IDENTITY_SHA256="
                + identity_digest
                + "\n"
            ),
        )

        class WheelRunner:
            def run(
                self, command: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                if "download" in command:
                    destination = Path(command[command.index("--dest") + 1])
                    wheel = destination / "fixture-1.0-py3-none-any.whl"
                    wheel.write_bytes(b"sealed wheel\n")
                    os.chmod(wheel, 0o600)
                elif "venv" in command:
                    venv = Path(command[-1])
                    (venv / "bin").mkdir(parents=True, mode=0o700)
                    python = venv / "bin/python"
                    python.write_bytes(b"fixture python\n")
                    os.chmod(python, 0o700)
                return subprocess.CompletedProcess(command, 0, "", "")

        controller = self.controller(runner=WheelRunner())
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        lock_payload = (
            b"fixture==1.0 --hash=sha256:" + b"1" * 64 + b"\n"
        )
        lock_sha = CONTROLLER.sha256_bytes(lock_payload)
        cache_key = CONTROLLER.sha256_bytes(
            CONTROLLER.canonical_json_bytes(
                {
                    "worker_lock_sha256": lock_sha,
                    "base_python_identity_sha256": identity_digest,
                    "platform": sys.platform,
                }
            )
        )
        staging = (
            controller.wheel_cache_dir
            / f".{cache_key}.staging-{OPERATION_ID}"
        )
        staging.mkdir(mode=0o700)
        owner = controller._wheel_cache_owner(
            operation_id=OPERATION_ID,
            wheel_cache_key=cache_key,
            worker_lock_sha256=lock_sha,
            base_python_identity_sha256=identity_digest,
        )
        CONTROLLER.atomic_json(staging / ".owner.json", owner)
        partial = staging / "partial.whl"
        partial.write_bytes(b"partial wheel response\n")
        os.chmod(partial, 0o600)
        staging_inventory = CONTROLLER.directory_inventory_digest(staging)

        result = controller.abort_prepare(operation_id=OPERATION_ID)
        archived = (
            Path(result["archive_path"])
            / "wheel-staging"
            / cache_key.removeprefix("sha256:")
        )
        self.assertFalse(staging.exists())
        self.assertEqual(
            CONTROLLER.directory_inventory_digest(archived),
            staging_inventory,
        )

        successor = "deploy-20260716-wheel-successor"
        successor_operation, _descriptor, _ready = (
            controller._operation_paths(successor)
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                successor_operation,
                operation_id=successor,
                target_sha=TARGET_SHA,
            )
        identity = {
            "resolved_path": str(Path(sys.executable).resolve()),
            "identity_sha256": identity_digest,
        }
        with (
            controller.deployment_lock(),
            mock.patch.object(
                controller,
                "_base_python_identity",
                return_value=identity,
            ),
            mock.patch.object(
                CONTROLLER,
                "shared_inspect_base_python_identity",
                return_value=identity,
            ),
        ):
            record = CONTROLLER.PullDeployController.prepare_md_slot(
                controller,
                operation_id=successor,
                target_sha=TARGET_SHA,
                target_tree=TARGET_TREE,
                lock_payload=lock_payload,
            )
        self.assertEqual(record["wheel_cache_key"], cache_key)

    def test_prepare_abort_rejects_ownerless_tombstone_with_foreign_record(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        foreign = controller.prepare_md_slot(
            operation_id="deploy-foreign-slot-owner",
            target_sha=TARGET_SHA,
            target_tree=TARGET_TREE,
            lock_payload=b"foreign-record\n",
        )
        foreign_root = controller.venv_root / f"md-{foreign['slot']}"
        shutil.rmtree(foreign_root)
        record_path = (
            controller.slots_state_dir / f"md-{foreign['slot']}.json"
        )
        record_bytes = record_path.read_bytes()
        tombstone = (
            controller.venv_root
            / f".{foreign['slot']}.discard-{OPERATION_ID}"
        )
        tombstone.mkdir(mode=0o700)
        tombstone_inventory = CONTROLLER.directory_inventory_digest(
            tombstone
        )

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "ownerless Worker slot quarantine conflicts",
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)
        self.assertEqual(record_path.read_bytes(), record_bytes)
        self.assertEqual(
            CONTROLLER.directory_inventory_digest(tombstone),
            tombstone_inventory,
        )
        self.assertFalse(controller.prepare_aborts_dir.exists())

    def test_prepare_abort_rejects_staging_tamper_after_durable_intent(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        staging = (
            controller.prepared_root / f".{OPERATION_ID}.preparing"
        )
        staging.mkdir(mode=0o700)
        with (
            mock.patch.object(
                controller,
                "_reconcile_prepare_abort_staging",
                side_effect=OSError("injected intent interruption"),
            ),
            self.assertRaisesRegex(OSError, "intent interruption"),
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)
        tamper = staging / "foreign"
        tamper.write_bytes(b"appeared after intent\n")
        os.chmod(tamper, 0o600)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "changed after prepare-abort intent",
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)
        self.assertTrue(operation.is_dir())
        self.assertTrue(tamper.is_file())

    def test_prepare_abort_cas_deletes_and_archives_prepared_ref(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        ref_state: dict[str, str | None] = {"sha": TARGET_SHA}

        def observe(_ref: str) -> str | None:
            return ref_state["sha"]

        def git(*arguments: str, **_kwargs: object):
            self.assertEqual(
                arguments,
                (
                    "update-ref",
                    "--no-deref",
                    "-d",
                    f"refs/nexpoly/prepared/{OPERATION_ID}",
                    TARGET_SHA,
                ),
            )
            ref_state["sha"] = None
            return subprocess.CompletedProcess(arguments, 0, "", "")

        sealed: list[str] = []
        with (
            mock.patch.object(
                controller,
                "_observe_prepare_abort_prepared_ref",
                side_effect=observe,
            ),
            mock.patch.object(controller, "_git", side_effect=git),
            mock.patch.object(
                controller,
                "_fsync_prepare_abort_prepared_ref_backend",
                side_effect=lambda ref: sealed.append(ref),
            ),
        ):
            result = controller.abort_prepare(operation_id=OPERATION_ID)
        self.assertIsNone(ref_state["sha"])
        self.assertEqual(
            sealed,
            [f"refs/nexpoly/prepared/{OPERATION_ID}"],
        )
        self.assertEqual(
            CONTROLLER.load_private_json(
                Path(result["archive_path"]) / "prepared-ref.json"
            ),
            {
                "schema_version": 1,
                "operation_id": OPERATION_ID,
                "ref": f"refs/nexpoly/prepared/{OPERATION_ID}",
                "target_sha": TARGET_SHA,
            },
        )

    def test_prepare_abort_rejects_external_prepared_ref_symlink_before_cas_delete(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        seed_complete_private_git_layout(controller.production_root)
        external = self.root / "external-prepare-abort-refs"
        (external / "prepared").mkdir(parents=True, mode=0o700)
        sentinel = external / "prepared" / OPERATION_ID
        write_private(sentinel, "external-prepared-ref-sentinel\n")
        before = sentinel.read_bytes()
        (controller.production_root / ".git/refs/nexpoly").symlink_to(
            external,
            target_is_directory=True,
        )
        with (
            mock.patch.object(
                controller,
                "_observe_prepare_abort_prepared_ref",
                return_value=TARGET_SHA,
            ),
            mock.patch.object(
                controller,
                "_assert_source_successor_repository_state",
                return_value=None,
            ),
            mock.patch.object(controller, "_git") as git_call,
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "cannot be opened without links",
            ),
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)
        git_call.assert_not_called()
        self.assertEqual(sentinel.read_bytes(), before)

    def test_prepare_abort_reseals_already_absent_ref_before_completion(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        ref_state: dict[str, str | None] = {"sha": TARGET_SHA}

        def observe(_ref: str) -> str | None:
            return ref_state["sha"]

        def delete_ref(*_arguments: str, **_kwargs: object):
            ref_state["sha"] = None
            return subprocess.CompletedProcess([], 0, "", "")

        with (
            mock.patch.object(
                controller,
                "_observe_prepare_abort_prepared_ref",
                side_effect=observe,
            ),
            mock.patch.object(controller, "_git", side_effect=delete_ref),
            mock.patch.object(
                controller,
                "_fsync_prepare_abort_prepared_ref_backend",
                side_effect=OSError("first loss during ref fsync"),
            ),
            self.assertRaisesRegex(OSError, "first loss"),
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)

        self.assertIsNone(ref_state["sha"])
        self.assertEqual(
            controller._load_prepare_abort_journal(OPERATION_ID)["phase"],
            "operation-archive-intent",
        )
        sealed: list[str] = []
        original_advance = controller._advance_prepare_abort

        def lose_completed_journal(
            journal: dict[str, object],
            phase: str,
            **updates: object,
        ) -> dict[str, object]:
            if phase == "completed":
                self.assertEqual(
                    sealed,
                    [f"refs/nexpoly/prepared/{OPERATION_ID}"],
                )
                raise OSError("second loss before completed journal")
            return original_advance(journal, phase, **updates)

        with (
            mock.patch.object(
                controller,
                "_observe_prepare_abort_prepared_ref",
                side_effect=observe,
            ),
            mock.patch.object(controller, "_git") as unexpected_git,
            mock.patch.object(
                controller,
                "_fsync_prepare_abort_prepared_ref_backend",
                side_effect=lambda ref: sealed.append(ref),
            ),
            mock.patch.object(
                controller,
                "_advance_prepare_abort",
                side_effect=lose_completed_journal,
            ),
            self.assertRaisesRegex(OSError, "second loss"),
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)
        unexpected_git.assert_not_called()
        self.assertEqual(
            controller._load_prepare_abort_journal(OPERATION_ID)["phase"],
            "operation-archive-intent",
        )

        with mock.patch.object(
            controller,
            "_observe_prepare_abort_prepared_ref",
            side_effect=observe,
        ):
            recovered = controller.abort_prepare(operation_id=OPERATION_ID)
        self.assertEqual(recovered["status"], "aborted")

    def test_prepare_abort_initially_absent_ref_reseals_before_completion(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        ref = f"refs/nexpoly/prepared/{OPERATION_ID}"

        with (
            mock.patch.object(
                controller,
                "_observe_prepare_abort_prepared_ref",
                return_value=None,
            ),
            mock.patch.object(controller, "_git") as unexpected_git,
            mock.patch.object(
                controller,
                "_fsync_prepare_abort_prepared_ref_backend",
                side_effect=OSError("first loss during absent ref fsync"),
            ),
            self.assertRaisesRegex(OSError, "first loss"),
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)
        unexpected_git.assert_not_called()
        journal = controller._load_prepare_abort_journal(OPERATION_ID)
        self.assertIsNone(journal["prepared_ref"]["target_sha"])
        self.assertEqual(journal["phase"], "operation-archive-intent")

        sealed: list[str] = []
        original_advance = controller._advance_prepare_abort

        def lose_completed_journal(
            current: dict[str, object],
            phase: str,
            **updates: object,
        ) -> dict[str, object]:
            if phase == "completed":
                self.assertEqual(sealed, [ref])
                raise OSError("second loss before absent-ref completion")
            return original_advance(current, phase, **updates)

        with (
            mock.patch.object(
                controller,
                "_observe_prepare_abort_prepared_ref",
                return_value=None,
            ),
            mock.patch.object(controller, "_git") as unexpected_git,
            mock.patch.object(
                controller,
                "_fsync_prepare_abort_prepared_ref_backend",
                side_effect=lambda name: sealed.append(name),
            ),
            mock.patch.object(
                controller,
                "_advance_prepare_abort",
                side_effect=lose_completed_journal,
            ),
            self.assertRaisesRegex(OSError, "second loss"),
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)
        unexpected_git.assert_not_called()
        self.assertEqual(
            controller._load_prepare_abort_journal(OPERATION_ID)["phase"],
            "operation-archive-intent",
        )

        with (
            mock.patch.object(
                controller,
                "_observe_prepare_abort_prepared_ref",
                return_value=None,
            ),
            mock.patch.object(
                controller,
                "_fsync_prepare_abort_prepared_ref_backend",
            ) as final_fsync,
        ):
            recovered = controller.abort_prepare(operation_id=OPERATION_ID)
        self.assertEqual(recovered["status"], "aborted")
        final_fsync.assert_called_once_with(ref)

    def test_prepare_abort_missing_ref_handles_real_git_243_rc128(
        self,
    ) -> None:
        repository = self.root / "prepare-abort-real-git"
        repository.mkdir(mode=0o700)
        git_environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }

        def git(*arguments: str, check: bool = True):
            return subprocess.run(
                ["/usr/bin/git", *arguments],
                cwd=repository,
                env=git_environment,
                check=check,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        git("init", "--initial-branch=main")
        git("config", "user.name", "Prepare Abort Test")
        git("config", "user.email", "prepare-abort@example.invalid")
        tracked = repository / "tracked"
        tracked.write_text("prepared ref fixture\n", encoding="utf-8")
        git("add", "tracked")
        git("commit", "-m", "prepared ref fixture")
        commit_sha = git("rev-parse", "HEAD").stdout.strip()
        ref = f"refs/nexpoly/prepared/{OPERATION_ID}"
        controller = CONTROLLER.PullDeployController(
            repository,
            self.runtime,
            runner=CONTROLLER.SystemRunner(),
            lifecycle=FakeLifecycle(),
            apply=True,
        )
        with mock.patch.object(
            controller, "_git_trust_preflight", return_value=None
        ), mock.patch.object(
            controller, "_clean_environment", return_value=git_environment
        ):
            legacy = controller._git(
                "show-ref", "--verify", "--hash", ref, check=False
            )
            version = git("--version").stdout.strip()
            if version.startswith("git version 2.43."):
                self.assertEqual(legacy.returncode, 128)
            else:
                self.assertNotEqual(legacy.returncode, 0)
            self.assertIsNone(
                controller._observe_prepare_abort_prepared_ref(ref)
            )
            git("update-ref", ref, commit_sha)
            self.assertEqual(
                controller._observe_prepare_abort_prepared_ref(ref),
                commit_sha,
            )
            blob_sha = git("hash-object", "-w", "tracked").stdout.strip()
            git("update-ref", ref, blob_sha)
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "does not name a commit",
            ):
                controller._observe_prepare_abort_prepared_ref(ref)
            main_before = git("rev-parse", "refs/heads/main").stdout.strip()
            git("symbolic-ref", ref, "refs/heads/main")
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "must not be symbolic",
            ):
                controller._observe_prepare_abort_prepared_ref(ref)
            self.assertEqual(
                git("rev-parse", "refs/heads/main").stdout.strip(),
                main_before,
            )
            git("update-ref", "--no-deref", "-d", ref)
            git_dir = repository / ".git"
            flushed: list[Path] = []
            original_flush = controller._fsync_prepare_abort_git_path

            def record_flush(path: Path, *, directory: bool) -> None:
                flushed.append(path)
                original_flush(path, directory=directory)

            with (
                mock.patch.object(
                    controller,
                    "_git_trust_preflight",
                    return_value={},
                ),
                mock.patch.object(
                    controller,
                    "_fsync_prepare_abort_git_path",
                    side_effect=record_flush,
                ),
            ):
                controller._fsync_prepare_abort_prepared_ref_backend(ref)
            self.assertIn(git_dir / "refs", flushed)
            self.assertIn(git_dir, flushed)

    def test_target_prepare_forward_resumes_predecessor_abort_journal(
        self,
    ) -> None:
        controller = self.controller()
        prior_operation = "deploy-20260716-abort-recovery"
        operation, _descriptor, _ready = controller._operation_paths(
            prior_operation
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=prior_operation,
                target_sha=TARGET_SHA,
            )
        with mock.patch.object(
            controller,
            "_reconcile_prepare_abort_archive",
            side_effect=OSError("injected predecessor interruption"),
        ), self.assertRaisesRegex(OSError, "predecessor interruption"):
            controller.abort_prepare(operation_id=prior_operation)
        journal = controller._load_prepare_abort_journal(prior_operation)
        self.assertEqual(journal["phase"], "operation-archive-intent")

        recovered_prepare = controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )

        self.assertEqual(
            recovered_prepare["status"],
            "predecessor-abort-recovered",
        )
        recovered = controller._load_prepare_abort_journal(prior_operation)
        self.assertEqual(recovered["phase"], "completed")
        self.assertFalse(operation.exists())
        new_operation, _new_descriptor, _new_ready = (
            controller._operation_paths(OPERATION_ID)
        )
        self.assertFalse(new_operation.exists())
        prepared = controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        self.assertEqual(prepared["status"], "ready")

    def test_target_prepare_rejects_abort_drift_before_new_operation(
        self,
    ) -> None:
        controller = self.controller()
        prior_operation = "deploy-20260716-abort-drift"
        operation, _descriptor, _ready = controller._operation_paths(
            prior_operation
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=prior_operation,
                target_sha=TARGET_SHA,
            )
        with mock.patch.object(
            controller,
            "_reconcile_prepare_abort_archive",
            side_effect=OSError("injected predecessor interruption"),
        ), self.assertRaisesRegex(OSError, "predecessor interruption"):
            controller.abort_prepare(operation_id=prior_operation)
        tamper = operation / "foreign"
        write_private(tamper, "unsealed drift\n")

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "prepare operation changed",
        ):
            controller.prepare(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )
        new_operation, _new_descriptor, _new_ready = (
            controller._operation_paths(OPERATION_ID)
        )
        self.assertFalse(new_operation.exists())

    def test_target_prepare_rejects_unrelated_predecessor_before_abort_mutation(
        self,
    ) -> None:
        controller = self.controller()
        prior_operation = "deploy-20260716-unrelated-predecessor"
        operation, _descriptor, _ready = controller._operation_paths(
            prior_operation
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=prior_operation,
                target_sha=TARGET_SHA,
            )
        with mock.patch.object(
            controller,
            "_reconcile_prepare_abort_archive",
            side_effect=OSError("injected predecessor interruption"),
        ), self.assertRaisesRegex(OSError, "predecessor interruption"):
            controller.abort_prepare(operation_id=prior_operation)
        journal_path = (
            controller.prepare_aborts_dir / f"{prior_operation}.json"
        )
        journal_before = journal_path.read_bytes()
        operation_before = CONTROLLER.directory_inventory_digest(operation)
        git_permission = adopted_git_permission_takeover_fixture(
            controller.production_root,
            controller.runtime_root,
            source_sha=PREVIOUS_SHA,
            source_tree=PREVIOUS_TREE,
        )
        unit_permission = adopted_unit_permission_binding_fixture(
            git_permission,
            source_sha=TARGET_SHA,
            source_tree=TARGET_TREE,
        )
        controller._fixture_unit_permission_takeover = unit_permission  # type: ignore[attr-defined]
        ci = controller.ci_evidence(TARGET_SHA)
        raw_unit = {
            "source_tree": TARGET_TREE,
            "plan": {
                "delivery_gate": {
                    "remote_main": TARGET_SHA,
                    "ci": ci,
                }
            },
        }

        with mock.patch.object(
            controller,
            "_private_json_with_digest",
            return_value=(
                raw_unit,
                unit_permission["authority_file_sha256"],
            ),
        ), mock.patch.object(
            controller,
            "_abort_prepare_locked",
        ) as abort_locked, self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "predecessor prepare-abort identity differs",
        ):
            controller.prepare(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )

        abort_locked.assert_not_called()
        self.assertEqual(journal_path.read_bytes(), journal_before)
        self.assertEqual(
            CONTROLLER.directory_inventory_digest(operation),
            operation_before,
        )
        new_operation, _new_descriptor, _new_ready = (
            controller._operation_paths(OPERATION_ID)
        )
        self.assertFalse(new_operation.exists())

    def test_prepare_abort_rejects_prepared_ref_cas_drift_on_resume(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        ref_state: dict[str, str | None] = {"sha": TARGET_SHA}
        with (
            mock.patch.object(
                controller,
                "_observe_prepare_abort_prepared_ref",
                side_effect=lambda _ref: ref_state["sha"],
            ),
            mock.patch.object(
                controller,
                "_reconcile_prepare_abort_staging",
                side_effect=OSError("injected ref interruption"),
            ),
            self.assertRaisesRegex(OSError, "ref interruption"),
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)
        ref_state["sha"] = "f" * 40
        with (
            mock.patch.object(
                controller,
                "_observe_prepare_abort_prepared_ref",
                side_effect=lambda _ref: ref_state["sha"],
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "prepared Git ref CAS changed",
            ),
        ):
            controller.abort_prepare(operation_id=OPERATION_ID)
        self.assertTrue(operation.is_dir())
        self.assertEqual(ref_state["sha"], "f" * 40)

    def test_active_slot_uses_a_b_values_canonical_digest_and_no_current_symlink(
        self,
    ) -> None:
        controller = self.controller()
        record = controller.prepare_md_slot(
            operation_id=OPERATION_ID,
            target_sha=TARGET_SHA,
            target_tree=TARGET_TREE,
            lock_payload=b"fixture-lock\n",
        )
        descriptor = {
            "operation_id": OPERATION_ID,
            "monomer_md": {
                "slot": record["slot"],
                "slot_record": record,
                "slot_record_sha256": CONTROLLER.canonical_json_digest(record),
            },
        }
        active = controller._activate_slot(descriptor)
        self.assertIn(active["slot"], {"a", "b"})
        self.assertEqual(
            active["slot_record_sha256"], CONTROLLER.canonical_json_digest(record)
        )
        self.assertFalse((self.runtime / "worker-venvs/md-current").exists())
        self.assertEqual(controller.choose_inactive_slot(), "b")

    def test_prepare_seals_descriptor_and_apply_rejects_tampering(self) -> None:
        controller = self.controller()
        prepared = controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self.assertEqual(prepared["status"], "ready")
        descriptor_path = (
            self.runtime / "state/prepared" / OPERATION_ID / "descriptor.json"
        )
        descriptor = CONTROLLER.validate_descriptor(
            CONTROLLER.load_private_json(descriptor_path)
        )
        self.assertEqual(descriptor["repository"]["target_tree"], TARGET_TREE)
        descriptor["compose"]["sha256"] = "sha256:" + "0" * 64
        CONTROLLER.atomic_json(descriptor_path, descriptor)
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "READY record differs"):
            controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)

    def test_new_prepare_accepts_token_rotation_but_apply_rejects_same_operation_drift(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        token = self.runtime / "config/github-api-token"
        write_private(token, "rotated-token\n")
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "production configuration changed after prepare",
        ):
            controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)

        # A distinct operation may intentionally seal the rotated credential.
        next_operation = "deploy-20260716-0002"
        next_controller = self.controller()
        prepared = next_controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=next_operation,
        )
        self.assertEqual(prepared["status"], "ready")

    def test_slots_rotate_a_b_a(self) -> None:
        controller = self.controller()

        def prepare_and_activate(operation: str, payload: bytes) -> dict[str, object]:
            record = controller.prepare_md_slot(
                operation_id=operation,
                target_sha=TARGET_SHA,
                target_tree=TARGET_TREE,
                lock_payload=payload,
            )
            CONTROLLER.atomic_json(
                controller.active_slot_path,
                {
                    "schema_version": CONTROLLER.ACTIVE_SLOT_SCHEMA_VERSION,
                    "component": "monomer-md",
                    "slot": record["slot"],
                    "source_sha": TARGET_SHA,
                    "source_tree": TARGET_TREE,
                    "worker_lock_sha256": record["worker_lock_sha256"],
                    "slot_record_sha256": CONTROLLER.worker_record_digest(record),
                    "operation_id": operation,
                    "activated_at": CONTROLLER.utc_now(),
                },
            )
            return record

        first = prepare_and_activate("deploy-round-a1", b"one\n")
        second = prepare_and_activate("deploy-round-b2", b"two\n")
        third = prepare_and_activate("deploy-round-a3", b"three\n")
        self.assertEqual(
            [first["slot"], second["slot"], third["slot"]], ["a", "b", "a"]
        )


class StrictLifecycleEvidenceTests(unittest.TestCase):
    def test_acceptance_stability_verifier_is_read_only(self) -> None:
        lifecycle = CONTROLLER.SystemLifecycle()
        runtime_identity = {"repository": {"sha": TARGET_SHA}}
        recovery_fence = {"fixture_instance": "same-processes"}
        with (
            mock.patch.object(
                lifecycle,
                "verify_runtime_identity",
                return_value=runtime_identity,
            ) as runtime,
            mock.patch.object(
                lifecycle,
                "_capture_runtime_recovery_fence",
                return_value=recovery_fence,
            ) as fence,
            mock.patch.object(
                lifecycle,
                "_verify_candidate_image_inventory",
            ) as image_inventory,
            mock.patch.object(lifecycle, "verify") as mutating_verify,
        ):
            result = lifecycle.verify_acceptance_stability(
                SimpleNamespace(),
                {"operation_id": OPERATION_ID},
            )

        runtime.assert_called_once_with(
            mock.ANY,
            {"operation_id": OPERATION_ID},
            require_ingress=False,
        )
        fence.assert_called_once_with(
            mock.ANY,
            {"operation_id": OPERATION_ID},
            resumed=False,
        )
        image_inventory.assert_called_once_with(
            mock.ANY,
            {"operation_id": OPERATION_ID},
        )
        mutating_verify.assert_not_called()
        self.assertEqual(result["runtime_identity"], runtime_identity)
        self.assertEqual(result["recovery_fence"], recovery_fence)

    def test_final_acceptance_verifier_rejects_lost_web_image(self) -> None:
        lifecycle = CONTROLLER.SystemLifecycle()
        descriptor = {
            "operation_id": OPERATION_ID,
            "repository": {"target_sha": TARGET_SHA},
            "images": {
                role: image_record(role)
                for role in ("backend", "web")
            },
        }
        runner = mock.Mock()

        def inspect(command, **_kwargs):  # type: ignore[no-untyped-def]
            digest_ref = command[3]
            role = (
                "backend"
                if digest_ref == descriptor["images"]["backend"]["digest_ref"]
                else "web"
            )
            record = descriptor["images"][role]
            repo_digests = [record["digest_ref"]] if role == "backend" else []
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    [
                        {
                            "Id": record["image_id"],
                            "RepoDigests": repo_digests,
                            "Config": {
                                "Labels": {
                                    "org.opencontainers.image.revision": TARGET_SHA
                                }
                            },
                        }
                    ]
                ),
                "",
            )

        runner.run.side_effect = inspect
        controller = SimpleNamespace(
            runner=runner,
            control_environment=lambda: {},
        )
        with (
            mock.patch.object(
                lifecycle,
                "verify_runtime_identity",
                return_value={"repository": {"sha": TARGET_SHA}},
            ),
            mock.patch.object(
                lifecycle,
                "_capture_runtime_recovery_fence",
            ) as fence,
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "web immutable image identity changed",
            ),
        ):
            lifecycle.verify_acceptance_stability(controller, descriptor)

        self.assertEqual(runner.run.call_count, 2)
        fence.assert_not_called()

    def test_image_inventory_rejects_non_object_inspect_record(self) -> None:
        descriptor = {
            "repository": {"target_sha": TARGET_SHA},
            "images": {
                role: image_record(role)
                for role in ("backend", "web")
            },
        }
        runner = mock.Mock()
        runner.run.return_value = subprocess.CompletedProcess(
            ["docker", "image", "inspect"],
            0,
            "[null]",
            "",
        )
        controller = SimpleNamespace(
            runner=runner,
            control_environment=lambda: {},
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "backend immutable image inspection is malformed",
        ):
            CONTROLLER.SystemLifecycle._verify_candidate_image_inventory(
                controller,
                descriptor,
            )

    def test_observe_worker_guard_warning_keeps_runtime_ready(self) -> None:
        lifecycle = CONTROLLER.SystemLifecycle()
        descriptor = {
            "schema_version": CONTROLLER.DESCRIPTOR_SCHEMA_VERSION,
            "monomer_dft": {
                "runtime": {
                    "release_sha": TARGET_SHA,
                    "runtime_manifest_sha256": DIGEST_A,
                },
                "gpu": {
                    "index": "2",
                    "uuid": "GPU-" + "1" * 32,
                    "guard_mode": "observe",
                },
            },
        }
        worker = {
            "status": "ok",
            "runtime_ready": True,
            "release_sha": TARGET_SHA,
            "runtime_contract_sha256": DIGEST_A,
            "gpu_guard_mode": "observe",
            "gpu_guard_status": "stale",
            "gpu_contention_observed": False,
            "active_jobs": 0,
            "queued_jobs": 0,
            "accepting_jobs": False,
            "draining": True,
            "runtime": {
                "deployment": "prod",
                "physical_gpu": "2",
                "gpu_uuid": "GPU-" + "1" * 32,
                "gpu_guard_mode": "observe",
                "gpu_guard_status": "stale",
                "gpu_contention_observed": False,
                "fatal": False,
                "fatal_reason": None,
                "models": {
                    name: {"loaded": True, "warmed_up": True}
                    for name in CONTROLLER.MONOMER_DFT_MODEL_ALIASES
                },
            },
        }

        self.assertIs(
            lifecycle._validate_dft_runtime_identity(
                descriptor,
                worker,
                expected_accepting=False,
                allow_active=False,
                require_guard_readiness=False,
            ),
            worker,
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "runtime identity differs",
        ):
            lifecycle._validate_dft_runtime_identity(
                descriptor,
                worker,
                expected_accepting=False,
                allow_active=False,
                require_guard_readiness=True,
            )

    def test_test_root_mode_rejects_parent_child_overlap_with_production(self) -> None:
        with mock.patch.dict(os.environ, {"NEXPOLY_ALLOW_TEST_ROOT": "1"}):
            for runtime_root, production_root in (
                (CONTROLLER.RUNTIME_ROOT / "child", Path("/tmp/isolated-source")),
                (CONTROLLER.RUNTIME_ROOT.parent, Path("/tmp/isolated-source")),
                (Path("/tmp/isolated-runtime"), CONTROLLER.PRODUCTION_ROOT / "child"),
                (Path("/tmp/shared/root/runtime"), Path("/tmp/shared/root")),
            ):
                with (
                    self.subTest(
                        runtime_root=runtime_root, production_root=production_root
                    ),
                    self.assertRaisesRegex(
                        CONTROLLER.PullDeployError, "forbidden for production paths"
                    ),
                ):
                    CONTROLLER.test_root_mode(
                        runtime_root=runtime_root,
                        production_root=production_root,
                    )

    def active_payload(self, *, version: int = 1) -> dict[str, object]:
        fields = (
            CONTROLLER.ACTIVE_JOB_FIELDS_V1
            if version == 1
            else CONTROLLER.ACTIVE_JOB_FIELDS_V2
        )
        return {
            "active_jobs_schema_version": version,
            "drain": {
                "enabled": True,
                "reason": "fixture drain",
                "release_sha": TARGET_SHA,
                "activated_at": CONTROLLER.utc_now(),
                "activated_by": "pull-deploy-controller",
                "updated_at": CONTROLLER.utc_now(),
            },
            "active_jobs": {name: 0 for name in fields},
            "active_total": 0,
        }

    def persistent_payload(
        self, *, version: int | None = None
    ) -> dict[str, object]:
        selected = 1 if version is None else version
        fields = (
            CONTROLLER.PERSISTENT_JOB_FIELDS_V1
            if selected == 1
            else CONTROLLER.PERSISTENT_JOB_FIELDS_V2
        )
        payload: dict[str, object] = {
            "drain": {
                "enabled": False,
                "reason": None,
                "release_sha": None,
                "activated_at": None,
                "activated_by": None,
                "updated_at": CONTROLLER.utc_now(),
            },
            "active_jobs": {name: 0 for name in fields},
            "active_total": 0,
        }
        if version is not None:
            payload["active_jobs_schema_version"] = version
        return payload

    def test_persistent_jobs_accepts_legacy_v1_and_explicit_v2(self) -> None:
        legacy = self.persistent_payload()
        explicit_v1 = self.persistent_payload(version=1)
        explicit_v2 = self.persistent_payload(version=2)
        for payload in (legacy, explicit_v1, explicit_v2):
            with self.subTest(payload=payload):
                self.assertEqual(
                    CONTROLLER.validate_persistent_drain_evidence(payload),
                    payload,
                )

    def test_persistent_jobs_rejects_ambiguous_or_mismatched_versions(self) -> None:
        implicit_v2 = self.persistent_payload(version=2)
        implicit_v2.pop("active_jobs_schema_version")
        explicit_v1_with_v2 = self.persistent_payload(version=2)
        explicit_v1_with_v2["active_jobs_schema_version"] = 1
        unsupported = self.persistent_payload(version=2)
        unsupported["active_jobs_schema_version"] = 3
        for payload in (implicit_v2, explicit_v1_with_v2, unsupported):
            with self.subTest(payload=payload), self.assertRaises(
                CONTROLLER.PullDeployError
            ):
                CONTROLLER.validate_persistent_drain_evidence(payload)

    def test_active_jobs_rejects_unknown_categories_and_boolean_counts(self) -> None:
        payload = self.active_payload()
        payload["active_jobs"]["unexpected"] = 0  # type: ignore[index]
        with self.assertRaises(CONTROLLER.PullDeployError):
            CONTROLLER.validate_active_jobs_evidence(payload, require_drained=True)
        payload = self.active_payload()
        payload["active_jobs"]["monomer_md"] = False  # type: ignore[index]
        with self.assertRaises(CONTROLLER.PullDeployError):
            CONTROLLER.validate_active_jobs_evidence(payload, require_drained=True)

    def test_worker_resume_requires_accepting_zero_work(self) -> None:
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "did not resume"):
            CONTROLLER.validate_worker_control_evidence(
                {
                    "status": "ready",
                    "accepting_jobs": False,
                    "active_jobs": 0,
                    "worker_instance_id": "worker-1",
                },
                action="resume",
                require_zero=True,
            )

    def test_worker_unchanged_resume_allows_capacity_full_active_job(self) -> None:
        evidence = CONTROLLER.validate_worker_control_evidence(
            {
                "status": "ready",
                "accepting_jobs": False,
                "active_jobs": 1,
                "worker_instance_id": "worker-1",
            },
            action="resume-unchanged",
            require_zero=False,
        )
        self.assertEqual(evidence["active_jobs"], 1)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "did not resume unchanged"
        ):
            CONTROLLER.validate_worker_control_evidence(
                {
                    "status": "ready",
                    "accepting_jobs": False,
                    "active_jobs": 0,
                    "worker_instance_id": "worker-1",
                },
                action="resume-unchanged",
                require_zero=False,
            )

    def test_backend_resume_rejects_a_still_drained_response(self) -> None:
        payload = self.active_payload()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "differs from the expected admission",
        ):
            CONTROLLER.validate_active_jobs_evidence(
                payload,
                require_drained=False,
                require_resumed=True,
            )

    def test_transport_runtime_identity_is_exact_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="pull-worker-identity-"
        ) as raw:
            root = Path(raw)
            production = root / "source"
            python = root / "venv/bin/python"
            production.mkdir()
            python.parent.mkdir(parents=True)
            python.symlink_to(Path(sys.executable).resolve())
            controller = SimpleNamespace(production_root=production)
            slot_record = {
                "slot": "a",
                "venv_prefix": str(root / "venv"),
                "worker_lock_sha256": DIGEST_A,
                "base_python_identity_sha256": DIGEST_B,
            }
            descriptor = {
                "repository": {
                    "target_sha": TARGET_SHA,
                    "target_tree": TARGET_TREE,
                },
                "monomer_md": {
                    "slot_record": slot_record,
                    "slot_record_sha256": DIGEST_A,
                },
            }
            worker: dict[str, object] = {
                "status": "ok",
                "mode": "real",
                "source_sha": TARGET_SHA,
                "source_tree": TARGET_TREE,
                "source_root": str(production),
                "venv_slot": "a",
                "venv_prefix": str(root / "venv"),
                "worker_lock_sha256": DIGEST_A,
                "slot_record_sha256": DIGEST_A,
                "base_python_identity_sha256": DIGEST_B,
                "python_executable": str(python.resolve(strict=True)),
                "db_configured": True,
                "runtime_ready": True,
                "max_active_jobs": 3,
                "default_steps": 300,
                "max_steps": 300,
                "cuda_visible_devices": "2",
                "gpu_broker_enabled": False,
                "active_jobs": 0,
                "accepting_jobs": True,
                "draining": False,
                "worker_instance_id": "worker-fixture",
                "protocols": {
                    "Transport": {
                        "supported": True,
                        "runtime_ready": True,
                        "runtime_error": None,
                    }
                },
            }
            lifecycle = CONTROLLER.SystemLifecycle()
            self.assertIs(
                lifecycle._validate_worker_runtime_identity(
                    controller,
                    descriptor,
                    worker,
                    expected_accepting=True,
                ),
                worker,
            )

            for active_jobs, accepting_jobs in ((1, True), (2, True), (3, False)):
                with self.subTest(
                    active_jobs=active_jobs,
                    accepting_jobs=accepting_jobs,
                ):
                    active_worker = {
                        **worker,
                        "active_jobs": active_jobs,
                        "accepting_jobs": accepting_jobs,
                    }
                    self.assertIs(
                        lifecycle._validate_worker_runtime_identity(
                            controller,
                            descriptor,
                            active_worker,
                            expected_accepting=None,
                            allow_active=True,
                        ),
                        active_worker,
                    )
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "active-job state differs",
            ):
                lifecycle._validate_worker_runtime_identity(
                    controller,
                    descriptor,
                    {
                        **worker,
                        "active_jobs": 4,
                        "accepting_jobs": False,
                    },
                    expected_accepting=None,
                    allow_active=True,
                )

            invalid_protocols = (
                None,
                [],
                {},
                {"Transport": None},
                {"Transport": []},
                {
                    "Transport": {
                        "supported": False,
                        "runtime_ready": True,
                        "runtime_error": None,
                    }
                },
                {
                    "Transport": {
                        "supported": True,
                        "runtime_ready": False,
                        "runtime_error": None,
                    }
                },
                {
                    "Transport": {
                        "supported": True,
                        "runtime_ready": True,
                    }
                },
                {
                    "Transport": {
                        "supported": True,
                        "runtime_ready": True,
                        "runtime_error": "private runtime detail",
                    }
                },
            )
            for protocols in invalid_protocols:
                with (
                    self.subTest(protocols=protocols),
                    self.assertRaises(CONTROLLER.PullDeployError),
                ):
                    lifecycle._validate_worker_runtime_identity(
                        controller,
                        descriptor,
                        {**worker, "protocols": protocols},
                        expected_accepting=True,
                    )


class SystemDrainFencingTests(unittest.TestCase):
    class Harness(CONTROLLER.SystemLifecycle):
        def __init__(self) -> None:
            self.backend_statuses: list[dict[str, object]] = []
            self.backend_processes: list[dict[str, object]] = []
            self.socket_sets: list[list[tuple[str, Path]]] = []
            self.worker_health: list[dict[str, object]] = []
            self.identity_checks = 0

        @staticmethod
        def _next(values):  # type: ignore[no-untyped-def]
            if len(values) > 1:
                return values.pop(0)
            return values[0]

        def _backend_active_status(self, _controller, _descriptor):  # type: ignore[no-untyped-def]
            return self._next(self.backend_statuses)

        def _backend_process_identity(self, _controller, _descriptor):  # type: ignore[no-untyped-def]
            return self._next(self.backend_processes)

        def _worker_sockets(  # type: ignore[no-untyped-def]
            self, _controller, *, require_md=False, require_dft=False
        ):
            del require_md, require_dft
            return self._next(self.socket_sets)

        def _worker_request(self, _controller, _socket, *, method, endpoint):  # type: ignore[no-untyped-def]
            self.assert_request(method, endpoint)
            return self._next(self.worker_health)

        @staticmethod
        def assert_request(method: str, endpoint: str) -> None:
            if (method, endpoint) != ("GET", "/health"):
                raise AssertionError((method, endpoint))

        def _validate_worker_runtime_identity(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            del args, kwargs
            self.identity_checks += 1
            return {}

    @staticmethod
    def backend_status(
        active: int, *, actor: str = "pull-deploy-controller"
    ) -> dict[str, object]:
        counts = {name: 0 for name in CONTROLLER.ACTIVE_JOB_FIELDS_V1}
        counts["monomer_md"] = active
        return {
            "active_jobs_schema_version": 1,
            "drain": {
                "enabled": True,
                "reason": "fixture drain",
                "release_sha": TARGET_SHA,
                "activated_at": CONTROLLER.utc_now(),
                "activated_by": actor,
                "updated_at": CONTROLLER.utc_now(),
            },
            "active_jobs": counts,
            "active_total": active,
        }

    @staticmethod
    def worker_health(active: int) -> dict[str, object]:
        return {
            "status": "ok",
            "accepting_jobs": False,
            "draining": True,
            "active_jobs": active,
            "worker_instance_id": "worker-fixed",
        }

    @staticmethod
    def descriptor() -> dict[str, object]:
        return {"repository": {"target_sha": TARGET_SHA}}

    def configured(self) -> "SystemDrainFencingTests.Harness":
        harness = self.Harness()
        process = {
            "container_id": "a" * 64,
            "image_id": "sha256:" + "b" * 64,
            "pid": 123,
            "started_at": "2026-07-17T00:00:00Z",
            "restart_count": 0,
        }
        harness.backend_statuses = [
            self.backend_status(1),
            self.backend_status(0),
        ]
        harness.backend_processes = [process, process]
        harness.socket_sets = [
            [("monomer-md", Path("/fixture/md.sock"))],
            [("monomer-md", Path("/fixture/md.sock"))],
        ]
        harness.worker_health = [self.worker_health(1), self.worker_health(0)]
        return harness

    def test_wait_allows_work_to_finish_while_fencing_exact_instances(self) -> None:
        harness = self.configured()
        with mock.patch.object(CONTROLLER.time, "sleep", return_value=None):
            evidence = harness._wait_for_zero_work(
                object(),
                self.descriptor(),
                {"monomer-md": "worker-fixed"},
                harness.backend_processes[0],
            )
        self.assertEqual(evidence["backend"]["active_total"], 0)
        self.assertEqual(harness.identity_checks, 2)

    def test_wait_rejects_foreign_drain_owner_and_instance_or_socket_drift(
        self,
    ) -> None:
        harness = self.configured()
        harness.backend_statuses[0] = self.backend_status(1, actor="other")
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "ownership"):
            harness._wait_for_zero_work(
                object(),
                self.descriptor(),
                {"monomer-md": "worker-fixed"},
                harness.backend_processes[0],
            )

        harness = self.configured()
        harness.worker_health[1] = {
            **harness.worker_health[1],
            "worker_instance_id": "worker-restarted",
        }
        with (
            mock.patch.object(CONTROLLER.time, "sleep", return_value=None),
            self.assertRaisesRegex(CONTROLLER.PullDeployError, "instance changed"),
        ):
            harness._wait_for_zero_work(
                object(),
                self.descriptor(),
                {"monomer-md": "worker-fixed"},
                harness.backend_processes[0],
            )

        harness = self.configured()
        harness.backend_processes[1] = {**harness.backend_processes[0], "pid": 999}
        with (
            mock.patch.object(CONTROLLER.time, "sleep", return_value=None),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError, "Backend instance changed"
            ),
        ):
            harness._wait_for_zero_work(
                object(),
                self.descriptor(),
                {"monomer-md": "worker-fixed"},
                harness.backend_processes[0],
            )

        harness = self.configured()
        harness.socket_sets[1] = [
            ("monomer-md", Path("/fixture/md.sock")),
            ("monomer-dft", Path("/fixture/dft.sock")),
        ]
        with (
            mock.patch.object(CONTROLLER.time, "sleep", return_value=None),
            self.assertRaisesRegex(CONTROLLER.PullDeployError, "socket set changed"),
        ):
            harness._wait_for_zero_work(
                object(),
                self.descriptor(),
                {"monomer-md": "worker-fixed"},
                harness.backend_processes[0],
            )

    def test_resume_unchanged_accepts_full_capacity_without_restarting(self) -> None:
        process = {
            "container_id": "a" * 64,
            "image_id": "sha256:" + "b" * 64,
            "pid": 123,
            "started_at": "2026-07-17T00:00:00Z",
            "restart_count": 0,
        }
        worker_process = {
            "main_pid": 456,
            "invocation_id": "worker-invocation",
            "active_enter_monotonic": 789,
        }

        class ResumeHarness(CONTROLLER.SystemLifecycle):
            def __init__(self) -> None:
                self.requests: list[tuple[str, str]] = []
                self.health_reads = 0
                self.backend_reads = 0
                self.mutate_final = False
                self.control_called = False

            def _environment(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return {}

            def postgres_runtime_identity(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return {
                    "schema_version": 1,
                    **mutable_data_evidence()["postgres_runtime"],
                    "captured_at": CONTROLLER.utc_now(),
                }

            def _isolate_ingress(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return None

            def prepare_recovery_runtime(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return {
                    "runtime_state": "drained",
                    "ingress_isolated": True,
                    "verification": {
                        "health": "ok",
                        "recovery_fence": {
                            "backend_process": process,
                            "monomer_md_process": worker_process,
                            "workers": {
                                "monomer-md": {
                                    "socket": "/fixture/md.sock",
                                    "worker_instance_id": "worker-fixed",
                                }
                            },
                        },
                    },
                }

            def _backend_process_identity(self, _controller, _descriptor):  # type: ignore[no-untyped-def]
                self.backend_reads += 1
                if self.mutate_final and self.backend_reads >= 2:
                    return {**process, "pid": 999}
                return process

            def _worker_process_identity(self, _controller, _descriptor):  # type: ignore[no-untyped-def]
                return worker_process

            def _worker_sockets(  # type: ignore[no-untyped-def]
                self, _controller, *, require_md=False, require_dft=False
            ):
                del require_md, require_dft
                return [("monomer-md", Path("/fixture/md.sock"))]

            def _worker_request(self, _controller, _socket, *, method, endpoint):  # type: ignore[no-untyped-def]
                self.requests.append((method, endpoint))
                return {
                    "status": "ready",
                    "accepting_jobs": True,
                    "active_jobs": 0,
                    "worker_instance_id": "worker-fixed",
                }

            def _validate_worker_runtime_identity(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                return {}

            def verify_runtime_identity(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                return {}

            def _capture_runtime_recovery_fence(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                backend = self._backend_process_identity(None, None)
                return {
                    "backend_process": backend,
                    "monomer_md_process": worker_process,
                    "workers": {
                        "monomer-md": {
                            "socket": "/fixture/md.sock",
                            "worker_instance_id": "worker-fixed",
                        }
                    },
                }

            def admission_is_open(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return self.control_called

            def verify_open_runtime(self, _controller, _descriptor, verification):  # type: ignore[no-untyped-def]
                if (
                    self._expected_runtime_recovery_fence(verification)[
                        "backend_process"
                    ]
                    != process
                ):
                    raise AssertionError("unexpected final fence")

            def _control_cli(self, _controller, _descriptor, *arguments):  # type: ignore[no-untyped-def]
                self.assertEqual(arguments[0], "resume")
                self.control_called = True
                counts = {name: 0 for name in CONTROLLER.PERSISTENT_JOB_FIELDS_V1}
                return {
                    "drain": {
                        "enabled": False,
                        "reason": None,
                        "release_sha": None,
                        "activated_at": None,
                        "activated_by": None,
                        "updated_at": CONTROLLER.utc_now(),
                    },
                    "active_jobs": counts,
                    "active_total": 0,
                }

            @staticmethod
            def assertEqual(left, right):  # type: ignore[no-untyped-def]
                if left != right:
                    raise AssertionError((left, right))

        class Runner:
            @staticmethod
            def run(*args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                return subprocess.CompletedProcess([], 0, "", "")

        controller = type(
            "Controller",
            (),
            {
                "runner": Runner(),
                "production_root": Path("/fixture/source"),
                "config_dir": Path("/fixture/config"),
                "control_environment": lambda _self: {},
            },
        )()
        descriptor = {
            "repository": {"target_sha": TARGET_SHA},
            "_expected_postgres_runtime_identity": {
                "schema_version": 1,
                **mutable_data_evidence()["postgres_runtime"],
            },
        }
        harness = ResumeHarness()
        persisted: list[dict[str, object]] = []
        harness.resume_unchanged(controller, descriptor, persisted.append)
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["recovery_fence"]["backend_process"], process)
        self.assertEqual(
            harness.requests,
            [("POST", "/resume")],
        )
        self.assertTrue(harness.control_called)

        restarted = ResumeHarness()
        restarted.mutate_final = True
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "changed"):
            restarted.resume_unchanged(controller, descriptor, lambda _value: None)
        self.assertFalse(restarted.control_called)

    def test_open_runtime_recovery_rejects_instance_different_from_marker(self) -> None:
        expected = {
            "backend_process": {
                "container_id": "a" * 64,
                "image_id": "sha256:" + "b" * 64,
                "pid": 123,
                "started_at": "2026-07-17T00:00:00Z",
                "restart_count": 0,
            },
            "monomer_md_process": {
                "main_pid": 456,
                "invocation_id": "worker-invocation",
                "active_enter_monotonic": 789,
            },
            "workers": {
                "monomer-md": {
                    "socket": "/fixture/md.sock",
                    "worker_instance_id": "worker-fixed",
                }
            },
        }

        class OpenHarness(CONTROLLER.SystemLifecycle):
            def admission_is_open(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return True

            def verify_runtime_identity(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                return {
                    "containers": {"backend": {"container_id": "a" * 64}},
                    "worker": {"worker_instance_id": "worker-fixed"},
                }

            def _capture_runtime_recovery_fence(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                return {
                    **expected,
                    "backend_process": {
                        **expected["backend_process"],
                        "pid": 999,
                    },
                }

        class Runner:
            @staticmethod
            def run(*args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                return None

        controller = type(
            "Controller",
            (),
            {"runner": Runner(), "control_environment": lambda _self: {}},
        )()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "differs from committed verification"
        ):
            OpenHarness().verify_open_runtime(
                controller,
                {},
                {"recovery_fence": expected},
            )

    def test_resume_keeps_ingress_isolated_until_backend_and_fence_are_open(
        self,
    ) -> None:
        fence = {
            "backend_process": {
                "container_id": "a" * 64,
                "image_id": "sha256:" + "b" * 64,
                "pid": 123,
                "started_at": "2026-07-17T00:00:00Z",
                "restart_count": 0,
            },
            "monomer_md_process": {
                "main_pid": 456,
                "invocation_id": "worker-invocation",
                "active_enter_monotonic": 789,
            },
            "workers": {
                "monomer-md": {
                    "socket": "/fixture/md.sock",
                    "worker_instance_id": "worker-fixed",
                }
            },
        }

        class OrderingHarness(CONTROLLER.SystemLifecycle):
            def __init__(
                self,
                *,
                fail_final: bool = False,
                drift_postgres_after_ingress: bool = False,
            ) -> None:
                self.events: list[str] = []
                self.fail_final = fail_final
                self.drift_postgres_after_ingress = (
                    drift_postgres_after_ingress
                )
                self.backend_open = False
                self.postgres_reads = 0

            def _environment(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return {}

            def postgres_runtime_identity(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.postgres_reads += 1
                runtime = dict(mutable_data_evidence()["postgres_runtime"])
                if (
                    self.drift_postgres_after_ingress
                    and self.postgres_reads >= 3
                ):
                    runtime["container_id"] = "f" * 64
                return {
                    "schema_version": 1,
                    **runtime,
                    "captured_at": CONTROLLER.utc_now(),
                }

            def _isolate_ingress(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.events.append("isolate-ingress")

            def _capture_runtime_recovery_fence(self, *_args, resumed, **_kwargs):  # type: ignore[no-untyped-def]
                self.events.append(f"capture-{resumed}")
                return fence

            def _worker_sockets(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return [("monomer-md", Path("/fixture/md.sock"))]

            def _worker_request(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.events.append("worker-resume")
                return {
                    "status": "ready",
                    "accepting_jobs": True,
                    "active_jobs": 0,
                    "worker_instance_id": "worker-fixed",
                }

            def _control_cli(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.events.append("backend-resume")
                self.backend_open = True
                counts = {name: 0 for name in CONTROLLER.PERSISTENT_JOB_FIELDS_V1}
                return {
                    "drain": {
                        "enabled": False,
                        "reason": None,
                        "release_sha": None,
                        "activated_at": None,
                        "activated_by": None,
                        "updated_at": CONTROLLER.utc_now(),
                    },
                    "active_jobs": counts,
                    "active_total": 0,
                }

            def admission_is_open(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.events.append("admission-status")
                return self.backend_open

            def verify_runtime_identity(self, *_args, **kwargs):  # type: ignore[no-untyped-def]
                if kwargs.get("require_ingress") is not False:
                    raise AssertionError("internal verification exposed ingress")
                self.events.append("verify-internal")
                return {}

            def verify_open_runtime(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.events.append("verify-open")
                if self.fail_final:
                    raise CONTROLLER.PullDeployError(
                        "injected final open verification failure"
                    )

            def _compose(self, _controller, *arguments):  # type: ignore[no-untyped-def]
                return ["compose", *arguments]

        class Runner:
            def __init__(self, lifecycle: OrderingHarness) -> None:
                self.lifecycle = lifecycle

            def run(self, command, **_kwargs):  # type: ignore[no-untyped-def]
                if "up" in command and command[-1] == "nginx":
                    if not self.lifecycle.backend_open:
                        raise AssertionError("nginx started before Backend admission")
                    self.lifecycle.events.append("nginx-start")
                return subprocess.CompletedProcess(command, 0, "", "")

        descriptor = {
            "repository": {"target_sha": TARGET_SHA},
            "_expected_postgres_runtime_identity": {
                "schema_version": 1,
                **mutable_data_evidence()["postgres_runtime"],
            },
        }
        verification = {"health": "ok", "recovery_fence": fence}
        lifecycle = OrderingHarness()
        controller = type(
            "Controller",
            (),
            {
                "runner": Runner(lifecycle),
                "production_root": Path("/fixture/source"),
                "control_environment": lambda _self: {},
            },
        )()
        lifecycle.resume(controller, descriptor, verification)
        self.assertEqual(
            lifecycle.events,
            [
                "isolate-ingress",
                "capture-False",
                "worker-resume",
                "capture-True",
                "backend-resume",
                "admission-status",
                "verify-internal",
                "capture-True",
                "nginx-start",
                "verify-open",
            ],
        )

        failing = OrderingHarness(fail_final=True)
        failing_controller = type(
            "Controller",
            (),
            {
                "runner": Runner(failing),
                "production_root": Path("/fixture/source"),
                "control_environment": lambda _self: {},
            },
        )()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "final open verification"
        ):
            failing.resume(failing_controller, descriptor, verification)
        self.assertEqual(failing.events[-1], "isolate-ingress")

        drifting = OrderingHarness(drift_postgres_after_ingress=True)
        drifting_controller = type(
            "Controller",
            (),
            {
                "runner": Runner(drifting),
                "production_root": Path("/fixture/source"),
                "control_environment": lambda _self: {},
            },
        )()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "PostgreSQL identity changed while resuming ingress",
        ):
            drifting.resume(
                drifting_controller,
                descriptor,
                verification,
            )
        self.assertEqual(drifting.events[-1], "isolate-ingress")


class LifecycleStateMachineTests(PullDeployTestCase):
    def _prepare_legacy_v2(
        self,
        controller: FixtureController,
        operation_id: str,
    ) -> None:
        """Keep historical terminal-rollback tests outside ordinary V4."""

        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=operation_id,
        )
        _operation, descriptor_path, ready_path = controller._operation_paths(
            operation_id
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        self.assertEqual(
            descriptor["schema_version"], CONTROLLER.DESCRIPTOR_SCHEMA_VERSION
        )
        descriptor["schema_version"] = (
            CONTROLLER.LEGACY_DESCRIPTOR_SCHEMA_VERSION
        )
        descriptor["monomer_md"]["worker_env"] = descriptor["monomer_md"][
            "worker_env"
        ]["previous"]
        descriptor.pop("monomer_dft")
        descriptor.pop("adopted_deployment")
        descriptor.pop("adopted_deployment_sha256")
        CONTROLLER.validate_descriptor(descriptor)
        CONTROLLER.atomic_json(descriptor_path, descriptor)
        ready = CONTROLLER.load_private_json(ready_path)
        ready["descriptor_sha256"] = CONTROLLER.sha256_file(descriptor_path)
        CONTROLLER.atomic_json(ready_path, ready)
        controller._validate_ready(ready, descriptor, descriptor_path)

    @staticmethod
    def _probe_report_binding() -> dict[str, str]:
        return {
            "report_sha256": "sha256:" + "1" * 64,
            "file_sha256": "sha256:" + "2" * 64,
            "authority_sha256": "sha256:" + "3" * 64,
        }

    def _expire_acceptance_hold(self, controller: FixtureController) -> None:
        marker = CONTROLLER.load_private_json(controller.marker_path)
        evidence = marker.get("acceptance_evidence")
        if isinstance(evidence, dict):
            staged = CONTROLLER.dt.datetime(
                2026, 1, 1, tzinfo=CONTROLLER.dt.timezone.utc
            )
            pre_captured = staged + CONTROLLER.dt.timedelta(seconds=1)
            probes_completed = staged + CONTROLLER.dt.timedelta(seconds=2)
            verified = staged + CONTROLLER.dt.timedelta(seconds=3)
            wire = lambda value: value.isoformat().replace("+00:00", "Z")
            marker["acceptance_started_at"] = wire(staged)
            marker["acceptance_not_before"] = wire(
                staged
                + CONTROLLER.dt.timedelta(
                    seconds=CONTROLLER.ACCEPTANCE_HOLD_SECONDS
                )
            )
            authority_path = Path(marker["acceptance_authority_path"])
            authority = CONTROLLER.load_private_json(authority_path)
            authority["staged_at"] = marker["acceptance_started_at"]
            authority["acceptance_not_before"] = marker[
                "acceptance_not_before"
            ]
            CONTROLLER.atomic_json(authority_path, authority)
            marker["acceptance_authority_sha256"] = (
                CONTROLLER.sha256_file(authority_path)
            )
            probe_intent = marker["acceptance_probe_intent"]
            probe_intent["recorded_at"] = wire(pre_captured)
            probe_intent["pre_probe_mutable_data_evidence"][
                "captured_at"
            ] = wire(pre_captured)
            operation, _descriptor, _ready = controller._operation_paths(
                marker["operation_id"]
            )
            report_path = operation / (
                f"production-acceptance-{marker['operation_id']}.json"
            )
            report = CONTROLLER.load_private_json(report_path)
            report["authority"] = authority
            report["authority_sha256"] = marker[
                "acceptance_authority_sha256"
            ]
            report["started_at"] = wire(probes_completed)
            report["finished_at"] = wire(probes_completed)
            unsealed = dict(report)
            unsealed.pop("report_sha256", None)
            report["report_sha256"] = CONTROLLER.canonical_json_digest(
                unsealed
            )
            CONTROLLER.atomic_json(report_path, report)
            evidence["probe_report_sha256"] = report["report_sha256"]
            evidence["probe_report_file_sha256"] = (
                CONTROLLER.sha256_file(report_path)
            )
            evidence["probe_authority_sha256"] = marker[
                "acceptance_authority_sha256"
            ]
            evidence["probes_completed_at"] = wire(probes_completed)
            evidence["post_probe_mutable_data_evidence"][
                "captured_at"
            ] = wire(verified)
            evidence["verified_at"] = wire(verified)
            evidence["observation_started_at"] = evidence["verified_at"]
            evidence["observation_not_before"] = wire(
                verified
                + CONTROLLER.dt.timedelta(
                    seconds=CONTROLLER.ACCEPTANCE_HOLD_SECONDS
                )
            )
            CONTROLLER.atomic_json(controller.marker_path, marker)
            return
        marker["acceptance_started_at"] = "2026-01-01T00:00:00Z"
        marker["acceptance_not_before"] = "2026-01-01T00:15:00Z"
        authority_path = Path(marker["acceptance_authority_path"])
        authority = CONTROLLER.load_private_json(authority_path)
        authority["staged_at"] = marker["acceptance_started_at"]
        authority["acceptance_not_before"] = marker[
            "acceptance_not_before"
        ]
        CONTROLLER.atomic_json(authority_path, authority)
        marker["acceptance_authority_sha256"] = CONTROLLER.sha256_file(
            authority_path
        )
        CONTROLLER.atomic_json(controller.marker_path, marker)

    def _write_passing_probe_report(
        self, controller: FixtureController
    ) -> None:
        marker = CONTROLLER.load_private_json(controller.marker_path)
        authority = CONTROLLER.load_private_json(
            Path(marker["acceptance_authority_path"])
        )
        probe_timestamp = CONTROLLER.utc_now()
        report = {
            "schema_version": 1,
            "status": "passed",
            "operation_id": marker["operation_id"],
            "source_sha": marker["source_sha"],
            "authority": authority,
            "authority_sha256": marker["acceptance_authority_sha256"],
            "loopback_endpoint": "http://127.0.0.1:9000",
            "started_at": probe_timestamp,
            "finished_at": probe_timestamp,
            "sections": {
                name: {"status": "passed"}
                for name in ("dft", "md", "read_only_apis", "frontend")
            },
            "error": None,
        }
        report["report_sha256"] = CONTROLLER.canonical_json_digest(report)
        operation, _descriptor, _ready = controller._operation_paths(
            marker["operation_id"]
        )
        CONTROLLER.atomic_json(
            operation / f"production-acceptance-{marker['operation_id']}.json",
            report,
        )

    @staticmethod
    def _mutate_acceptance_history(
        snapshot: dict[str, object],
        *,
        generation: int,
        rows: int = 1,
    ) -> None:
        md_jobs = next(
            record
            for record in snapshot["business_tables"]
            if (record["schema"], record["table"])
            == ("md", "monomer_md_jobs")
        )
        md_jobs["row_count"] += rows
        md_jobs["content_sha256"] = (
            "sha256:" + f"{generation % 16:x}" * 64
        )
        bridge = snapshot["bridge_projection"]
        bridge["row_count"] += rows
        bridge["content_sha256"] = (
            "sha256:" + f"{(generation + 1) % 16:x}" * 64
        )
        reseal_mutable_data_evidence(snapshot)

    @staticmethod
    def _refresh_acceptance_drain(
        snapshot: dict[str, object],
        *,
        generation: int,
        reason: str,
    ) -> None:
        control = snapshot["governed_controls"]["deployment_control"]
        control["table"]["content_sha256"] = (
            "sha256:" + f"{generation % 16:x}" * 64
        )
        control["row"]["reason"] = reason
        control["row"]["updated_at"] = (
            f"2026-07-17T00:{generation % 60:02d}:00Z"
        )
        reseal_mutable_data_evidence(snapshot)

    def _rejected_acceptance_with_previous(
        self,
        lifecycle: FakeLifecycle,
        *,
        operation_id: str,
    ) -> tuple[FixtureController, dict[str, object]]:
        """Build a staged rejection whose rollback has governed old state."""

        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        previous = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=operation_id,
        )
        controller.apply_staged(
            target_sha=TARGET_SHA,
            operation_id=operation_id,
        )
        self._write_passing_probe_report(controller)
        controller.accept(
            target_sha=TARGET_SHA,
            operation_id=operation_id,
        )
        self._expire_acceptance_hold(controller)
        original_fence = json.loads(json.dumps(lifecycle.recovery_fence))
        lifecycle.recovery_fence["fixture_instance"] = "replacement-instance"
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "stability changed",
        ):
            controller.accept(
                target_sha=TARGET_SHA,
                operation_id=operation_id,
            )
        lifecycle.recovery_fence = original_fence
        controller._rollback_failed_attempt = (  # type: ignore[method-assign]
            CONTROLLER.PullDeployController._rollback_failed_attempt.__get__(
                controller,
                CONTROLLER.PullDeployController,
            )
        )
        return controller, previous

    @staticmethod
    def _acceptance_runner_descriptor() -> dict[str, object]:
        return {
            "operation_id": OPERATION_ID,
            "repository": {"target_sha": TARGET_SHA},
            "images": {
                "web": {
                    "digest_ref": f"ghcr.io/lzq2514/nexpoly-web@{DIGEST_A}",
                    "image_id": "sha256:" + "b" * 64,
                }
            },
            "monomer_dft": {"gpu": {"uuid": "GPU-" + "1" * 32}},
        }

    def test_acceptance_proxy_rejects_non_loopback_port_binding(self) -> None:
        lifecycle = CONTROLLER.SystemLifecycle()
        descriptor = self._acceptance_runner_descriptor()
        record = {
            "Id": "1" * 64,
            "Name": f"/nexpoly-acceptance-{OPERATION_ID}",
            "Image": descriptor["images"]["web"]["image_id"],
            "Config": {
                "Image": descriptor["images"]["web"]["digest_ref"],
                "Labels": {
                    "com.nexpoly.acceptance-operation": OPERATION_ID
                },
            },
            "HostConfig": {
                "PortBindings": {
                    "80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "9000"}]
                },
                "NetworkMode": "nexpoly_default",
                "Privileged": False,
                "PublishAllPorts": False,
                "AutoRemove": True,
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            },
            "NetworkSettings": {
                "Ports": {
                    "80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "9000"}]
                },
                "Networks": {
                    "nexpoly_default": {
                        "NetworkID": "2" * 64,
                        "IPAddress": "172.20.0.9",
                    }
                }
            },
        }
        runner = mock.Mock()
        name = f"nexpoly-acceptance-{OPERATION_ID}"

        def run(command, **_kwargs):  # type: ignore[no-untyped-def]
            if command[:4] == ["docker", "container", "ls", "--all"]:
                return subprocess.CompletedProcess(
                    command, 0, f"{'1' * 64}\t{name}\n", ""
                )
            return subprocess.CompletedProcess(
                command, 0, json.dumps([record]), ""
            )

        runner.run.side_effect = run
        controller = SimpleNamespace(
            runner=runner,
            control_environment=lambda: {},
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "loopback-only candidate"
        ):
            lifecycle._inspect_acceptance_proxy(
                controller,
                descriptor,
                name=name,
                expected_networks={"nexpoly_default": "2" * 64},
            )

    def test_container_absence_requires_successful_exact_enumeration(self) -> None:
        lifecycle = CONTROLLER.SystemLifecycle()
        name = f"nexpoly-acceptance-{OPERATION_ID}"
        descriptor = self._acceptance_runner_descriptor()

        for stderr in ("permission denied", "daemon unavailable"):
            runner = mock.Mock()
            runner.run.return_value = subprocess.CompletedProcess(
                ["docker"], 1, "", stderr
            )
            controller = SimpleNamespace(
                runner=runner,
                control_environment=lambda: {},
            )
            with self.subTest(stderr=stderr), self.assertRaisesRegex(
                CONTROLLER.PullDeployError, "cannot enumerate"
            ):
                lifecycle._inspect_acceptance_proxy(
                    controller,
                    descriptor,
                    name=name,
                    expected_networks={},
                )

        runner = mock.Mock()
        runner.run.return_value = subprocess.CompletedProcess(
            ["docker"], 0, "", ""
        )
        controller = SimpleNamespace(
            runner=runner,
            control_environment=lambda: {},
        )
        self.assertIsNone(
            lifecycle._inspect_acceptance_proxy(
                controller,
                descriptor,
                name=name,
                expected_networks={},
            )
        )

    def test_container_cleanup_does_not_treat_inspect_style_rc1_as_absence(
        self,
    ) -> None:
        lifecycle = CONTROLLER.SystemLifecycle()
        runner = mock.Mock()
        runner.run.side_effect = [
            subprocess.CompletedProcess(["docker"], 0, "", ""),
            subprocess.CompletedProcess(
                ["docker"], 1, "", "permission denied"
            ),
        ]
        controller = SimpleNamespace(
            runner=runner,
            control_environment=lambda: {},
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "cannot prove acceptance proxy container cleanup"
        ):
            lifecycle._remove_container_and_prove_absent(
                controller,
                f"nexpoly-acceptance-{OPERATION_ID}",
                container_id="1" * 64,
                label="acceptance proxy",
            )

    def _exercise_acceptance_proxy_start_failure(
        self, *, committed_container: bool
    ) -> tuple[CONTROLLER.SystemLifecycle, mock.Mock]:
        lifecycle = CONTROLLER.SystemLifecycle()
        descriptor = self._acceptance_runner_descriptor()
        proxy_id = "1" * 64
        inspect_results = [None, proxy_id if committed_container else None]
        if not committed_container:
            inspect_results.append(None)
        runner = mock.Mock()

        def run(command: list[str], **_kwargs: object):  # type: ignore[no-untyped-def]
            if command[:3] == ["docker", "container", "inspect"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        [
                            {
                                "NetworkSettings": {
                                    "Networks": {
                                        "nexpoly_default": {
                                            "NetworkID": "2" * 64
                                        }
                                    }
                                }
                            }
                        ]
                    ),
                    "",
                )
            if "run" in command:
                return subprocess.CompletedProcess(command, 1, "", "busy")
            return subprocess.CompletedProcess(command, 0, "", "")

        runner.run.side_effect = run
        controller = SimpleNamespace(
            runner=runner,
            production_root=self.production,
            runtime_root=self.runtime,
            config_dir=self.runtime / "config",
            control_environment=lambda: {},
        )
        with (
            mock.patch.object(
                lifecycle,
                "_environment",
                return_value={},
            ),
            mock.patch.object(
                lifecycle,
                "_backend_process_identity",
                return_value={"container_id": "3" * 64},
            ),
            mock.patch.object(
                lifecycle,
                "_inspect_acceptance_proxy",
                side_effect=inspect_results,
            ),
            mock.patch.object(lifecycle, "_control_cli", return_value={}),
            mock.patch.object(
                lifecycle,
                "_required_worker_sockets",
                return_value=[("monomer-md", Path("/md")), ("monomer-dft", Path("/dft"))],
            ),
            mock.patch.object(lifecycle, "_worker_request", return_value={}),
            mock.patch.object(lifecycle, "_wait_for_zero_work"),
            mock.patch.object(
                lifecycle, "_remove_container_and_prove_absent"
            ) as remove,
            mock.patch.object(
                CONTROLLER,
                "validate_persistent_drain_evidence",
                return_value={},
            ),
            mock.patch.object(
                CONTROLLER,
                "validate_worker_control_evidence",
                return_value={"worker_instance_id": "worker-1"},
            ),
        ):
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                (
                    "start response differs"
                    if committed_container
                    else "could not be started"
                ),
            ):
                lifecycle.run_acceptance_probes(
                    controller,
                    descriptor,
                    self.runtime / "state/prepared" / OPERATION_ID / "acceptance-authority.json",
                )
            if committed_container:
                remove.assert_called_once_with(
                    controller,
                    f"nexpoly-acceptance-{OPERATION_ID}",
                    container_id=proxy_id,
                    label="acceptance proxy",
                )
            else:
                remove.assert_not_called()
        return lifecycle, runner

    def test_acceptance_proxy_unknown_start_commit_is_cleaned(self) -> None:
        self._exercise_acceptance_proxy_start_failure(
            committed_container=True
        )

    def test_acceptance_proxy_occupied_port_redrains_without_cleanup(self) -> None:
        self._exercise_acceptance_proxy_start_failure(
            committed_container=False
        )

    def _write_rehearsal_stub(
        self,
        controller: FixtureController,
    ) -> Path:
        report_path = (
            controller.audit_dir
            / "deployment-rehearsals"
            / OPERATION_ID
            / "report.json"
        )
        report_path.parent.mkdir(parents=True, mode=0o700)
        os.chmod(report_path.parent, 0o700)
        CONTROLLER.atomic_json(
            report_path,
            {
                "report": {"status": "passed"},
                "report_sha256": "sha256:" + "1" * 64,
            },
        )
        return report_path

    def test_apply_uses_target_rehearsal_validator(self) -> None:
        controller = self.controller()
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        descriptor, descriptor_digest = controller._load_prepared(
            OPERATION_ID, TARGET_SHA
        )
        report_path = self._write_rehearsal_stub(controller)
        authority = {
            "report_sha256": "sha256:" + "2" * 64,
            "completed_at": CONTROLLER.utc_now(),
            "dump_sha256": "sha256:" + "3" * 64,
            "journal_head_sha256": "sha256:" + "4" * 64,
        }
        validator = mock.Mock(return_value=authority)
        module = SimpleNamespace(validate_rehearsal_report=validator)

        with mock.patch.object(
            controller,
            "_postgres_rehearsal_module",
            return_value=module,
        ):
            evidence = (
                CONTROLLER.PullDeployController._load_postgres_rehearsal_report(
                    controller, descriptor, descriptor_digest
                )
            )

        self.assertEqual(evidence["path"], str(report_path))
        self.assertEqual(evidence["report_sha256"], authority["report_sha256"])
        self.assertEqual(evidence["operation_id"], OPERATION_ID)
        self.assertEqual(evidence["target_sha"], TARGET_SHA)
        self.assertEqual(evidence["descriptor_sha256"], descriptor_digest)
        validator.assert_called_once_with(
            CONTROLLER.load_private_json(report_path),
            descriptor=descriptor,
            descriptor_sha256=descriptor_digest,
            ready_sha256=CONTROLLER.sha256_file(
                controller._operation_paths(OPERATION_ID)[2]
            ),
            runtime_root=controller.runtime_root,
            verify_runtime=True,
        )

    def test_rehearsal_validator_loads_from_candidate_control_release(self) -> None:
        controller = self.controller()
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        descriptor, _descriptor_digest = controller._load_prepared(
            OPERATION_ID, TARGET_SHA
        )

        module = controller._postgres_rehearsal_module(descriptor)

        self.assertTrue(callable(module.validate_rehearsal_report))
        self.assertEqual(
            Path(module.__file__).name,
            "production_postgres_rehearsal.py",
        )

    def test_apply_wraps_target_rehearsal_validation_failure(self) -> None:
        controller = self.controller()
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        descriptor, descriptor_digest = controller._load_prepared(
            OPERATION_ID, TARGET_SHA
        )
        self._write_rehearsal_stub(controller)
        module = SimpleNamespace(
            validate_rehearsal_report=mock.Mock(
                side_effect=ValueError("property count drift")
            )
        )

        with (
            mock.patch.object(
                controller,
                "_postgres_rehearsal_module",
                return_value=module,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "failed target validation",
            ),
        ):
            CONTROLLER.PullDeployController._load_postgres_rehearsal_report(
                controller, descriptor, descriptor_digest
            )

    def test_apply_consumes_rehearsal_before_marker_and_drain(self) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        sequence: list[str] = []
        original_drain = lifecycle.drain

        def gate(*_args: object) -> dict[str, str]:
            self.assertFalse(controller.marker_path.exists())
            sequence.append("rehearsal-gate")
            return {
                "schema_version": 1,
                "operation_id": OPERATION_ID,
                "target_sha": TARGET_SHA,
                "descriptor_sha256": CONTROLLER.sha256_file(
                    controller._operation_paths(OPERATION_ID)[1]
                ),
                "path": (
                    "/var/lib/nexpoly-fixture/audit/deployment-rehearsals/"
                    f"{OPERATION_ID}/report.json"
                ),
                "file_sha256": "sha256:" + "a" * 64,
                "report_sha256": "sha256:" + "b" * 64,
                "completed_at": "2026-01-01T00:00:00Z",
                "dump_sha256": "sha256:" + "c" * 64,
                "journal_head_sha256": "sha256:" + "d" * 64,
            }

        def drain(*args: object) -> dict[str, object]:
            sequence.append("drain")
            return original_drain(*args)

        with (
            mock.patch.object(
                controller,
                "_load_postgres_rehearsal_report",
                side_effect=gate,
            ),
            mock.patch.object(lifecycle, "drain", side_effect=drain),
        ):
            controller.apply_staged(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )

        self.assertEqual(sequence[:2], ["rehearsal-gate", "drain"])
        marker = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(
            marker["postgres_rehearsal"],
            marker["candidate_state"]["postgres_rehearsal"],
        )

    def test_recovery_revalidates_exact_postgres_rehearsal_before_runtime(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        lifecycle.events.clear()
        marker = CONTROLLER.load_private_json(controller.marker_path)
        changed = dict(marker["postgres_rehearsal"])
        changed["dump_sha256"] = "sha256:" + "e" * 64

        with mock.patch.object(
            controller,
            "_load_postgres_rehearsal_report",
            return_value=changed,
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "changed during recovery"
        ):
            controller.recover_interrupted()
        self.assertNotIn("recovery-isolate", lifecycle.events)

        with mock.patch.object(
            controller,
            "_load_postgres_rehearsal_report",
            side_effect=CONTROLLER.PullDeployError("report missing"),
        ), self.assertRaisesRegex(CONTROLLER.PullDeployError, "report missing"):
            controller.recover_interrupted()
        self.assertNotIn("recovery-isolate", lifecycle.events)

        descriptor, digest = controller._load_prepared(
            OPERATION_ID, TARGET_SHA, allow_deployment_database_recovery=True
        )
        tampered = json.loads(json.dumps(marker))
        tampered["postgres_rehearsal"]["report_sha256"] = (
            "sha256:" + "f" * 64
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "candidate state PostgreSQL rehearsal"
        ):
            CONTROLLER.validate_recovery_marker(
                tampered,
                descriptor=descriptor,
                descriptor_digest=digest,
            )

    def test_terminal_success_audit_retains_postgres_rehearsal_binding(self) -> None:
        controller = self.controller()
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        audit = CONTROLLER.load_private_json(
            controller.audit_dir / OPERATION_ID / "success.json"
        )
        self.assertEqual(
            audit["postgres_rehearsal"], state["postgres_rehearsal"]
        )
        self.assertEqual(
            audit["candidate_state"]["postgres_rehearsal"],
            state["postgres_rehearsal"],
        )

    def test_v4_apply_stays_drained_until_explicit_accept(self) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)

        state = controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )

        marker = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(marker["phase"], "awaiting-acceptance")
        self.assertEqual(marker["candidate_state"], state)
        self.assertNotIn("resume", lifecycle.events)
        self.assertFalse(
            (controller.audit_dir / OPERATION_ID / "operation-state.json").exists()
        )
        with mock.patch.object(
            lifecycle,
            "run_acceptance_probes",
            side_effect=lambda *_args: self._write_passing_probe_report(
                controller
            ),
        ) as probes:
            observing = controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )
        probes.assert_called_once()
        self.assertEqual(observing["status"], "maintenance-observation")
        self.assertFalse(observing["terminal"])
        self.assertFalse(observing["public_admission_open"])
        self.assertEqual(observing["next_action"], "rerun accept after acceptance_not_before")
        marker = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(marker["phase"], "acceptance-started")
        self.assertFalse(
            (controller.audit_dir / OPERATION_ID / "operation-state.json").exists()
        )
        evidence = marker["acceptance_evidence"]
        probe_intent = marker["acceptance_probe_intent"]
        pre_probe = probe_intent["pre_probe_mutable_data_evidence"]
        post_probe = evidence["post_probe_mutable_data_evidence"]
        self.assertEqual(pre_probe["schema_version"], 7)
        self.assertEqual(post_probe["schema_version"], 7)
        self.assertEqual(
            probe_intent["pre_probe_mutable_data_identity_sha256"],
            CONTROLLER.canonical_json_digest(
                CONTROLLER.acceptance_full_mutable_data_identity(pre_probe)
            ),
        )
        self.assertEqual(
            evidence["post_probe_mutable_data_identity_sha256"],
            CONTROLLER.canonical_json_digest(
                CONTROLLER.acceptance_full_mutable_data_identity(post_probe)
            ),
        )
        self.assertNotEqual(
            probe_intent["pre_probe_mutable_data_identity_sha256"],
            CONTROLLER.canonical_json_digest(
                CONTROLLER.mutable_data_identity(pre_probe)
            ),
        )
        started = CONTROLLER._external_database_audit_timestamp(
            evidence["observation_started_at"], "test observation start"
        )
        not_before = CONTROLLER._external_database_audit_timestamp(
            evidence["observation_not_before"], "test observation not-before"
        )
        self.assertEqual(
            not_before - started,
            CONTROLLER.dt.timedelta(seconds=CONTROLLER.ACCEPTANCE_HOLD_SECONDS),
        )
        verified = CONTROLLER._external_database_audit_timestamp(
            evidence["verified_at"], "test acceptance verification"
        )
        probes_completed = CONTROLLER._external_database_audit_timestamp(
            evidence["probes_completed_at"], "test probes completion"
        )
        self.assertEqual(evidence["observation_started_at"], evidence["verified_at"])
        self.assertGreaterEqual(verified, probes_completed)
        self.assertNotIn("resume", lifecycle.events)
        with mock.patch.object(
            lifecycle, "run_acceptance_probes"
        ) as rerun, self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "post-probe maintenance observation"
        ):
            controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )
        rerun.assert_not_called()

    def test_acceptance_probe_transition_allows_only_reviewed_dynamics(
        self,
    ) -> None:
        before = mutable_data_evidence(ledger_length=15)
        after = json.loads(json.dumps(before))
        self._mutate_acceptance_history(after, generation=5, rows=3)
        dft_jobs = next(
            record
            for record in after["business_tables"]
            if (record["schema"], record["table"])
            == ("monomer_dft", "jobs")
        )
        dft_jobs["row_count"] += 1
        dft_jobs["content_sha256"] = "sha256:" + "6" * 64
        for sequence in after["sequences"]:
            if (
                f"{sequence['schema']}.{sequence['sequence']}"
                in CONTROLLER.ACCEPTANCE_PROBE_MUTABLE_SEQUENCES
            ):
                sequence["last_value"] += 4
                sequence["is_called"] = True
        self._refresh_acceptance_drain(
            after,
            generation=8,
            reason=f"post-acceptance drain {OPERATION_ID}",
        )
        reseal_mutable_data_evidence(after)

        identity = CONTROLLER.validate_acceptance_probe_mutable_transition(
            before,
            after,
            operation_id=OPERATION_ID,
            release_sha=TARGET_SHA,
        )
        self.assertEqual(identity["business_tables"], after["business_tables"])

        static_drift = json.loads(json.dumps(after))
        static_drift["static_tables"][0]["content_sha256"] = (
            "sha256:" + "0" * 64
        )
        reseal_mutable_data_evidence(static_drift)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "immutable or non-probe",
        ):
            CONTROLLER.validate_acceptance_probe_mutable_transition(
                before,
                static_drift,
                operation_id=OPERATION_ID,
                release_sha=TARGET_SHA,
            )

    def test_observe_guard_transitions_do_not_change_runtime_stability(
        self,
    ) -> None:
        ready = {
            "runtime_identity": {
                "repository": {"sha": TARGET_SHA},
                "containers": {"backend": {"container_id": "a" * 64}},
                "dft_guard": {
                    "status": "ready",
                    "contention": False,
                    "observed_at": "2026-08-14T00:00:00Z",
                },
                "dft_unit": {
                    "unit_sha256": "sha256:" + "7" * 64,
                    "invocation_id": "dft-invocation-1",
                },
                "dft_worker": {
                    "worker_instance_id": "dft-worker-1",
                    "runtime_ready": True,
                    "gpu_guard_mode": "observe",
                    "gpu_guard_status": "ready",
                    "gpu_contention_observed": False,
                    "runtime": {
                        "gpu_uuid": "GPU-" + "1" * 32,
                        "guard_status": "ready",
                        "gpu_guard_mode": "observe",
                        "gpu_guard_status": "ready",
                        "gpu_contention_observed": False,
                        "runtime_inventory_sha256": "sha256:" + "8" * 64,
                    },
                },
                "verified_at": "2026-08-14T00:00:00Z",
            },
            "recovery_fence": {"fixture_instance": "same"},
        }
        quarantined = json.loads(json.dumps(ready))
        quarantined["runtime_identity"]["dft_guard"].update(
            status="quarantined",
            contention=True,
            observed_at="2026-08-14T00:01:00Z",
        )
        worker = quarantined["runtime_identity"]["dft_worker"]
        worker["gpu_guard_status"] = "quarantined"
        worker["gpu_contention_observed"] = True
        worker["runtime"]["guard_status"] = "quarantined"
        worker["runtime"]["gpu_guard_status"] = "quarantined"
        worker["runtime"]["gpu_contention_observed"] = True

        self.assertEqual(
            CONTROLLER.acceptance_runtime_stability_identity(ready),
            CONTROLLER.acceptance_runtime_stability_identity(quarantined),
        )
        quarantined["runtime_identity"]["dft_worker"][
            "worker_instance_id"
        ] = "dft-worker-2"
        self.assertNotEqual(
            CONTROLLER.acceptance_runtime_stability_identity(ready),
            CONTROLLER.acceptance_runtime_stability_identity(quarantined),
        )

        enforce_ready = json.loads(json.dumps(ready))
        enforce_ready["runtime_identity"]["dft_worker"][
            "gpu_guard_mode"
        ] = "enforce"
        enforce_ready["runtime_identity"]["dft_worker"]["runtime"][
            "gpu_guard_mode"
        ] = "enforce"
        enforce_quarantined = json.loads(json.dumps(enforce_ready))
        enforce_quarantined["runtime_identity"]["dft_guard"].update(
            status="quarantined",
            contention=True,
            observed_at="2026-08-14T00:01:00Z",
        )
        enforce_quarantined["runtime_identity"]["dft_worker"].update(
            gpu_guard_status="quarantined",
            gpu_contention_observed=True,
        )
        enforce_quarantined["runtime_identity"]["dft_worker"][
            "runtime"
        ].update(
            guard_status="quarantined",
            gpu_guard_status="quarantined",
            gpu_contention_observed=True,
        )
        self.assertNotEqual(
            CONTROLLER.acceptance_runtime_stability_identity(enforce_ready),
            CONTROLLER.acceptance_runtime_stability_identity(
                enforce_quarantined
            ),
        )

        for label, mutate in (
            (
                "gpu-uuid",
                lambda value: value["runtime_identity"]["dft_worker"][
                    "runtime"
                ].update(gpu_uuid="GPU-" + "2" * 32),
            ),
            (
                "guard-mode",
                lambda value: value["runtime_identity"]["dft_worker"].update(
                    gpu_guard_mode="enforce"
                ),
            ),
            (
                "unit",
                lambda value: value["runtime_identity"]["dft_unit"].update(
                    invocation_id="dft-invocation-2"
                ),
            ),
            (
                "runtime",
                lambda value: value["runtime_identity"]["dft_worker"][
                    "runtime"
                ].update(runtime_inventory_sha256="sha256:" + "9" * 64),
            ),
        ):
            drifted = json.loads(json.dumps(ready))
            mutate(drifted)
            with self.subTest(label=label):
                self.assertNotEqual(
                    CONTROLLER.acceptance_runtime_stability_identity(ready),
                    CONTROLLER.acceptance_runtime_stability_identity(drifted),
                )

    def test_accept_resume_lost_response_full_open_preserves_business_writes(
        self,
    ) -> None:
        lifecycle = LostResumeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        repository_identity = controller.repository_identity
        controller.repository_identity = lambda **kwargs: {  # type: ignore[method-assign]
            **repository_identity(**kwargs),
            "trust": {"fixture": "stable-trust-surface"},
        }
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self._expire_acceptance_hold(controller)
        self._write_passing_probe_report(controller)
        observing = controller.accept(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self.assertEqual(observing["status"], "maintenance-observation")
        self._expire_acceptance_hold(controller)
        lifecycle.lose_next_resume = True
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lost response after admission commit",
        ):
            controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )
        marker = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(marker["phase"], "acceptance-resume-started")
        self.assertEqual(
            marker["acceptance_resume_intent"]["operation_id"],
            OPERATION_ID,
        )
        self.assertEqual(
            marker["acceptance_resume_intent"][
                "candidate_state_sha256"
            ],
            marker["candidate_state_sha256"],
        )
        self.assertTrue(lifecycle.admission_open)

        # A public request may commit after the runtime opened and before the
        # controller receives the resume response.  Recovery must prove the
        # already-open runtime without recapturing or comparing mutable rows.
        accepted_business_writes = 1
        lifecycle.events.clear()
        with mock.patch.object(
            controller,
            "_capture_mutable_data",
            side_effect=AssertionError(
                "full-open forward recovery recaptured mutable data"
            ),
        ) as mutable_capture:
            recovered = controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )
        self.assertEqual(recovered, state)
        self.assertEqual(accepted_business_writes, 1)
        mutable_capture.assert_not_called()
        self.assertEqual(
            lifecycle.events,
            ["admission-status", "verify-open"],
        )
        self.assertFalse(controller.marker_path.exists())

    def test_accept_resume_partial_persistent_open_redrains_forward_and_preserves_writes(
        self,
    ) -> None:
        class PartialPersistentResumeLifecycle(FakeLifecycle):
            def __init__(self) -> None:
                super().__init__()
                self.lose_after_internal_admission = False
                self.public_ingress_open = False

            def resume(
                self,
                controller: object,
                descriptor: object,
                expected_verification: object,
            ) -> None:
                marker = CONTROLLER.load_private_json(
                    getattr(controller, "marker_path")
                )
                if marker.get("verification") != expected_verification:
                    raise AssertionError(
                        "runtime fence was not durable before resume"
                    )
                FakeLifecycle.resume(
                    self,
                    controller,
                    descriptor,
                    expected_verification,
                )
                if self.lose_after_internal_admission:
                    self.lose_after_internal_admission = False
                    raise CONTROLLER.PullDeployError(
                        "injected lost response after partial admission commit"
                    )
                self.public_ingress_open = True

            def verify_open_runtime(
                self,
                controller: object,
                descriptor: object,
                expected_verification: object,
            ) -> None:
                FakeLifecycle.verify_open_runtime(
                    self,
                    controller,
                    descriptor,
                    expected_verification,
                )
                if not self.public_ingress_open:
                    raise CONTROLLER.PullDeployError(
                        "public ingress did not commit"
                    )

        lifecycle = PartialPersistentResumeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self._expire_acceptance_hold(controller)
        self._write_passing_probe_report(controller)
        controller.accept(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self._expire_acceptance_hold(controller)
        lifecycle.lose_after_internal_admission = True
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lost response after partial admission commit",
        ):
            controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )
        marker = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(marker["phase"], "acceptance-resume-started")
        self.assertTrue(lifecycle.admission_open)
        self.assertFalse(lifecycle.public_ingress_open)

        accepted_business_writes = 1
        lifecycle.events.clear()
        with mock.patch.object(
            controller,
            "_capture_mutable_data",
            side_effect=AssertionError(
                "partial-open forward recovery recaptured mutable data"
            ),
        ) as mutable_capture:
            recovered = controller.recover_interrupted()

        self.assertEqual(recovered, state)
        self.assertEqual(accepted_business_writes, 1)
        mutable_capture.assert_not_called()
        self.assertEqual(
            lifecycle.events,
            [
                "admission-status",
                "verify-open",
                "recovery-isolate",
                "recovery-redrain",
                "verify-acceptance-stability",
                "resume",
            ],
        )
        self.assertTrue(lifecycle.admission_open)
        self.assertTrue(lifecycle.public_ingress_open)
        self.assertNotIn("restore_database", lifecycle.events)
        self.assertNotIn("acceptance-probes", lifecycle.events)
        self.assertFalse(controller.marker_path.exists())

    def test_accept_resume_rejects_repository_trust_drift_before_terminalization(
        self,
    ) -> None:
        lifecycle = LostResumeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        repository_identity = controller.repository_identity

        def trusted_repository(label: str):  # type: ignore[no-untyped-def]
            return lambda **kwargs: {  # type: ignore[return-value]
                **repository_identity(**kwargs),
                "trust": {"fixture": label},
            }

        controller.repository_identity = trusted_repository(  # type: ignore[method-assign]
            "sealed-trust"
        )
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self._expire_acceptance_hold(controller)
        self._write_passing_probe_report(controller)
        controller.accept(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self._expire_acceptance_hold(controller)
        lifecycle.lose_next_resume = True
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lost response after admission commit",
        ):
            controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )

        controller.repository_identity = trusted_repository(  # type: ignore[method-assign]
            "drifted-trust"
        )
        lifecycle.events.clear()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "sealed runtime/Worker/source authority changed",
        ):
            controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )

        retained = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(retained["phase"], "acceptance-resume-started")
        self.assertEqual(lifecycle.events, [])
        self.assertFalse(
            (controller.audit_dir / OPERATION_ID / "operation-state.json").exists()
        )

    def test_accept_resume_rejects_synchronized_replacement_runtime_authority(
        self,
    ) -> None:
        lifecycle = LostResumeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self._expire_acceptance_hold(controller)
        self._write_passing_probe_report(controller)
        controller.accept(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self._expire_acceptance_hold(controller)
        lifecycle.lose_next_resume = True
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lost response after admission commit",
        ):
            controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )

        lifecycle.runtime_instance = "fixture-runtime-2"
        lifecycle.recovery_fence = {
            "fixture_instance": "replacement-instance"
        }
        marker = CONTROLLER.load_private_json(controller.marker_path)
        marker["verification"]["runtime_identity"][
            "worker_instance_id"
        ] = lifecycle.runtime_instance
        marker["verification"]["recovery_fence"] = dict(
            lifecycle.recovery_fence
        )
        CONTROLLER.atomic_json(controller.marker_path, marker)

        lifecycle.events.clear()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "sealed runtime/Worker/source authority changed",
        ):
            controller.recover_interrupted()

        retained = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(retained["phase"], "acceptance-resume-started")
        self.assertEqual(lifecycle.events, [])
        self.assertFalse(
            (controller.audit_dir / OPERATION_ID / "operation-state.json").exists()
        )

    def test_accept_resume_redrain_crash_preserves_full_authority_for_retry(
        self,
    ) -> None:
        lifecycle = LostResumeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self._expire_acceptance_hold(controller)
        self._write_passing_probe_report(controller)
        controller.accept(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self._expire_acceptance_hold(controller)
        lifecycle.lose_next_resume = True
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lost response after admission commit",
        ):
            controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )

        before = CONTROLLER.load_private_json(controller.marker_path)
        sealed_verification = json.loads(json.dumps(before["verification"]))
        lifecycle.admission_open = False
        lifecycle.fail_at = "verify-acceptance-stability"
        lifecycle.events.clear()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "injected verify-acceptance-stability failure",
        ):
            controller.recover_interrupted()

        crashed = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(crashed["phase"], "acceptance-resume-started")
        self.assertEqual(crashed["verification"], sealed_verification)
        self.assertEqual(
            lifecycle.events,
            [
                "admission-status",
                "recovery-isolate",
                "recovery-redrain",
                "verify-acceptance-stability",
            ],
        )

        lifecycle.fail_at = None
        lifecycle.events.clear()
        recovered = controller.recover_interrupted()
        self.assertEqual(recovered, state)
        self.assertEqual(
            lifecycle.events,
            [
                "admission-status",
                "recovery-isolate",
                "recovery-redrain",
                "verify-acceptance-stability",
                "resume",
            ],
        )
        self.assertFalse(controller.marker_path.exists())

    def test_accept_terminalization_rereads_exact_candidate_state(self) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self._expire_acceptance_hold(controller)
        self._write_passing_probe_report(controller)
        controller.accept(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self._expire_acceptance_hold(controller)
        original_resume = lifecycle.resume

        def resume_then_remove_current(*args, **kwargs):  # type: ignore[no-untyped-def]
            original_resume(*args, **kwargs)
            controller.current_state_path.unlink()

        lifecycle.resume = resume_then_remove_current  # type: ignore[method-assign]
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lost its candidate current state",
        ):
            controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )

        marker = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(marker["phase"], "admission-resumed")
        self.assertTrue(lifecycle.admission_open)
        self.assertFalse(
            (controller.audit_dir / OPERATION_ID / "success.json").exists()
        )
        self.assertFalse(
            (
                controller.audit_dir
                / OPERATION_ID
                / "operation-state.json"
            ).exists()
        )

    def test_lost_acceptance_resume_blocks_rollback_before_side_effects(
        self,
    ) -> None:
        lifecycle = LostResumeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self._expire_acceptance_hold(controller)
        self._write_passing_probe_report(controller)
        controller.accept(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self._expire_acceptance_hold(controller)
        lifecycle.lose_next_resume = True
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lost response after admission commit",
        ):
            controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )

        descriptor, descriptor_digest = controller._load_prepared(
            OPERATION_ID,
            TARGET_SHA,
            allow_deployment_database_recovery=True,
        )
        sealed_marker = CONTROLLER.load_private_json(controller.marker_path)
        for label, mutate in (
            (
                "missing",
                lambda value: value.pop("acceptance_resume_intent"),
            ),
            (
                "operation",
                lambda value: value["acceptance_resume_intent"].update(
                    operation_id="another-resume-operation"
                ),
            ),
            (
                "candidate",
                lambda value: value["acceptance_resume_intent"].update(
                    candidate_state_sha256="sha256:" + "f" * 64
                ),
            ),
            (
                "timestamp",
                lambda value: value["acceptance_resume_intent"].update(
                    recorded_at="not-a-timestamp"
                ),
            ),
            (
                "before-observation-deadline",
                lambda value: value["acceptance_resume_intent"].update(
                    recorded_at=value["acceptance_evidence"][
                        "observation_started_at"
                    ]
                ),
            ),
            (
                "future",
                lambda value: value["acceptance_resume_intent"].update(
                    recorded_at="2999-01-01T00:00:00Z"
                ),
            ),
        ):
            changed = json.loads(json.dumps(sealed_marker))
            mutate(changed)
            with self.subTest(label=label), self.assertRaises(
                CONTROLLER.PullDeployError
            ):
                CONTROLLER.validate_recovery_marker(
                    changed,
                    descriptor=descriptor,
                    descriptor_digest=descriptor_digest,
                )

        marker_bytes = controller.marker_path.read_bytes()
        state_bytes = controller.current_state_path.read_bytes()
        lifecycle.events.clear()
        with (
            mock.patch.object(controller, "_write_marker") as write_marker,
            mock.patch.object(
                controller, "_rollback_failed_attempt"
            ) as rollback_attempt,
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "unknown public-admission commit; rollback is forbidden",
            ),
        ):
            controller.rollback(operation_id=OPERATION_ID)

        write_marker.assert_not_called()
        rollback_attempt.assert_not_called()
        self.assertEqual(lifecycle.events, [])
        self.assertEqual(controller.marker_path.read_bytes(), marker_bytes)
        self.assertEqual(controller.current_state_path.read_bytes(), state_bytes)

    def test_resume_intent_fence_drift_stays_forward_only_and_blocks_rollback(
        self,
    ) -> None:
        lifecycle = LostResumeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self._expire_acceptance_hold(controller)
        self._write_passing_probe_report(controller)
        controller.accept(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self._expire_acceptance_hold(controller)
        lifecycle.lose_next_resume = True
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lost response after admission commit",
        ):
            controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )
        resume_started = CONTROLLER.load_private_json(controller.marker_path)
        resume_intent = resume_started["acceptance_resume_intent"]

        lifecycle.recovery_fence["fixture_instance"] = "replacement-instance"
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "forward fix is required",
        ):
            controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )
        forward_only = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(
            forward_only["phase"], "acceptance-resume-started"
        )
        self.assertNotIn("acceptance_rejected", forward_only)
        self.assertEqual(
            forward_only["acceptance_resume_intent"], resume_intent
        )
        self.assertIn(
            "forward fix is required",
            forward_only["forward_recovery_error"],
        )
        self.assertNotIn(
            "explicit rollback", forward_only["forward_recovery_error"]
        )
        descriptor, descriptor_digest = controller._load_prepared(
            OPERATION_ID,
            TARGET_SHA,
            allow_deployment_database_recovery=True,
        )
        missing_evidence = json.loads(json.dumps(forward_only))
        missing_evidence.pop("acceptance_evidence")
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "staged acceptance evidence is invalid",
        ):
            CONTROLLER.validate_recovery_marker(
                missing_evidence,
                descriptor=descriptor,
                descriptor_digest=descriptor_digest,
            )

        marker_bytes = controller.marker_path.read_bytes()
        state_bytes = controller.current_state_path.read_bytes()
        lifecycle.events.clear()
        with (
            mock.patch.object(controller, "_write_marker") as write_marker,
            mock.patch.object(
                controller, "_rollback_failed_attempt"
            ) as rollback_attempt,
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "unknown public-admission commit; rollback is forbidden",
            ),
        ):
            controller.rollback(operation_id=OPERATION_ID)
        write_marker.assert_not_called()
        rollback_attempt.assert_not_called()
        self.assertEqual(lifecycle.events, [])
        self.assertEqual(controller.marker_path.read_bytes(), marker_bytes)
        self.assertEqual(controller.current_state_path.read_bytes(), state_bytes)

        lifecycle.events.clear()
        with (
            mock.patch.object(
                controller, "_rollback_failed_attempt"
            ) as rollback_attempt,
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError, "forward fix is required"
            ),
        ):
            controller.recover_interrupted()
        rollback_attempt.assert_not_called()
        self.assertEqual(
            lifecycle.events,
            ["admission-status", "verify-open", "recovery-isolate"],
        )
        recovered_marker = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(
            recovered_marker["phase"], "acceptance-resume-started"
        )
        self.assertEqual(
            recovered_marker["acceptance_resume_intent"], resume_intent
        )
        self.assertEqual(controller.current_state_path.read_bytes(), state_bytes)

    def test_stopped_runtime_after_resume_intent_never_restarts_or_probes(
        self,
    ) -> None:
        lifecycle = LostResumeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self._expire_acceptance_hold(controller)
        self._write_passing_probe_report(controller)
        controller.accept(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self._expire_acceptance_hold(controller)
        lifecycle.lose_next_resume = True
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lost response after admission commit",
        ):
            controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )
        sticky = CONTROLLER.load_private_json(controller.marker_path)[
            "acceptance_resume_intent"
        ]
        lifecycle.runtime_state = "stopped"
        lifecycle.admission_open = False
        lifecycle.events.clear()

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "forward fix is required",
        ):
            controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )

        marker = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(marker["phase"], "acceptance-resume-started")
        self.assertEqual(marker["acceptance_resume_intent"], sticky)
        self.assertNotIn("acceptance_rejected", marker)
        self.assertIn("forward fix is required", marker["forward_recovery_error"])
        self.assertNotIn("explicit rollback", marker["forward_recovery_error"])
        self.assertNotIn("start", lifecycle.events)
        self.assertNotIn("acceptance-probes", lifecycle.events)
        self.assertNotIn("verify", lifecycle.events)
        self.assertNotIn("verify-acceptance-stability", lifecycle.events)
        self.assertNotIn("resume", lifecycle.events)

    def test_sticky_acceptance_rejection_remains_valid_and_requires_reviewed_fix(
        self,
    ) -> None:
        lifecycle = LostResumeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self._expire_acceptance_hold(controller)
        self._write_passing_probe_report(controller)
        controller.accept(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self._expire_acceptance_hold(controller)
        lifecycle.lose_next_resume = True
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lost response after admission commit",
        ):
            controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )

        marker = CONTROLLER.load_private_json(controller.marker_path)
        marker["phase"] = "acceptance-rejected"
        marker["acceptance_rejected"] = True
        marker["updated_at"] = CONTROLLER.utc_now()
        CONTROLLER.atomic_json(controller.marker_path, marker)
        descriptor, descriptor_digest = controller._load_prepared(
            OPERATION_ID,
            TARGET_SHA,
            allow_deployment_database_recovery=True,
        )
        CONTROLLER.validate_recovery_marker(
            marker,
            descriptor=descriptor,
            descriptor_digest=descriptor_digest,
        )

        lifecycle.events.clear()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "separately reviewed forward fix is required",
        ):
            controller.recover_interrupted()

        retained = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(retained["phase"], "acceptance-rejected")
        self.assertTrue(retained["acceptance_rejected"])
        self.assertEqual(
            retained["acceptance_resume_intent"],
            marker["acceptance_resume_intent"],
        )
        self.assertEqual(lifecycle.events, [])
        CONTROLLER.validate_recovery_marker(
            retained,
            descriptor=descriptor,
            descriptor_digest=descriptor_digest,
        )

    def test_stopped_observation_requires_rollback_then_fresh_deployment(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self._write_passing_probe_report(controller)
        controller.accept(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        self._expire_acceptance_hold(controller)
        lifecycle.runtime_state = "stopped"
        lifecycle.admission_open = False
        lifecycle.events.clear()

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "explicit rollback followed by a fresh deployment operation",
        ):
            controller.accept(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )

        marker = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(marker["phase"], "acceptance-rejected")
        self.assertIn(
            "explicit rollback followed by a fresh deployment operation",
            marker["error"],
        )
        self.assertNotIn("acceptance reset", marker["error"])
        self.assertNotIn("start", lifecycle.events)
        self.assertNotIn("acceptance-probes", lifecycle.events)

    def test_final_accept_rejects_worker_fence_drift_and_requires_rollback(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self._expire_acceptance_hold(controller)
        self._write_passing_probe_report(controller)
        observing = controller.accept(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self.assertEqual(observing["status"], "maintenance-observation")
        self._expire_acceptance_hold(controller)
        first_marker = CONTROLLER.load_private_json(controller.marker_path)
        first_evidence = json.loads(
            json.dumps(first_marker["acceptance_evidence"])
        )
        lifecycle.recovery_fence["fixture_instance"] = "replacement-instance"

        with mock.patch.object(
            lifecycle, "run_acceptance_probes"
        ) as probes, self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "stability changed"
        ):
            controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )

        probes.assert_not_called()
        rejected = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(rejected["phase"], "acceptance-rejected")
        self.assertTrue(rejected["acceptance_rejected"])
        self.assertEqual(rejected["acceptance_evidence"], first_evidence)
        self.assertIn("explicit rollback is required", rejected["error"])
        self.assertNotIn("forward fix", rejected["error"])
        self.assertFalse(lifecycle.admission_open)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "staged acceptance boundary"
        ):
            controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )

    def test_final_accept_allows_only_owned_drain_refresh(self) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self._expire_acceptance_hold(controller)
        live_snapshot = json.loads(
            json.dumps(
                CONTROLLER.validate_mutable_data_pair(
                    state["mutable_data_audit"]
                )["after"]
            )
        )

        def capture(*_args: object) -> dict[str, object]:
            captured = json.loads(json.dumps(live_snapshot))
            captured["captured_at"] = CONTROLLER.utc_now()
            return captured

        with (
            mock.patch.object(
                controller, "_capture_mutable_data", side_effect=capture
            ),
            mock.patch.object(
                lifecycle,
                "run_acceptance_probes",
                side_effect=lambda *_args: self._write_passing_probe_report(
                    controller
                ),
            ),
        ):
            observing = controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )
            self._refresh_acceptance_drain(
                live_snapshot,
                generation=7,
                reason=f"post-acceptance drain {OPERATION_ID}",
            )
            self._expire_acceptance_hold(controller)
            lifecycle.events.clear()
            accepted = controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )

        self.assertEqual(observing["status"], "maintenance-observation")
        self.assertEqual(accepted, state)
        self.assertIn("verify-acceptance-stability", lifecycle.events)
        self.assertNotIn("verify", lifecycle.events)

    def test_final_accept_rejects_post_probe_business_drift(self) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self._expire_acceptance_hold(controller)
        live_snapshot = json.loads(
            json.dumps(
                CONTROLLER.validate_mutable_data_pair(
                    state["mutable_data_audit"]
                )["after"]
            )
        )

        def capture(*_args: object) -> dict[str, object]:
            captured = json.loads(json.dumps(live_snapshot))
            captured["captured_at"] = CONTROLLER.utc_now()
            return captured

        with (
            mock.patch.object(
                controller, "_capture_mutable_data", side_effect=capture
            ),
            mock.patch.object(
                lifecycle,
                "run_acceptance_probes",
                side_effect=lambda *_args: self._write_passing_probe_report(
                    controller
                ),
            ),
        ):
            controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )
            history = next(
                record
                for record in live_snapshot["business_tables"]
                if (record["schema"], record["table"])
                == ("online_knowledge", "history")
            )
            history["row_count"] += 1
            history["content_sha256"] = "sha256:" + "0" * 64
            reseal_mutable_data_evidence(live_snapshot)
            self._expire_acceptance_hold(controller)
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "stability changed",
            ):
                controller.accept(
                    target_sha=TARGET_SHA, operation_id=OPERATION_ID
                )

        marker = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(marker["phase"], "acceptance-rejected")
        self.assertIn(
            "post_probe_mutable_data_stability_sha256",
            marker["error"],
        )
        self.assertFalse(lifecycle.admission_open)

    def test_accept_cleans_crash_left_proxy_before_consuming_report(self) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self._expire_acceptance_hold(controller)
        self._write_passing_probe_report(controller)
        observing = controller.accept(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self.assertEqual(observing["status"], "maintenance-observation")
        self._expire_acceptance_hold(controller)
        lifecycle.events.clear()

        def cleanup(*_args: object) -> None:
            lifecycle.events.append("cleanup-acceptance-proxy")

        with mock.patch.object(
            lifecycle,
            "cleanup_acceptance_probe_proxy",
            side_effect=cleanup,
        ):
            controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )

        self.assertLess(
            lifecycle.events.index("recovery-isolate"),
            lifecycle.events.index("cleanup-acceptance-proxy"),
        )
        self.assertLess(
            lifecycle.events.index("cleanup-acceptance-proxy"),
            lifecycle.events.index("verify-acceptance-stability"),
        )

    def test_failed_acceptance_probe_is_archived_before_retry(self) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self._expire_acceptance_hold(controller)
        marker = CONTROLLER.load_private_json(controller.marker_path)
        authority = CONTROLLER.load_private_json(
            Path(marker["acceptance_authority_path"])
        )
        probe_timestamp = authority["staged_at"]
        failed = {
            "schema_version": 1,
            "status": "failed",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "authority": authority,
            "authority_sha256": marker["acceptance_authority_sha256"],
            "loopback_endpoint": "http://127.0.0.1:9000",
            "started_at": probe_timestamp,
            "finished_at": probe_timestamp,
            "sections": {"dft": {"status": "failed"}},
            "error": "synthetic probe failure",
        }
        failed["report_sha256"] = CONTROLLER.canonical_json_digest(failed)
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        failed_path = operation / f"production-acceptance-{OPERATION_ID}.json"
        CONTROLLER.atomic_json(failed_path, failed)
        failed_file_digest = CONTROLLER.sha256_file(failed_path)

        with mock.patch.object(
            lifecycle,
            "run_acceptance_probes",
            side_effect=lambda *_args: self._write_passing_probe_report(
                controller
            ),
        ):
            observing = controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )
            self._expire_acceptance_hold(controller)
            accepted = controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )

        self.assertEqual(observing["status"], "maintenance-observation")
        self.assertEqual(accepted, state)
        archived = (
            operation
            / "failed-acceptance-probes"
            / (
                f"production-acceptance-{OPERATION_ID}-"
                f"{failed_file_digest.removeprefix('sha256:')}.json"
            )
        )
        self.assertTrue(archived.exists())
        self.assertEqual(
            CONTROLLER.load_private_json(failed_path)["status"], "passed"
        )

    def test_failed_partial_probe_mutation_retries_from_sealed_prebaseline(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self._expire_acceptance_hold(controller)
        live_snapshot = json.loads(
            json.dumps(
                CONTROLLER.validate_mutable_data_pair(
                    state["mutable_data_audit"]
                )["after"]
            )
        )
        attempts = 0

        def probes(*_args: object) -> None:
            nonlocal attempts
            attempts += 1
            self._mutate_acceptance_history(
                live_snapshot,
                generation=attempts + 4,
            )
            if attempts == 1:
                raise CONTROLLER.PullDeployError(
                    "synthetic partial probe failure"
                )
            self._write_passing_probe_report(controller)

        def capture(*_args: object) -> dict[str, object]:
            captured = json.loads(json.dumps(live_snapshot))
            captured["captured_at"] = CONTROLLER.utc_now()
            return captured

        with (
            mock.patch.object(
                controller, "_capture_mutable_data", side_effect=capture
            ),
            mock.patch.object(
                lifecycle, "run_acceptance_probes", side_effect=probes
            ),
        ):
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "partial probe failure",
            ):
                controller.accept(
                    target_sha=TARGET_SHA,
                    operation_id=OPERATION_ID,
                )
            first_marker = CONTROLLER.load_private_json(
                controller.marker_path
            )
            self.assertEqual(first_marker["phase"], "awaiting-acceptance")
            first_intent = json.loads(
                json.dumps(first_marker["acceptance_probe_intent"])
            )

            observing = controller.accept(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )
            second_marker = CONTROLLER.load_private_json(
                controller.marker_path
            )
            self.assertEqual(
                second_marker["acceptance_probe_intent"], first_intent
            )
            self.assertNotEqual(
                second_marker["acceptance_evidence"][
                    "pre_probe_mutable_data_identity_sha256"
                ],
                second_marker["acceptance_evidence"][
                    "post_probe_mutable_data_identity_sha256"
                ],
            )
            self._expire_acceptance_hold(controller)
            accepted = controller.accept(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )

        self.assertEqual(attempts, 2)
        self.assertEqual(observing["status"], "maintenance-observation")
        self.assertEqual(accepted, state)

    def test_recovery_failure_before_probe_intent_cannot_absorb_database_drift(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        live_snapshot = json.loads(
            json.dumps(
                CONTROLLER.validate_mutable_data_pair(
                    state["mutable_data_audit"]
                )["after"]
            )
        )
        original_recovery = controller._prepare_runtime_recovery
        attempts = 0

        def recover(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                self._mutate_acceptance_history(
                    live_snapshot,
                    generation=11,
                )
                raise CONTROLLER.PullDeployError(
                    "synthetic recovery fence failure"
                )
            return original_recovery(*args, **kwargs)

        def capture(*_args: object) -> dict[str, object]:
            observed = json.loads(json.dumps(live_snapshot))
            observed["captured_at"] = CONTROLLER.utc_now()
            return observed

        with (
            mock.patch.object(
                controller,
                "_prepare_runtime_recovery",
                side_effect=recover,
            ),
            mock.patch.object(
                controller,
                "_capture_mutable_data",
                side_effect=capture,
            ),
            mock.patch.object(lifecycle, "run_acceptance_probes") as probes,
        ):
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "recovery fence failure",
            ):
                controller.accept(
                    target_sha=TARGET_SHA,
                    operation_id=OPERATION_ID,
                )
            failed_recovery = CONTROLLER.load_private_json(
                controller.marker_path
            )
            self.assertNotIn("acceptance_probe_intent", failed_recovery)

            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "pre-probe database changed outside the owned drain",
            ):
                controller.accept(
                    target_sha=TARGET_SHA,
                    operation_id=OPERATION_ID,
                )

        probes.assert_not_called()
        marker = CONTROLLER.load_private_json(controller.marker_path)
        self.assertNotIn("acceptance_probe_intent", marker)

    def test_passing_probe_report_survives_crash_before_observation_marker(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        def write_then_lose_response(*_args: object) -> None:
            self._write_passing_probe_report(controller)
            raise CONTROLLER.PullDeployError("lost passing probe response")

        with mock.patch.object(
            lifecycle,
            "run_acceptance_probes",
            side_effect=write_then_lose_response,
        ):
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError, "lost passing probe response"
            ):
                controller.accept(
                    target_sha=TARGET_SHA, operation_id=OPERATION_ID
                )
        crashed = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(crashed["phase"], "awaiting-acceptance")
        self.assertIn("acceptance_probe_intent", crashed)

        with mock.patch.object(
            lifecycle, "run_acceptance_probes"
        ) as probes:
            observing = controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )

        probes.assert_not_called()
        marker = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(observing["status"], "maintenance-observation")
        self.assertEqual(marker["phase"], "acceptance-started")
        self.assertEqual(
            marker["acceptance_evidence"]["probe_report_sha256"],
            observing["probe_report_sha256"],
        )
        self.assertFalse(lifecycle.admission_open)

    def test_final_accept_revalidates_sealed_probe_report_before_resume(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self._expire_acceptance_hold(controller)
        self._write_passing_probe_report(controller)
        observing = controller.accept(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self.assertEqual(observing["status"], "maintenance-observation")
        self._expire_acceptance_hold(controller)

        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        report_path = operation / f"production-acceptance-{OPERATION_ID}.json"
        report = CONTROLLER.load_private_json(report_path)
        report["sections"]["frontend"]["status"] = "failed"
        report_without_seal = dict(report)
        report_without_seal.pop("report_sha256")
        report["report_sha256"] = CONTROLLER.canonical_json_digest(
            report_without_seal
        )
        CONTROLLER.atomic_json(report_path, report)
        lifecycle.events.clear()

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "sealed passing result"
        ):
            controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )

        self.assertEqual(
            CONTROLLER.load_private_json(controller.marker_path)["phase"],
            "acceptance-started",
        )
        self.assertFalse(lifecycle.admission_open)
        self.assertNotIn("resume", lifecycle.events)

    def test_acceptance_probe_failure_can_rollback_with_database_restore(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )

        with mock.patch.object(
            lifecycle,
            "run_acceptance_probes",
            side_effect=CONTROLLER.PullDeployError("synthetic probe failure"),
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "synthetic probe failure"
        ):
            controller.accept(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            )

        marker = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(marker["phase"], "awaiting-acceptance")
        self.assertFalse(lifecycle.admission_open)
        with mock.patch.object(
            controller,
            "_rollback_failed_attempt",
            side_effect=lambda descriptor, marker: (
                CONTROLLER.PullDeployController._rollback_failed_attempt(
                    controller, descriptor, marker
                )
            ),
        ), mock.patch.object(
            CONTROLLER.SystemLifecycle,
            "_run_bootstrap_hook",
            return_value=None,
        ):
            result = controller.rollback(operation_id=OPERATION_ID)
        self.assertEqual(result["status"], "rejected-before-acceptance")
        self.assertIn("restore_database", lifecycle.events)
        self.assertFalse(controller.current_state_path.exists())
        self.assertFalse(controller.marker_path.exists())

    def test_staged_rollback_stop_crash_preserves_acceptance_provenance(
        self,
    ) -> None:
        operation_id = "deploy-20260814-accept-stop-crash"
        lifecycle = FakeLifecycle()
        controller, previous = self._rejected_acceptance_with_previous(
            lifecycle,
            operation_id=operation_id,
        )
        rejected = CONTROLLER.load_private_json(controller.marker_path)
        probe_intent = json.loads(
            json.dumps(rejected["acceptance_probe_intent"])
        )
        evidence = json.loads(json.dumps(rejected["acceptance_evidence"]))
        original_stop = lifecycle.stop
        lose_response = True
        stop_effects = 0

        def stop(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal lose_response, stop_effects
            if lifecycle.runtime_state != "stopped":
                stop_effects += 1
            original_stop(*args, **kwargs)
            if lose_response:
                lose_response = False
                raise CONTROLLER.PullDeployError(
                    "lost response after runtime stop commit"
                )

        lifecycle.stop = stop  # type: ignore[method-assign]
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lost response after runtime stop commit",
        ):
            controller.rollback(operation_id=operation_id)

        crashed = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(crashed["phase"], "runtime-stop-started")
        self.assertEqual(lifecycle.runtime_state, "stopped")
        self.assertEqual(stop_effects, 1)
        self.assertTrue(crashed["acceptance_rejected"])
        self.assertEqual(crashed["acceptance_probe_intent"], probe_intent)
        self.assertEqual(crashed["acceptance_evidence"], evidence)

        lifecycle.events.clear()
        self.assertIsNone(controller.recover_interrupted())
        self.assertEqual(stop_effects, 1)
        self.assertIn("restore_database", lifecycle.events)
        self.assertFalse(controller.marker_path.exists())
        self.assertEqual(
            CONTROLLER.load_private_json(controller.current_state_path),
            previous,
        )

    def test_staged_rollback_database_restore_lost_response_replays_safely(
        self,
    ) -> None:
        operation_id = "deploy-20260814-accept-restore-crash"
        lifecycle = FakeLifecycle()
        controller, previous = self._rejected_acceptance_with_previous(
            lifecycle,
            operation_id=operation_id,
        )
        rejected = CONTROLLER.load_private_json(controller.marker_path)
        probe_intent = json.loads(
            json.dumps(rejected["acceptance_probe_intent"])
        )
        evidence = json.loads(json.dumps(rejected["acceptance_evidence"]))
        original_restore = lifecycle.restore_database
        lose_response = True
        restored_generation = 1
        restore_effects = 0
        restore_calls = 0

        def restore(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal lose_response, restored_generation
            nonlocal restore_effects, restore_calls
            restore_calls += 1
            result = original_restore(*args, **kwargs)
            if restored_generation != 0:
                restored_generation = 0
                restore_effects += 1
            if lose_response:
                lose_response = False
                raise CONTROLLER.PullDeployError(
                    "lost response after database restore commit"
                )
            return result

        lifecycle.restore_database = restore  # type: ignore[method-assign]
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lost response after database restore commit",
        ):
            controller.rollback(operation_id=operation_id)

        crashed = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(crashed["phase"], "database-restore-started")
        self.assertTrue(crashed["database_restore_started"])
        self.assertEqual(restored_generation, 0)
        self.assertEqual(restore_effects, 1)
        self.assertEqual(restore_calls, 1)
        self.assertEqual(crashed["acceptance_probe_intent"], probe_intent)
        self.assertEqual(crashed["acceptance_evidence"], evidence)

        lifecycle.events.clear()
        self.assertIsNone(controller.recover_interrupted())
        # The controller cannot know whether the external restore committed
        # when its response was lost, so it deliberately reissues the exact
        # restore.  The fake generation models the required idempotence: the
        # second call leaves the already-restored generation unchanged.
        self.assertEqual(restore_calls, 2)
        self.assertEqual(restore_effects, 1)
        self.assertEqual(restored_generation, 0)
        self.assertIn("restore_database", lifecycle.events)
        self.assertFalse(controller.marker_path.exists())
        self.assertEqual(
            CONTROLLER.load_private_json(controller.current_state_path),
            previous,
        )

    def test_lost_rollback_resume_preserves_post_open_writes(self) -> None:
        operation_id = "deploy-20260814-accept-resume-loss"
        lifecycle = LostResumeLifecycle()
        controller, previous = self._rejected_acceptance_with_previous(
            lifecycle,
            operation_id=operation_id,
        )
        restore_calls = 0
        accepted_writes = 0
        original_restore = lifecycle.restore_database

        def restore(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal restore_calls, accepted_writes
            restore_calls += 1
            accepted_writes = 0
            return original_restore(*args, **kwargs)

        lifecycle.restore_database = restore  # type: ignore[method-assign]
        lifecycle.lose_next_resume = True
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lost response after admission commit",
        ):
            controller.rollback(operation_id=operation_id)

        crashed = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(
            crashed["phase"], "rollback-admission-resume-started"
        )
        self.assertIn("rollback_admission_resume_intent", crashed)
        self.assertIn("acceptance_probe_intent", crashed)
        self.assertIn("acceptance_evidence", crashed)
        intent = crashed["rollback_admission_resume_intent"]
        self.assertEqual(
            intent["candidate_state_sha256"],
            crashed["candidate_state_sha256"],
        )
        self.assertEqual(
            intent["previous_authority_sha256"],
            CONTROLLER.sha256_file(controller.current_state_path),
        )
        self.assertTrue(lifecycle.admission_open)
        self.assertEqual(restore_calls, 1)

        # Model a write accepted after the old runtime became public but
        # before the controller observed the resume response.
        accepted_writes += 1
        lifecycle.events.clear()
        retried = controller.rollback(operation_id=operation_id)

        self.assertEqual(retried["status"], "rejected-before-acceptance")
        self.assertEqual(accepted_writes, 1)
        self.assertEqual(restore_calls, 1)
        self.assertEqual(lifecycle.events, ["admission-status", "verify-open"])
        self.assertNotIn("restore_database", lifecycle.events)
        self.assertNotIn("stop", lifecycle.events)
        self.assertNotIn("recovery-isolate", lifecycle.events)
        self.assertFalse(controller.marker_path.exists())
        self.assertEqual(
            CONTROLLER.load_private_json(controller.current_state_path),
            previous,
        )

    def test_partial_rollback_resume_recovers_forward_without_restore(self) -> None:
        class PartialResumeLifecycle(FakeLifecycle):
            lose_after_persistent_resume = False
            ingress_open = False

            def resume(self, controller, descriptor, expected):  # type: ignore[no-untyped-def]
                marker = CONTROLLER.load_private_json(
                    getattr(controller, "marker_path")
                )
                if marker.get("verification") != expected:
                    raise AssertionError("rollback resume fence was not durable")
                if self.lose_after_persistent_resume:
                    self.lose_after_persistent_resume = False
                    self._event("resume")
                    self.admission_open = True
                    self.ingress_open = False
                    raise CONTROLLER.PullDeployError(
                        "lost response after persistent resume"
                    )
                super().resume(controller, descriptor, expected)
                self.ingress_open = True

            def verify_open_runtime(  # type: ignore[no-untyped-def]
                self, controller, descriptor, expected
            ):
                self._event("verify-open")
                if not self.ingress_open:
                    raise CONTROLLER.PullDeployError(
                        "public ingress is not open"
                    )
                if expected != self.verification():
                    raise CONTROLLER.PullDeployError(
                        "open runtime instance differs from committed verification"
                    )

        operation_id = "deploy-20260814-partial-resume"
        lifecycle = PartialResumeLifecycle()
        controller, _previous = self._rejected_acceptance_with_previous(
            lifecycle,
            operation_id=operation_id,
        )
        restore_calls = 0
        original_restore = lifecycle.restore_database

        def restore(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal restore_calls
            restore_calls += 1
            return original_restore(*args, **kwargs)

        lifecycle.restore_database = restore  # type: ignore[method-assign]
        lifecycle.lose_after_persistent_resume = True
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lost response after persistent resume",
        ):
            controller.rollback(operation_id=operation_id)
        self.assertEqual(restore_calls, 1)
        self.assertTrue(lifecycle.admission_open)
        self.assertFalse(lifecycle.ingress_open)

        lifecycle.events.clear()
        self.assertIsNone(controller.recover_interrupted())

        self.assertEqual(restore_calls, 1)
        self.assertTrue(lifecycle.admission_open)
        self.assertTrue(lifecycle.ingress_open)
        self.assertEqual(
            lifecycle.events[:2], ["admission-status", "verify-open"]
        )
        self.assertIn("recovery-isolate", lifecycle.events)
        self.assertIn("recovery-redrain", lifecycle.events)
        self.assertIn("resume", lifecycle.events)
        self.assertNotIn("restore_database", lifecycle.events)
        self.assertNotIn("stop", lifecycle.events)
        self.assertFalse(controller.marker_path.exists())

    def test_staged_rollback_retires_candidate_before_acceptance(self) -> None:
        controller = self.controller()
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply_staged(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )

        result = controller.rollback(operation_id=OPERATION_ID)

        self.assertEqual(result["status"], "rejected-before-acceptance")
        self.assertFalse(controller.current_state_path.exists())
        self.assertFalse(controller.marker_path.exists())
        operation_state = CONTROLLER.load_private_json(
            controller.audit_dir / OPERATION_ID / "operation-state.json"
        )
        self.assertEqual(operation_state["outcome"], "failed")
        self.assertTrue(
            (
                controller.audit_dir
                / OPERATION_ID
                / "rejected-before-acceptance.json"
            ).exists()
        )

    def test_pre_stop_rollback_validates_previous_before_resume(
        self,
    ) -> None:
        previous = {"fixture": "previous-state"}
        fake = SimpleNamespace(
            lifecycle=mock.Mock(),
            _is_pre_stop_abort_marker=mock.Mock(return_value=True),
            _adopted_previous=CONTROLLER.PullDeployController._adopted_previous,
            _validate_steady_deployment_state=mock.Mock(
                side_effect=CONTROLLER.PullDeployError(
                    "injected previous terminal failure"
                )
            ),
            _previous_runtime_descriptor=mock.Mock(),
            _recover_unchanged_and_resume=mock.Mock(),
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "previous terminal failure",
        ):
            CONTROLLER.PullDeployController._rollback_failed_attempt(
                fake,
                {"previous_deployment": previous},
                {},
            )
        fake._validate_steady_deployment_state.assert_called_once_with(
            previous
        )
        fake._previous_runtime_descriptor.assert_not_called()
        fake._recover_unchanged_and_resume.assert_not_called()

    def test_stopped_start_unknown_commit_recovers_authorized_new_instance(
        self,
    ) -> None:
        class LostStartLifecycle(FakeLifecycle):
            lose_start = True

            def start(self, controller, _descriptor):  # type: ignore[no-untyped-def]
                persisted = CONTROLLER.load_private_json(controller.marker_path)
                self.assert_postgres_fence(persisted)
                self._event("start")
                self.runtime_state = "live"
                self.admission_open = False
                self.recovery_fence = {"fixture_instance": "started-instance"}
                if self.lose_start:
                    self.lose_start = False
                    raise CONTROLLER.PullDeployError("injected start response loss")

            @staticmethod
            def assert_postgres_fence(marker):  # type: ignore[no-untyped-def]
                expected = CONTROLLER.postgres_runtime_fence_identity(
                    {
                        "schema_version": 1,
                        **mutable_data_evidence()["postgres_runtime"],
                        "captured_at": marker["postgres_runtime_fence"][
                            "captured_at"
                        ],
                    }
                )
                actual = CONTROLLER.postgres_runtime_fence_identity(
                    marker["postgres_runtime_fence"]
                )
                if actual != expected:
                    raise AssertionError(
                        "PostgreSQL runtime fence was not durable before start"
                    )

        lifecycle = LostStartLifecycle()
        lifecycle.runtime_state = "stopped"
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        marker = {
            "phase": "state-committed",
            "updated_at": CONTROLLER.utc_now(),
        }
        CONTROLLER.atomic_json(controller.marker_path, marker)

        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "start response loss"):
            controller._recover_runtime_and_resume(
                marker,
                descriptor,
                allow_unfenced=False,
            )
        persisted = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(
            persisted["runtime_start_intent"]["target_sha"],
            TARGET_SHA,
        )
        self.assertEqual(
            CONTROLLER.postgres_runtime_fence_identity(
                persisted["postgres_runtime_fence"]
            ),
            {
                "schema_version": 1,
                **mutable_data_evidence()["postgres_runtime"],
            },
        )
        self.assertNotIn("verification", persisted)

        lifecycle.events.clear()
        controller._recover_runtime_and_resume(
            persisted,
            descriptor,
            allow_unfenced=False,
        )
        self.assertEqual(
            lifecycle.events,
            ["recovery-isolate", "recovery-redrain", "verify", "resume"],
        )
        final_marker = CONTROLLER.load_private_json(controller.marker_path)
        self.assertNotIn("runtime_start_intent", final_marker)
        self.assertEqual(
            final_marker["verification"]["recovery_fence"],
            {"fixture_instance": "started-instance"},
        )

    def test_apply_uses_prepared_evidence_and_commits_state_after_verification(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self.assertEqual(state["source_sha"], TARGET_SHA)
        self.assertEqual(controller.source_sha, TARGET_SHA)
        self.assertEqual(
            lifecycle.events,
            [
                "drain",
                "recovery-isolate",
                "recovery-redrain",
                "stop",
                "backup",
                "migrate",
                "start",
                "verify",
                "recovery-isolate",
                "recovery-redrain",
                "verify",
                "recovery-isolate",
                "recovery-redrain",
                "verify-acceptance-stability",
                "admission-status",
                "recovery-isolate",
                "recovery-redrain",
                "verify-acceptance-stability",
                "resume",
            ],
        )
        self.assertFalse((self.runtime / "state/deploy-in-progress.json").exists())
        self.assertTrue(
            (self.runtime / "audit" / OPERATION_ID / "success.json").is_file()
        )
        current = CONTROLLER.load_private_json(
            self.runtime / "state/current-deployment.json"
        )
        self.assertEqual(current["active_monomer_md_slot"]["slot"], "a")
        self.assertEqual(
            [record["version"] for record in current["migrations"]],
            [record["version"] for record in B_MANIFEST_RECORDS[:11]],
        )

    def test_current_state_cas_rejects_drift_and_accepts_exact_write_response_loss(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        previous = controller.apply(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        previous_digest = CONTROLLER.sha256_file(
            controller.current_state_path
        )
        candidate = json.loads(json.dumps(previous))
        candidate["deployed_at"] = "2026-07-18T12:00:00Z"
        candidate_digest = CONTROLLER.sha256_bytes(
            CONTROLLER.canonical_json_bytes(candidate) + b"\n"
        )
        original_atomic_json = CONTROLLER.atomic_json
        lost = False

        def lose_response(path, value, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal lost
            result = original_atomic_json(path, value, *args, **kwargs)
            if Path(path) == controller.current_state_path and not lost:
                lost = True
                raise OSError("injected lost state write response")
            return result

        with (
            mock.patch.object(
                CONTROLLER,
                "atomic_json",
                side_effect=lose_response,
            ),
            self.assertRaisesRegex(OSError, "lost state write response"),
        ):
            controller._commit_current_state_cas(
                candidate,
                candidate_sha256=candidate_digest,
                expected_pre_state=previous,
                expected_pre_state_sha256=previous_digest,
            )
        self.assertEqual(
            controller._commit_current_state_cas(
                candidate,
                candidate_sha256=candidate_digest,
                expected_pre_state=previous,
                expected_pre_state_sha256=previous_digest,
            ),
            "already-committed",
        )

        pretty_payload = (
            json.dumps(previous, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        controller.current_state_path.write_bytes(pretty_payload)
        os.chmod(controller.current_state_path, 0o600)
        with controller.current_state_path.open("rb") as stream:
            os.fsync(stream.fileno())
        CONTROLLER.fsync_directory(
            controller.current_state_path.parent
        )
        pretty_digest = CONTROLLER.sha256_file(
            controller.current_state_path
        )
        self.assertNotEqual(pretty_digest, previous_digest)
        self.assertEqual(
            controller._commit_current_state_cas(
                candidate,
                candidate_sha256=candidate_digest,
                expected_pre_state=previous,
                expected_pre_state_sha256=pretty_digest,
            ),
            "committed",
        )

        CONTROLLER.atomic_json(controller.current_state_path, previous)
        drifted = json.loads(json.dumps(previous))
        drifted["deployed_at"] = "2026-07-18T12:00:01Z"
        CONTROLLER.atomic_json(controller.current_state_path, drifted)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "changed before commit",
        ):
            controller._commit_current_state_cas(
                candidate,
                candidate_sha256=candidate_digest,
                expected_pre_state=previous,
                expected_pre_state_sha256=previous_digest,
            )

    def test_normal_apply_reloads_pre_state_after_long_transaction(self) -> None:
        controller = self.controller()
        controller.prepare(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        controller.apply(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        successor = "deploy-normal-state-cas-drift"
        controller.prepare(
            target_sha=TARGET_SHA, operation_id=successor
        )
        original_revalidate = (
            controller._revalidate_candidate_database_state
        )
        drifted: dict[str, object] | None = None

        def drift_before_commit(descriptor, state, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal drifted
            result = original_revalidate(
                descriptor, state, **kwargs
            )
            if state.get("operation_id") == successor:
                drifted = CONTROLLER.load_private_json(
                    controller.current_state_path
                )
                drifted["deployed_at"] = "2026-07-18T12:00:02Z"
                CONTROLLER.atomic_json(
                    controller.current_state_path, drifted
                )
            return result

        with (
            mock.patch.object(
                controller,
                "_revalidate_candidate_database_state",
                side_effect=drift_before_commit,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "state commit is ambiguous",
            ),
        ):
            controller.apply(
                target_sha=TARGET_SHA, operation_id=successor
            )
        self.assertEqual(
            CONTROLLER.load_private_json(
                controller.current_state_path
            ),
            drifted,
        )
        self.assertTrue(controller.marker_path.exists())

    def test_terminal_v4_rollback_requires_forward_fix_before_side_effects(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        deployed = controller.apply(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        self.assertEqual(
            descriptor["schema_version"], CONTROLLER.DESCRIPTOR_SCHEMA_VERSION
        )
        self.assertTrue(lifecycle.admission_open)
        self.assertFalse(controller.marker_path.exists())
        state_bytes = controller.current_state_path.read_bytes()
        source_identity = controller.repository_identity()
        backup_inventory = sorted(
            (
                path.relative_to(controller.backups_dir).as_posix(),
                CONTROLLER.sha256_file(path),
            )
            for path in controller.backups_dir.rglob("*")
            if path.is_file()
        )
        lifecycle.events.clear()
        with (
            mock.patch.object(controller, "_write_marker") as write_marker,
            mock.patch.object(lifecycle, "drain") as drain,
            mock.patch.object(lifecycle, "backup_rollback") as backup,
            mock.patch.object(lifecycle, "stop") as stop,
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "forbidden after public admission; deploy a forward fix",
            ),
        ):
            controller.rollback(operation_id=OPERATION_ID)

        write_marker.assert_not_called()
        drain.assert_not_called()
        backup.assert_not_called()
        stop.assert_not_called()
        self.assertEqual(lifecycle.events, [])
        self.assertFalse(controller.marker_path.exists())
        self.assertEqual(
            controller.current_state_path.read_bytes(), state_bytes
        )
        self.assertEqual(controller.repository_identity(), source_identity)
        self.assertEqual(
            sorted(
                (
                    path.relative_to(controller.backups_dir).as_posix(),
                    CONTROLLER.sha256_file(path),
                )
                for path in controller.backups_dir.rglob("*")
                if path.is_file()
            ),
            backup_inventory,
        )
        self.assertEqual(
            controller._load_operation_state(OPERATION_ID)["outcome"],
            "deployed",
        )
        self.assertEqual(
            CONTROLLER.load_private_json(controller.current_state_path),
            deployed,
        )

    def test_steady_state_requires_outcome_and_exact_success_audit(self) -> None:
        controller = self.controller()
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        state = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        operation_state = (
            controller.audit_dir / OPERATION_ID / "operation-state.json"
        )
        operation_state.unlink()
        CONTROLLER.fsync_directory(operation_state.parent)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "deployed terminal outcome",
        ):
            controller._validate_steady_deployment_state(
                state,
                revalidate_live=False,
            )

        controller._record_operation_outcome(
            operation_id=OPERATION_ID,
            descriptor_sha256=state["descriptor_sha256"],
            outcome="deployed",
        )
        success = controller.audit_dir / OPERATION_ID / "success.json"
        success.unlink()
        CONTROLLER.fsync_directory(success.parent)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "exact immutable success audit",
        ):
            controller._validate_steady_deployment_state(
                state,
                revalidate_live=False,
            )

    def test_steady_state_rejects_replayed_pre_0012_success_state(self) -> None:
        controller = self.controller()
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        state = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        contract_operation = "contract-0012-replay-fixture"
        contract_root = (
            controller.runtime_root / "state" / "contract-operations"
        )
        contract_root.mkdir(mode=0o700)
        CONTROLLER.atomic_json(
            contract_root / f"{contract_operation}.json",
            {
                "schema_version": 2,
                "status": "success",
                "operation_id": contract_operation,
                "deployment_operation_id": OPERATION_ID,
                "pull_descriptor_sha256": state["descriptor_sha256"],
            },
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "omits an already successful 0012",
        ):
            controller._validate_steady_deployment_state(
                state,
                revalidate_live=False,
            )

    def test_failure_is_rolled_back_and_audited_without_leaving_marker(self) -> None:
        lifecycle = FakeLifecycle(fail_at="start")
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "injected start"):
            controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self.assertTrue(controller.rollback_called)
        self.assertEqual(controller.source_sha, PREVIOUS_SHA)
        self.assertFalse((self.runtime / "state/deploy-in-progress.json").exists())
        failed = CONTROLLER.load_private_json(
            self.runtime / "audit" / OPERATION_ID / "failed.json"
        )
        self.assertEqual(failed["rollback"], "success")

    def test_committed_state_crash_recovers_forward_instead_of_restoring_database(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": CONTROLLER.load_private_json(descriptor_path)[
                "controller"
            ]["executor_control"],
            "executor_control_sha256": CONTROLLER.load_private_json(descriptor_path)[
                "controller"
            ]["executor_control_sha256"],
            "phase": "state-committed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": True,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": True,
            "database_change_started": True,
            "verification": lifecycle.verification(),
            "candidate_state": state,
            "candidate_state_sha256": CONTROLLER.sha256_bytes(
                CONTROLLER.canonical_json_bytes(state) + b"\n"
            ),
        }
        CONTROLLER.atomic_json(
            controller.marker_path, v4_recovery_marker(marker)
        )
        lifecycle.events.clear()
        recovered = controller.recover_interrupted()
        self.assertEqual(recovered, state)
        self.assertEqual(lifecycle.events, [])
        self.assertEqual(
            CONTROLLER.load_private_json(controller.marker_path)["phase"],
            "awaiting-acceptance",
        )

    def test_open_admission_unknown_commit_keeps_marker_on_instance_drift(self) -> None:
        initial = FakeLifecycle()
        controller = self.controller(lifecycle=initial)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        verification = {"health": "ok", "recovery_fence": {"fixture": True}}
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "state-committed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": True,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": True,
            "database_change_started": True,
            "verification": verification,
            "candidate_state": state,
            "candidate_state_sha256": CONTROLLER.sha256_bytes(
                CONTROLLER.canonical_json_bytes(state) + b"\n"
            ),
        }
        CONTROLLER.atomic_json(
            controller.marker_path, v4_recovery_marker(marker)
        )

        class RestartedOpenLifecycle(FakeLifecycle):
            def verify_open_runtime(
                self,
                _controller: object,
                _descriptor: object,
                expected_verification: object | None = None,
            ) -> None:
                self._event("verify-open-restarted")
                if expected_verification != verification:
                    raise AssertionError(expected_verification)
                raise CONTROLLER.PullDeployError(
                    "open runtime instance differs from committed verification"
                )

        restarted = RestartedOpenLifecycle(admission_open=True)
        controller.lifecycle = restarted
        self.assertEqual(controller.recover_interrupted(), state)
        self.assertTrue(controller.marker_path.is_file())
        self.assertEqual(restarted.events, [])
        self.assertNotIn("stop", restarted.events)
        self.assertNotIn("start", restarted.events)

    def test_candidate_reverify_fence_survives_lost_resume_response(self) -> None:
        lifecycle = LostResumeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "state-committed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": True,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": True,
            "database_change_started": True,
            "verification": {
                "health": "ok",
                "recovery_fence": {"fixture_instance": "stale-instance"},
            },
            "candidate_state": state,
            "candidate_state_sha256": CONTROLLER.sha256_bytes(
                CONTROLLER.canonical_json_bytes(state) + b"\n"
            ),
        }
        CONTROLLER.atomic_json(
            controller.marker_path, v4_recovery_marker(marker)
        )
        lifecycle.admission_open = False
        lifecycle.runtime_state = "stopped"
        lifecycle.recovery_fence = {"fixture_instance": "reverified-instance"}
        lifecycle.lose_next_resume = True
        lifecycle.events.clear()

        self.assertEqual(controller.recover_interrupted(), state)
        persisted = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(persisted["phase"], "awaiting-acceptance")
        self.assertFalse(lifecycle.admission_open)

        lifecycle.events.clear()
        recovered = controller.recover_interrupted()
        self.assertEqual(recovered, state)
        self.assertEqual(lifecycle.events[0], "recovery-isolate")
        self.assertNotIn("resume", lifecycle.events)
        self.assertTrue(controller.marker_path.exists())

    def test_admission_resumed_recovery_only_verifies_open_runtime(self) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "admission-resumed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": True,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": True,
            "database_change_started": True,
            "verification": lifecycle.verification(),
            "candidate_state": state,
            "candidate_state_sha256": CONTROLLER.sha256_bytes(
                CONTROLLER.canonical_json_bytes(state) + b"\n"
            ),
        }
        CONTROLLER.atomic_json(
            controller.marker_path, v4_recovery_marker(marker)
        )
        lifecycle.events.clear()

        recovered = controller.recover_interrupted()

        self.assertEqual(recovered, state)
        self.assertEqual(lifecycle.events, ["verify-open"])
        self.assertFalse(controller.marker_path.exists())

    def test_explicit_rollback_unknown_commit_rejects_changed_instance(self) -> None:
        lifecycle = LostResumeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        self._prepare_legacy_v2(controller, OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        second_operation = "deploy-20260716-explicit-fence"
        self._prepare_legacy_v2(controller, second_operation)
        controller.apply(target_sha=TARGET_SHA, operation_id=second_operation)
        lifecycle.lose_next_resume = True
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "lost response after admission commit"
        ):
            controller.rollback(operation_id=second_operation)
        persisted = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(persisted["verification"], lifecycle.verification())

        lifecycle.recovery_fence = {"fixture_instance": "replacement-instance"}
        lifecycle.events.clear()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "differs from committed verification"
        ):
            controller.recover_interrupted()
        self.assertTrue(controller.marker_path.is_file())
        self.assertEqual(lifecycle.events, ["recovery-isolate"])
        self.assertNotIn("start", lifecycle.events)
        self.assertNotIn("stop", lifecycle.events)

    def test_failed_deploy_rollback_unknown_commit_rejects_changed_instance(
        self,
    ) -> None:
        class FailedCandidateLifecycle(LostResumeLifecycle):
            fail_next_verify = False

            def verify(self, controller, descriptor):  # type: ignore[no-untyped-def]
                if self.fail_next_verify:
                    self._event("verify")
                    self.fail_next_verify = False
                    self.lose_next_resume = True
                    raise CONTROLLER.PullDeployError(
                        "injected candidate verification failure"
                    )
                return super().verify(controller, descriptor)

        lifecycle = FailedCandidateLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller._rollback_failed_attempt = (  # type: ignore[method-assign]
            CONTROLLER.PullDeployController._rollback_failed_attempt.__get__(
                controller, CONTROLLER.PullDeployController
            )
        )
        # The fixture uses one synthetic target SHA for every operation.  A
        # real failed upgrade has a distinct candidate, so project the sealed
        # previous state as non-candidate for this rollback-path test.
        controller._candidate_current_state = lambda *_args: None  # type: ignore[method-assign]
        second_operation = "deploy-20260716-failed-rollback-fence"
        controller.prepare(target_sha=TARGET_SHA, operation_id=second_operation)
        lifecycle.fail_next_verify = True
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "deployment and rollback failed",
        ):
            controller.apply(target_sha=TARGET_SHA, operation_id=second_operation)
        persisted = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(persisted["verification"], lifecycle.verification())
        self.assertTrue(lifecycle.admission_open)

        controller._reconcile_effect_commit_windows = lambda *_args: None  # type: ignore[method-assign]
        lifecycle.recovery_fence = {"fixture_instance": "replacement-instance"}
        lifecycle.events.clear()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "differs from committed verification"
        ):
            controller.recover_interrupted()
        self.assertTrue(controller.marker_path.is_file())
        self.assertEqual(
            lifecycle.events,
            ["admission-status", "verify-open", "recovery-isolate"],
        )
        self.assertNotIn("start", lifecycle.events)
        self.assertNotIn("stop", lifecycle.events)
        self.assertNotIn("restore_database", lifecycle.events)

    def test_pre_stop_unknown_commit_rejects_changed_instance(self) -> None:
        lifecycle = LostUnchangedResumeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller._rollback_failed_attempt = (  # type: ignore[method-assign]
            CONTROLLER.PullDeployController._rollback_failed_attempt.__get__(
                controller, CONTROLLER.PullDeployController
            )
        )
        operation = "deploy-20260716-prestop-fence"
        controller.prepare(target_sha=TARGET_SHA, operation_id=operation)
        _directory, descriptor_path, _ready = controller._operation_paths(operation)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": operation,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "drained",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": False,
            "source_switched": False,
            "slot_switched": False,
            "control_switched": False,
            "unit_switched": False,
            "asset_switched": False,
            "database_change_started": False,
            "drain": {"active_total": 0},
        }
        CONTROLLER.atomic_json(
            controller.marker_path, v4_recovery_marker(marker)
        )
        lifecycle.admission_open = False
        lifecycle.lose_next_unchanged_resume = True
        lifecycle.events.clear()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "lost unchanged-resume response"
        ):
            controller.recover_interrupted()
        persisted = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(persisted["verification"], lifecycle.verification())
        self.assertTrue(lifecycle.admission_open)

        lifecycle.recovery_fence = {"fixture_instance": "replacement-instance"}
        lifecycle.events.clear()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "differs from committed verification"
        ):
            controller.recover_interrupted()
        self.assertTrue(controller.marker_path.is_file())
        self.assertEqual(lifecycle.events, ["recovery-isolate"])
        self.assertNotIn("start", lifecycle.events)
        self.assertNotIn("stop", lifecycle.events)

    def test_pre_stop_crashes_resume_unchanged_without_reconcile_or_restart(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller._rollback_failed_attempt = (  # type: ignore[method-assign]
            CONTROLLER.PullDeployController._rollback_failed_attempt.__get__(
                controller, CONTROLLER.PullDeployController
            )
        )

        phases = [
            ("drain-started", None),
            ("drained", None),
            ("failed", "drained"),
        ]
        for index, (phase, failed_phase) in enumerate(phases, start=2):
            operation = f"deploy-20260716-prestop-{index}"
            controller.prepare(target_sha=TARGET_SHA, operation_id=operation)
            _directory, descriptor_path, _ready = controller._operation_paths(operation)
            descriptor = CONTROLLER.load_private_json(descriptor_path)
            marker = {
                "schema_version": 2,
                "action": "deploy",
                "operation_id": operation,
                "source_sha": TARGET_SHA,
                "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
                "executor_control": descriptor["controller"]["executor_control"],
                "executor_control_sha256": descriptor["controller"][
                    "executor_control_sha256"
                ],
                "phase": phase,
                "started_at": CONTROLLER.utc_now(),
                "updated_at": CONTROLLER.utc_now(),
                "runtime_stopped": False,
                "source_switched": False,
                "slot_switched": False,
                "control_switched": False,
                "unit_switched": False,
                "asset_switched": False,
                "database_change_started": False,
            }
            if phase in {"drained", "failed"}:
                marker["drain"] = {"active_total": 0}
            if failed_phase is not None:
                marker["failed_phase"] = failed_phase
            CONTROLLER.atomic_json(
                controller.marker_path, v4_recovery_marker(marker)
            )
            lifecycle.admission_open = False
            lifecycle.events.clear()
            recovered = controller.recover_interrupted()
            self.assertIsNone(recovered)
            expected = [
                "recovery-isolate",
                "recovery-redrain",
                "resume-unchanged",
            ]
            self.assertEqual(lifecycle.events, expected)
            self.assertNotIn("start", lifecycle.events)
            self.assertNotIn("stop", lifecycle.events)
            self.assertFalse(controller.marker_path.exists())

    def test_open_pre_stop_intent_without_fence_isolated_and_redrained(self) -> None:
        lifecycle = FakeLifecycle(admission_open=True)
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller._rollback_failed_attempt = (  # type: ignore[method-assign]
            CONTROLLER.PullDeployController._rollback_failed_attempt.__get__(
                controller, CONTROLLER.PullDeployController
            )
        )
        operation = "deploy-20260716-prestop-no-fence"
        controller.prepare(target_sha=TARGET_SHA, operation_id=operation)
        _directory, descriptor_path, _ready = controller._operation_paths(operation)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": operation,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "prepared",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": False,
            "source_switched": False,
            "slot_switched": False,
            "control_switched": False,
            "unit_switched": False,
            "asset_switched": False,
            "database_change_started": False,
        }
        CONTROLLER.atomic_json(
            controller.marker_path, v4_recovery_marker(marker)
        )
        lifecycle.events.clear()
        self.assertIsNone(controller.recover_interrupted())
        self.assertFalse(controller.marker_path.exists())
        self.assertEqual(
            lifecycle.events,
            ["recovery-isolate", "recovery-redrain", "resume-unchanged"],
        )
        self.assertNotIn("start", lifecycle.events)
        self.assertNotIn("stop", lifecycle.events)

    def test_first_bootstrap_pre_stop_uses_only_legacy_unchanged_resume(self) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _directory, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        self.assertIsNone(descriptor["previous_deployment"])
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "drain-started",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": False,
            "source_switched": False,
            "slot_switched": False,
            "control_switched": False,
            "unit_switched": False,
            "asset_switched": False,
            "database_change_started": False,
        }
        CONTROLLER.atomic_json(
            controller.marker_path, v4_recovery_marker(marker)
        )
        controller._rollback_failed_attempt = (  # type: ignore[method-assign]
            CONTROLLER.PullDeployController._rollback_failed_attempt.__get__(
                controller, CONTROLLER.PullDeployController
            )
        )
        controller.recover_interrupted()
        self.assertEqual(lifecycle.events, ["resume-bootstrap-unchanged"])
        self.assertNotIn("stop", lifecycle.events)

    def test_first_bootstrap_lost_restore_response_never_restarts_open_legacy(
        self,
    ) -> None:
        lifecycle = FakeLifecycle(admission_open=True)
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _directory, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "failed",
            "failed_phase": "runtime-started",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": False,
            "slot_switched": False,
            "control_switched": False,
            "unit_switched": False,
            "asset_switched": False,
            "database_change_started": False,
        }
        marker = v4_recovery_marker(marker)
        controller._reconcile_effect_commit_windows = lambda *_args: None  # type: ignore[method-assign]
        CONTROLLER.PullDeployController._rollback_failed_attempt(
            controller, descriptor, marker
        )
        self.assertEqual(
            lifecycle.events,
            ["bootstrap-admission-status", "resume-bootstrap-unchanged"],
        )
        self.assertNotIn("stop", lifecycle.events)

    def test_post_canary_crash_persists_redrain_before_idempotent_stop_retry(
        self,
    ) -> None:
        lifecycle = FakeLifecycle(fail_at="stop")
        controller = self.controller(lifecycle=lifecycle)
        controller._rollback_failed_attempt = (  # type: ignore[method-assign]
            CONTROLLER.PullDeployController._rollback_failed_attempt.__get__(
                controller, CONTROLLER.PullDeployController
            )
        )
        controller._reconcile_effect_commit_windows = lambda *_args: None  # type: ignore[method-assign]
        descriptor = {"previous_deployment": {"source_sha": PREVIOUS_SHA}}
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": "sha256:" + "a" * 64,
            "phase": "failed",
            "failed_phase": "verifying",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": False,
            "slot_switched": False,
            "control_switched": False,
            "unit_switched": False,
            "asset_switched": False,
            "database_change_started": False,
        }
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "injected stop"):
            controller._rollback_failed_attempt(descriptor, marker)
        self.assertEqual(
            lifecycle.events,
            ["recovery-isolate", "recovery-redrain", "stop"],
        )
        self.assertEqual(marker["phase"], "runtime-stop-started")
        self.assertIn("drain", marker)

        lifecycle.events.clear()
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "injected stop"):
            controller._rollback_failed_attempt(descriptor, marker)
        self.assertEqual(
            lifecycle.events,
            ["recovery-isolate", "recovery-redrain", "stop"],
        )

    def test_recovery_marker_rejects_invalid_pre_stop_and_rollback_evidence(
        self,
    ) -> None:
        controller = self.controller(lifecycle=FakeLifecycle())
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _directory, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        descriptor_digest = CONTROLLER.sha256_file(descriptor_path)
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": descriptor_digest,
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "drain-started",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": False,
            "source_switched": False,
            "slot_switched": False,
            "control_switched": False,
            "unit_switched": False,
            "asset_switched": False,
            "database_change_started": False,
            "pre_stop_abort": False,
        }
        marker = v4_recovery_marker(marker)
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "pre-stop abort flag"):
            CONTROLLER.validate_recovery_marker(
                marker,
                descriptor=descriptor,
                descriptor_digest=descriptor_digest,
            )
        marker.pop("pre_stop_abort")
        marker.update(
            action="explicit-rollback",
            phase="explicit-rollback-stop-started",
            rollback_current_state_sha256="sha256:" + "a" * 64,
            rollback_source_terminal_audit_sha256="sha256:" + "b" * 64,
            rollback_attempt_id="rollback-attempt-fixture-001",
            rollback_backup_operation_id="rollback-independent-001",
        )
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "drain evidence"):
            CONTROLLER.validate_recovery_marker(
                marker,
                descriptor=descriptor,
                descriptor_digest=descriptor_digest,
            )

    def test_explicit_rollback_uses_independent_backup_and_reuses_it_after_crash(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        self._prepare_legacy_v2(controller, OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)

        second_operation = "deploy-20260716-0002"
        self._prepare_legacy_v2(controller, second_operation)
        controller.apply(target_sha=TARGET_SHA, operation_id=second_operation)
        lifecycle.events.clear()

        original_restore = controller._restore_source
        restore_calls = 0

        def fail_restore_once(descriptor):  # type: ignore[no-untyped-def]
            nonlocal restore_calls
            restore_calls += 1
            if restore_calls == 1:
                raise CONTROLLER.PullDeployError("injected source restore crash")
            return original_restore(descriptor)

        controller._restore_source = fail_restore_once  # type: ignore[method-assign]
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "source restore crash"):
            controller.rollback(operation_id=second_operation)
        marker = CONTROLLER.load_private_json(controller.marker_path)
        rollback_backup = marker["rollback_backup"]
        self.assertNotEqual(
            Path(rollback_backup["path"]).parent,
            controller.backups_dir / second_operation,
        )
        self.assertEqual(lifecycle.events.count("backup"), 1)

        controller._restore_source = original_restore  # type: ignore[method-assign]
        recovered = controller.recover_interrupted()
        self.assertEqual(recovered["operation_id"], OPERATION_ID)
        self.assertEqual(lifecycle.events.count("backup"), 1)
        self.assertFalse(controller.marker_path.exists())

    def test_explicit_rollback_recovers_write_before_phase_commit(self) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        self._prepare_legacy_v2(controller, OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        second_operation = "deploy-20260716-rollback-write-window"
        self._prepare_legacy_v2(controller, second_operation)
        controller.apply(
            target_sha=TARGET_SHA,
            operation_id=second_operation,
        )
        original_atomic_json = CONTROLLER.atomic_json
        lost_response = False

        def lose_current_state_response(path, value, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal lost_response
            result = original_atomic_json(path, value, *args, **kwargs)
            if (
                not lost_response
                and Path(path) == controller.current_state_path
                and isinstance(value, dict)
                and value.get("rollback_provenance") is not None
            ):
                lost_response = True
                raise CONTROLLER.PullDeployError(
                    "injected lost rollback state-write response"
                )
            return result

        with (
            mock.patch.object(
                CONTROLLER,
                "atomic_json",
                side_effect=lose_current_state_response,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "lost rollback state-write response",
            ),
        ):
            controller.rollback(operation_id=second_operation)
        marker = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(
            marker["phase"],
            "explicit-rollback-state-commit-started",
        )
        self.assertEqual(
            CONTROLLER.sha256_file(controller.current_state_path),
            marker["rollback_candidate_state_sha256"],
        )

        recovered = controller.recover_interrupted()

        self.assertEqual(recovered["operation_id"], OPERATION_ID)
        self.assertFalse(controller.marker_path.exists())
        self.assertEqual(
            controller._load_operation_state(second_operation)["outcome"],
            "rolled-back",
        )

    def test_explicit_rollback_preserves_pretty_previous_file_digest(self) -> None:
        controller = self.controller(lifecycle=FakeLifecycle())
        self._prepare_legacy_v2(controller, OPERATION_ID)
        previous = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        canonical_digest = CONTROLLER.sha256_file(
            controller.current_state_path
        )
        pretty_payload = (
            json.dumps(previous, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        controller.current_state_path.write_bytes(pretty_payload)
        os.chmod(controller.current_state_path, 0o600)
        with controller.current_state_path.open("rb") as stream:
            os.fsync(stream.fileno())
        CONTROLLER.fsync_directory(
            controller.current_state_path.parent
        )
        pretty_digest = CONTROLLER.sha256_file(
            controller.current_state_path
        )
        self.assertNotEqual(pretty_digest, canonical_digest)

        successor = "deploy-pretty-previous-rollback"
        self._prepare_legacy_v2(controller, successor)
        _root, descriptor_path, _ready = controller._operation_paths(
            successor
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        self.assertEqual(
            descriptor["previous_deployment_sha256"],
            pretty_digest,
        )
        controller.apply(
            target_sha=TARGET_SHA,
            operation_id=successor,
        )

        rolled_back = controller.rollback(operation_id=successor)

        self.assertEqual(rolled_back["operation_id"], OPERATION_ID)
        self.assertEqual(
            rolled_back["rollback_provenance"][
                "sealed_previous_state_sha256"
            ],
            pretty_digest,
        )

    def test_two_pre_stop_rollback_aborts_use_distinct_nonterminal_audits(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        self._prepare_legacy_v2(controller, OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        second_operation = "deploy-20260716-abort-retry"
        self._prepare_legacy_v2(controller, second_operation)
        controller.apply(
            target_sha=TARGET_SHA,
            operation_id=second_operation,
        )
        lifecycle.fail_at = "drain"
        # This test isolates nonterminal audit append semantics.  The fixture
        # deliberately reuses one synthetic unit path across both operations,
        # which makes effect-byte reconciliation ambiguous.
        controller._reconcile_effect_commit_windows = (  # type: ignore[method-assign]
            lambda _descriptor, _marker: None
        )

        for _attempt in range(2):
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "injected drain failure",
            ):
                controller.rollback(operation_id=second_operation)
            recovered = controller.recover_interrupted()
            self.assertEqual(recovered["operation_id"], second_operation)
            self.assertFalse(controller.marker_path.exists())

        audits = sorted(
            (controller.audit_dir / second_operation).glob(
                "recovered-explicit-rollback-aborted-*.json"
            )
        )
        self.assertEqual(len(audits), 2)
        self.assertNotEqual(audits[0].name, audits[1].name)
        operation_state = controller._load_operation_state(second_operation)
        self.assertEqual(operation_state["outcome"], "deployed")

    def test_explicit_rollback_rejects_source_audit_tamper_before_drain(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        self._prepare_legacy_v2(controller, OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        second_operation = "deploy-20260716-source-audit-tamper"
        self._prepare_legacy_v2(controller, second_operation)
        controller.apply(
            target_sha=TARGET_SHA,
            operation_id=second_operation,
        )
        source_audit = (
            controller.audit_dir / second_operation / "success.json"
        )
        os.chmod(source_audit, 0o644)
        lifecycle.events.clear()

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "mode 0600",
        ):
            controller.rollback(operation_id=second_operation)

        self.assertEqual(lifecycle.events, [])
        self.assertFalse(controller.marker_path.exists())
        self.assertEqual(
            controller._load_operation_state(second_operation)["outcome"],
            "deployed",
        )

    def test_stable_cli_uses_fixed_roots_and_has_no_extra_mutation_flags(self) -> None:
        parsed = CONTROLLER.parser().parse_args(
            [
                "prepare",
                "--sha",
                TARGET_SHA,
                "--operation-id",
                OPERATION_ID,
            ]
        )
        self.assertFalse(hasattr(parsed, "production_root"))
        self.assertFalse(hasattr(parsed, "runtime_root"))
        self.assertFalse(hasattr(parsed, "apply"))
        accepted = CONTROLLER.parser().parse_args(
            [
                "accept",
                "--sha",
                TARGET_SHA,
                "--operation-id",
                OPERATION_ID,
            ]
        )
        self.assertEqual(accepted.command, "accept")
        with self.assertRaises(SystemExit):
            CONTROLLER.parser().parse_args(
                [
                    "prepare",
                    "--sha",
                    TARGET_SHA,
                    "--operation-id",
                    OPERATION_ID,
                    "--apply",
                ]
            )

    def test_restored_bridge_cli_dispatches_every_content_address(self) -> None:
        descriptor_digest = "sha256:" + "a" * 64
        restored_terminal = "sha256:" + "b" * 64
        capsule_digest = "sha256:" + "c" * 64
        authority_sha = "5" * 40
        fake = mock.Mock()
        fake.recover_restored_first_bridge.return_value = {
            "action": "bridge-recover-restored",
            "apply": True,
        }
        arguments = [
            "bridge-recover-restored",
            "--authority-sha",
            authority_sha,
            "--target-sha",
            TARGET_SHA,
            "--operation-id",
            OPERATION_ID,
            "--capsule-sha256",
            capsule_digest,
            "--descriptor-sha256",
            descriptor_digest,
            "--restored-terminal-sha256",
            restored_terminal,
        ]
        with (
            mock.patch.object(
                CONTROLLER,
                "PullDeployController",
                return_value=fake,
            ) as constructor,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(CONTROLLER.main(arguments), 0)
        constructor.assert_called_once_with(
            CONTROLLER.PRODUCTION_ROOT,
            CONTROLLER.RUNTIME_ROOT,
            apply=True,
        )
        fake.recover_restored_first_bridge.assert_called_once_with(
            authority_sha=authority_sha,
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
            capsule_sha256=capsule_digest,
            descriptor_sha256=descriptor_digest,
            restored_terminal_sha256=restored_terminal,
        )


class UnitTransitionRunner:
    def __init__(self, target: Path, *, enabled: bool) -> None:
        self.target = target
        self.enabled = enabled
        self.commands: list[list[str]] = []

    def run(
        self, command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[:3] == ["systemctl", "--user", "enable"]:
            self.enabled = True
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["systemctl", "--user", "disable"]:
            self.enabled = False
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["systemctl", "--user", "show"]:
            present = self.target.exists()
            values = {
                "LoadState": "loaded" if present else "not-found",
                "FragmentPath": str(self.target) if present else "",
                "DropInPaths": "",
                "NeedDaemonReload": "no",
                "UnitFileState": "enabled" if present and self.enabled else "",
            }
            output = "".join(f"{key}={value}\n" for key, value in values.items())
            return subprocess.CompletedProcess(command, 0, output, "")
        return subprocess.CompletedProcess(command, 0, "", "")


class WorkerUnitTransitionTests(PullDeployTestCase):
    def _unit_descriptor(
        self, *, previous: bytes | None
    ) -> tuple[object, dict[str, object], Path]:
        target_parent = self.root / "systemd"
        target_parent.mkdir(mode=0o700)
        target = target_parent / CONTROLLER.MONOMER_MD_UNIT_NAME
        candidate = self.runtime / "state/prepared/candidate.service"
        candidate.write_bytes(b"candidate unit\n")
        os.chmod(candidate, 0o600)
        backup: Path | None = None
        if previous is not None:
            backup = self.runtime / "state/prepared/previous.service"
            backup.write_bytes(previous)
            os.chmod(backup, 0o600)
            target.write_bytes(b"candidate unit\n")
            os.chmod(target, 0o600)
        runner = UnitTransitionRunner(target, enabled=previous is not None)
        controller = CONTROLLER.PullDeployController(
            self.production, self.runtime, runner=runner, apply=True
        )
        controller.control_environment = lambda: {}  # type: ignore[method-assign]
        controller._revalidate_worker_controls = (  # type: ignore[method-assign]
            lambda _descriptor, **_kwargs: None
        )
        unit = {
            "candidate_path": str(candidate),
            "target_path": str(target),
            "sha256": CONTROLLER.sha256_file(candidate),
            "previous_present": previous is not None,
            "previous_sha256": (
                CONTROLLER.sha256_bytes(previous) if previous is not None else None
            ),
            "previous_backup_path": str(backup) if backup is not None else None,
        }
        return controller, {"monomer_md": {"systemd_unit": unit}}, target

    def test_absent_unit_is_enabled_then_disabled_and_removed_on_rollback(self) -> None:
        controller, descriptor, target = self._unit_descriptor(previous=None)
        controller._install_candidate_worker_unit(descriptor)
        self.assertTrue(target.is_file())
        self.assertIn(
            ["systemctl", "--user", "enable", CONTROLLER.MONOMER_MD_UNIT_NAME],
            controller.runner.commands,
        )
        controller._restore_previous_worker_unit(descriptor)
        self.assertFalse(target.exists())
        self.assertIn(
            ["systemctl", "--user", "disable", CONTROLLER.MONOMER_MD_UNIT_NAME],
            controller.runner.commands,
        )

    def test_existing_enabled_unit_restores_exact_previous_bytes(self) -> None:
        controller, descriptor, target = self._unit_descriptor(previous=b"old unit\n")
        controller._restore_previous_worker_unit(descriptor)
        self.assertEqual(target.read_bytes(), b"old unit\n")
        self.assertNotIn(
            ["systemctl", "--user", "disable", CONTROLLER.MONOMER_MD_UNIT_NAME],
            controller.runner.commands,
        )


class DescriptorV4TransactionTests(PullDeployTestCase):
    def test_descriptor_seals_guard_installation_runtime_and_timer_policy(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        guard = descriptor["monomer_dft"]["guard"]

        self.assertEqual(
            guard["service"]["name"],
            CONTROLLER.MONOMER_DFT_GUARD_SERVICE_NAME,
        )
        self.assertEqual(
            guard["timer"]["name"],
            CONTROLLER.MONOMER_DFT_GUARD_TIMER_NAME,
        )
        self.assertEqual(guard["timer_policy"], {"enabled": True, "active": True})
        for unit in (guard["service"], guard["timer"]):
            self.assertIn("main_pid", unit)
            self.assertIn("invocation_id", unit)
            self.assertTrue(unit["sha256"].startswith("sha256:"))
        self.assertEqual(
            guard["git_units"]["service"]["sha256"],
            guard["service"]["sha256"],
        )
        self.assertEqual(
            guard["git_units"]["timer"]["sha256"],
            guard["timer"]["sha256"],
        )

        changed = json.loads(json.dumps(descriptor))
        changed["monomer_dft"]["guard"]["timer"]["sha256"] = DIGEST_A
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "guard control identity|guard unit",
        ):
            CONTROLLER.validate_descriptor(changed)

    def test_prepare_switches_md_queue_capacity_from_one_to_three_and_rolls_back(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        transition = descriptor["monomer_md"]["worker_env"]
        target = Path(transition["target"]["path"])

        self.assertIn(b"MONOMER_MD_MAX_ACTIVE_JOBS=1\n", target.read_bytes())
        self.assertIn(
            b"MONOMER_MD_MAX_ACTIVE_JOBS=3\n",
            Path(transition["candidate_path"]).read_bytes(),
        )
        controller._switch_worker_environment(descriptor)
        self.assertEqual(
            CONTROLLER.sha256_file(target), transition["target"]["sha256"]
        )
        controller._restore_previous_worker_environment(descriptor)
        self.assertEqual(
            CONTROLLER.sha256_file(target), transition["previous"]["sha256"]
        )
        self.assertIn(b"MONOMER_MD_MAX_ACTIVE_JOBS=1\n", target.read_bytes())

    def test_dft_environment_and_unit_switch_restore_exact_previous_bytes(
        self,
    ) -> None:
        unit_root = self.root / "dft-systemd"
        unit_root.mkdir(mode=0o700)
        unit_target = unit_root / CONTROLLER.MONOMER_DFT_UNIT_NAME
        unit_target.write_bytes(b"previous DFT unit\n")
        os.chmod(unit_target, 0o600)
        unit_candidate = self.runtime / "state/prepared/dft-candidate.service"
        unit_candidate.write_bytes(b"candidate DFT unit\n")
        os.chmod(unit_candidate, 0o600)
        env_target = self.runtime / CONTROLLER.MONOMER_DFT_RUNTIME_ENV
        write_private(env_target, "MONOMER_DFT_RELEASE_SHA=" + PREVIOUS_SHA + "\n")
        env_candidate = self.runtime / "state/prepared/dft-candidate.env"
        write_private(env_candidate, "MONOMER_DFT_RELEASE_SHA=" + TARGET_SHA + "\n")
        env_backup = self.runtime / "state/prepared/dft-previous.env"
        write_private(env_backup, env_target.read_text(encoding="utf-8"))
        unit_backup = self.runtime / "state/prepared/dft-previous.service"
        write_private(unit_backup, unit_target.read_text(encoding="utf-8"))
        runner = UnitTransitionRunner(unit_target, enabled=True)
        controller = CONTROLLER.PullDeployController(
            self.production, self.runtime, runner=runner, apply=True
        )
        controller.control_environment = lambda: {}  # type: ignore[method-assign]
        controller._revalidate_dft_controls = (  # type: ignore[method-assign]
            lambda _descriptor: None
        )
        descriptor = {
            "schema_version": CONTROLLER.DESCRIPTOR_SCHEMA_VERSION,
            "monomer_dft": {
                "runtime_env": {
                    "target": {
                        "path": str(env_target),
                        "sha256": CONTROLLER.sha256_file(env_candidate),
                    },
                    "candidate_path": str(env_candidate),
                    "previous_present": True,
                    "previous_sha256": CONTROLLER.sha256_file(env_target),
                    "previous_backup_path": str(env_backup),
                },
                "systemd_unit": {
                    "candidate_path": str(unit_candidate),
                    "target_path": str(unit_target),
                    "sha256": CONTROLLER.sha256_file(unit_candidate),
                    "previous_present": True,
                    "previous_sha256": CONTROLLER.sha256_file(unit_target),
                    "previous_backup_path": str(unit_backup),
                    "previous_systemd_state": {
                        "UnitFileState": "enabled",
                    },
                },
            },
        }

        controller._switch_dft_runtime(descriptor)
        controller._install_candidate_dft_unit(descriptor)
        self.assertEqual(env_target.read_bytes(), env_candidate.read_bytes())
        self.assertEqual(unit_target.read_bytes(), unit_candidate.read_bytes())
        controller._restore_previous_dft_runtime(descriptor)
        controller._restore_previous_dft_unit(descriptor)
        self.assertEqual(env_target.read_bytes(), env_backup.read_bytes())
        self.assertEqual(unit_target.read_bytes(), unit_backup.read_bytes())

    def test_descriptor_v4_and_current_v3_require_dual_worker_authority(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        self.assertEqual(
            descriptor["schema_version"], CONTROLLER.DESCRIPTOR_SCHEMA_VERSION
        )
        self.assertEqual(
            set(descriptor["monomer_dft"]),
            {"runtime", "runtime_env", "systemd_unit", "gpu", "guard"},
        )
        self.assertIn("adopted_deployment", descriptor)
        for field in (
            "monomer_dft",
            "adopted_deployment",
            "adopted_deployment_sha256",
        ):
            changed = json.loads(json.dumps(descriptor))
            changed.pop(field)
            with self.subTest(descriptor_field=field), self.assertRaises(
                CONTROLLER.PullDeployError
            ):
                CONTROLLER.validate_descriptor(changed)

        state = controller.apply(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self.assertEqual(
            state["schema_version"], CONTROLLER.CURRENT_STATE_SCHEMA_VERSION
        )
        self.assertEqual(state["authority_kind"], "governed-deployment")
        self.assertIsNone(state["adoption_evidence"])
        self.assertIsNone(state["adoption_evidence_sha256"])
        self.assertIsNone(state["adopted_deployment_sha256"])
        self.assertEqual(
            state["monomer_dft"]["runtime"]["release_sha"], TARGET_SHA
        )
        for field in (
            "authority_kind",
            "adoption_evidence",
            "adoption_evidence_sha256",
            "adopted_deployment_sha256",
            "monomer_dft",
            "postgres_rehearsal",
        ):
            changed = json.loads(json.dumps(state))
            changed.pop(field)
            with self.subTest(current_field=field), self.assertRaises(
                CONTROLLER.PullDeployError
            ):
                CONTROLLER.validate_current_deployment_state(changed)


class DftGuardTransactionTests(PullDeployTestCase):
    class RecordingLifecycle(FakeLifecycle):
        def __init__(self, *, lose_first_stop_response: bool = False) -> None:
            super().__init__()
            self.guard_events: list[str] = []
            self.lose_first_stop_response = lose_first_stop_response

        def stop_dft_guard(
            self,
            controller: object,
            descriptor: dict[str, object],
            *,
            allow_already_stopped: bool,
        ) -> dict[str, object]:
            self.guard_events.append(
                "guard-stop-retry" if allow_already_stopped else "guard-stop"
            )
            evidence = super().stop_dft_guard(
                controller,
                descriptor,
                allow_already_stopped=allow_already_stopped,
            )
            if self.lose_first_stop_response:
                self.lose_first_stop_response = False
                raise CONTROLLER.PullDeployError(
                    "injected lost guard-stop response"
                )
            return evidence

        def restore_dft_guard(
            self,
            controller: object,
            descriptor: dict[str, object],
        ) -> dict[str, object]:
            self.guard_events.append("guard-restore")
            return super().restore_dft_guard(controller, descriptor)

        def start(self, controller: object, descriptor: object) -> None:
            self.guard_events.append("dft-start")
            super().start(controller, descriptor)

    def _prepared(self, lifecycle: FakeLifecycle) -> tuple[object, dict[str, object]]:
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        return controller, CONTROLLER.load_private_json(descriptor_path)

    def test_lost_guard_stop_response_recovers_and_restores_before_dft_start(
        self,
    ) -> None:
        lifecycle = self.RecordingLifecycle(lose_first_stop_response=True)
        controller, descriptor = self._prepared(lifecycle)
        marker: dict[str, object] = {"dft_guard_scheduling_stopped": False}

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lost guard-stop response",
        ):
            controller._stop_dft_guard_scheduling(marker, descriptor)
        persisted = CONTROLLER.load_private_json(controller.marker_path)
        self.assertIn("dft_guard_stop_intent", persisted)
        self.assertFalse(persisted["dft_guard_scheduling_stopped"])

        controller._stop_dft_guard_scheduling(marker, descriptor)
        self.assertTrue(marker["dft_guard_scheduling_stopped"])
        self.assertEqual(
            lifecycle.guard_events,
            ["guard-stop", "guard-stop-retry"],
        )
        controller._record_runtime_start_intent(marker, descriptor)
        controller._start_runtime(marker, descriptor)
        self.assertFalse(marker["dft_guard_scheduling_stopped"])
        self.assertEqual(
            lifecycle.guard_events[-2:],
            ["guard-restore", "dft-start"],
        )

    def test_second_stop_cycle_clears_old_restore_commit_before_rollback_stop(
        self,
    ) -> None:
        lifecycle = self.RecordingLifecycle()
        controller, descriptor = self._prepared(lifecycle)
        marker: dict[str, object] = {"dft_guard_scheduling_stopped": False}
        controller._stop_dft_guard_scheduling(marker, descriptor)
        controller._record_runtime_start_intent(marker, descriptor)
        controller._start_runtime(marker, descriptor)
        controller._stop_dft_guard_scheduling(marker, descriptor)

        self.assertTrue(marker["dft_guard_scheduling_stopped"])
        self.assertNotIn("dft_guard_restore_evidence", marker)
        self.assertIn("dft_guard_stop_evidence", marker)
        self.assertEqual(lifecycle.guard_events[-1], "guard-stop")

    def test_start_crash_after_guard_restore_reestablishes_stop_before_retry(
        self,
    ) -> None:
        lifecycle = self.RecordingLifecycle()
        controller, descriptor = self._prepared(lifecycle)
        marker: dict[str, object] = {"dft_guard_scheduling_stopped": False}
        controller._stop_dft_guard_scheduling(marker, descriptor)
        controller._record_runtime_start_intent(marker, descriptor)
        controller._restore_dft_guard_scheduling(marker, descriptor)
        self.assertFalse(marker["dft_guard_scheduling_stopped"])

        # This is the crash window: guard restore committed, DFT start did
        # not.  A rollback/forward retry records a new start intent only after
        # returning the guard to its stopped fence.
        controller._record_runtime_start_intent(marker, descriptor)

        self.assertTrue(marker["dft_guard_scheduling_stopped"])
        self.assertEqual(lifecycle.guard_events[-1], "guard-stop")

    def test_source_switch_refences_guard_and_checkout_after_long_backup(
        self,
    ) -> None:
        lifecycle = self.RecordingLifecycle()
        controller, descriptor = self._prepared(lifecycle)
        marker: dict[str, object] = {"dft_guard_scheduling_stopped": False}
        controller._stop_dft_guard_scheduling(marker, descriptor)
        first_evidence = marker["dft_guard_stop_evidence"]

        with mock.patch.object(
            CONTROLLER.SystemLifecycle,
            "_assert_no_checkout_readers",
        ) as reader_fence:
            CONTROLLER.PullDeployController._refresh_dft_guard_source_switch_fence(
                controller,
                marker,
                descriptor,
            )

        self.assertEqual(lifecycle.guard_events[-1], "guard-stop-retry")
        reader_fence.assert_called_once_with(controller.production_root)
        self.assertTrue(marker["dft_guard_scheduling_stopped"])
        self.assertEqual(
            marker["dft_guard_source_switch_fence"][
                "guard_stop_evidence_sha256"
            ],
            CONTROLLER.canonical_json_digest(
                marker["dft_guard_stop_evidence"]
            ),
        )
        self.assertIsNot(first_evidence, marker["dft_guard_stop_evidence"])

    def test_guard_stop_retry_invalidates_stale_source_switch_fence(
        self,
    ) -> None:
        lifecycle = self.RecordingLifecycle()
        controller, descriptor = self._prepared(lifecycle)
        marker: dict[str, object] = {"dft_guard_scheduling_stopped": False}
        tick = 0

        def advancing_utc() -> str:
            nonlocal tick
            tick += 1
            minute, second = divmod(tick, 60)
            return f"2026-07-16T00:{minute:02d}:{second:02d}Z"

        with (
            mock.patch.object(CONTROLLER, "utc_now", side_effect=advancing_utc),
            mock.patch.object(
                CONTROLLER.SystemLifecycle,
                "_assert_no_checkout_readers",
            ),
        ):
            controller._refresh_dft_guard_source_switch_fence(
                marker,
                descriptor,
            )
            first_digest = CONTROLLER.canonical_json_digest(
                marker["dft_guard_stop_evidence"]
            )
            self.assertIn("dft_guard_source_switch_fence", marker)

            # Recovery later stops the guard again before reconstructing the
            # previous runtime.  Its fresh evidence must invalidate the old
            # source-adjacent fence even within the same logical stop cycle.
            controller._stop_dft_guard_scheduling(marker, descriptor)

        self.assertNotEqual(
            CONTROLLER.canonical_json_digest(
                marker["dft_guard_stop_evidence"]
            ),
            first_digest,
        )
        self.assertNotIn("dft_guard_source_switch_fence", marker)

    def test_lost_guard_restore_response_is_externally_restopped_before_source_switch(
        self,
    ) -> None:
        lifecycle = self.RecordingLifecycle()
        controller, descriptor = self._prepared(lifecycle)
        marker: dict[str, object] = {"dft_guard_scheduling_stopped": False}
        controller._stop_dft_guard_scheduling(marker, descriptor)

        # Simulate systemctl/timer restoration committing while the marker
        # update is lost: disk still says stopped=True, external state is live.
        lifecycle.restore_dft_guard(controller, descriptor)
        self.assertTrue(marker["dft_guard_scheduling_stopped"])
        with mock.patch.object(
            CONTROLLER.SystemLifecycle,
            "_assert_no_checkout_readers",
        ):
            CONTROLLER.PullDeployController._refresh_dft_guard_source_switch_fence(
                controller,
                marker,
                descriptor,
            )

        self.assertEqual(lifecycle.guard_events[-1], "guard-stop-retry")
        self.assertTrue(marker["dft_guard_scheduling_stopped"])
        self.assertIn("dft_guard_source_switch_fence", marker)

    def test_partial_start_with_durable_intent_converges_to_stop_then_restarts(
        self,
    ) -> None:
        lifecycle = self.RecordingLifecycle()
        controller, descriptor = self._prepared(lifecycle)
        lifecycle.runtime_state = "partial"
        marker: dict[str, object] = {
            "action": "deploy",
            "phase": "runtime-start-started",
            "runtime_stopped": True,
            "dft_guard_scheduling_stopped": False,
            "runtime_start_intent": {
                "target_sha": TARGET_SHA,
                "recorded_at": CONTROLLER.utc_now(),
            },
        }

        controller._recover_runtime_and_resume(
            marker,
            descriptor,
            allow_unfenced=True,
        )

        self.assertIn("recovery-partial-stop", lifecycle.events)
        self.assertIn("start", lifecycle.events)
        self.assertEqual(lifecycle.runtime_state, "live")
        self.assertTrue(lifecycle.admission_open)
        self.assertEqual(lifecycle.guard_events[-2:], ["guard-restore", "dft-start"])
        self.assertIn("postgres_runtime_fence", marker)

    def test_partial_stop_with_durable_stop_phase_converges_without_restart(
        self,
    ) -> None:
        lifecycle = self.RecordingLifecycle()
        controller, descriptor = self._prepared(lifecycle)
        lifecycle.runtime_state = "partial"
        marker: dict[str, object] = {
            "action": "deploy",
            "phase": "runtime-stop-started",
            "runtime_stopped": False,
            "dft_guard_scheduling_stopped": False,
        }

        recovery = controller._prepare_runtime_recovery(
            marker,
            descriptor,
            allow_unfenced=True,
        )

        self.assertEqual(recovery["runtime_state"], "stopped")
        self.assertTrue(recovery["partial_runtime_converged"])
        self.assertEqual(lifecycle.runtime_state, "stopped")
        self.assertNotIn("start", lifecycle.events)
        self.assertTrue(marker["dft_guard_scheduling_stopped"])
        self.assertIn("postgres_runtime_fence", marker)

    def test_stopped_runtime_after_lost_stop_response_reissues_full_stop_proof(
        self,
    ) -> None:
        lifecycle = self.RecordingLifecycle()
        controller, descriptor = self._prepared(lifecycle)
        # The prior stop committed externally, but its response and the
        # runtime_stopped marker update were lost.
        lifecycle.runtime_state = "stopped"
        marker: dict[str, object] = {
            "action": "deploy",
            "phase": "runtime-stop-started",
            "runtime_stopped": False,
            "dft_guard_scheduling_stopped": False,
        }

        recovery = controller._prepare_runtime_recovery(
            marker,
            descriptor,
            allow_unfenced=True,
        )

        self.assertEqual(recovery["runtime_state"], "stopped")
        self.assertTrue(recovery["partial_runtime_converged"])
        self.assertIn("recovery-stopped-stop", lifecycle.events)
        self.assertIn("stop", lifecycle.events)
        self.assertIn("postgres_runtime_fence", marker)

    def test_system_stopped_recovery_never_trusts_presence_probe_alone(
        self,
    ) -> None:
        lifecycle = CONTROLLER.SystemLifecycle()
        controller = SimpleNamespace()
        descriptor: dict[str, object] = {}
        postgres = {"fixture": "postgres-fence"}
        with (
            mock.patch.object(lifecycle, "_isolate_ingress"),
            mock.patch.object(
                lifecycle,
                "_recovery_runtime_presence",
                return_value="stopped",
            ),
            mock.patch.object(
                lifecycle,
                "stop",
                return_value=postgres,
            ) as stop,
            mock.patch.object(
                lifecycle,
                "_prove_runtime_stopped",
                return_value=postgres,
            ) as prove,
        ):
            authorized = lifecycle.prepare_recovery_runtime(
                controller,
                descriptor,
                None,
                allow_unfenced=True,
                allow_partial_stop=True,
            )
            self.assertIs(authorized["postgres_runtime_fence"], postgres)
            stop.assert_called_once_with(controller, descriptor)
            prove.assert_not_called()

            stop.reset_mock()
            read_only = lifecycle.prepare_recovery_runtime(
                controller,
                descriptor,
                None,
                allow_unfenced=True,
            )
            self.assertIs(read_only["postgres_runtime_fence"], postgres)
            prove.assert_called_once_with(controller, descriptor)
            stop.assert_not_called()

    def test_transitional_worker_states_with_durable_stop_intent_converge(
        self,
    ) -> None:
        for runtime_state in ("failed", "activating", "deactivating"):
            with self.subTest(runtime_state=runtime_state):
                lifecycle = self.RecordingLifecycle()
                controller, descriptor = self._prepared(lifecycle)
                lifecycle.runtime_state = runtime_state
                marker: dict[str, object] = {
                    "action": "deploy",
                    "phase": "runtime-stop-started",
                    "runtime_stopped": False,
                    "dft_guard_scheduling_stopped": False,
                }

                recovery = controller._prepare_runtime_recovery(
                    marker,
                    descriptor,
                    allow_unfenced=True,
                )

                self.assertEqual(recovery["runtime_state"], "stopped")
                self.assertIn(
                    f"recovery-{runtime_state}-stop", lifecycle.events
                )
                self.assertEqual(lifecycle.runtime_state, "stopped")
                self.assertIn("postgres_runtime_fence", marker)
                controller.marker_path.unlink()

    def test_systemd_transitional_states_are_determinate_partial_runtime(
        self,
    ) -> None:
        class Lifecycle(CONTROLLER.SystemLifecycle):
            @staticmethod
            def _environment(_controller, _descriptor):  # type: ignore[no-untyped-def]
                return {}

            @staticmethod
            def _compose(_controller, *arguments):  # type: ignore[no-untyped-def]
                return ["fixture-compose", *arguments]

        class Runner:
            def __init__(self, state: str, returncode: int) -> None:
                self.state = state
                self.returncode = returncode

            def run(self, command, **_kwargs):  # type: ignore[no-untyped-def]
                if list(command) == [
                    "fixture-compose",
                    "ps",
                    "--quiet",
                    "backend",
                ]:
                    return subprocess.CompletedProcess(command, 0, "", "")
                if list(command) == [
                    "systemctl",
                    "--user",
                    "is-active",
                    CONTROLLER.MONOMER_MD_UNIT_NAME,
                ]:
                    return subprocess.CompletedProcess(
                        command,
                        self.returncode,
                        self.state + "\n",
                        "",
                    )
                raise AssertionError(command)

        lifecycle = Lifecycle()
        for state, returncode in (
            ("failed", 3),
            ("activating", 0),
            ("deactivating", 0),
        ):
            with self.subTest(state=state):
                controller = SimpleNamespace(
                    production_root=self.production,
                    runner=Runner(state, returncode),
                )
                self.assertEqual(
                    lifecycle._recovery_runtime_presence(controller, {}),
                    "partial",
                )

    def test_transitional_worker_states_without_durable_intent_fail_closed(
        self,
    ) -> None:
        for runtime_state in ("failed", "activating", "deactivating"):
            with self.subTest(runtime_state=runtime_state):
                lifecycle = self.RecordingLifecycle()
                controller, descriptor = self._prepared(lifecycle)
                lifecycle.runtime_state = runtime_state
                marker: dict[str, object] = {
                    "action": "deploy",
                    "phase": "drained",
                    "runtime_stopped": False,
                    "dft_guard_scheduling_stopped": False,
                }

                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "partially stopped",
                ):
                    controller._prepare_runtime_recovery(
                        marker,
                        descriptor,
                        allow_unfenced=True,
                    )

                self.assertEqual(lifecycle.runtime_state, runtime_state)
                self.assertNotIn("stop", lifecycle.events)

    def test_partial_runtime_without_durable_start_or_stop_intent_fails_closed(
        self,
    ) -> None:
        lifecycle = self.RecordingLifecycle()
        controller, descriptor = self._prepared(lifecycle)
        lifecycle.runtime_state = "partial"
        marker: dict[str, object] = {
            "action": "deploy",
            "phase": "drained",
            "runtime_stopped": False,
            "dft_guard_scheduling_stopped": False,
        }

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "partially stopped",
        ):
            controller._prepare_runtime_recovery(
                marker,
                descriptor,
                allow_unfenced=True,
            )

        self.assertEqual(lifecycle.runtime_state, "partial")
        self.assertEqual(lifecycle.guard_events, [])

    def test_system_guard_stop_and_restore_order_accepts_quarantine(self) -> None:
        lifecycle = CONTROLLER.SystemLifecycle()
        fixture_lifecycle = FakeLifecycle()
        controller, descriptor = self._prepared(fixture_lifecycle)
        guard = descriptor["monomer_dft"]["guard"]
        state_path = self.runtime / "state/guard-transaction.json"
        descriptor["monomer_dft"]["gpu"]["guard_state_path"] = str(state_path)

        class Runner:
            def __init__(self) -> None:
                self.commands: list[list[str]] = []
                self.timer_enabled = True
                self.timer_active = True

            def run(self, command, **_kwargs):  # type: ignore[no-untyped-def]
                self.commands.append(list(command))
                if command[:3] == ["systemctl", "--user", "is-active"]:
                    return subprocess.CompletedProcess(
                        command, 3, "inactive\n", ""
                    )
                if command[:3] == ["systemctl", "--user", "stop"]:
                    if command[-1] == CONTROLLER.MONOMER_DFT_GUARD_TIMER_NAME:
                        self.timer_active = False
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[:3] == ["systemctl", "--user", "enable"]:
                    self.timer_enabled = True
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[:3] == ["systemctl", "--user", "disable"]:
                    self.timer_enabled = False
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[:3] == ["systemctl", "--user", "start"]:
                    if command[-1] == CONTROLLER.MONOMER_DFT_GUARD_TIMER_NAME:
                        self.timer_active = True
                    else:
                        write_private(
                            state_path,
                            json.dumps(
                                {
                                    "schema_version": 1,
                                    "observed_at": CONTROLLER.utc_now(),
                                    "gpu_index": CONTROLLER.MONOMER_DFT_GPU_INDEX,
                                    "gpu_uuid": CONTROLLER.MONOMER_DFT_GPU_UUID,
                                    "status": "quarantined",
                                    "allowed_processes": [],
                                    "unknown_processes": [{"redacted": True}],
                                    "authorities": {},
                                },
                                sort_keys=True,
                            )
                            + "\n",
                        )
                    return subprocess.CompletedProcess(command, 0, "", "")
                raise AssertionError(f"unexpected command: {command}")

        runner = Runner()
        controller.runner = runner
        controller.control_environment = lambda: {}  # type: ignore[method-assign]

        def snapshot(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            result = json.loads(json.dumps(guard))
            result["timer"]["systemd_state"]["UnitFileState"] = (
                "enabled" if runner.timer_enabled else "disabled"
            )
            result["timer"]["systemd_state"]["ActiveState"] = (
                "active" if runner.timer_active else "inactive"
            )
            result["timer"]["systemd_state"]["SubState"] = (
                "waiting" if runner.timer_active else "dead"
            )
            result["service"]["systemd_state"]["ActiveState"] = "inactive"
            result["service"]["systemd_state"]["SubState"] = "dead"
            result["timer_policy"] = {
                "enabled": runner.timer_enabled,
                "active": runner.timer_active,
            }
            return result

        controller._revalidate_dft_guard_controls = snapshot  # type: ignore[method-assign]
        stopped = lifecycle.stop_dft_guard(
            controller,
            descriptor,
            allow_already_stopped=False,
        )
        self.assertEqual(stopped["status"], "stopped")
        timer_stop = runner.commands.index(
            [
                "systemctl",
                "--user",
                "stop",
                CONTROLLER.MONOMER_DFT_GUARD_TIMER_NAME,
            ]
        )
        service_stop = runner.commands.index(
            [
                "systemctl",
                "--user",
                "stop",
                CONTROLLER.MONOMER_DFT_GUARD_SERVICE_NAME,
            ]
        )
        self.assertLess(timer_stop, service_stop)

        restored = lifecycle.restore_dft_guard(controller, descriptor)
        self.assertEqual(restored["observation"]["status"], "quarantined")
        service_start = max(
            index
            for index, command in enumerate(runner.commands)
            if command
            == [
                "systemctl",
                "--user",
                "start",
                CONTROLLER.MONOMER_DFT_GUARD_SERVICE_NAME,
            ]
        )
        timer_start = max(
            index
            for index, command in enumerate(runner.commands)
            if command
            == [
                "systemctl",
                "--user",
                "start",
                CONTROLLER.MONOMER_DFT_GUARD_TIMER_NAME,
            ]
        )
        self.assertLess(service_start, timer_start)
        self.assertTrue(runner.timer_active)


class DftBuildCrashAndAbortTests(PullDeployTestCase):
    class BuildRunner:
        def __init__(self, python: Path) -> None:
            self.python = python
            self.fail_download = True
            self.commands: list[list[str]] = []

        def run(
            self, command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            self.commands.append(command)
            if command[-1:] == ["--version"]:
                return subprocess.CompletedProcess(
                    command, 0, "uv 0.11.21 fixture\n", ""
                )
            if command[0] == str(self.python) and "import sys" in command[-1]:
                return subprocess.CompletedProcess(command, 0, "3.12\n", "")
            if "download" in command:
                if self.fail_download:
                    self.fail_download = False
                    raise CONTROLLER.PullDeployError(
                        "injected DFT wheel download crash"
                    )
                return subprocess.CompletedProcess(command, 0, "", "")
            if "venv" in command:
                venv = Path(command[-1])
                (venv / "bin").mkdir(parents=True, mode=0o700)
                (venv / "bin/python").symlink_to(self.python)
                (venv / "bin/python3").symlink_to("python")
                (venv / "bin/python3.12").symlink_to("python")
            return subprocess.CompletedProcess(command, 0, "", "")

        def request_json(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("unexpected network request")

    def test_dft_prepare_rebuilds_owned_wheelhouse_after_download_crash(
        self,
    ) -> None:
        tool_root = self.root / "dft-tools"
        artifact_root = self.root / "dft-artifacts"
        tool_root.mkdir(mode=0o700)
        artifact_root.mkdir(mode=0o700)
        uv = Path("/usr/bin/true")
        python = tool_root / "python3.12"
        python.write_bytes(b"fixture python\n")
        os.chmod(python, 0o755)
        wheel_name = "aimnet_fixture-1.0-py3-none-any.whl"
        artifact_payloads = {wheel_name: b"fixture wheel\n"}
        for index in range(6):
            artifact_payloads[f"model-{index}.pt"] = f"model-{index}\n".encode()
        for name, payload in artifact_payloads.items():
            path = artifact_root / name
            path.write_bytes(payload)
            os.chmod(path, 0o600)
        source_lock = {
            "schema_version": 1,
            "source": {"python_minor": "3.12", "uv_version": "0.11.21"},
            "wheel": {
                "filename": wheel_name,
                "sha256": CONTROLLER.sha256_file(
                    artifact_root / wheel_name
                ).removeprefix("sha256:"),
                "file_count": 1,
                "inventory_sha256": "d" * 64,
                "record_path": "aimnet_fixture-1.0.dist-info/RECORD",
                "record_sha256": "e" * 64,
            },
            "registry": {
                "path": "aimnet/calculators/model_registry.yaml",
                "sha256": "f" * 64,
            },
            "models": [
                {
                    "file": f"model-{index}.pt",
                    "alias": alias,
                    "sha256": CONTROLLER.sha256_file(
                        artifact_root / f"model-{index}.pt"
                    ).removeprefix("sha256:"),
                    "registry_sha256": CONTROLLER.sha256_file(
                        artifact_root / f"model-{index}.pt"
                    ).removeprefix("sha256:"),
                    "cache_sha256": CONTROLLER.sha256_file(
                        artifact_root / f"model-{index}.pt"
                    ).removeprefix("sha256:"),
                }
                for index, alias in enumerate(
                    sorted(CONTROLLER.MONOMER_DFT_MODEL_ALIASES)
                )
            ],
        }
        source_lock_payload = (
            json.dumps(source_lock, sort_keys=True) + "\n"
        ).encode()
        requirements = b"fixture==1.0 --hash=sha256:" + b"f" * 64 + b"\n"
        runner = self.BuildRunner(python)
        controller = CONTROLLER.PullDeployController(
            self.production, self.runtime, runner=runner, apply=True
        )
        controller._git_show = (  # type: ignore[method-assign]
            lambda _sha, relative: (
                requirements
                if relative.endswith("requirements.lock")
                else source_lock_payload
            )
        )
        uv_digest = CONTROLLER.sha256_file(uv)
        inventory_digest = "sha256:" + "e" * 64
        with (
            mock.patch.object(CONTROLLER, "MONOMER_DFT_UV", uv),
            mock.patch.object(CONTROLLER, "MONOMER_DFT_UV_SHA256", uv_digest),
            mock.patch.object(CONTROLLER, "MONOMER_DFT_PYTHON", python),
            mock.patch.object(
                CONTROLLER,
                "MONOMER_DFT_PYTHON_SHA256",
                CONTROLLER.sha256_file(python),
            ),
            mock.patch.object(
                CONTROLLER, "MONOMER_DFT_RUNTIME_ARTIFACT_ROOT", artifact_root
            ),
            mock.patch.object(
                CONTROLLER.PullDeployController,
                "_validate_dft_pip_toolchain",
                return_value=None,
            ),
            mock.patch.object(
                CONTROLLER.PullDeployController,
                "_validate_dft_aimnet_wheel",
                return_value=None,
            ),
            mock.patch.object(
                CONTROLLER.PullDeployController,
                "_validate_installed_dft_aimnet",
                return_value=None,
            ),
            mock.patch.object(
                CONTROLLER.PullDeployController,
                "_dft_wheelhouse_inventory",
                return_value=inventory_digest,
            ),
            controller.deployment_lock(),
        ):
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError, "wheel download crash"
            ):
                controller.prepare_dft_runtime(
                    operation_id=OPERATION_ID,
                    target_sha=TARGET_SHA,
                    target_tree=TARGET_TREE,
                )
            staging = (
                controller.venv_root
                / "dft"
                / f".{TARGET_SHA}.preparing-{OPERATION_ID}"
            )
            build_cache = (
                controller.venv_root
                / "dft/.build-cache"
                / TARGET_SHA
                / OPERATION_ID
            )
            self.assertTrue((staging / ".preparing.json").is_file())
            self.assertTrue((build_cache / "owner.json").is_file())

            runtime = controller.prepare_dft_runtime(
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
                target_tree=TARGET_TREE,
            )

        self.assertEqual(runtime["release_sha"], TARGET_SHA)
        self.assertFalse(staging.exists())
        self.assertFalse(build_cache.exists())
        downloads = [command for command in runner.commands if "download" in command]
        installs = [command for command in runner.commands if "install" in command]
        self.assertEqual(len(downloads), 2)
        self.assertTrue(installs)
        for command in installs:
            self.assertIn("--offline", command)
            self.assertIn("--no-cache", command)

    def test_prepare_abort_archives_owned_dft_runtime_and_build_cache(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        owner = {
            "schema_version": 1,
            "operation_id": OPERATION_ID,
            "release_sha": TARGET_SHA,
            "source_tree": TARGET_TREE,
        }
        dft_root = controller.venv_root / "dft"
        staging = dft_root / f".{TARGET_SHA}.preparing-{OPERATION_ID}"
        staging.mkdir(parents=True, mode=0o700)
        CONTROLLER.atomic_json(staging / ".preparing.json", owner)
        write_private(staging / "partial-runtime", "partial\n")
        cache = dft_root / ".build-cache" / TARGET_SHA / OPERATION_ID
        cache.mkdir(parents=True, mode=0o700)
        CONTROLLER.atomic_json(
            cache / "owner.json",
            {
                **owner,
                "requirements_lock_sha256": "sha256:" + "1" * 64,
            },
        )
        write_private(cache / "partial-wheel", "partial\n")

        result = controller.abort_prepare(operation_id=OPERATION_ID)
        journal = CONTROLLER.load_private_json(
            controller.prepare_aborts_dir / f"{OPERATION_ID}.json"
        )
        archive = Path(result["archive_path"]) / "monomer-dft-runtime"
        self.assertFalse(staging.exists())
        self.assertFalse(cache.exists())
        self.assertTrue((archive / "staging/.preparing.json").is_file())
        self.assertTrue((archive / "build-cache/owner.json").is_file())
        self.assertIsNotNone(
            journal["dft_staging"]["staging_inventory_sha256"]
        )
        self.assertIsNotNone(journal["dft_staging"]["cache_inventory_sha256"])


class DualWorkerStopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="dual-worker-stop-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.production = self.root / "production"
        self.runtime = self.root / "runtime"
        self.config = self.runtime / "config"
        self.state = self.runtime / "state"
        for path in (self.production, self.runtime, self.config, self.state):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        self.descriptor = {
            "images": {
                "backend": {"digest_ref": "example.invalid/backend@" + DIGEST_A},
                "web": {"digest_ref": "example.invalid/web@" + DIGEST_B},
            }
        }

    def controller(self, runner: object) -> object:
        return SimpleNamespace(
            runner=runner,
            production_root=self.production,
            runtime_root=self.runtime,
            config_dir=self.config,
            state_dir=self.state,
            marker_path=self.state / "deploy-in-progress.json",
            control_environment=lambda: {},
            production_deploy_values=lambda **_kwargs: {
                "NEXPOLY_POSTGRES_USER": "nexpoly",
                "NEXPOLY_POSTGRES_DB": "nexpoly",
            },
        )

    def test_descriptor_v4_stops_and_proves_both_worker_processes_zero(
        self,
    ) -> None:
        runner = PostgresRuntimeFencingTests.Runner()
        controller = self.controller(runner)
        descriptor = {
            **self.descriptor,
            "schema_version": CONTROLLER.DESCRIPTOR_SCHEMA_VERSION,
        }
        PostgresRuntimeFencingTests.Lifecycle().stop(controller, descriptor)
        for unit in (
            CONTROLLER.MONOMER_MD_UNIT_NAME,
            CONTROLLER.MONOMER_DFT_UNIT_NAME,
        ):
            self.assertIn(
                ["systemctl", "--user", "stop", unit], runner.commands
            )
            self.assertIn(
                [
                    "systemctl",
                    "--user",
                    "show",
                    unit,
                    "--property=ActiveState",
                    "--property=MainPID",
                ],
                runner.commands,
            )

    def test_checkout_reader_probe_rejects_same_uid_process(self) -> None:
        checkout = self.root / "checkout"
        checkout.mkdir(mode=0o700)
        proc_root = self.root / "proc"
        process = proc_root / "4242"
        (process / "fd").mkdir(parents=True, mode=0o700)
        (process / "cwd").symlink_to(checkout)
        (process / "exe").symlink_to("/usr/bin/python3")
        (process / "stat").write_text(
            "4242 (fixture) S " + " ".join(str(index) for index in range(1, 24)),
            encoding="utf-8",
        )
        (process / "cmdline").write_bytes(b"fixture\0")
        (process / "maps").write_text("", encoding="utf-8")
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "live checkout retains 1"
        ):
            CONTROLLER.SystemLifecycle._assert_no_checkout_readers(
                checkout, proc_root=proc_root
            )


class ProductionSourceTrustProjectionTests(unittest.TestCase):
    def test_controller_reproof_matches_successor_publisher_projection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="production-source-trust-projection-"
        ) as temporary:
            source = Path(temporary) / "source"
            source.mkdir(mode=0o700)

            def git(*arguments: str) -> str:
                return subprocess.run(
                    ["/usr/bin/git", "-C", str(source), *arguments],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ).stdout.strip()

            previous_umask = os.umask(0o077)
            try:
                git("init", "-b", "main")
                git("config", "user.name", "Source Trust Test")
                git("config", "user.email", "source-trust@example.invalid")
                (source / "payload.txt").write_text(
                    "trusted production source\n",
                    encoding="utf-8",
                )
                git("add", "payload.txt")
                git("commit", "-m", "trusted production source")
            finally:
                os.umask(previous_umask)

            source_sha = git("rev-parse", "HEAD")
            source_tree = git("rev-parse", "HEAD^{tree}")
            controller = object.__new__(CONTROLLER.PullDeployController)
            controller.production_root = source
            with mock.patch.dict(
                os.environ,
                {
                    "HOME": "/ambient-home-must-not-enter-source-trust",
                    "GIT_SSH_COMMAND": "/ambient-ssh-must-not-enter-source-trust",
                },
            ):
                observed = controller._production_source_trust_digest(
                    source_sha=source_sha,
                    source_tree=source_tree,
                )
                publisher_projection = (
                    CONTROLLER._git_source_trust.repository_trust_evidence(
                        source,
                        source_sha=source_sha,
                        source_tree=source_tree,
                        branch="refs/heads/main",
                        origin=None,
                        ambient={},
                    )["evidence_sha256"]
                )
            self.assertEqual(observed, publisher_projection)


class AdoptedFirstDeploymentTests(PullDeployTestCase):
    def _grow_directory_size(self, path: Path, *, label: str) -> tuple[int, int]:
        before = path.stat().st_size
        created: list[Path] = []
        expanded = before
        for index in range(4096):
            entry = path / (
                f".nexpoly-{label}-growth-{index:05d}-" + "x" * 64
            )
            write_private(entry, "growth\n")
            created.append(entry)
            expanded = path.stat().st_size
            if expanded > before:
                break
        self.assertGreater(expanded, before)
        for entry in created:
            entry.unlink()
        after_cleanup = path.stat().st_size
        self.assertGreater(after_cleanup, before)
        return before, after_cleanup

    def _seed_adopted_authority(
        self, controller: FixtureController
    ) -> dict[str, object]:
        adoption_operation = "adopt-runtime-fixture-001"
        slot = controller.prepare_md_slot(
            operation_id=adoption_operation,
            target_sha=PREVIOUS_SHA,
            target_tree=PREVIOUS_TREE,
            lock_payload=b"adopted-lock\n",
        )
        active_slot = {
            "schema_version": CONTROLLER.ACTIVE_SLOT_SCHEMA_VERSION,
            "component": "monomer-md",
            "slot": slot["slot"],
            "source_sha": PREVIOUS_SHA,
            "source_tree": PREVIOUS_TREE,
            "worker_lock_sha256": slot["worker_lock_sha256"],
            "slot_record_sha256": CONTROLLER.worker_record_digest(slot),
            "operation_id": adoption_operation,
            "activated_at": CONTROLLER.utc_now(),
        }
        CONTROLLER.atomic_json(controller.active_slot_path, active_slot)
        worker_env = controller.config_dir / "worker.env"
        write_private(
            worker_env,
            "MONOMER_MD_WORKER_MODE=real\nMONOMER_MD_MAX_ACTIVE_JOBS=1\n",
        )
        dft_root = controller.runtime_root / "adopted-dft-runtime"
        dft_root.mkdir(mode=0o700)
        dft_manifest = dft_root / "runtime.json"
        write_private(dft_manifest, '{"adopted":true}\n')
        dft_env = controller.config_dir / "monomer-dft-runtime.env"
        dft_inventory = "sha256:" + "8" * 64
        dft_values = {
            "MONOMER_DFT_RELEASE_SHA": PREVIOUS_SHA,
            "MONOMER_DFT_RUNTIME_CONTRACT_SHA256": CONTROLLER.sha256_file(
                dft_manifest
            ),
            "MONOMER_DFT_RUNTIME_INVENTORY_SHA256": dft_inventory,
            "MONOMER_DFT_PYTHON": str(dft_root / "venv/bin/python"),
            "AIMNET_CACHE_DIR": str(dft_root / "aimnet-cache"),
            "WARP_CACHE_PATH": str(dft_root / "warp-cache"),
            "NEXPOLY_DFT_GPU_GUARD_MODE": "enforce",
        }
        write_private(
            dft_env,
            "".join(f"{key}={value}\n" for key, value in dft_values.items()),
        )
        md_unit = controller.config_dir / CONTROLLER.MONOMER_MD_UNIT_NAME
        dft_unit = controller.config_dir / CONTROLLER.MONOMER_DFT_UNIT_NAME
        write_private(md_unit, "adopted MD unit\n")
        write_private(dft_unit, "adopted DFT unit\n")
        active_control = controller.active_control_evidence()
        adoption_evidence = {"plan_sha256": "sha256:" + "7" * 64}
        migrations = json.loads(json.dumps(B_MANIFEST_RECORDS[:11]))
        adopted = {
            "schema_version": 1,
            "status": "adopted",
            "authority_kind": "manual-runtime-adoption",
            "operation_id": adoption_operation,
            "source_sha": PREVIOUS_SHA,
            "source_tree": PREVIOUS_TREE,
            "bootstrap_source_sha": active_control["source_sha"],
            "bootstrap_source_tree": active_control["source_tree"],
            "active_control": active_control,
            "adoption_evidence": adoption_evidence,
            "adoption_evidence_sha256": CONTROLLER.canonical_json_digest(
                adoption_evidence
            ),
            "images": {
                role: {
                    "digest_ref": image_record(role, PREVIOUS_SHA)["digest_ref"],
                    "image_id": "sha256:" + value * 64,
                    "container_id": value * 64,
                    "restart_count": 0,
                }
                for role, value in (("backend", "a"), ("web", "b"))
            },
            "production_config": {
                name: {
                    "path": str(controller.config_dir / f"{name}.env"),
                    "sha256": "sha256:" + value * 64,
                    "size": 10,
                    "mode": "0600",
                }
                for name, value in (("deploy_env", "c"), ("app_env", "d"))
            },
            "asset_identity": {
                "pointer": str(controller.state_dir / "current-assets"),
                "root": str(controller.runtime_root / "adopted-assets"),
                "manifest_sha256": CONTROLLER.SCHEMA_V2_ASSET_MANIFEST_DIGEST,
            },
            "migrations": migrations,
            "database": {
                "postgres_major": 16,
                "ledger": [
                    {"version": record["version"], "checksum": record["checksum"]}
                    for record in migrations
                ],
            },
            "maintenance": {"active": False, "queued": False},
            "monomer_md": {
                "active_slot_path": str(controller.active_slot_path),
                "active_slot_file_sha256": CONTROLLER.sha256_file(
                    controller.active_slot_path
                ),
                "active_slot": active_slot,
                "slot_record_path": str(
                    controller.slots_state_dir / f"md-{slot['slot']}.json"
                ),
                "slot_record_file_sha256": CONTROLLER.sha256_file(
                    controller.slots_state_dir / f"md-{slot['slot']}.json"
                ),
                "slot_record": slot,
                "worker_env": {
                    "path": str(worker_env),
                    "sha256": CONTROLLER.sha256_file(worker_env),
                    "size": worker_env.stat().st_size,
                    "mode": "0600",
                },
                "systemd_unit": {"target_path": str(md_unit), "sha256": CONTROLLER.sha256_file(md_unit)},
                "health": {"active": 0, "queued": 0},
            },
            "monomer_dft": {
                "runtime": {
                    "root": str(dft_root),
                    "runtime_manifest_path": str(dft_manifest),
                    "runtime_manifest_sha256": CONTROLLER.sha256_file(dft_manifest),
                    "release_sha": PREVIOUS_SHA,
                    "source_tree": PREVIOUS_TREE,
                    "python": str(dft_root / "venv/bin/python"),
                    "requirements_lock_sha256": "sha256:" + "1" * 64,
                    "aimnet_source_lock_sha256": "sha256:" + "2" * 64,
                    "models": {
                        name: "sha256:" + f"{index:x}" * 64
                        for index, name in enumerate(
                            sorted(CONTROLLER.MONOMER_DFT_MODEL_ALIASES), start=3
                        )
                    },
                    "runtime_inventory_sha256": dft_inventory,
                },
                "runtime_env": {
                    "path": str(dft_env),
                    "sha256": CONTROLLER.sha256_file(dft_env),
                    "values": dft_values,
                },
                "systemd_unit": {
                    "target_path": str(dft_unit),
                    "sha256": CONTROLLER.sha256_file(dft_unit),
                    "systemd_state": {
                        "LoadState": "loaded",
                        "FragmentPath": str(dft_unit),
                        "DropInPaths": "",
                        "NeedDaemonReload": "no",
                        "UnitFileState": "enabled",
                        "ActiveState": "active",
                        "SubState": "running",
                    },
                    "process_identity": {
                        "main_pid": 321,
                        "invocation_id": "adopted-dft-invocation",
                    },
                    "control_release_id": active_control["release_id"],
                    "launcher_path": str(
                        controller.control_releases_dir
                        / active_control["release_id"]
                        / "worker_slot_runtime.py"
                    ),
                    "launcher_sha256": "sha256:" + "a" * 64,
                },
                "gpu": {
                    "index": CONTROLLER.MONOMER_DFT_GPU_INDEX,
                    "uuid": CONTROLLER.MONOMER_DFT_GPU_UUID,
                    "guard_mode": "enforce",
                    "guard_state_path": str(CONTROLLER.MONOMER_DFT_GUARD_STATE),
                    "guard_schema_version": 1,
                    "guard_status": "quarantined",
                    "contention_observed": True,
                },
                "health": {"active": 0, "queued": 0},
            },
            "adopted_at": CONTROLLER.utc_now(),
        }
        self.assertNotEqual(
            adopted["monomer_md"]["slot_record_file_sha256"],
            active_slot["slot_record_sha256"],
        )
        CONTROLLER.validate_adopted_deployment(adopted)
        # The controller fixture models target Git blobs without a real Git
        # object database.  Bind its prerequisite source paths to the exact
        # already-installed fixture bytes, including the test-root pg_service
        # path substitution.
        prerequisite_blobs = {
            source_path: (controller.config_dir / name).read_bytes()
            for source_path, name, _mode, _classification, _evidence_key in (
                CONTROLLER.ADOPTED_PREREQUISITE_FILES
            )
        }
        original_git_show = controller._git_show
        controller._git_show = (  # type: ignore[method-assign]
            lambda target_sha, relative: (
                prerequisite_blobs[relative]
                if relative in prerequisite_blobs
                else original_git_show(target_sha, relative)
            )
        )
        # The active A controller can produce the unchanged legacy
        # production-config schema before the adopted marker exists.  The
        # target controller then binds those same hashes to create-only
        # prerequisite provenance without adding a descriptor field.
        production_config = controller.production_config_evidence(
            check_free_space=False
        )
        bootstrap = CONTROLLER.load_private_json(
            controller.state_dir / "bootstrap-control.json"
        )
        CONTROLLER.atomic_json(controller.adopted_state_path, adopted)
        prerequisite_files = []
        for (
            source_path,
            name,
            mode,
            classification,
            evidence_key,
        ) in CONTROLLER.ADOPTED_PREREQUISITE_FILES:
            prerequisite_files.append(
                {
                    "source_path": source_path,
                    "destination": str(controller.config_dir / name),
                    "name": name,
                    "sha256": production_config[evidence_key],
                    "mode": mode,
                    "classification": classification,
                    "disposition": "existing-exact",
                }
            )
        prerequisite_plan = {
            "schema_version": 1,
            "authority_kind": "manual-runtime-adoption-prerequisites",
            "operation_id": "adopt-prereq-fixture-001",
            "source_sha": TARGET_SHA,
            "source_tree": TARGET_TREE,
            "source_readiness": bootstrap["source_readiness"],
            "source_readiness_sha256": CONTROLLER.canonical_json_digest(
                bootstrap["source_readiness"]
            ),
            "delivery_gate": bootstrap["delivery_gate"],
            "delivery_gate_sha256": CONTROLLER.canonical_json_digest(
                bootstrap["delivery_gate"]
            ),
            "adopted_deployment_sha256": CONTROLLER.sha256_file(
                controller.adopted_state_path
            ),
            "files": prerequisite_files,
            "preserved_pgpass": {
                "path": str(
                    controller.config_dir / CONTROLLER.MUTABLE_DATA_PGPASS
                ),
                "sha256": production_config[
                    "mutable_data_audit_pgpass_sha256"
                ],
                "mode": "0600",
            },
            "mutations": {
                "services": False,
                "source": False,
                "database": False,
                "credentials": False,
            },
        }
        prerequisite_authority = {
            "schema_version": 1,
            "status": "completed",
            "authority_kind": prerequisite_plan["authority_kind"],
            "operation_id": prerequisite_plan["operation_id"],
            "source_sha": prerequisite_plan["source_sha"],
            "source_tree": prerequisite_plan["source_tree"],
            "adopted_deployment_sha256": prerequisite_plan[
                "adopted_deployment_sha256"
            ],
            "plan_sha256": CONTROLLER.canonical_json_digest(
                prerequisite_plan
            ),
            "plan": prerequisite_plan,
            "completed_at": CONTROLLER.utc_now(),
        }
        CONTROLLER.atomic_json(
            controller.runtime_root
            / CONTROLLER.ADOPTED_PREREQUISITES_RELATIVE_PATH,
            prerequisite_authority,
        )
        bootstrap.update(
            {
                "schema_version": 3,
                "status": "completed",
                "authority_kind": "manual-runtime-adoption",
                "adopted_deployment": adopted,
                "adopted_deployment_sha256": CONTROLLER.canonical_json_digest(
                    adopted
                ),
                "adoption_evidence_sha256": adopted[
                    "adoption_evidence_sha256"
                ],
                "active_control": active_control,
            }
        )
        CONTROLLER.atomic_json(
            controller.state_dir / "bootstrap-control.json", bootstrap
        )
        controller.active_control_evidence = (  # type: ignore[method-assign]
            lambda: CONTROLLER._control_runtime.validate_active_control_record(
                CONTROLLER.load_private_json(controller.active_control_path)
            )
        )
        controller._fixture_git_permission_takeover = (  # type: ignore[attr-defined]
            adopted_git_permission_takeover_fixture(
                controller.production_root,
                controller.runtime_root,
                source_sha=PREVIOUS_SHA,
                source_tree=PREVIOUS_TREE,
                adopted_deployment_sha256=CONTROLLER.sha256_file(
                    controller.adopted_state_path
                ),
            )
        )
        controller._fixture_git_permission_source_successor_takeover = (  # type: ignore[attr-defined]
            adopted_git_permission_source_successor_fixture(
                controller._fixture_git_permission_takeover,  # type: ignore[attr-defined]
                target_sha=TARGET_SHA,
                target_tree=TARGET_TREE,
                production_root=controller.production_root,
                file_payloads={
                    path: controller._git_show(TARGET_SHA, path)
                    for path in CONTROLLER.ADOPTED_UNIT_PERMISSION_SUCCESSOR_V2_FILES
                },
            )
        )
        controller._fixture_production_source_trust_sha256 = (  # type: ignore[attr-defined]
            controller._fixture_git_permission_source_successor_takeover[  # type: ignore[attr-defined]
                "source_trust_sha256"
            ]
        )
        controller._fixture_unit_permission_takeover = (  # type: ignore[attr-defined]
            adopted_unit_permission_binding_fixture(
                controller._fixture_git_permission_takeover,  # type: ignore[attr-defined]
                source_successor_takeover=(
                    controller._fixture_git_permission_source_successor_takeover  # type: ignore[attr-defined]
                ),
                adopted_deployment_sha256=CONTROLLER.sha256_file(
                    controller.adopted_state_path
                ),
            )
        )
        return adopted

    @staticmethod
    def _rewrite_prerequisite_source(
        controller: FixtureController,
        *,
        source_sha: str,
        source_tree: str,
    ) -> dict[str, object]:
        authority_path = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_PREREQUISITES_RELATIVE_PATH
        )
        authority = CONTROLLER.load_private_json(authority_path)
        plan = authority["plan"]
        readiness = plan["source_readiness"]
        readiness.update(
            {
                "source_sha": source_sha,
                "source_tree": source_tree,
                "origin_main_sha": source_sha,
            }
        )
        delivery = plan["delivery_gate"]
        delivery["remote_main"] = source_sha
        delivery["ci"]["head_sha"] = source_sha
        plan.update(
            {
                "source_sha": source_sha,
                "source_tree": source_tree,
                "source_readiness_sha256": (
                    CONTROLLER.canonical_json_digest(readiness)
                ),
                "delivery_gate_sha256": CONTROLLER.canonical_json_digest(
                    delivery
                ),
            }
        )
        authority.update(
            {
                "source_sha": source_sha,
                "source_tree": source_tree,
                "plan_sha256": CONTROLLER.canonical_json_digest(plan),
            }
        )
        CONTROLLER.atomic_json(authority_path, authority)
        return authority

    def _seed_git_permission_wrapper(
        self,
        controller: FixtureController,
        adopted: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        marker_path = controller.git_permission_marker_path
        captured = git_permission_marker_fixture(
            controller.production_root, marker_path, phase="captured"
        )
        hardened = git_permission_marker_fixture(
            controller.production_root, marker_path, phase="hardened"
        )
        CONTROLLER.atomic_json(marker_path, hardened)
        bootstrap_path = controller.state_dir / "bootstrap-control.json"
        prerequisite_path = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_PREREQUISITES_RELATIVE_PATH
        )
        bootstrap = CONTROLLER.load_private_json(bootstrap_path)
        prerequisites = CONTROLLER.load_private_json(prerequisite_path)
        impact_fields = (
            "schema_version",
            "policy",
            "repository",
            "marker_path",
            "records",
            "inventory_sha256",
            "original_permissions_sha256",
            "hardened_permissions_sha256",
        )
        impact = {field: captured[field] for field in impact_fields}
        readiness = json.loads(json.dumps(bootstrap["source_readiness"]))
        readiness.update(
            {
                "source_sha": PREVIOUS_SHA,
                "source_tree": PREVIOUS_TREE,
                "origin_main_sha": PREVIOUS_SHA,
            }
        )
        delivery = json.loads(json.dumps(bootstrap["delivery_gate"]))
        delivery["remote_main"] = PREVIOUS_SHA
        delivery["ci"]["head_sha"] = PREVIOUS_SHA
        plan = {
            "schema_version": 1,
            "authority_kind": (
                CONTROLLER.ADOPTED_GIT_PERMISSION_AUTHORITY_KIND
            ),
            "operation_id": "adopt-git-permission-fixture-001",
            "source_sha": PREVIOUS_SHA,
            "source_tree": PREVIOUS_TREE,
            "source_readiness": readiness,
            "source_readiness_sha256": CONTROLLER.canonical_json_digest(
                readiness
            ),
            "delivery_gate": delivery,
            "delivery_gate_sha256": CONTROLLER.canonical_json_digest(
                delivery
            ),
            "adopted_deployment_sha256": CONTROLLER.sha256_file(
                controller.adopted_state_path
            ),
            "bootstrap_control_sha256": CONTROLLER.sha256_file(
                bootstrap_path
            ),
            "adopted_prerequisites_sha256": CONTROLLER.sha256_file(
                prerequisite_path
            ),
            "adopted_prerequisites_plan_sha256": prerequisites[
                "plan_sha256"
            ],
            "production_source": {
                "source_sha": adopted["source_sha"],
                "source_tree": adopted["source_tree"],
            },
            "permission_takeover": captured,
            "permission_impact_sha256": (
                CONTROLLER.canonical_json_digest(impact)
            ),
            "mutations": {
                "services": False,
                "source_content": False,
                "source_refs": False,
                "database": False,
                "credentials": False,
                "git_permissions": True,
                "runtime_authority": True,
            },
        }
        authority = {
            "schema_version": 1,
            "status": "completed",
            "authority_kind": plan["authority_kind"],
            "operation_id": plan["operation_id"],
            "source_sha": plan["source_sha"],
            "source_tree": plan["source_tree"],
            "production_source_sha": adopted["source_sha"],
            "production_source_tree": adopted["source_tree"],
            "adopted_deployment_sha256": plan[
                "adopted_deployment_sha256"
            ],
            "bootstrap_control_sha256": plan[
                "bootstrap_control_sha256"
            ],
            "adopted_prerequisites_sha256": plan[
                "adopted_prerequisites_sha256"
            ],
            "plan_sha256": CONTROLLER.canonical_json_digest(plan),
            "permission_impact_sha256": plan[
                "permission_impact_sha256"
            ],
            "permission_marker_sha256": CONTROLLER.sha256_file(marker_path),
            "permission_evidence_sha256": hardened["evidence_sha256"],
            "permission_inventory_sha256": hardened[
                "inventory_sha256"
            ],
            "original_permissions_sha256": hardened[
                "original_permissions_sha256"
            ],
            "hardened_permissions_sha256": hardened[
                "hardened_permissions_sha256"
            ],
            "plan": plan,
            "completed_at": "2026-08-14T00:00:00Z",
        }
        CONTROLLER.atomic_json(
            controller.adopted_git_permissions_path, authority
        )
        transaction_root = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_GIT_PERMISSION_TRANSACTION_DIRECTORY
        )
        transaction_root.mkdir(mode=0o700, exist_ok=True)
        CONTROLLER.atomic_json(
            transaction_root / f"{authority['operation_id']}.json",
            {
                "schema_version": 1,
                "status": "completed",
                "phase": "completed",
                "operation_id": authority["operation_id"],
                "plan": plan,
                "plan_sha256": authority["plan_sha256"],
                "permission_impact_sha256": authority[
                    "permission_impact_sha256"
                ],
                "permission_checkpoint": "permission:hardened",
                "permission_marker_sha256": authority[
                    "permission_marker_sha256"
                ],
                "permission_evidence_sha256": authority[
                    "permission_evidence_sha256"
                ],
                "source_trust_sha256": "sha256:" + "6" * 64,
                "created_at": "2026-08-14T00:00:00Z",
                "completed_at": authority["completed_at"],
                "aborted_at": None,
            },
        )
        return hardened, authority

    def _seed_unit_permission_wrapper(
        self,
        controller: FixtureController,
        adopted: dict[str, object],
        marker: dict[str, object],
        source_successor: dict[str, object] | None = None,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        marker_digest = CONTROLLER.sha256_file(
            controller.git_permission_marker_path
        )
        git_combined = (
            CONTROLLER.PullDeployController._validate_adopted_git_permission_authority(
                controller,
                adopted=adopted,
                marker=marker,
                marker_digest=marker_digest,
            )
        )
        md_path = Path(
            adopted["monomer_md"]["systemd_unit"]["target_path"]
        )
        dft_path = Path(
            adopted["monomer_dft"]["systemd_unit"]["target_path"]
        )
        parent_metadata = md_path.parent.lstat()
        parent = {
            "path": str(md_path.parent),
            "type": "directory",
            "device": parent_metadata.st_dev,
            "inode": parent_metadata.st_ino,
            "uid": parent_metadata.st_uid,
            "gid": parent_metadata.st_gid,
            "mode": f"{CONTROLLER.stat.S_IMODE(parent_metadata.st_mode):04o}",
            "nlink": parent_metadata.st_nlink,
            "size": parent_metadata.st_size,
        }

        def unit_record(
            role: str,
            name: str,
            path: Path,
            *,
            mode: str,
            inode: int | None = None,
        ) -> dict[str, object]:
            metadata = path.lstat()
            return {
                "role": role,
                "name": name,
                "path": str(path),
                "parent": dict(parent),
                "type": "file",
                "device": metadata.st_dev,
                "inode": metadata.st_ino if inode is None else inode,
                "uid": metadata.st_uid,
                "gid": metadata.st_gid,
                "mode": mode,
                "target_mode": "0600",
                "nlink": 1,
                "size": metadata.st_size,
                "content_sha256": CONTROLLER.sha256_file(path),
                "action": (
                    "atomic-inode-replace"
                    if role == "monomer-md"
                    else "no-op-cas"
                ),
                "systemd_state": {
                    "LoadState": "loaded",
                    "FragmentPath": str(path),
                    "DropInPaths": "",
                    "NeedDaemonReload": "no",
                    "UnitFileState": "enabled",
                    "ActiveState": "active",
                    "SubState": "running",
                },
                "process_identity": {
                    "main_pid": 401 if role == "monomer-md" else 402,
                    "invocation_id": (
                        "1" * 32 if role == "monomer-md" else "2" * 32
                    ),
                },
            }

        md_metadata = md_path.lstat()
        original_units = [
            unit_record(
                "monomer-md",
                CONTROLLER.MONOMER_MD_UNIT_NAME,
                md_path,
                mode="0664",
                inode=md_metadata.st_ino + 1000000,
            ),
            unit_record(
                "monomer-dft",
                CONTROLLER.MONOMER_DFT_UNIT_NAME,
                dft_path,
                mode="0600",
            ),
        ]
        hardened_units = json.loads(json.dumps(original_units))
        hardened_units[0].update(
            {
                "inode": md_metadata.st_ino,
                "mode": "0600",
            }
        )
        bootstrap_path = controller.state_dir / "bootstrap-control.json"
        prerequisite_path = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_PREREQUISITES_RELATIVE_PATH
        )
        bootstrap = CONTROLLER.load_private_json(bootstrap_path)
        prerequisite = CONTROLLER.load_private_json(prerequisite_path)
        compact = adopted_unit_permission_binding_fixture(
            git_combined,
            source_successor_takeover=source_successor,
            adopted_deployment_sha256=CONTROLLER.sha256_file(
                controller.adopted_state_path
            ),
            file_digests=(
                None
                if source_successor is not None
                else {
                    path: CONTROLLER.sha256_bytes(
                        controller._git_show(TARGET_SHA, path)
                    )
                    for path in CONTROLLER.ADOPTED_UNIT_PERMISSION_SUCCESSOR_FILES
                }
            ),
        )
        operation_id = "adopt-unit-permission-fixture-001"
        final_name = CONTROLLER.ADOPTED_UNIT_PERMISSIONS_RELATIVE_PATH.name
        staging_name = f".{final_name}.create-{operation_id}"
        quarantine_name = f"{staging_name}.quarantine"
        authority_publication = {
            "schema_version": 1,
            "policy": (
                CONTROLLER.ADOPTED_UNIT_PERMISSION_AUTHORITY_PUBLICATION_POLICY
            ),
            "directory": str(controller.state_dir),
            "entries": [
                {
                    "role": "final",
                    "name": final_name,
                    "path": str(controller.state_dir / final_name),
                    "initially_absent": True,
                },
                {
                    "role": "staging",
                    "name": staging_name,
                    "path": str(controller.state_dir / staging_name),
                    "initially_absent": True,
                },
                {
                    "role": "staging-quarantine",
                    "name": quarantine_name,
                    "path": str(controller.state_dir / quarantine_name),
                    "initially_absent": True,
                },
            ],
        }
        impact = {
            "schema_version": 1,
            "policy": "nexpoly-adopted-unit-permission-hardening-v1",
            "units": original_units,
            "authority_publication": authority_publication,
        }
        plan = {
            "schema_version": 2 if source_successor is not None else 1,
            "authority_kind": (
                CONTROLLER.ADOPTED_UNIT_PERMISSION_AUTHORITY_KIND
            ),
            "operation_id": operation_id,
            "source_sha": TARGET_SHA,
            "source_tree": TARGET_TREE,
            "source_readiness": bootstrap["source_readiness"],
            "source_readiness_sha256": CONTROLLER.canonical_json_digest(
                bootstrap["source_readiness"]
            ),
            "delivery_gate": bootstrap["delivery_gate"],
            "delivery_gate_sha256": CONTROLLER.canonical_json_digest(
                bootstrap["delivery_gate"]
            ),
            "adopted_deployment_sha256": CONTROLLER.sha256_file(
                controller.adopted_state_path
            ),
            "bootstrap_control_sha256": CONTROLLER.sha256_file(
                bootstrap_path
            ),
            "adopted_prerequisites_sha256": CONTROLLER.sha256_file(
                prerequisite_path
            ),
            "adopted_prerequisites_plan_sha256": prerequisite[
                "plan_sha256"
            ],
            "production_source": {
                "source_sha": adopted["source_sha"],
                "source_tree": adopted["source_tree"],
            },
            "adopted_git_permissions_sha256": git_combined[
                "authority_file_sha256"
            ],
            "git_permission_successor": compact[
                "git_permission_successor"
            ],
            "units": original_units,
            "authority_publication": authority_publication,
            "unit_permission_impact_sha256": (
                CONTROLLER.canonical_json_digest(impact)
            ),
            "mutations": {
                "services_restarted": False,
                "source": False,
                "database": False,
                "credentials": False,
                "md_unit_inode": True,
                "dft_unit": False,
                "runtime_authority": True,
                "systemd_daemon_reload": True,
            },
        }
        if source_successor is not None:
            plan[
                "adopted_git_permission_source_successor_sha256"
            ] = source_successor["authority_file_sha256"]
        plan_digest = CONTROLLER.canonical_json_digest(plan)
        owner = {
            "schema_version": 1,
            "authority_kind": (
                CONTROLLER.ADOPTED_UNIT_PERMISSION_AUTHORITY_KIND
            ),
            "operation_id": operation_id,
            "plan_sha256": plan_digest,
            "md_original_sha256": CONTROLLER.canonical_json_digest(
                original_units[0]
            ),
            "content_sha256": original_units[0]["content_sha256"],
        }
        backup_root = (
            controller.state_dir / "adopted-unit-permission-backups"
        )
        backup_root.mkdir(mode=0o700)
        operation_root = backup_root / operation_id
        operation_root.mkdir(mode=0o700)
        write_private(
            backup_root / f".{operation_id}.owner.json",
            json.dumps(owner, sort_keys=True) + "\n",
        )
        write_private(
            operation_root / ".owner.json",
            json.dumps(owner, sort_keys=True) + "\n",
        )
        backup_path = operation_root / CONTROLLER.MONOMER_MD_UNIT_NAME
        write_private(backup_path, md_path.read_text(encoding="utf-8"))
        backup_metadata = backup_path.lstat()
        backup_unit = {
            "path": str(backup_path),
            "type": "file",
            "device": backup_metadata.st_dev,
            "inode": backup_metadata.st_ino,
            "uid": backup_metadata.st_uid,
            "gid": backup_metadata.st_gid,
            "mode": "0600",
            "nlink": backup_metadata.st_nlink,
            "size": backup_metadata.st_size,
            "content_sha256": CONTROLLER.sha256_file(backup_path),
        }
        backup = {
            "schema_version": 1,
            "operation_id": operation_id,
            "owner": owner,
            "owner_sha256": CONTROLLER.canonical_json_digest(owner),
            "claim_path": str(
                backup_root / f".{operation_id}.owner.json"
            ),
            "claim_sha256": CONTROLLER.canonical_json_digest(owner),
            "unit": backup_unit,
            "unit_sha256": CONTROLLER.canonical_json_digest(backup_unit),
            "inventory_sha256": CONTROLLER.private_tree_inventory_digest(
                operation_root
            ),
        }
        authority = {
            "schema_version": 2 if source_successor is not None else 1,
            "status": "completed",
            "authority_kind": plan["authority_kind"],
            "operation_id": operation_id,
            "source_sha": TARGET_SHA,
            "source_tree": TARGET_TREE,
            "production_source_sha": adopted["source_sha"],
            "production_source_tree": adopted["source_tree"],
            "adopted_deployment_sha256": plan[
                "adopted_deployment_sha256"
            ],
            "bootstrap_control_sha256": plan[
                "bootstrap_control_sha256"
            ],
            "adopted_prerequisites_sha256": plan[
                "adopted_prerequisites_sha256"
            ],
            "adopted_git_permissions_sha256": plan[
                "adopted_git_permissions_sha256"
            ],
            "adopted_git_permission_source_sha": compact[
                "adopted_git_permission_source_sha"
            ],
            "adopted_git_permission_source_tree": compact[
                "adopted_git_permission_source_tree"
            ],
            "plan_sha256": plan_digest,
            "unit_permission_impact_sha256": plan[
                "unit_permission_impact_sha256"
            ],
            "original_units": original_units,
            "original_units_sha256": CONTROLLER.canonical_json_digest(
                original_units
            ),
            "hardened_units": hardened_units,
            "hardened_units_sha256": CONTROLLER.canonical_json_digest(
                hardened_units
            ),
            "backup": backup,
            "backup_sha256": CONTROLLER.canonical_json_digest(backup),
            "backup_content_sha256": backup_unit["content_sha256"],
            "plan": plan,
            "completed_at": "2026-08-14T00:00:00Z",
        }
        if source_successor is not None:
            authority[
                "adopted_git_permission_source_successor_sha256"
            ] = source_successor["authority_file_sha256"]
        CONTROLLER.atomic_json(
            controller.adopted_unit_permissions_path,
            authority,
        )
        transition_fields = {
            "path",
            "type",
            "device",
            "inode",
            "uid",
            "gid",
            "mode",
            "nlink",
            "size",
            "content_sha256",
        }
        replacement = {
            field: hardened_units[0][field]
            for field in transition_fields
        }
        staging = {
            **replacement,
            "path": str(
                md_path.parent
                / (
                    f".{CONTROLLER.MONOMER_MD_UNIT_NAME}."
                    f"{operation_id}.replacement"
                )
            ),
        }
        transaction_root = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_UNIT_PERMISSION_TRANSACTION_DIRECTORY
        )
        transaction_root.mkdir(mode=0o700, exist_ok=True)
        CONTROLLER.atomic_json(
            transaction_root / f"{operation_id}.json",
            {
                "schema_version": authority["schema_version"],
                "status": "completed",
                "phase": "completed",
                "operation_id": operation_id,
                "plan": plan,
                "plan_sha256": plan_digest,
                "unit_permission_impact_sha256": authority[
                    "unit_permission_impact_sha256"
                ],
                "replacement_checkpoint": "hardened",
                "backup": backup,
                "staging": staging,
                "replacement": replacement,
                "unit_evidence": hardened_units,
                "source_trust_sha256": (
                    source_successor["source_trust_sha256"]
                    if source_successor is not None
                    else "sha256:" + "6" * 64
                ),
                "created_at": "2026-08-13T23:59:59Z",
                "completed_at": authority["completed_at"],
                "aborted_at": None,
            },
        )
        return git_combined, hardened_units

    def _seed_aborted_unit_permission_journal(
        self,
        controller: FixtureController,
        *,
        completed_operation_id: str = "adopt-unit-permission-fixture-001",
        operation_id: str = "adopt-unit-permission-aborted-001",
    ) -> Path:
        transaction_root = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_UNIT_PERMISSION_TRANSACTION_DIRECTORY
        )
        completed = CONTROLLER.load_private_json(
            transaction_root / f"{completed_operation_id}.json"
        )
        plan = json.loads(json.dumps(completed["plan"]))
        plan["operation_id"] = operation_id
        publication = plan["authority_publication"]
        for entry in publication["entries"]:
            old_name = entry["name"]
            new_name = old_name.replace(
                completed_operation_id,
                operation_id,
            )
            entry["name"] = new_name
            entry["path"] = str(controller.state_dir / new_name)
        impact = {
            "schema_version": 1,
            "policy": "nexpoly-adopted-unit-permission-hardening-v1",
            "units": plan["units"],
            "authority_publication": publication,
        }
        plan["unit_permission_impact_sha256"] = (
            CONTROLLER.canonical_json_digest(impact)
        )
        path = transaction_root / f"{operation_id}.json"
        CONTROLLER.atomic_json(
            path,
            {
                "schema_version": plan["schema_version"],
                "status": "aborted",
                "phase": "aborted",
                "operation_id": operation_id,
                "plan": plan,
                "plan_sha256": CONTROLLER.canonical_json_digest(plan),
                "unit_permission_impact_sha256": plan[
                    "unit_permission_impact_sha256"
                ],
                "replacement_checkpoint": None,
                "backup": None,
                "staging": None,
                "replacement": None,
                "unit_evidence": None,
                "source_trust_sha256": None,
                "created_at": "2026-08-13T23:59:57Z",
                "completed_at": None,
                "aborted_at": "2026-08-13T23:59:58Z",
            },
        )
        return path

    def _seed_source_successor_wrapper(
        self,
        controller: FixtureController,
        adopted: dict[str, object],
        marker: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        marker_digest = CONTROLLER.sha256_file(
            controller.git_permission_marker_path
        )
        root = (
            CONTROLLER.PullDeployController._validate_adopted_git_permission_authority(
                controller,
                adopted=adopted,
                marker=marker,
                marker_digest=marker_digest,
            )
        )
        root_binding = root["authority"]
        bootstrap_path = controller.state_dir / "bootstrap-control.json"
        prerequisite_path = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_PREREQUISITES_RELATIVE_PATH
        )
        bootstrap = CONTROLLER.load_private_json(bootstrap_path)
        snapshot_operation = "snapshot-git-controller-fixture-001"
        snapshot_root = (
            controller.runtime_root
            / "backups/production-git"
            / snapshot_operation
        )
        snapshot_backup = snapshot_root / "git"
        snapshot_backup.mkdir(parents=True, mode=0o700)
        snapshot_root.chmod(0o700)
        snapshot_manifest = {
            "schema_version": 1,
            "policy": "nexpoly-production-git-raw-manifest-v1",
            "root_mode": "0700",
            "records": [],
            "records_sha256": CONTROLLER.sha256_bytes(
                CONTROLLER.canonical_json_bytes([]) + b"\n"
            ),
            "file_count": 0,
            "directory_count": 0,
            "total_file_bytes": 0,
        }
        snapshot_manifest_path = snapshot_root / "MANIFEST.json"
        CONTROLLER.atomic_json(snapshot_manifest_path, snapshot_manifest)
        snapshot_manifest_digest = CONTROLLER.sha256_file(
            snapshot_manifest_path
        )
        snapshot_summary = {
            "records_sha256": snapshot_manifest["records_sha256"],
            "file_count": 0,
            "directory_count": 0,
            "total_file_bytes": 0,
        }
        snapshot_authority = {
            "schema_version": 1,
            "status": "completed",
            "authority_kind": (
                "manual-runtime-adoption-production-git-snapshot"
            ),
            "policy": "nexpoly-production-git-golden-snapshot-v1",
            "operation_id": snapshot_operation,
            "target_source_sha": TARGET_SHA,
            "target_source_tree": TARGET_TREE,
            "production_source_sha": adopted["source_sha"],
            "production_source_tree": adopted["source_tree"],
            "production_git_dir": str(controller.production_root / ".git"),
            "backup_git_dir": str(snapshot_backup),
            "manifest_path": str(snapshot_manifest_path),
            "manifest_sha256": snapshot_manifest_digest,
            "manifest_summary": snapshot_summary,
            "fsck": {
                "schema_version": 1,
                "policy": "git-fsck-strict-full-no-reflogs-v1",
                "exit_code": 0,
                "stdout_sha256": "sha256:" + "6" * 64,
                "stderr_sha256": "sha256:" + "7" * 64,
                "stdout_lines": 0,
                "stderr_lines": 0,
            },
            "delivery_gate": bootstrap["delivery_gate"],
            "delivery_gate_sha256": CONTROLLER.sha256_bytes(
                CONTROLLER.canonical_json_bytes(
                    bootstrap["delivery_gate"]
                )
                + b"\n"
            ),
            "plan_sha256": "sha256:" + "8" * 64,
            "snapshot_impact_sha256": "sha256:" + "9" * 64,
            "copy_policy": (
                "descriptor-relative-read-write-no-link-no-reflink-v1"
            ),
            "completed_at": "2026-08-14T00:00:00Z",
        }
        snapshot_path = (
            controller.runtime_root
            / CONTROLLER.PRODUCTION_GIT_SNAPSHOT_RELATIVE_PATH
        )
        CONTROLLER.atomic_json(snapshot_path, snapshot_authority)
        snapshot_digest = CONTROLLER.sha256_file(snapshot_path)
        snapshot_compact = {
            "authority_kind": snapshot_authority["authority_kind"],
            "operation_id": snapshot_operation,
            "target_source_sha": TARGET_SHA,
            "target_source_tree": TARGET_TREE,
            "production_source_sha": adopted["source_sha"],
            "production_source_tree": adopted["source_tree"],
            "manifest_sha256": snapshot_manifest_digest,
            "manifest_summary": snapshot_summary,
            "delivery_gate_sha256": snapshot_authority[
                "delivery_gate_sha256"
            ],
            "completed_at": snapshot_authority["completed_at"],
            "authority_sha256": snapshot_digest,
        }
        files_fixture = adopted_git_permission_source_successor_fixture(
            root,
            target_sha=TARGET_SHA,
            target_tree=TARGET_TREE,
            production_root=controller.production_root,
            file_payloads={
                path: controller._git_show(TARGET_SHA, path)
                for path in CONTROLLER.ADOPTED_UNIT_PERMISSION_SUCCESSOR_V2_FILES
            },
            delivery_gate=bootstrap["delivery_gate"],
        )
        files = files_fixture["fixed_files"]
        changed_paths = list(
            CONTROLLER.ADOPTED_GIT_PERMISSION_SOURCE_SUCCESSOR_ALLOWED_CHANGED_FILES
        )
        operation_id = "adopt-git-successor-fixture-001"
        final_name = (
            CONTROLLER.ADOPTED_GIT_PERMISSION_SOURCE_SUCCESSOR_RELATIVE_PATH.name
        )
        staging_name = f".{final_name}.create-{operation_id}"
        publication = {
            "schema_version": 1,
            "policy": "nexpoly-source-successor-authority-publication-v1",
            "directory": str(controller.state_dir),
            "entries": [
                {
                    "role": "final",
                    "name": final_name,
                    "path": str(controller.state_dir / final_name),
                    "initially_absent": True,
                },
                {
                    "role": "staging",
                    "name": staging_name,
                    "path": str(controller.state_dir / staging_name),
                    "initially_absent": True,
                },
                {
                    "role": "staging-quarantine",
                    "name": f"{staging_name}.quarantine",
                    "path": str(
                        controller.state_dir
                        / f"{staging_name}.quarantine"
                    ),
                    "initially_absent": True,
                },
            ],
        }
        mutations = {
            "services": False,
            "source": False,
            "source_refs": False,
            "database": False,
            "credentials": False,
            "git_permissions": False,
            "units": False,
            "runtime_authority": True,
        }
        root_journal_path = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_GIT_PERMISSION_TRANSACTION_DIRECTORY
            / f"{root_binding['operation_id']}.json"
        )
        root_journal = CONTROLLER.load_private_json(root_journal_path)
        predecessor_source_trust_sha256 = root_journal[
            "source_trust_sha256"
        ]
        production_source_trust_sha256 = files_fixture[
            "source_trust_sha256"
        ]
        repository_transition = files_fixture[
            "production_repository_transition"
        ]
        repository_transition_sha256 = files_fixture[
            "production_repository_transition_sha256"
        ]
        self.assertNotEqual(
            files_fixture["source_trust_sha256"],
            predecessor_source_trust_sha256,
        )
        predecessor = {
            "authority_kind": root_binding["authority_kind"],
            "operation_id": root_binding["operation_id"],
            "source_sha": root_binding["source_sha"],
            "source_tree": root_binding["source_tree"],
            "authority_sha256": root["authority_file_sha256"],
            "plan_sha256": root_binding["plan_sha256"],
            "permission_marker_sha256": root_binding[
                "permission_marker_sha256"
            ],
            "permission_evidence_sha256": root_binding[
                "permission_evidence_sha256"
            ],
            "permission_inventory_sha256": root_binding[
                "permission_inventory_sha256"
            ],
            "original_permissions_sha256": root_binding[
                "original_permissions_sha256"
            ],
            "hardened_permissions_sha256": root_binding[
                "hardened_permissions_sha256"
            ],
            "completed_journal_sha256": CONTROLLER.sha256_file(
                root_journal_path
            ),
            "source_trust_sha256": predecessor_source_trust_sha256,
        }
        marker_projection = {
            "path": str(controller.git_permission_marker_path),
            "raw_sha256": root_binding["permission_marker_sha256"],
            "evidence_sha256": root_binding[
                "permission_evidence_sha256"
            ],
            "inventory_sha256": root_binding[
                "permission_inventory_sha256"
            ],
            "original_permissions_sha256": root_binding[
                "original_permissions_sha256"
            ],
            "hardened_permissions_sha256": root_binding[
                "hardened_permissions_sha256"
            ],
        }
        records = {record["path"]: record for record in files}
        required_jobs = bootstrap["delivery_gate"]["ci"]["required_jobs"]
        verifier = {
            "schema_version": 1,
            "policy": "nexpoly-frozen-predecessor-verifier-agreement-v1",
            "candidate_execution": "forbidden-before-authority",
            "predecessor_source_sha": root_binding["source_sha"],
            "predecessor_source_tree": root_binding["source_tree"],
            "bootstrap": records["scripts/bootstrap_pull_deploy.py"],
            "git_source_trust": records["scripts/git_source_trust.py"],
            "ci_contract": records["scripts/bridge_deploy_core.py"],
            "required_jobs": required_jobs,
            "required_jobs_sha256": CONTROLLER.canonical_json_digest(
                required_jobs
            ),
        }
        impact = {
            "schema_version": 1,
            "policy": (
                "nexpoly-adopted-git-permission-source-successor-impact-v1"
            ),
            "snapshot_authority_sha256": snapshot_digest,
            "predecessor_authority_sha256": root[
                "authority_file_sha256"
            ],
            "predecessor_marker_sha256": root_binding[
                "permission_marker_sha256"
            ],
            "production_source_trust_sha256": (
                production_source_trust_sha256
            ),
            "production_repository_transition_sha256": (
                repository_transition_sha256
            ),
            "target": {
                "source_sha": TARGET_SHA,
                "source_tree": TARGET_TREE,
            },
            "files": files,
            "files_sha256": CONTROLLER.canonical_json_digest(files),
            "changed_paths": changed_paths,
            "changed_paths_sha256": CONTROLLER.canonical_json_digest(
                changed_paths
            ),
            "authority_publication": publication,
            "mutations": mutations,
        }
        plan = {
            "schema_version": 2,
            "authority_kind": (
                CONTROLLER.ADOPTED_GIT_PERMISSION_SOURCE_SUCCESSOR_AUTHORITY_KIND
            ),
            "policy": (
                CONTROLLER.ADOPTED_GIT_PERMISSION_SOURCE_SUCCESSOR_POLICY
            ),
            "operation_id": operation_id,
            "source_sha": TARGET_SHA,
            "source_tree": TARGET_TREE,
            "source_readiness": bootstrap["source_readiness"],
            "source_readiness_sha256": CONTROLLER.canonical_json_digest(
                bootstrap["source_readiness"]
            ),
            "delivery_gate": bootstrap["delivery_gate"],
            "delivery_gate_sha256": CONTROLLER.canonical_json_digest(
                bootstrap["delivery_gate"]
            ),
            "adopted_deployment_sha256": root_binding[
                "adopted_deployment_sha256"
            ],
            "bootstrap_control_sha256": root_binding[
                "bootstrap_control_sha256"
            ],
            "adopted_prerequisites_sha256": root_binding[
                "adopted_prerequisites_sha256"
            ],
            "production_git_snapshot": snapshot_compact,
            "snapshot_authority_sha256": snapshot_digest,
            "production_source": {
                "source_sha": adopted["source_sha"],
                "source_tree": adopted["source_tree"],
            },
            "predecessor": predecessor,
            "marker": marker_projection,
            "verifier_agreement": verifier,
            "files": files,
            "files_sha256": CONTROLLER.canonical_json_digest(files),
            "changed_paths": changed_paths,
            "changed_paths_sha256": CONTROLLER.canonical_json_digest(
                changed_paths
            ),
            "authority_publication": publication,
            "source_successor_impact": impact,
            "source_successor_impact_sha256": (
                CONTROLLER.canonical_json_digest(impact)
            ),
            "production_source_trust_sha256": (
                production_source_trust_sha256
            ),
            "production_repository_transition": repository_transition,
            "production_repository_transition_sha256": (
                repository_transition_sha256
            ),
            "mutations": mutations,
        }
        authority = {
            "schema_version": 2,
            "status": "completed",
            "authority_kind": plan["authority_kind"],
            "policy": plan["policy"],
            "operation_id": operation_id,
            "source_sha": TARGET_SHA,
            "source_tree": TARGET_TREE,
            "predecessor_source_sha": root_binding["source_sha"],
            "predecessor_source_tree": root_binding["source_tree"],
            "predecessor_authority_sha256": root[
                "authority_file_sha256"
            ],
            "predecessor_marker_sha256": root_binding[
                "permission_marker_sha256"
            ],
            "adopted_deployment_sha256": plan[
                "adopted_deployment_sha256"
            ],
            "bootstrap_control_sha256": plan[
                "bootstrap_control_sha256"
            ],
            "adopted_prerequisites_sha256": plan[
                "adopted_prerequisites_sha256"
            ],
            "snapshot_authority_sha256": snapshot_digest,
            "plan_sha256": CONTROLLER.canonical_json_digest(plan),
            "source_successor_impact_sha256": plan[
                "source_successor_impact_sha256"
            ],
            "files_sha256": plan["files_sha256"],
            "changed_paths": changed_paths,
            "changed_paths_sha256": plan["changed_paths_sha256"],
            "delivery_gate": plan["delivery_gate"],
            "delivery_gate_sha256": plan["delivery_gate_sha256"],
            "verifier_agreement_sha256": (
                CONTROLLER.canonical_json_digest(verifier)
            ),
            "production_source_trust_sha256": (
                production_source_trust_sha256
            ),
            "production_repository_transition_sha256": (
                repository_transition_sha256
            ),
            "plan": plan,
            "completed_at": "2026-08-14T00:00:00Z",
        }
        CONTROLLER.atomic_json(
            controller.adopted_git_permission_source_successor_path,
            authority,
        )
        transaction_root = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_GIT_PERMISSION_SOURCE_SUCCESSOR_TRANSACTION_DIRECTORY
        )
        transaction_root.mkdir(mode=0o700)
        journal = {
            "schema_version": 1,
            "status": "completed",
            "phase": "completed",
            "operation_id": operation_id,
            "plan": plan,
            "plan_sha256": authority["plan_sha256"],
            "source_successor_impact_sha256": authority[
                "source_successor_impact_sha256"
            ],
            "production_source_trust_sha256": authority[
                "production_source_trust_sha256"
            ],
            "created_at": "2026-08-14T00:00:00Z",
            "completed_at": authority["completed_at"],
            "aborted_at": None,
        }
        CONTROLLER.atomic_json(
            transaction_root / f"{operation_id}.json",
            journal,
        )
        compact = (
            CONTROLLER.PullDeployController._validate_adopted_git_permission_source_successor_authority(
                controller,
                adopted=adopted,
                git_permission_takeover=root,
            )
        )
        return root, compact

    def _activate_raw_source_successor_consumer(
        self,
        controller: FixtureController,
    ) -> tuple[dict[str, object], dict[str, object]]:
        adopted = self._seed_adopted_authority(controller)
        marker, _root_raw = self._seed_git_permission_wrapper(
            controller, adopted
        )
        root, source_successor = self._seed_source_successor_wrapper(
            controller,
            adopted,
            marker,
        )
        unit_root, _hardened = self._seed_unit_permission_wrapper(
            controller,
            adopted,
            marker,
            source_successor=source_successor,
        )
        unit = (
            CONTROLLER.PullDeployController._validate_adopted_unit_permission_authority(
                controller,
                adopted=adopted,
                git_permission_takeover=unit_root,
                git_permission_source_successor_takeover=source_successor,
                verify_live=False,
            )
        )
        controller._fixture_git_permission_takeover = root  # type: ignore[attr-defined]
        controller._fixture_git_permission_source_successor_takeover = None  # type: ignore[attr-defined]
        controller._fixture_unit_permission_takeover = unit  # type: ignore[attr-defined]
        controller._fixture_production_source_trust_sha256 = source_successor[  # type: ignore[attr-defined]
            "source_trust_sha256"
        ]

        def production_source_trust_digest(
            *, source_sha: str, source_tree: str
        ) -> str:
            self.assertEqual(
                (source_sha, source_tree),
                (
                    source_successor["production_source_sha"],
                    source_successor["production_source_tree"],
                ),
            )
            return str(source_successor["source_trust_sha256"])

        controller._production_source_trust_digest = (  # type: ignore[method-assign]
            production_source_trust_digest
        )
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        return root, source_successor

    def test_source_successor_raw_chain_and_unit_v2_are_consumed(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _root_raw = self._seed_git_permission_wrapper(
            controller, adopted
        )
        root, source_successor = self._seed_source_successor_wrapper(
            controller,
            adopted,
            marker,
        )
        controller._fixture_git_permission_takeover = root  # type: ignore[attr-defined]
        controller._fixture_git_permission_source_successor_takeover = (  # type: ignore[attr-defined]
            source_successor
        )
        observed = (
            CONTROLLER.PullDeployController._git_permission_source_successor_takeover(
                controller,
                git_permission_takeover=root,
            )
        )
        self.assertEqual(observed, source_successor)
        raw_successor = CONTROLLER.load_private_json(
            controller.adopted_git_permission_source_successor_path
        )
        self.assertNotEqual(
            raw_successor["plan"]["predecessor"]["source_trust_sha256"],
            raw_successor["production_source_trust_sha256"],
        )
        self.assertEqual(
            raw_successor["production_source_trust_sha256"],
            raw_successor["plan"]["production_source_trust_sha256"],
        )
        unit_root, _hardened = self._seed_unit_permission_wrapper(
            controller,
            adopted,
            marker,
            source_successor=source_successor,
        )
        unit = (
            CONTROLLER.PullDeployController._validate_adopted_unit_permission_authority(
                controller,
                adopted=adopted,
                git_permission_takeover=unit_root,
                git_permission_source_successor_takeover=source_successor,
                verify_live=False,
            )
        )
        self.assertEqual(unit["schema_version"], 2)
        self.assertEqual(
            unit["adopted_git_permission_source_successor_sha256"],
            source_successor["authority_file_sha256"],
        )
        self.assertEqual(
            unit["git_permission_successor"]["source_successor_authority"],
            source_successor,
        )

    def test_transition_validator_requires_direct_commit_baseline_refs(
        self,
    ) -> None:
        controller = self.controller()
        transition = production_repository_transition_fixture(
            controller.production_root,
            predecessor_sha=PREVIOUS_SHA,
            production_sha=PREVIOUS_SHA,
            production_tree=PREVIOUS_TREE,
            target_sha=TARGET_SHA,
            target_tree=TARGET_TREE,
            baseline_trust_sha256="sha256:" + "3" * 64,
        )
        cases = (
            ("refs/heads/main", "object_type", "blob"),
            (
                "refs/heads/main",
                "symbolic_target",
                "refs/heads/indirect-main",
            ),
            (CONTROLLER.DEPLOY_REMOTE_REF, "object_type", "tag"),
            (
                CONTROLLER.DEPLOY_REMOTE_REF,
                "symbolic_target",
                "refs/remotes/nexpoly-deploy/indirect-main",
            ),
        )
        for ref_name, field, replacement in cases:
            with self.subTest(ref=ref_name, field=field):
                changed = json.loads(json.dumps(transition))
                record = next(
                    record
                    for record in changed["logical_refs"]
                    if record["name"] == ref_name
                )
                record[field] = replacement
                changed["logical_refs_sha256"] = (
                    CONTROLLER.canonical_json_digest(
                        changed["logical_refs"]
                    )
                )
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "production logical ref baseline differs",
                ):
                    CONTROLLER.validate_production_repository_transition(
                        changed,
                        production_root=controller.production_root,
                        production_sha=PREVIOUS_SHA,
                        production_tree=PREVIOUS_TREE,
                        target_sha=TARGET_SHA,
                        target_tree=TARGET_TREE,
                        baseline_trust_sha256="sha256:" + "3" * 64,
                    )

    def test_source_successor_raw_and_journal_ambiguity_fail_closed(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _root_raw = self._seed_git_permission_wrapper(
            controller, adopted
        )
        root, source_successor = self._seed_source_successor_wrapper(
            controller,
            adopted,
            marker,
        )
        controller._fixture_git_permission_takeover = root  # type: ignore[attr-defined]
        authority_path = (
            controller.adopted_git_permission_source_successor_path
        )
        authority = CONTROLLER.load_private_json(authority_path)
        authority_path.write_text(
            json.dumps(authority, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(authority_path, 0o600)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "not canonical",
        ):
            CONTROLLER.PullDeployController._git_permission_source_successor_takeover(
                controller,
                git_permission_takeover=root,
            )
        CONTROLLER.atomic_json(authority_path, authority)

        transaction_root = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_GIT_PERMISSION_SOURCE_SUCCESSOR_TRANSACTION_DIRECTORY
        )
        journal_path = transaction_root / f"{source_successor['operation_id']}.json"
        journal = CONTROLLER.load_private_json(journal_path)
        journal_path.write_text(
            json.dumps(journal, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(journal_path, 0o600)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "journal differs",
        ):
            CONTROLLER.PullDeployController._git_permission_source_successor_takeover(
                controller,
                git_permission_takeover=root,
            )
        CONTROLLER.atomic_json(journal_path, journal)

        changed = json.loads(json.dumps(journal))
        changed["completed_at"] = "2026-08-14T00:00:01Z"
        CONTROLLER.atomic_json(journal_path, changed)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "journal differs",
        ):
            CONTROLLER.PullDeployController._git_permission_source_successor_takeover(
                controller,
                git_permission_takeover=root,
            )
        CONTROLLER.atomic_json(journal_path, journal)

        changed = json.loads(json.dumps(journal))
        changed["created_at"] = "2026-08-14T00:00:01Z"
        CONTROLLER.atomic_json(journal_path, changed)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "completion precedes creation",
        ):
            controller._source_successor_completed_journal_snapshot(
                source_successor
            )
        CONTROLLER.atomic_json(journal_path, journal)

        journal["unexpected"] = True
        CONTROLLER.atomic_json(journal_path, journal)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "journal",
        ):
            CONTROLLER.PullDeployController._git_permission_source_successor_takeover(
                controller,
                git_permission_takeover=root,
            )
        journal.pop("unexpected")
        CONTROLLER.atomic_json(journal_path, journal)

        staging = (
            controller.state_dir
            / (
                "."
                + CONTROLLER.ADOPTED_GIT_PERMISSION_SOURCE_SUCCESSOR_RELATIVE_PATH.name
                + ".create-foreign"
            )
        )
        write_private(staging, "residue\n")
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "one completed source-successor authority",
        ):
            CONTROLLER.PullDeployController._git_permission_source_successor_takeover(
                controller,
                git_permission_takeover=root,
            )
        staging.unlink()

        transaction_staging = controller.state_dir / (
            "."
            + CONTROLLER.ADOPTED_GIT_PERMISSION_SOURCE_SUCCESSOR_TRANSACTION_DIRECTORY.name
            + ".create-foreign"
        )
        transaction_staging.mkdir(mode=0o700)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "one completed source-successor authority",
        ):
            CONTROLLER.PullDeployController._git_permission_source_successor_takeover(
                controller,
                git_permission_takeover=root,
            )

    def test_source_successor_missing_completed_artifacts_fail_closed(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _root_raw = self._seed_git_permission_wrapper(
            controller, adopted
        )
        root, source_successor = self._seed_source_successor_wrapper(
            controller,
            adopted,
            marker,
        )
        controller._fixture_git_permission_takeover = root  # type: ignore[attr-defined]
        authority_path = (
            controller.adopted_git_permission_source_successor_path
        )
        authority = CONTROLLER.load_private_json(authority_path)
        transaction_root = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_GIT_PERMISSION_SOURCE_SUCCESSOR_TRANSACTION_DIRECTORY
        )
        journal_path = transaction_root / f"{source_successor['operation_id']}.json"
        journal = CONTROLLER.load_private_json(journal_path)

        authority_path.unlink()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "one completed source-successor authority",
        ):
            CONTROLLER.PullDeployController._git_permission_source_successor_takeover(
                controller,
                git_permission_takeover=root,
            )
        CONTROLLER.atomic_json(authority_path, authority)

        journal_path.unlink()
        transaction_root.rmdir()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "one completed source-successor authority",
        ):
            CONTROLLER.PullDeployController._git_permission_source_successor_takeover(
                controller,
                git_permission_takeover=root,
            )
        transaction_root.mkdir(mode=0o700)
        CONTROLLER.atomic_json(journal_path, journal)

        journal_path.unlink()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "journal lineage is incomplete",
        ):
            CONTROLLER.PullDeployController._git_permission_source_successor_takeover(
                controller,
                git_permission_takeover=root,
            )

    def test_source_successor_completed_journal_read_after_drift_is_rejected(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _root_raw = self._seed_git_permission_wrapper(
            controller, adopted
        )
        root, source_successor = self._seed_source_successor_wrapper(
            controller,
            adopted,
            marker,
        )
        controller._fixture_git_permission_takeover = root  # type: ignore[attr-defined]
        journal_path = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_GIT_PERMISSION_SOURCE_SUCCESSOR_TRANSACTION_DIRECTORY
            / f"{source_successor['operation_id']}.json"
        )
        original = CONTROLLER.load_private_json(journal_path)
        real_snapshot = (
            controller._source_successor_completed_journal_snapshot
        )

        for action in ("swap", "delete"):
            with self.subTest(action=action):
                calls = 0

                def drift_after_read(binding):  # type: ignore[no-untyped-def]
                    nonlocal calls
                    observed = real_snapshot(binding)
                    calls += 1
                    if calls == 1:
                        if action == "swap":
                            replacement = json.loads(json.dumps(original))
                            replacement["created_at"] = (
                                "2026-08-14T00:00:01Z"
                            )
                            CONTROLLER.atomic_json(
                                journal_path, replacement
                            )
                        else:
                            journal_path.unlink()
                    return observed

                with mock.patch.object(
                    controller,
                    "_source_successor_completed_journal_snapshot",
                    side_effect=drift_after_read,
                ), self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "journal|changed while validating",
                ):
                    CONTROLLER.PullDeployController._git_permission_source_successor_takeover(
                        controller,
                        git_permission_takeover=root,
                    )
                CONTROLLER.atomic_json(journal_path, original)

    def test_source_successor_journal_cas_is_required_by_plan(self) -> None:
        controller = self.controller()
        self._activate_raw_source_successor_consumer(controller)
        with mock.patch.object(
            controller,
            "_source_successor_completed_journal_snapshot",
            side_effect=CONTROLLER.PullDeployError(
                "injected source-successor journal CAS drift"
            ),
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "journal CAS drift",
        ):
            controller.plan(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )

    def test_source_successor_journal_cas_is_required_by_prepare(self) -> None:
        controller = self.controller()
        self._activate_raw_source_successor_consumer(controller)
        with mock.patch.object(
            controller,
            "_source_successor_completed_journal_snapshot",
            side_effect=CONTROLLER.PullDeployError(
                "injected source-successor journal CAS drift"
            ),
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "journal CAS drift",
        ):
            controller.prepare(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )

    def test_source_successor_journal_cas_is_required_pre_switch(self) -> None:
        controller = self.controller()
        self._activate_raw_source_successor_consumer(controller)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(
            controller.prepared_root / OPERATION_ID / "descriptor.json"
        )
        with mock.patch.object(
            controller,
            "_source_successor_completed_journal_snapshot",
            side_effect=CONTROLLER.PullDeployError(
                "injected source-successor journal CAS drift"
            ),
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "journal CAS drift",
        ):
            CONTROLLER.PullDeployController._revalidate_pre_switch(
                controller,
                descriptor,
            )

    def test_source_successor_manifest_drift_is_rejected(self) -> None:
        controller = self.controller()
        self._seed_adopted_authority(controller)
        original = controller._fixture_git_permission_source_successor_takeover  # type: ignore[attr-defined]

        def drift_mode(files: list[dict[str, object]]) -> None:
            files[0]["target"]["mode"] = "100644"

        def drift_blob(files: list[dict[str, object]]) -> None:
            files[0]["target"]["blob_sha"] = "f" * 40

        def drift_relation(files: list[dict[str, object]]) -> None:
            files[0]["relation"] = "changed"

        def drift_bridge(files: list[dict[str, object]]) -> None:
            self.assertEqual(
                files[-1]["path"], "scripts/bridge_deploy_core.py"
            )
            files[-1]["target"]["sha256"] = "sha256:" + "f" * 64

        for label, mutate in (
            ("mode", drift_mode),
            ("blob", drift_blob),
            ("relation", drift_relation),
            ("bridge", drift_bridge),
        ):
            with self.subTest(label=label):
                changed = json.loads(json.dumps(original))
                mutate(changed["fixed_files"])
                changed["fixed_files_sha256"] = (
                    CONTROLLER.canonical_json_digest(changed["fixed_files"])
                )
                changed["identity_sha256"] = (
                    CONTROLLER.canonical_json_digest(
                        {
                            key: value
                            for key, value in changed.items()
                            if key != "identity_sha256"
                        }
                    )
                )
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "manifest|Git identity|relation",
                ):
                    CONTROLLER.validate_adopted_git_permission_source_successor_binding(
                        changed
                    )

    def test_source_successor_raw_trust_must_match_plan_and_impact(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _root_raw = self._seed_git_permission_wrapper(
            controller, adopted
        )
        root, _source_successor = self._seed_source_successor_wrapper(
            controller,
            adopted,
            marker,
        )
        authority_path = (
            controller.adopted_git_permission_source_successor_path
        )
        original = CONTROLLER.load_private_json(authority_path)
        replacement = "sha256:" + "f" * 64

        for label in ("authority", "plan", "impact"):
            with self.subTest(label=label):
                changed = json.loads(json.dumps(original))
                plan = changed["plan"]
                if label == "authority":
                    changed["production_source_trust_sha256"] = replacement
                elif label == "plan":
                    plan["production_source_trust_sha256"] = replacement
                    changed["plan_sha256"] = (
                        CONTROLLER.canonical_json_digest(plan)
                    )
                else:
                    impact = plan["source_successor_impact"]
                    impact["production_source_trust_sha256"] = replacement
                    plan["source_successor_impact_sha256"] = (
                        CONTROLLER.canonical_json_digest(impact)
                    )
                    changed["source_successor_impact_sha256"] = plan[
                        "source_successor_impact_sha256"
                    ]
                    changed["plan_sha256"] = (
                        CONTROLLER.canonical_json_digest(plan)
                    )
                CONTROLLER.atomic_json(authority_path, changed)
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "evidence differs|provenance differs|repository transition",
                ):
                    CONTROLLER.PullDeployController._validate_adopted_git_permission_source_successor_authority(
                        controller,
                        adopted=adopted,
                        git_permission_takeover=root,
                    )
        CONTROLLER.atomic_json(authority_path, original)

    def test_source_successor_root_completed_journal_anchor_is_strict(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _root_raw = self._seed_git_permission_wrapper(
            controller, adopted
        )
        root, _source_successor = self._seed_source_successor_wrapper(
            controller,
            adopted,
            marker,
        )
        root_binding = root["authority"]
        transaction_root = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_GIT_PERMISSION_TRANSACTION_DIRECTORY
        )
        journal_path = transaction_root / f"{root_binding['operation_id']}.json"
        journal = CONTROLLER.load_private_json(journal_path)

        journal_path.write_text(
            json.dumps(journal, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(journal_path, 0o600)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "completed journal differs",
        ):
            CONTROLLER.PullDeployController._validate_adopted_git_permission_source_successor_authority(
                controller,
                adopted=adopted,
                git_permission_takeover=root,
            )
        CONTROLLER.atomic_json(journal_path, journal)

        changed = json.loads(json.dumps(journal))
        changed["source_trust_sha256"] = "sha256:" + "f" * 64
        CONTROLLER.atomic_json(journal_path, changed)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "evidence differs",
        ):
            CONTROLLER.PullDeployController._validate_adopted_git_permission_source_successor_authority(
                controller,
                adopted=adopted,
                git_permission_takeover=root,
            )
        CONTROLLER.atomic_json(journal_path, journal)

        journal_path.unlink()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "journal lineage is incomplete",
        ):
            CONTROLLER.PullDeployController._validate_adopted_git_permission_source_successor_authority(
                controller,
                adopted=adopted,
                git_permission_takeover=root,
            )
        CONTROLLER.atomic_json(journal_path, journal)

        CONTROLLER.atomic_json(transaction_root / "foreign.json", {})
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "journal lineage is incomplete",
        ):
            CONTROLLER.PullDeployController._validate_adopted_git_permission_source_successor_authority(
                controller,
                adopted=adopted,
                git_permission_takeover=root,
            )

    def test_v4_binding_rejects_source_successor_digest_drift(self) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(
            controller.prepared_root / OPERATION_ID / "descriptor.json"
        )
        for label, field, replacement in (
            ("successor-raw", "authority_file_sha256", "f"),
            ("root-predecessor", "predecessor_authority_sha256", "0"),
        ):
            with self.subTest(label=label):
                changed = json.loads(json.dumps(descriptor))
                binding = changed["adopted_prerequisite_target_binding"]
                successor = binding[
                    "git_permission_source_successor_authority"
                ]
                successor[field] = "sha256:" + replacement * 64
                successor["identity_sha256"] = (
                    CONTROLLER.canonical_json_digest(
                        {
                            key: value
                            for key, value in successor.items()
                            if key != "identity_sha256"
                        }
                    )
                )
                binding["identity_sha256"] = (
                    CONTROLLER.canonical_json_digest(
                        {
                            key: value
                            for key, value in binding.items()
                            if key != "identity_sha256"
                        }
                    )
                )
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "source-successor authority differs|unit permission authority differs",
                ):
                    CONTROLLER.validate_descriptor(changed)

    def test_v5_router_authority_is_required_before_read_only_plan(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        controller._fixture_bootstrap_router_successor = None  # type: ignore[attr-defined]
        before = CONTROLLER.directory_inventory_digest(
            controller.runtime_root
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lacks bootstrap-router authority",
        ):
            controller.plan(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )
        self.assertEqual(
            CONTROLLER.directory_inventory_digest(controller.runtime_root),
            before,
        )

    def test_v5_router_anchors_are_exactly_carried_into_current_state(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(
            controller.prepared_root / OPERATION_ID / "descriptor.json"
        )
        state = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        binding = descriptor["adopted_prerequisite_target_binding"]
        lineage = state["adoption_successor_lineage"]
        self.assertEqual(binding["schema_version"], 5)
        self.assertEqual(lineage["schema_version"], 3)
        for field in (
            "production_git_snapshot_authority_sha256",
            "bootstrap_router_intent_sha256",
            "bootstrap_router_authority_sha256",
        ):
            self.assertEqual(lineage[field], binding[field])
            changed_state = json.loads(json.dumps(state))
            changed_state["adoption_successor_lineage"][field] = (
                "sha256:" + "f" * 64
            )
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "successor lineage differs from descriptor authority",
            ):
                CONTROLLER.validate_current_state_adoption_lineage(
                    changed_state,
                    descriptor=descriptor,
                )

    def test_v4_binding_closes_materialization_to_transition(self) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        binding = controller.plan(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )["adopted_prerequisite_target_binding"]
        transition = binding[
            "git_permission_source_successor_authority"
        ]["production_repository_transition"]

        def reseal(document: dict[str, object]) -> None:
            document["identity_sha256"] = (
                CONTROLLER.canonical_json_digest(
                    {
                        key: value
                        for key, value in document.items()
                        if key != "identity_sha256"
                    }
                )
            )

        def for_phase(phase: str) -> dict[str, object]:
            changed = json.loads(json.dumps(binding))
            materialization = changed[
                "production_repository_materialization"
            ]
            materialized = phase in {"materialized", "prepared"}
            materialization.update(
                {
                    "phase": phase,
                    "semantic_object_count": transition[
                        "expected_materialized_object_count"
                        if materialized
                        else "baseline_semantic_object_count"
                    ],
                    "semantic_object_inventory_sha256": transition[
                        "expected_materialized_objects_sha256"
                        if materialized
                        else "baseline_semantic_objects_sha256"
                    ],
                    "target_reachable_object_count": (
                        transition["target_reachable_object_count"]
                        if materialized
                        else None
                    ),
                    "target_reachable_objects_sha256": (
                        transition["target_reachable_objects_sha256"]
                        if materialized
                        else None
                    ),
                    "operation_id": (
                        OPERATION_ID if phase == "prepared" else None
                    ),
                }
            )
            reseal(changed)
            return changed

        for phase in ("baseline", "materialized", "prepared"):
            with self.subTest(phase=phase, evidence="valid"):
                CONTROLLER.validate_adopted_prerequisite_target_binding(
                    for_phase(phase)
                )
            for field, replacement in (
                ("semantic_object_count", 9_999),
                (
                    "semantic_object_inventory_sha256",
                    "sha256:" + "9" * 64,
                ),
            ):
                with self.subTest(
                    phase=phase,
                    evidence="semantic",
                    field=field,
                ):
                    changed = for_phase(phase)
                    changed[
                        "production_repository_materialization"
                    ][field] = replacement
                    reseal(changed)
                    with self.assertRaisesRegex(
                        CONTROLLER.PullDeployError,
                        "semantic inventory differs from transition",
                    ):
                        CONTROLLER.validate_adopted_prerequisite_target_binding(
                            changed
                        )

        for phase in ("materialized", "prepared"):
            for field, replacement in (
                ("target_reachable_object_count", 9_999),
                (
                    "target_reachable_objects_sha256",
                    "sha256:" + "8" * 64,
                ),
            ):
                with self.subTest(
                    phase=phase,
                    evidence="closure",
                    field=field,
                ):
                    changed = for_phase(phase)
                    changed[
                        "production_repository_materialization"
                    ][field] = replacement
                    reseal(changed)
                    with self.assertRaisesRegex(
                        CONTROLLER.PullDeployError,
                        "target closure differs from transition",
                    ):
                        CONTROLLER.validate_adopted_prerequisite_target_binding(
                            changed
                        )

    def test_manual_current_v3_rejects_source_successor_lineage_deletion(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _root_raw = self._seed_git_permission_wrapper(
            controller, adopted
        )
        root, source_successor = self._seed_source_successor_wrapper(
            controller,
            adopted,
            marker,
        )
        controller._fixture_git_permission_takeover = root  # type: ignore[attr-defined]
        self._seed_unit_permission_wrapper(
            controller,
            adopted,
            marker,
            source_successor=source_successor,
        )
        source_journal_path = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_GIT_PERMISSION_SOURCE_SUCCESSOR_TRANSACTION_DIRECTORY
            / f"{source_successor['operation_id']}.json"
        )
        CONTROLLER.atomic_json(controller.current_state_path, {})
        manual_state = {
            "schema_version": CONTROLLER.CURRENT_STATE_SCHEMA_VERSION,
            "authority_kind": "manual-runtime-adoption",
            "adoption_successor_lineage": {
                "schema_version": 1,
                "source_successor_authority_sha256": source_successor[
                    "authority_file_sha256"
                ],
                "source_successor_completed_journal_sha256": (
                    CONTROLLER.sha256_file(source_journal_path)
                ),
                "unit_permission_authority_sha256": CONTROLLER.sha256_file(
                    controller.adopted_unit_permissions_path
                ),
            },
        }
        controller.adopted_git_permission_source_successor_path.unlink()
        transaction_root = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_GIT_PERMISSION_SOURCE_SUCCESSOR_TRANSACTION_DIRECTORY
        )
        for entry in transaction_root.iterdir():
            entry.unlink()
        transaction_root.rmdir()
        with mock.patch.object(
            CONTROLLER,
            "validate_current_deployment_state",
            return_value=manual_state,
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "manual adoption lineage requires the source-successor authority",
        ):
            CONTROLLER.PullDeployController._git_permission_source_successor_takeover(
                controller,
                git_permission_takeover=root,
            )

    def test_historical_manual_current_v3_allows_missing_source_successor(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _root_raw = self._seed_git_permission_wrapper(
            controller, adopted
        )
        root, _hardened = self._seed_unit_permission_wrapper(
            controller,
            adopted,
            marker,
        )
        CONTROLLER.atomic_json(controller.current_state_path, {})
        historical_state = {
            "schema_version": CONTROLLER.CURRENT_STATE_SCHEMA_VERSION,
            "authority_kind": "manual-runtime-adoption",
        }
        with mock.patch.object(
            CONTROLLER,
            "validate_current_deployment_state",
            return_value=historical_state,
        ):
            observed = CONTROLLER.PullDeployController._git_permission_source_successor_takeover(
                controller,
                git_permission_takeover=root,
            )
        self.assertIsNone(observed)

    def test_manual_current_v3_pins_unit_permission_raw_digest(self) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _root_raw = self._seed_git_permission_wrapper(
            controller, adopted
        )
        root, source_successor = self._seed_source_successor_wrapper(
            controller,
            adopted,
            marker,
        )
        self._seed_unit_permission_wrapper(
            controller,
            adopted,
            marker,
            source_successor=source_successor,
        )
        source_journal_path = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_GIT_PERMISSION_SOURCE_SUCCESSOR_TRANSACTION_DIRECTORY
            / f"{source_successor['operation_id']}.json"
        )
        CONTROLLER.atomic_json(controller.current_state_path, {})
        manual_state = {
            "schema_version": CONTROLLER.CURRENT_STATE_SCHEMA_VERSION,
            "authority_kind": "manual-runtime-adoption",
            "adoption_successor_lineage": {
                "schema_version": 1,
                "source_successor_authority_sha256": source_successor[
                    "authority_file_sha256"
                ],
                "source_successor_completed_journal_sha256": (
                    CONTROLLER.sha256_file(source_journal_path)
                ),
                "unit_permission_authority_sha256": "sha256:" + "f" * 64,
            },
        }
        with mock.patch.object(
            CONTROLLER,
            "validate_current_deployment_state",
            return_value=manual_state,
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "unit permission authority differs from current-state lineage",
        ):
            CONTROLLER.PullDeployController._unit_permission_takeover(
                controller,
                git_permission_takeover=root,
                git_permission_source_successor_takeover=source_successor,
                verify_live=False,
            )

    def test_manual_current_v3_pins_source_authority_and_journal_digests(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _root_raw = self._seed_git_permission_wrapper(
            controller, adopted
        )
        root, source_successor = self._seed_source_successor_wrapper(
            controller,
            adopted,
            marker,
        )
        controller._fixture_git_permission_takeover = root  # type: ignore[attr-defined]
        self._seed_unit_permission_wrapper(
            controller,
            adopted,
            marker,
            source_successor=source_successor,
        )
        authority_path = (
            controller.adopted_git_permission_source_successor_path
        )
        journal_path = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_GIT_PERMISSION_SOURCE_SUCCESSOR_TRANSACTION_DIRECTORY
            / f"{source_successor['operation_id']}.json"
        )
        authority = CONTROLLER.load_private_json(authority_path)
        journal = CONTROLLER.load_private_json(journal_path)
        CONTROLLER.atomic_json(controller.current_state_path, {})
        manual_state = {
            "schema_version": CONTROLLER.CURRENT_STATE_SCHEMA_VERSION,
            "authority_kind": "manual-runtime-adoption",
            "adoption_successor_lineage": {
                "schema_version": 1,
                "source_successor_authority_sha256": CONTROLLER.sha256_file(
                    authority_path
                ),
                "source_successor_completed_journal_sha256": (
                    CONTROLLER.sha256_file(journal_path)
                ),
                "unit_permission_authority_sha256": CONTROLLER.sha256_file(
                    controller.adopted_unit_permissions_path
                ),
            },
        }
        for label in ("authority", "journal"):
            with self.subTest(label=label):
                if label == "authority":
                    changed = json.loads(json.dumps(authority))
                    changed["completed_at"] = "2026-08-14T00:00:02Z"
                    CONTROLLER.atomic_json(authority_path, changed)
                    changed_journal = json.loads(json.dumps(journal))
                    changed_journal["completed_at"] = changed[
                        "completed_at"
                    ]
                    CONTROLLER.atomic_json(
                        journal_path, changed_journal
                    )
                else:
                    changed = json.loads(json.dumps(journal))
                    changed["created_at"] = "2026-08-13T23:59:59Z"
                    CONTROLLER.atomic_json(journal_path, changed)
                with mock.patch.object(
                    CONTROLLER,
                    "validate_current_deployment_state",
                    return_value=manual_state,
                ), self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "differs from current-state lineage",
                ):
                    CONTROLLER.PullDeployController._git_permission_source_successor_takeover(
                        controller,
                        git_permission_takeover=root,
                    )
                CONTROLLER.atomic_json(authority_path, authority)
                CONTROLLER.atomic_json(journal_path, journal)

    def test_historical_unit_v1_without_source_successor_remains_readable(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _root_raw = self._seed_git_permission_wrapper(
            controller, adopted
        )
        root, _hardened = self._seed_unit_permission_wrapper(
            controller,
            adopted,
            marker,
        )
        controller._fixture_git_permission_takeover = root  # type: ignore[attr-defined]
        with mock.patch.object(
            controller,
            "_git_permission_source_successor_takeover",
            return_value=None,
        ):
            unit = CONTROLLER.PullDeployController._unit_permission_takeover(
                controller,
                git_permission_takeover=root,
                verify_live=False,
            )
        self.assertIsNotNone(unit)
        self.assertNotIn("schema_version", unit)
        self.assertEqual(
            unit["git_permission_successor"]["schema_version"], 1
        )

    def test_unit_v2_consumes_completed_journal_and_aborted_inventory(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _root_raw = self._seed_git_permission_wrapper(
            controller, adopted
        )
        root, source_successor = self._seed_source_successor_wrapper(
            controller,
            adopted,
            marker,
        )
        unit_root, _hardened = self._seed_unit_permission_wrapper(
            controller,
            adopted,
            marker,
            source_successor=source_successor,
        )
        unit = (
            CONTROLLER.PullDeployController._validate_adopted_unit_permission_authority(
                controller,
                adopted=adopted,
                git_permission_takeover=unit_root,
                git_permission_source_successor_takeover=source_successor,
                verify_live=False,
            )
        )
        before_journal, before_inventory = (
            controller._unit_permission_transaction_digests(unit)
        )
        self._seed_aborted_unit_permission_journal(controller)
        observed = (
            CONTROLLER.PullDeployController._unit_permission_takeover(
                controller,
                git_permission_takeover=root,
                git_permission_source_successor_takeover=(
                    source_successor
                ),
                verify_live=False,
            )
        )
        self.assertEqual(observed, unit)
        after_journal, after_inventory = (
            controller._unit_permission_transaction_digests(unit)
        )
        self.assertEqual(after_journal, before_journal)
        self.assertNotEqual(after_inventory, before_inventory)

    def test_unit_completed_journal_semantic_drift_is_rejected(self) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _root_raw = self._seed_git_permission_wrapper(
            controller, adopted
        )
        root, source_successor = self._seed_source_successor_wrapper(
            controller,
            adopted,
            marker,
        )
        self._seed_unit_permission_wrapper(
            controller,
            adopted,
            marker,
            source_successor=source_successor,
        )
        journal_path = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_UNIT_PERMISSION_TRANSACTION_DIRECTORY
            / "adopt-unit-permission-fixture-001.json"
        )
        original = CONTROLLER.load_private_json(journal_path)
        cases = (
            ("checkpoint", ("replacement_checkpoint",), "staged"),
            ("backup", ("backup", "inventory_sha256"), "sha256:" + "0" * 64),
            ("staging", ("staging", "inode"), 1),
            ("evidence", ("unit_evidence", 0, "inode"), 1),
            ("source-trust", ("source_trust_sha256",), "sha256:" + "0" * 64),
        )
        for label, path, value in cases:
            with self.subTest(label=label):
                changed = json.loads(json.dumps(original))
                target = changed
                for component in path[:-1]:
                    target = target[component]
                target[path[-1]] = value
                CONTROLLER.atomic_json(journal_path, changed)
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "journal|transaction",
                ):
                    CONTROLLER.PullDeployController._unit_permission_takeover(
                        controller,
                        git_permission_takeover=root,
                        git_permission_source_successor_takeover=(
                            source_successor
                        ),
                        verify_live=False,
                    )
                CONTROLLER.atomic_json(journal_path, original)

    def test_unit_authority_commit_crash_requires_completed_journal(
        self,
    ) -> None:
        controller = self.controller()
        self._activate_raw_source_successor_consumer(controller)
        journal_path = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_UNIT_PERMISSION_TRANSACTION_DIRECTORY
            / "adopt-unit-permission-fixture-001.json"
        )
        completed = CONTROLLER.load_private_json(journal_path)
        interrupted = json.loads(json.dumps(completed))
        interrupted["status"] = "applying"
        interrupted["phase"] = "authority-commit-intent"
        CONTROLLER.atomic_json(journal_path, interrupted)

        for command in ("plan", "prepare"):
            with self.subTest(command=command), self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "transaction inventory is not terminal|one completed journal",
            ):
                getattr(controller, command)(
                    target_sha=TARGET_SHA,
                    operation_id=OPERATION_ID,
                )

        # This models the adopter replaying its matching commit intent and
        # durably publishing the completed terminal journal.
        CONTROLLER.atomic_json(journal_path, completed)
        self.assertEqual(
            controller.plan(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )["target_sha"],
            TARGET_SHA,
        )

    def test_unit_transaction_snapshot_rereads_same_name_digest(self) -> None:
        controller = self.controller()
        self._activate_raw_source_successor_consumer(controller)
        unit = controller._fixture_unit_permission_takeover  # type: ignore[attr-defined]
        journal_path = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_UNIT_PERMISSION_TRANSACTION_DIRECTORY
            / "adopt-unit-permission-fixture-001.json"
        )
        original = CONTROLLER.load_private_json(journal_path)
        private_read = controller._private_json_with_digest
        journal_reads = 0

        def replace_after_first_read(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal journal_reads
            result = private_read(*args, **kwargs)
            if Path(args[0]) == journal_path:
                journal_reads += 1
                if journal_reads == 1:
                    changed = json.loads(json.dumps(original))
                    changed["created_at"] = "2026-08-13T23:59:58Z"
                    CONTROLLER.atomic_json(journal_path, changed)
            return result

        with mock.patch.object(
            controller,
            "_private_json_with_digest",
            side_effect=replace_after_first_read,
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "transaction changed while validating",
        ):
            CONTROLLER.PullDeployController._unit_permission_transaction_snapshot(
                controller,
                unit,
            )

    def test_unit_transaction_inventory_is_pinned_by_current_lineage(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _root_raw = self._seed_git_permission_wrapper(
            controller, adopted
        )
        root, source_successor = self._seed_source_successor_wrapper(
            controller,
            adopted,
            marker,
        )
        unit_root, _hardened = self._seed_unit_permission_wrapper(
            controller,
            adopted,
            marker,
            source_successor=source_successor,
        )
        unit = (
            CONTROLLER.PullDeployController._validate_adopted_unit_permission_authority(
                controller,
                adopted=adopted,
                git_permission_takeover=unit_root,
                git_permission_source_successor_takeover=source_successor,
                verify_live=False,
            )
        )
        aborted_path = self._seed_aborted_unit_permission_journal(controller)
        unit_journal, unit_inventory = (
            controller._unit_permission_transaction_digests(unit)
        )
        source_journal = (
            controller._source_successor_completed_journal_digest(
                source_successor
            )
        )
        manual_state = {
            "schema_version": CONTROLLER.CURRENT_STATE_SCHEMA_VERSION,
            "authority_kind": "manual-runtime-adoption",
            "adoption_successor_lineage": {
                "schema_version": 2,
                "source_successor_authority_sha256": source_successor[
                    "authority_file_sha256"
                ],
                "source_successor_completed_journal_sha256": (
                    source_journal
                ),
                "unit_permission_authority_sha256": unit[
                    "authority_file_sha256"
                ],
                "unit_permission_completed_journal_sha256": unit_journal,
                "unit_permission_transaction_inventory_sha256": (
                    unit_inventory
                ),
            },
        }
        changed = CONTROLLER.load_private_json(aborted_path)
        changed["aborted_at"] = "2026-08-13T23:59:59Z"
        CONTROLLER.atomic_json(aborted_path, changed)
        CONTROLLER.atomic_json(controller.current_state_path, {})
        with mock.patch.object(
            CONTROLLER,
            "validate_current_deployment_state",
            return_value=manual_state,
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "current-state lineage",
        ):
            CONTROLLER.PullDeployController._unit_permission_takeover(
                controller,
                git_permission_takeover=root,
                git_permission_source_successor_takeover=source_successor,
                verify_live=False,
            )

    def test_unit_transaction_cas_is_required_by_plan_and_prepare(self) -> None:
        controller = self.controller()
        self._activate_raw_source_successor_consumer(controller)
        for command in ("plan", "prepare"):
            with self.subTest(command=command):
                with mock.patch.object(
                    controller,
                    "_unit_permission_transaction_digests",
                    side_effect=CONTROLLER.PullDeployError(
                        "injected unit transaction CAS drift"
                    ),
                ), self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "transaction CAS drift",
                ):
                    getattr(controller, command)(
                        target_sha=TARGET_SHA,
                        operation_id=OPERATION_ID,
                    )

    def test_historical_outer_v3_without_source_successor_remains_readable(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(
            controller.prepared_root / OPERATION_ID / "descriptor.json"
        )
        historical = json.loads(json.dumps(descriptor))
        binding = historical["adopted_prerequisite_target_binding"]
        binding["schema_version"] = 3
        binding.pop("git_permission_source_successor_authority")
        binding.pop("production_repository_materialization")
        binding.pop("source_successor_completed_journal_sha256")
        binding.pop("unit_permission_completed_journal_sha256")
        binding.pop("unit_permission_transaction_inventory_sha256")
        binding.pop("production_git_snapshot_authority_sha256")
        binding.pop("bootstrap_router_intent_sha256")
        binding.pop("bootstrap_router_authority_sha256")
        binding["unit_permission_authority"] = (
            adopted_unit_permission_binding_fixture(
                controller._fixture_git_permission_takeover,  # type: ignore[attr-defined]
                source_sha=TARGET_SHA,
                source_tree=TARGET_TREE,
                adopted_deployment_sha256=binding["authority"][
                    "adopted_deployment_sha256"
                ],
                file_digests={
                    path: CONTROLLER.sha256_bytes(
                        controller._git_show(TARGET_SHA, path)
                    )
                    for path in (
                        CONTROLLER.ADOPTED_UNIT_PERMISSION_SUCCESSOR_FILES
                    )
                },
            )
        )
        binding["identity_sha256"] = CONTROLLER.canonical_json_digest(
            {
                key: value
                for key, value in binding.items()
                if key != "identity_sha256"
            }
        )
        observed = CONTROLLER.validate_descriptor(historical)
        self.assertEqual(
            observed["adopted_prerequisite_target_binding"][
                "schema_version"
            ],
            3,
        )
        self.assertEqual(
            observed["adopted_prerequisite_target_binding"][
                "unit_permission_authority"
            ]["git_permission_successor"]["schema_version"],
            1,
        )

    def test_source_successor_full_ci_gate_drift_is_rejected_by_plan(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        original_source = json.loads(
            json.dumps(
                controller._fixture_git_permission_source_successor_takeover  # type: ignore[attr-defined]
            )
        )
        original_unit = json.loads(
            json.dumps(controller._fixture_unit_permission_takeover)  # type: ignore[attr-defined]
        )
        cases = (
            ("run-id", lambda ci: ci.__setitem__("workflow_run_id", 43)),
            ("attempt", lambda ci: ci.__setitem__("run_attempt", 2)),
            (
                "required-jobs",
                lambda ci: ci.__setitem__(
                    "required_jobs", ci["required_jobs"][:-1]
                ),
            ),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                changed = json.loads(json.dumps(original_source))
                mutate(changed["delivery_gate"]["ci"])
                changed["delivery_gate_sha256"] = (
                    CONTROLLER.canonical_json_digest(
                        changed["delivery_gate"]
                    )
                )
                changed["identity_sha256"] = CONTROLLER.canonical_json_digest(
                    {
                        key: value
                        for key, value in changed.items()
                        if key != "identity_sha256"
                    }
                )
                controller._fixture_git_permission_source_successor_takeover = changed  # type: ignore[attr-defined]
                controller._fixture_unit_permission_takeover = json.loads(  # type: ignore[attr-defined]
                    json.dumps(original_unit)
                )
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "target CI differs",
                ):
                    controller.plan(
                        target_sha=TARGET_SHA,
                        operation_id=OPERATION_ID,
                    )
        controller._fixture_git_permission_source_successor_takeover = original_source  # type: ignore[attr-defined]

    def test_source_successor_production_trust_drift_is_rejected_by_plan(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        changed = json.loads(
            json.dumps(
                controller._fixture_git_permission_source_successor_takeover  # type: ignore[attr-defined]
            )
        )
        changed["source_trust_sha256"] = "sha256:" + "f" * 64
        changed["identity_sha256"] = CONTROLLER.canonical_json_digest(
            {
                key: value
                for key, value in changed.items()
                if key != "identity_sha256"
            }
        )
        controller._fixture_git_permission_source_successor_takeover = changed  # type: ignore[attr-defined]
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "production source trust differs",
        ):
            controller.plan(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )

    def test_source_successor_production_trust_drift_is_rejected_by_prepare(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        changed = json.loads(
            json.dumps(
                controller._fixture_git_permission_source_successor_takeover  # type: ignore[attr-defined]
            )
        )
        changed["source_trust_sha256"] = "sha256:" + "f" * 64
        changed["identity_sha256"] = CONTROLLER.canonical_json_digest(
            {
                key: value
                for key, value in changed.items()
                if key != "identity_sha256"
            }
        )
        controller._fixture_git_permission_source_successor_takeover = changed  # type: ignore[attr-defined]
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "production source trust differs",
        ):
            controller.prepare(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )

    def test_source_successor_production_trust_drift_is_rejected_pre_switch(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(
            controller.prepared_root / OPERATION_ID / "descriptor.json"
        )
        changed = json.loads(
            json.dumps(
                controller._fixture_git_permission_source_successor_takeover  # type: ignore[attr-defined]
            )
        )
        changed["source_trust_sha256"] = "sha256:" + "f" * 64
        changed["identity_sha256"] = CONTROLLER.canonical_json_digest(
            {
                key: value
                for key, value in changed.items()
                if key != "identity_sha256"
            }
        )
        controller._fixture_git_permission_source_successor_takeover = changed  # type: ignore[attr-defined]
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "production source trust differs",
        ):
            CONTROLLER.PullDeployController._revalidate_pre_switch(
                controller,
                descriptor,
            )

    def test_adopted_unit_authority_publication_contract_tamper_is_rejected(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _git_authority = self._seed_git_permission_wrapper(
            controller,
            adopted,
        )
        git_combined, _hardened_units = self._seed_unit_permission_wrapper(
            controller,
            adopted,
            marker,
        )
        original = CONTROLLER.load_private_json(
            controller.adopted_unit_permissions_path
        )
        original_entries = original["plan"]["authority_publication"][
            "entries"
        ]
        cases = (
            ("schema-type", ("schema_version",), True),
            ("policy", ("policy",), "foreign-publication-policy"),
            ("directory", ("directory",), str(controller.runtime_root)),
            ("final-role", ("entries", 0, "role"), "staging"),
            ("final-name", ("entries", 0, "name"), "foreign.json"),
            (
                "staging-operation",
                ("entries", 1, "name"),
                ".adopted-unit-permissions.json.create-adopt-unit-permission-foreign-001",
            ),
            (
                "quarantine-path",
                ("entries", 2, "path"),
                str(controller.state_dir / ".foreign.quarantine"),
            ),
            ("absence-type", ("entries", 0, "initially_absent"), 1),
            (
                "entry-order",
                ("entries",),
                list(reversed(original_entries)),
            ),
        )

        for label, path, value in cases:
            with self.subTest(label=label):
                authority = json.loads(json.dumps(original))
                target = authority["plan"]["authority_publication"]
                for component in path[:-1]:
                    target = target[component]
                target[path[-1]] = value
                authority["plan_sha256"] = (
                    CONTROLLER.canonical_json_digest(authority["plan"])
                )
                CONTROLLER.atomic_json(
                    controller.adopted_unit_permissions_path,
                    authority,
                )
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "authority publication differs",
                ):
                    CONTROLLER.PullDeployController._validate_adopted_unit_permission_authority(
                        controller,
                        adopted=adopted,
                        git_permission_takeover=git_combined,
                        verify_live=False,
                    )

        CONTROLLER.atomic_json(
            controller.adopted_unit_permissions_path,
            original,
        )
        observed = (
            CONTROLLER.PullDeployController._validate_adopted_unit_permission_authority(
                controller,
                adopted=adopted,
                git_permission_takeover=git_combined,
                verify_live=False,
            )
        )
        self.assertEqual(observed["operation_id"], original["operation_id"])

    def test_adopted_unit_permission_impact_must_bind_publication(self) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _git_authority = self._seed_git_permission_wrapper(
            controller,
            adopted,
        )
        git_combined, _hardened_units = self._seed_unit_permission_wrapper(
            controller,
            adopted,
            marker,
        )
        authority = CONTROLLER.load_private_json(
            controller.adopted_unit_permissions_path
        )
        legacy_impact = {
            "schema_version": 1,
            "policy": "nexpoly-adopted-unit-permission-hardening-v1",
            "units": authority["plan"]["units"],
        }
        legacy_digest = CONTROLLER.canonical_json_digest(legacy_impact)
        authority["unit_permission_impact_sha256"] = legacy_digest
        authority["plan"]["unit_permission_impact_sha256"] = legacy_digest
        authority["plan_sha256"] = CONTROLLER.canonical_json_digest(
            authority["plan"]
        )
        CONTROLLER.atomic_json(
            controller.adopted_unit_permissions_path,
            authority,
        )

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "evidence differs from predecessors",
        ):
            CONTROLLER.PullDeployController._validate_adopted_unit_permission_authority(
                controller,
                adopted=adopted,
                git_permission_takeover=git_combined,
                verify_live=False,
            )

    def test_adopted_unit_permission_backup_inventory_drift_is_rejected(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _git_authority = self._seed_git_permission_wrapper(
            controller, adopted
        )
        git_combined, _hardened_units = (
            self._seed_unit_permission_wrapper(
                controller,
                adopted,
                marker,
            )
        )
        observed = (
            CONTROLLER.PullDeployController._validate_adopted_unit_permission_authority(
                controller,
                adopted=adopted,
                git_permission_takeover=git_combined,
                verify_live=False,
            )
        )
        self.assertEqual(
            observed["backup_sha256"],
            CONTROLLER.load_private_json(
                controller.adopted_unit_permissions_path
            )["backup_sha256"],
        )
        authority = CONTROLLER.load_private_json(
            controller.adopted_unit_permissions_path
        )
        authority["backup"]["inventory_sha256"] = "sha256:" + "7" * 64
        authority["backup_sha256"] = CONTROLLER.canonical_json_digest(
            authority["backup"]
        )
        CONTROLLER.atomic_json(
            controller.adopted_unit_permissions_path,
            authority,
        )

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "backup inventory differs",
        ):
            CONTROLLER.PullDeployController._validate_adopted_unit_permission_authority(
                controller,
                adopted=adopted,
                git_permission_takeover=git_combined,
                verify_live=False,
            )

    def test_adopted_unit_parent_growth_is_transition_only_and_live_sealed(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _git_authority = self._seed_git_permission_wrapper(
            controller, adopted
        )
        git_combined, hardened_units = self._seed_unit_permission_wrapper(
            controller,
            adopted,
            marker,
        )
        parent = Path(hardened_units[0]["path"]).parent
        before, after = self._grow_directory_size(
            parent,
            label="unit-authority",
        )
        authority = CONTROLLER.load_private_json(
            controller.adopted_unit_permissions_path
        )
        self.assertEqual(
            authority["original_units"][1]["parent"]["size"],
            before,
        )
        for record in authority["hardened_units"]:
            record["parent"]["size"] = after
        authority["hardened_units_sha256"] = (
            CONTROLLER.canonical_json_digest(authority["hardened_units"])
        )
        CONTROLLER.atomic_json(
            controller.adopted_unit_permissions_path,
            authority,
        )
        controller.runner = AdoptedUnitRunner(authority["hardened_units"])

        observed = (
            CONTROLLER.PullDeployController._validate_adopted_unit_permission_authority(
                controller,
                adopted=adopted,
                git_permission_takeover=git_combined,
                verify_live=True,
            )
        )
        self.assertEqual(
            observed["hardened_units_sha256"],
            authority["hardened_units_sha256"],
        )

        sealed_size, drifted_size = self._grow_directory_size(
            parent,
            label="unit-live-drift",
        )
        self.assertEqual(sealed_size, after)
        self.assertGreater(drifted_size, sealed_size)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "unit parent differs",
        ):
            CONTROLLER.PullDeployController._validate_adopted_unit_permission_authority(
                controller,
                adopted=adopted,
                git_permission_takeover=git_combined,
                verify_live=True,
            )

    def test_production_git_permission_takeover_rejects_missing_marker(
        self,
    ) -> None:
        controller = self.controller()
        controller.test_root_mode = False
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "permission takeover is unavailable",
        ):
            CONTROLLER.PullDeployController._git_permission_takeover(
                controller
            )

    def test_manual_adoption_rejects_raw_permission_marker_without_wrapper(
        self,
    ) -> None:
        controller = self.controller()
        self._seed_adopted_authority(controller)
        marker = git_permission_marker_fixture(
            controller.production_root,
            controller.git_permission_marker_path,
            phase="hardened",
        )
        CONTROLLER.atomic_json(controller.git_permission_marker_path, marker)
        marker_digest = CONTROLLER.sha256_file(
            controller.git_permission_marker_path
        )
        controller.test_root_mode = False
        with mock.patch.object(
            controller,
            "_verified_raw_git_permission_takeover",
            return_value=(marker, marker_digest),
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "adopted prerequisite lineage requires",
        ):
            CONTROLLER.PullDeployController._git_permission_takeover(
                controller
            )

    def test_adopted_git_permission_wrapper_digest_drift_is_rejected(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, authority = self._seed_git_permission_wrapper(
            controller, adopted
        )
        authority["permission_marker_sha256"] = "sha256:" + "f" * 64
        CONTROLLER.atomic_json(
            controller.adopted_git_permissions_path, authority
        )
        marker_digest = CONTROLLER.sha256_file(
            controller.git_permission_marker_path
        )
        with mock.patch.object(
            controller,
            "_verified_raw_git_permission_takeover",
            return_value=(marker, marker_digest),
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "marker differs from wrapper",
        ):
            CONTROLLER.PullDeployController._git_permission_takeover(
                controller
            )

    def test_adopted_git_permission_wrapper_cannot_downgrade_to_legacy(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _authority = self._seed_git_permission_wrapper(
            controller, adopted
        )
        bootstrap_path = controller.state_dir / "bootstrap-control.json"
        bootstrap = CONTROLLER.load_private_json(bootstrap_path)
        bootstrap["schema_version"] = 2
        bootstrap.pop("authority_kind", None)
        bootstrap.pop("adopted_deployment", None)
        bootstrap.pop("adopted_deployment_sha256", None)
        bootstrap.pop("adoption_evidence_sha256", None)
        bootstrap.pop("active_control", None)
        CONTROLLER.atomic_json(bootstrap_path, bootstrap)
        controller.adopted_state_path.unlink()
        marker_digest = CONTROLLER.sha256_file(
            controller.git_permission_marker_path
        )

        with mock.patch.object(
            controller,
            "_verified_raw_git_permission_takeover",
            return_value=(marker, marker_digest),
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lost its adoption binding",
        ):
            CONTROLLER.PullDeployController._git_permission_takeover(
                controller
            )

    def test_manual_current_lineage_rejects_complete_authority_removal(
        self,
    ) -> None:
        controller = self.controller()
        controller.test_root_mode = False
        CONTROLLER.atomic_json(controller.current_state_path, {})
        manual_state = {
            "schema_version": CONTROLLER.CURRENT_STATE_SCHEMA_VERSION,
            "authority_kind": "manual-runtime-adoption",
        }

        with mock.patch.object(
            CONTROLLER,
            "validate_current_deployment_state",
            return_value=manual_state,
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "manual adoption lineage requires",
        ):
            CONTROLLER.PullDeployController._git_permission_takeover(
                controller
            )

    def test_manual_prerequisite_lineage_rejects_first_deploy_downgrade(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _authority = self._seed_git_permission_wrapper(
            controller, adopted
        )
        controller.adopted_git_permissions_path.unlink()
        controller.adopted_state_path.unlink()
        (controller.state_dir / "bootstrap-control.json").unlink()
        self.assertFalse(controller.current_state_path.exists())
        marker_digest = CONTROLLER.sha256_file(
            controller.git_permission_marker_path
        )

        with mock.patch.object(
            controller,
            "_verified_raw_git_permission_takeover",
            return_value=(marker, marker_digest),
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "adopted prerequisite lineage requires",
        ):
            CONTROLLER.PullDeployController._git_permission_takeover(
                controller
            )

    def test_legacy_raw_permission_marker_without_adoption_residue_is_valid(
        self,
    ) -> None:
        controller = self.controller()
        controller.test_root_mode = False
        marker = git_permission_marker_fixture(
            controller.production_root,
            controller.git_permission_marker_path,
            phase="hardened",
        )
        CONTROLLER.atomic_json(controller.git_permission_marker_path, marker)
        marker_digest = CONTROLLER.sha256_file(
            controller.git_permission_marker_path
        )

        with mock.patch.object(
            controller,
            "_verified_raw_git_permission_takeover",
            return_value=(marker, marker_digest),
        ):
            observed = (
                CONTROLLER.PullDeployController._git_permission_takeover(
                    controller
                )
            )
        self.assertEqual(observed, marker)

    def test_adopted_git_permission_wrapper_happy_path_is_sealed_in_plan(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        marker, _authority = self._seed_git_permission_wrapper(
            controller, adopted
        )
        marker_digest = CONTROLLER.sha256_file(
            controller.git_permission_marker_path
        )
        with mock.patch.object(
            controller,
            "_verified_raw_git_permission_takeover",
            return_value=(marker, marker_digest),
        ):
            combined = (
                CONTROLLER.PullDeployController._git_permission_takeover(
                    controller
                )
            )
        self.assertEqual(
            combined["authority_kind"],
            CONTROLLER.ADOPTED_GIT_PERMISSION_AUTHORITY_KIND,
        )
        self.assertEqual(
            combined["authority"]["permission_marker_sha256"],
            marker_digest,
        )
        controller._fixture_git_permission_takeover = combined  # type: ignore[attr-defined]
        trust = {
            "schema_version": 1,
            "evidence_sha256": "sha256:" + "9" * 64,
        }

        def repository_git(*arguments: str, **_kwargs: object):
            output = {
                ("symbolic-ref", "--short", "HEAD"): "main\n",
                (
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                ): "",
                ("ls-files", "-z", "--cached"): "",
                ("remote", "get-url", "origin"): (
                    CONTROLLER.REPOSITORY_HTTPS_URL + "\n"
                ),
                ("rev-parse", "HEAD"): PREVIOUS_SHA + "\n",
                ("rev-parse", "HEAD^{tree}"): PREVIOUS_TREE + "\n",
            }[arguments]
            return subprocess.CompletedProcess([], 0, output, "")

        with mock.patch.object(
            controller, "_git_trust_preflight", return_value={"fixture": True}
        ), mock.patch.object(
            controller, "_git", side_effect=repository_git
        ), mock.patch.object(
            controller,
            "_clean_environment",
            return_value={"GIT_SSH_COMMAND": "fixture-ssh"},
        ), mock.patch.object(
            CONTROLLER._git_source_trust,
            "repository_trust_evidence",
            return_value=trust,
        ), mock.patch.object(
            CONTROLLER._git_source_trust,
            "require_stable_trust_surface",
            return_value=None,
        ):
            repository = (
                CONTROLLER.PullDeployController.repository_identity(
                    controller
                )
            )
        self.assertEqual(repository["permission_takeover"], combined)
        plan = controller.plan(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        binding = plan["adopted_prerequisite_target_binding"]
        self.assertEqual(binding["schema_version"], 5)
        self.assertEqual(
            binding["git_permission_authority"]["authority_file_sha256"],
            combined["authority_file_sha256"],
        )
        self.assertEqual(
            binding["unit_permission_authority"][
                "adopted_git_permissions_sha256"
            ],
            combined["authority_file_sha256"],
        )
        self.assertEqual(
            binding["production_git_snapshot_authority_sha256"],
            binding["git_permission_source_successor_authority"][
                "snapshot_authority_sha256"
            ],
        )
        self.assertEqual(
            binding["bootstrap_router_intent_sha256"],
            "sha256:" + "6" * 64,
        )
        self.assertEqual(
            binding["bootstrap_router_authority_sha256"],
            "sha256:" + "7" * 64,
        )

    def test_adopted_git_permission_descriptor_replay_rejects_wrapper_drift(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(
            controller.prepared_root / OPERATION_ID / "descriptor.json"
        )
        changed = json.loads(
            json.dumps(controller._fixture_git_permission_takeover)  # type: ignore[attr-defined]
        )
        changed["authority_file_sha256"] = "sha256:" + "f" * 64
        changed["identity_sha256"] = CONTROLLER.canonical_json_digest(
            {
                key: value
                for key, value in changed.items()
                if key != "identity_sha256"
            }
        )
        controller._fixture_git_permission_takeover = changed  # type: ignore[attr-defined]
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "target binding changed after prepare",
        ):
            controller._revalidate_adopted_prerequisite_target_binding(
                descriptor
            )

    def test_adopted_git_permission_wrapper_hardlink_is_rejected(self) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _authority = self._seed_git_permission_wrapper(
            controller, adopted
        )
        os.link(
            controller.adopted_git_permissions_path,
            controller.state_dir / "adopted-git-permissions-hardlink.json",
        )
        marker_digest = CONTROLLER.sha256_file(
            controller.git_permission_marker_path
        )
        with mock.patch.object(
            controller,
            "_verified_raw_git_permission_takeover",
            return_value=(marker, marker_digest),
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "private input is unsafe",
        ):
            CONTROLLER.PullDeployController._git_permission_takeover(
                controller
            )

    def test_adopted_git_permission_marker_toctou_is_rejected(self) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _authority = self._seed_git_permission_wrapper(
            controller, adopted
        )
        marker_digest = CONTROLLER.sha256_file(
            controller.git_permission_marker_path
        )
        changed = json.loads(json.dumps(marker))
        changed["generation"] = int(changed["generation"]) + 1
        changed["evidence_sha256"] = (
            CONTROLLER._git_source_trust._permission_document_digest(
                changed
            )
        )
        with mock.patch.object(
            controller,
            "_verified_raw_git_permission_takeover",
            side_effect=[
                (marker, marker_digest),
                (changed, "sha256:" + "f" * 64),
            ],
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "changed while validating",
        ):
            CONTROLLER.PullDeployController._git_permission_takeover(
                controller
            )

    def test_adopted_git_permission_wrapper_toctou_is_rejected(self) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _authority = self._seed_git_permission_wrapper(
            controller, adopted
        )
        marker_digest = CONTROLLER.sha256_file(
            controller.git_permission_marker_path
        )
        validate = controller._validate_adopted_git_permission_authority

        def validate_then_replace(**arguments):  # type: ignore[no-untyped-def]
            combined = validate(**arguments)
            replacement = CONTROLLER.load_private_json(
                controller.adopted_git_permissions_path
            )
            replacement["completed_at"] = "2026-08-14T00:00:01Z"
            CONTROLLER.atomic_json(
                controller.adopted_git_permissions_path, replacement
            )
            return combined

        with mock.patch.object(
            controller,
            "_verified_raw_git_permission_takeover",
            return_value=(marker, marker_digest),
        ), mock.patch.object(
            controller,
            "_validate_adopted_git_permission_authority",
            side_effect=validate_then_replace,
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "changed while validating",
        ):
            CONTROLLER.PullDeployController._git_permission_takeover(
                controller
            )

    def test_adopted_git_permission_base_authority_toctou_is_rejected(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        marker, _authority = self._seed_git_permission_wrapper(
            controller, adopted
        )
        marker_digest = CONTROLLER.sha256_file(
            controller.git_permission_marker_path
        )
        validate = controller._validate_adopted_git_permission_authority
        prerequisite_path = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_PREREQUISITES_RELATIVE_PATH
        )
        calls = 0

        def validate_then_replace(**arguments):  # type: ignore[no-untyped-def]
            nonlocal calls
            combined = validate(**arguments)
            if calls == 0:
                replacement = CONTROLLER.load_private_json(
                    prerequisite_path
                )
                replacement["completed_at"] = "2026-08-14T00:00:01Z"
                CONTROLLER.atomic_json(prerequisite_path, replacement)
            calls += 1
            return combined

        with mock.patch.object(
            controller,
            "_verified_raw_git_permission_takeover",
            return_value=(marker, marker_digest),
        ), mock.patch.object(
            controller,
            "_validate_adopted_git_permission_authority",
            side_effect=validate_then_replace,
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "permission plan authority differs",
        ):
            CONTROLLER.PullDeployController._git_permission_takeover(
                controller
            )

    def test_adopted_legacy_image_values_are_exact_inert_and_stripped(self) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        deploy_env = controller.config_dir / "deploy.env"
        original = deploy_env.read_text(encoding="utf-8")
        legacy = {
            "NEXPOLY_BACKEND_IMAGE": adopted["images"]["backend"][
                "digest_ref"
            ],
            "NEXPOLY_WEB_IMAGE": adopted["images"]["web"]["digest_ref"],
        }
        write_private(
            deploy_env,
            original
            + "".join(f"{key}={value}\n" for key, value in legacy.items()),
        )

        values = CONTROLLER.PullDeployController.production_deploy_values(
            controller, check_free_space=False
        )
        self.assertNotIn("NEXPOLY_BACKEND_IMAGE", values)
        self.assertNotIn("NEXPOLY_WEB_IMAGE", values)
        target_descriptor = {
            "images": {
                "backend": image_record("backend", TARGET_SHA),
                "web": image_record("web", TARGET_SHA),
            }
        }
        with mock.patch.object(
            controller,
            "production_deploy_values",
            side_effect=lambda **kwargs: (
                CONTROLLER.PullDeployController.production_deploy_values(
                    controller, **kwargs
                )
            ),
        ):
            environment = CONTROLLER.SystemLifecycle()._environment(
                controller, target_descriptor
            )
        self.assertEqual(
            environment["NEXPOLY_BACKEND_IMAGE"],
            target_descriptor["images"]["backend"]["digest_ref"],
        )
        self.assertEqual(
            environment["NEXPOLY_WEB_IMAGE"],
            target_descriptor["images"]["web"]["digest_ref"],
        )

        for key in legacy:
            lines = deploy_env.read_text(encoding="utf-8").splitlines()
            changed = [
                f"{key}=ghcr.io/example/drift@sha256:{'f' * 64}"
                if line.startswith(f"{key}=")
                else line
                for line in lines
            ]
            write_private(deploy_env, "\n".join(changed) + "\n")
            with self.subTest(key=key), self.assertRaisesRegex(
                CONTROLLER.PullDeployError, "differ from manual adoption"
            ):
                CONTROLLER.PullDeployController.production_deploy_values(
                    controller, check_free_space=False
                )
            write_private(
                deploy_env,
                original
                + "".join(
                    f"{name}={value}\n" for name, value in legacy.items()
                ),
            )

    def test_legacy_image_values_without_adoption_remain_forbidden(self) -> None:
        controller = self.controller()
        deploy_env = controller.config_dir / "deploy.env"
        write_private(
            deploy_env,
            deploy_env.read_text(encoding="utf-8")
            + f"NEXPOLY_BACKEND_IMAGE=ghcr.io/example/backend@{DIGEST_A}\n"
            + f"NEXPOLY_WEB_IMAGE=ghcr.io/example/web@{DIGEST_B}\n",
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "complete manual adoption"
        ):
            CONTROLLER.PullDeployController.production_deploy_values(
                controller, check_free_space=False
            )

    def test_first_governed_deploy_uses_adopted_runtime_as_previous_authority(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller.active_control_evidence = (  # type: ignore[method-assign]
            lambda: CONTROLLER._control_runtime.validate_active_control_record(
                CONTROLLER.load_private_json(controller.active_control_path)
            )
        )
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )

        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        self.assertIsNone(descriptor["previous_deployment"])
        self.assertEqual(descriptor["adopted_deployment"], adopted)
        self.assertEqual(
            descriptor["adopted_deployment_sha256"],
            CONTROLLER.canonical_json_digest(adopted),
        )
        state = controller.apply(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        self.assertEqual(state["authority_kind"], "manual-runtime-adoption")
        self.assertEqual(state["adoption_evidence"], adopted["adoption_evidence"])
        self.assertEqual(
            state["adoption_evidence_sha256"],
            adopted["adoption_evidence_sha256"],
        )
        self.assertEqual(
            state["adopted_deployment_sha256"],
            CONTROLLER.canonical_json_digest(adopted),
        )
        successor_binding = descriptor[
            "adopted_prerequisite_target_binding"
        ]
        successor_lineage = state["adoption_successor_lineage"]
        self.assertEqual(successor_lineage["schema_version"], 3)
        self.assertEqual(
            successor_lineage["source_successor_authority_sha256"],
            successor_binding[
                "git_permission_source_successor_authority"
            ]["authority_file_sha256"],
        )
        self.assertEqual(
            successor_lineage["unit_permission_authority_sha256"],
            successor_binding["unit_permission_authority"][
                "authority_file_sha256"
            ],
        )
        for field in (
            "production_git_snapshot_authority_sha256",
            "bootstrap_router_intent_sha256",
            "bootstrap_router_authority_sha256",
        ):
            self.assertEqual(
                successor_lineage[field], successor_binding[field]
            )
        self.assertEqual(
            successor_lineage[
                "unit_permission_completed_journal_sha256"
            ],
            successor_binding[
                "unit_permission_completed_journal_sha256"
            ],
        )
        self.assertEqual(
            successor_lineage[
                "unit_permission_transaction_inventory_sha256"
            ],
            successor_binding[
                "unit_permission_transaction_inventory_sha256"
            ],
        )
        historical_previous = json.loads(json.dumps(state))
        historical_previous["adoption_successor_lineage"] = {
            key: value
            for key, value in successor_lineage.items()
            if key
            not in {
                "unit_permission_completed_journal_sha256",
                "unit_permission_transaction_inventory_sha256",
                "production_git_snapshot_authority_sha256",
                "bootstrap_router_intent_sha256",
                "bootstrap_router_authority_sha256",
            }
        }
        historical_previous["adoption_successor_lineage"][
            "schema_version"
        ] = 1
        upgrade_descriptor = json.loads(json.dumps(descriptor))
        upgrade_descriptor["previous_deployment"] = historical_previous
        CONTROLLER.validate_current_state_adoption_lineage(
            state,
            descriptor=upgrade_descriptor,
        )
        stale_candidate = json.loads(json.dumps(state))
        stale_candidate["adoption_successor_lineage"] = (
            historical_previous["adoption_successor_lineage"]
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "successor lineage differs from previous state",
        ):
            CONTROLLER.validate_current_state_adoption_lineage(
                stale_candidate,
                descriptor=upgrade_descriptor,
            )
        historical_state = json.loads(json.dumps(state))
        historical_state.pop("adoption_successor_lineage")
        CONTROLLER.validate_current_deployment_state(historical_state)
        malformed_lineage = json.loads(json.dumps(state))
        malformed_lineage["adoption_successor_lineage"].pop(
            "source_successor_completed_journal_sha256"
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "successor lineage has an invalid shape",
        ):
            CONTROLLER.validate_current_deployment_state(malformed_lineage)
        self.assertEqual(
            state["postgres_rehearsal"]["operation_id"], OPERATION_ID
        )
        self.assertEqual(
            state["postgres_rehearsal"]["target_sha"], TARGET_SHA
        )
        CONTROLLER.validate_current_deployment_state(state)

    def test_target_read_only_plan_accepts_raw_adopted_authority(self) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller.active_control_evidence = (  # type: ignore[method-assign]
            lambda: CONTROLLER._control_runtime.validate_active_control_record(
                CONTROLLER.load_private_json(controller.active_control_path)
            )
        )
        adopted_runtime_checks: list[dict[str, object]] = []
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: adopted_runtime_checks.append(observed)
        )

        def inventory(root: Path) -> tuple[tuple[object, ...], ...]:
            records: list[tuple[object, ...]] = []
            for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
                metadata = path.lstat()
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    payload = ("link", os.readlink(path))
                elif path.is_dir():
                    payload = ("directory", None)
                else:
                    payload = ("file", CONTROLLER.sha256_file(path))
                records.append(
                    (
                        relative,
                        metadata.st_mode,
                        metadata.st_uid,
                        metadata.st_gid,
                        metadata.st_size,
                        metadata.st_mtime_ns,
                        metadata.st_ctime_ns,
                        payload,
                    )
                )
            return tuple(records)

        production_before = inventory(controller.production_root)
        runtime_before = inventory(controller.runtime_root)

        plan = controller.plan(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )

        self.assertEqual(inventory(controller.production_root), production_before)
        self.assertEqual(inventory(controller.runtime_root), runtime_before)
        self.assertFalse(plan["apply"])
        self.assertFalse(plan["service_mutation"])
        self.assertEqual(plan["authority_kind"], "manual-runtime-adoption")
        self.assertEqual(
            plan["adopted_deployment_sha256"],
            CONTROLLER.canonical_json_digest(adopted),
        )
        self.assertEqual(
            plan["active_control"], adopted["active_control"]
        )
        self.assertEqual(adopted_runtime_checks, [adopted])

        mismatched_prerequisites = {
            **CONTROLLER.load_private_json(
                controller.runtime_root
                / CONTROLLER.ADOPTED_PREREQUISITES_RELATIVE_PATH
            ),
            "source_sha": "5" * 40,
        }
        with mock.patch.object(
            controller,
            "_validate_adopted_prerequisite_provenance",
            return_value=mismatched_prerequisites,
        ), mock.patch.object(
            controller,
            "prerequisite_is_ancestor",
            False,
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "not an ancestor of the final target",
        ):
            controller.plan(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )

        with mock.patch.object(
            controller,
            "_active_slot",
            return_value={
                **adopted["monomer_md"]["active_slot"],
                "operation_id": "another-adopted-slot-operation",
            },
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "slot differs from adopted deployment authority",
        ):
            controller.plan(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )

        with mock.patch.object(
            controller,
            "active_control_evidence",
            return_value={
                **adopted["active_control"],
                "generation": adopted["active_control"]["generation"] + 1,
            },
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "control authority differs from adopted deployment authority",
        ):
            controller.plan(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )

        with mock.patch.object(
            controller,
            "repository_identity",
            return_value={
                "sha": "6" * 40,
                "tree": PREVIOUS_TREE,
                "origin": CONTROLLER.REPOSITORY_SSH_URL,
            },
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "source identity differs from adopted deployment authority",
        ):
            controller.plan(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )

        self.assertEqual(inventory(controller.production_root), production_before)
        self.assertEqual(inventory(controller.runtime_root), runtime_before)

    def test_adopted_slot_separates_canonical_identity_from_raw_file_cas(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        monomer_md = adopted["monomer_md"]
        self.assertEqual(
            CONTROLLER.worker_record_digest(monomer_md["slot_record"]),
            monomer_md["active_slot"]["slot_record_sha256"],
        )
        self.assertNotEqual(
            monomer_md["slot_record_file_sha256"],
            monomer_md["active_slot"]["slot_record_sha256"],
        )
        with mock.patch.object(
            CONTROLLER._control_runtime,
            "adopted_dft_runtime_inventory",
            return_value=adopted["monomer_dft"]["runtime"][
                "runtime_inventory_sha256"
            ],
        ):
            controller._revalidate_adopted_runtime(adopted)
            write_private(
                Path(monomer_md["slot_record_path"]),
                CONTROLLER.canonical_json_bytes(
                    monomer_md["slot_record"]
                ).decode("utf-8"),
            )
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "slot record changed",
            ):
                controller._revalidate_adopted_runtime(adopted)

    def test_adoption_lineage_survives_successor_and_fences_recovery_and_rollback(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller.active_control_evidence = (  # type: ignore[method-assign]
            lambda: CONTROLLER._control_runtime.validate_active_control_record(
                CONTROLLER.load_private_json(controller.active_control_path)
            )
        )
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        first = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        successor_operation = "deploy-20260716-adoption-successor"
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=successor_operation,
        )
        second = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=successor_operation,
        )
        lineage_fields = (
            "authority_kind",
            "adoption_evidence",
            "adoption_evidence_sha256",
            "adopted_deployment_sha256",
        )
        self.assertEqual(
            {field: second[field] for field in lineage_fields},
            {field: first[field] for field in lineage_fields},
        )
        self.assertEqual(
            second["adoption_successor_lineage"],
            first["adoption_successor_lineage"],
        )
        operation, descriptor_path, _ready = controller._operation_paths(
            successor_operation
        )
        self.assertEqual(operation, descriptor_path.parent)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        descriptor_digest = CONTROLLER.sha256_file(descriptor_path)

        replaced = json.loads(json.dumps(second))
        replaced["adoption_evidence"] = {
            "plan_sha256": "sha256:" + "8" * 64
        }
        replaced["adoption_evidence_sha256"] = (
            CONTROLLER.canonical_json_digest(replaced["adoption_evidence"])
        )
        replaced["adopted_deployment_sha256"] = "sha256:" + "9" * 64
        CONTROLLER.validate_current_deployment_state(replaced)
        deploy_marker = v4_recovery_marker(
            {
                "schema_version": 2,
                "action": "deploy",
                "operation_id": successor_operation,
                "source_sha": TARGET_SHA,
                "descriptor_sha256": descriptor_digest,
                "executor_control": descriptor["controller"][
                    "executor_control"
                ],
                "executor_control_sha256": descriptor["controller"][
                    "executor_control_sha256"
                ],
                "current_state_precondition_sha256": descriptor[
                    "previous_deployment_sha256"
                ],
                "phase": "state-commit-started",
                "started_at": CONTROLLER.utc_now(),
                "updated_at": CONTROLLER.utc_now(),
                "runtime_stopped": True,
                "source_switched": True,
                "slot_switched": True,
                "control_switched": True,
                "unit_switched": True,
                "asset_switched": True,
                "database_change_started": True,
                "candidate_state": replaced,
                "candidate_state_sha256": CONTROLLER.sha256_bytes(
                    CONTROLLER.canonical_json_bytes(replaced) + b"\n"
                ),
            }
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "adoption lineage differs",
        ):
            CONTROLLER.validate_recovery_marker(
                deploy_marker,
                descriptor=descriptor,
                descriptor_digest=descriptor_digest,
            )

        dropped = json.loads(json.dumps(first))
        dropped.update(
            {
                "authority_kind": "governed-deployment",
                "adoption_evidence": None,
                "adoption_evidence_sha256": None,
                "adopted_deployment_sha256": None,
            }
        )
        dropped.pop("adoption_successor_lineage")
        CONTROLLER.validate_current_deployment_state(dropped)
        rollback_source_digest = CONTROLLER.sha256_file(
            controller.current_state_path
        )
        rollback_marker = {
            **deploy_marker,
            "action": "explicit-rollback",
            "phase": "explicit-rollback-state-commit-started",
            "current_state_precondition_sha256": rollback_source_digest,
            "rollback_current_state_sha256": rollback_source_digest,
            "rollback_source_terminal_audit_sha256": "sha256:" + "a" * 64,
            "rollback_attempt_id": "rollback-attempt-adoption-fixture",
            "rollback_backup_operation_id": "rollback-backup-adoption-fixture",
            "drain": {},
            "rollback_candidate_state": dropped,
            "rollback_candidate_state_sha256": CONTROLLER.sha256_bytes(
                CONTROLLER.canonical_json_bytes(dropped) + b"\n"
            ),
        }
        rollback_marker.pop("candidate_state")
        rollback_marker.pop("candidate_state_sha256")
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "adoption lineage differs",
        ):
            CONTROLLER.validate_recovery_marker(
                rollback_marker,
                descriptor=descriptor,
                descriptor_digest=descriptor_digest,
            )

    def test_adopted_prerequisites_keep_a_config_schema_and_are_target_verified(
        self,
    ) -> None:
        controller = self.controller()
        self._seed_adopted_authority(controller)

        evidence = controller.production_config_evidence(check_free_space=False)
        self.assertEqual(set(evidence), CONTROLLER.PRODUCTION_CONFIG_FIELDS)
        self.assertNotIn("adopted_prerequisites_sha256", evidence)

        authority_path = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_PREREQUISITES_RELATIVE_PATH
        )
        authority = CONTROLLER.load_private_json(authority_path)
        unexpected = json.loads(json.dumps(authority))
        unexpected["plan"]["unexpected"] = True
        unexpected["plan_sha256"] = CONTROLLER.canonical_json_digest(
            unexpected["plan"]
        )
        CONTROLLER.atomic_json(authority_path, unexpected)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "plan authority differs"
        ):
            controller._validate_adopted_prerequisite_provenance(evidence)
        CONTROLLER.atomic_json(authority_path, authority)

        helper = controller.config_dir / "deployment-mutable-data-audit"
        helper.write_bytes(b"tampered\n")
        os.chmod(helper, 0o700)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "provenance differs: deployment-mutable-data-audit",
        ):
            controller._validate_adopted_prerequisite_provenance(evidence)

    def test_ancestor_prerequisites_bind_identically_in_plan_and_prepare(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        self._rewrite_prerequisite_source(
            controller,
            source_sha=PREVIOUS_SHA,
            source_tree=PREVIOUS_TREE,
        )
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )

        plan = controller.plan(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        binding = plan["adopted_prerequisite_target_binding"]
        self.assertEqual(binding["mode"], "ancestor-byte-identical")
        self.assertEqual(binding["authority"]["source_sha"], PREVIOUS_SHA)
        self.assertEqual(binding["target"]["source_sha"], TARGET_SHA)

        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        prepared_binding = descriptor[
            "adopted_prerequisite_target_binding"
        ]
        self.assertEqual(
            binding["production_repository_materialization"]["phase"],
            "baseline",
        )
        self.assertEqual(
            prepared_binding["production_repository_materialization"][
                "phase"
            ],
            "prepared",
        )
        self.assertEqual(
            prepared_binding["production_repository_materialization"][
                "operation_id"
            ],
            OPERATION_ID,
        )
        self.assertEqual(
            {
                key: value
                for key, value in prepared_binding.items()
                if key
                not in {
                    "production_repository_materialization",
                    "identity_sha256",
                }
            },
            {
                key: value
                for key, value in binding.items()
                if key
                not in {
                    "production_repository_materialization",
                    "identity_sha256",
                }
            },
        )
        self.assertEqual(descriptor["ci"]["head_sha"], TARGET_SHA)
        authority = CONTROLLER.load_private_json(
            controller.runtime_root
            / CONTROLLER.ADOPTED_PREREQUISITES_RELATIVE_PATH
        )
        self.assertEqual(
            authority["plan"]["delivery_gate"]["ci"]["head_sha"],
            PREVIOUS_SHA,
        )

    def test_direct_plan_rejects_target_superseded_during_prerequisite_proof(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        remote_probes = 0

        def moving_remote_main() -> str:
            nonlocal remote_probes
            remote_probes += 1
            return TARGET_SHA if remote_probes == 1 else "5" * 40

        controller.remote_main = moving_remote_main  # type: ignore[method-assign]
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "target changed during prerequisite compatibility proof",
        ):
            controller.plan(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )
        self.assertEqual(remote_probes, 2)

    def test_ancestor_prerequisite_rejects_nonancestor_and_blob_drift(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        self._rewrite_prerequisite_source(
            controller,
            source_sha=PREVIOUS_SHA,
            source_tree=PREVIOUS_TREE,
        )
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        controller.prerequisite_is_ancestor = False
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "not an ancestor of the final target",
        ):
            controller.plan(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "not an ancestor of the final target",
        ):
            controller.prepare(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )

        controller.prerequisite_is_ancestor = True
        original_git_show = controller._git_show
        changed_path = CONTROLLER.ADOPTED_PREREQUISITE_FILES[0][0]

        def changed_target_blob(source_sha: str, relative: str) -> bytes:
            if source_sha == TARGET_SHA and relative == changed_path:
                return b"changed target prerequisite\n"
            return original_git_show(source_sha, relative)

        controller._git_show = changed_target_blob  # type: ignore[method-assign]
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "final prerequisite Git blob differs",
        ):
            controller.plan(
                target_sha=TARGET_SHA,
                operation_id="deploy-20260716-target-blob-drift",
            )

    def test_prerequisite_binding_reproves_authority_blob(self) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        self._rewrite_prerequisite_source(
            controller,
            source_sha=PREVIOUS_SHA,
            source_tree=PREVIOUS_TREE,
        )
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        original_git_show = controller._git_show
        changed_path = CONTROLLER.ADOPTED_PREREQUISITE_FILES[0][0]

        def changed_authority_blob(source_sha: str, relative: str) -> bytes:
            if source_sha == PREVIOUS_SHA and relative == changed_path:
                return b"changed authority prerequisite\n"
            return original_git_show(source_sha, relative)

        controller._git_show = changed_authority_blob  # type: ignore[method-assign]
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "final prerequisite Git blob differs",
        ):
            controller.prepare(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )

    def test_adopted_descriptor_requires_and_exclusively_owns_binding(self) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)

        missing = json.loads(json.dumps(descriptor))
        missing.pop("adopted_prerequisite_target_binding")
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lacks prerequisite target binding",
        ):
            CONTROLLER.validate_descriptor(missing)

        unexpected = json.loads(json.dumps(descriptor))
        unexpected["adopted_deployment"] = None
        unexpected["adopted_deployment_sha256"] = None
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "without adoption has a prerequisite target binding|differs from repository",
        ):
            CONTROLLER.validate_descriptor(unexpected)

    def test_adopted_binding_v3_rejects_mixed_unit_provenance_and_blobs(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        binding = descriptor["adopted_prerequisite_target_binding"]

        def reseal(document: dict[str, object]) -> None:
            document["identity_sha256"] = CONTROLLER.canonical_json_digest(
                {
                    key: value
                    for key, value in document.items()
                    if key != "identity_sha256"
                }
            )

        for field, replacement in (
            ("production_source_sha", "f" * 40),
            ("production_source_tree", "e" * 40),
            ("bootstrap_control_sha256", "sha256:" + "d" * 64),
            ("adopted_prerequisites_sha256", "sha256:" + "c" * 64),
        ):
            with self.subTest(field=field):
                changed = json.loads(json.dumps(binding))
                unit = changed["unit_permission_authority"]
                unit[field] = replacement
                reseal(unit)
                reseal(changed)
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "unit permission authority differs",
                ):
                    CONTROLLER.validate_adopted_prerequisite_target_binding(
                        changed
                    )

        changed = json.loads(json.dumps(binding))
        unit = changed["unit_permission_authority"]
        successor = unit["git_permission_successor"]
        successor["files"][10]["target"]["sha256"] = (
            "sha256:" + "b" * 64
        )
        successor["files_sha256"] = CONTROLLER.canonical_json_digest(
            successor["files"]
        )
        reseal(successor)
        reseal(unit)
        reseal(changed)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "successor authority chain differs|unit permission authority differs",
        ):
            CONTROLLER.validate_adopted_prerequisite_target_binding(changed)

    def test_adopted_descriptor_binds_nested_authorities_to_adopted_bytes(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)

        def reseal(document: dict[str, object]) -> None:
            document["identity_sha256"] = CONTROLLER.canonical_json_digest(
                {
                    key: value
                    for key, value in document.items()
                    if key != "identity_sha256"
                }
            )

        for field, replacement in (
            ("production_source_sha", "f" * 40),
            ("production_source_tree", "e" * 40),
        ):
            with self.subTest(field=field):
                changed = json.loads(json.dumps(descriptor))
                binding = changed["adopted_prerequisite_target_binding"]
                permission = binding["git_permission_authority"]
                source_successor = binding[
                    "git_permission_source_successor_authority"
                ]
                unit = binding["unit_permission_authority"]
                unit_successor = unit["git_permission_successor"]
                nested_source_successor = unit_successor[
                    "source_successor_authority"
                ]
                permission[field] = replacement
                source_successor[field] = replacement
                unit[field] = replacement
                nested_source_successor[field] = replacement
                reseal(permission)
                reseal(source_successor)
                reseal(nested_source_successor)
                reseal(unit_successor)
                reseal(unit)
                reseal(binding)
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "repository transition|prerequisite target binding differs",
                ):
                    CONTROLLER.validate_adopted_prerequisite_target_binding(
                        binding
                    )

        changed = json.loads(json.dumps(descriptor))
        binding = changed["adopted_prerequisite_target_binding"]
        replacement_digest = "sha256:" + "9" * 64
        binding["authority"]["adopted_deployment_sha256"] = (
            replacement_digest
        )
        permission = binding["git_permission_authority"]
        source_successor = binding[
            "git_permission_source_successor_authority"
        ]
        unit = binding["unit_permission_authority"]
        unit_successor = unit["git_permission_successor"]
        nested_source_successor = unit_successor[
            "source_successor_authority"
        ]
        permission["adopted_deployment_sha256"] = replacement_digest
        source_successor["adopted_deployment_sha256"] = replacement_digest
        unit["adopted_deployment_sha256"] = replacement_digest
        nested_source_successor[
            "adopted_deployment_sha256"
        ] = replacement_digest
        reseal(permission)
        reseal(source_successor)
        reseal(nested_source_successor)
        reseal(unit_successor)
        reseal(unit)
        reseal(binding)
        CONTROLLER.validate_adopted_prerequisite_target_binding(binding)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "prerequisite target binding differs from repository",
        ):
            CONTROLLER.validate_descriptor(changed)

    def test_adopted_descriptor_schema_discriminators_are_exact_integers(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)

        def reseal(document: dict[str, object]) -> None:
            document["identity_sha256"] = CONTROLLER.canonical_json_digest(
                {
                    key: value
                    for key, value in document.items()
                    if key != "identity_sha256"
                }
            )

        for invalid_schema in (True, 1.0):
            with self.subTest(authority_schema=invalid_schema):
                changed_adopted = json.loads(json.dumps(adopted))
                changed_adopted["schema_version"] = invalid_schema
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "adopted deployment authority has an invalid shape",
                ):
                    CONTROLLER.validate_adopted_deployment(changed_adopted)

        changed_descriptor = json.loads(json.dumps(descriptor))
        changed_descriptor["schema_version"] = 4.0
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "descriptor has an invalid shape",
        ):
            CONTROLLER.validate_descriptor(changed_descriptor)

        changed_controller = json.loads(json.dumps(descriptor))
        changed_controller["controller"]["schema_version"] = True
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "controller schema is unsupported",
        ):
            CONTROLLER.validate_descriptor(changed_controller)

        binding = descriptor["adopted_prerequisite_target_binding"]
        for invalid_schema in (3.0, [], {}):
            with self.subTest(binding_schema=invalid_schema):
                changed_binding = json.loads(json.dumps(binding))
                changed_binding["schema_version"] = invalid_schema
                reseal(changed_binding)
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "target binding has an invalid shape",
                ):
                    CONTROLLER.validate_adopted_prerequisite_target_binding(
                        changed_binding
                    )

        changed_trust = json.loads(json.dumps(binding))
        changed_trust["target_source_trust"]["schema_version"] = True
        changed_trust["target_source_trust_sha256"] = (
            CONTROLLER.canonical_json_digest(
                changed_trust["target_source_trust"]
            )
        )
        reseal(changed_trust)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "target source trust differs",
        ):
            CONTROLLER.validate_adopted_prerequisite_target_binding(
                changed_trust
            )

        changed_successor = json.loads(json.dumps(binding))
        unit = changed_successor["unit_permission_authority"]
        successor = unit["git_permission_successor"]
        successor["schema_version"] = True
        reseal(successor)
        reseal(unit)
        reseal(changed_successor)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "successor has an invalid shape",
        ):
            CONTROLLER.validate_adopted_prerequisite_target_binding(
                changed_successor
            )

        git_takeover = adopted_git_permission_takeover_fixture(
            controller.production_root,
            controller.runtime_root,
        )
        changed_takeover = json.loads(json.dumps(git_takeover))
        changed_takeover["schema_version"] = True
        reseal(changed_takeover)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "takeover evidence has an invalid shape",
        ):
            CONTROLLER.validate_adopted_git_permission_takeover(
                changed_takeover
            )

        historical = json.loads(json.dumps(descriptor))
        historical_binding = historical[
            "adopted_prerequisite_target_binding"
        ]
        historical_binding["schema_version"] = 2
        historical_binding.pop("unit_permission_authority")
        historical_binding.pop(
            "git_permission_source_successor_authority"
        )
        historical_binding.pop("production_repository_materialization")
        historical_binding.pop("source_successor_completed_journal_sha256")
        historical_binding.pop("unit_permission_completed_journal_sha256")
        historical_binding.pop(
            "unit_permission_transaction_inventory_sha256"
        )
        historical_binding.pop(
            "production_git_snapshot_authority_sha256"
        )
        historical_binding.pop("bootstrap_router_intent_sha256")
        historical_binding.pop("bootstrap_router_authority_sha256")
        historical_permission = historical_binding[
            "git_permission_authority"
        ]
        historical_permission["source_sha"] = TARGET_SHA
        historical_permission["source_tree"] = TARGET_TREE
        reseal(historical_permission)
        reseal(historical_binding)
        CONTROLLER.validate_descriptor(historical)

    def test_raw_adoption_chain_schema_discriminators_are_exact_integers(
        self,
    ) -> None:
        controller = self.controller()
        git_combined, source_successor = (
            self._activate_raw_source_successor_consumer(controller)
        )
        adopted = CONTROLLER.load_private_json(
            controller.adopted_state_path
        )
        marker = CONTROLLER.load_private_json(
            controller.git_permission_marker_path
        )
        production_config = controller.production_config_evidence(
            check_free_space=False
        )
        bootstrap_path = controller.state_dir / "bootstrap-control.json"
        prerequisite_path = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_PREREQUISITES_RELATIVE_PATH
        )
        git_authority_path = controller.adopted_git_permissions_path
        unit_authority_path = controller.adopted_unit_permissions_path
        marker_digest = CONTROLLER.sha256_file(
            controller.git_permission_marker_path
        )
        backup_root = (
            controller.state_dir / "adopted-unit-permission-backups"
        )
        backup_operation = backup_root / "adopt-unit-permission-fixture-001"
        backup_claims = (
            backup_root / ".adopt-unit-permission-fixture-001.owner.json",
            backup_operation / ".owner.json",
        )

        def set_schema(
            document: dict[str, object],
            path: tuple[str, ...],
            value: object,
        ) -> None:
            target: object = document
            for field in path:
                self.assertIsInstance(target, dict)
                target = target[field]  # type: ignore[index]
            self.assertIsInstance(target, dict)
            target["schema_version"] = value  # type: ignore[index]

        def assert_rejected(
            *,
            path: Path,
            schema_path: tuple[str, ...],
            value: object,
            validate: object,
            message: str,
            reseal: object | None = None,
            auxiliary_paths: tuple[Path, ...] = (),
        ) -> None:
            snapshots = {
                candidate: candidate.read_bytes()
                for candidate in (path, *auxiliary_paths)
            }
            document = CONTROLLER.load_private_json(path)
            set_schema(document, schema_path, value)
            if reseal is not None:
                reseal(document)  # type: ignore[operator]
            CONTROLLER.atomic_json(path, document)
            try:
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError, message
                ):
                    validate()  # type: ignore[operator]
            finally:
                for candidate, payload in snapshots.items():
                    CONTROLLER.atomic_bytes(candidate, payload)

        validate_prerequisite = lambda: (
            controller._validate_adopted_prerequisite_provenance(
                production_config
            )
        )
        validate_git = lambda: (
            CONTROLLER.PullDeployController._validate_adopted_git_permission_authority(
                controller,
                adopted=adopted,
                marker=marker,
                marker_digest=marker_digest,
            )
        )
        validate_unit = lambda: (
            CONTROLLER.PullDeployController._validate_adopted_unit_permission_authority(
                controller,
                adopted=adopted,
                git_permission_takeover=git_combined,
                git_permission_source_successor_takeover=(
                    source_successor
                ),
                verify_live=False,
            )
        )

        def reseal_plan(document: dict[str, object]) -> None:
            plan = document["plan"]
            self.assertIsInstance(plan, dict)
            readiness = plan["source_readiness"]
            self.assertIsInstance(readiness, dict)
            plan["source_readiness_sha256"] = (
                CONTROLLER.canonical_json_digest(readiness)
            )
            document["plan_sha256"] = CONTROLLER.canonical_json_digest(
                plan
            )

        def reseal_unit_plan(document: dict[str, object]) -> None:
            reseal_plan(document)
            plan_digest = document["plan_sha256"]
            backup = document["backup"]
            self.assertIsInstance(backup, dict)
            owner = backup["owner"]
            self.assertIsInstance(owner, dict)
            owner["plan_sha256"] = plan_digest
            owner_digest = CONTROLLER.canonical_json_digest(owner)
            backup["owner_sha256"] = owner_digest
            backup["claim_sha256"] = owner_digest
            for claim in backup_claims:
                CONTROLLER.atomic_json(claim, owner)
            backup["inventory_sha256"] = (
                CONTROLLER.private_tree_inventory_digest(backup_operation)
            )
            document["backup_sha256"] = (
                CONTROLLER.canonical_json_digest(backup)
            )

        def reseal_backup(document: dict[str, object]) -> None:
            document["backup_sha256"] = CONTROLLER.canonical_json_digest(
                document["backup"]
            )

        cases = (
            (
                "bootstrap",
                bootstrap_path,
                (),
                3.0,
                controller._adoption_bootstrap_binding,
                "bootstrap-control authority is incomplete",
                None,
                (),
            ),
            (
                "prerequisite-authority",
                prerequisite_path,
                (),
                True,
                validate_prerequisite,
                "prerequisite provenance is invalid",
                None,
                (),
            ),
            (
                "prerequisite-plan",
                prerequisite_path,
                ("plan",),
                1.0,
                validate_prerequisite,
                "prerequisite plan authority differs",
                reseal_plan,
                (),
            ),
            (
                "prerequisite-readiness",
                prerequisite_path,
                ("plan", "source_readiness"),
                2.0,
                validate_prerequisite,
                "prerequisite source readiness differs",
                reseal_plan,
                (),
            ),
            (
                "git-authority",
                git_authority_path,
                (),
                True,
                validate_git,
                "Git permission authority has an invalid shape",
                None,
                (),
            ),
            (
                "git-plan",
                git_authority_path,
                ("plan",),
                1.0,
                validate_git,
                "Git permission plan authority differs",
                reseal_plan,
                (),
            ),
            (
                "git-readiness",
                git_authority_path,
                ("plan", "source_readiness"),
                2.0,
                validate_git,
                "Git permission source readiness differs",
                reseal_plan,
                (),
            ),
            (
                "unit-authority",
                unit_authority_path,
                (),
                True,
                validate_unit,
                "unit permission authority has an invalid shape",
                None,
                (),
            ),
            (
                "unit-plan",
                unit_authority_path,
                ("plan",),
                1.0,
                validate_unit,
                "unit permission plan authority differs",
                reseal_unit_plan,
                backup_claims,
            ),
            (
                "unit-readiness",
                unit_authority_path,
                ("plan", "source_readiness"),
                2.0,
                validate_unit,
                "unit permission source readiness differs",
                reseal_unit_plan,
                backup_claims,
            ),
            (
                "unit-backup",
                unit_authority_path,
                ("backup",),
                True,
                validate_unit,
                "unit permission backup authority differs",
                reseal_backup,
                (),
            ),
            (
                "unit-backup-owner",
                unit_authority_path,
                ("backup", "owner"),
                True,
                validate_unit,
                "unit permission backup authority differs",
                reseal_backup,
                (),
            ),
        )
        for (
            label,
            path,
            schema_path,
            value,
            validate,
            message,
            reseal,
            auxiliary_paths,
        ) in cases:
            with self.subTest(label=label, value=value):
                assert_rejected(
                    path=path,
                    schema_path=schema_path,
                    value=value,
                    validate=validate,
                    message=message,
                    reseal=reseal,
                    auxiliary_paths=auxiliary_paths,
                )

        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        state = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        changed_state = json.loads(json.dumps(state))
        changed_state["schema_version"] = 3.0
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "current deployment state has an invalid shape",
        ):
            CONTROLLER.validate_current_deployment_state(changed_state)

    def test_already_ready_replay_rejects_prerequisite_blob_drift_without_mutation(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        self._rewrite_prerequisite_source(
            controller,
            source_sha=PREVIOUS_SHA,
            source_tree=PREVIOUS_TREE,
        )
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        operation, descriptor_path, ready_path = controller._operation_paths(
            OPERATION_ID
        )
        descriptor_payload = descriptor_path.read_bytes()
        ready_payload = ready_path.read_bytes()
        operation_tree_before = {
            path.relative_to(operation).as_posix(): path.read_bytes()
            for path in operation.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        original_git_show = controller._git_show
        changed_path = CONTROLLER.ADOPTED_PREREQUISITE_FILES[0][0]

        def changed_target_blob(source_sha: str, relative: str) -> bytes:
            if source_sha == TARGET_SHA and relative == changed_path:
                return b"changed already-ready target prerequisite\n"
            return original_git_show(source_sha, relative)

        controller._git_show = changed_target_blob  # type: ignore[method-assign]
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "final prerequisite Git blob differs",
        ):
            controller.prepare(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )

        self.assertEqual(descriptor_path.read_bytes(), descriptor_payload)
        self.assertEqual(ready_path.read_bytes(), ready_payload)
        self.assertEqual(
            {
                path.relative_to(operation).as_posix(): path.read_bytes()
                for path in operation.rglob("*")
                if path.is_file() and not path.is_symlink()
            },
            operation_tree_before,
        )
        self.assertEqual(controller.source_sha, PREVIOUS_SHA)

    def test_descriptor_only_recovery_rejects_prerequisite_authority_drift_before_ready(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        self._rewrite_prerequisite_source(
            controller,
            source_sha=PREVIOUS_SHA,
            source_tree=PREVIOUS_TREE,
        )
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        operation, descriptor_path, ready_path = controller._operation_paths(
            OPERATION_ID
        )
        ready_path.unlink()
        CONTROLLER.fsync_directory(ready_path.parent)
        descriptor_payload = descriptor_path.read_bytes()
        operation_tree_before = {
            path.relative_to(operation).as_posix(): path.read_bytes()
            for path in operation.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        authority_path = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_PREREQUISITES_RELATIVE_PATH
        )
        authority = CONTROLLER.load_private_json(authority_path)
        delivery = authority["plan"]["delivery_gate"]
        delivery["ci"]["required_jobs"] = ["drifted-old-authority-gate"]
        authority["plan"]["delivery_gate_sha256"] = (
            CONTROLLER.canonical_json_digest(delivery)
        )
        authority["plan_sha256"] = CONTROLLER.canonical_json_digest(
            authority["plan"]
        )
        CONTROLLER.atomic_json(authority_path, authority)

        with mock.patch.object(
            CONTROLLER,
            "atomic_json",
            wraps=CONTROLLER.atomic_json,
        ) as atomic_write, self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "binding changed after prepare",
        ):
            controller._resume_descriptor_without_ready(
                descriptor_path=descriptor_path,
                ready_path=ready_path,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
                bridge_relation=None,
                authority_sha=None,
                prefetch_operation_id=None,
            )

        atomic_write.assert_not_called()
        self.assertFalse(ready_path.exists())
        self.assertFalse(ready_path.is_symlink())
        self.assertEqual(descriptor_path.read_bytes(), descriptor_payload)
        self.assertEqual(
            {
                path.relative_to(operation).as_posix(): path.read_bytes()
                for path in operation.rglob("*")
                if path.is_file() and not path.is_symlink()
            },
            operation_tree_before,
        )
        self.assertEqual(controller.source_sha, PREVIOUS_SHA)

    def test_pre_switch_reproves_prerequisite_authority_binding(self) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        self._rewrite_prerequisite_source(
            controller,
            source_sha=PREVIOUS_SHA,
            source_tree=PREVIOUS_TREE,
        )
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        authority_path = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_PREREQUISITES_RELATIVE_PATH
        )
        authority = CONTROLLER.load_private_json(authority_path)
        delivery = authority["plan"]["delivery_gate"]
        delivery["ci"]["required_jobs"] = ["different-successful-old-gate"]
        authority["plan"]["delivery_gate_sha256"] = (
            CONTROLLER.canonical_json_digest(delivery)
        )
        authority["plan_sha256"] = CONTROLLER.canonical_json_digest(
            authority["plan"]
        )
        CONTROLLER.atomic_json(authority_path, authority)

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "binding changed after prepare",
        ):
            CONTROLLER.PullDeployController._revalidate_pre_switch(
                controller, descriptor
            )
        self.assertEqual(controller.source_sha, PREVIOUS_SHA)

    def test_pre_switch_rejects_prerequisite_ancestry_drift(self) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        self._rewrite_prerequisite_source(
            controller,
            source_sha=PREVIOUS_SHA,
            source_tree=PREVIOUS_TREE,
        )
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        controller.prerequisite_is_ancestor = False

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "not an ancestor of the final target",
        ):
            CONTROLLER.PullDeployController._revalidate_pre_switch(
                controller, descriptor
            )
        self.assertEqual(controller.source_sha, PREVIOUS_SHA)

    def test_pre_switch_rejects_prerequisite_blob_drift(self) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        self._rewrite_prerequisite_source(
            controller,
            source_sha=PREVIOUS_SHA,
            source_tree=PREVIOUS_TREE,
        )
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        original_git_show = controller._git_show
        changed_path = CONTROLLER.ADOPTED_PREREQUISITE_FILES[-1][0]

        def changed_target_blob(source_sha: str, relative: str) -> bytes:
            if source_sha == TARGET_SHA and relative == changed_path:
                return b"changed after prepare\n"
            return original_git_show(source_sha, relative)

        controller._git_show = changed_target_blob  # type: ignore[method-assign]
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "final prerequisite Git blob differs",
        ):
            CONTROLLER.PullDeployController._revalidate_pre_switch(
                controller, descriptor
            )
        self.assertEqual(controller.source_sha, PREVIOUS_SHA)

    def test_prepare_rejects_target_superseded_during_prerequisite_proof(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        self._rewrite_prerequisite_source(
            controller,
            source_sha=PREVIOUS_SHA,
            source_tree=PREVIOUS_TREE,
        )
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        controller._prepared_adopted_prerequisite_target_binding = (  # type: ignore[method-assign]
            lambda authority, *, target_sha, target_tree, operation_id: (
                CONTROLLER.PullDeployController._prepared_adopted_prerequisite_target_binding(
                    controller,
                    authority,
                    target_sha=target_sha,
                    target_tree=target_tree,
                    operation_id=operation_id,
                )
            )
        )
        controller._verify_production_repository_transition = (  # type: ignore[method-assign]
            lambda _source_successor, *, target_sha, target_tree, phase,
            operation_id=None: production_repository_materialization_fixture(
                phase="prepared",
                operation_id=operation_id,
            )
        )

        original_git = controller._git

        def compatibility_git(
            *arguments: str, **kwargs: object
        ) -> SimpleNamespace:
            if arguments[:2] == ("cat-file", "-t"):
                return SimpleNamespace(stdout="commit\n", returncode=0)
            if arguments == ("rev-parse", f"{PREVIOUS_SHA}^{{tree}}"):
                return SimpleNamespace(stdout=PREVIOUS_TREE + "\n", returncode=0)
            if arguments == ("rev-parse", f"{TARGET_SHA}^{{tree}}"):
                return SimpleNamespace(stdout=TARGET_TREE + "\n", returncode=0)
            if arguments[:2] == ("merge-base", "--is-ancestor"):
                return SimpleNamespace(stdout="", returncode=0)
            return original_git(*arguments, **kwargs)

        controller._git = compatibility_git  # type: ignore[method-assign]
        source_records = {
            record["path"]: record
            for record in controller._fixture_git_permission_source_successor_takeover[  # type: ignore[attr-defined]
                "fixed_files"
            ]
        }
        controller._production_git_blob_identity = (  # type: ignore[method-assign]
            lambda commit, path: source_records[path][
                "predecessor" if commit == PREVIOUS_SHA else "target"
            ]
        )
        remote_probes = 0

        def moving_remote_main() -> str:
            nonlocal remote_probes
            remote_probes += 1
            return TARGET_SHA if remote_probes < 3 else "5" * 40

        controller.remote_main = moving_remote_main  # type: ignore[method-assign]
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "compatibility target changed during proof",
        ):
            controller.prepare(
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
            )
        self.assertGreaterEqual(remote_probes, 3)
        self.assertEqual(controller.source_sha, PREVIOUS_SHA)

    def test_pre_switch_rejects_target_superseded_during_prerequisite_proof(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        self._rewrite_prerequisite_source(
            controller,
            source_sha=PREVIOUS_SHA,
            source_tree=PREVIOUS_TREE,
        )
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        controller._prepared_adopted_prerequisite_target_binding = (  # type: ignore[method-assign]
            lambda authority, *, target_sha, target_tree, operation_id: (
                CONTROLLER.PullDeployController._prepared_adopted_prerequisite_target_binding(
                    controller,
                    authority,
                    target_sha=target_sha,
                    target_tree=target_tree,
                    operation_id=operation_id,
                )
            )
        )
        controller._verify_production_repository_transition = (  # type: ignore[method-assign]
            lambda _source_successor, *, target_sha, target_tree, phase,
            operation_id=None: production_repository_materialization_fixture(
                phase="prepared",
                operation_id=operation_id,
            )
        )

        original_git = controller._git

        def compatibility_git(
            *arguments: str, **kwargs: object
        ) -> SimpleNamespace:
            if arguments[:2] == ("cat-file", "-t"):
                return SimpleNamespace(stdout="commit\n", returncode=0)
            if arguments == ("rev-parse", f"{PREVIOUS_SHA}^{{tree}}"):
                return SimpleNamespace(stdout=PREVIOUS_TREE + "\n", returncode=0)
            if arguments == ("rev-parse", f"{TARGET_SHA}^{{tree}}"):
                return SimpleNamespace(stdout=TARGET_TREE + "\n", returncode=0)
            if arguments[:2] == ("merge-base", "--is-ancestor"):
                return SimpleNamespace(stdout="", returncode=0)
            return original_git(*arguments, **kwargs)

        controller._git = compatibility_git  # type: ignore[method-assign]
        source_records = {
            record["path"]: record
            for record in controller._fixture_git_permission_source_successor_takeover[  # type: ignore[attr-defined]
                "fixed_files"
            ]
        }
        controller._production_git_blob_identity = (  # type: ignore[method-assign]
            lambda commit, path: source_records[path][
                "predecessor" if commit == PREVIOUS_SHA else "target"
            ]
        )
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        remote_probes = 0

        def moving_remote_main() -> str:
            nonlocal remote_probes
            remote_probes += 1
            return TARGET_SHA if remote_probes < 3 else "5" * 40

        controller.remote_main = moving_remote_main  # type: ignore[method-assign]
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "compatibility target changed during proof",
        ):
            CONTROLLER.PullDeployController._revalidate_pre_switch(
                controller, descriptor
            )
        self.assertGreaterEqual(remote_probes, 3)
        self.assertEqual(controller.source_sha, PREVIOUS_SHA)

    def test_manual_bootstrap_missing_or_mismatched_adopted_state_fails_early(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller.adopted_state_path.unlink()

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "requires adopted-deployment",
        ):
            controller.production_config_evidence(check_free_space=False)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "requires adopted-deployment",
        ):
            controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor, ready = controller._operation_paths(OPERATION_ID)
        self.assertFalse(descriptor.exists())
        self.assertFalse(ready.exists())

        CONTROLLER.atomic_json(controller.adopted_state_path, adopted)
        mismatched = json.loads(json.dumps(adopted))
        mismatched["adopted_at"] = "2026-07-31T00:00:00Z"
        CONTROLLER.atomic_json(controller.adopted_state_path, mismatched)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "bootstrap authority differs",
        ):
            controller.production_config_evidence(check_free_space=False)

    def test_first_adopted_prepare_binds_final_source_and_ci_delivery_gate(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller.active_control_evidence = (  # type: ignore[method-assign]
            lambda: CONTROLLER._control_runtime.validate_active_control_record(
                CONTROLLER.load_private_json(controller.active_control_path)
            )
        )
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        authority_path = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_PREREQUISITES_RELATIVE_PATH
        )
        authority = CONTROLLER.load_private_json(authority_path)
        authority["plan"]["delivery_gate"]["ci"]["required_jobs"] = [
            "different-successful-gate"
        ]
        authority["plan"]["delivery_gate_sha256"] = (
            CONTROLLER.canonical_json_digest(
                authority["plan"]["delivery_gate"]
            )
        )
        authority["plan_sha256"] = CONTROLLER.canonical_json_digest(
            authority["plan"]
        )
        CONTROLLER.atomic_json(authority_path, authority)

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "CI differs from final prerequisite delivery gate",
        ):
            controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)

    def test_first_adopted_prepare_rejects_nonfinal_prerequisite_source(
        self,
    ) -> None:
        controller = self.controller()
        adopted = self._seed_adopted_authority(controller)
        controller.active_control_evidence = (  # type: ignore[method-assign]
            lambda: CONTROLLER._control_runtime.validate_active_control_record(
                CONTROLLER.load_private_json(controller.active_control_path)
            )
        )
        controller._revalidate_adopted_runtime = (  # type: ignore[method-assign]
            lambda observed: self.assertEqual(observed, adopted)
        )
        authority_path = (
            controller.runtime_root
            / CONTROLLER.ADOPTED_PREREQUISITES_RELATIVE_PATH
        )
        authority = CONTROLLER.load_private_json(authority_path)
        authority["source_tree"] = PREVIOUS_TREE
        authority["plan"]["source_tree"] = PREVIOUS_TREE
        authority["plan"]["source_readiness"]["source_tree"] = PREVIOUS_TREE
        authority["plan"]["source_readiness_sha256"] = (
            CONTROLLER.canonical_json_digest(
                authority["plan"]["source_readiness"]
            )
        )
        authority["plan_sha256"] = CONTROLLER.canonical_json_digest(
            authority["plan"]
        )
        CONTROLLER.atomic_json(authority_path, authority)

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "different source tree",
        ):
            controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)


class BootstrapQuiesceContractTests(unittest.TestCase):
    def test_example_output_is_accepted_by_dedicated_controller_validator(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bootstrap-quiesce-") as temporary:
            root = Path(temporary)
            production = root / "production"
            runtime = root / "runtime"
            fake_bin = root / "bin"
            production.mkdir()
            (runtime / "config").mkdir(parents=True)
            fake_bin.mkdir()
            docker = fake_bin / "docker"
            docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(docker, 0o700)
            probe = runtime / "config/bootstrap-active-jobs-probe"
            jobs = {name: 0 for name in CONTROLLER.ACTIVE_JOB_FIELDS_V2}
            evidence_source = {
                "ingress_isolated": True,
                "active_jobs": jobs,
                "active_total": 0,
                "active_jobs_schema_version": 2,
            }
            probe.write_text(
                "#!/bin/sh\nprintf '%s\\n' '"
                + json.dumps(evidence_source, separators=(",", ":"))
                + "'\n",
                encoding="utf-8",
            )
            os.chmod(probe, 0o700)
            source = (
                REPOSITORY_ROOT / "ops/config/bootstrap-quiesce.example"
            ).read_text(encoding="utf-8")
            source = source.replace(
                "/data/lzq/gith/nexpoly-runtime", str(runtime)
            ).replace("/data/lzq/gith/nexpoly", str(production))
            hook = root / "bootstrap-quiesce"
            hook.write_text(source, encoding="utf-8")
            os.chmod(hook, 0o700)
            result = subprocess.run(
                [str(hook)],
                cwd=production,
                env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            evidence = json.loads(result.stdout)
            self.assertEqual(
                CONTROLLER.validate_bootstrap_quiesce_evidence(evidence), evidence
            )
            with self.assertRaisesRegex(CONTROLLER.PullDeployError, "invalid shape"):
                CONTROLLER.validate_active_jobs_evidence(evidence, require_drained=True)


if __name__ == "__main__":
    unittest.main()
