#!/usr/bin/env python3
"""Pure validation and crash-safe authority for one historical bridge deploy.

This module does not fetch Git, contact GitHub, switch a checkout, or start a
service.  The deployment controller supplies independently observed Git/CI
facts; this module binds them to one authority-main policy, one exact ancestor
target and one globally single-use takeover token.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any


POLICY_SCHEMA_VERSION = 1
BRIDGE_DESCRIPTOR_SCHEMA_VERSION = 1
TOKEN_SCHEMA_VERSION = 2
BRIDGE_MODE = "first-governed-takeover"
AUTHORITY_REF = "refs/heads/main"
POLICY_RELATIVE_PATH = "ops/config/production-bridge-policy.json"
TOKEN_RELATIVE_PATH = Path("state/bridge-takeover.json")
TOKEN_RETIREMENT_DIRECTORY = Path("bridge-token-retirements")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RELEASE_ID_RE = re.compile(r"^[0-9a-f]{64}$")
OPERATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
LEDGER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
TARGET_REF_RE = re.compile(
    r"^refs/nexpoly/bridge-target/([0-9a-f]{40})$"
)
IMAGE_ROOTS = {
    "backend": "ghcr.io/lzq390/nexpoly-backend",
    "web": "ghcr.io/lzq390/nexpoly-web",
}
REQUIRED_CI_JOBS = {
    "ci-gate",
    "Publish and smoke immutable main images",
    "bridge-validation",
}
REQUIRED_LEDGER_ORDER = ("pre-0012", "post-0012", "post-0013")
REQUIRED_LEDGER_NAMES = set(REQUIRED_LEDGER_ORDER)
EXTERNAL_DATABASE_AUDIT_POLICY = {
    "schema_version": 1,
    "evidence_schema_version": 2,
    "registry_schema_version": 2,
    "require_exact_registry_digest": True,
    "require_fresh_snapshot": True,
}
CONTRACT_MIGRATION = {
    "version": "0012_drop_polytao_jobs",
    "checksum": (
        "c59b6f1efe9f926ad135379bd1a7141a7920730fa93c0e802646b1b913511728"
    ),
}
FINAL_MIGRATION = {
    "version": "0013_monomer_dft_jobs",
    "checksum": (
        "ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc"
    ),
}
FINAL_MIGRATION_RECORD = {
    **FINAL_MIGRATION,
    "kind": "expand",
    "epoch": 2,
    "requires_contracts": [dict(CONTRACT_MIGRATION)],
}
TOKEN_STATUSES = {
    "reserved",
    "prepared",
    "commit-intent",
    "consumed",
    "retired-precommit",
}


class BridgeDeployError(RuntimeError):
    """A bridge policy, descriptor, or token transition is invalid."""


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def canonical_json_digest(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise BridgeDeployError(f"{label} is not an exact commit/tree SHA")
    return value


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise BridgeDeployError(f"{label} is not an exact sha256 digest")
    return value


def _require_release_id(value: object, label: str) -> str:
    if not isinstance(value, str) or RELEASE_ID_RE.fullmatch(value) is None:
        raise BridgeDeployError(f"{label} is not a control release identity")
    return value


def _require_operation_id(value: object) -> str:
    if not isinstance(value, str) or OPERATION_ID_RE.fullmatch(value) is None:
        raise BridgeDeployError("bridge operation ID is invalid")
    return value


def _validate_images(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(IMAGE_ROOTS):
        raise BridgeDeployError("bridge target image policy is invalid")
    result: dict[str, str] = {}
    for role, root in IMAGE_ROOTS.items():
        reference = value.get(role)
        if (
            not isinstance(reference, str)
            or not reference.startswith(root + "@")
            or reference.count("@") != 1
        ):
            raise BridgeDeployError(f"bridge {role} image is not digest-pinned")
        _require_digest(reference.split("@", 1)[1], f"bridge {role} image digest")
        result[role] = reference
    return result


def _migration_rows(records: object) -> list[dict[str, str]]:
    if not isinstance(records, list) or not records:
        raise BridgeDeployError("bridge migration ledger is empty or invalid")
    rows: list[dict[str, str]] = []
    previous = ""
    for record in records:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("version"), str)
            or re.fullmatch(r"^[0-9]{4}_[a-z0-9_]+$", record["version"]) is None
            or not isinstance(record.get("checksum"), str)
            or re.fullmatch(r"^[0-9a-f]{64}$", record["checksum"]) is None
            or record["version"] <= previous
        ):
            raise BridgeDeployError("bridge migration ledger is not canonical")
        previous = record["version"]
        rows.append(
            {
                "version": record["version"],
                "checksum": record["checksum"],
            }
        )
    return rows


def migration_ledger_digest(records: object) -> str:
    """Digest the exact ordered version/checksum ledger, independent of metadata."""

    return canonical_json_digest(_migration_rows(records))


def _validate_manifest_records(records: object) -> list[dict[str, Any]]:
    if not isinstance(records, list) or not records:
        raise BridgeDeployError("bridge migration manifest is empty or invalid")
    normalized: list[dict[str, Any]] = []
    for record in records:
        if (
            not isinstance(record, dict)
            or set(record)
            != {
                "version",
                "kind",
                "epoch",
                "checksum",
                "requires_contracts",
            }
            or record.get("kind") not in {"baseline", "expand", "contract"}
            or isinstance(record.get("epoch"), bool)
            or not isinstance(record.get("epoch"), int)
            or record["epoch"] < 1
            or not isinstance(record.get("requires_contracts"), list)
        ):
            raise BridgeDeployError("bridge migration manifest is invalid")
        normalized.append(json.loads(json.dumps(record)))
    _migration_rows(normalized)
    return normalized


def expected_migration_registry(
    *,
    target_manifest_sha256: object,
    target_records: object,
    authority_manifest_sha256: object,
    authority_records: object,
) -> list[dict[str, str]]:
    """Derive the only three ledgers accepted by the frozen B/F pair."""

    target_digest = _require_digest(
        target_manifest_sha256, "bridge target migration manifest"
    )
    authority_digest = _require_digest(
        authority_manifest_sha256, "bridge authority migration manifest"
    )
    target = _validate_manifest_records(target_records)
    authority = _validate_manifest_records(authority_records)
    if (
        len(target) < 2
        or target[-1].get("version") != CONTRACT_MIGRATION["version"]
        or target[-1].get("checksum") != CONTRACT_MIGRATION["checksum"]
        or target[-1].get("kind") != "contract"
        or target[-1].get("epoch") != 1
        or target[-1].get("requires_contracts") != []
    ):
        raise BridgeDeployError(
            "bridge target manifest lacks the exact trailing 0012 contract"
        )
    if authority != [*target, FINAL_MIGRATION_RECORD]:
        raise BridgeDeployError(
            "bridge authority manifest is not the unique B plus 0013 extension"
        )
    before_contract = target[:-1]
    return [
        {
            "name": "pre-0012",
            "manifest_sha256": target_digest,
            "terminal_version": before_contract[-1]["version"],
            "ledger_sha256": migration_ledger_digest(before_contract),
        },
        {
            "name": "post-0012",
            "manifest_sha256": target_digest,
            "terminal_version": CONTRACT_MIGRATION["version"],
            "ledger_sha256": migration_ledger_digest(target),
        },
        {
            "name": "post-0013",
            "manifest_sha256": authority_digest,
            "terminal_version": FINAL_MIGRATION["version"],
            "ledger_sha256": migration_ledger_digest(authority),
        },
    ]


def validate_migration_registry(
    policy: object,
    *,
    target_manifest_sha256: object,
    target_records: object,
    authority_manifest_sha256: object,
    authority_records: object,
) -> list[dict[str, str]]:
    """Bind policy registry rows to the exact B and F Git manifests."""

    normalized = validate_policy(policy)
    expected = expected_migration_registry(
        target_manifest_sha256=target_manifest_sha256,
        target_records=target_records,
        authority_manifest_sha256=authority_manifest_sha256,
        authority_records=authority_records,
    )
    if normalized["accepted_migration_ledgers"] != expected:
        raise BridgeDeployError(
            "bridge migration compatibility differs from exact B/F manifests"
        )
    return expected


def match_migration_ledger(
    accepted_ledgers: object,
    records: object,
) -> dict[str, str]:
    """Return the exact accepted registry row for an observed migration ledger."""

    ledgers = _validate_accepted_ledgers(accepted_ledgers)
    rows = _migration_rows(records)
    digest = canonical_json_digest(rows)
    terminal = rows[-1]["version"]
    matches = [
        record
        for record in ledgers
        if record["ledger_sha256"] == digest
        and record["terminal_version"] == terminal
    ]
    if len(matches) != 1:
        raise BridgeDeployError(
            "observed migration ledger is outside the frozen B/F registry"
        )
    if matches[0]["name"] == "post-0013" and rows[-1] != FINAL_MIGRATION:
        raise BridgeDeployError("observed 0013 ledger checksum differs")
    return dict(matches[0])


def _validate_accepted_ledgers(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) != len(REQUIRED_LEDGER_ORDER):
        raise BridgeDeployError("bridge migration compatibility is incomplete")
    normalized: list[dict[str, str]] = []
    names: set[str] = set()
    for record in value:
        if (
            not isinstance(record, dict)
            or set(record)
            != {
                "name",
                "manifest_sha256",
                "terminal_version",
                "ledger_sha256",
            }
            or not isinstance(record.get("name"), str)
            or LEDGER_NAME_RE.fullmatch(record["name"]) is None
            or record["name"] in names
            or not isinstance(record.get("terminal_version"), str)
            or re.fullmatch(
                r"^[0-9]{4}_[a-z0-9_]+$", record["terminal_version"]
            )
            is None
        ):
            raise BridgeDeployError("bridge migration compatibility record is invalid")
        names.add(record["name"])
        normalized.append(
            {
                "name": record["name"],
                "manifest_sha256": _require_digest(
                    record.get("manifest_sha256"),
                    "bridge migration manifest",
                ),
                "terminal_version": record["terminal_version"],
                "ledger_sha256": _require_digest(
                    record.get("ledger_sha256"),
                    "bridge migration ledger",
                ),
            }
        )
    if [record["name"] for record in normalized] != list(REQUIRED_LEDGER_ORDER):
        raise BridgeDeployError(
            "bridge migration compatibility order is not canonical"
        )
    return normalized


def validate_policy(document: object) -> dict[str, Any]:
    fields = {
        "schema_version",
        "mode",
        "authority_ref",
        "target_sha",
        "target_tree",
        "target_ref",
        "target_images",
        "asset_manifest_digest",
        "datasets_on_asset_change",
        "final_migration",
        "accepted_migration_ledgers",
        "external_database_audit",
        "required_ci_jobs",
        "policy_id",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != POLICY_SCHEMA_VERSION
        or document.get("mode") != BRIDGE_MODE
        or document.get("authority_ref") != AUTHORITY_REF
    ):
        raise BridgeDeployError("bridge policy has an invalid shape")
    target_sha = _require_sha(document.get("target_sha"), "bridge target SHA")
    target_tree = _require_sha(document.get("target_tree"), "bridge target tree")
    target_ref = document.get("target_ref")
    match = TARGET_REF_RE.fullmatch(str(target_ref))
    if match is None or match.group(1) != target_sha:
        raise BridgeDeployError("bridge policy target ref is not the exact private ref")
    images = _validate_images(document.get("target_images"))
    asset = _require_digest(
        document.get("asset_manifest_digest"),
        "bridge asset manifest",
    )
    datasets = document.get("datasets_on_asset_change")
    if (
        not isinstance(datasets, list)
        or datasets != []
    ):
        raise BridgeDeployError(
            "bridge assets must not request database dataset rebuilds"
        )
    if document.get("final_migration") != FINAL_MIGRATION:
        raise BridgeDeployError("bridge final 0013 registration differs")
    normalized_ledgers = _validate_accepted_ledgers(
        document.get("accepted_migration_ledgers")
    )
    external_database_audit = document.get("external_database_audit")
    if (
        not isinstance(external_database_audit, dict)
        or set(external_database_audit)
        != {*EXTERNAL_DATABASE_AUDIT_POLICY, "media_registry_sha256"}
        or any(
            external_database_audit.get(key) != value
            for key, value in EXTERNAL_DATABASE_AUDIT_POLICY.items()
        )
    ):
        raise BridgeDeployError(
            "bridge external database audit policy is invalid"
        )
    media_registry_sha256 = _require_digest(
        external_database_audit.get("media_registry_sha256"),
        "bridge external database media registry",
    )
    jobs = document.get("required_ci_jobs")
    if (
        not isinstance(jobs, list)
        or jobs != sorted(set(jobs))
        or not REQUIRED_CI_JOBS.issubset(set(jobs))
        or any(not isinstance(value, str) or not value for value in jobs)
    ):
        raise BridgeDeployError("bridge policy CI jobs are incomplete")
    normalized = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "mode": BRIDGE_MODE,
        "authority_ref": AUTHORITY_REF,
        "target_sha": target_sha,
        "target_tree": target_tree,
        "target_ref": target_ref,
        "target_images": images,
        "asset_manifest_digest": asset,
        "datasets_on_asset_change": list(datasets),
        "final_migration": dict(FINAL_MIGRATION),
        "accepted_migration_ledgers": normalized_ledgers,
        "external_database_audit": {
            **EXTERNAL_DATABASE_AUDIT_POLICY,
            "media_registry_sha256": media_registry_sha256,
        },
        "required_ci_jobs": list(jobs),
        "policy_id": document.get("policy_id"),
    }
    identity = {key: value for key, value in normalized.items() if key != "policy_id"}
    if normalized["policy_id"] != canonical_json_digest(identity):
        raise BridgeDeployError("bridge policy identity differs")
    return normalized


def parse_policy(payload: bytes) -> dict[str, Any]:
    if not payload or len(payload) > 1024 * 1024:
        raise BridgeDeployError("bridge policy payload is empty or too large")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeDeployError("bridge policy is invalid JSON") from exc
    return validate_policy(document)


def validate_relation(
    policy: object,
    *,
    authority_sha: object,
    authority_tree: object,
    remote_main: object,
    target_sha: object,
    target_tree: object,
    target_ref: object,
    is_ancestor: object,
) -> dict[str, Any]:
    policy = validate_policy(policy)
    authority_sha = _require_sha(authority_sha, "bridge authority SHA")
    authority_tree = _require_sha(authority_tree, "bridge authority tree")
    remote_main = _require_sha(remote_main, "bridge remote main")
    target_sha = _require_sha(target_sha, "bridge observed target SHA")
    target_tree = _require_sha(target_tree, "bridge observed target tree")
    if (
        remote_main != authority_sha
        or target_sha != policy["target_sha"]
        or target_tree != policy["target_tree"]
        or target_ref != policy["target_ref"]
        or authority_sha == target_sha
        or is_ancestor is not True
    ):
        raise BridgeDeployError("bridge authority/target relation differs from policy")
    return {
        "authority_sha": authority_sha,
        "authority_tree": authority_tree,
        "remote_main": remote_main,
        "authority_ref": AUTHORITY_REF,
        "target_sha": target_sha,
        "target_tree": target_tree,
        "target_ref": target_ref,
        "target_is_ancestor": True,
    }


def validate_bridge_descriptor(document: object) -> dict[str, Any]:
    fields = {
        "schema_version",
        "mode",
        "operation_id",
        "authority",
        "target",
        "policy",
        "policy_sha256",
        "ancestry",
        "token",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != BRIDGE_DESCRIPTOR_SCHEMA_VERSION
        or document.get("mode") != BRIDGE_MODE
    ):
        raise BridgeDeployError("bridge descriptor has an invalid shape")
    operation_id = _require_operation_id(document.get("operation_id"))
    policy = validate_policy(document.get("policy"))
    if canonical_json_digest(policy) != _require_digest(
        document.get("policy_sha256"), "bridge policy payload"
    ):
        raise BridgeDeployError("bridge descriptor policy digest differs")
    authority = document.get("authority")
    if not isinstance(authority, dict) or set(authority) != {
        "sha",
        "tree",
        "remote_ref",
        "remote_main",
        "control_release_id",
        "ci_evidence_sha256",
    }:
        raise BridgeDeployError("bridge authority descriptor is invalid")
    authority_sha = _require_sha(authority.get("sha"), "bridge authority SHA")
    authority_tree = _require_sha(authority.get("tree"), "bridge authority tree")
    remote_main = _require_sha(authority.get("remote_main"), "bridge remote main")
    if (
        authority.get("remote_ref") != AUTHORITY_REF
        or remote_main != authority_sha
    ):
        raise BridgeDeployError("bridge authority freshness differs")
    authority_control = _require_release_id(
        authority.get("control_release_id"), "bridge authority control"
    )
    ci_digest = _require_digest(
        authority.get("ci_evidence_sha256"), "bridge authority CI evidence"
    )
    target = document.get("target")
    if not isinstance(target, dict) or set(target) != {
        "sha",
        "tree",
        "exact_ref",
        "control_release_id",
        "images",
        "asset_manifest_digest",
        "datasets_on_asset_change",
    }:
        raise BridgeDeployError("bridge target descriptor is invalid")
    target_sha = _require_sha(target.get("sha"), "bridge target SHA")
    target_tree = _require_sha(target.get("tree"), "bridge target tree")
    target_control = _require_release_id(
        target.get("control_release_id"), "bridge target control"
    )
    images = _validate_images(target.get("images"))
    asset = _require_digest(
        target.get("asset_manifest_digest"), "bridge target asset"
    )
    datasets = target.get("datasets_on_asset_change")
    if (
        target_sha != policy["target_sha"]
        or target_tree != policy["target_tree"]
        or target.get("exact_ref") != policy["target_ref"]
        or images != policy["target_images"]
        or asset != policy["asset_manifest_digest"]
        or datasets != policy["datasets_on_asset_change"]
        or target_control == authority_control
    ):
        raise BridgeDeployError("bridge target differs from authority policy")
    ancestry = document.get("ancestry")
    expected_ancestry = {
        "ancestor_sha": target_sha,
        "descendant_sha": authority_sha,
        "verified": True,
    }
    if ancestry != expected_ancestry:
        raise BridgeDeployError("bridge ancestry proof differs")
    token = document.get("token")
    if not isinstance(token, dict) or set(token) != {
        "token_id",
        "token_sha256",
    }:
        raise BridgeDeployError("bridge token descriptor is invalid")
    token_id = _require_digest(token.get("token_id"), "bridge token identity")
    token_digest = _require_digest(token.get("token_sha256"), "bridge token digest")
    return {
        "schema_version": BRIDGE_DESCRIPTOR_SCHEMA_VERSION,
        "mode": BRIDGE_MODE,
        "operation_id": operation_id,
        "authority": {
            "sha": authority_sha,
            "tree": authority_tree,
            "remote_ref": AUTHORITY_REF,
            "remote_main": remote_main,
            "control_release_id": authority_control,
            "ci_evidence_sha256": ci_digest,
        },
        "target": {
            "sha": target_sha,
            "tree": target_tree,
            "exact_ref": policy["target_ref"],
            "control_release_id": target_control,
            "images": images,
            "asset_manifest_digest": asset,
            "datasets_on_asset_change": list(policy["datasets_on_asset_change"]),
        },
        "policy": policy,
        "policy_sha256": document["policy_sha256"],
        "ancestry": expected_ancestry,
        "token": {
            "token_id": token_id,
            "token_sha256": token_digest,
        },
    }


def build_bridge_descriptor(
    *,
    operation_id: str,
    authority_sha: str,
    authority_tree: str,
    authority_control_release_id: str,
    ci_evidence: object,
    target_control_release_id: str,
    policy: object,
    token_id: str,
    token_sha256: str,
) -> dict[str, Any]:
    policy = validate_policy(policy)
    document = {
        "schema_version": BRIDGE_DESCRIPTOR_SCHEMA_VERSION,
        "mode": BRIDGE_MODE,
        "operation_id": operation_id,
        "authority": {
            "sha": authority_sha,
            "tree": authority_tree,
            "remote_ref": AUTHORITY_REF,
            "remote_main": authority_sha,
            "control_release_id": authority_control_release_id,
            "ci_evidence_sha256": canonical_json_digest(ci_evidence),
        },
        "target": {
            "sha": policy["target_sha"],
            "tree": policy["target_tree"],
            "exact_ref": policy["target_ref"],
            "control_release_id": target_control_release_id,
            "images": policy["target_images"],
            "asset_manifest_digest": policy["asset_manifest_digest"],
            "datasets_on_asset_change": policy["datasets_on_asset_change"],
        },
        "policy": policy,
        "policy_sha256": canonical_json_digest(policy),
        "ancestry": {
            "ancestor_sha": policy["target_sha"],
            "descendant_sha": authority_sha,
            "verified": True,
        },
        "token": {
            "token_id": token_id,
            "token_sha256": token_sha256,
        },
    }
    return validate_bridge_descriptor(document)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    payload = canonical_json_bytes(document) + b"\n"
    temporary = path.parent / f".{path.name}.{os.urandom(12).hex()}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fchmod(stream.fileno(), 0o600)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def _require_private_state_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise BridgeDeployError("bridge state directory is unsafe")


def _load_token(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        payload = path.read_bytes()
    except OSError as exc:
        raise BridgeDeployError("bridge token authority is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or len(payload) > 1024 * 1024
    ):
        raise BridgeDeployError("bridge token authority is unsafe")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeDeployError("bridge token authority is invalid JSON") from exc
    return validate_token(document)


def load_token_authority(state_root: Path) -> dict[str, Any]:
    """Load the permanent global bridge-token authority without mutating it."""

    _require_private_state_directory(state_root)
    document = _load_token(state_root / TOKEN_RELATIVE_PATH.name)
    _validate_retirement_chain(state_root, document)
    return document


def token_lineage_contains(
    state_root: Path,
    current: object,
    *,
    operation_id: str,
    policy_id: str,
    descriptor_sha256: str,
    token_id: str,
    token_sha256: str,
) -> bool:
    """Prove that an exact prior token is in the validated retirement chain."""

    observed = validate_token(current)
    _validate_retirement_chain(state_root, observed)
    operation_id = _require_operation_id(operation_id)
    policy_id = _require_digest(policy_id, "bridge token lineage policy")
    descriptor_sha256 = _require_digest(
        descriptor_sha256,
        "bridge token lineage descriptor",
    )
    token_id = _require_digest(token_id, "bridge token lineage identity")
    token_sha256 = _require_digest(token_sha256, "bridge token lineage digest")
    digest = observed["previous_retirement_sha256"]
    while digest is not None:
        archived = _load_retirement_archive(state_root, digest)
        if (
            archived["operation_id"] == operation_id
            and archived["policy_id"] == policy_id
            and archived["descriptor_sha256"] == descriptor_sha256
            and archived["token_id"] == token_id
            and archived["token_sha256"] == token_sha256
        ):
            return True
        digest = archived["previous_retirement_sha256"]
    return False


def token_identity(token: bytes) -> dict[str, str]:
    """Return the public identity of an in-memory one-time token."""

    if not isinstance(token, bytes) or len(token) < 32:
        raise BridgeDeployError("bridge token entropy is insufficient")
    return {
        "token_id": sha256_bytes(b"nexpoly-bridge-token-id\0" + token),
        "token_sha256": sha256_bytes(token),
    }


def validate_token(document: object) -> dict[str, Any]:
    fields = {
        "schema_version",
        "mode",
        "generation",
        "previous_retirement_sha256",
        "status",
        "operation_id",
        "policy_id",
        "descriptor_sha256",
        "token_id",
        "token_sha256",
        "candidate_state_sha256",
        "prepared_at",
        "commit_started_at",
        "consumed_at",
        "retirement",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != TOKEN_SCHEMA_VERSION
        or document.get("mode") != BRIDGE_MODE
        or document.get("status") not in TOKEN_STATUSES
    ):
        raise BridgeDeployError("bridge token authority has an invalid shape")
    generation = document.get("generation")
    previous_retirement = document.get("previous_retirement_sha256")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise BridgeDeployError("bridge token generation is invalid")
    if generation == 1:
        if previous_retirement is not None:
            raise BridgeDeployError("first bridge token has a retirement predecessor")
    else:
        _require_digest(
            previous_retirement, "bridge token retirement predecessor"
        )
    _require_operation_id(document.get("operation_id"))
    _require_digest(document.get("policy_id"), "bridge token policy")
    descriptor_sha256 = document.get("descriptor_sha256")
    _require_digest(document.get("token_id"), "bridge token identity")
    _require_digest(document.get("token_sha256"), "bridge token digest")
    candidate = document.get("candidate_state_sha256")
    started = document.get("commit_started_at")
    consumed = document.get("consumed_at")
    retirement = document.get("retirement")
    if not isinstance(document.get("prepared_at"), str) or not document["prepared_at"]:
        raise BridgeDeployError("bridge token preparation timestamp is invalid")
    if document["status"] == "reserved":
        if (
            descriptor_sha256 is not None
            or candidate is not None
            or started is not None
            or consumed is not None
            or retirement is not None
        ):
            raise BridgeDeployError("reserved bridge token has committed effects")
    elif document["status"] == "prepared":
        _require_digest(descriptor_sha256, "bridge token descriptor")
        if (
            candidate is not None
            or started is not None
            or consumed is not None
            or retirement is not None
        ):
            raise BridgeDeployError("prepared bridge token has committed effects")
    elif document["status"] == "commit-intent":
        _require_digest(descriptor_sha256, "bridge token descriptor")
        _require_digest(candidate, "bridge candidate state")
        if (
            not isinstance(started, str)
            or not started
            or consumed is not None
            or retirement is not None
        ):
            raise BridgeDeployError("bridge commit intent is incomplete")
    elif document["status"] == "consumed":
        _require_digest(descriptor_sha256, "bridge token descriptor")
        _require_digest(candidate, "bridge candidate state")
        if (
            not isinstance(started, str)
            or not started
            or not isinstance(consumed, str)
            or not consumed
            or retirement is not None
        ):
            raise BridgeDeployError("consumed bridge token is incomplete")
    else:
        _require_digest(descriptor_sha256, "retired bridge token descriptor")
        if (
            candidate is not None
            or started is not None
            or consumed is not None
            or not isinstance(retirement, dict)
            or set(retirement)
            != {
                "reason",
                "operation_state_sha256",
                "terminal_audit_sha256",
                "restored_terminal_sha256",
                "recovery_capsule_sha256",
                "retired_at",
            }
            or retirement.get("reason") != "failed-precommit-restored"
            or not isinstance(retirement.get("retired_at"), str)
            or not retirement["retired_at"]
        ):
            raise BridgeDeployError("retired bridge token is incomplete")
        _require_digest(
            retirement.get("operation_state_sha256"),
            "retired bridge operation state",
        )
        _require_digest(
            retirement.get("terminal_audit_sha256"),
            "retired bridge terminal audit",
        )
        _require_digest(
            retirement.get("restored_terminal_sha256"),
            "retired bridge restore terminal",
        )
        _require_digest(
            retirement.get("recovery_capsule_sha256"),
            "retired bridge recovery capsule",
        )
    return dict(document)


def token_record_digest(document: object) -> str:
    """Digest the exact durable JSON representation of one token generation."""

    record = validate_token(document)
    return sha256_bytes(canonical_json_bytes(record) + b"\n")


def retirement_reuse_authority(document: object) -> dict[str, Any]:
    """Return the exact content-addressed proof needed to allocate a successor."""

    record = validate_token(document)
    if record["status"] != "retired-precommit":
        raise BridgeDeployError("bridge token is not a retired precommit authority")
    retirement = record["retirement"]
    return {
        "generation": record["generation"],
        "operation_id": record["operation_id"],
        "descriptor_sha256": record["descriptor_sha256"],
        "operation_state_sha256": retirement["operation_state_sha256"],
        "terminal_audit_sha256": retirement["terminal_audit_sha256"],
        "restored_terminal_sha256": retirement["restored_terminal_sha256"],
        "recovery_capsule_sha256": retirement["recovery_capsule_sha256"],
        "retired_token_sha256": token_record_digest(record),
    }


def _retirement_archive_path(state_root: Path, digest: str) -> Path:
    digest = _require_digest(digest, "bridge token retirement archive")
    return (
        state_root
        / TOKEN_RETIREMENT_DIRECTORY
        / f"{digest.removeprefix('sha256:')}.json"
    )


def _load_retirement_archive(
    state_root: Path, digest: str
) -> dict[str, Any]:
    _require_private_state_directory(
        state_root / TOKEN_RETIREMENT_DIRECTORY
    )
    path = _retirement_archive_path(state_root, digest)
    record = _load_token(path)
    if token_record_digest(record) != digest:
        raise BridgeDeployError("bridge token retirement archive digest differs")
    if record["status"] != "retired-precommit":
        raise BridgeDeployError("bridge token retirement archive is not terminal")
    return record


def _validate_retirement_chain(
    state_root: Path, current: dict[str, Any]
) -> None:
    expected_generation = current["generation"] - 1
    digest = current["previous_retirement_sha256"]
    seen: set[str] = set()
    while expected_generation:
        if digest in seen:
            raise BridgeDeployError("bridge token retirement chain contains a cycle")
        seen.add(digest)
        archived = _load_retirement_archive(state_root, digest)
        if archived["generation"] != expected_generation:
            raise BridgeDeployError("bridge token retirement generation is discontinuous")
        digest = archived["previous_retirement_sha256"]
        expected_generation -= 1
    if digest is not None:
        raise BridgeDeployError("bridge token retirement chain has an extra ancestor")


def _ensure_retirement_directory(state_root: Path) -> Path:
    directory = state_root / TOKEN_RETIREMENT_DIRECTORY
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _require_private_state_directory(directory)
    return directory


def _archive_retired_token(
    state_root: Path, record: dict[str, Any]
) -> str:
    digest = token_record_digest(record)
    directory = _ensure_retirement_directory(state_root)
    path = _retirement_archive_path(state_root, digest)
    payload = canonical_json_bytes(record) + b"\n"
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        archived = _load_retirement_archive(state_root, digest)
        if archived != record:
            raise BridgeDeployError("bridge token retirement archive conflicts")
        return digest
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fchmod(stream.fileno(), 0o600)
            os.fsync(stream.fileno())
        _fsync_directory(directory)
    except BaseException:
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    return digest


def reserve_token(
    state_root: Path,
    *,
    operation_id: str,
    policy_id: str,
    token: bytes | None = None,
    predecessor_retirement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _require_private_state_directory(state_root)
    path = state_root / TOKEN_RELATIVE_PATH.name
    operation_id = _require_operation_id(operation_id)
    policy_id = _require_digest(policy_id, "bridge policy identity")
    if path.exists() or path.is_symlink():
        existing = load_token_authority(state_root)
        if (
            existing["operation_id"] == operation_id
            and existing["policy_id"] == policy_id
            and existing["status"] != "retired-precommit"
        ):
            return existing
        if existing["status"] != "retired-precommit":
            raise BridgeDeployError(
                "bridge takeover token is already owned or consumed"
            )
        expected_predecessor = retirement_reuse_authority(existing)
        if predecessor_retirement != expected_predecessor:
            raise BridgeDeployError(
                "bridge token successor lacks exact failed restore authority"
            )
        if existing["operation_id"] == operation_id:
            raise BridgeDeployError(
                "retired bridge operation ID cannot be rearmed"
            )
        predecessor_digest = _archive_retired_token(state_root, existing)
        generation = existing["generation"] + 1
    else:
        if predecessor_retirement is not None:
            raise BridgeDeployError(
                "first bridge token cannot claim a retirement predecessor"
            )
        predecessor_digest = None
        generation = 1
    token = os.urandom(32) if token is None else token
    identity = token_identity(token)
    document = {
        "schema_version": TOKEN_SCHEMA_VERSION,
        "mode": BRIDGE_MODE,
        "generation": generation,
        "previous_retirement_sha256": predecessor_digest,
        "status": "reserved",
        "operation_id": operation_id,
        "policy_id": policy_id,
        "descriptor_sha256": None,
        **identity,
        "candidate_state_sha256": None,
        "prepared_at": utc_now(),
        "commit_started_at": None,
        "consumed_at": None,
        "retirement": None,
    }
    _atomic_json(path, document)
    return load_token_authority(state_root)


def bind_token_descriptor(
    state_root: Path,
    *,
    operation_id: str,
    policy_id: str,
    descriptor_sha256: str,
) -> dict[str, Any]:
    """CAS-bind a reservation to the exact descriptor published on disk."""

    _require_private_state_directory(state_root)
    path = state_root / TOKEN_RELATIVE_PATH.name
    record = load_token_authority(state_root)
    operation_id = _require_operation_id(operation_id)
    policy_id = _require_digest(policy_id, "bridge policy identity")
    descriptor_sha256 = _require_digest(
        descriptor_sha256, "bridge descriptor digest"
    )
    if (
        record["operation_id"] != operation_id
        or record["policy_id"] != policy_id
    ):
        raise BridgeDeployError("bridge token belongs to another deployment")
    if record["status"] == "reserved":
        record.update(
            {
                "status": "prepared",
                "descriptor_sha256": descriptor_sha256,
            }
        )
        _atomic_json(path, record)
        return load_token_authority(state_root)
    if record["descriptor_sha256"] == descriptor_sha256:
        return record
    raise BridgeDeployError("bridge token is bound to another descriptor")


def prepare_token(
    state_root: Path,
    *,
    operation_id: str,
    policy_id: str,
    descriptor_sha256: str,
    token: bytes | None = None,
    predecessor_retirement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compatibility helper that reserves and binds in two durable steps."""

    reserve_token(
        state_root,
        operation_id=operation_id,
        policy_id=policy_id,
        token=token,
        predecessor_retirement=predecessor_retirement,
    )
    return bind_token_descriptor(
        state_root,
        operation_id=operation_id,
        policy_id=policy_id,
        descriptor_sha256=descriptor_sha256,
    )


