#!/usr/bin/python3 -I
"""Source-pinned provisioning for the mutable-data PostgreSQL audit LOGIN.

Planning is strictly read-only.  Apply accepts two independent confirmations,
holds the deployment lock, uses a crash-safe journal, and changes the role and
its locally generated SCRAM verifier in one PostgreSQL transaction.  The
plaintext password is never sent to PostgreSQL or placed in argv, output, the
journal, or the completion report.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import contextlib
import copy
import datetime as dt
import errno
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import stringprep
import subprocess
import sys
import unicodedata
from typing import Any, Iterator, Mapping


SOURCE_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
PRODUCTION_ROOT = Path("/data/lzq/gith/nexpoly")
SCRIPT_PATH = "scripts/provision_mutable_data_audit_role.py"
ROLE_SQL_PATH = "ops/config/mutable-data-audit-role.sql.example"
PGPASS_PATH = RUNTIME_ROOT / "config/mutable-data-audit.pgpass"
GIT_DEPLOY_KEY_PATH = RUNTIME_ROOT / "config/git-deploy-key"
GIT_KNOWN_HOSTS_PATH = RUNTIME_ROOT / "config/known_hosts"
DATABASE = "nexpoly"
ADMIN_USER = "polyprop"
AUDIT_USER = "nexpoly_mutable_audit"
HOST = "127.0.0.1"
PORT = "55432"
SSH_ORIGIN = "git@github.com:lzq390/ZhijuPoly.git"
ADOPTION_AUTHORITY_KIND = "manual-runtime-adoption"
PREREQUISITE_AUTHORITY_KIND = "manual-runtime-adoption-prerequisites"
SCRAM_ITERATIONS = 4096
SCRAM_SALT_BYTES = 16
GOVERNED_SCHEMAS = (
    "core",
    "dft",
    "experimental",
    "generation",
    "governance",
    "knowledge",
    "lab",
    "md",
    "model_registry",
    "monomer_dft",
    "online_knowledge",
    "pi",
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CONTAINER_RE = re.compile(r"^[0-9a-f]{64}$")
SYSTEM_ID_RE = re.compile(r"^[0-9]{10,30}$")
OPERATION_RE = re.compile(r"^mutable-role-[a-z0-9][a-z0-9._-]{7,95}$")
SCRAM_RE = re.compile(
    r"^SCRAM-SHA-256\$4096:[A-Za-z0-9+/]{22}==\$"
    r"[A-Za-z0-9+/]{43}=:[A-Za-z0-9+/]{43}=$"
)
JOURNAL_PHASES = (
    "intent",
    "database-commit-intent",
    "database-committed",
    "verified",
    "completed",
)


class RoleProvisionError(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RoleProvisionError(message)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _digest(value: object) -> str:
    return _digest_bytes(_canonical_bytes(value))


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = {
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }
    if extra:
        environment.update(extra)
    return environment


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    input_bytes: bytes | bytearray | memoryview | None = None,
    extra_environment: Mapping[str, str] | None = None,
    check: bool = True,
    timeout: int = 300,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=_clean_environment(extra_environment),
            input=input_bytes,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RoleProvisionError(
            f"controlled command did not complete: {Path(command[0]).name}"
        ) from exc
    if check and completed.returncode != 0:
        # Neither argv nor database diagnostics are repeated.  The transaction
        # input can contain a SCRAM verifier and login diagnostics can vary by
        # server configuration.
        raise RoleProvisionError(
            f"controlled command failed: {Path(command[0]).name}"
        )
    return completed


def _private_directory(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RoleProvisionError(f"private directory is unavailable: {path}") from exc
    _require(
        stat.S_ISDIR(metadata.st_mode)
        and not path.is_symlink()
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700,
        f"private directory is unsafe: {path}",
    )
    return metadata


def _read_private(
    path: Path, *, maximum_bytes: int, allowed_nlinks: frozenset[int] = frozenset({1})
) -> bytes:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise RoleProvisionError(f"private file is unavailable: {path}") from exc
    try:
        before = os.fstat(descriptor)
        _require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == os.geteuid()
            and stat.S_IMODE(before.st_mode) == 0o600
            and before.st_nlink in allowed_nlinks
            and 0 < before.st_size <= maximum_bytes,
            f"private file is unsafe: {path}",
        )
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            _require(bool(chunk), f"private file changed while reading: {path}")
            chunks.append(chunk)
            remaining -= len(chunk)
        _require(not os.read(descriptor, 1), f"private file grew while reading: {path}")
        after = os.fstat(descriptor)
        identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_uid,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        _require(identity(before) == identity(after), f"private file changed: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _load_private_json(path: Path) -> tuple[dict[str, Any], bytes]:
    payload = _read_private(path, maximum_bytes=16 * 1024 * 1024)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RoleProvisionError(f"private JSON is invalid: {path}") from exc
    _require(isinstance(value, dict), f"private JSON is not an object: {path}")
    return value, payload


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_private_directory(path: Path) -> None:
    parent = path.parent
    _private_directory(parent)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise RoleProvisionError(f"cannot create private directory: {path}") from exc
    _private_directory(path)
    _fsync_directory(parent)


def _write_once(path: Path, value: object) -> None:
    _private_directory(path.parent)
    payload = _canonical_bytes(value) + b"\n"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise RoleProvisionError(f"refusing to overwrite role authority: {path}") from exc
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _atomic_private_json(path: Path, value: object) -> None:
    _private_directory(path.parent)
    payload = _canonical_bytes(value) + b"\n"
    temporary = path.parent / f".{path.name}.next"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(temporary, flags, 0o600)
    except OSError as exc:
        raise RoleProvisionError("cannot create role journal staging file") from exc
    installed = False
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        installed = True
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not installed:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


@contextlib.contextmanager
def _deploy_lock(runtime_root: Path) -> Iterator[None]:
    _private_directory(runtime_root)
    _private_directory(runtime_root / "state")
    path = runtime_root / "state/deploy.lock"
    try:
        descriptor = os.open(
            path,
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise RoleProvisionError("deploy lock is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        _require(
            stat.S_ISREG(opened.st_mode)
            and opened.st_uid == os.geteuid()
            and stat.S_IMODE(opened.st_mode) == 0o600
            and opened.st_nlink == 1,
            "deploy lock is unsafe",
        )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = os.fstat(descriptor)
        current = path.lstat()
        _require(
            (
                locked.st_dev,
                locked.st_ino,
                locked.st_mode,
                locked.st_uid,
                locked.st_nlink,
                locked.st_size,
            )
            == (
                opened.st_dev,
                opened.st_ino,
                opened.st_mode,
                opened.st_uid,
                opened.st_nlink,
                opened.st_size,
            )
            and (current.st_dev, current.st_ino)
            == (locked.st_dev, locked.st_ino)
            and not path.is_symlink(),
            "deploy lock changed while locking",
        )
        yield
    finally:
        os.close(descriptor)


def _source_stat_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _source_directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_source_directory(path: Path, *, parent_fd: int | None = None) -> int:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise RoleProvisionError(f"source directory is unavailable: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        observed = (
            path.stat(follow_symlinks=False)
            if parent_fd is None
            else os.stat(path, dir_fd=parent_fd, follow_symlinks=False)
        )
        _require(
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and stat.S_IMODE(metadata.st_mode) == 0o700
            and _source_directory_identity(metadata)
            == _source_directory_identity(observed),
            f"source directory is unsafe: {path}",
        )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_source_file(path: Path, *, parent_fd: int | None = None) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    noatime = getattr(os, "O_NOATIME", 0)
    try:
        descriptor = os.open(path, flags | noatime, dir_fd=parent_fd)
    except OSError as exc:
        if noatime and exc.errno in {errno.EPERM, errno.EINVAL, errno.ENOTSUP}:
            try:
                descriptor = os.open(path, flags, dir_fd=parent_fd)
            except OSError as fallback:
                raise RoleProvisionError(
                    f"source file is unavailable: {path}"
                ) from fallback
        else:
            raise RoleProvisionError(f"source file is unavailable: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        observed = (
            path.stat(follow_symlinks=False)
            if parent_fd is None
            else os.stat(path, dir_fd=parent_fd, follow_symlinks=False)
        )
        _require(
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and not metadata.st_mode & 0o077
            and metadata.st_nlink == 1
            and _source_stat_identity(metadata) == _source_stat_identity(observed),
            f"source file is unsafe: {path}",
        )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _source_file_bytes(path: Path, *, maximum_bytes: int) -> bytes:
    descriptor = _open_source_file(path)
    try:
        before = os.fstat(descriptor)
        _require(
            0 <= before.st_size <= maximum_bytes,
            f"source file is oversized: {path}",
        )
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            _require(bool(chunk), f"source file was truncated: {path}")
            chunks.append(chunk)
            remaining -= len(chunk)
        _require(not os.read(descriptor, 1), f"source file grew: {path}")
        after = os.fstat(descriptor)
        _require(
            _source_stat_identity(before) == _source_stat_identity(after),
            f"source file changed while reading: {path}",
        )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _validate_pre_git_config(payload: bytes) -> None:
    parser = configparser.RawConfigParser(
        interpolation=None,
        strict=False,
        empty_lines_in_values=False,
    )
    try:
        parser.read_string(payload.decode("utf-8"))
    except (UnicodeError, configparser.Error) as exc:
        raise RoleProvisionError("source Git config is malformed") from exc
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
    normalized: dict[str, dict[str, str]] = {}
    for section in parser.sections():
        section_name = section.lower()
        permitted = allowed.get(section_name)
        _require(
            permitted is not None,
            f"source Git config contains executable or redirect policy: {section}",
        )
        values: dict[str, str] = {}
        for raw_key, raw_value in parser.items(section, raw=True):
            key = raw_key.lower()
            _require(
                key in permitted,
                "source Git config contains executable or redirect policy: "
                f"{section}.{raw_key}",
            )
            value = raw_value.strip()
            _require(
                "\x00" not in value and "\n" not in value and "\r" not in value,
                "source Git config value is unsafe",
            )
            values[key] = value
        normalized[section_name] = values
    core = normalized.get("core", {})
    remote = normalized.get('remote "origin"', {})
    branch = normalized.get('branch "main"', {})
    _require(
        core.get("repositoryformatversion", "0") == "0"
        and core.get("bare", "false").lower() in {"false", "no", "off", "0"}
        and remote.get("url") == SSH_ORIGIN
        and branch.get("remote", "origin") == "origin"
        and branch.get("merge", "refs/heads/main") == "refs/heads/main",
        "source Git config identity is invalid",
    )


def _validate_pre_git_attributes(payload: bytes, *, label: str) -> None:
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise RoleProvisionError(f"source Git attributes are malformed: {label}") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = stripped.split()
        attributes = {
            token.strip("\"'").lstrip("-!").split("=", 1)[0].lower()
            for token in tokens[1:]
        }
        _require(
            not attributes & {"filter", "diff", "merge", "textconv"},
            f"source contains executable Git attributes: {label}",
        )


def _walk_private_source(
    directory_fd: int,
    relative: Path,
    *,
    regular_paths: set[str],
    directory_paths: set[str],
) -> None:
    before = os.fstat(directory_fd)
    _require(
        stat.S_ISDIR(before.st_mode)
        and before.st_uid == os.geteuid()
        and stat.S_IMODE(before.st_mode) == 0o700,
        "source directory metadata is unsafe",
    )
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError as exc:
        raise RoleProvisionError("cannot inventory source before Git") from exc
    for name in names:
        _require(
            bool(name) and name not in {".", ".."} and "/" not in name and "\x00" not in name,
            "source entry name is unsafe",
        )
        child_relative = relative / name
        child_name = child_relative.as_posix()
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise RoleProvisionError(
                f"source entry is unavailable: {child_name}"
            ) from exc
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_source_directory(Path(name), parent_fd=directory_fd)
            try:
                directory_paths.add(child_name)
                _walk_private_source(
                    child_fd,
                    child_relative,
                    regular_paths=regular_paths,
                    directory_paths=directory_paths,
                )
                observed = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                _require(
                    _source_directory_identity(metadata)
                    == _source_directory_identity(observed),
                    f"source directory changed: {child_name}",
                )
            finally:
                os.close(child_fd)
            continue
        _require(
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and not metadata.st_mode & 0o077
            and metadata.st_nlink == 1,
            f"source entry is unsafe: {child_name}",
        )
        descriptor = _open_source_file(Path(name), parent_fd=directory_fd)
        try:
            _require(
                _source_stat_identity(metadata)
                == _source_stat_identity(os.fstat(descriptor)),
                f"source file changed: {child_name}",
            )
        finally:
            os.close(descriptor)
        regular_paths.add(child_name)
    _require(
        _source_directory_identity(before)
        == _source_directory_identity(os.fstat(directory_fd)),
        "source directory changed during inventory",
    )


def _paths_overlap(left: Path, right: Path) -> bool:
    left = left.absolute()
    right = right.absolute()
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _assert_pre_git_source_safety(root: Path) -> None:
    """Reject executable Git policy before every Git subprocess."""

    root = root.absolute()
    _require(
        not any(
            _paths_overlap(root, protected)
            for protected in (PRODUCTION_ROOT, RUNTIME_ROOT)
        ),
        "role source must be independent of production/runtime",
    )
    try:
        parent = root.parent.lstat()
    except OSError as exc:
        raise RoleProvisionError("role source parent is unavailable") from exc
    _require(
        stat.S_ISDIR(parent.st_mode)
        and not root.parent.is_symlink()
        and parent.st_uid == os.geteuid()
        and not parent.st_mode & 0o077,
        "role source parent is not private",
    )
    root_fd = _open_source_directory(root)
    try:
        regular_paths: set[str] = set()
        directory_paths: set[str] = set()
        _walk_private_source(
            root_fd,
            Path(),
            regular_paths=regular_paths,
            directory_paths=directory_paths,
        )
    finally:
        os.close(root_fd)
    _require(
        {".git", ".git/objects", ".git/refs"}.issubset(directory_paths)
        and {".git/config", ".git/HEAD", ".git/index"}.issubset(regular_paths),
        "role source Git layout is incomplete",
    )
    forbidden = {
        ".git/commondir",
        ".git/config.worktree",
        ".git/info/grafts",
        ".git/info/sparse-checkout",
        ".git/objects/info/alternates",
        ".git/objects/info/http-alternates",
        ".git/refs/replace",
        ".git/shallow",
    }
    _require(
        not forbidden & (regular_paths | directory_paths),
        "role source uses external or mutable Git policy",
    )
    _require(
        not any(
            path.startswith(".git/") and path.endswith(".lock")
            for path in regular_paths | directory_paths
        ),
        "role source has an active Git lock",
    )
    _require(
        _source_file_bytes(root / ".git/HEAD", maximum_bytes=4096).strip()
        == b"ref: refs/heads/main",
        "role source HEAD is not exact local main",
    )
    _validate_pre_git_config(
        _source_file_bytes(root / ".git/config", maximum_bytes=1024 * 1024)
    )
    attribute_paths = sorted(
        path
        for path in regular_paths
        if path == ".git/info/attributes"
        or path == ".gitattributes"
        or path.endswith("/.gitattributes") and not path.startswith(".git/")
    )
    for relative in attribute_paths:
        _validate_pre_git_attributes(
            _source_file_bytes(root / relative, maximum_bytes=1024 * 1024),
            label=relative,
        )


def _trusted_root_binary(path: Path) -> None:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RoleProvisionError(f"trusted executable is unavailable: {path}") from exc
    _require(
        stat.S_ISREG(metadata.st_mode)
        and not path.is_symlink()
        and resolved == path
        and metadata.st_uid == 0
        and not metadata.st_mode & 0o022,
        f"trusted executable is unsafe: {path}",
    )


def _credential_identity(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        observed = path.stat(follow_symlinks=False)
        _require(
            stat.S_ISREG(opened.st_mode)
            and opened.st_uid == os.geteuid()
            and stat.S_IMODE(opened.st_mode) == 0o600
            and opened.st_nlink == 1
            and 0 < opened.st_size <= 1024 * 1024
            and _source_stat_identity(opened) == _source_stat_identity(observed),
            f"Git credential material is unsafe: {path}",
        )
    finally:
        os.close(descriptor)


def _git_ssh_command() -> str:
    _trusted_root_binary(Path("/usr/bin/ssh"))
    for path in (GIT_DEPLOY_KEY_PATH, GIT_KNOWN_HOSTS_PATH):
        _credential_identity(path)
    return (
        f"/usr/bin/ssh -F /dev/null -i {GIT_DEPLOY_KEY_PATH} "
        "-o IdentitiesOnly=yes -o BatchMode=yes -o PasswordAuthentication=no "
        "-o KbdInteractiveAuthentication=no -o StrictHostKeyChecking=yes "
        f"-o UserKnownHostsFile={GIT_KNOWN_HOSTS_PATH} "
        "-o GlobalKnownHostsFile=/dev/null -o KnownHostsCommand=none "
        "-o ProxyCommand=none -o ProxyJump=none -o ProxyUseFdpass=no "
        "-o PermitLocalCommand=no -o LocalCommand=none -o IdentityAgent=none "
        "-o ForwardAgent=no -o ClearAllForwardings=yes -o ControlMaster=no "
        "-o ControlPath=none -o RequestTTY=no"
    )


def _git(*arguments: str, check: bool = True) -> bytes:
    _assert_pre_git_source_safety(SOURCE_ROOT)
    _trusted_root_binary(Path("/usr/bin/git"))
    ssh_command = _git_ssh_command()
    return _run(
        [
            "/usr/bin/git",
            "--no-optional-locks",
            "-c",
            "credential.helper=",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "core.sparseCheckout=false",
            "-c",
            "core.attributesFile=/dev/null",
            "-c",
            "core.excludesFile=/dev/null",
            "-c",
            "diff.external=",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.file.allow=never",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "protocol.ssh.allow=always",
            "-c",
            "fetch.fsckObjects=true",
            "-c",
            "transfer.fsckObjects=true",
            "-c",
            "fetch.writeCommitGraph=false",
            *arguments,
        ],
        cwd=SOURCE_ROOT,
        extra_environment={
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_PROTOCOL_FROM_USER": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_ASKPASS": "/bin/false",
            "SSH_ASKPASS": "/bin/false",
            "GIT_PAGER": "cat",
            "GIT_EDITOR": "/bin/false",
            "GIT_SEQUENCE_EDITOR": "/bin/false",
            "GIT_EXTERNAL_DIFF": "",
            "GIT_DIR": str(SOURCE_ROOT / ".git"),
            "GIT_WORK_TREE": str(SOURCE_ROOT),
            "GIT_OBJECT_DIRECTORY": str(SOURCE_ROOT / ".git/objects"),
            "GIT_INDEX_FILE": str(SOURCE_ROOT / ".git/index"),
            "GIT_SSH_COMMAND": ssh_command,
            "GIT_SSH_VARIANT": "ssh",
        },
        check=check,
    ).stdout


def _source_authority(source_sha: str) -> tuple[dict[str, str], bytes]:
    _require(SHA_RE.fullmatch(source_sha) is not None, "source SHA is invalid")
    _private_directory(SOURCE_ROOT)
    _private_directory(SOURCE_ROOT / ".git")
    head = _git("rev-parse", "--verify", "HEAD").decode().strip()
    tree = _git("rev-parse", "--verify", f"{source_sha}^{{tree}}").decode().strip()
    remote_main = _git("rev-parse", "--verify", "refs/remotes/origin/main").decode().strip()
    remote_lines = _git(
        "ls-remote",
        "--exit-code",
        SSH_ORIGIN,
        "refs/heads/main",
    ).decode().splitlines()
    _require(
        head == source_sha
        and remote_main == source_sha
        and SHA_RE.fullmatch(tree) is not None
        and remote_lines == [f"{source_sha}\trefs/heads/main"],
        "source is not the current remote main authority",
    )
    _require(
        not _git("status", "--porcelain=v1", "--untracked-files=all"),
        "source checkout is not clean",
    )
    _require(
        _git("remote").decode().splitlines() == ["origin"]
        and _git("remote", "get-url", "--all", "origin").decode().splitlines()
        == [SSH_ORIGIN]
        and _git("remote", "get-url", "--push", "--all", "origin").decode().splitlines()
        == [SSH_ORIGIN],
        "source origin is not the private SSH authority",
    )
    _require(
        _git("rev-parse", "--is-shallow-repository").decode().strip() == "false"
        and not _git("for-each-ref", "--format=%(refname)", "refs/replace/"),
        "source checkout uses shallow or replacement history",
    )
    for relative in (
        ".git/commondir",
        ".git/info/grafts",
        ".git/objects/info/alternates",
        ".git/objects/info/http-alternates",
    ):
        path = SOURCE_ROOT / relative
        _require(not path.exists() and not path.is_symlink(), "source object authority is external")
    blobs: dict[str, bytes] = {}
    for relative in (SCRIPT_PATH, ROLE_SQL_PATH):
        blob = _git("show", f"{source_sha}:{relative}")
        path = SOURCE_ROOT / relative
        _require(
            path.is_file() and not path.is_symlink() and path.read_bytes() == blob,
            f"source-controlled role artifact differs: {relative}",
        )
        blobs[relative] = blob
    role_sql = blobs[ROLE_SQL_PATH]
    _require(
        role_sql.startswith(b"\\set ON_ERROR_STOP on\n")
        and role_sql.count(b"__SCRAM_VERIFIER_LITERAL__") == 1
        and role_sql.count(b"__IN_TRANSACTION_SEALED_CAS__") == 1
        and role_sql.count(b"__IN_TRANSACTION_DESIRED_ASSERT__") == 1
        and role_sql.rstrip().endswith(b"COMMIT;"),
        "reviewed role SQL transaction contract is invalid",
    )
    return {
        "sha": source_sha,
        "tree": tree,
        "remote_main": remote_main,
        "script_sha256": _digest_bytes(blobs[SCRIPT_PATH]),
        "role_sql_sha256": _digest_bytes(role_sql),
    }, role_sql


def _strict_adopted_authority(source_sha: str) -> dict[str, str]:
    state = RUNTIME_ROOT / "state"
    adopted, adopted_payload = _load_private_json(state / "adopted-deployment.json")
    bootstrap, bootstrap_payload = _load_private_json(state / "bootstrap-control.json")
    active, active_payload = _load_private_json(state / "active-control.json")
    prerequisites, prerequisites_payload = _load_private_json(
        state / "adopted-prerequisites.json"
    )
    _require(
        adopted.get("schema_version") == 1
        and adopted.get("status") == "adopted"
        and adopted.get("authority_kind") == ADOPTION_AUTHORITY_KIND,
        "adopted deployment authority is invalid",
    )
    adoption = adopted.get("adoption_evidence")
    _require(
        isinstance(adoption, dict)
        and adopted.get("operation_id") == adoption.get("operation_id")
        and adopted.get("adoption_evidence_sha256") == _digest(adoption)
        and adopted.get("source_sha") == (adoption.get("live_repository") or {}).get("head")
        and adopted.get("source_tree") == (adoption.get("live_repository") or {}).get("tree")
        and all(
            adopted.get(name) == adoption.get(name)
            for name in (
                "images",
                "production_config",
                "asset_identity",
                "migrations",
                "database",
                "maintenance",
                "monomer_md",
                "monomer_dft",
            )
        ),
        "adopted deployment evidence binding is invalid",
    )
    readiness = bootstrap.get("source_readiness")
    delivery = bootstrap.get("delivery_gate")
    _require(
        bootstrap.get("schema_version") == 3
        and bootstrap.get("status") == "completed"
        and bootstrap.get("authority_kind") == ADOPTION_AUTHORITY_KIND
        and bootstrap.get("operation_id") == adopted.get("operation_id")
        and bootstrap.get("adopted_deployment") == adopted
        and bootstrap.get("adopted_deployment_sha256") == _digest(adopted)
        and bootstrap.get("adoption") == adoption
        and bootstrap.get("adoption_evidence_sha256") == _digest(adoption)
        and bootstrap.get("active_control") == active
        and adopted.get("active_control") == active
        and isinstance(readiness, dict)
        and readiness.get("ready") is True
        and readiness.get("branch") == "main"
        and readiness.get("origin") == SSH_ORIGIN
        and readiness.get("source_sha") == bootstrap.get("source_sha")
        and readiness.get("source_tree") == bootstrap.get("source_tree")
        and readiness.get("origin_main_sha") == bootstrap.get("source_sha")
        and bootstrap.get("source_readiness_sha256") == _digest(readiness)
        and isinstance(delivery, dict)
        and delivery.get("remote_main") == bootstrap.get("source_sha")
        and isinstance(delivery.get("ci"), dict)
        and delivery["ci"].get("head_sha") == bootstrap.get("source_sha")
        and delivery["ci"].get("conclusion") == "success",
        "bootstrap v3 adoption authority is invalid",
    )
    prereq_plan = prerequisites.get("plan")
    _require(
        prerequisites.get("schema_version") == 1
        and prerequisites.get("status") == "completed"
        and prerequisites.get("authority_kind") == PREREQUISITE_AUTHORITY_KIND
        and prerequisites.get("source_sha") == source_sha
        and isinstance(prereq_plan, dict)
        and prerequisites.get("source_tree") == prereq_plan.get("source_tree")
        and prerequisites.get("plan_sha256") == _digest(prereq_plan)
        and prereq_plan.get("source_sha") == source_sha
        and prereq_plan.get("authority_kind") == PREREQUISITE_AUTHORITY_KIND
        and prereq_plan.get("adopted_deployment_sha256") == _digest(adopted)
        and prereq_plan.get("delivery_gate_sha256") == _digest(prereq_plan.get("delivery_gate"))
        and isinstance(prereq_plan.get("delivery_gate"), dict)
        and prereq_plan["delivery_gate"].get("remote_main") == source_sha
        and isinstance(prereq_plan["delivery_gate"].get("ci"), dict)
        and prereq_plan["delivery_gate"]["ci"].get("head_sha") == source_sha
        and prereq_plan["delivery_gate"]["ci"].get("conclusion") == "success"
        and (prereq_plan.get("mutations") or {}).get("database") is False
        and (prereq_plan.get("mutations") or {}).get("credentials") is False,
        "completed adopted prerequisite authority is invalid",
    )
    for forbidden in (
        "deploy-in-progress.json",
        "contract-0012-in-progress.json",
        "current-deployment.json",
    ):
        path = state / forbidden
        _require(not path.exists() and not path.is_symlink(), "role prerequisite overlaps deployment state")
    return {
        "adopted_file_sha256": _digest_bytes(adopted_payload),
        "adopted_sha256": _digest(adopted),
        "bootstrap_file_sha256": _digest_bytes(bootstrap_payload),
        "active_file_sha256": _digest_bytes(active_payload),
        "prerequisites_file_sha256": _digest_bytes(prerequisites_payload),
        "prerequisites_plan_sha256": str(prerequisites["plan_sha256"]),
    }


def _split_pgpass_bytes(line: bytes) -> list[bytearray]:
    fields: list[bytearray] = []
    value = bytearray()
    escaped = False
    for character in line:
        if escaped:
            value.append(character)
            escaped = False
        elif character == 0x5C:
            escaped = True
        elif character == 0x3A and len(fields) < 4:
            fields.append(value)
            value = bytearray()
        else:
            value.append(character)
    _require(not escaped, "pgpass has a trailing escape")
    fields.append(value)
    return fields


def _pgpass_authority(path: Path) -> tuple[bytearray, str]:
    payload = _read_private(path, maximum_bytes=64 * 1024)
    lines = [line for line in payload.splitlines() if line and not line.startswith(b"#")]
    _require(len(lines) == 1, "mutable audit pgpass must contain one authority")
    fields = _split_pgpass_bytes(lines[0])
    try:
        _require(
            len(fields) == 5
            and [bytes(value) for value in fields[:4]]
            == [HOST.encode(), PORT.encode(), DATABASE.encode(), AUDIT_USER.encode()],
            "mutable audit pgpass endpoint identity differs",
        )
        password = fields[4]
        _require(
            16 <= len(password) <= 1024
            and b"\x00" not in password
            and b"\r" not in password
            and b"\n" not in password,
            "mutable audit password has an invalid shape",
        )
        fields[4] = bytearray()
        return password, _digest_bytes(payload)
    finally:
        for value in fields[:4]:
            value[:] = b"\x00" * len(value)


def _saslprep(password: bytearray) -> bytes:
    try:
        text = bytes(password).decode("utf-8")
    except UnicodeDecodeError:
        return bytes(password)
    mapped = "".join(
        " " if stringprep.in_table_c12(character) else character
        for character in text
        if not stringprep.in_table_b1(character)
    )
    normalized = unicodedata.normalize("NFKC", mapped)
    prohibited = (
        stringprep.in_table_a1,
        stringprep.in_table_c12,
        stringprep.in_table_c21,
        stringprep.in_table_c22,
        stringprep.in_table_c3,
        stringprep.in_table_c4,
        stringprep.in_table_c5,
        stringprep.in_table_c6,
        stringprep.in_table_c7,
        stringprep.in_table_c8,
        stringprep.in_table_c9,
    )
    if any(check(character) for character in normalized for check in prohibited):
        return bytes(password)
    if any(stringprep.in_table_d1(character) for character in normalized):
        if (
            any(stringprep.in_table_d2(character) for character in normalized)
            or not normalized
            or not stringprep.in_table_d1(normalized[0])
            or not stringprep.in_table_d1(normalized[-1])
        ):
            return bytes(password)
    return normalized.encode("utf-8")


def _scram_verifier(password: bytearray, *, salt: bytes | None = None) -> str:
    salt = os.urandom(SCRAM_SALT_BYTES) if salt is None else salt
    _require(len(salt) == SCRAM_SALT_BYTES, "SCRAM salt length is invalid")
    prepared = bytearray(_saslprep(password))
    salted = bytearray()
    client_key = bytearray()
    stored_key = bytearray()
    server_key = bytearray()
    try:
        salted.extend(
            hashlib.pbkdf2_hmac(
                "sha256", memoryview(prepared), salt, SCRAM_ITERATIONS
            )
        )
        client_key.extend(hmac.new(salted, b"Client Key", hashlib.sha256).digest())
        stored_key.extend(hashlib.sha256(memoryview(client_key)).digest())
        server_key.extend(hmac.new(salted, b"Server Key", hashlib.sha256).digest())
        verifier = (
            f"SCRAM-SHA-256${SCRAM_ITERATIONS}:"
            f"{base64.b64encode(salt).decode('ascii')}$"
            f"{base64.b64encode(stored_key).decode('ascii')}:"
            f"{base64.b64encode(server_key).decode('ascii')}"
        )
        _require(SCRAM_RE.fullmatch(verifier) is not None, "generated SCRAM verifier is invalid")
        return verifier
    finally:
        for secret in (prepared, salted, client_key, stored_key, server_key):
            secret[:] = b"\x00" * len(secret)


def _live_postgres() -> dict[str, Any]:
    ids = _run(
        [
            "docker",
            "ps",
            "--filter",
            "label=com.docker.compose.project=nexpoly",
            "--filter",
            "label=com.docker.compose.service=lab-postgres",
            "--format",
            "{{.ID}}",
        ]
    ).stdout.decode().splitlines()
    ids = [value.strip() for value in ids if value.strip()]
    _require(len(ids) == 1 and re.fullmatch(r"[0-9a-f]{12,64}", ids[0]) is not None, "production PostgreSQL container is ambiguous")
    inspected = _run(["docker", "container", "inspect", ids[0]]).stdout
    try:
        values = json.loads(inspected)
        record = values[0]
    except (json.JSONDecodeError, IndexError, TypeError) as exc:
        raise RoleProvisionError("production PostgreSQL inspect is invalid") from exc
    _require(isinstance(values, list) and len(values) == 1 and isinstance(record, dict), "production PostgreSQL inspect is ambiguous")
    container_id = str(record.get("Id", ""))
    image_id = str(record.get("Image", ""))
    config = record.get("Config") or {}
    labels = config.get("Labels") or {}
    state = record.get("State") or {}
    host = record.get("HostConfig") or {}
    mounts = record.get("Mounts") or []
    networks = (record.get("NetworkSettings") or {}).get("Networks") or {}
    expected_configs = f"{PRODUCTION_ROOT}/docker-compose.yml,{PRODUCTION_ROOT}/docker-compose.prod.yml"
    expected_env = f"{RUNTIME_ROOT}/config/deploy.env"
    _require(
        CONTAINER_RE.fullmatch(container_id) is not None
        and container_id.startswith(ids[0])
        and DIGEST_RE.fullmatch(image_id) is not None
        and record.get("Name") == "/nexpoly-lab-postgres-1"
        and record.get("Path") == "docker-entrypoint.sh"
        and record.get("Args") == ["postgres"]
        and config.get("Entrypoint") == ["docker-entrypoint.sh"]
        and config.get("Cmd") == ["postgres"]
        and config.get("User") in {"", None}
        and re.fullmatch(r"postgres:16-alpine@sha256:[0-9a-f]{64}", str(config.get("Image", ""))) is not None
        and config["Image"].rsplit("@", 1)[1] == image_id
        and labels.get("com.docker.compose.project") == "nexpoly"
        and labels.get("com.docker.compose.service") == "lab-postgres"
        and labels.get("com.docker.compose.container-number") == "1"
        and labels.get("com.docker.compose.oneoff") == "False"
        and labels.get("com.docker.compose.project.working_dir") == str(PRODUCTION_ROOT)
        and labels.get("com.docker.compose.project.config_files") == expected_configs
        and labels.get("com.docker.compose.project.environment_file") == expected_env
        and labels.get("com.docker.compose.image") == image_id
        and re.fullmatch(r"[0-9a-f]{64}", str(labels.get("com.docker.compose.config-hash", ""))) is not None
        and state.get("Running") is True
        and state.get("Restarting") is False
        and state.get("Paused") is False
        and state.get("Dead") is False
        and isinstance(state.get("Pid"), int)
        and state["Pid"] > 1
        and host.get("NetworkMode") == "nexpoly_default"
        and host.get("PortBindings")
        == {"5432/tcp": [{"HostIp": HOST, "HostPort": PORT}]}
        and host.get("RestartPolicy") == {"Name": "unless-stopped", "MaximumRetryCount": 0}
        and len(mounts) == 1
        and mounts[0].get("Type") == "volume"
        and mounts[0].get("Name") == "nexpoly_app_postgres_data"
        and mounts[0].get("Source")
        == "/var/lib/docker/volumes/nexpoly_app_postgres_data/_data"
        and mounts[0].get("Destination") == "/var/lib/postgresql/data"
        and mounts[0].get("RW") is True
        and set(networks) == {"nexpoly_default"}
        and re.fullmatch(r"[0-9a-f]{64}", str(networks["nexpoly_default"].get("NetworkID", ""))) is not None
        and {"lab-postgres", "nexpoly-lab-postgres-1"}.issubset(
            set(networks["nexpoly_default"].get("Aliases") or [])
        ),
        "production PostgreSQL runtime differs",
    )
    return {
        "container_id": container_id,
        "image_id": image_id,
        "image_ref": config["Image"],
        "compose_config_sha256": "sha256:" + str(labels.get("com.docker.compose.config-hash", "")),
        "created_at": record.get("Created"),
        "started_at": state.get("StartedAt"),
        "main_pid": state.get("Pid"),
        "network_id": networks["nexpoly_default"].get("NetworkID"),
        "volume_name": mounts[0]["Name"],
    }


def _database_projection_query() -> str:
    schemas = ",".join("'" + value + "'" for value in GOVERNED_SCHEMAS)
    return f"""
    WITH target AS (
      SELECT * FROM pg_catalog.pg_roles WHERE rolname='{AUDIT_USER}'
    ), governed(schema_name) AS (
      SELECT unnest(ARRAY[{schemas}]::text[])
    ), present_schemas AS (
      SELECT namespace.oid, namespace.nspname
      FROM pg_catalog.pg_namespace AS namespace
      JOIN governed ON governed.schema_name=namespace.nspname
    )
    SELECT jsonb_build_object(
      'system_identifier', (SELECT system_identifier::text FROM pg_catalog.pg_control_system()),
      'database_oid', (SELECT oid::text FROM pg_catalog.pg_database WHERE datname=current_database()),
      'database_owner', (SELECT pg_catalog.pg_get_userbyid(datdba) FROM pg_catalog.pg_database WHERE datname=current_database()),
      'session_user', session_user,
      'session_superuser', (SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname=session_user),
      'present_governed_schemas', (SELECT coalesce(jsonb_agg(nspname ORDER BY nspname),'[]'::jsonb) FROM present_schemas),
      'role', (SELECT jsonb_build_object(
        'can_login',roles.rolcanlogin,'superuser',roles.rolsuper,'create_db',roles.rolcreatedb,
        'create_role',roles.rolcreaterole,'inherit',roles.rolinherit,'replication',roles.rolreplication,
        'bypass_rls',roles.rolbypassrls,'settings',coalesce(to_jsonb(roles.rolconfig),'[]'::jsonb),
        'password_kind', CASE WHEN auth.rolpassword LIKE 'SCRAM-SHA-256$%' THEN 'scram-sha-256' WHEN auth.rolpassword IS NULL THEN 'none' ELSE 'other' END
      ) FROM target roles JOIN pg_catalog.pg_authid auth ON auth.oid=roles.oid),
      'database_privileges', jsonb_build_object(
        'connect',CASE WHEN EXISTS(SELECT 1 FROM target) THEN pg_catalog.has_database_privilege('{AUDIT_USER}',current_database(),'CONNECT') ELSE false END,
        'create',CASE WHEN EXISTS(SELECT 1 FROM target) THEN pg_catalog.has_database_privilege('{AUDIT_USER}',current_database(),'CREATE') ELSE false END,
        'temporary',CASE WHEN EXISTS(SELECT 1 FROM target) THEN pg_catalog.has_database_privilege('{AUDIT_USER}',current_database(),'TEMPORARY') ELSE pg_catalog.has_database_privilege('public',current_database(),'TEMPORARY') END
      ),
      'memberships', (SELECT coalesce(jsonb_agg(jsonb_build_object(
        'role',parent.rolname,'admin',member.admin_option,'inherit',member.inherit_option,'set',member.set_option
      ) ORDER BY parent.rolname),'[]'::jsonb)
      FROM target JOIN pg_catalog.pg_auth_members member ON member.member=target.oid
      JOIN pg_catalog.pg_roles parent ON parent.oid=member.roleid),
      'database_role_settings', (SELECT coalesce(jsonb_agg(jsonb_build_object(
        'database',coalesce(database.datname,'*'),'settings',to_jsonb(setting.setconfig)
      ) ORDER BY setting.setdatabase),'[]'::jsonb)
      FROM target JOIN pg_catalog.pg_db_role_setting setting ON setting.setrole=target.oid
      LEFT JOIN pg_catalog.pg_database database ON database.oid=setting.setdatabase),
      'schemas', (SELECT coalesce(jsonb_agg(jsonb_build_object(
        'schema',namespace.nspname,'oid',namespace.oid::text,
        'usage',CASE WHEN EXISTS(SELECT 1 FROM target) THEN pg_catalog.has_schema_privilege('{AUDIT_USER}',namespace.oid,'USAGE') ELSE false END,
        'create',CASE WHEN EXISTS(SELECT 1 FROM target) THEN pg_catalog.has_schema_privilege('{AUDIT_USER}',namespace.oid,'CREATE') ELSE false END
      ) ORDER BY namespace.nspname),'[]'::jsonb) FROM present_schemas namespace),
      'relations', (SELECT coalesce(jsonb_agg(jsonb_build_object(
        'relation',namespace.nspname||'.'||relation.relname,'oid',relation.oid::text,'kind',relation.relkind,'owner',pg_catalog.pg_get_userbyid(relation.relowner),
        'select',CASE WHEN EXISTS(SELECT 1 FROM target) THEN pg_catalog.has_table_privilege('{AUDIT_USER}',relation.oid,'SELECT') ELSE false END,
        'insert',CASE WHEN EXISTS(SELECT 1 FROM target) THEN pg_catalog.has_table_privilege('{AUDIT_USER}',relation.oid,'INSERT') ELSE false END,
        'update',CASE WHEN EXISTS(SELECT 1 FROM target) THEN pg_catalog.has_table_privilege('{AUDIT_USER}',relation.oid,'UPDATE') ELSE false END,
        'delete',CASE WHEN EXISTS(SELECT 1 FROM target) THEN pg_catalog.has_table_privilege('{AUDIT_USER}',relation.oid,'DELETE') ELSE false END,
        'truncate',CASE WHEN EXISTS(SELECT 1 FROM target) THEN pg_catalog.has_table_privilege('{AUDIT_USER}',relation.oid,'TRUNCATE') ELSE false END,
        'references',CASE WHEN EXISTS(SELECT 1 FROM target) THEN pg_catalog.has_table_privilege('{AUDIT_USER}',relation.oid,'REFERENCES') ELSE false END,
        'trigger',CASE WHEN EXISTS(SELECT 1 FROM target) THEN pg_catalog.has_table_privilege('{AUDIT_USER}',relation.oid,'TRIGGER') ELSE false END
      ) ORDER BY namespace.nspname,relation.relname),'[]'::jsonb)
      FROM pg_catalog.pg_class relation JOIN present_schemas namespace ON namespace.oid=relation.relnamespace
      WHERE relation.relkind IN ('r','p','v','m','f')),
      'sequences', (SELECT coalesce(jsonb_agg(jsonb_build_object(
        'sequence',namespace.nspname||'.'||relation.relname,'oid',relation.oid::text,'owner',pg_catalog.pg_get_userbyid(relation.relowner),
        'select',CASE WHEN EXISTS(SELECT 1 FROM target) THEN pg_catalog.has_sequence_privilege('{AUDIT_USER}',relation.oid,'SELECT') ELSE false END,
        'usage',CASE WHEN EXISTS(SELECT 1 FROM target) THEN pg_catalog.has_sequence_privilege('{AUDIT_USER}',relation.oid,'USAGE') ELSE false END,
        'update',CASE WHEN EXISTS(SELECT 1 FROM target) THEN pg_catalog.has_sequence_privilege('{AUDIT_USER}',relation.oid,'UPDATE') ELSE false END
      ) ORDER BY namespace.nspname,relation.relname),'[]'::jsonb)
      FROM pg_catalog.pg_class relation JOIN present_schemas namespace ON namespace.oid=relation.relnamespace WHERE relation.relkind='S'),
      'column_write_grants', (SELECT coalesce(jsonb_agg(namespace.nspname||'.'||relation.relname||'.'||attribute.attname ORDER BY 1),'[]'::jsonb)
      FROM target CROSS JOIN pg_catalog.pg_attribute attribute
      JOIN pg_catalog.pg_class relation ON relation.oid=attribute.attrelid
      JOIN present_schemas namespace ON namespace.oid=relation.relnamespace
      WHERE attribute.attnum>0 AND NOT attribute.attisdropped AND pg_catalog.has_column_privilege('{AUDIT_USER}',relation.oid,attribute.attnum,'INSERT,UPDATE,REFERENCES')),
      'outside_governed_privileges', (SELECT coalesce(jsonb_agg(identity ORDER BY identity),'[]'::jsonb) FROM (
        SELECT 'schema:'||namespace.nspname||':CREATE' identity
        FROM pg_catalog.pg_namespace namespace
        WHERE namespace.nspname !~ '^pg_' AND namespace.nspname<>'information_schema'
          AND namespace.nspname <> ALL(ARRAY[{schemas}]::text[])
          AND CASE WHEN EXISTS(SELECT 1 FROM target) THEN pg_catalog.has_schema_privilege('{AUDIT_USER}',namespace.oid,'CREATE') ELSE pg_catalog.has_schema_privilege('public',namespace.oid,'CREATE') END
        UNION ALL
        SELECT 'relation:'||namespace.nspname||'.'||relation.relname
        FROM pg_catalog.pg_class relation JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace
        WHERE namespace.nspname !~ '^pg_' AND namespace.nspname<>'information_schema'
          AND namespace.nspname <> ALL(ARRAY[{schemas}]::text[]) AND relation.relkind IN ('r','p','v','m','f')
          AND CASE WHEN EXISTS(SELECT 1 FROM target) THEN pg_catalog.has_table_privilege('{AUDIT_USER}',relation.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') ELSE pg_catalog.has_table_privilege('public',relation.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') END
        UNION ALL
        SELECT 'sequence:'||namespace.nspname||'.'||relation.relname
        FROM pg_catalog.pg_class relation JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace
        WHERE namespace.nspname !~ '^pg_' AND namespace.nspname<>'information_schema'
          AND namespace.nspname <> ALL(ARRAY[{schemas}]::text[])
          AND CASE WHEN relation.relkind='S' THEN CASE WHEN EXISTS(SELECT 1 FROM target) THEN pg_catalog.has_sequence_privilege('{AUDIT_USER}',relation.oid,'SELECT,USAGE,UPDATE') ELSE pg_catalog.has_sequence_privilege('public',relation.oid,'SELECT,USAGE,UPDATE') END ELSE false END
        UNION ALL
        SELECT 'column:'||namespace.nspname||'.'||relation.relname||'.'||attribute.attname
        FROM pg_catalog.pg_attribute attribute JOIN pg_catalog.pg_class relation ON relation.oid=attribute.attrelid
        JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace
        WHERE namespace.nspname !~ '^pg_' AND namespace.nspname<>'information_schema'
          AND namespace.nspname <> ALL(ARRAY[{schemas}]::text[]) AND attribute.attnum>0 AND NOT attribute.attisdropped
          AND CASE WHEN EXISTS(SELECT 1 FROM target) THEN pg_catalog.has_column_privilege('{AUDIT_USER}',relation.oid,attribute.attnum,'SELECT,INSERT,UPDATE,REFERENCES') ELSE pg_catalog.has_column_privilege('public',relation.oid,attribute.attnum,'SELECT,INSERT,UPDATE,REFERENCES') END
      ) outside_authority),
      'default_privileges', (SELECT coalesce(jsonb_agg(jsonb_build_object(
        'owner',owner.rolname,'schema',coalesce(namespace.nspname,'*'),'object_type',defaults.defaclobjtype,
        'privilege',acl.privilege_type,'grantable',acl.is_grantable
      ) ORDER BY owner.rolname,coalesce(namespace.nspname,'*'),defaults.defaclobjtype,acl.privilege_type),'[]'::jsonb)
      FROM target CROSS JOIN pg_catalog.pg_default_acl defaults
      JOIN pg_catalog.pg_roles owner ON owner.oid=defaults.defaclrole
      LEFT JOIN pg_catalog.pg_namespace namespace ON namespace.oid=defaults.defaclnamespace
      CROSS JOIN LATERAL pg_catalog.aclexplode(defaults.defaclacl) acl
      WHERE acl.grantee=target.oid),
      'owned_objects', (SELECT coalesce(jsonb_agg(identity ORDER BY identity),'[]'::jsonb) FROM target CROSS JOIN LATERAL (
        SELECT 'database:'||datname identity FROM pg_catalog.pg_database WHERE datdba=target.oid
        UNION ALL SELECT 'schema:'||nspname FROM pg_catalog.pg_namespace WHERE nspowner=target.oid
        UNION ALL SELECT 'relation:'||oid::regclass::text FROM pg_catalog.pg_class WHERE relowner=target.oid
        UNION ALL SELECT 'routine:'||oid::regprocedure::text FROM pg_catalog.pg_proc WHERE proowner=target.oid
        UNION ALL SELECT 'type:'||oid::regtype::text FROM pg_catalog.pg_type WHERE typowner=target.oid
        UNION ALL SELECT 'large-object:'||oid::text FROM pg_catalog.pg_largeobject_metadata WHERE lomowner=target.oid
        UNION ALL SELECT 'foreign-data-wrapper:'||fdwname FROM pg_catalog.pg_foreign_data_wrapper WHERE fdwowner=target.oid
        UNION ALL SELECT 'foreign-server:'||srvname FROM pg_catalog.pg_foreign_server WHERE srvowner=target.oid
        UNION ALL SELECT 'tablespace:'||spcname FROM pg_catalog.pg_tablespace WHERE spcowner=target.oid
        UNION ALL SELECT 'extension:'||extname FROM pg_catalog.pg_extension WHERE extowner=target.oid
      ) owned),
      'security_definer_execute', (SELECT coalesce(jsonb_agg(routine.oid::regprocedure::text ORDER BY routine.oid::regprocedure::text),'[]'::jsonb)
      FROM target CROSS JOIN pg_catalog.pg_proc routine JOIN pg_catalog.pg_namespace namespace ON namespace.oid=routine.pronamespace
      WHERE namespace.nspname !~ '^pg_' AND namespace.nspname<>'information_schema'
        AND routine.prosecdef AND pg_catalog.has_function_privilege('{AUDIT_USER}',routine.oid,'EXECUTE')),
      'large_object_update_count', (SELECT count(DISTINCT object.oid)
      FROM target CROSS JOIN pg_catalog.pg_largeobject_metadata object
      CROSS JOIN LATERAL pg_catalog.aclexplode(coalesce(object.lomacl,pg_catalog.acldefault('L',object.lomowner))) acl
      WHERE acl.privilege_type='UPDATE' AND acl.grantee IN (0,target.oid)),
      'large_object_mutators', (SELECT jsonb_agg(jsonb_build_object(
        'routine',routine.oid::regprocedure::text,'oid',routine.oid::text,'owner',pg_catalog.pg_get_userbyid(routine.proowner),
        'other_acl',(SELECT coalesce(jsonb_agg(jsonb_build_object(
          'grantee',grantee.rolname,'grantor',grantor.rolname,'privilege',acl.privilege_type,'grantable',acl.is_grantable
        ) ORDER BY grantee.rolname,grantor.rolname,acl.privilege_type),'[]'::jsonb)
        FROM pg_catalog.aclexplode(coalesce(routine.proacl,pg_catalog.acldefault('f',routine.proowner))) acl
        JOIN pg_catalog.pg_roles grantee ON grantee.oid=acl.grantee
        JOIN pg_catalog.pg_roles grantor ON grantor.oid=acl.grantor
        WHERE grantee.rolname NOT IN ('{AUDIT_USER}','pg_database_owner')),
        'public_execute',pg_catalog.has_function_privilege('public',routine.oid,'EXECUTE'),
        'database_owner_execute',pg_catalog.has_function_privilege('pg_database_owner',routine.oid,'EXECUTE'),
        'audit_execute',CASE WHEN EXISTS(SELECT 1 FROM target) THEN pg_catalog.has_function_privilege('{AUDIT_USER}',routine.oid,'EXECUTE') ELSE false END
      ) ORDER BY routine.oid::regprocedure::text) FROM pg_catalog.pg_proc routine WHERE routine.oid IN (
        'pg_catalog.lo_creat(integer)'::regprocedure,'pg_catalog.lo_create(oid)'::regprocedure,
        'pg_catalog.lo_from_bytea(oid,bytea)'::regprocedure,'pg_catalog.lo_put(oid,bigint,bytea)'::regprocedure,
        'pg_catalog.lo_unlink(oid)'::regprocedure,'pg_catalog.lowrite(integer,bytea)'::regprocedure,
        'pg_catalog.lo_truncate(integer,integer)'::regprocedure,'pg_catalog.lo_truncate64(integer,bigint)'::regprocedure
      ))
    )
    """


def _admin_json(container_id: str) -> dict[str, Any]:
    sql = (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
        + _database_projection_query()
        + ";\nCOMMIT;\n"
    )
    completed = _run(
        [
            "docker", "exec", "-i", container_id, "psql", "-X", "--quiet",
            "--set", "ON_ERROR_STOP=1", "--tuples-only", "--no-align",
            "--username", ADMIN_USER, "--dbname", DATABASE, "--file=-",
        ],
        input_bytes=sql.encode("utf-8"),
    )
    try:
        value = json.loads(completed.stdout.decode().strip())
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RoleProvisionError("mutable role database evidence is invalid") from exc
    _require(
        isinstance(value, dict)
        and SYSTEM_ID_RE.fullmatch(str(value.get("system_identifier", ""))) is not None
        and str(value.get("database_oid", "")).isdigit()
        and value.get("database_owner") == ADMIN_USER
        and value.get("session_user") == ADMIN_USER
        and value.get("session_superuser") is True
        and isinstance(value.get("present_governed_schemas"), list)
        and set(value["present_governed_schemas"]).issubset(GOVERNED_SCHEMAS)
        and set(value["present_governed_schemas"]) >= (set(GOVERNED_SCHEMAS) - {"generation"}),
        "mutable role administrator/database identity differs",
    )
    return value


def _login_matches() -> bool:
    sql = (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY; "
        "SELECT current_user||':'||current_database()||':'||current_setting('transaction_read_only'); ROLLBACK;"
    )
    completed = _run(
        [
            "psql", "-X", "--quiet", "--no-password", "--set", "ON_ERROR_STOP=1",
            "--tuples-only", "--no-align", "--host", HOST, "--port", PORT,
            "--username", AUDIT_USER, "--dbname", DATABASE, "--command", sql,
        ],
        extra_environment={"PGPASSFILE": str(PGPASS_PATH)},
        check=False,
        timeout=30,
    )
    expected = f"{AUDIT_USER}:{DATABASE}:on".encode()
    return completed.returncode == 0 and completed.stdout.strip() == expected


def _observe_state(container_id: str) -> dict[str, Any]:
    return {"database": _admin_json(container_id), "pgpass_login_matches": _login_matches()}


def _desired_state(before: Mapping[str, Any]) -> dict[str, Any]:
    desired = copy.deepcopy(dict(before))
    database = desired["database"]
    database["role"] = {
        "can_login": True,
        "superuser": False,
        "create_db": False,
        "create_role": False,
        "inherit": True,
        "replication": False,
        "bypass_rls": False,
        "settings": ["default_transaction_read_only=on"],
        "password_kind": "scram-sha-256",
    }
    database["database_privileges"] = {
        "connect": True,
        "create": False,
        # PostgreSQL normally grants TEMPORARY through the database PUBLIC ACL.
        # It is intentionally preserved: read-only transactions may use
        # session-local temporary objects but cannot persist application data.
        "temporary": bool(database["database_privileges"]["temporary"]),
    }
    database["memberships"] = []
    database["database_role_settings"] = [
        {"database": "*", "settings": ["default_transaction_read_only=on"]}
    ]
    for record in database["schemas"]:
        record["usage"] = True
        record["create"] = False
    for record in database["relations"]:
        record.update({
            "select": True, "insert": False, "update": False, "delete": False,
            "truncate": False, "references": False, "trigger": False,
        })
    for record in database["sequences"]:
        record.update({"select": True, "usage": False, "update": False})
    database["column_write_grants"] = []
    database["outside_governed_privileges"] = []
    database["default_privileges"] = sorted(
        [
            {
                "owner": ADMIN_USER,
                "schema": schema,
                "object_type": object_type,
                "privilege": "SELECT",
                "grantable": False,
            }
            for schema in database["present_governed_schemas"]
            for object_type in ("S", "r")
        ],
        key=lambda record: (record["owner"], record["schema"], record["object_type"], record["privilege"]),
    )
    database["owned_objects"] = []
    database["security_definer_execute"] = []
    database["large_object_update_count"] = 0
    for record in database["large_object_mutators"]:
        record["public_execute"] = False
        record["database_owner_execute"] = True
        record["audit_execute"] = False
    desired["pgpass_login_matches"] = True
    return desired


def _normalized_state(value: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(value))


def _state_is_desired(value: Mapping[str, Any], desired: Mapping[str, Any]) -> bool:
    return _normalized_state(value) == _normalized_state(desired)


def build_plan(source_sha: str, operation_id: str) -> dict[str, Any]:
    _require(OPERATION_RE.fullmatch(operation_id) is not None, "role operation ID is invalid")
    _private_directory(RUNTIME_ROOT)
    _private_directory(RUNTIME_ROOT / "state")
    _private_directory(RUNTIME_ROOT / "config")
    source, role_sql = _source_authority(source_sha)
    adoption = _strict_adopted_authority(source_sha)
    password, pgpass_sha256 = _pgpass_authority(PGPASS_PATH)
    password[:] = b"\x00" * len(password)
    postgres = _live_postgres()
    before = _observe_state(postgres["container_id"])
    desired = _desired_state(before)
    lo_impact = {
        "scope": "PUBLIC execute on eight pg_catalog large-object mutators in nexpoly",
        "before": before["database"]["large_object_mutators"],
        "desired": desired["database"]["large_object_mutators"],
    }
    plan = {
        "schema_version": 2,
        "action": "provision-mutable-data-audit-role",
        "apply": False,
        "operation_id": operation_id,
        "source": source,
        "role_sql": {"path": ROLE_SQL_PATH, "sha256": _digest_bytes(role_sql)},
        "pgpass": {"path": str(PGPASS_PATH), "sha256": pgpass_sha256},
        "adoption": adoption,
        "postgres": postgres,
        "before": before,
        "before_sha256": _digest(before),
        "desired": desired,
        "desired_sha256": _digest(desired),
        "already_exact": _state_is_desired(before, desired),
        "public_lo_acl_impact": lo_impact,
        "public_lo_acl_impact_sha256": _digest(lo_impact),
        "mutations": [
            "normalize only nexpoly_mutable_audit and remove all role memberships",
            "grant CONNECT plus explicit SELECT/USAGE only inside present governed nexpoly schemas",
            "install polyprop/schema-scoped future SELECT default ACLs",
            "revoke PUBLIC execute from eight large-object mutators after explicit impact confirmation",
            "set one locally generated SCRAM verifier in the same role transaction",
        ],
    }
    plan["plan_sha256"] = _digest(plan)
    return plan


def _jsonb_literal(value: object) -> str:
    return "'" + _canonical_bytes(value).decode("ascii").replace("'", "''") + "'::jsonb"


def _transaction_projection_sql(*, before: object, desired: object) -> str:
    projection = _database_projection_query().strip().rstrip(";")
    return f"""
