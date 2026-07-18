#!/usr/bin/env python3
"""Fail-closed, read-only production deployment readiness aggregation.

The command consumes a short-lived, privately stored observation envelope and
cross-binds every deployment authority to one exact F commit and one exact B
bridge commit.  It intentionally has no collection code: it cannot fetch Git,
pull an image, invoke Compose, start a container, contact PostgreSQL, or write
runtime state.

Offline fixtures exercise the same strict schema and cross-binding rules
without reading production paths.  Live mode additionally revalidates durable
runtime evidence using read-only operations only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Mapping


sys.dont_write_bytecode = True


SCRIPT_ROOT = Path(__file__).absolute().parent


def _load_python_module(
    module_name: str,
    path: Path,
    *,
    exact_mode: int | None = None,
) -> Any:
    """Load one owner-controlled file without relying on ambient sys.path."""

    existing = sys.modules.get(module_name)
    if existing is not None:
        if Path(str(getattr(existing, "__file__", ""))).absolute() != path.absolute():
            raise RuntimeError("readiness validator module path changed")
        return existing
    path = path.absolute()
    try:
        metadata = path.lstat()
        parent = path.parent.lstat()
    except OSError as exc:
        raise RuntimeError("required readiness validator is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
        or exact_mode is not None
        and stat.S_IMODE(metadata.st_mode) != exact_mode
        or not stat.S_ISDIR(parent.st_mode)
        or path.parent.is_symlink()
        or parent.st_uid != os.geteuid()
        or parent.st_mode & 0o022
    ):
        raise RuntimeError("required readiness validator is unsafe")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("required readiness validator cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _load_sibling(module_name: str, filename: str) -> Any:
    return _load_python_module(module_name, SCRIPT_ROOT / filename)


asset_release_contract = _load_sibling(
    "nexpoly_readiness_asset_release_contract",
    "asset_release_contract.py",
)
bridge_deploy_core = _load_sibling(
    "nexpoly_readiness_bridge_deploy_core",
    "bridge_deploy_core.py",
)
site_helper_contracts = _load_sibling(
    "nexpoly_readiness_site_helper_contracts",
    "site_helper_contracts.py",
)
monomer_dft_runtime_contract = _load_sibling(
    "nexpoly_readiness_monomer_dft_runtime_contract",
    "monomer_dft_runtime_contract.py",
)

SCHEMA_VERSION = 1
OUTPUT_SCHEMA_VERSION = 1
RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
EVIDENCE_RELATIVE_PATH = Path("audit/production-readiness/evidence.json")
EXTERNAL_DATABASE_AUDIT_RELATIVE_PATH = Path(
    "audit/contracts/0012/external-database-audit.json"
)
MUTABLE_DATA_AUDIT_RELATIVE_PATH = Path(
    "audit/mutable-data/production-readiness-before.json"
)
REPOSITORY_SSH_URL = "git@github.com:lzq390/ZhijuPoly.git"
REPOSITORY_SOURCE_URL = "https://github.com/lzq390/ZhijuPoly"
POSTGRES16_IMAGE = (
    "postgres:16-alpine@"
    "sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
)
POSTGRES_AUDIT_IMAGES = {
    "14": (
        "postgres:14-alpine@"
        "sha256:f1341c01408dc7278e9d365ed4f860cd3f87dd16b4464ac326fc0f422083a579"
    ),
    "15": (
        "postgres:15-alpine@"
        "sha256:3d0f7584ed7d04e27fa050d6683a74746608faf21f202be78460d679cc56461f"
    ),
    "16": POSTGRES16_IMAGE,
    "18": (
        "postgres:18-alpine@"
        "sha256:9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15"
    ),
}
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
MAX_LIVE_EVIDENCE_AGE = dt.timedelta(minutes=15)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RELEASE_ID_RE = re.compile(r"^[0-9a-f]{64}$")
OPERATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
MIGRATION_RE = re.compile(r"^[0-9]{4}_[a-z0-9_]+$")
WHEEL_RE = re.compile(r"^[A-Za-z0-9_.+-]+\.whl$")
PYTHON_VERSION_RE = re.compile(r"^3\.12(?:\.[0-9]+)?$")
UV_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

TOP_FIELDS = {
    "schema_version",
    "captured_at",
    "authority",
    "bridge",
    "git",
    "ci",
    "oci",
    "asset",
    "prepared",
    "prefetch",
    "helpers",
    "takeover",
    "alias",
    "external_media",
    "postgres",
    "migrations",
    "mutable_data",
    "native_runtime",
    "capacity",
    "conflicts",
    "observation",
    "evidence_sha256",
}
SECTION_NAMES = tuple(
    sorted(
        TOP_FIELDS
        - {
            "schema_version",
            "captured_at",
            "authority",
            "bridge",
            "evidence_sha256",
        }
    )
)


class ProductionReadinessError(RuntimeError):
    """The supplied evidence does not prove production readiness."""


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def canonical_json_digest(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _fail(message: str) -> None:
    raise ProductionReadinessError(message)


def _exact_dict(
    value: object,
    fields: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        _fail(f"{label} has an invalid shape")
    return dict(value)


def _sealed(
    value: object,
    fields: set[str],
    label: str,
) -> dict[str, Any]:
    document = _exact_dict(value, fields | {"evidence_sha256"}, label)
    identity = {
        key: document[key]
        for key in sorted(fields)
    }
    if document["evidence_sha256"] != canonical_json_digest(identity):
        _fail(f"{label} seal differs")
    return document


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        _fail(f"{label} is not an exact Git SHA")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        _fail(f"{label} is not a full sha256 digest")
    return value


def _operation_id(value: object, label: str) -> str:
    if not isinstance(value, str) or OPERATION_ID_RE.fullmatch(value) is None:
        _fail(f"{label} is invalid")
    return value


def _positive_int(value: object, label: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail(f"{label} is invalid")
    return value


def _utc_timestamp(value: object, label: str) -> dt.datetime:
    if (
        not isinstance(value, str)
        or not value.endswith("Z")
        or "." in value
    ):
        _fail(f"{label} is not canonical UTC")
    try:
        parsed = dt.datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        _fail(f"{label} is not canonical UTC")
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != dt.timedelta(0)
        or parsed.microsecond != 0
    ):
        _fail(f"{label} is not canonical UTC")
    return parsed


def _source_record(value: object, label: str) -> dict[str, str]:
    record = _exact_dict(value, {"sha", "tree"}, label)
    return {
        "sha": _sha(record["sha"], f"{label} SHA"),
        "tree": _sha(record["tree"], f"{label} tree"),
    }


def _validate_media_cas(
    value: object,
    *,
    registry_sha256: str,
    media_count: int,
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "status",
        "registry_sha256",
        "manifest_sha256",
        "inventory_sha256",
        "media_count",
    }
    record = _sealed(value, fields, "external media CAS")
    if (
        record["schema_version"] != 1
        or record["status"] != "ready"
        or record["registry_sha256"] != registry_sha256
        or record["media_count"] != media_count
    ):
        _fail("external media CAS is not ready or registry-bound")
    for name in ("registry_sha256", "manifest_sha256", "inventory_sha256"):
        _digest(record[name], f"external media CAS {name}")
    return record


def _validate_git(
    value: object,
    *,
    authority: Mapping[str, str],
    bridge: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    fields = {
        "remote_main_before",
        "remote_main_after",
        "local",
        "target_ref",
        "target_sha",
        "target_tree",
        "target_is_ancestor",
        "policy",
        "policy_sha256",
    }
    section = _sealed(value, fields, "Git evidence")
    local = _exact_dict(
        section["local"],
        {
            "branch",
            "source_root",
            "head_sha",
            "head_tree",
            "origin",
            "remote_names",
            "origin_fetch_urls",
            "origin_push_urls",
            "owner_private",
            "standalone_object_database",
            "shallow",
            "dirty_entries",
            "ignored_entries",
            "unreachable_objects",
            "replace_refs",
            "special_index_entries",
            "sparse_index",
            "group_or_world_writable",
        },
        "bootstrap source trust",
    )
    source_root = local.get("source_root")
    source_path = Path(str(source_root))
    production_checkout = Path("/data/lzq/gith/nexpoly")
    if (
        not isinstance(source_root, str)
        or not source_path.is_absolute()
        or ".." in source_path.parts
        or source_path == production_checkout
        or production_checkout in source_path.parents
        or source_path == RUNTIME_ROOT
        or RUNTIME_ROOT in source_path.parents
    ):
        _fail("bootstrap source path is unsafe")
    if (
        section["remote_main_before"] != authority["sha"]
        or section["remote_main_after"] != authority["sha"]
        or local
        != {
            "branch": "main",
            "source_root": source_root,
            "head_sha": authority["sha"],
            "head_tree": authority["tree"],
            "origin": REPOSITORY_SSH_URL,
            "remote_names": ["origin"],
            "origin_fetch_urls": [REPOSITORY_SSH_URL],
            "origin_push_urls": [REPOSITORY_SSH_URL],
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
        }
    ):
        _fail("bootstrap source or remote main is not trusted")
    try:
        policy = bridge_deploy_core.validate_policy(section["policy"])
        relation = bridge_deploy_core.validate_relation(
            policy,
            authority_sha=authority["sha"],
            authority_tree=authority["tree"],
            remote_main=section["remote_main_after"],
            target_sha=section["target_sha"],
            target_tree=section["target_tree"],
            target_ref=section["target_ref"],
            is_ancestor=section["target_is_ancestor"],
        )
    except Exception as exc:
        raise ProductionReadinessError("bridge policy or relation is invalid") from exc
    if (
        relation["target_sha"] != bridge["sha"]
        or relation["target_tree"] != bridge["tree"]
        or section["policy_sha256"] != canonical_json_digest(policy)
    ):
        _fail("bridge policy identity differs from F/B authority")
    return section, policy


def _validate_ci(
    value: object,
    *,
    authority: Mapping[str, str],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    section = _sealed(
        value,
        {"authority_sha", "descriptor_ci_sha256", "jobs"},
        "CI evidence",
    )
    if section["authority_sha"] != authority["sha"]:
        _fail("CI authority differs")
    _digest(section["descriptor_ci_sha256"], "descriptor CI evidence")
    jobs = section["jobs"]
    if not isinstance(jobs, list) or not jobs:
        _fail("CI job inventory is empty")
    normalized: list[dict[str, Any]] = []
    for raw in jobs:
        record = _exact_dict(
            raw,
            {
                "name",
                "conclusion",
                "head_sha",
                "run_id",
                "attempt",
                "workflow_sha256",
            },
            "CI job",
        )
        if (
            not isinstance(record["name"], str)
            or not record["name"]
            or record["conclusion"] != "success"
            or record["head_sha"] != authority["sha"]
        ):
            _fail("CI job is not a successful exact-authority result")
        _positive_int(record["run_id"], "CI run ID")
        _positive_int(record["attempt"], "CI run attempt")
        _digest(record["workflow_sha256"], "CI workflow")
        normalized.append(record)
    names = [record["name"] for record in normalized]
    if (
        names != sorted(names)
        or len(names) != len(set(names))
        or set(names) != set(policy["required_ci_jobs"])
    ):
        _fail("CI job coverage differs from bridge policy")
    return section


def _image_record(
    value: object,
    *,
    role: str,
    revision: str,
    expected_ref: str | None,
) -> dict[str, Any]:
    record = _exact_dict(
        value,
        {
            "role",
            "digest_ref",
            "index_digest",
            "platform_digest",
            "image_id",
            "revision",
            "source",
            "version",
        },
        f"{role} image",
    )
    root = bridge_deploy_core.IMAGE_ROOTS[role]
    if (
        record["role"] != role
        or not isinstance(record["digest_ref"], str)
        or not record["digest_ref"].startswith(root + "@sha256:")
        or record["index_digest"] != record["digest_ref"].split("@", 1)[1]
        or record["revision"] != revision
        or record["source"] != REPOSITORY_SOURCE_URL
        or record["version"] != f"sha-{revision}"
        or expected_ref is not None
        and record["digest_ref"] != expected_ref
    ):
        _fail(f"{role} image authority differs")
    for name in ("index_digest", "platform_digest", "image_id"):
        _digest(record[name], f"{role} image {name}")
    return record


def _validate_oci(
    value: object,
    *,
    authority: Mapping[str, str],
    bridge: Mapping[str, str],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    section = _sealed(
        value,
        {
            "authority_sha",
            "bridge_sha",
            "authority_images",
            "bridge_images",
            "postgres_audit",
            "postgres_restore",
            "prefetch_images_sha256",
        },
        "OCI evidence",
    )
    if (
        section["authority_sha"] != authority["sha"]
        or section["bridge_sha"] != bridge["sha"]
    ):
        _fail("OCI source authority differs")
    _digest(section["prefetch_images_sha256"], "prefetch OCI image set")
    for group, revision, expected in (
        ("authority_images", authority["sha"], None),
        ("bridge_images", bridge["sha"], policy["target_images"]),
    ):
        images = _exact_dict(
            section[group],
            set(bridge_deploy_core.IMAGE_ROOTS),
            group,
        )
        for role in sorted(images):
            _image_record(
                images[role],
                role=role,
                revision=revision,
                expected_ref=None if expected is None else expected[role],
            )
    raw_postgres_audit = _exact_dict(
        section["postgres_audit"],
        set(POSTGRES_AUDIT_IMAGES),
        "PostgreSQL audit images",
    )
    postgres_audit: dict[str, dict[str, Any]] = {}
    for major, reference in sorted(POSTGRES_AUDIT_IMAGES.items()):
        record = _exact_dict(
            raw_postgres_audit[major],
            {"digest_ref", "index_digest", "platform_digest", "image_id"},
            f"PostgreSQL {major} audit image",
        )
        if (
            record["digest_ref"] != reference
            or record["index_digest"] != reference.split("@", 1)[1]
        ):
            _fail(f"PostgreSQL {major} audit image differs")
        for name in ("index_digest", "platform_digest", "image_id"):
            _digest(record[name], f"PostgreSQL {major} audit {name}")
        postgres_audit[major] = record
    postgres_restore = _exact_dict(
        section["postgres_restore"],
        {"digest_ref", "index_digest", "platform_digest", "image_id"},
        "PostgreSQL restore image",
    )
    if (
        postgres_restore != postgres_audit["16"]
        or postgres_restore["digest_ref"] != POSTGRES16_IMAGE
        or postgres_restore["index_digest"]
        != POSTGRES16_IMAGE.split("@", 1)[1]
    ):
        _fail(
            "PostgreSQL restore image is not the exact PG16 audit image"
        )
    for name in ("index_digest", "platform_digest", "image_id"):
        _digest(postgres_restore[name], f"PostgreSQL restore {name}")
    return section


def _validate_builder_proof(
    value: object,
    *,
    builder_source: Mapping[str, Any],
    authority: Mapping[str, str],
    bridge: Mapping[str, str],
) -> None:
    proof = _exact_dict(
        value,
        {
            "schema_version",
            "bundle_sha256",
            "builder",
            "target",
            "authority",
            "ancestry",
            "network_used",
            "temporary_clone_fsck",
            "proof_sha256",
        },
        "asset builder proof",
    )
    identity = {key: proof[key] for key in proof if key != "proof_sha256"}
    if (
        proof["schema_version"] != 1
        or proof["builder"] != builder_source
        or proof["target"] != dict(bridge)
        or proof["authority"] != dict(authority)
        or proof["ancestry"]
        != {
            "builder_to_target": True,
            "target_to_authority": True,
            "builder_to_authority": True,
        }
        or proof["network_used"] is not False
        or proof["temporary_clone_fsck"] is not True
        or proof["proof_sha256"] != canonical_json_digest(identity)
    ):
        _fail("asset builder proof differs from F/B authority")
    _digest(proof["bundle_sha256"], "asset proof bundle")


def _validate_asset(
    value: object,
    *,
    authority: Mapping[str, str],
    bridge: Mapping[str, str],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "manifest_digest",
        "manifest_sha256",
        "predecessor_asset_digest",
        "inventory_sha256",
        "file_count",
        "asset_tree_digests",
        "builder_source",
        "builder_proof",
        "datasets_on_asset_change",
        "database_effect",
        "live_pointer_start_sha256",
        "live_pointer_end_sha256",
        "release_validation_sha256",
        "prefetch_identity_sha256",
    }
    section = _sealed(value, fields, "schema-v2 asset evidence")
    builder = _exact_dict(
        section["builder_source"],
        {"repository", "script_path", "commit", "tree", "script_blob"},
        "asset builder source",
    )
    for name in ("commit", "tree", "script_blob"):
        _sha(builder[name], f"asset builder {name}")
    if (
        section["schema_version"] != 2
        or section["manifest_digest"] != policy["asset_manifest_digest"]
        or section["manifest_sha256"] != section["manifest_digest"]
        or section["predecessor_asset_digest"]
        != asset_release_contract.PREDECESSOR_ASSET_DIGEST
        or section["asset_tree_digests"]
        != asset_release_contract.ASSET_TREE_DIGESTS
        or section["datasets_on_asset_change"] != []
        or section["database_effect"] != "none"
        or section["live_pointer_start_sha256"]
        != section["live_pointer_end_sha256"]
        or builder["repository"] != asset_release_contract.BUILD_SOURCE_REPOSITORY
        or builder["script_path"] != asset_release_contract.BUILD_SOURCE_SCRIPT
    ):
        _fail("schema-v2 asset identity or no-database-effect seal differs")
    for name in (
        "manifest_digest",
        "manifest_sha256",
        "inventory_sha256",
        "live_pointer_start_sha256",
        "release_validation_sha256",
        "prefetch_identity_sha256",
    ):
        _digest(section[name], f"asset {name}")
    _positive_int(section["file_count"], "asset file count")
    _validate_builder_proof(
        section["builder_proof"],
        builder_source=builder,
        authority=authority,
        bridge=bridge,
    )
    return section


def _validate_prepared(
    value: object,
    *,
    authority: Mapping[str, str],
    bridge: Mapping[str, str],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    fields = {
        "operation_id",
        "status",
        "descriptor_schema_version",
        "descriptor_sha256",
        "ready_sha256",
        "authority_sha",
        "authority_tree",
        "target_sha",
        "target_tree",
        "policy_sha256",
        "prefetch_identity_sha256",
        "takeover_binding_sha256",
        "bridge_token_sha256",
        "descriptor_ci_sha256",
        "control_handoff_sha256",
    }
    section = _sealed(value, fields, "prepared deployment evidence")
    _operation_id(section["operation_id"], "prepared operation ID")
    if (
        section["status"] != "ready"
        or section["descriptor_schema_version"] != 3
        or section["authority_sha"] != authority["sha"]
        or section["authority_tree"] != authority["tree"]
        or section["target_sha"] != bridge["sha"]
        or section["target_tree"] != bridge["tree"]
        or section["policy_sha256"] != canonical_json_digest(policy)
    ):
        _fail("prepared deployment differs from F/B authority")
    for name in (
        "descriptor_sha256",
        "ready_sha256",
        "policy_sha256",
        "prefetch_identity_sha256",
        "takeover_binding_sha256",
        "bridge_token_sha256",
        "descriptor_ci_sha256",
        "control_handoff_sha256",
    ):
        _digest(section[name], f"prepared {name}")
    return section


def _validate_prefetch(
    value: object,
    *,
    authority: Mapping[str, str],
    bridge: Mapping[str, str],
    policy: Mapping[str, Any],
    asset: Mapping[str, Any],
    prepared: Mapping[str, Any],
) -> dict[str, Any]:
    fields = {
        "operation_id",
        "status",
        "identity_sha256",
        "ready_sha256",
        "authority_sha",
        "authority_tree",
        "target_sha",
        "target_tree",
        "policy_sha256",
        "asset_manifest_digest",
        "asset_evidence_sha256",
        "source_readiness_sha256",
        "recovery_tools_sha256",
        "git_bundle_sha256",
        "images_sha256",
        "wheel_caches_sha256",
    }
    section = _sealed(value, fields, "prefetch evidence")
    operation_id = _operation_id(section["operation_id"], "prefetch operation ID")
    if (
        not operation_id.startswith("prefetch-")
        or section["status"] != "ready"
        or section["identity_sha256"] != prepared["prefetch_identity_sha256"]
        or section["authority_sha"] != authority["sha"]
        or section["authority_tree"] != authority["tree"]
        or section["target_sha"] != bridge["sha"]
        or section["target_tree"] != bridge["tree"]
        or section["policy_sha256"] != canonical_json_digest(policy)
        or section["asset_manifest_digest"] != asset["manifest_digest"]
        or section["asset_evidence_sha256"]
        != asset["prefetch_identity_sha256"]
    ):
        _fail("prefetch differs from prepared F/B assets")
    for name in fields - {
        "operation_id",
        "status",
        "authority_sha",
        "authority_tree",
        "target_sha",
        "target_tree",
        "asset_manifest_digest",
    }:
        _digest(section[name], f"prefetch {name}")
    return section


def _validate_helpers(
    value: object,
    *,
    authority: Mapping[str, str],
) -> dict[str, Any]:
    section = _sealed(
        value,
        {
            "status",
            "installation_sha256",
            "required_helpers",
            "control_source_sha",
            "control_source_tree",
            "control_release_id",
            "control_manifest_sha256",
            "entrypoint_sha256",
        },
        "site helper evidence",
    )
    if (
        section["status"] != "ready"
        or section["required_helpers"] != sorted(site_helper_contracts.HELPERS)
        or section["control_source_sha"] != authority["sha"]
        or section["control_source_tree"] != authority["tree"]
        or not isinstance(section["control_release_id"], str)
        or RELEASE_ID_RE.fullmatch(section["control_release_id"]) is None
    ):
        _fail("site helper/control inventory is incomplete")
    for name in (
        "installation_sha256",
        "control_manifest_sha256",
        "entrypoint_sha256",
    ):
        _digest(section[name], f"site helper/control {name}")
    return section


def _validate_takeover(
    value: object,
    *,
    authority: Mapping[str, str],
    prepared: Mapping[str, Any],
) -> dict[str, Any]:
    section = _sealed(
        value,
        {
            "operation_id",
            "status",
            "authority_sha",
            "authority_tree",
            "binding_sha256",
            "bootstrap_control_sha256",
            "bootstrap_transaction_sha256",
            "bootstrap_transaction_identity_sha256",
        },
        "legacy takeover evidence",
    )
    _operation_id(section["operation_id"], "legacy takeover operation ID")
    if (
        section["status"] != "completed"
        or section["authority_sha"] != authority["sha"]
        or section["authority_tree"] != authority["tree"]
        or section["binding_sha256"] != prepared["takeover_binding_sha256"]
    ):
        _fail("legacy takeover completion differs")
    _digest(section["binding_sha256"], "legacy takeover binding")
    for name in (
        "bootstrap_control_sha256",
        "bootstrap_transaction_sha256",
        "bootstrap_transaction_identity_sha256",
    ):
        _digest(section[name], f"legacy takeover {name}")
    return section


def _validate_alias(value: object) -> dict[str, Any]:
    section = _sealed(
        value,
        {
            "operation_id",
            "status",
            "completed_marker_sha256",
            "backup_sha256",
            "restore_audit_sha256",
            "postgres_system_identifier_sha256",
        },
        "0005 alias evidence",
    )
    _operation_id(section["operation_id"], "alias operation ID")
    if section["status"] != "completed":
        _fail("0005 alias operation is incomplete")
    for name in (
        "completed_marker_sha256",
        "backup_sha256",
        "restore_audit_sha256",
        "postgres_system_identifier_sha256",
    ):
        _digest(section[name], f"alias {name}")
    return section


def _validate_external_media(value: object) -> dict[str, Any]:
    fields = {
        "status",
        "captured_at",
        "audit_relative_path",
        "audit_sha256",
        "validation_sha256",
        "media_authority_rules_sha256",
        "registry_sha256",
        "inventory_complete",
        "writable_target",
        "requires_0014",
        "media_count",
        "cas",
    }
    section = _sealed(value, fields, "external media evidence")
    if (
        section["status"] != "ready"
        or section["audit_relative_path"]
        != EXTERNAL_DATABASE_AUDIT_RELATIVE_PATH.as_posix()
        or section["inventory_complete"] is not True
        or section["writable_target"]
        != {"stack": "production", "database": "nexpoly"}
        or section["requires_0014"] is not False
    ):
        _fail("external database/media audit is not deployable")
    _utc_timestamp(section["captured_at"], "external media capture time")
    for name in (
        "audit_sha256",
        "validation_sha256",
        "media_authority_rules_sha256",
        "registry_sha256",
    ):
        _digest(section[name], f"external media {name}")
    _positive_int(section["media_count"], "external media count")
    cas = section["cas"]
    if cas is not None:
        _validate_media_cas(
            cas,
            registry_sha256=section["registry_sha256"],
            media_count=section["media_count"],
        )
    return section


def _validate_postgres(
    value: object,
    *,
    alias: Mapping[str, Any],
) -> dict[str, Any]:
    fields = {
        "status",
        "container_id_sha256",
        "image_digest",
        "volume_identity_sha256",
        "system_identifier_sha256",
        "ledger_source_sha256",
        "running",
        "unchanged_from_alias",
        "read_only_probe",
    }
    section = _sealed(value, fields, "PostgreSQL runtime evidence")
    if (
        section["status"] != "ready"
        or section["running"] is not True
        or section["unchanged_from_alias"] is not True
        or section["read_only_probe"] is not True
        or section["system_identifier_sha256"]
        != alias["postgres_system_identifier_sha256"]
    ):
        _fail("PostgreSQL runtime identity is not preserved")
    for name in (
        "container_id_sha256",
        "image_digest",
        "volume_identity_sha256",
        "system_identifier_sha256",
        "ledger_source_sha256",
    ):
        _digest(section[name], f"PostgreSQL {name}")
    return section


def _validate_migrations(
    value: object,
    *,
    policy: Mapping[str, Any],
    postgres: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    fields = {
        "records",
        "ledger_sha256",
        "manifest_sha256",
        "registry_name",
        "0012_applied",
        "0013_applied",
    }
    section = _sealed(value, fields, "migration evidence")
    records = section["records"]
    if not isinstance(records, list) or not records:
        _fail("migration ledger is empty")
    for raw in records:
        record = _exact_dict(raw, {"version", "checksum"}, "migration record")
        if (
            not isinstance(record["version"], str)
            or MIGRATION_RE.fullmatch(record["version"]) is None
            or not isinstance(record["checksum"], str)
            or re.fullmatch(r"^[0-9a-f]{64}$", record["checksum"]) is None
        ):
            _fail("migration record is malformed")
    try:
        matched = bridge_deploy_core.match_migration_ledger(
            policy["accepted_migration_ledgers"],
            records,
        )
    except Exception as exc:
        raise ProductionReadinessError(
            "migration ledger is outside B/F compatibility"
        ) from exc
    expected_flags = {
        "pre-0012": (False, False),
        "post-0012": (True, False),
        "post-0013": (True, True),
    }[matched["name"]]
    if (
        section["ledger_sha256"] != matched["ledger_sha256"]
        or section["manifest_sha256"] != matched["manifest_sha256"]
        or section["registry_name"] != matched["name"]
        or (section["0012_applied"], section["0013_applied"]) != expected_flags
        or section["ledger_sha256"] != postgres["ledger_source_sha256"]
    ):
        _fail("migration state differs from frozen compatibility registry")
    return section, matched


def _validate_mutable_data(
    value: object,
    *,
    postgres: Mapping[str, Any],
    migrations: Mapping[str, Any],
) -> dict[str, Any]:
    fields = {
        "operation_id",
        "status",
        "captured_at",
        "audit_relative_path",
        "audit_sha256",
        "validation_sha256",
        "snapshot_sha256",
        "business_tables_sha256",
        "static_tables_sha256",
        "postgres_runtime_sha256",
        "system_identifier_sha256",
        "migration_ledger_sha256",
        "transaction_read_only",
    }
    section = _sealed(value, fields, "mutable-data evidence")
    _operation_id(section["operation_id"], "mutable-data operation ID")
    if (
        section["status"] != "ready"
        or section["audit_relative_path"]
        != MUTABLE_DATA_AUDIT_RELATIVE_PATH.as_posix()
        or section["transaction_read_only"] is not True
        or section["system_identifier_sha256"]
        != postgres["system_identifier_sha256"]
        or section["migration_ledger_sha256"]
        != migrations["ledger_sha256"]
    ):
        _fail("mutable-data seal differs from PostgreSQL or migration state")
    _utc_timestamp(section["captured_at"], "mutable-data capture time")
    for name in (
        "audit_sha256",
        "validation_sha256",
        "snapshot_sha256",
        "business_tables_sha256",
        "static_tables_sha256",
        "postgres_runtime_sha256",
        "system_identifier_sha256",
        "migration_ledger_sha256",
    ):
        _digest(section[name], f"mutable-data {name}")
    return section


def _validate_native_runtime(
    value: object,
    *,
    authority: Mapping[str, str],
    oci: Mapping[str, Any],
) -> dict[str, Any]:
    fields = {
        "status",
        "authority_sha",
        "python_version",
        "uv_version",
        "build_lock_sha256",
        "wheel_filename",
        "wheel_sha256",
        "wheel_inventory_sha256",
        "record_sha256",
        "aimnet_source",
        "model_registry_sha256",
        "models_sha256",
        "prefetch_wheel_caches_sha256",
        "gpu_acceptance",
    }
    section = _sealed(value, fields, "native Worker runtime evidence")
    runtime_lock = monomer_dft_runtime_contract.RUNTIME_CONTRACT
    wheel_lock = runtime_lock["wheel"]
    source_lock = runtime_lock["source"]
    if (
        section["status"] != "ready"
        or section["authority_sha"] != authority["sha"]
        or not isinstance(section["python_version"], str)
        or PYTHON_VERSION_RE.fullmatch(section["python_version"]) is None
        or ".".join(section["python_version"].split(".")[:2])
        != runtime_lock["python_minor"]
        or not isinstance(section["uv_version"], str)
        or UV_VERSION_RE.fullmatch(section["uv_version"]) is None
        or section["uv_version"] != runtime_lock["uv_version"]
        or not isinstance(section["wheel_filename"], str)
        or WHEEL_RE.fullmatch(section["wheel_filename"]) is None
        or section["build_lock_sha256"] != runtime_lock["build_lock_sha256"]
        or section["wheel_filename"] != wheel_lock["filename"]
        or section["wheel_sha256"] != wheel_lock["sha256"]
        or section["wheel_inventory_sha256"] != wheel_lock["inventory_sha256"]
        or section["record_sha256"] != wheel_lock["record_sha256"]
        or section["model_registry_sha256"] != runtime_lock["registry_sha256"]
        or section["models_sha256"] != runtime_lock["models_sha256"]
    ):
        _fail("native Worker build identity differs from the fixed AIMNet runtime lock")
    for name in (
        "build_lock_sha256",
        "wheel_sha256",
        "wheel_inventory_sha256",
        "record_sha256",
        "model_registry_sha256",
        "models_sha256",
        "prefetch_wheel_caches_sha256",
    ):
        _digest(section[name], f"native runtime {name}")
    source = _exact_dict(
        section["aimnet_source"],
        {"commit", "tree", "archive_sha256"},
        "AIMNet source",
    )
    _sha(source["commit"], "AIMNet commit")
    _sha(source["tree"], "AIMNet tree")
    _digest(source["archive_sha256"], "AIMNet archive")
    if (
        source["commit"] != source_lock["commit"]
        or source["tree"] != source_lock["tree"]
    ):
        _fail("AIMNet source differs from the fixed runtime lock")
    acceptance = _exact_dict(
        section["gpu_acceptance"],
        {
            "status",
            "authority_tree",
            "image_digest",
            "model_registry_sha256",
            "gpus",
            "production_gpu_2_touched",
            "report_sha256",
        },
        "GPU acceptance",
    )
    authority_backend = oci["authority_images"]["backend"]["index_digest"]
    if (
        acceptance["status"] != "passed"
        or acceptance["authority_tree"] != authority["tree"]
        or acceptance["image_digest"] != authority_backend
        or acceptance["model_registry_sha256"]
        != section["model_registry_sha256"]
        or acceptance["gpus"] != [1, 3]
        or acceptance["production_gpu_2_touched"] is not False
    ):
        _fail("GPU acceptance is not bound to F or touched production GPU2")
    _digest(acceptance["report_sha256"], "GPU acceptance report")
    return section


def _validate_capacity(value: object) -> dict[str, Any]:
    fields = {
        "status",
        "disk_bytes_available",
        "disk_bytes_required",
        "memory_bytes_available",
        "memory_bytes_required",
        "wheel_cache_bytes",
        "asset_release_bytes",
        "backup_bytes_required",
    }
    section = _sealed(value, fields, "capacity evidence")
    for name in fields - {"status"}:
        _positive_int(section[name], f"capacity {name}")
    if (
        section["status"] != "sufficient"
        or section["disk_bytes_available"] < section["disk_bytes_required"]
        or section["memory_bytes_available"] < section["memory_bytes_required"]
        or section["disk_bytes_required"]
        < section["wheel_cache_bytes"]
        + section["asset_release_bytes"]
        + section["backup_bytes_required"]
    ):
        _fail("maintenance capacity is insufficient or incomplete")
    return section


def _validate_conflicts(value: object) -> dict[str, Any]:
    fields = {
        "deploy",
        "contract_0012",
        "alias",
        "takeover",
        "bridge",
        "prepared",
        "control_handoff",
    }
    section = _sealed(value, fields, "conflict evidence")
    for name in fields:
        if section[name] != []:
            _fail(f"{name} conflict marker exists")
    return section


def _validate_observation(value: object) -> dict[str, Any]:
    fields = {
        "collector_sha256",
        "production_command_read_only",
        "git_fetch_used",
        "image_pull_used",
        "container_mutation_used",
        "service_mutation_used",
        "state_write_used",
        "database_transaction_read_only",
    }
    section = _sealed(value, fields, "observation method")
    _digest(section["collector_sha256"], "observation collector")
    if (
        section["production_command_read_only"] is not True
        or section["git_fetch_used"] is not False
        or section["image_pull_used"] is not False
        or section["container_mutation_used"] is not False
        or section["service_mutation_used"] is not False
        or section["state_write_used"] is not False
        or section["database_transaction_read_only"] is not True
    ):
        _fail("observation was not strictly read-only")
    return section


def validate_evidence(
    document: object,
    *,
    expected_authority: str,
    expected_bridge: str,
    enforce_freshness: bool,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Validate and cross-bind one complete readiness observation envelope."""

    value = _exact_dict(document, TOP_FIELDS, "production readiness evidence")
    if value["schema_version"] != SCHEMA_VERSION:
        _fail("production readiness schema is unsupported")
    captured_at = _utc_timestamp(value["captured_at"], "capture time")
    current = now or dt.datetime.now(dt.timezone.utc)
    if (
        captured_at > current + dt.timedelta(minutes=1)
        or enforce_freshness
        and current - captured_at > MAX_LIVE_EVIDENCE_AGE
    ):
        _fail("production readiness evidence is stale")
    authority = _source_record(value["authority"], "authority F")
    bridge = _source_record(value["bridge"], "bridge B")
    if (
        authority["sha"] != _sha(expected_authority, "expected authority F")
        or bridge["sha"] != _sha(expected_bridge, "expected bridge B")
        or authority["sha"] == bridge["sha"]
    ):
        _fail("requested F/B authority differs from evidence")
    git, policy = _validate_git(
        value["git"],
        authority=authority,
        bridge=bridge,
    )
    ci = _validate_ci(value["ci"], authority=authority, policy=policy)
    oci = _validate_oci(
        value["oci"],
        authority=authority,
        bridge=bridge,
        policy=policy,
    )
    asset = _validate_asset(
        value["asset"],
        authority=authority,
        bridge=bridge,
        policy=policy,
    )
    prepared = _validate_prepared(
        value["prepared"],
        authority=authority,
        bridge=bridge,
        policy=policy,
    )
    prefetch = _validate_prefetch(
        value["prefetch"],
        authority=authority,
        bridge=bridge,
        policy=policy,
        asset=asset,
        prepared=prepared,
    )
    helpers = _validate_helpers(value["helpers"], authority=authority)
    takeover = _validate_takeover(
        value["takeover"],
        authority=authority,
        prepared=prepared,
    )
    alias = _validate_alias(value["alias"])
    external_media = _validate_external_media(value["external_media"])
    external_policy = policy.get("external_database_audit")
    if (
        not isinstance(external_policy, dict)
        or external_media["media_authority_rules_sha256"]
        != external_policy.get("media_authority_rules_sha256")
    ):
        _fail("external media authority differs from bridge policy")
    postgres = _validate_postgres(value["postgres"], alias=alias)
    migrations, migration = _validate_migrations(
        value["migrations"],
        policy=policy,
        postgres=postgres,
    )
    mutable_data = _validate_mutable_data(
        value["mutable_data"],
        postgres=postgres,
        migrations=migrations,
    )
    for label, section in (
        ("external media", external_media),
        ("mutable-data", mutable_data),
    ):
        section_time = _utc_timestamp(
            section["captured_at"],
            f"{label} capture time",
        )
        age = captured_at - section_time
        if age < -dt.timedelta(minutes=1) or age > MAX_LIVE_EVIDENCE_AGE:
            _fail(f"{label} evidence is stale")
    native_runtime = _validate_native_runtime(
        value["native_runtime"],
        authority=authority,
        oci=oci,
    )
    capacity = _validate_capacity(value["capacity"])
    conflicts = _validate_conflicts(value["conflicts"])
    observation = _validate_observation(value["observation"])
    if (
        prepared["prefetch_identity_sha256"] != prefetch["identity_sha256"]
        or prepared["takeover_binding_sha256"] != takeover["binding_sha256"]
        or prepared["descriptor_ci_sha256"] != ci["descriptor_ci_sha256"]
        or oci["prefetch_images_sha256"] != prefetch["images_sha256"]
        or native_runtime["prefetch_wheel_caches_sha256"]
        != prefetch["wheel_caches_sha256"]
        or mutable_data["operation_id"] != prepared["operation_id"]
    ):
        _fail("prepared dependencies differ")
    identity = {
        key: value[key]
        for key in sorted(TOP_FIELDS - {"evidence_sha256"})
    }
    if value["evidence_sha256"] != canonical_json_digest(identity):
        _fail("top-level readiness evidence seal differs")
    return {
        "document": value,
        "captured_at": captured_at,
        "authority": authority,
        "bridge": bridge,
        "policy": policy,
        "migration": migration,
        "sections": {
            "git": git,
            "ci": ci,
            "oci": oci,
            "asset": asset,
            "prepared": prepared,
            "prefetch": prefetch,
            "helpers": helpers,
            "takeover": takeover,
            "alias": alias,
            "external_media": external_media,
            "postgres": postgres,
            "migrations": migrations,
            "mutable_data": mutable_data,
            "native_runtime": native_runtime,
            "capacity": capacity,
            "conflicts": conflicts,
            "observation": observation,
        },
    }