def retire_precommit_token(
    state_root: Path,
    *,
    operation_id: str,
    descriptor_sha256: str,
    operation_state_sha256: str,
    terminal_audit_sha256: str,
    restored_terminal_sha256: str,
    recovery_capsule_sha256: str,
) -> dict[str, Any]:
    """Permanently end one prepared generation after an exact legacy restore."""

    _require_private_state_directory(state_root)
    path = state_root / TOKEN_RELATIVE_PATH.name
    record = load_token_authority(state_root)
    operation_id = _require_operation_id(operation_id)
    descriptor_sha256 = _require_digest(
        descriptor_sha256, "retired bridge descriptor"
    )
    operation_state_sha256 = _require_digest(
        operation_state_sha256, "retired bridge operation state"
    )
    terminal_audit_sha256 = _require_digest(
        terminal_audit_sha256, "retired bridge terminal audit"
    )
    restored_terminal_sha256 = _require_digest(
        restored_terminal_sha256, "retired bridge restore terminal"
    )
    recovery_capsule_sha256 = _require_digest(
        recovery_capsule_sha256, "retired bridge recovery capsule"
    )
    if (
        record["operation_id"] != operation_id
        or record["descriptor_sha256"] != descriptor_sha256
    ):
        raise BridgeDeployError("bridge token retirement belongs to another deployment")
    retirement = {
        "reason": "failed-precommit-restored",
        "operation_state_sha256": operation_state_sha256,
        "terminal_audit_sha256": terminal_audit_sha256,
        "restored_terminal_sha256": restored_terminal_sha256,
        "recovery_capsule_sha256": recovery_capsule_sha256,
        "retired_at": (
            record["retirement"]["retired_at"]
            if record["status"] == "retired-precommit"
            else utc_now()
        ),
    }
    if record["status"] == "retired-precommit":
        if record["retirement"] != retirement:
            raise BridgeDeployError("retired bridge token authority differs")
        return record
    if record["status"] != "prepared":
        raise BridgeDeployError(
            "only a prepared precommit bridge token can be retired"
        )
    record.update(
        {
            "status": "retired-precommit",
            "retirement": retirement,
        }
    )
    _atomic_json(path, record)
    return load_token_authority(state_root)