LOCK TABLE
    pg_catalog.pg_authid,
    pg_catalog.pg_auth_members,
    pg_catalog.pg_db_role_setting,
    pg_catalog.pg_database,
    pg_catalog.pg_namespace,
    pg_catalog.pg_class,
    pg_catalog.pg_attribute,
    pg_catalog.pg_default_acl,
    pg_catalog.pg_proc,
    pg_catalog.pg_largeobject_metadata,
    pg_catalog.pg_shdepend,
    pg_catalog.pg_type,
    pg_catalog.pg_foreign_data_wrapper,
    pg_catalog.pg_foreign_server,
    pg_catalog.pg_tablespace,
    pg_catalog.pg_extension
IN SHARE MODE;
SELECT pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended('nexpoly:mutable-data-audit-role:v2', 0)
);
DO $sealed_before_cas$
DECLARE
    live_projection jsonb;
BEGIN
    SELECT projection.value
    INTO STRICT live_projection
    FROM (
        {projection}
    ) AS projection(value);
    IF live_projection IS DISTINCT FROM {_jsonb_literal(before)}
       AND live_projection IS DISTINCT FROM {_jsonb_literal(desired)}
    THEN
        RAISE EXCEPTION 'sealed mutable-audit before/desired CAS differs'
            USING ERRCODE = '40001';
    END IF;
