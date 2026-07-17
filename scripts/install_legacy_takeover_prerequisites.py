#!/usr/bin/python3 -I -B
"""Install source-pinned, pre-bootstrap legacy takeover prerequisites."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
from typing import Any, Callable, Iterator


SOURCE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
PRODUCTION_ROOT = Path("/data/lzq/gith/nexpoly")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SITE_IMPLEMENTATION_MARKER = b"SITE_IMPLEMENTATION_REQUIRED"
REVIEWED_WRAPPERS = {
    "bootstrap-quiesce": "ops/config/bootstrap-quiesce.example",
    "bootstrap-status": "ops/config/bootstrap-status.example",
    "bootstrap-resume-unchanged": (
        "ops/config/bootstrap-resume-unchanged.example"
    ),
    "bootstrap-rollback": "ops/config/bootstrap-rollback.example",
}
RECOVERY_FILES = {
    "legacy_takeover.py": "scripts/legacy_takeover.py",
    "legacy_takeover_evidence.py": "scripts/legacy_takeover_evidence.py",
    "site_helper_contracts.py": "scripts/site_helper_contracts.py",
    "nexpoly-legacy-takeover": "scripts/nexpoly-legacy-takeover",
}
ATTESTATION_FILES = {
    "install_legacy_takeover_prerequisites.py": (
        "scripts/install_legacy_takeover_prerequisites.py"
    ),
    "bootstrap-active-jobs-probe.example": (
        "ops/config/bootstrap-active-jobs-probe.example"
    ),
    "bootstrap-legacy-runtime-status.example": (
        "ops/config/bootstrap-legacy-runtime-status.example"
    ),
    "bootstrap-legacy-runtime-resume-unchanged.example": (
        "ops/config/bootstrap-legacy-runtime-resume-unchanged.example"
    ),
    "bootstrap-legacy-runtime-restore.example": (
        "ops/config/bootstrap-legacy-runtime-restore.example"
    ),
    "contract-0012-external-database-audit.example": (
        "ops/config/contract-0012-external-database-audit.example"
    ),
    "legacy-takeover-classification.json.example": (
        "ops/config/legacy-takeover-classification.json.example"
    ),
    "mutable-data-audit.pg_service.conf.example": (
        "ops/config/mutable-data-audit.pg_service.conf.example"
    ),
    "mutable-data-audit.pgpass.example": (
        "ops/config/mutable-data-audit.pgpass.example"
    ),
}
CLASSIFICATION_NAME = "legacy-takeover-classification.json"
MUTABLE_SERVICE_NAME = "mutable-data-audit.pg_service.conf"
MUTABLE_PGPASS_NAME = "mutable-data-audit.pgpass"


class PrerequisiteInstallError(RuntimeError):
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


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PrerequisiteInstallError(f"cannot load source module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _private_directory(path: Path, *, create: bool = False) -> None:
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PrerequisiteInstallError(
            f"private directory is unavailable: {path}"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise PrerequisiteInstallError(f"private directory is unsafe: {path}")


def _private_file(path: Path, mode: int) -> bytes:
    try:
        metadata = path.lstat()
        payload = path.read_bytes()
    except OSError as exc:
        raise PrerequisiteInstallError(f"input file is unavailable: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise PrerequisiteInstallError(f"input file is unsafe: {path}")
    return payload


def _source_payload(root: Path, relative: str) -> bytes:
    path = root / relative
    try:
        metadata = path.lstat()
        payload = path.read_bytes()
    except OSError as exc:
        raise PrerequisiteInstallError(
            f"reviewed source file is unavailable: {relative}"
        ) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        raise PrerequisiteInstallError(
            f"reviewed source file is unsafe: {relative}"
        )
    return payload


def _validate_mutable_pgpass(payload: bytes) -> None:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrerequisiteInstallError(
            "mutable-data audit pgpass is not UTF-8"
        ) from exc
    lines = text.splitlines()
    if (
        len(lines) != 1
        or not text.endswith("\n")
        or "\x00" in text
    ):
        raise PrerequisiteInstallError(
            "mutable-data audit pgpass must contain exactly one record"
        )
    fields = lines[0].split(":", 4)
    if (
        len(fields) != 5
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
        raise PrerequisiteInstallError(
            "mutable-data audit pgpass identity or secret is invalid"
        )


def _authority_payload(root: Path, authority_sha: str, relative: str) -> bytes:
    try:
        result = subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(root),
                "show",
                f"{authority_sha}:{relative}",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PrerequisiteInstallError(
            f"cannot read F authority blob: {relative}"
        ) from exc
    return result.stdout


def _bound_source_payload(
    root: Path,
    authority_sha: str,
    relative: str,
    authority_reader: Callable[[Path, str, str], bytes],
) -> bytes:
    live = _source_payload(root, relative)
    authority = authority_reader(root, authority_sha, relative)
    if live != authority:
        raise PrerequisiteInstallError(
            f"reviewed source differs from F authority blob: {relative}"
        )
    return authority


@contextmanager
def _global_deploy_lock(runtime_root: Path) -> Iterator[None]:
    _private_directory(runtime_root)
    state = runtime_root / "state"
    _private_directory(state)
    path = state / "deploy.lock"
    descriptor = os.open(
        path,
        os.O_RDWR
        | os.O_CREAT
        | os.O_CLOEXEC
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise PrerequisiteInstallError("global deploy lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PrerequisiteInstallError(
                "another process holds the global deploy lock"
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _install_exact(path: Path, payload: bytes, mode: int) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private_directory(path.parent)
    expected_digest = sha256_bytes(payload)
    if path.exists() or path.is_symlink():
        actual = _private_file(path, mode)
        if actual != payload:
            raise PrerequisiteInstallError(
                f"installed prerequisite conflicts: {path}"
            )
        return {
            "path": str(path),
            "mode": f"{mode:04o}",
            "sha256": expected_digest,
            "installed": False,
        }
    prefix = f".{path.name}.install-"
    for stale in path.parent.glob(f"{prefix}*"):
        try:
            metadata = stale.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stale.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            raise PrerequisiteInstallError(
                f"unsafe stale prerequisite temp exists: {stale}"
            )
        stale.unlink()
    temporary = path.parent / f"{prefix}{secrets.token_hex(16)}"
    descriptor: int | None = None
    installed = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
            installed = True
        except FileExistsError:
            actual = _private_file(path, mode)
            if actual != payload:
                raise PrerequisiteInstallError(
                    f"installed prerequisite conflicts: {path}"
                )
        temporary.unlink()
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise PrerequisiteInstallError(
            f"cannot install prerequisite: {path}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    _private_file(path, mode)
    return {
        "path": str(path),
        "mode": f"{mode:04o}",
        "sha256": expected_digest,
        "installed": installed,
    }


def verify_fresh_source(
    source_root: Path,
    *,
    authority_sha: str,
    authority_tree: str,
    readiness: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if (
        SHA_RE.fullmatch(authority_sha) is None
        or SHA_RE.fullmatch(authority_tree) is None
    ):
        raise PrerequisiteInstallError("full F commit and tree are required")
    if readiness is None:
        bootstrap = _load_module(
            "legacy_takeover_bootstrap_readiness",
            source_root / "scripts/bootstrap_pull_deploy.py",
        )
        readiness = bootstrap.bootstrap_source_readiness
    try:
        report = readiness(source_root, expected_sha=authority_sha)
    except Exception as exc:
        raise PrerequisiteInstallError(
            "fresh F source readiness failed"
        ) from exc
    if (
        not isinstance(report, dict)
        or report.get("ready") is not True
        or report.get("source_sha") != authority_sha
        or report.get("source_tree") != authority_tree
        or report.get("standalone_object_database") is not True
        or report.get("dirty_entries") != 0
        or report.get("ignored_entries") != 0
        or report.get("unreachable_objects") != 0
    ):
        raise PrerequisiteInstallError("fresh F source identity differs")
    return report


def _install_prerequisites_locked(
    *,
    source_root: Path,
    runtime_root: Path,
    authority_sha: str,
    authority_tree: str,
    apply: bool,
    readiness: Callable[..., dict[str, Any]] | None = None,
    authority_reader: Callable[[Path, str, str], bytes] = _authority_payload,
    production_root: Path = PRODUCTION_ROOT,
    ignored_paths: list[str] | None = None,
) -> dict[str, Any]:
    source_root = source_root.absolute()
    runtime_root = runtime_root.absolute()
    source_report = verify_fresh_source(
        source_root,
        authority_sha=authority_sha,
        authority_tree=authority_tree,
        readiness=readiness,
    )
    contracts = _load_module(
        "legacy_takeover_install_contracts",
        source_root / "scripts/site_helper_contracts.py",
    )
    site_names = sorted(set(contracts.HELPERS) - set(REVIEWED_WRAPPERS))
    staging = runtime_root / "bootstrap-input"
    config = runtime_root / "config"
    recovery_bin = runtime_root / "legacy-takeover/bin"
    source_payloads = {
        name: _bound_source_payload(
            source_root,
            authority_sha,
            relative,
            authority_reader,
        )
        for name, relative in {
            **REVIEWED_WRAPPERS,
            **RECOVERY_FILES,
            **ATTESTATION_FILES,
        }.items()
    }
    verify_fresh_source(
        source_root,
        authority_sha=authority_sha,
        authority_tree=authority_tree,
        readiness=readiness,
    )
    plan = {
        "schema_version": 1,
        "authority_sha": authority_sha,
        "authority_tree": authority_tree,
        "source_readiness": source_report,
        "source_hashes": {
            name: sha256_bytes(payload)
            for name, payload in sorted(source_payloads.items())
        },
        "site_helpers": site_names,
        "runtime_root": str(runtime_root),
        "apply": apply,
    }
    if not apply:
        return plan
    _private_directory(runtime_root, create=True)
    _private_directory(staging)
    _private_directory(config, create=True)
    _private_directory(runtime_root / "legacy-takeover", create=True)
    _private_directory(recovery_bin, create=True)
    mutable_pgpass = _private_file(
        config / MUTABLE_PGPASS_NAME,
        0o600,
    )
    _validate_mutable_pgpass(mutable_pgpass)
    site_payloads = {
        name: _private_file(staging / name, 0o700)
        for name in site_names
    }
    for name, payload in site_payloads.items():
        if SITE_IMPLEMENTATION_MARKER in payload:
            raise PrerequisiteInstallError(
                f"site helper is still a fail-closed template: {name}"
            )
    classification = _private_file(
        staging / CLASSIFICATION_NAME,
        0o600,
    )
    legacy = _load_module(
        "legacy_takeover_install_validator",
        source_root / "scripts/legacy_takeover.py",
    )
    if ignored_paths is None:
        try:
            payload = subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(production_root),
                    "ls-files",
                    "--others",
                    "--ignored",
                    "--exclude-standard",
                    "--directory",
                    "--no-empty-directory",
                    "-z",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise PrerequisiteInstallError(
                "cannot enumerate production ignored paths"
            ) from exc
        ignored_paths = [
            value.decode("utf-8").removesuffix("/")
            for value in payload.split(b"\0")
            if value
        ]
    try:
        classification_document = json.loads(classification)
        legacy.validate_classification(
            classification_document,
            ignored_paths=ignored_paths,
        )
    except Exception as exc:
        raise PrerequisiteInstallError(
            "private classification map failed exact validation"
        ) from exc

    installed: dict[str, Any] = {}
    for name in sorted(REVIEWED_WRAPPERS):
        installed[name] = _install_exact(
            config / name,
            source_payloads[name],
            0o700,
        )
    for name, payload in sorted(site_payloads.items()):
        installed[name] = _install_exact(config / name, payload, 0o700)
    installed[CLASSIFICATION_NAME] = _install_exact(
        config / CLASSIFICATION_NAME,
        classification,
        0o600,
    )
    installed[MUTABLE_SERVICE_NAME] = _install_exact(
        config / MUTABLE_SERVICE_NAME,
        source_payloads[
            "mutable-data-audit.pg_service.conf.example"
        ],
        0o600,
    )
    installed[MUTABLE_PGPASS_NAME] = {
        "path": str(config / MUTABLE_PGPASS_NAME),
        "mode": "0600",
        "sha256": sha256_bytes(mutable_pgpass),
        "installed": False,
        "provisioned": True,
    }
    for name in sorted(RECOVERY_FILES):
        installed[name] = _install_exact(
            recovery_bin / name,
            source_payloads[name],
            0o700,
        )
    try:
        helper_report = contracts.inspect_helper_installation(runtime_root)
    except Exception as exc:
        raise PrerequisiteInstallError(
            "installed site-helper readiness failed"
        ) from exc
    for name, payload in site_payloads.items():
        if _private_file(staging / name, 0o700) != payload:
            raise PrerequisiteInstallError(
                f"private site helper drifted during installation: {name}"
            )
    if _private_file(staging / CLASSIFICATION_NAME, 0o600) != classification:
        raise PrerequisiteInstallError(
            "private classification drifted during installation"
        )
    current_pgpass = _private_file(
        config / MUTABLE_PGPASS_NAME,
        0o600,
    )
    _validate_mutable_pgpass(current_pgpass)
    if current_pgpass != mutable_pgpass:
        raise PrerequisiteInstallError(
            "mutable-data audit pgpass drifted during installation"
        )
    verify_fresh_source(
        source_root,
        authority_sha=authority_sha,
        authority_tree=authority_tree,
        readiness=readiness,
    )
    manifest = {
        "schema_version": 1,
        "authority_sha": authority_sha,
        "authority_tree": authority_tree,
        "source_hashes": plan["source_hashes"],
        "installed": {
            name: {
                "path": record["path"],
                "mode": record["mode"],
                "sha256": record["sha256"],
            }
            for name, record in sorted(installed.items())
        },
        "helper_report_sha256": sha256_bytes(
            canonical_json_bytes(helper_report)
        ),
        "classification_sha256": sha256_bytes(classification),
    }
    installed_manifest = _install_exact(
        runtime_root / "legacy-takeover/INSTALL-MANIFEST.json",
        canonical_json_bytes(manifest) + b"\n",
        0o600,
    )
    return {
        **plan,
        "ready": True,
        "installed": installed,
        "install_manifest": installed_manifest,
        "helper_report": helper_report,
        "classification_sha256": sha256_bytes(classification),
    }


def install_prerequisites(
    *,
    source_root: Path,
    runtime_root: Path,
    authority_sha: str,
    authority_tree: str,
    apply: bool,
    readiness: Callable[..., dict[str, Any]] | None = None,
    authority_reader: Callable[[Path, str, str], bytes] = _authority_payload,
    production_root: Path = PRODUCTION_ROOT,
    ignored_paths: list[str] | None = None,
) -> dict[str, Any]:
    source_root = source_root.absolute()
    runtime_root = runtime_root.absolute()
    if not apply:
        return _install_prerequisites_locked(
            source_root=source_root,
            runtime_root=runtime_root,
            authority_sha=authority_sha,
            authority_tree=authority_tree,
            apply=False,
            readiness=readiness,
            authority_reader=authority_reader,
            production_root=production_root,
            ignored_paths=ignored_paths,
        )
    # Provisioning of the fixed private runtime/state directories is an
    # explicit prerequisite. That lets this installer share exactly the same
    # lock inode and order as bootstrap, Pull and legacy takeover.
    _private_directory(runtime_root, create=False)
    _private_directory(runtime_root / "state", create=False)
    with _global_deploy_lock(runtime_root):
        return _install_prerequisites_locked(
            source_root=source_root,
            runtime_root=runtime_root,
            authority_sha=authority_sha,
            authority_tree=authority_tree,
            apply=True,
            readiness=readiness,
            authority_reader=authority_reader,
            production_root=production_root,
            ignored_paths=ignored_paths,
        )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--authority-sha", required=True)
    value.add_argument("--authority-tree", required=True)
    value.add_argument("--apply", action="store_true")
    return value


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        report = install_prerequisites(
            source_root=SOURCE_ROOT,
            runtime_root=RUNTIME_ROOT,
            authority_sha=arguments.authority_sha,
            authority_tree=arguments.authority_tree,
            apply=arguments.apply,
        )
    except PrerequisiteInstallError as exc:
        print(f"legacy-takeover-installer: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