def begin_state_commit(
    state_root: Path,
    *,
    operation_id: str,
    descriptor_sha256: str,
    candidate_state_sha256: str,
) -> dict[str, Any]:
    _require_private_state_directory(state_root)
    path = state_root / TOKEN_RELATIVE_PATH.name
    record = load_token_authority(state_root)
    if (
        record["operation_id"] != _require_operation_id(operation_id)
        or record["descriptor_sha256"]
        != _require_digest(descriptor_sha256, "bridge descriptor digest")
    ):
        raise BridgeDeployError("bridge token belongs to another deployment")
    candidate_state_sha256 = _require_digest(
        candidate_state_sha256, "bridge candidate state"
    )
    if record["status"] == "prepared":
        record.update(
            {
                "status": "commit-intent",
                "candidate_state_sha256": candidate_state_sha256,
                "commit_started_at": utc_now(),
            }
        )
        _atomic_json(path, record)
        return load_token_authority(state_root)
    if (
        record["status"] in {"commit-intent", "consumed"}
        and record["candidate_state_sha256"] == candidate_state_sha256
    ):
        return record
    raise BridgeDeployError("bridge token commit authority is terminal or different")


def reconcile_token(
    state_root: Path,
    *,
    operation_id: str,
    descriptor_sha256: str,
    observed_current_state_sha256: str | None,
) -> dict[str, Any]:
    _require_private_state_directory(state_root)
    path = state_root / TOKEN_RELATIVE_PATH.name
    record = load_token_authority(state_root)
    if (
        record["operation_id"] != _require_operation_id(operation_id)
        or record["descriptor_sha256"]
        != _require_digest(descriptor_sha256, "bridge descriptor digest")
    ):
        raise BridgeDeployError("bridge token belongs to another deployment")
    if observed_current_state_sha256 is not None:
        observed_current_state_sha256 = _require_digest(
            observed_current_state_sha256, "observed current deployment state"
        )
    candidate = record["candidate_state_sha256"]
    if record["status"] == "prepared":
        if observed_current_state_sha256 is not None:
            raise BridgeDeployError(
                "current deployment appeared before bridge commit intent"
            )
        return record
    if observed_current_state_sha256 is None:
        if record["status"] == "consumed":
            raise BridgeDeployError("consumed bridge token lost its current state")
        return record
    if observed_current_state_sha256 != candidate:
        raise BridgeDeployError("bridge current state differs from commit intent")
    if record["status"] == "commit-intent":
        record.update({"status": "consumed", "consumed_at": utc_now()})
        _atomic_json(path, record)
        return load_token_authority(state_root)
    return record


def consume_token(
    state_root: Path,
    *,
    operation_id: str,
    descriptor_sha256: str,
    candidate_state_sha256: str,
) -> dict[str, Any]:
    candidate_state_sha256 = _require_digest(
        candidate_state_sha256, "bridge candidate state"
    )
    record = reconcile_token(
        state_root,
        operation_id=operation_id,
        descriptor_sha256=descriptor_sha256,
        observed_current_state_sha256=candidate_state_sha256,
    )
    if record["status"] != "consumed":
        raise BridgeDeployError("bridge token did not commit atomically")
    return record