def _read_json_file(
    path: Path,
    *,
    private: bool,
    maximum_bytes: int = MAX_EVIDENCE_BYTES,
) -> tuple[dict[str, Any], str]:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > maximum_bytes:
                _fail("required evidence file is unsafe")
        after = os.fstat(descriptor)
        current = path.lstat()
    except OSError as exc:
        raise ProductionReadinessError("required evidence is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    payload = b"".join(chunks)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size > maximum_bytes
        or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        or (
            current.st_dev,
            current.st_ino,
            current.st_mode,
            current.st_size,
            current.st_mtime_ns,
            current.st_ctime_ns,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        or not payload
        or len(payload) > maximum_bytes
        or private
        and (
            before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
        )
    ):
        _fail("required evidence file is unsafe")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionReadinessError("required evidence is invalid JSON") from exc
    if not isinstance(document, dict):
        _fail("required evidence is not a JSON object")
    return document, sha256_bytes(payload)


def _require_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProductionReadinessError(
            "required readiness directory is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        _fail("required readiness directory is unsafe")


def _runtime_path(runtime_root: Path, relative: Path) -> Path:
    path = runtime_root / relative
    if path.parent != runtime_root and runtime_root not in path.parents:
        _fail("runtime evidence path escapes its root")
    return path


def _validate_live_prepared(
    runtime_root: Path,
    validated: Mapping[str, Any],
) -> None:
    section = validated["sections"]["prepared"]
    operation = _runtime_path(
        runtime_root,
        Path("state/prepared") / section["operation_id"],
    )
    descriptor_path = operation / "descriptor.json"
    ready_path = operation / "ready.json"
    descriptor, descriptor_file_digest = _read_json_file(
        descriptor_path,
        private=True,
    )
    ready, ready_file_digest = _read_json_file(ready_path, private=True)
    if (
        descriptor_file_digest != section["descriptor_sha256"]
        or ready_file_digest != section["ready_sha256"]
    ):
        _fail("prepared deployment files changed")
    try:
        pull_deploy_controller = _load_sibling(
            "nexpoly_readiness_pull_deploy_controller",
            "pull_deploy_controller.py",
        )
        normalized = pull_deploy_controller.validate_descriptor(descriptor)
    except Exception as exc:
        raise ProductionReadinessError(
            "prepared descriptor validation failed"
        ) from exc
    expected_ready_fields = {
        "schema_version",
        "status",
        "operation_id",
        "source_sha",
        "descriptor_sha256",
        "executor_control",
        "executor_control_sha256",
        "slot_record_sha256",
        "prepared_at",
    }
    if (
        normalized.get("schema_version") != 3
        or normalized.get("operation_id") != section["operation_id"]
        or normalized["bridge"]["authority"]["sha"]
        != validated["authority"]["sha"]
        or normalized["bridge"]["authority"]["tree"]
        != validated["authority"]["tree"]
        or normalized["bridge"]["target"]["sha"] != validated["bridge"]["sha"]
        or normalized["bridge"]["target"]["tree"] != validated["bridge"]["tree"]
        or normalized["bridge"]["policy_sha256"] != section["policy_sha256"]
        or normalized["prefetch"]["identity_sha256"]
        != section["prefetch_identity_sha256"]
        or normalized["legacy_takeover"]["binding_sha256"]
        != section["takeover_binding_sha256"]
        or canonical_json_digest(normalized["ci"])
        != section["descriptor_ci_sha256"]
        or not isinstance(ready, dict)
        or set(ready) != expected_ready_fields
        or ready.get("schema_version") != 1
        or ready.get("status") != "ready"
        or ready.get("operation_id") != section["operation_id"]
        or ready.get("source_sha") != validated["bridge"]["sha"]
        or ready.get("descriptor_sha256") != section["descriptor_sha256"]
        or ready.get("executor_control")
        != normalized["controller"]["executor_control"]
        or ready.get("executor_control_sha256")
        != normalized["controller"]["executor_control_sha256"]
        or ready.get("slot_record_sha256")
        != normalized["monomer_md"]["slot_record_sha256"]
    ):
        _fail("prepared descriptor or READY binding differs")
    handoff_directory = _runtime_path(
        runtime_root,
        Path("state/control-handoffs"),
    )
    _require_private_directory(handoff_directory)
    expected_handoff_name = f"{section['operation_id']}.json"
    if {
        entry.name
        for entry in handoff_directory.iterdir()
    } != {expected_handoff_name}:
        _fail("control handoff inventory differs from prepared operation")
    handoff_path = handoff_directory / expected_handoff_name
    handoff, handoff_file_digest = _read_json_file(
        handoff_path,
        private=True,
    )
    handoff_fields = {
        "schema_version",
        "protocol_version",
        "operation_id",
        "authority_sha",
        "authority_tree",
        "target_sha",
        "target_tree",
        "target_ref",
        "policy_id",
        "policy_sha256",
        "prefetch_operation_id",
        "prefetch",
        "legacy_takeover",
        "previous_active_control",
        "previous_active_control_sha256",
        "executor_control",
        "executor_control_sha256",
        "created_at",
    }
    bridge = normalized["bridge"]
    if (
        handoff_file_digest != section["control_handoff_sha256"]
        or not isinstance(handoff, dict)
        or set(handoff) != handoff_fields
        or handoff.get("schema_version") != 2
        or handoff.get("protocol_version")
        != pull_deploy_controller._control_runtime.PROTOCOL_VERSION
        or handoff.get("operation_id") != section["operation_id"]
        or handoff.get("authority_sha") != validated["authority"]["sha"]
        or handoff.get("authority_tree") != validated["authority"]["tree"]
        or handoff.get("target_sha") != validated["bridge"]["sha"]
        or handoff.get("target_tree") != validated["bridge"]["tree"]
        or handoff.get("target_ref") != bridge["target"]["exact_ref"]
        or handoff.get("policy_id") != bridge["policy"]["policy_id"]
        or handoff.get("policy_sha256") != section["policy_sha256"]
        or handoff.get("prefetch_operation_id")
        != normalized["prefetch"]["operation_id"]
        or handoff.get("prefetch") != normalized["prefetch"]
        or handoff.get("legacy_takeover")
        != normalized["legacy_takeover"]
        or handoff.get("previous_active_control")
        != normalized["controller"]["previous_active_control"]
        or canonical_json_digest(handoff.get("previous_active_control"))
        != handoff.get("previous_active_control_sha256")
        or handoff.get("executor_control")
        != normalized["controller"]["executor_control"]
        or canonical_json_digest(handoff.get("executor_control"))
        != handoff.get("executor_control_sha256")
        or not isinstance(handoff.get("created_at"), str)
        or not handoff["created_at"]
    ):
        _fail("selector control handoff differs from prepared descriptor")
    try:
        token = bridge_deploy_core.load_token_authority(runtime_root / "state")
    except Exception as exc:
        raise ProductionReadinessError("bridge token is unavailable") from exc
    if (
        token.get("operation_id") != section["operation_id"]
        or token.get("policy_id") != validated["policy"]["policy_id"]
        or token.get("descriptor_sha256") != section["descriptor_sha256"]
        or bridge_deploy_core.token_record_digest(token)
        != section["bridge_token_sha256"]
        or token.get("status") != "prepared"
    ):
        _fail("bridge token differs from the prepared operation")


def _validate_live_prefetch(
    runtime_root: Path,
    validated: Mapping[str, Any],
) -> None:
    section = validated["sections"]["prefetch"]
    path = _runtime_path(
        runtime_root,
        Path("prefetch") / section["operation_id"] / "ready.json",
    )
    ready, file_digest = _read_json_file(path, private=True)
    if (
        file_digest != section["ready_sha256"]
        or ready.get("schema_version") != 2
        or ready.get("status") != "ready"
        or ready.get("operation_id") != section["operation_id"]
        or ready.get("identity_sha256") != section["identity_sha256"]
        or ready.get("source")
        != {
            "authority": dict(validated["authority"]),
            "target": dict(validated["bridge"]),
        }
        or ready.get("policy_sha256") != section["policy_sha256"]
        or not isinstance(ready.get("asset"), dict)
        or ready["asset"].get("identity_sha256")
        != section["asset_evidence_sha256"]
        or not isinstance(ready.get("source_readiness"), dict)
        or ready["source_readiness"].get("ready") is not True
        or ready["source_readiness"].get("source_root")
        != validated["sections"]["git"]["local"]["source_root"]
        or ready["source_readiness"].get("owner_private") is not True
        or ready["source_readiness"].get("standalone_object_database") is not True
        or ready["source_readiness"].get("dirty_entries") != 0
        or ready["source_readiness"].get("ignored_entries") != 0
        or ready["source_readiness"].get("unreachable_objects") != 0
        or ready.get("source_readiness_sha256")
        != section["source_readiness_sha256"]
        or canonical_json_digest(ready.get("recovery_tools"))
        != section["recovery_tools_sha256"]
        or canonical_json_digest(ready.get("images"))
        != section["images_sha256"]
        or canonical_json_digest(ready.get("wheel_caches"))
        != section["wheel_caches_sha256"]
        or not isinstance(ready.get("git_bundle"), dict)
        or ready["git_bundle"].get("sha256") != section["git_bundle_sha256"]
    ):
        _fail("prefetch READY binding differs")
    identity = {
        key: ready[key]
        for key in sorted(set(ready) - {"identity_sha256"})
    }
    if canonical_json_digest(identity) != ready["identity_sha256"]:
        _fail("prefetch READY identity differs")
    image_groups = (
        ("authority", "authority_images"),
        ("target", "bridge_images"),
    )
    oci = validated["sections"]["oci"]
    raw_images = ready["images"]
    for raw_group, summary_group in image_groups:
        for role in sorted(bridge_deploy_core.IMAGE_ROOTS):
            raw = raw_images[raw_group][role]
            summary = oci[summary_group][role]
            if (
                summary["digest_ref"] != raw.get("digest_ref")
                or summary["index_digest"]
                != raw.get("oci_reference_digest")
                or summary["image_id"] != raw.get("local_image_id")
                or summary["revision"] != raw.get("revision")
                or summary["source"] != raw.get("source")
                or summary["version"] != raw.get("version")
            ):
                _fail("prefetched OCI image summary differs")
    raw_postgres_audit = raw_images["postgres_audit"]
    for major in sorted(POSTGRES_AUDIT_IMAGES):
        raw_postgres = raw_postgres_audit[major]
        summary_postgres = oci["postgres_audit"][major]
        if (
            summary_postgres["digest_ref"]
            != raw_postgres.get("digest_ref")
            or summary_postgres["index_digest"]
            != raw_postgres.get("oci_reference_digest")
            or summary_postgres["image_id"]
            != raw_postgres.get("local_image_id")
        ):
            _fail(
                f"prefetched PostgreSQL {major} audit image summary differs"
            )
    if oci["postgres_restore"] != oci["postgres_audit"]["16"]:
        _fail("PostgreSQL restore summary is not the PG16 audit image")


def _validate_live_helpers(
    runtime_root: Path,
    validated: Mapping[str, Any],
) -> None:
    section = validated["sections"]["helpers"]
    try:
        report = site_helper_contracts.inspect_helper_installation(runtime_root)
    except Exception as exc:
        raise ProductionReadinessError("site helper inspection failed") from exc
    if (
        not isinstance(report, dict)
        or canonical_json_digest(report)
        != section["installation_sha256"]
    ):
        _fail("installed site helpers changed")
    helpers = report.get("helpers")
    collector = (
        helpers.get("production-readiness-collector")
        if isinstance(helpers, dict)
        else None
    )
    if (
        not isinstance(collector, dict)
        or collector.get("sha256")
        != validated["sections"]["observation"]["collector_sha256"]
    ):
        _fail("installed readiness collector differs from observation")
    try:
        selector = _load_python_module(
            "nexpoly_readiness_control_runtime_selector",
            runtime_root / "bin/control_runtime_selector.py",
            exact_mode=0o700,
        )
        active, manifest, root = selector.load_active_control(runtime_root)
    except Exception as exc:
        raise ProductionReadinessError(
            "active control release validation failed"
        ) from exc
    entrypoint = manifest["entrypoints"].get("production-readiness")
    if (
        active["source_sha"] != validated["authority"]["sha"]
        or active["source_tree"] != validated["authority"]["tree"]
        or active["release_id"] != section["control_release_id"]
        or manifest["source_sha"] != section["control_source_sha"]
        or manifest["source_tree"] != section["control_source_tree"]
        or manifest["release_id"] != section["control_release_id"]
        or sha256_file(root / selector.CONTROL_MANIFEST_NAME)
        != section["control_manifest_sha256"]
        or not isinstance(entrypoint, dict)
        or entrypoint.get("kind") != "python"
        or entrypoint.get("file") != "production_readiness.py"
        or sha256_file(root / "production_readiness.py")
        != section["entrypoint_sha256"]
        or os.environ.get("NEXPOLY_ACTIVE_CONTROL_ROOT") != str(root)
        or os.environ.get("NEXPOLY_ACTIVE_CONTROL_RELEASE_ID")
        != section["control_release_id"]
    ):
        _fail("running readiness control is not the exact F release")


def _validate_live_takeover(
    runtime_root: Path,
    validated: Mapping[str, Any],
) -> None:
    section = validated["sections"]["takeover"]
    try:
        legacy_takeover_evidence = _load_sibling(
            "nexpoly_readiness_legacy_takeover_evidence",
            "legacy_takeover_evidence.py",
        )
        binding = legacy_takeover_evidence.validate_completed(
            runtime_root,
            section["operation_id"],
            validated["authority"]["sha"],
            validated["authority"]["tree"],
        )
    except Exception as exc:
        raise ProductionReadinessError(
            "legacy takeover completion validation failed"
        ) from exc
    if binding.get("binding_sha256") != section["binding_sha256"]:
        _fail("legacy takeover binding changed")
    bootstrap_path = _runtime_path(
        runtime_root,
        Path("state/bootstrap-control.json"),
    )
    bootstrap, bootstrap_digest = _read_json_file(
        bootstrap_path,
        private=True,
    )
    child_directory = _runtime_path(
        runtime_root,
        Path("state/legacy-takeover/bootstrap-children"),
    )
    _require_private_directory(child_directory)
    child_name = (
        f"{section['operation_id']}-{validated['authority']['sha']}.json"
    )
    if {entry.name for entry in child_directory.iterdir()} != {child_name}:
        _fail("bootstrap child transaction inventory differs")
    child_path = child_directory / child_name
    child, child_digest = _read_json_file(child_path, private=True)
    try:
        bootstrap_pull_deploy = _load_sibling(
            "nexpoly_readiness_bootstrap_pull_deploy",
            "bootstrap_pull_deploy.py",
        )
        child = bootstrap_pull_deploy._validate_bootstrap_transaction(
            child,
            path=child_path,
        )
    except Exception as exc:
        raise ProductionReadinessError(
            "bootstrap child transaction validation failed"
        ) from exc
    active_path = _runtime_path(
        runtime_root,
        Path("state/active-control.json"),
    )
    active, active_digest = _read_json_file(active_path, private=True)
    authority_commit = child.get("step_evidence", {}).get(
        "authority_commit"
    )
    if (
        bootstrap_digest != section["bootstrap_control_sha256"]
        or child_digest != section["bootstrap_transaction_sha256"]
        or child.get("identity_sha256")
        != section["bootstrap_transaction_identity_sha256"]
        or child.get("status") != "completed"
        or child.get("phase") != "completed"
        or child.get("operation_id") != section["operation_id"]
        or child.get("source_sha") != validated["authority"]["sha"]
        or child.get("source_tree") != validated["authority"]["tree"]
        or not isinstance(child.get("identity"), dict)
        or child["identity"].get("legacy_takeover") != binding
        or not isinstance(authority_commit, dict)
        or authority_commit.get("bootstrap_control_sha256")
        != bootstrap_digest
        or authority_commit.get("active_control_sha256") != active_digest
        or authority_commit.get("active_control") != active
        or not isinstance(bootstrap, dict)
        or bootstrap.get("schema_version") != 2
        or bootstrap.get("status") != "completed"
        or bootstrap.get("source_sha") != validated["authority"]["sha"]
        or bootstrap.get("source_tree") != validated["authority"]["tree"]
        or bootstrap.get("legacy_takeover") != binding
        or bootstrap.get("active_control") != active
    ):
        _fail("bootstrap child transaction differs from F/takeover authority")


def _validate_live_alias(
    runtime_root: Path,
    validated: Mapping[str, Any],
) -> None:
    section = validated["sections"]["alias"]
    try:
        control_runtime_selector = _load_python_module(
            "nexpoly_readiness_control_runtime_selector",
            runtime_root / "bin/control_runtime_selector.py",
            exact_mode=0o700,
        )
        marker = control_runtime_selector.load_production_0005_alias_gate(
            runtime_root,
            require_completed=True,
        )
    except Exception as exc:
        raise ProductionReadinessError(
            "0005 alias completion validation failed"
        ) from exc
    identity = marker.get("identity") if isinstance(marker, dict) else None
    backup = marker.get("database_backup") if isinstance(marker, dict) else None
    backup_digest = (
        backup.get("dump_sha256") if isinstance(backup, dict) else None
    )
    audit_digest = (
        marker.get("audit_manifest_sha256")
        if isinstance(marker, dict)
        else None
    )
    if (
        not isinstance(identity, dict)
        or identity.get("operation_id") != section["operation_id"]
        or canonical_json_digest(marker) != section["completed_marker_sha256"]
        or "sha256:" + str(backup_digest) != section["backup_sha256"]
        or "sha256:" + str(audit_digest)
        != section["restore_audit_sha256"]
        or sha256_bytes(
            str(identity.get("database_system_identifier", "")).encode("ascii")
        )
        != section["postgres_system_identifier_sha256"]
    ):
        _fail("0005 alias completion binding changed")


def _validate_live_external_media(
    runtime_root: Path,
    validated: Mapping[str, Any],
) -> None:
    section = validated["sections"]["external_media"]
    authority_path = _runtime_path(
        runtime_root,
        Path("config/postgres-media-authority-rules.json"),
    )
    registry_path = _runtime_path(
        runtime_root,
        Path("config/postgres-media-registry.json"),
    )
    _authority_document, authority_digest = _read_json_file(
        authority_path,
        private=True,
    )
    registry_document, registry_digest = _read_json_file(
        registry_path,
        private=True,
    )
    if (
        authority_digest
        != section["media_authority_rules_sha256"]
        or registry_digest != section["registry_sha256"]
        or registry_document.get("media_authority_rules_sha256")
        != authority_digest
    ):
        _fail("live external media authority or registry changed")
    path = _runtime_path(
        runtime_root,
        Path(section["audit_relative_path"]),
    )
    document, file_digest = _read_json_file(path, private=True)
    try:
        normalized = site_helper_contracts.validate_external_database_audit(
            document,
            expected_users=None,
            expected_media_authority_rules_digest=section[
                "media_authority_rules_sha256"
            ],
            expected_media_registry_digest=section["registry_sha256"],
        )
    except Exception as exc:
        raise ProductionReadinessError(
            "external database/media audit validation failed"
        ) from exc
    if (
        file_digest != section["audit_sha256"]
        or canonical_json_digest(normalized) != section["validation_sha256"]
        or len(normalized["media"]) != section["media_count"]
        or normalized["media_registry"]["captured_at"]
        != section["captured_at"]
        or normalized["requires_0014"] is not False
    ):
        _fail("external database/media evidence changed")


def _validate_live_mutable_data(
    runtime_root: Path,
    validated: Mapping[str, Any],
) -> None:
    section = validated["sections"]["mutable_data"]
    path = _runtime_path(
        runtime_root,
        Path(section["audit_relative_path"]),
    )
    document, file_digest = _read_json_file(path, private=True)
    try:
        normalized = site_helper_contracts.validate_mutable_data_audit(
            document
        )
    except Exception as exc:
        raise ProductionReadinessError(
            "mutable-data audit validation failed"
        ) from exc
    runtime = normalized["postgres_runtime"]
    postgres = validated["sections"]["postgres"]
    if (
        file_digest != section["audit_sha256"]
        or canonical_json_digest(normalized) != section["validation_sha256"]
        or normalized["captured_at"] != section["captured_at"]
        or normalized["operation_id"] != section["operation_id"]
        or normalized["snapshot_sha256"] != section["snapshot_sha256"]
        or canonical_json_digest(normalized["business_tables"])
        != section["business_tables_sha256"]
        or canonical_json_digest(normalized["static_tables"])
        != section["static_tables_sha256"]
        or canonical_json_digest(runtime)
        != section["postgres_runtime_sha256"]
        or canonical_json_digest(normalized["migration_ledger"])
        != section["migration_ledger_sha256"]
        or sha256_bytes(
            normalized["database_system_identifier"].encode("ascii")
        )
        != section["system_identifier_sha256"]
        or sha256_bytes(runtime["container_id"].encode("ascii"))
        != postgres["container_id_sha256"]
        or runtime["image_id"] != postgres["image_digest"]
        or canonical_json_digest(runtime["data_volume"])
        != postgres["volume_identity_sha256"]
        or normalized["transaction_read_only"] is not True
        or normalized["transaction_deferrable"] is not True
        or normalized["transaction_isolation"] != "repeatable read"
    ):
        _fail("mutable-data or PostgreSQL runtime seal changed")


def _validate_live_conflicts(runtime_root: Path) -> None:
    fixed_markers = (
        Path("state/deploy-in-progress.json"),
        Path("state/contract-0012-in-progress.json"),
        Path("legacy-takeover/state/active.json"),
    )
    if any(
        (_runtime_path(runtime_root, relative).exists()
         or _runtime_path(runtime_root, relative).is_symlink())
        for relative in fixed_markers
    ):
        _fail("a live deployment/contract/takeover conflict marker exists")


def validate_live_bindings(
    runtime_root: Path,
    validated: Mapping[str, Any],
) -> None:
    """Revalidate durable bindings without any state or external mutations."""

    _validate_live_prepared(runtime_root, validated)
    _validate_live_prefetch(runtime_root, validated)
    _validate_live_helpers(runtime_root, validated)
    _validate_live_takeover(runtime_root, validated)
    _validate_live_alias(runtime_root, validated)
    _validate_live_external_media(runtime_root, validated)
    _validate_live_mutable_data(runtime_root, validated)
    _validate_live_conflicts(runtime_root)


def readiness_output(validated: Mapping[str, Any]) -> dict[str, Any]:
    sections = validated["sections"]
    checks = {
        name: {
            "status": "ready",
            "evidence_sha256": sections[name]["evidence_sha256"],
        }
        for name in SECTION_NAMES
    }
    cas = sections["external_media"]["cas"]
    identity = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "status": "ready",
        "ready": True,
        "captured_at": validated["document"]["captured_at"],
        "authority": dict(validated["authority"]),
        "bridge": dict(validated["bridge"]),
        "policy_sha256": sections["git"]["policy_sha256"],
        "migration": {
            "state": validated["migration"]["name"],
            "manifest_sha256": validated["migration"]["manifest_sha256"],
            "ledger_sha256": validated["migration"]["ledger_sha256"],
        },
        "asset_manifest_digest": sections["asset"]["manifest_digest"],
        "external_media": {
            "media_authority_rules_sha256": sections["external_media"][
                "media_authority_rules_sha256"
            ],
            "registry_sha256": sections["external_media"]["registry_sha256"],
            "cas_status": "ready" if cas is not None else "not-present",
            "cas_evidence_sha256": (
                cas["evidence_sha256"] if cas is not None else None
            ),
        },
        "postgres_identity_sha256": canonical_json_digest(
            {
                name: sections["postgres"][name]
                for name in (
                    "container_id_sha256",
                    "image_digest",
                    "volume_identity_sha256",
                    "system_identifier_sha256",
                )
            }
        ),
        "checks": checks,
        "input_evidence_sha256": validated["document"]["evidence_sha256"],
    }
    return {
        **identity,
        "readiness_sha256": canonical_json_digest(identity),
    }


def output_json_schema() -> dict[str, Any]:
    """Machine-readable strict schema for the sanitized command result."""

    digest = {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}
    source = {
        "type": "object",
        "additionalProperties": False,
        "required": ["sha", "tree"],
        "properties": {
            "sha": {"type": "string", "pattern": r"^[0-9a-f]{40}$"},
            "tree": {"type": "string", "pattern": r"^[0-9a-f]{40}$"},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://nexpoly.invalid/schemas/production-readiness-output-v1.json",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "status",
            "ready",
            "captured_at",
            "authority",
            "bridge",
            "policy_sha256",
            "migration",
            "asset_manifest_digest",
            "external_media",
            "postgres_identity_sha256",
            "checks",
            "input_evidence_sha256",
            "readiness_sha256",
        ],
        "properties": {
            "schema_version": {"const": OUTPUT_SCHEMA_VERSION},
            "status": {"const": "ready"},
            "ready": {"const": True},
            "captured_at": {
                "type": "string",
                "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$",
            },
            "authority": source,
            "bridge": source,
            "policy_sha256": digest,
            "migration": {
                "type": "object",
                "additionalProperties": False,
                "required": ["state", "manifest_sha256", "ledger_sha256"],
                "properties": {
                    "state": {
                        "enum": ["pre-0012", "post-0012", "post-0013"]
                    },
                    "manifest_sha256": digest,
                    "ledger_sha256": digest,
                },
            },
            "asset_manifest_digest": digest,
            "external_media": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "media_authority_rules_sha256",
                    "registry_sha256",
                    "cas_status",
                    "cas_evidence_sha256",
                ],
                "properties": {
                    "media_authority_rules_sha256": digest,
                    "registry_sha256": digest,
                    "cas_status": {"enum": ["ready", "not-present"]},
                    "cas_evidence_sha256": {
                        "anyOf": [digest, {"type": "null"}]
                    },
                },
            },
            "postgres_identity_sha256": digest,
            "checks": {
                "type": "object",
                "additionalProperties": False,
                "required": list(SECTION_NAMES),
                "properties": {
                    name: {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["status", "evidence_sha256"],
                        "properties": {
                            "status": {"const": "ready"},
                            "evidence_sha256": digest,
                        },
                    }
                    for name in SECTION_NAMES
                },
            },
            "input_evidence_sha256": digest,
            "readiness_sha256": digest,
        },
    }


