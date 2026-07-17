#!/usr/bin/python3 -I -B
"""Finalize one exact restored F -> B bridge from an external B capsule.

This entry has no deploy, rollback, Git, Docker, database, service or asset
mutation capability.  It can only commit the terminal bookkeeping for a
first-bridge attempt after the source-pinned legacy takeover validator proves
that the original runtime is already fully restored.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any


RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
CAPSULES_ROOT = (
    RUNTIME_ROOT / "legacy-takeover/runtime/bridge-recovery-capsules"
)
CAPSULE_FILES = {
    "bridge_recovery_capsule.py",
    "bridge_deploy_core.py",
    "legacy_takeover_evidence.py",
}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
OPERATION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
TAKEOVER_RE = re.compile(r"^takeover-[a-z0-9][a-z0-9-]{7,79}$")
MAX_JSON_BYTES = 64 * 1024 * 1024


class RecoveryError(RuntimeError):
    """The content-addressed recovery authority is incomplete or different."""


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def canonical_json_digest(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise RecoveryError(f"{label} is not an exact SHA-256 digest")
    return value


def require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise RecoveryError(f"{label} is not an exact Git SHA")
    return value


def require_operation(value: object) -> str:
    if not isinstance(value, str) or OPERATION_RE.fullmatch(value) is None:
        raise RecoveryError("bridge recovery operation ID is invalid")
    return value


def require_private_directory(path: Path, *, create: bool = False) -> None:
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RecoveryError(f"private directory is unavailable: {path}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RecoveryError(f"private directory is unsafe: {path}")


def load_private_json(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        payload = path.read_bytes()
    except OSError as exc:
        raise RecoveryError(f"private JSON is unavailable: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or len(payload) > MAX_JSON_BYTES
    ):
        raise RecoveryError(f"private JSON is unsafe: {path}")
    try:
        document = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"private JSON is invalid: {path}") from exc
    if not isinstance(document, dict):
        raise RecoveryError(f"private JSON is not an object: {path}")
    return document


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, document: dict[str, Any]) -> None:
    require_private_directory(path.parent)
    payload = canonical_json_bytes(document) + b"\n"
    temporary = path.parent / f".{path.name}.{os.urandom(12).hex()}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fchmod(stream.fileno(), 0o600)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RecoveryError(f"cannot load recovery module: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def load_capsule(
    *,
    capsule_sha256: str,
    operation_id: str,
    authority_sha: str,
    target_sha: str,
    descriptor_sha256: str,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    capsule_sha256 = require_digest(capsule_sha256, "recovery capsule")
    operation_id = require_operation(operation_id)
    authority_sha = require_sha(authority_sha, "bridge authority")
    target_sha = require_sha(target_sha, "bridge target")
    descriptor_sha256 = require_digest(
        descriptor_sha256, "bridge descriptor"
    )
    require_private_directory(RUNTIME_ROOT)
    require_private_directory(RUNTIME_ROOT / "legacy-takeover")
    require_private_directory(RUNTIME_ROOT / "legacy-takeover/runtime")
    require_private_directory(CAPSULES_ROOT)
    root = CAPSULES_ROOT / capsule_sha256.removeprefix("sha256:")
    require_private_directory(root)
    metadata = load_private_json(root / "capsule.json")
    expected_fields = {
        "schema_version",
        "capsule_sha256",
        "operation_id",
        "authority_sha",
        "target_sha",
        "descriptor_sha256",
        "control_release_id",
        "takeover_operation_id",
        "files",
    }
    if (
        set(metadata) != expected_fields
        or metadata.get("schema_version") != 1
        or metadata.get("capsule_sha256") != capsule_sha256
        or canonical_json_digest(
            {
                key: value
                for key, value in metadata.items()
                if key != "capsule_sha256"
            }
        )
        != capsule_sha256
        or metadata.get("operation_id") != operation_id
        or metadata.get("authority_sha") != authority_sha
        or metadata.get("target_sha") != target_sha
        or metadata.get("descriptor_sha256") != descriptor_sha256
        or not isinstance(metadata.get("control_release_id"), str)
        or re.fullmatch(r"^[0-9a-f]{64}$", metadata["control_release_id"])
        is None
        or not isinstance(metadata.get("takeover_operation_id"), str)
        or TAKEOVER_RE.fullmatch(metadata["takeover_operation_id"]) is None
    ):
        raise RecoveryError("bridge recovery capsule identity differs")
    files = metadata.get("files")
    if not isinstance(files, dict) or set(files) != CAPSULE_FILES:
        raise RecoveryError("bridge recovery capsule file inventory differs")
    control = root / "control"
    require_private_directory(control)
    if {
        path.name
        for path in control.iterdir()
        if path.name != "__pycache__"
    } != CAPSULE_FILES:
        raise RecoveryError("bridge recovery capsule contains extra control files")
    for name in sorted(CAPSULE_FILES):
        record = files.get(name)
        path = control / name
        try:
            file_metadata = path.lstat()
        except OSError as exc:
            raise RecoveryError(
                f"bridge recovery control is unavailable: {name}"
            ) from exc
        if (
            not isinstance(record, dict)
            or set(record) != {"sha256", "mode"}
            or record.get("mode") != "0700"
            or DIGEST_RE.fullmatch(str(record.get("sha256"))) is None
            or not stat.S_ISREG(file_metadata.st_mode)
            or path.is_symlink()
            or file_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(file_metadata.st_mode) != 0o700
            or sha256_file(path) != record["sha256"]
        ):
            raise RecoveryError(f"bridge recovery control changed: {name}")
    descriptor_path = root / "descriptor.json"
    descriptor = load_private_json(descriptor_path)
    if sha256_file(descriptor_path) != descriptor_sha256:
        raise RecoveryError("bridge recovery descriptor digest differs")
    return root, metadata, descriptor


def validate_descriptor(
    descriptor: dict[str, Any],
    metadata: dict[str, Any],
    bridge_core: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if (
        descriptor.get("schema_version") != 3
        or descriptor.get("operation_id") != metadata["operation_id"]
        or descriptor.get("previous_deployment") is not None
        or not isinstance(descriptor.get("repository"), dict)
        or descriptor["repository"].get("target_sha")
        != metadata["target_sha"]
        or not isinstance(descriptor.get("controller"), dict)
        or not isinstance(
            descriptor["controller"].get("executor_control"), dict
        )
        or descriptor["controller"]["executor_control"].get("release_id")
        != metadata["control_release_id"]
        or descriptor["controller"].get("executor_control_sha256")
        != canonical_json_digest(
            descriptor["controller"]["executor_control"]
        )
    ):
        raise RecoveryError("bridge recovery descriptor identity differs")
    try:
        bridge = bridge_core.validate_bridge_descriptor(descriptor.get("bridge"))
    except Exception as exc:
        raise RecoveryError("bridge recovery authority descriptor is invalid") from exc
    if (
        bridge.get("operation_id") != metadata["operation_id"]
        or bridge["authority"].get("sha") != metadata["authority_sha"]
        or bridge["target"].get("sha") != metadata["target_sha"]
    ):
        raise RecoveryError("bridge recovery authority/target differs")
    takeover = descriptor.get("legacy_takeover")
    takeover_fields = {
        "schema_version",
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
        "binding_sha256",
    }
    if (
        not isinstance(takeover, dict)
        or set(takeover) != takeover_fields
        or takeover.get("schema_version") != 1
        or takeover.get("operation_id")
        != metadata["takeover_operation_id"]
        or takeover.get("authority_sha") != metadata["authority_sha"]
        or takeover.get("binding_sha256")
        != canonical_json_digest(
            {
                key: value
                for key, value in takeover.items()
                if key != "binding_sha256"
            }
        )
    ):
        raise RecoveryError("bridge recovery takeover binding differs")
    return bridge, takeover


def validate_restored_takeover(
    takeover: dict[str, Any],
    legacy_evidence: Any,
    expected_terminal: str,
) -> dict[str, Any]:
    try:
        status = legacy_evidence.load_status(
            RUNTIME_ROOT, takeover["operation_id"]
        )
        restored = legacy_evidence.validate_restored(
            RUNTIME_ROOT,
            takeover["operation_id"],
            takeover["authority_sha"],
            takeover["authority_tree"],
            expected_git_identity=takeover["git_identity"],
            status_document=status,
        )
    except Exception as exc:
        raise RecoveryError("exact legacy takeover restore is invalid") from exc
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
    ):
        if restored.get(name) != takeover.get(name):
            raise RecoveryError("legacy takeover restore authority differs")
    if restored.get("restored_terminal_sha256") != expected_terminal:
        raise RecoveryError("legacy takeover restore terminal differs")
    return restored


@contextlib.contextmanager
def deploy_lock() -> Any:
    path = RUNTIME_ROOT / "state/deploy.lock"
    try:
        descriptor = os.open(
            path,
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise RecoveryError("deploy.lock is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise RecoveryError("deploy.lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield descriptor
    except BlockingIOError as exc:
        raise RecoveryError("another deployment holds deploy.lock") from exc
    finally:
        os.close(descriptor)


def ensure_private_descendant(path: Path) -> None:
    require_private_directory(RUNTIME_ROOT)
    current = RUNTIME_ROOT
    try:
        relative = path.relative_to(RUNTIME_ROOT)
    except ValueError as exc:
        raise RecoveryError("recovery audit escapes runtime root") from exc
    for component in relative.parts:
        current = current / component
        require_private_directory(current, create=True)


def idempotent_audit(
    path: Path, marker: dict[str, Any], status: str
) -> None:
    expected = {**marker, "status": status}
    if path.exists() or path.is_symlink():
        existing = load_private_json(path)
        recorded_at = existing.pop("recorded_at", None)
        if (
            not isinstance(recorded_at, str)
            or not recorded_at
            or existing != expected
        ):
            raise RecoveryError("terminal bridge recovery audit differs")
        return
    atomic_json(path, {**expected, "recorded_at": utc_now()})


def idempotent_failed_state(
    path: Path, operation_id: str, descriptor_sha256: str
) -> None:
    expected = {
        "schema_version": 1,
        "operation_id": operation_id,
        "descriptor_sha256": descriptor_sha256,
        "outcome": "failed",
    }
    if path.exists() or path.is_symlink():
        existing = load_private_json(path)
        recorded_at = existing.pop("recorded_at", None)
        if (
            not isinstance(recorded_at, str)
            or not recorded_at
            or existing != expected
        ):
            raise RecoveryError("terminal bridge operation state differs")
        return
    atomic_json(path, {**expected, "recorded_at": utc_now()})


def finalize(args: argparse.Namespace) -> dict[str, Any]:
    root, metadata, descriptor = load_capsule(
        capsule_sha256=args.capsule_sha256,
        operation_id=args.operation_id,
        authority_sha=args.authority_sha,
        target_sha=args.target_sha,
        descriptor_sha256=args.descriptor_sha256,
    )
    bridge_core = load_module(
        "nexpoly_bridge_recovery_core",
        root / "control/bridge_deploy_core.py",
    )
    legacy_evidence = load_module(
        "nexpoly_bridge_recovery_legacy_evidence",
        root / "control/legacy_takeover_evidence.py",
    )
    bridge, takeover = validate_descriptor(
        descriptor, metadata, bridge_core
    )
    expected_terminal = require_digest(
        args.restored_terminal_sha256,
        "legacy takeover restored terminal",
    )
    marker_path = RUNTIME_ROOT / "state/deploy-in-progress.json"
    current_path = RUNTIME_ROOT / "state/current-deployment.json"
    active_slot_path = RUNTIME_ROOT / "state/monomer-md-active-slot.json"
    with deploy_lock():
        # Revalidate every content address and terminal runtime fact after
        # taking the same global lock used by bootstrap, Pull and takeover.
        root, locked_metadata, locked_descriptor = load_capsule(
            capsule_sha256=args.capsule_sha256,
            operation_id=args.operation_id,
            authority_sha=args.authority_sha,
            target_sha=args.target_sha,
            descriptor_sha256=args.descriptor_sha256,
        )
        if locked_metadata != metadata or locked_descriptor != descriptor:
            raise RecoveryError("bridge recovery capsule changed before lock")
        restored = validate_restored_takeover(
            takeover, legacy_evidence, expected_terminal
        )
        if current_path.exists() or current_path.is_symlink():
            raise RecoveryError(
                "bridge recovery cannot retire after current state committed"
            )
        if active_slot_path.exists() or active_slot_path.is_symlink():
            raise RecoveryError(
                "bridge recovery found an active governed Worker slot"
            )
        marker = load_private_json(marker_path)
        capsule_binding = {
            "capsule_sha256": metadata["capsule_sha256"],
            "descriptor_sha256": metadata["descriptor_sha256"],
            "control_release_id": metadata["control_release_id"],
            "recovery_entry_sha256": metadata["files"][
                "bridge_recovery_capsule.py"
            ]["sha256"],
        }
        restore_intent = marker.get("takeover_restore_started")
        if (
            marker.get("schema_version") != 2
            or marker.get("action") != "deploy"
            or marker.get("phase") != "failed"
            or marker.get("operation_id") != metadata["operation_id"]
            or marker.get("source_sha") != metadata["target_sha"]
            or marker.get("descriptor_sha256")
            != metadata["descriptor_sha256"]
            or marker.get("executor_control")
            != descriptor["controller"]["executor_control"]
            or marker.get("executor_control_sha256")
            != descriptor["controller"]["executor_control_sha256"]
            or marker.get("bridge_recovery_capsule") != capsule_binding
            or not isinstance(marker.get("started_at"), str)
            or not marker["started_at"]
            or not isinstance(marker.get("updated_at"), str)
            or not marker["updated_at"]
            or any(
                not isinstance(marker.get(field), bool)
                for field in (
                    "runtime_stopped",
                    "source_switched",
                    "slot_switched",
                    "control_switched",
                    "unit_switched",
                    "asset_switched",
                    "database_change_started",
                )
            )
            or not isinstance(restore_intent, dict)
            or set(restore_intent)
            != {
                "operation_id",
                "worker_unit_sha256",
                "control_layout_sha256",
                "checkout_permissions_sha256",
                "started_at",
            }
            or restore_intent.get("operation_id")
            != takeover["operation_id"]
            or not isinstance(restore_intent.get("started_at"), str)
            or not restore_intent["started_at"]
            or any(
                DIGEST_RE.fullmatch(str(restore_intent.get(name))) is None
                for name in (
                    "worker_unit_sha256",
                    "control_layout_sha256",
                    "checkout_permissions_sha256",
                )
            )
            or marker.get("database_change_started") is True
            and (
                marker.get("database_restored") is not True
                or marker.get("mutable_data_restored") is None
            )
            or marker.get("asset_switched") is not False
            or marker.get("slot_switched") is not False
        ):
            raise RecoveryError("bridge recovery marker authority differs")
        terminal = restored["restored_terminal_sha256"]
        existing_terminal = marker.get("takeover_restored_terminal_sha256")
        if existing_terminal not in {None, terminal}:
            raise RecoveryError("bridge recovery marker terminal changed")
        if existing_terminal is None:
            marker.update(
                {
                    "source_switched": False,
                    "slot_switched": False,
                    "control_switched": False,
                    "unit_switched": False,
                    "asset_switched": False,
                    "runtime_stopped": False,
                    "takeover_restored_terminal_sha256": terminal,
                    "updated_at": utc_now(),
                }
            )
            marker.pop("runtime_start_intent", None)
            marker.pop("verification", None)
            atomic_json(marker_path, marker)
        elif (
            any(
                marker.get(field) is not False
                for field in (
                    "source_switched",
                    "slot_switched",
                    "control_switched",
                    "unit_switched",
                    "asset_switched",
                    "runtime_stopped",
                )
            )
            or marker.get("runtime_start_intent") is not None
            or marker.get("verification") is not None
        ):
            raise RecoveryError("bridge recovery terminal marker is inconsistent")

        external = (
            RUNTIME_ROOT
            / "legacy-takeover/runtime/pull-terminal"
            / metadata["operation_id"]
        )
        normal = RUNTIME_ROOT / "audit" / metadata["operation_id"]
        if normal.exists() or normal.is_symlink():
            raise RecoveryError("bridge recovery has an ambiguous audit root")
        ensure_private_descendant(external)
        state_path = external / "operation-state.json"
        try:
            token = bridge_core.load_token_authority(
                RUNTIME_ROOT / "state"
            )
        except Exception as exc:
            raise RecoveryError(
                "bridge recovery token authority is invalid"
            ) from exc
        if (
            token.get("operation_id") != metadata["operation_id"]
            or token.get("descriptor_sha256")
            != metadata["descriptor_sha256"]
            or token.get("policy_id") != bridge["policy"]["policy_id"]
            or token.get("token_id") != bridge["token"]["token_id"]
            or token.get("token_sha256") != bridge["token"]["token_sha256"]
            or token.get("status")
            not in {"prepared", "retired-precommit"}
        ):
            raise RecoveryError("bridge recovery token authority differs")
        if token["status"] == "prepared":
            audit_path = external / "recovered-takeover-restore.json"
            idempotent_audit(
                audit_path, marker, "recovered-takeover-restore"
            )
            idempotent_failed_state(
                state_path,
                metadata["operation_id"],
                metadata["descriptor_sha256"],
            )
            try:
                token = bridge_core.retire_precommit_token(
                    RUNTIME_ROOT / "state",
                    operation_id=metadata["operation_id"],
                    descriptor_sha256=metadata["descriptor_sha256"],
                    operation_state_sha256=sha256_file(state_path),
                    terminal_audit_sha256=sha256_file(audit_path),
                    restored_terminal_sha256=terminal,
                    recovery_capsule_sha256=metadata["capsule_sha256"],
                )
            except Exception as exc:
                raise RecoveryError(
                    "bridge recovery token retirement failed"
                ) from exc
        else:
            retirement = token["retirement"]
            idempotent_failed_state(
                state_path,
                metadata["operation_id"],
                metadata["descriptor_sha256"],
            )
            matching_audits: list[Path] = []
            for path in external.glob("*.json"):
                if path.name == "operation-state.json":
                    continue
                load_private_json(path)
                if (
                    sha256_file(path)
                    == retirement["terminal_audit_sha256"]
                ):
                    matching_audits.append(path)
            if (
                sha256_file(state_path)
                != retirement["operation_state_sha256"]
                or retirement["restored_terminal_sha256"] != terminal
                or retirement["recovery_capsule_sha256"]
                != metadata["capsule_sha256"]
                or len(matching_audits) != 1
            ):
                raise RecoveryError("retired bridge recovery evidence differs")
        if token.get("status") != "retired-precommit":
            raise RecoveryError("bridge recovery token did not retire")
        marker_path.unlink()
        fsync_directory(marker_path.parent)
        return {
            "action": "bridge-recover-restored",
            "apply": True,
            "operation_id": metadata["operation_id"],
            "authority_sha": metadata["authority_sha"],
            "target_sha": metadata["target_sha"],
            "descriptor_sha256": metadata["descriptor_sha256"],
            "restored_terminal_sha256": terminal,
            "capsule_sha256": metadata["capsule_sha256"],
            "token_status": token["status"],
        }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--capsule-sha256", required=True)
    value.add_argument("--authority-sha", required=True)
    value.add_argument("--target-sha", required=True)
    value.add_argument("--operation-id", required=True)
    value.add_argument("--descriptor-sha256", required=True)
    value.add_argument("--restored-terminal-sha256", required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    try:
        document = finalize(parser().parse_args(argv))
    except (RecoveryError, OSError) as exc:
        print(f"bridge-recover: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
