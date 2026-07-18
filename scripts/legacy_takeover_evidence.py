#!/usr/bin/python3 -I -B
"""Pure validators for source-pinned legacy-takeover installation/evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from typing import Any, Callable


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
OPERATION_RE = re.compile(r"^takeover-[a-z0-9][a-z0-9-]{7,79}$")
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_STATUS_BYTES = 16 * 1024 * 1024
CONTROL_LAYOUT_RELATIVE_PATHS = (
    "bin",
    "config/docker",
    "control-releases",
    "state/prepared",
    "state/control-handoffs",
    "state/worker-slots",
    "state/contract-operations",
    "state/contract-verification-databases",
    "state/maintenance",
    "state/monomer-md-worker-socket",
    "state/monomer-md-worker-runs",
    "state/gpu-resource",
    "state/active-control.json",
    "state/bootstrap-control.json",
    "audit",
    "backups",
    "wheel-cache",
    "worker-venvs",
)

REVIEWED_WRAPPERS = {
    "bootstrap-quiesce",
    "bootstrap-status",
    "bootstrap-resume-unchanged",
    "bootstrap-rollback",
}
SITE_HELPERS = {
    "bootstrap-active-jobs-probe",
    "bootstrap-legacy-runtime-status",
    "bootstrap-legacy-runtime-resume-unchanged",
    "bootstrap-legacy-runtime-restore",
    "contract-0012-external-database-audit",
    "deployment-mutable-data-audit",
}
RECOVERY_FILES = {
    "bridge_recovery_launcher.py",
    "bootstrap_pull_deploy.py",
    "bridge_deploy_core.py",
    "git_source_trust.py",
    "legacy_takeover.py",
    "legacy_takeover_evidence.py",
    "maintenance_prefetch.py",
    "postgres_media_evidence.py",
    "worker_slot_runtime.py",
    "site_helper_contracts.py",
    "nexpoly-legacy-takeover",
    "nexpoly-bridge-recover",
    "nexpoly-maintenance-prefetch",
    "nexpoly-postgres-media-evidence",
}
PRIVATE_CONFIG_FILES = {
    "legacy-takeover-classification.json",
    "mutable-data-audit.pg_service.conf",
    "mutable-data-audit.pgpass",
}
REQUIRED_INSTALLED_NAMES = (
    REVIEWED_WRAPPERS
    | SITE_HELPERS
    | RECOVERY_FILES
    | PRIVATE_CONFIG_FILES
)
REQUIRED_SOURCE_HASH_NAMES = (
    REVIEWED_WRAPPERS
    | RECOVERY_FILES
    | {
        "bridge_recovery_capsule.py",
        "install_legacy_takeover_prerequisites.py",
        "bootstrap-active-jobs-probe.example",
        "bootstrap-legacy-runtime-status.example",
        "bootstrap-legacy-runtime-resume-unchanged.example",
        "bootstrap-legacy-runtime-restore.example",
        "contract-0012-external-database-audit.example",
        "postgres-media-registry.json.example",
        "postgres-media-audit-role.sql.example",
        "legacy-takeover-classification.json.example",
        "mutable-data-audit.pg_service.conf.example",
        "mutable-data-audit.pgpass.example",
        "mutable-data-audit-role.sql.example",
    }
)
STATUS_FIELDS = {
    "schema_version",
    "operation_id",
    "active",
    "apply_phase",
    "restore_phase",
    "generation",
    "classification_sha256",
    "runtime_identity_sha256",
    "git_identity",
    "git_permission_takeover_sha256",
    "git_permission_inventory_sha256",
    "git_permission_restore_sha256",
    "applied_record_sha256",
    "pre_stopped_fence",
    "pre_stopped_fence_sha256",
    "control_layout_sha256",
    "control_layout_replacement_sha256",
    "checkout_permissions_sha256",
    "checkout_permissions_replacement_sha256",
    "restored_terminal_sha256",
    "moves",
}
PRE_STOPPED_FENCE_FIELDS = {
    "schema_version",
    "operation_id",
    "captured_at",
    "git_identity",
    "helper_report_sha256",
    "runtime_identity_sha256",
    "active_jobs_zero",
    "active_jobs_zero_sha256",
    "isolated_runtime",
    "stopped_runtime",
    "runtime_fence",
    "worker_unit_backup",
    "worker_unit_seal_sha256",
    "control_layout_sha256",
    "checkout_permissions_sha256",
    "control_layout_backups",
}


class LegacyTakeoverEvidenceError(RuntimeError):
    pass


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        before = path.stat(follow_symlinks=False)
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise LegacyTakeoverEvidenceError(
            f"cannot hash takeover evidence file: {path}"
        ) from exc
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
    )
    if identity(before) != identity(after):
        raise LegacyTakeoverEvidenceError(
            f"takeover evidence file changed while hashing: {path}"
        )
    return "sha256:" + digest.hexdigest()


def _private_file(path: Path, mode: int, maximum: int | None = None) -> bytes:
    try:
        metadata = path.lstat()
        payload = path.read_bytes()
    except OSError as exc:
        raise LegacyTakeoverEvidenceError(
            f"private takeover evidence is unavailable: {path}"
        ) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != mode
        or (maximum is not None and len(payload) > maximum)
    ):
        raise LegacyTakeoverEvidenceError(
            f"private takeover evidence is unsafe: {path}"
        )
    return payload


def _digest(value: object, label: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise LegacyTakeoverEvidenceError(f"{label} is not a full SHA-256")
    return value


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise LegacyTakeoverEvidenceError(f"cannot load validator: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _expected_installed_paths(runtime_root: Path) -> dict[str, tuple[Path, int]]:
    config = runtime_root / "config"
    recovery = runtime_root / "legacy-takeover/bin"
    result = {
        name: (config / name, 0o700)
        for name in REVIEWED_WRAPPERS | SITE_HELPERS
    }
    result.update(
        {name: (recovery / name, 0o700) for name in RECOVERY_FILES}
    )
    result.update(
        {
            "legacy-takeover-classification.json": (
                config / "legacy-takeover-classification.json",
                0o600,
            ),
            "mutable-data-audit.pg_service.conf": (
                config / "mutable-data-audit.pg_service.conf",
                0o600,
            ),
            "mutable-data-audit.pgpass": (
                config / "mutable-data-audit.pgpass",
                0o600,
            ),
        }
    )
    return result


def _validate_pgpass(payload: bytes) -> None:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LegacyTakeoverEvidenceError(
            "mutable-data audit pgpass is not UTF-8"
        ) from exc
    lines = text.splitlines()
    fields = lines[0].split(":", 4) if len(lines) == 1 else []
    if (
        len(lines) != 1
        or not text.endswith("\n")
        or len(fields) != 5
        or fields[:4]
        != [
            "127.0.0.1",
            "55432",
            "nexpoly",
            "nexpoly_mutable_audit",
        ]
        or not fields[4]
        or fields[4] == "<provision-owner-only-secret>"
        or any(character.isspace() for character in fields[4])
    ):
        raise LegacyTakeoverEvidenceError(
            "mutable-data audit pgpass identity is invalid"
        )


def validate_install_manifest(
    runtime_root: Path,
    authority_sha: str,
    authority_tree: str,
) -> dict[str, Any]:
    runtime_root = runtime_root.absolute()
    if (
        SHA_RE.fullmatch(authority_sha) is None
        or SHA_RE.fullmatch(authority_tree) is None
    ):
        raise LegacyTakeoverEvidenceError(
            "full F authority SHA and tree are required"
        )
    manifest_path = (
        runtime_root / "legacy-takeover/INSTALL-MANIFEST.json"
    )
    payload = _private_file(manifest_path, 0o600, MAX_MANIFEST_BYTES)
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegacyTakeoverEvidenceError(
            "legacy takeover install manifest is invalid JSON"
        ) from exc
    fields = {
        "schema_version",
        "authority_sha",
        "authority_tree",
        "source_hashes",
        "installed",
        "helper_report_sha256",
        "classification_sha256",
        "production_source_trust_sha256",
        "production_permission_takeover_sha256",
        "production_permission_inventory_sha256",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != 1
        or document.get("authority_sha") != authority_sha
        or document.get("authority_tree") != authority_tree
    ):
        raise LegacyTakeoverEvidenceError(
            "legacy takeover install authority differs"
        )
    source_hashes = document.get("source_hashes")
    if (
        not isinstance(source_hashes, dict)
        or set(source_hashes) != REQUIRED_SOURCE_HASH_NAMES
    ):
        raise LegacyTakeoverEvidenceError(
            "legacy takeover source-hash inventory differs"
        )
    for value in source_hashes.values():
        _digest(value, "takeover source hash")
    installed = document.get("installed")
    expected = _expected_installed_paths(runtime_root)
    if not isinstance(installed, dict) or set(installed) != set(expected):
        raise LegacyTakeoverEvidenceError(
            "legacy takeover installed inventory differs"
        )
    for name, (path, mode) in expected.items():
        record = installed[name]
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "mode", "sha256"}
            or record.get("path") != str(path)
            or record.get("mode") != f"{mode:04o}"
        ):
            raise LegacyTakeoverEvidenceError(
                f"legacy takeover installed binding differs: {name}"
            )
        expected_digest = _digest(
            record.get("sha256"),
            f"installed {name} digest",
        )
        _private_file(path, mode)
        if sha256_file(path) != expected_digest:
            raise LegacyTakeoverEvidenceError(
                f"legacy takeover installed file changed: {name}"
            )
    classification = runtime_root / (
        "config/legacy-takeover-classification.json"
    )
    if sha256_file(classification) != _digest(
        document.get("classification_sha256"),
        "classification digest",
    ):
        raise LegacyTakeoverEvidenceError(
            "legacy takeover classification differs from install manifest"
        )
    _digest(
        document.get("production_source_trust_sha256"),
        "production source trust digest",
        optional=True,
    )
    _digest(
        document.get("production_permission_takeover_sha256"),
        "production permission takeover digest",
        optional=True,
    )
    _digest(
        document.get("production_permission_inventory_sha256"),
        "production permission inventory digest",
        optional=True,
    )
    pgpass = _private_file(
        runtime_root / "config/mutable-data-audit.pgpass",
        0o600,
    )
    _validate_pgpass(pgpass)
    contracts = _load_module(
        "legacy_takeover_installed_site_contracts",
        runtime_root / "legacy-takeover/bin/site_helper_contracts.py",
    )
    try:
        helper_report = contracts.inspect_helper_installation(runtime_root)
    except Exception as exc:
        raise LegacyTakeoverEvidenceError(
            "legacy takeover helper readiness failed"
        ) from exc
    if sha256_bytes(canonical_json_bytes(helper_report)) != _digest(
        document.get("helper_report_sha256"),
        "helper report digest",
    ):
        raise LegacyTakeoverEvidenceError(
            "legacy takeover helper report differs"
        )
    return dict(document)


def validate_status_document(
    document: object,
    operation_id: str,
) -> dict[str, Any]:
    if OPERATION_RE.fullmatch(operation_id) is None:
        raise LegacyTakeoverEvidenceError("takeover operation ID is invalid")
    if (
        not isinstance(document, dict)
        or set(document) != STATUS_FIELDS
        or document.get("schema_version") != 2
        or document.get("operation_id") != operation_id
        or not isinstance(document.get("active"), bool)
        or isinstance(document.get("generation"), bool)
        or not isinstance(document.get("generation"), int)
        or document["generation"] <= 0
    ):
        raise LegacyTakeoverEvidenceError(
            "legacy takeover status has an invalid shape"
        )
    for name in (
        "classification_sha256",
        "runtime_identity_sha256",
        "control_layout_sha256",
        "checkout_permissions_sha256",
    ):
        _digest(document.get(name), f"takeover status {name}")
    for name in (
        "git_permission_takeover_sha256",
        "git_permission_inventory_sha256",
        "git_permission_restore_sha256",
        "applied_record_sha256",
        "pre_stopped_fence_sha256",
        "control_layout_replacement_sha256",
        "checkout_permissions_replacement_sha256",
        "restored_terminal_sha256",
    ):
        _digest(
            document.get(name),
            f"takeover status {name}",
            optional=True,
        )
    permission_takeover = document[
        "git_permission_takeover_sha256"
    ]
    permission_inventory = document[
        "git_permission_inventory_sha256"
    ]
    permission_restore = document["git_permission_restore_sha256"]
    if (permission_takeover is None) != (permission_inventory is None):
        raise LegacyTakeoverEvidenceError(
            "takeover Git permission authority is incomplete"
        )
    if (
        permission_restore is not None
        and permission_takeover is None
    ) or (
        document.get("restore_phase") != "restored"
        and permission_restore is not None
    ) or (
        document.get("restore_phase") == "restored"
        and permission_takeover is not None
        and permission_restore is None
    ):
        raise LegacyTakeoverEvidenceError(
            "takeover Git permission restore authority differs"
        )
    git_identity = document.get("git_identity")
    if (
        not isinstance(git_identity, dict)
        or set(git_identity)
        != {"branch", "head_sha", "head_tree", "local_main_sha"}
        or git_identity.get("branch") != "refs/heads/main"
        or any(
            not isinstance(git_identity.get(name), str)
            or SHA_RE.fullmatch(git_identity[name]) is None
            for name in ("head_sha", "head_tree", "local_main_sha")
        )
        or git_identity["head_sha"] != git_identity["local_main_sha"]
    ):
        raise LegacyTakeoverEvidenceError(
            "legacy takeover Git identity is invalid"
        )
    moves = document.get("moves")
    if not isinstance(moves, list) or not moves:
        raise LegacyTakeoverEvidenceError(
            "legacy takeover move status is empty"
        )
    seen: set[str] = set()
    for move in moves:
        if (
            not isinstance(move, dict)
            or set(move)
            != {"path", "class", "status", "restore_status"}
            or move.get("class") not in {"runtime", "secret", "asset"}
            or move.get("status")
            not in {
                "pending",
                "copy-intent",
                "destination-ready",
                "source-remove-intent",
                "externalized",
            }
            or move.get("restore_status")
            not in {
                "pending",
                "copy-intent",
                "source-ready",
                "destination-remove-intent",
                "restored",
            }
        ):
            raise LegacyTakeoverEvidenceError(
                "legacy takeover move status is invalid"
            )
        raw_path = move.get("path")
        if not isinstance(raw_path, str):
            raise LegacyTakeoverEvidenceError(
                "legacy takeover move path is invalid"
            )
        path = PurePosixPath(raw_path)
        if (
            path.is_absolute()
            or str(path) != raw_path
            or any(part in {"", ".", ".."} for part in path.parts)
            or raw_path in seen
        ):
            raise LegacyTakeoverEvidenceError(
                "legacy takeover move path is invalid"
            )
        seen.add(raw_path)
    fence = document.get("pre_stopped_fence")
    fence_digest = document.get("pre_stopped_fence_sha256")
    if fence is None:
        if fence_digest is not None:
            raise LegacyTakeoverEvidenceError(
                "takeover fence digest has no evidence"
            )
    else:
        if (
            not isinstance(fence, dict)
            or set(fence) != PRE_STOPPED_FENCE_FIELDS
            or fence.get("schema_version") != 1
            or fence.get("operation_id") != operation_id
            or fence.get("git_identity") != git_identity
            or fence.get("runtime_identity_sha256")
            != document["runtime_identity_sha256"]
            or fence.get("control_layout_sha256")
            != document["control_layout_sha256"]
            or fence.get("checkout_permissions_sha256")
            != document["checkout_permissions_sha256"]
            or sha256_bytes(canonical_json_bytes(fence)) != fence_digest
        ):
            raise LegacyTakeoverEvidenceError(
                "legacy takeover pre-stopped fence differs"
            )
        zero = fence.get("active_jobs_zero")
        if (
            not isinstance(zero, dict)
            or zero.get("active_jobs_schema_version") != 2
            or zero.get("active_total") != 0
            or sha256_bytes(canonical_json_bytes(zero))
            != fence.get("active_jobs_zero_sha256")
        ):
            raise LegacyTakeoverEvidenceError(
                "legacy takeover active-job fence differs"
            )
        backups = fence.get("control_layout_backups")
        if (
            not isinstance(backups, list)
            or len(backups) != len(CONTROL_LAYOUT_RELATIVE_PATHS)
            or [
                record.get("relative_path")
                for record in backups
                if isinstance(record, dict)
            ]
            != list(CONTROL_LAYOUT_RELATIVE_PATHS)
        ):
            raise LegacyTakeoverEvidenceError(
                "legacy takeover control backup inventory differs"
            )
    return dict(document)


def load_status(
    runtime_root: Path,
    operation_id: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    runtime_root = runtime_root.absolute()
    launcher = (
        runtime_root
        / "legacy-takeover/bin/nexpoly-legacy-takeover"
    )
    _private_file(launcher, 0o700)
    try:
        completed = runner(
            [
                str(launcher),
                "status",
                "--operation-id",
                operation_id,
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
            },
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LegacyTakeoverEvidenceError(
            "legacy takeover status command failed"
        ) from exc
    payload = completed.stdout
    if not isinstance(payload, str) or not payload or len(payload.encode()) > MAX_STATUS_BYTES:
        raise LegacyTakeoverEvidenceError(
            "legacy takeover status output is invalid"
        )
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise LegacyTakeoverEvidenceError(
            "legacy takeover status output is invalid JSON"
        ) from exc
    return validate_status_document(document, operation_id)


def snapshot_current_control_layout(runtime_root: Path) -> dict[str, Any]:
    runtime_root = runtime_root.absolute()
    legacy = _load_module(
        "legacy_takeover_layout_snapshot",
        Path(__file__).with_name("legacy_takeover.py"),
    )
    records = []
    for relative in legacy.CONTROL_LAYOUT_RELATIVE_PATHS:
        path = runtime_root / relative
        present = path.exists() or path.is_symlink()
        records.append(
            {
                "relative_path": relative,
                "present": present,
                "seal": legacy.seal_path(path) if present else None,
            }
        )
    identity = {"schema_version": 1, "records": records}
    return {
        **identity,
        "sha256": sha256_bytes(canonical_json_bytes(identity)),
    }


def snapshot_current_checkout_permissions(
    runtime_root: Path,
    operation_id: str,
) -> dict[str, Any]:
    runtime_root = runtime_root.absolute()
    if OPERATION_RE.fullmatch(operation_id) is None:
        raise LegacyTakeoverEvidenceError("takeover operation ID is invalid")
    state_path = (
        runtime_root
        / "state/legacy-takeover/operations"
        / f"{operation_id}.json"
    )
    payload = _private_file(state_path, 0o600, MAX_STATUS_BYTES)
    try:
        state = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegacyTakeoverEvidenceError(
            "legacy takeover operation state is invalid JSON"
        ) from exc
    records = state.get("checkout_permissions") if isinstance(state, dict) else None
    repository = state.get("repository") if isinstance(state, dict) else None
    if (
        state.get("schema_version") != 1
        or state.get("operation_id") != operation_id
        or not isinstance(repository, str)
        or not Path(repository).is_absolute()
        or not isinstance(records, list)
        or not records
        or any(
            not isinstance(record, dict)
            or not isinstance(record.get("path"), str)
            for record in records
        )
    ):
        raise LegacyTakeoverEvidenceError(
            "legacy takeover permission authority is invalid"
        )
    legacy = _load_module(
        "legacy_takeover_permission_snapshot",
        Path(__file__).with_name("legacy_takeover.py"),
    )
    try:
        current = legacy.snapshot_checkout_permissions(
            Path(repository),
            expected_paths=[record["path"] for record in records],
        )
    except Exception as exc:
        raise LegacyTakeoverEvidenceError(
            "cannot snapshot current checkout permissions"
        ) from exc
    identity = {"schema_version": 1, "records": current}
    return {
        **identity,
        "sha256": sha256_bytes(canonical_json_bytes(identity)),
    }


def validate_completed(
    runtime_root: Path,
    operation_id: str,
    authority_sha: str,
    authority_tree: str,
    *,
    expected_git_identity: dict[str, str] | None = None,
    status_document: object | None = None,
) -> dict[str, Any]:
    manifest = validate_install_manifest(
        runtime_root,
        authority_sha,
        authority_tree,
    )
    status = (
        load_status(runtime_root, operation_id)
        if status_document is None
        else validate_status_document(status_document, operation_id)
    )
    if (
        status["apply_phase"] != "complete"
        or status["restore_phase"] is not None
        or status["active"] is not False
        or status["applied_record_sha256"] is None
        or status["pre_stopped_fence_sha256"] is None
        or status["pre_stopped_fence"] is None
        or any(
            move["status"] != "externalized"
            or move["restore_status"] != "pending"
            for move in status["moves"]
        )
        or status["classification_sha256"]
        != manifest["classification_sha256"]
        or status["git_permission_takeover_sha256"]
        != manifest["production_permission_takeover_sha256"]
        or status["git_permission_inventory_sha256"]
        != manifest["production_permission_inventory_sha256"]
        or status["git_permission_restore_sha256"] is not None
    ):
        raise LegacyTakeoverEvidenceError(
            "legacy takeover is not a completed apply authority"
        )
    if (
        expected_git_identity is not None
        and status["git_identity"] != expected_git_identity
    ):
        raise LegacyTakeoverEvidenceError(
            "legacy takeover Git authority differs"
        )
    binding = {
        "schema_version": 1,
        "operation_id": operation_id,
        "authority_sha": authority_sha,
        "authority_tree": authority_tree,
        "install_manifest_sha256": sha256_file(
            runtime_root
            / "legacy-takeover/INSTALL-MANIFEST.json"
        ),
        "classification_sha256": status["classification_sha256"],
        "runtime_identity_sha256": status["runtime_identity_sha256"],
        "git_identity": status["git_identity"],
        "pre_stopped_fence_sha256": status[
            "pre_stopped_fence_sha256"
        ],
        "control_layout_sha256": status["control_layout_sha256"],
        "checkout_permissions_sha256": status[
            "checkout_permissions_sha256"
        ],
        "applied_record_sha256": status["applied_record_sha256"],
    }
    return {
        **binding,
        "binding_sha256": sha256_bytes(canonical_json_bytes(binding)),
    }


def validate_restored(
    runtime_root: Path,
    operation_id: str,
    authority_sha: str,
    authority_tree: str,
    *,
    expected_git_identity: dict[str, str] | None = None,
    status_document: object | None = None,
) -> dict[str, Any]:
    """Validate the exact terminal legacy restore after a failed first Pull."""

    manifest = validate_install_manifest(
        runtime_root,
        authority_sha,
        authority_tree,
    )
    status = (
        load_status(runtime_root, operation_id)
        if status_document is None
        else validate_status_document(status_document, operation_id)
    )
    if (
        status["apply_phase"] != "complete"
        or status["restore_phase"] != "restored"
        or status["active"] is not False
        or status["applied_record_sha256"] is None
        or status["pre_stopped_fence_sha256"] is None
        or status["pre_stopped_fence"] is None
        or status["control_layout_replacement_sha256"] is None
        or status["checkout_permissions_replacement_sha256"] is None
        or status["restored_terminal_sha256"] is None
        or any(
            move["status"] != "externalized"
            or move["restore_status"] != "restored"
            for move in status["moves"]
        )
        or status["classification_sha256"]
        != manifest["classification_sha256"]
        or status["git_permission_takeover_sha256"]
        != manifest["production_permission_takeover_sha256"]
        or status["git_permission_inventory_sha256"]
        != manifest["production_permission_inventory_sha256"]
        or (
            status["git_permission_takeover_sha256"] is not None
            and status["git_permission_restore_sha256"] is None
        )
    ):
        raise LegacyTakeoverEvidenceError(
            "legacy takeover is not an exact terminal restore"
        )
    if (
        expected_git_identity is not None
        and status["git_identity"] != expected_git_identity
    ):
        raise LegacyTakeoverEvidenceError(
            "restored legacy takeover Git authority differs"
        )
    binding = {
        "schema_version": 1,
        "operation_id": operation_id,
        "authority_sha": authority_sha,
        "authority_tree": authority_tree,
        "install_manifest_sha256": sha256_file(
            runtime_root
            / "legacy-takeover/INSTALL-MANIFEST.json"
        ),
        "classification_sha256": status["classification_sha256"],
        "runtime_identity_sha256": status["runtime_identity_sha256"],
        "git_identity": status["git_identity"],
        "pre_stopped_fence_sha256": status[
            "pre_stopped_fence_sha256"
        ],
        "control_layout_sha256": status["control_layout_sha256"],
        "control_layout_replacement_sha256": status[
            "control_layout_replacement_sha256"
        ],
        "checkout_permissions_sha256": status[
            "checkout_permissions_sha256"
        ],
        "checkout_permissions_replacement_sha256": status[
            "checkout_permissions_replacement_sha256"
        ],
        "applied_record_sha256": status["applied_record_sha256"],
        "restored_terminal_sha256": status[
            "restored_terminal_sha256"
        ],
    }
    return {
        **binding,
        "binding_sha256": sha256_bytes(canonical_json_bytes(binding)),
    }