def _error_output(code: str, exc: BaseException) -> dict[str, Any]:
    fingerprint = sha256_bytes(
        f"{type(exc).__name__}:{exc}".encode("utf-8", errors="replace")
    )
    identity = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "status": "not_ready",
        "ready": False,
        "error": {
            "code": code,
            "detail_sha256": fingerprint,
        },
    }
    return {
        **identity,
        "readiness_sha256": canonical_json_digest(identity),
    }


class _JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ProductionReadinessError(f"invalid command line: {message}")


def _parser() -> argparse.ArgumentParser:
    parser = _JSONArgumentParser(
        description="Aggregate exact F/B production deployment readiness"
    )
    parser.add_argument("--authority", help="exact final F commit SHA")
    parser.add_argument("--bridge", help="exact frozen B commit SHA")
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--offline-fixture",
        type=Path,
        help="validate a fixture without reading production paths",
    )
    source.add_argument(
        "--evidence",
        type=Path,
        help="explicitly name the fixed private runtime evidence path",
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=RUNTIME_ROOT,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--print-output-schema",
        action="store_true",
        help="print the strict sanitized output JSON Schema and exit",
    )
    return parser


def main(arguments: list[str] | None = None) -> int:
    parser = _parser()
    try:
        options = parser.parse_args(arguments)
        if options.print_output_schema:
            print(json.dumps(output_json_schema(), sort_keys=True))
            return 0
        authority = _sha(options.authority, "authority F")
        bridge = _sha(options.bridge, "bridge B")
        offline = options.offline_fixture is not None
        if (
            options.runtime_root != RUNTIME_ROOT
            and os.environ.get("NEXPOLY_ALLOW_TEST_ROOT") != "1"
        ):
            _fail("runtime root override is test-only")
        if offline:
            evidence_path = options.offline_fixture
        else:
            expected_evidence = (
                options.runtime_root / EVIDENCE_RELATIVE_PATH
            ).absolute()
            if (
                options.evidence is not None
                and options.evidence.absolute() != expected_evidence
            ):
                _fail("live evidence path is fixed")
            _require_private_directory(options.runtime_root)
            _require_private_directory(options.runtime_root / "audit")
            _require_private_directory(expected_evidence.parent)
            evidence_path = expected_evidence
        document, _file_digest = _read_json_file(
            evidence_path,
            private=not offline,
        )
        validated = validate_evidence(
            document,
            expected_authority=authority,
            expected_bridge=bridge,
            enforce_freshness=not offline,
        )
        if not offline:
            validate_live_bindings(options.runtime_root, validated)
        print(json.dumps(readiness_output(validated), sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(_error_output("evidence_rejected", exc), sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
