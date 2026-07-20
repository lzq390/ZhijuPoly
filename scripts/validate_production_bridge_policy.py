#!/usr/bin/env python3
"""Validate F's exact, non-self-referential production bridge policy.

The generic bridge schema remains readable by the frozen B controller.  This
repository-specific validator binds that policy to one exact B commit/tree,
the immutable B images, schema-v2 assets, migration ledgers, and the tracked
external-database authority inputs.  F's own SHA/tree are deliberately not
embedded in the policy, so a squash merge cannot invalidate it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
from types import ModuleType
from typing import Any


sys.dont_write_bytecode = True
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = REPOSITORY_ROOT / "ops/config/production-bridge-policy.json"
BRIDGE_CORE_PATH = REPOSITORY_ROOT / "scripts/bridge_deploy_core.py"
MEDIA_AUTHORITY_RULES_PATH = (
    REPOSITORY_ROOT / "ops/config/postgres-media-authority-rules.json"
)
AUDIT_ROLE_SQL_PATH = (
    REPOSITORY_ROOT / "ops/config/postgres-media-audit-role.sql.example"
)
GIT_BINARY = "/usr/bin/git"
HEAD_BOUND_INPUTS = {
    "validator": "scripts/validate_production_bridge_policy.py",
    "bridge_core": "scripts/bridge_deploy_core.py",
    "policy": "ops/config/production-bridge-policy.json",
    "release_input": "release-input.json",
    "migration_manifest": "backend/migrations/postgres/manifest.json",
    "final_migration_sql": (
        "backend/migrations/postgres/0013_monomer_dft_jobs.sql"
    ),
    "media_authority_rules": (
        "ops/config/postgres-media-authority-rules.json"
    ),
    "audit_role_sql": (
        "ops/config/postgres-media-audit-role.sql.example"
    ),
}

TARGET_SHA = "82a69ddb42bcd5c4666b5bf038d02414bccc6dde"
TARGET_TREE = "44e4b4c398b7b84abdeb40bc02b885569aba4d8b"
TARGET_REF = f"refs/nexpoly/bridge-target/{TARGET_SHA}"
TARGET_CORE_BLOB = "15b8a1378d4100a5c74666344107bf00661fe34f"
TARGET_BACKEND_IMAGE = (
    "ghcr.io/lzq390/nexpoly-backend@"
    "sha256:ec850b6873cca0340a63faf47ab19b3c4a65f1a656c5866e73487890a6f057f4"
)
TARGET_WEB_IMAGE = (
    "ghcr.io/lzq390/nexpoly-web@"
    "sha256:6b7e51ba07861e9894d484e7f0133128697c47fe02c230ab179a38c3d053d008"
)
ASSET_MANIFEST_SHA256 = (
    "sha256:0588cc6a9acd50efbcba49850bbea79ab44fa1752fa530b8537ccb21753ebc9b"
)
PREDECESSOR_ASSET_MANIFEST_SHA256 = (
    "sha256:ad19a4f1cb954b3ee6999b7157c798fd887ecd3fd7ae12e40ac20a97637575e2"
)
RELEASE_INPUT_SHA256 = (
    "sha256:6895562ae3778990e50af842da0f31c21ebe502c051bbbfa6d6f51315dc08d85"
)
TARGET_MANIFEST_SHA256 = (
    "sha256:3f149c17e596c9dfe7c88245894c36e3e2d22ab67cf38375c84f2b1d7d7224fa"
)
AUTHORITY_MANIFEST_SHA256 = (
    "sha256:f3dc3ae7b5cf835af3d8ff0090e472b768bd1ad8056b3791979014e270983a3e"
)
FINAL_MIGRATION_SQL_SHA256 = (
    "sha256:ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc"
)
MEDIA_AUTHORITY_RULES_SHA256 = (
    "sha256:80543b0e2a63e744c89c83c72d9977ac1978adf35f30444d3830d2b57fc50a12"
)
AUDIT_ROLE_SQL_SHA256 = (
    "sha256:206d15b6fc4f33c3a5ee97e803946289ccad1bae7db068535d1f78e5cd5bf95f"
)
REQUIRED_CI_JOBS = [
    "Publish and smoke immutable main images",
    "bridge-validation",
    "ci-gate",
    "exact-B bridge compatibility",
]
RELEASE_INPUT = {
    "schema_version": 2,
    "asset_manifest_digest": ASSET_MANIFEST_SHA256,
    "predecessor_asset_manifest_digest": PREDECESSOR_ASSET_MANIFEST_SHA256,
    "changed_asset_trees": ["byteff2"],
    "datasets_on_asset_change": [],
}


class ProductionBridgePolicyError(RuntimeError):
    """The tracked F policy differs from the reviewed B/F authority."""


def _load_current_core(source: bytes) -> ModuleType:
    module = ModuleType("nexpoly_production_bridge_core")
    module.__file__ = str(BRIDGE_CORE_PATH)
    try:
        exec(
            compile(source, str(BRIDGE_CORE_PATH), "exec"),
            module.__dict__,
        )
    except BaseException as exc:
        raise ProductionBridgePolicyError(
            "bridge core validator is unavailable"
        ) from exc
    return module


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _git(repository_root: Path, *arguments: str) -> bytes:
    try:
        return subprocess.run(
            [GIT_BINARY, "-C", str(repository_root), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "HOME": "/nonexistent",
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin",
            },
        ).stdout
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise ProductionBridgePolicyError(
            f"Git authority check failed ({' '.join(arguments)}): {detail}"
        ) from exc


def _read_regular(path: Path, *, maximum: int = 1024 * 1024) -> bytes:
    try:
        metadata = path.lstat()
        payload = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise ProductionBridgePolicyError(
            f"required policy input is unavailable: {path}"
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_dev != after.st_dev
        or metadata.st_ino != after.st_ino
        or metadata.st_size != after.st_size
        or len(payload) != after.st_size
        or len(payload) > maximum
    ):
        raise ProductionBridgePolicyError(
            f"required policy input is not a stable bounded regular file: {path}"
        )
    return payload


_LOADED_VALIDATOR_SOURCE = _read_regular(Path(__file__).resolve())
_LOADED_BRIDGE_CORE_SOURCE = _read_regular(BRIDGE_CORE_PATH)
bridge_core = _load_current_core(_LOADED_BRIDGE_CORE_SOURCE)


def _head_commit(repository_root: Path) -> str:
    return _git(
        repository_root,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
    ).decode("ascii").strip()


def _snapshot_head_bound_inputs(
    repository_root: Path,
    authority_sha: str,
) -> tuple[dict[str, dict[str, str]], dict[str, bytes]]:
    """Read every formal authority input from HEAD and the worktree.

    Git only records the executable bit, so the evidence records and compares
    the corresponding 100644/100755 mode in addition to the exact blob bytes.
    """

    bindings: dict[str, dict[str, str]] = {}
    payloads: dict[str, bytes] = {}
    for label, relative_path in HEAD_BOUND_INPUTS.items():
        tree_record = _git(
            repository_root,
            "--literal-pathspecs",
            "ls-tree",
            "-z",
            authority_sha,
            "--",
            relative_path,
        )
        records = [record for record in tree_record.split(b"\0") if record]
        if len(records) != 1:
            raise ProductionBridgePolicyError(
                f"{label} is not tracked exactly once by authority HEAD"
            )
        try:
            metadata, encoded_path = records[0].split(b"\t", 1)
            mode, object_type, blob = metadata.decode("ascii").split(" ")
            tracked_path = encoded_path.decode("utf-8")
        except (UnicodeError, ValueError) as exc:
            raise ProductionBridgePolicyError(
                f"{label} has an invalid authority tree record"
            ) from exc
        if (
            tracked_path != relative_path
            or object_type != "blob"
            or mode not in {"100644", "100755"}
            or len(blob) != 40
        ):
            raise ProductionBridgePolicyError(
                f"{label} is not an exact regular-file authority blob"
            )

        head_payload = _git(repository_root, "cat-file", "blob", blob)
        worktree_path = repository_root / relative_path
        worktree_payload = _read_regular(worktree_path)
        worktree_mode = (
            "100755"
            if worktree_path.lstat().st_mode & 0o111
            else "100644"
        )
        if worktree_payload != head_payload:
            raise ProductionBridgePolicyError(
                f"{label} working-tree bytes differ from authority HEAD"
            )
        if worktree_mode != mode:
            raise ProductionBridgePolicyError(
                f"{label} working-tree mode differs from authority HEAD"
            )
        if (
            label == "validator"
            and worktree_payload != _LOADED_VALIDATOR_SOURCE
        ):
            raise ProductionBridgePolicyError(
                "loaded production policy validator differs from authority HEAD"
            )
        if (
            label == "bridge_core"
            and worktree_payload != _LOADED_BRIDGE_CORE_SOURCE
        ):
            raise ProductionBridgePolicyError(
                "loaded bridge core differs from authority HEAD"
            )

        bindings[label] = {
            "path": relative_path,
            "mode": mode,
            "blob": blob,
            "sha256": _sha256(head_payload),
        }
        payloads[label] = head_payload
    return bindings, payloads


def _json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionBridgePolicyError(f"{label} is invalid JSON") from exc
    if not isinstance(document, dict):
        raise ProductionBridgePolicyError(f"{label} is not a JSON object")
    return document


def _manifest_records(
    payload: bytes,
    *,
    label: str,
) -> list[dict[str, Any]]:
    document = _json_object(payload, label=label)
    if (
        set(document) != {"schema_version", "migrations"}
        or document.get("schema_version") != 2
        or not isinstance(document.get("migrations"), list)
    ):
        raise ProductionBridgePolicyError(f"{label} has an invalid shape")
    return document["migrations"]


def _parse_with_frozen_target_core(
    source: bytes,
    policy_payload: bytes,
) -> tuple[dict[str, Any], set[str]]:
    namespace: dict[str, Any] = {
        "__name__": "nexpoly_frozen_target_bridge_core",
        "__file__": f"{TARGET_SHA}:scripts/bridge_deploy_core.py",
    }
    try:
        exec(compile(source, namespace["__file__"], "exec"), namespace)
        parsed = namespace["parse_policy"](policy_payload)
        required_jobs = set(namespace["REQUIRED_CI_JOBS"])
    except BaseException as exc:
        raise ProductionBridgePolicyError(
            "frozen B controller cannot independently parse the F policy"
        ) from exc
    if not isinstance(parsed, dict):
        raise ProductionBridgePolicyError(
            "frozen B controller returned an invalid policy"
        )
    return parsed, required_jobs


def _verify_tracked_external_authority() -> None:
    if _sha256(_read_regular(MEDIA_AUTHORITY_RULES_PATH)) != (
        MEDIA_AUTHORITY_RULES_SHA256
    ):
        raise ProductionBridgePolicyError(
            "tracked media authority rules digest differs"
        )
    if _sha256(_read_regular(AUDIT_ROLE_SQL_PATH)) != AUDIT_ROLE_SQL_SHA256:
        raise ProductionBridgePolicyError("tracked audit-role SQL digest differs")


def validate_policy_payload(
    policy_payload: bytes,
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> dict[str, Any]:
    """Validate one policy payload against exact B and current dynamic F."""

    repository_root = repository_root.resolve()
    try:
        policy = bridge_core.parse_policy(policy_payload)
    except bridge_core.BridgeDeployError as exc:
        raise ProductionBridgePolicyError(str(exc)) from exc
    if sorted(bridge_core.REQUIRED_CI_JOBS) != REQUIRED_CI_JOBS:
        raise ProductionBridgePolicyError(
            "current F bridge core does not require the exact F CI jobs"
        )
    _verify_tracked_external_authority()

    target_type = _git(repository_root, "cat-file", "-t", TARGET_SHA).decode(
        "ascii"
    ).strip()
    if target_type != "commit":
        raise ProductionBridgePolicyError("frozen B identity is not a Git commit")
    observed_target_tree = _git(
        repository_root,
        "rev-parse",
        "--verify",
        f"{TARGET_SHA}^{{tree}}",
    ).decode("ascii").strip()
    if observed_target_tree != TARGET_TREE:
        raise ProductionBridgePolicyError("frozen B tree differs")

    authority_sha = _head_commit(repository_root)
    authority_tree = _git(
        repository_root,
        "rev-parse",
        "--verify",
        "HEAD^{tree}",
    ).decode("ascii").strip()
    if authority_sha == TARGET_SHA:
        raise ProductionBridgePolicyError("F authority collapsed to B target")
    _git(
        repository_root,
        "merge-base",
        "--is-ancestor",
        TARGET_SHA,
        authority_sha,
    )

    target_core_blob = _git(
        repository_root,
        "rev-parse",
        "--verify",
        f"{TARGET_SHA}:scripts/bridge_deploy_core.py",
    ).decode("ascii").strip()
    if target_core_blob != TARGET_CORE_BLOB:
        raise ProductionBridgePolicyError("frozen B bridge validator blob differs")
    authority_core_blob = _git(
        repository_root,
        "rev-parse",
        "--verify",
        f"{authority_sha}:scripts/bridge_deploy_core.py",
    ).decode("ascii").strip()
    target_core_source = _git(
        repository_root,
        "show",
        f"{TARGET_SHA}:scripts/bridge_deploy_core.py",
    )

    target_release_input_payload = _git(
        repository_root,
        "show",
        f"{TARGET_SHA}:release-input.json",
    )
    authority_release_input_payload = _git(
        repository_root,
        "show",
        f"{authority_sha}:release-input.json",
    )
    if (
        _sha256(target_release_input_payload) != RELEASE_INPUT_SHA256
        or _sha256(authority_release_input_payload) != RELEASE_INPUT_SHA256
        or _json_object(
            target_release_input_payload,
            label="frozen B release input",
        )
        != RELEASE_INPUT
        or _json_object(
            authority_release_input_payload,
            label="current F release input",
        )
        != RELEASE_INPUT
    ):
        raise ProductionBridgePolicyError(
            "B/F schema-v2 release input or predecessor differs"
        )

    target_manifest_payload = _git(
        repository_root,
        "show",
        f"{TARGET_SHA}:backend/migrations/postgres/manifest.json",
    )
    authority_manifest_payload = _git(
        repository_root,
        "show",
        f"{authority_sha}:backend/migrations/postgres/manifest.json",
    )
    if _sha256(target_manifest_payload) != TARGET_MANIFEST_SHA256:
        raise ProductionBridgePolicyError("frozen B migration manifest differs")
    if _sha256(authority_manifest_payload) != AUTHORITY_MANIFEST_SHA256:
        raise ProductionBridgePolicyError("current F migration manifest differs")
    target_records = _manifest_records(
        target_manifest_payload,
        label="frozen B migration manifest",
    )
    authority_records = _manifest_records(
        authority_manifest_payload,
        label="current F migration manifest",
    )
    try:
        accepted_ledgers = bridge_core.validate_migration_registry(
            policy,
            target_manifest_sha256=TARGET_MANIFEST_SHA256,
            target_records=target_records,
            authority_manifest_sha256=AUTHORITY_MANIFEST_SHA256,
            authority_records=authority_records,
        )
    except bridge_core.BridgeDeployError as exc:
        raise ProductionBridgePolicyError(str(exc)) from exc

    migration_sql = _git(
        repository_root,
        "show",
        f"{authority_sha}:backend/migrations/postgres/0013_monomer_dft_jobs.sql",
    )
    if _sha256(migration_sql) != FINAL_MIGRATION_SQL_SHA256:
        raise ProductionBridgePolicyError("final 0013 SQL checksum differs")

    expected_policy: dict[str, Any] = {
        "schema_version": bridge_core.POLICY_SCHEMA_VERSION,
        "mode": bridge_core.BRIDGE_MODE,
        "authority_ref": bridge_core.AUTHORITY_REF,
        "target_sha": TARGET_SHA,
        "target_tree": TARGET_TREE,
        "target_ref": TARGET_REF,
        "target_images": {
            "backend": TARGET_BACKEND_IMAGE,
            "web": TARGET_WEB_IMAGE,
        },
        "asset_manifest_digest": ASSET_MANIFEST_SHA256,
        "datasets_on_asset_change": [],
        "final_migration": dict(bridge_core.FINAL_MIGRATION),
        "accepted_migration_ledgers": accepted_ledgers,
        "external_database_audit": {
            **bridge_core.EXTERNAL_DATABASE_AUDIT_POLICY,
            "media_authority_rules_sha256": MEDIA_AUTHORITY_RULES_SHA256,
            "audit_role_sql_sha256": AUDIT_ROLE_SQL_SHA256,
        },
        "required_ci_jobs": REQUIRED_CI_JOBS,
        "policy_id": None,
    }
    expected_policy["policy_id"] = bridge_core.canonical_json_digest(
        {
            key: value
            for key, value in expected_policy.items()
            if key != "policy_id"
        }
    )
    if policy != expected_policy:
        raise ProductionBridgePolicyError(
            "production bridge policy differs from exact reviewed B/F pins"
        )

    target_parsed_policy, target_required_ci_jobs = _parse_with_frozen_target_core(
        target_core_source,
        policy_payload,
    )
    if target_parsed_policy != policy:
        raise ProductionBridgePolicyError(
            "frozen B and current F normalize the policy differently"
        )
    expected_target_jobs = {
        "Publish and smoke immutable main images",
        "bridge-validation",
        "ci-gate",
    }
    if (
        target_required_ci_jobs != expected_target_jobs
        or set(REQUIRED_CI_JOBS) - target_required_ci_jobs
        != {"exact-B bridge compatibility"}
    ):
        raise ProductionBridgePolicyError("F/B required CI job compatibility differs")

    return {
        "schema_version": 1,
        "policy_id": policy["policy_id"],
        "policy_sha256": bridge_core.canonical_json_digest(policy),
        "authority": {
            "ref": bridge_core.AUTHORITY_REF,
            "sha": authority_sha,
            "tree": authority_tree,
            "identity_source": "current-HEAD-not-policy-self-reference",
            "bridge_core_blob": authority_core_blob,
            "required_ci_jobs": list(REQUIRED_CI_JOBS),
        },
        "target": {
            "sha": TARGET_SHA,
            "tree": TARGET_TREE,
            "ref": TARGET_REF,
            "bridge_core_blob": TARGET_CORE_BLOB,
            "required_ci_jobs": sorted(target_required_ci_jobs),
            "images": dict(policy["target_images"]),
        },
        "asset": {
            "manifest_sha256": ASSET_MANIFEST_SHA256,
            "predecessor_manifest_sha256": PREDECESSOR_ASSET_MANIFEST_SHA256,
            "release_input_sha256": RELEASE_INPUT_SHA256,
            "datasets_on_asset_change": [],
        },
        "migrations": {
            "target_manifest_sha256": TARGET_MANIFEST_SHA256,
            "authority_manifest_sha256": AUTHORITY_MANIFEST_SHA256,
            "final_sql_sha256": FINAL_MIGRATION_SQL_SHA256,
            "accepted_ledgers": accepted_ledgers,
        },
        "external_database_audit": {
            **bridge_core.EXTERNAL_DATABASE_AUDIT_POLICY,
            "media_authority_rules_sha256": MEDIA_AUTHORITY_RULES_SHA256,
            "audit_role_sql_sha256": AUDIT_ROLE_SQL_SHA256,
        },
    }


def validate_tracked_policy() -> dict[str, Any]:
    repository_root = REPOSITORY_ROOT.resolve()
    authority_sha = _head_commit(repository_root)
    before_bindings, before_payloads = _snapshot_head_bound_inputs(
        repository_root,
        authority_sha,
    )
    evidence = validate_policy_payload(
        before_payloads["policy"],
        repository_root=repository_root,
    )
    if evidence["authority"]["sha"] != authority_sha:
        raise ProductionBridgePolicyError(
            "authority HEAD changed during policy validation"
        )

    after_bindings, after_payloads = _snapshot_head_bound_inputs(
        repository_root,
        authority_sha,
    )
    if (
        after_bindings != before_bindings
        or after_payloads != before_payloads
        or _head_commit(repository_root) != authority_sha
    ):
        raise ProductionBridgePolicyError(
            "authority inputs changed during policy validation"
        )
    evidence["authority"]["head_bound_inputs"] = before_bindings
    return evidence


def readiness_status() -> dict[str, Any]:
    """Return a fail-closed repository readiness result without live mutation."""

    try:
        evidence = validate_tracked_policy()
    except ProductionBridgePolicyError as exc:
        return {
            "schema_version": 1,
            "ready": False,
            "status": "not_ready",
            "blockers": [
                {
                    "code": "production_bridge_policy_invalid",
                    "detail": str(exc),
                }
            ],
        }
    return {
        "schema_version": 1,
        "ready": True,
        "status": "ready",
        "blockers": [],
        "evidence": evidence,
    }


def main() -> int:
    status = readiness_status()
    print(json.dumps(status, sort_keys=True, separators=(",", ":")))
    return 0 if status["ready"] is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