END
$sealed_before_cas$;
""".strip()


def _transaction_desired_assert_sql(*, desired: object) -> str:
    projection = _database_projection_query().strip().rstrip(";")
    return f"""
DO $sealed_desired_assert$
DECLARE
    live_projection jsonb;
BEGIN
    SELECT projection.value
    INTO STRICT live_projection
    FROM (
        {projection}
    ) AS projection(value);
    IF live_projection IS DISTINCT FROM {_jsonb_literal(desired)}
    THEN
        RAISE EXCEPTION 'sealed mutable-audit desired state differs before commit'
            USING ERRCODE = '40001';
    END IF;
END
$sealed_desired_assert$;
""".strip()


def _transaction_sql(
    role_sql: bytes,
    verifier: str,
    *,
    before_database: Mapping[str, Any],
    desired_database: Mapping[str, Any],
) -> bytearray:
    _require(SCRAM_RE.fullmatch(verifier) is not None, "SCRAM verifier is invalid")
    literal = verifier.replace("'", "''").encode("ascii")
    placeholder = b"__SCRAM_VERIFIER_LITERAL__"
    cas_placeholder = b"__IN_TRANSACTION_SEALED_CAS__"
    desired_placeholder = b"__IN_TRANSACTION_DESIRED_ASSERT__"
    _require(
        role_sql.count(placeholder) == 1
        and role_sql.count(cas_placeholder) == 1
        and role_sql.count(desired_placeholder) == 1,
        "role SQL transaction placeholder differs",
    )
    payload = role_sql.replace(placeholder, b"'" + literal + b"'")
    payload = payload.replace(
        cas_placeholder,
        _transaction_projection_sql(
            before=before_database, desired=desired_database
        ).encode("utf-8"),
    )
    payload = payload.replace(
        desired_placeholder,
        _transaction_desired_assert_sql(desired=desired_database).encode("utf-8"),
    )
    return bytearray(payload)


def _apply_transaction(container_id: str, payload: bytearray) -> None:
    try:
        _run(
            [
                "docker", "exec", "-i", container_id, "psql", "-X", "--quiet",
                "--set", "ON_ERROR_STOP=1", "--username", ADMIN_USER,
                "--dbname", DATABASE, "--file=-",
            ],
            input_bytes=memoryview(payload),
        )
    finally:
        payload[:] = b"\x00" * len(payload)


def _journal_document(
    *, operation_id: str, plan: Mapping[str, Any], phase: str, previous: Mapping[str, Any] | None
) -> dict[str, Any]:
    _require(phase in JOURNAL_PHASES, "role journal phase is invalid")
    history = [] if previous is None else list(previous["history"])
    history.append({"phase": phase, "recorded_at": _utc_now()})
    return {
        "schema_version": 2,
        "operation_id": operation_id,
        "source_sha": plan["source"]["sha"],
        "plan_sha256": plan["plan_sha256"],
        "phase": phase,
        "history": history,
        "plan": dict(plan),
    }


def _validate_journal(value: Mapping[str, Any], operation_id: str) -> dict[str, Any]:
    _require(
        set(value)
        == {
            "schema_version",
            "operation_id",
            "source_sha",
            "plan_sha256",
            "phase",
            "history",
            "plan",
        }
        and value.get("schema_version") == 2
        and value.get("operation_id") == operation_id
        and value.get("phase") in JOURNAL_PHASES
        and isinstance(value.get("plan"), dict)
        and value.get("plan_sha256") == value["plan"].get("plan_sha256")
        and _digest({key: item for key, item in value["plan"].items() if key != "plan_sha256"})
        == value["plan_sha256"]
        and value.get("source_sha") == (value["plan"].get("source") or {}).get("sha")
        and SHA_RE.fullmatch(str(value.get("source_sha", ""))) is not None
        and DIGEST_RE.fullmatch(str(value.get("plan_sha256", ""))) is not None
        and isinstance(value.get("history"), list)
        and value["history"]
        and value["history"][-1].get("phase") == value["phase"],
        "mutable role journal is invalid",
    )
    _require(
        all(
            isinstance(record, dict)
            and set(record) == {"phase", "recorded_at"}
            and isinstance(record.get("recorded_at"), str)
            and record["recorded_at"].endswith("Z")
            for record in value["history"]
        ),
        "mutable role journal history record is invalid",
    )
    try:
        for record in value["history"]:
            parsed = dt.datetime.fromisoformat(record["recorded_at"].replace("Z", "+00:00"))
            _require(parsed.tzinfo is not None, "mutable role journal timestamp lacks UTC")
    except ValueError as exc:
        raise RoleProvisionError("mutable role journal timestamp is invalid") from exc
    phases = [record["phase"] for record in value["history"]]
    _require(
        all(phase in JOURNAL_PHASES for phase in phases)
        and phases == list(JOURNAL_PHASES[: len(phases)]),
        "mutable role journal history is invalid",
    )
    return dict(value)


def _advance_journal(path: Path, current: dict[str, Any] | None, phase: str, plan: dict[str, Any]) -> dict[str, Any]:
    if current is not None:
        live, _payload = _load_private_json(path)
        _require(live == current, "mutable role journal changed before advance")
        expected_index = JOURNAL_PHASES.index(current["phase"]) + 1
        _require(expected_index < len(JOURNAL_PHASES) and JOURNAL_PHASES[expected_index] == phase, "mutable role journal transition is invalid")
    else:
        _require(phase == "intent" and not path.exists() and not path.is_symlink(), "mutable role journal intent destination exists")
    updated = _journal_document(operation_id=plan["operation_id"], plan=plan, phase=phase, previous=current)
    _atomic_private_json(path, updated)
    loaded, _payload = _load_private_json(path)
    _require(loaded == updated, "mutable role journal write was not durable")
    return updated


def _recover_journal_staging(path: Path, operation_id: str) -> None:
    staging = path.parent / f".{path.name}.next"
    if not (staging.exists() or staging.is_symlink()):
        return
    candidate_raw, _payload = _load_private_json(staging)
    candidate = _validate_journal(candidate_raw, operation_id)
    if path.exists() or path.is_symlink():
        current_raw, _payload = _load_private_json(path)
        current = _validate_journal(current_raw, operation_id)
        _require(
            candidate["plan"] == current["plan"]
            and candidate["history"][:-1] == current["history"]
            and JOURNAL_PHASES.index(candidate["phase"])
            == JOURNAL_PHASES.index(current["phase"]) + 1,
            "staged mutable role journal is not the exact next state",
        )
    else:
        _require(
            candidate["phase"] == "intent" and len(candidate["history"]) == 1,
            "staged mutable role journal lacks its predecessor",
        )
    os.replace(staging, path)
    _fsync_directory(path.parent)


def _validate_completed(value: Mapping[str, Any], *, source_sha: str, operation_id: str, plan_sha256: str) -> dict[str, Any]:
    report = value.get("report")
    _require(
        set(value)
        == {
            "schema_version",
            "status",
            "operation_id",
            "source_sha",
            "plan_sha256",
            "report",
            "report_sha256",
        }
        and value.get("schema_version") == 2
        and value.get("status") == "completed"
        and value.get("operation_id") == operation_id
        and value.get("source_sha") == source_sha
        and value.get("plan_sha256") == plan_sha256
        and isinstance(report, dict)
        and value.get("report_sha256") == _digest(report)
        and "password" not in json.dumps(value, sort_keys=True).lower()
        and "SCRAM-SHA-256$" not in json.dumps(value, sort_keys=True),
        "completed mutable role authority differs",
    )
    return dict(value)


def _promote_completed_staging(
    path: Path, *, source_sha: str, operation_id: str, plan_sha256: str
) -> None:
    staging = path.parent / f".{path.name}.next"
    if not (staging.exists() or staging.is_symlink()):
        return
    if path.exists() or path.is_symlink():
        staged_metadata = staging.lstat()
        final_metadata = path.lstat()
        _require(
            stat.S_ISREG(staged_metadata.st_mode)
            and stat.S_ISREG(final_metadata.st_mode)
            and not staging.is_symlink()
            and not path.is_symlink()
            and staged_metadata.st_uid == os.geteuid()
            and final_metadata.st_uid == os.geteuid()
            and stat.S_IMODE(staged_metadata.st_mode) == 0o600
            and stat.S_IMODE(final_metadata.st_mode) == 0o600
            and (staged_metadata.st_dev, staged_metadata.st_ino)
            == (final_metadata.st_dev, final_metadata.st_ino)
            and staged_metadata.st_nlink == final_metadata.st_nlink == 2,
            "completed role authority staging identity differs",
        )
        payload = _read_private(
            path, maximum_bytes=16 * 1024 * 1024, allowed_nlinks=frozenset({2})
        )
        try:
            candidate = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RoleProvisionError("completed role staging JSON is invalid") from exc
        _validate_completed(
            candidate,
            source_sha=source_sha,
            operation_id=operation_id,
            plan_sha256=plan_sha256,
        )
        staging.unlink()
        _fsync_directory(path.parent)
        return
    candidate, _payload = _load_private_json(staging)
    _validate_completed(
        candidate,
        source_sha=source_sha,
        operation_id=operation_id,
        plan_sha256=plan_sha256,
    )
    try:
        os.link(staging, path, follow_symlinks=False)
    except OSError as exc:
        raise RoleProvisionError("cannot commit completed role authority") from exc
    _fsync_directory(path.parent)
    staging.unlink()
    _fsync_directory(path.parent)


def _write_completed_atomic(path: Path, value: Mapping[str, Any]) -> None:
    staging = path.parent / f".{path.name}.next"
    _require(
        not path.exists()
        and not path.is_symlink()
        and not staging.exists()
        and not staging.is_symlink(),
        "completed role authority destination exists",
    )
    _write_once(staging, value)
    _promote_completed_staging(
        path,
        source_sha=str(value["source_sha"]),
        operation_id=str(value["operation_id"]),
        plan_sha256=str(value["plan_sha256"]),
    )


def apply_plan(
    source_sha: str,
    operation_id: str,
    confirm_plan_sha256: str,
    confirm_public_lo_acl_sha256: str,
) -> dict[str, Any]:
    _require(DIGEST_RE.fullmatch(confirm_plan_sha256) is not None, "plan confirmation is invalid")
    _require(DIGEST_RE.fullmatch(confirm_public_lo_acl_sha256) is not None, "PUBLIC LO ACL confirmation is invalid")
    audit_parent = RUNTIME_ROOT / "audit"
    _private_directory(audit_parent)
    root = audit_parent / "mutable-data-role"
    _ensure_private_directory(root)
    operation = root / operation_id
    _ensure_private_directory(operation)
    journal_path = operation / "journal.json"
    completed_path = operation / "completed.json"
    entries = {entry.name for entry in operation.iterdir()}
    _require(
        entries
        <= {
            "journal.json",
            ".journal.json.next",
            "completed.json",
            ".completed.json.next",
        },
        "mutable role operation directory contains unknown entries",
    )
    _recover_journal_staging(journal_path, operation_id)
    if journal_path.exists() or journal_path.is_symlink():
        journal_raw, _payload = _load_private_json(journal_path)
        journal = _validate_journal(journal_raw, operation_id)
        plan = journal["plan"]
    else:
        _require(not completed_path.exists() and not completed_path.is_symlink(), "completed role authority lacks its journal")
        plan = build_plan(source_sha, operation_id)
        _require(plan["plan_sha256"] == confirm_plan_sha256, "role plan changed before apply")
        _require(plan["public_lo_acl_impact_sha256"] == confirm_public_lo_acl_sha256, "PUBLIC LO ACL impact confirmation differs")
        journal = _advance_journal(journal_path, None, "intent", plan)
    _require(
        plan.get("plan_sha256") == confirm_plan_sha256
        and plan.get("public_lo_acl_impact_sha256") == confirm_public_lo_acl_sha256
        and (plan.get("source") or {}).get("sha") == source_sha,
        "sealed mutable role plan differs",
    )
    _promote_completed_staging(
        completed_path,
        source_sha=source_sha,
        operation_id=operation_id,
        plan_sha256=confirm_plan_sha256,
    )
    if completed_path.exists() or completed_path.is_symlink():
        completed_raw, _payload = _load_private_json(completed_path)
        completed = _validate_completed(
            completed_raw, source_sha=source_sha, operation_id=operation_id, plan_sha256=confirm_plan_sha256
        )
        if journal["phase"] == "verified":
            journal = _advance_journal(journal_path, journal, "completed", plan)
        _require(journal["phase"] == "completed", "completed role authority has incomplete journal")
        source, role_sql = _source_authority(source_sha)
        adoption = _strict_adopted_authority(source_sha)
        password, pgpass_sha256 = _pgpass_authority(PGPASS_PATH)
        password[:] = b"\x00" * len(password)
        postgres = _live_postgres()
        observed = _observe_state(postgres["container_id"])
        _require(
            source == plan["source"]
            and adoption == plan["adoption"]
            and _digest_bytes(role_sql) == plan["role_sql"]["sha256"]
            and pgpass_sha256 == plan["pgpass"]["sha256"]
            and postgres == plan["postgres"]
            and _state_is_desired(observed, plan["desired"]),
            "completed mutable role live authority differs",
        )
        return completed
    source, role_sql = _source_authority(source_sha)
    adoption = _strict_adopted_authority(source_sha)
    password, pgpass_sha256 = _pgpass_authority(PGPASS_PATH)
    recovered_commit_response = False
    try:
        postgres = _live_postgres()
        _require(
            source == plan["source"]
            and adoption == plan["adoption"]
            and _digest_bytes(role_sql) == plan["role_sql"]["sha256"]
            and pgpass_sha256 == plan["pgpass"]["sha256"]
            and postgres == plan["postgres"],
            "mutable role authority changed after intent",
        )
        observed = _observe_state(postgres["container_id"])
        matches_before = observed == plan["before"]
        matches_desired = _state_is_desired(observed, plan["desired"])
        _require(matches_before or matches_desired, "mutable role state is neither sealed before nor desired")
        if journal["phase"] == "intent":
            journal = _advance_journal(journal_path, journal, "database-commit-intent", plan)
        if journal["phase"] == "database-commit-intent":
            if matches_desired:
                # The only durable phase preceding a PostgreSQL COMMIT is
                # database-commit-intent.  Desired state plus a successful
                # pgpass login therefore proves a lost COMMIT response.
                recovered_commit_response = not bool(plan.get("already_exact"))
            else:
                _require(matches_before, "mutable role before-state CAS differs")
                verifier = _scram_verifier(password)
                payload = _transaction_sql(
                    role_sql,
                    verifier,
                    before_database=plan["before"]["database"],
                    desired_database=plan["desired"]["database"],
                )
                verifier = ""
                _apply_transaction(postgres["container_id"], payload)
                observed = _observe_state(postgres["container_id"])
                _require(_state_is_desired(observed, plan["desired"]), "mutable role transaction did not reach desired state")
            journal = _advance_journal(journal_path, journal, "database-committed", plan)
        _require(journal["phase"] in {"database-committed", "verified"}, "mutable role journal cannot resume")
        final_postgres = _live_postgres()
        final_state = _observe_state(postgres["container_id"])
        _require(
            final_postgres == plan["postgres"]
            and _state_is_desired(final_state, plan["desired"]),
            "mutable role final identity or contract differs",
        )
        if journal["phase"] == "database-committed":
            journal = _advance_journal(journal_path, journal, "verified", plan)
        report = {
            "before_sha256": plan["before_sha256"],
            "desired_sha256": plan["desired_sha256"],
            "observed_after_sha256": _digest(final_state),
            "pgpass_sha256": plan["pgpass"]["sha256"],
            "public_lo_acl_impact_sha256": plan["public_lo_acl_impact_sha256"],
            "postgres": plan["postgres"],
            "source": plan["source"],
            "adoption": plan["adoption"],
            "commit_response_recovered": recovered_commit_response,
            "completed_at": _utc_now(),
        }
        completed = {
            "schema_version": 2,
            "status": "completed",
            "operation_id": operation_id,
            "source_sha": source_sha,
            "plan_sha256": confirm_plan_sha256,
            "report": report,
            "report_sha256": _digest(report),
        }
        _write_completed_atomic(completed_path, completed)
        completed_raw, _payload = _load_private_json(completed_path)
        completed = _validate_completed(
            completed_raw, source_sha=source_sha, operation_id=operation_id, plan_sha256=confirm_plan_sha256
        )
        journal = _advance_journal(journal_path, journal, "completed", plan)
        return completed
    finally:
        password[:] = b"\x00" * len(password)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--operation-id", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan", action="store_true")
    action.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-plan-sha256")
    parser.add_argument("--confirm-public-lo-acl-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    if not sys.flags.isolated:
        print("mutable-role-provision: isolated Python startup is required", file=sys.stderr)
        return 2
    args = _parser().parse_args(argv)
    try:
        with _deploy_lock(RUNTIME_ROOT):
            if args.plan:
                _require(
                    args.confirm_plan_sha256 is None
                    and args.confirm_public_lo_acl_sha256 is None,
                    "read-only plan rejects confirmations",
                )
                result = build_plan(args.sha, args.operation_id)
            else:
                _require(isinstance(args.confirm_plan_sha256, str), "apply requires plan confirmation")
                _require(
                    isinstance(args.confirm_public_lo_acl_sha256, str),
                    "apply requires explicit PUBLIC LO ACL impact confirmation",
                )
                result = apply_plan(
                    args.sha,
                    args.operation_id,
                    args.confirm_plan_sha256,
                    args.confirm_public_lo_acl_sha256,
                )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (RoleProvisionError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"mutable-role-provision: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
