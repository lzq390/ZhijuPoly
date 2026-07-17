#!/usr/bin/python3 -I -B
"""Install source-pinned, pre-bootstrap legacy takeover prerequisites."""

from __future__ import annotations

import argparse
import configparser
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
import types
from typing import Any, Callable, Iterator


def _load_git_source_trust() -> Any:
    module_name = "nexpoly_installer_git_source_trust"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = Path(__file__).with_name("git_source_trust.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Git source trust policy cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


GIT_SOURCE_TRUST = _load_git_source_trust()

SOURCE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
PRODUCTION_ROOT = Path("/data/lzq/gith/nexpoly")
REPOSITORY_SSH_URL = "git@github.com:lzq390/ZhijuPoly.git"
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
    "bridge_recovery_launcher.py": "scripts/bridge_recovery_launcher.py",
    "bootstrap_pull_deploy.py": "scripts/bootstrap_pull_deploy.py",
    "bridge_deploy_core.py": "scripts/bridge_deploy_core.py",
    "legacy_takeover.py": "scripts/legacy_takeover.py",
    "legacy_takeover_evidence.py": "scripts/legacy_takeover_evidence.py",
    "maintenance_prefetch.py": "scripts/maintenance_prefetch.py",
    "postgres_media_evidence.py": "scripts/postgres_media_evidence.py",
    "git_source_trust.py": "scripts/git_source_trust.py",
    "worker_slot_runtime.py": "scripts/worker_slot_runtime.py",
    "site_helper_contracts.py": "scripts/site_helper_contracts.py",
    "nexpoly-legacy-takeover": "scripts/nexpoly-legacy-takeover",
    "nexpoly-bridge-recover": "scripts/nexpoly-bridge-recover",
    "nexpoly-maintenance-prefetch": "scripts/nexpoly-maintenance-prefetch",
    "nexpoly-postgres-media-evidence": (
        "scripts/nexpoly-postgres-media-evidence"
    ),
}
ATTESTATION_FILES = {
    "bridge_recovery_capsule.py": "scripts/bridge_recovery_capsule.py",
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
    "postgres-media-registry.json.example": (
        "ops/config/postgres-media-registry.json.example"
    ),
    "postgres-media-audit-role.sql.example": (
        "ops/config/postgres-media-audit-role.sql.example"
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
    "mutable-data-audit-role.sql.example": (
        "ops/config/mutable-data-audit-role.sql.example"
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


def _module_from_payload(
    name: str,
    payload: bytes,
    *,
    filename: str,
    injected_modules: dict[str, Any] | None = None,
) -> Any:
    """Compile only commit-bound bytes, never a mutable worktree module."""

    module = types.ModuleType(name)
    module.__file__ = filename
    module.__package__ = ""
    replacements = {name: module, **(injected_modules or {})}
    previous = {
        key: sys.modules.get(key)
        for key in replacements
    }
    missing = {
        key
        for key in replacements
        if key not in sys.modules
    }
    try:
        sys.modules.update(replacements)
        exec(compile(payload, filename, "exec"), module.__dict__)
    except BaseException as exc:
        raise PrerequisiteInstallError(
            f"cannot compile F authority module: {filename}"
        ) from exc
    finally:
        for key in replacements:
            if key in missing:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = previous[key]
    return module


def _assert_minimal_private_source(root: Path) -> tuple[Path, Path]:
    """Reject unsafe/external Git layouts before the first Git invocation."""

    root = root.absolute()
    git_dir = root / ".git"
    try:
        parent = root.parent.lstat()
        root_metadata = root.lstat()
        git_metadata = git_dir.lstat()
        objects_metadata = (git_dir / "objects").lstat()
    except OSError as exc:
        raise PrerequisiteInstallError(
            "fresh F source layout is unavailable"
        ) from exc
    for path, metadata in (
        (root.parent, parent),
        (root, root_metadata),
        (git_dir, git_metadata),
        (git_dir / "objects", objects_metadata),
    ):
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise PrerequisiteInstallError(
                "fresh F source must be an owner-private standalone clone"
            )
    for relative in (
        ".git/commondir",
        ".git/info/grafts",
        ".git/objects/info/alternates",
        ".git/objects/info/http-alternates",
    ):
        marker = root / relative
        if marker.exists() or marker.is_symlink():
            raise PrerequisiteInstallError(
                "fresh F source uses external Git storage"
            )
    config = git_dir / "config"
    try:
        config_metadata = config.lstat()
        config_payload = config.read_bytes()
    except OSError as exc:
        raise PrerequisiteInstallError(
            "fresh F source Git config is unavailable"
        ) from exc
    if (
        not stat.S_ISREG(config_metadata.st_mode)
        or config.is_symlink()
        or config_metadata.st_uid != os.geteuid()
        or config_metadata.st_mode & 0o077
        or len(config_payload) > 1024 * 1024
    ):
        raise PrerequisiteInstallError(
            "fresh F source Git config is unsafe"
        )
    parser = configparser.RawConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(config_payload.decode("utf-8"))
    except (UnicodeError, configparser.Error) as exc:
        raise PrerequisiteInstallError(
            "fresh F source Git config is malformed"
        ) from exc
    allowed: dict[str, set[str]] = {
        "core": {
            "repositoryformatversion",
            "filemode",
            "bare",
            "logallrefupdates",
            "ignorecase",
            "precomposeunicode",
        },
        'remote "origin"': {"url", "fetch", "tagopt"},
        'branch "main"': {"remote", "merge", "vscode-merge-base"},
        "user": {"name", "email"},
    }
    for section in parser.sections():
        permitted = allowed.get(section.lower())
        keys = {
            key.lower()
            for key, _value in parser.items(section, raw=True)
        }
        if permitted is None or not keys.issubset(permitted):
            raise PrerequisiteInstallError(
                "fresh F source Git config contains unsupported policy"
            )
    return git_dir, root


def _authority_git_environment(root: Path) -> dict[str, str]:
    _assert_minimal_private_source(root)
    try:
        return GIT_SOURCE_TRUST.safe_git_environment(
            root,
            ambient=os.environ,
        )
    except Exception as exc:
        raise PrerequisiteInstallError(
            "fresh F source Git environment is unsafe"
        ) from exc


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
    if SHA_RE.fullmatch(authority_sha) is None:
        raise PrerequisiteInstallError("full F authority SHA is required")
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or ".." in relative_path.parts
        or relative_path.as_posix() != relative
    ):
        raise PrerequisiteInstallError("F authority path is invalid")
    try:
        preflight = GIT_SOURCE_TRUST.repository_preflight_evidence(
            root,
            ambient=os.environ,
        )
        result = subprocess.run(
            GIT_SOURCE_TRUST.safe_git_command(
                root,
                "cat-file",
                "blob",
                f"{authority_sha}:{relative}",
            ),
            cwd=root,
            env=_authority_git_environment(root),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception as exc:
        raise PrerequisiteInstallError(
            f"cannot read F authority blob: {relative}"
        ) from exc
    try:
        tree = subprocess.run(
            GIT_SOURCE_TRUST.safe_git_command(
                root,
                "rev-parse",
                f"{authority_sha}^{{tree}}",
            ),
            cwd=root,
            env=_authority_git_environment(root),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        origin = subprocess.run(
            GIT_SOURCE_TRUST.safe_git_command(
                root,
                "remote",
                "get-url",
                "origin",
            ),
            cwd=root,
            env=_authority_git_environment(root),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        evidence = GIT_SOURCE_TRUST.repository_trust_evidence(
            root,
            source_sha=authority_sha,
            source_tree=tree,
            branch="refs/heads/main",
            origin=origin,
            ambient=os.environ,
        )
        GIT_SOURCE_TRUST.require_stable_trust_surface(preflight, evidence)
    except Exception as exc:
        raise PrerequisiteInstallError(
            "F authority Git trust evidence changed"
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
    authority_reader: Callable[[Path, str, str], bytes] = _authority_payload,
) -> dict[str, Any]:
    if (
        SHA_RE.fullmatch(authority_sha) is None
        or SHA_RE.fullmatch(authority_tree) is None
    ):
        raise PrerequisiteInstallError("full F commit and tree are required")
    if readiness is None:
        bootstrap_payload = authority_reader(
            source_root,
            authority_sha,
            "scripts/bootstrap_pull_deploy.py",
        )
        bootstrap = _module_from_payload(
            "legacy_takeover_bootstrap_readiness",
            bootstrap_payload,
            filename=(
                f"git:{authority_sha}:scripts/bootstrap_pull_deploy.py"
            ),
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
        or report.get("schema_version") != 2
        or report.get("ready") is not True
        or report.get("source_sha") != authority_sha
        or report.get("source_tree") != authority_tree
        or report.get("branch") != "main"
        or report.get("origin") != REPOSITORY_SSH_URL
        or report.get("remote_names") != ["origin"]
        or report.get("origin_fetch_urls") != [REPOSITORY_SSH_URL]
        or report.get("origin_push_urls") != [REPOSITORY_SSH_URL]
        or report.get("origin_main_sha") != authority_sha
        or report.get("standalone_object_database") is not True
        or report.get("dirty_entries") != 0
        or report.get("ignored_entries") != 0
        or report.get("unreachable_objects") != 0
        or report.get("replace_refs") != 0
        or report.get("special_index_entries") != 0
        or report.get("sparse_index") is not False
    ):
        raise PrerequisiteInstallError("fresh F source identity differs")
    if authority_reader is _authority_payload:
        try:
            preflight = GIT_SOURCE_TRUST.repository_preflight_evidence(
                source_root,
                ambient=os.environ,
            )
            trust = GIT_SOURCE_TRUST.repository_trust_evidence(
                source_root,
                source_sha=authority_sha,
                source_tree=authority_tree,
                branch="refs/heads/main",
                origin=REPOSITORY_SSH_URL,
                ambient=os.environ,
            )
            GIT_SOURCE_TRUST.require_stable_trust_surface(
                preflight,
                trust,
            )
        except Exception as exc:
            raise PrerequisiteInstallError(
                "fresh F Git trust evidence changed"
            ) from exc
        report = {**report, "git_source_trust": trust}
    return report


def _production_ignored_inventory(
    production_root: Path,
) -> tuple[list[str], dict[str, Any]]:
    """Enumerate ignored paths under the same sealed Git interpretation."""

    try:
        preflight = GIT_SOURCE_TRUST.repository_preflight_evidence(
            production_root,
            ambient=os.environ,
        )
        environment = GIT_SOURCE_TRUST.safe_git_environment(
            production_root,
            ambient=os.environ,
        )

        def git(*arguments: str, text: bool = True) -> str | bytes:
            return subprocess.run(
                GIT_SOURCE_TRUST.safe_git_command(
                    production_root,
                    *arguments,
                ),
                cwd=production_root,
                env=environment,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=text,
            ).stdout

        branch = str(git("symbolic-ref", "--quiet", "HEAD")).strip()
        source_sha = str(
            git("rev-parse", "--verify", "HEAD^{commit}")
        ).strip()
        source_tree = str(
            git("rev-parse", "--verify", "HEAD^{tree}")
        ).strip()
        origin = str(git("remote", "get-url", "origin")).strip()
        payload = bytes(
            git(
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--directory",
                "--no-empty-directory",
                "-z",
                text=False,
            )
        )
        evidence = GIT_SOURCE_TRUST.repository_trust_evidence(
            production_root,
            source_sha=source_sha,
            source_tree=source_tree,
            branch=branch,
            origin=origin,
            ambient=os.environ,
        )
        GIT_SOURCE_TRUST.require_stable_trust_surface(preflight, evidence)
    except Exception as exc:
        raise PrerequisiteInstallError(
            "cannot enumerate trusted production ignored paths"
        ) from exc
    try:
        ignored = [
            value.decode("utf-8").removesuffix("/")
            for value in payload.split(b"\0")
            if value
        ]
    except UnicodeError as exc:
        raise PrerequisiteInstallError(
            "production ignored path inventory is not UTF-8"
        ) from exc
    return ignored, evidence


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
        authority_reader=authority_reader,
    )
    contracts_payload = _bound_source_payload(
        source_root,
        authority_sha,
        "scripts/site_helper_contracts.py",
        authority_reader,
    )
    contracts = _module_from_payload(
        "legacy_takeover_install_contracts",
        contracts_payload,
        filename=f"git:{authority_sha}:scripts/site_helper_contracts.py",
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
        authority_reader=authority_reader,
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
    legacy = _module_from_payload(
        "legacy_takeover_install_validator",
        source_payloads["legacy_takeover.py"],
        filename=f"git:{authority_sha}:scripts/legacy_takeover.py",
        injected_modules={
            "site_helper_contracts": contracts,
            "nexpoly_legacy_git_source_trust": GIT_SOURCE_TRUST,
        },
    )
    production_source_trust: dict[str, Any] | None = None
    if ignored_paths is None:
        ignored_paths, production_source_trust = (
            _production_ignored_inventory(production_root)
        )
    if production_source_trust is not None:
        plan["production_source_trust"] = production_source_trust
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
        authority_reader=authority_reader,
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
        "production_source_trust_sha256": (
            production_source_trust["evidence_sha256"]
            if production_source_trust is not None
            else None
        ),
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
