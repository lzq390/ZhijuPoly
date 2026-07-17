#!/usr/bin/env python3
"""Fail-closed Git execution and content-bound production source evidence.

The production checkout is a deployment input, not a developer convenience.
Every caller must therefore use the same explicit Git directory, work tree,
index and object database, while system/global config and ambient Git
redirection are excluded.  The evidence emitted here binds the interpreted
local config, index, refs and object-store topology to the commit/tree that a
caller accepted.

This module intentionally uses only the Python standard library so it can be
installed in both bootstrap recovery tools and content-addressed controls.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
from typing import Any, Mapping


GIT_BINARY = Path("/usr/bin/git")
POLICY_NAME = "nexpoly-production-git-source-v1"
SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 1024 * 1024
MAX_CONTROL_FILE_BYTES = 64 * 1024 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# These values can redirect repository discovery, object reads, config,
# identity, hooks/helpers, or the binary implementation before our command
# line policy takes effect.  Harmless presentation values such as GIT_PAGER
# are not trusted either; they are simply omitted from the child environment.
FORBIDDEN_AMBIENT_EXACT = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_EXEC_PATH",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_REPLACE_REF_BASE",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_WORK_TREE",
        "GIT_CEILING_DIRECTORIES",
        "SSH_ASKPASS",
    }
)
FORBIDDEN_AMBIENT_PREFIXES = (
    "GIT_CONFIG_KEY_",
    "GIT_CONFIG_VALUE_",
    "GIT_TRACE",
)

# Local config is data in the evidence, never an extension point.  In
# particular, include/includeIf, extensions, partial clone, alternates,
# fsmonitor, sparse checkout, hooks, filters, credentials and external work
# trees have no permitted representation.
ALLOWED_CONFIG: dict[str, frozenset[str]] = {
    "core": frozenset(
        {
            "repositoryformatversion",
            "filemode",
            "bare",
            "logallrefupdates",
            "ignorecase",
            "precomposeunicode",
        }
    ),
    'remote "origin"': frozenset({"url", "fetch", "pushurl", "tagopt"}),
    'branch "main"': frozenset(
        {"remote", "merge", "vscode-merge-base"}
    ),
    "user": frozenset({"name", "email"}),
}

FORBIDDEN_MARKERS = (
    "commondir",
    "shallow",
    "config.worktree",
    "info/grafts",
    "info/sparse-checkout",
    "objects/info/alternates",
    "objects/info/http-alternates",
    "refs/replace",
)
FORBIDDEN_INDEX_EXTENSIONS = frozenset(
    {
        "FSMN",  # fsmonitor
        "UNTR",  # untracked cache
        "link",  # split/shared index
        "sdir",  # sparse index
    }
)
ALLOWED_INDEX_EXTENSIONS = frozenset(
    {
        "TREE",
        "REUC",
        "EOIE",
        "IEOT",
    }
)

SAFE_CONFIG_OVERRIDES = (
    "credential.helper=",
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.untrackedCache=false",
    "core.sparseCheckout=false",
    "core.sparseCheckoutCone=false",
    "core.attributesFile=/dev/null",
    "core.excludesFile=/dev/null",
    "diff.external=",
    "protocol.allow=never",
    "protocol.file.allow=never",
    "protocol.ext.allow=never",
    "protocol.ssh.allow=always",
    "protocol.https.allow=always",
    "fetch.fsckObjects=true",
    "transfer.fsckObjects=true",
    "fetch.writeCommitGraph=false",
    "maintenance.auto=false",
)
_OBJECT_DIGEST_CACHE: dict[
    tuple[int, int, int, int, int],
    str,
] = {}
_VERIFIED_OBJECT_STORES: set[str] = set()


class GitSourceTrustError(RuntimeError):
    """The repository cannot be interpreted as a trusted production source."""


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
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise GitSourceTrustError(f"cannot hash trusted file: {path}") from exc
    try:
        before = os.fstat(descriptor)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise GitSourceTrustError(f"trusted file changed while hashing: {path}")
    return "sha256:" + digest.hexdigest()


def _cached_object_digest(path: Path, expected: os.stat_result) -> str:
    """Hash immutable object bytes once per inode version in this process."""

    key = (
        expected.st_dev,
        expected.st_ino,
        expected.st_size,
        expected.st_mtime_ns,
        expected.st_ctime_ns,
    )
    cached = _OBJECT_DIGEST_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise GitSourceTrustError(
            "Git object entry cannot be hashed safely"
        ) from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        before_key = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if before_key != key:
            raise GitSourceTrustError(
                "Git object entry changed before content hashing"
            )
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        after_key = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
    finally:
        os.close(descriptor)
    if after_key != key:
        raise GitSourceTrustError(
            "Git object entry changed during content hashing"
        )
    value = "sha256:" + digest.hexdigest()
    _OBJECT_DIGEST_CACHE[key] = value
    return value


def assert_trusted_ambient_environment(
    environment: Mapping[str, str] | None = None,
) -> None:
    """Reject ambient variables that can redirect Git before isolation."""

    values = os.environ if environment is None else environment
    dangerous = sorted(
        key
        for key in values
        if key in FORBIDDEN_AMBIENT_EXACT
        or key.startswith(FORBIDDEN_AMBIENT_PREFIXES)
    )
    if dangerous:
        raise GitSourceTrustError(
            "ambient Git control variables are forbidden: "
            + ", ".join(dangerous)
        )


def _require_absolute_root(root: Path) -> Path:
    root = root.absolute()
    if not root.is_absolute() or ".." in root.parts:
        raise GitSourceTrustError("repository root must be absolute")
    try:
        metadata = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise GitSourceTrustError("repository root is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or root.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
        or resolved != root
    ):
        raise GitSourceTrustError(
            "repository root must be owner-controlled and non-symlink"
        )
    return root


def _require_directory(path: Path, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GitSourceTrustError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        raise GitSourceTrustError(f"{label} is not owner-controlled")
    return metadata


def _read_control_file(
    path: Path,
    *,
    label: str,
    required: bool,
    maximum: int = MAX_CONTROL_FILE_BYTES,
) -> tuple[bytes, os.stat_result] | None:
    present = path.exists() or path.is_symlink()
    if not present:
        if required:
            raise GitSourceTrustError(f"{label} is unavailable")
        return None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise GitSourceTrustError(f"{label} cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_mode & 0o022
            or before.st_nlink != 1
            or not 0 <= before.st_size <= maximum
        ):
            raise GitSourceTrustError(f"{label} has unsafe metadata")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise GitSourceTrustError(f"{label} was truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise GitSourceTrustError(f"{label} changed while being read")
    return b"".join(chunks), before


def _canonical_config(payload: bytes) -> list[dict[str, str]]:
    if len(payload) > MAX_CONFIG_BYTES:
        raise GitSourceTrustError("local Git config is unexpectedly large")
    parser = configparser.RawConfigParser(
        interpolation=None,
        strict=False,
        empty_lines_in_values=False,
    )
    parser.optionxform = str
    try:
        parser.read_string(payload.decode("utf-8"))
    except (UnicodeError, configparser.Error) as exc:
        raise GitSourceTrustError("local Git config is malformed") from exc
    records: list[dict[str, str]] = []
    for section in parser.sections():
        normalized_section = section.lower()
        allowed = ALLOWED_CONFIG.get(normalized_section)
        if allowed is None:
            raise GitSourceTrustError(
                f"local Git config contains forbidden section: {section}"
            )
        for raw_key, raw_value in parser.items(section, raw=True):
            key = raw_key.lower()
            if key not in allowed:
                raise GitSourceTrustError(
                    "local Git config contains executable or redirect policy: "
                    f"{section}.{raw_key}"
                )
            value = raw_value.strip()
            if "\x00" in value or "\n" in value or "\r" in value:
                raise GitSourceTrustError("local Git config value is unsafe")
            records.append(
                {
                    "section": normalized_section,
                    "key": key,
                    "value": value,
                }
            )
    core = {
        record["key"]: record["value"].lower()
        for record in records
        if record["section"] == "core"
    }
    if (
        core.get("repositoryformatversion") not in {None, "0"}
        or core.get("bare") not in {None, "false", "no", "off", "0"}
    ):
        raise GitSourceTrustError("local Git repository format is unsupported")
    return sorted(
        records,
        key=lambda record: (
            record["section"],
            record["key"],
            record["value"],
        ),
    )


def _index_extensions(payload: bytes) -> list[str]:
    if len(payload) < 32 or payload[:4] != b"DIRC":
        raise GitSourceTrustError("Git index header is invalid")
    version, count = struct.unpack(">II", payload[4:12])
    if version not in {2, 3}:
        # Version 4 path compression and future formats are deliberately not
        # accepted by the production trust parser.
        raise GitSourceTrustError("Git index version is unsupported")
    offset = 12
    checksum_size = 20
    payload_end = len(payload) - checksum_size
    for _entry in range(count):
        start = offset
        if offset + 62 > payload_end:
            raise GitSourceTrustError("Git index entry is truncated")
        flags = struct.unpack(">H", payload[offset + 60 : offset + 62])[0]
        offset += 62
        if version == 3 and flags & 0x4000:
            if offset + 2 > payload_end:
                raise GitSourceTrustError("Git extended index entry is truncated")
            offset += 2
        name_length = flags & 0x0FFF
        if name_length == 0x0FFF:
            terminator = payload.find(b"\0", offset, payload_end)
            if terminator < 0:
                raise GitSourceTrustError("Git index pathname is unterminated")
            offset = terminator + 1
        else:
            offset += name_length
            if offset >= payload_end or payload[offset] != 0:
                raise GitSourceTrustError("Git index pathname is malformed")
            offset += 1
        offset = start + ((offset - start + 7) // 8) * 8
        if offset > payload_end:
            raise GitSourceTrustError("Git index padding is malformed")
    extensions: list[str] = []
    while offset < payload_end:
        if offset + 8 > payload_end:
            raise GitSourceTrustError("Git index extension is truncated")
        raw_signature = payload[offset : offset + 4]
        size = struct.unpack(">I", payload[offset + 4 : offset + 8])[0]
        offset += 8
        if offset + size > payload_end:
            raise GitSourceTrustError("Git index extension payload is truncated")
        try:
            signature = raw_signature.decode("ascii")
        except UnicodeError as exc:
            raise GitSourceTrustError("Git index extension is invalid") from exc
        if signature in FORBIDDEN_INDEX_EXTENSIONS:
            raise GitSourceTrustError(
                f"Git index contains forbidden extension: {signature}"
            )
        if signature not in ALLOWED_INDEX_EXTENSIONS:
            raise GitSourceTrustError(
                f"Git index contains unsupported extension: {signature}"
            )
        extensions.append(signature)
        offset += size
    if offset != payload_end:
        raise GitSourceTrustError("Git index structure is malformed")
    return extensions


def _object_store_evidence(objects: Path) -> dict[str, Any]:
    _require_directory(objects, label="Git object database")
    records: list[dict[str, Any]] = []
    total_size = 0
    for directory, directory_names, file_names in os.walk(
        objects,
        topdown=True,
        followlinks=False,
    ):
        current = Path(directory)
        _require_directory(current, label="Git object directory")
        for name in sorted(directory_names):
            child = current / name
            _require_directory(child, label="Git object directory")
        for name in sorted(file_names):
            path = current / name
            relative = path.relative_to(objects).as_posix()
            if name.endswith(".promisor"):
                raise GitSourceTrustError(
                    "promisor/partial-clone object storage is forbidden"
                )
            try:
                descriptor = os.open(
                    path,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                )
            except OSError as exc:
                raise GitSourceTrustError(
                    "Git object entry cannot be opened safely"
                ) from exc
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o022
                    or metadata.st_nlink != 1
                    or metadata.st_size < 0
                ):
                    raise GitSourceTrustError(
                        "Git object entry has unsafe metadata"
                    )
                before = (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                )
                pack_header = b""
                pack_trailer = b""
                if name.endswith(".pack"):
                    pack_header = os.read(descriptor, 4)
                    if metadata.st_size >= 20:
                        os.lseek(descriptor, -20, os.SEEK_END)
                        pack_trailer = os.read(descriptor, 20)
                after_metadata = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            after = (
                after_metadata.st_dev,
                after_metadata.st_ino,
                after_metadata.st_size,
                after_metadata.st_mtime_ns,
            )
            if before != after:
                raise GitSourceTrustError(
                    "Git object entry changed while being inventoried"
                )
            total_size += metadata.st_size
            item: dict[str, Any] = {
                "path": relative,
                "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
                "size": metadata.st_size,
                "sha256": _cached_object_digest(path, metadata),
            }
            if name.endswith(".pack"):
                if (
                    metadata.st_size < 32
                    or pack_header != b"PACK"
                    or len(pack_trailer) != 20
                ):
                    raise GitSourceTrustError("Git pack file is malformed")
                # The trailing SHA-1 covers the full pack body and is the
                # object-format-native content binding used by this SHA-1 repo.
                item["pack_trailer_sha1"] = pack_trailer.hex()
            elif re.fullmatch(r"[0-9a-f]{38}", name) and re.fullmatch(
                r"[0-9a-f]{2}", path.parent.name
            ):
                item["object_id"] = path.parent.name + name
            records.append(item)
    return {
        "object_count": len(records),
        "total_size": total_size,
        "inventory_sha256": sha256_bytes(canonical_json_bytes(records)),
        "standalone": True,
        "promisor": False,
        "alternates": False,
    }


def _refs_evidence(git_dir: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    refs = git_dir / "refs"
    _require_directory(refs, label="Git refs directory")
    for directory, directory_names, file_names in os.walk(
        refs,
        topdown=True,
        followlinks=False,
    ):
        current = Path(directory)
        _require_directory(current, label="Git refs directory")
        for name in sorted(directory_names):
            child = current / name
            _require_directory(child, label="Git refs directory")
        for name in sorted(file_names):
            path = current / name
            relative = path.relative_to(git_dir).as_posix()
            if relative.startswith("refs/replace/"):
                raise GitSourceTrustError("Git replacement refs are forbidden")
            record = _read_control_file(
                path,
                label="Git ref",
                required=True,
                maximum=1024 * 1024,
            )
            assert record is not None
            payload, metadata = record
            records.append(
                {
                    "path": relative,
                    "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
                    "sha256": sha256_bytes(payload),
                }
            )
    packed = _read_control_file(
        git_dir / "packed-refs",
        label="packed Git refs",
        required=False,
        maximum=MAX_CONFIG_BYTES,
    )
    packed_digest: str | None = None
    if packed is not None:
        packed_payload, _metadata = packed
        try:
            packed_text = packed_payload.decode("ascii")
        except UnicodeError as exc:
            raise GitSourceTrustError("packed Git refs are malformed") from exc
        if any(
            line.strip().endswith(" refs/replace")
            or " refs/replace/" in line
            for line in packed_text.splitlines()
        ):
            raise GitSourceTrustError("packed Git replacement refs are forbidden")
        packed_digest = sha256_bytes(packed_payload)
    return {
        "loose_count": len(records),
        "loose_sha256": sha256_bytes(canonical_json_bytes(records)),
        "packed_refs_sha256": packed_digest,
        "replace_refs": 0,
    }


def _git_binary_evidence() -> dict[str, Any]:
    try:
        metadata = GIT_BINARY.lstat()
        resolved = GIT_BINARY.resolve(strict=True)
    except OSError as exc:
        raise GitSourceTrustError("fixed Git binary is unavailable") from exc
    if (
        resolved != GIT_BINARY
        or not stat.S_ISREG(metadata.st_mode)
        or GIT_BINARY.is_symlink()
        or metadata.st_uid != 0
        or metadata.st_mode & 0o022
    ):
        raise GitSourceTrustError("fixed Git binary identity is unsafe")
    return {
        "path": str(GIT_BINARY),
        "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
        "size": metadata.st_size,
        "sha256": sha256_file(GIT_BINARY),
    }


def safe_git_environment(
    root: Path,
    *,
    ambient: Mapping[str, str] | None = None,
    home: str = "/nonexistent",
    ssh_command: str | None = None,
) -> dict[str, str]:
    """Return the complete environment for a trusted Git child process."""

    assert_trusted_ambient_environment(ambient)
    root = _require_absolute_root(root)
    git_dir = root / ".git"
    _require_directory(git_dir, label="Git metadata directory")
    _require_directory(git_dir / "objects", label="Git object database")
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": home,
        "LANG": "C",
        "LC_ALL": "C",
        "XDG_CONFIG_HOME": "/nonexistent",
        "GIT_DIR": str(git_dir),
        "GIT_COMMON_DIR": str(git_dir),
        "GIT_WORK_TREE": str(root),
        "GIT_OBJECT_DIRECTORY": str(git_dir / "objects"),
        "GIT_INDEX_FILE": str(git_dir / "index"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
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
    }
    environment["GIT_SSH_COMMAND"] = ssh_command or "/bin/false"
    return environment


def safe_git_command(
    root: Path,
    *arguments: str,
    executable: str = "/usr/bin/git",
) -> list[str]:
    """Build a Git command whose local policy is explicitly overridden."""

    root = _require_absolute_root(root)
    if (
        not arguments
        or not isinstance(arguments[0], str)
        or arguments[0].startswith("-")
    ):
        raise GitSourceTrustError("trusted Git command requires an explicit subcommand")
    if executable not in {"/usr/bin/git", "git"}:
        raise GitSourceTrustError("trusted Git command uses an unexpected executable")
    forbidden = (
        "-c",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--super-prefix",
    )
    for argument in arguments:
        if (
            not isinstance(argument, str)
            or "\x00" in argument
            or "\n" in argument
            or "\r" in argument
            or argument in forbidden
            or any(argument.startswith(value + "=") for value in forbidden)
        ):
            raise GitSourceTrustError(
                "trusted Git command contains a control-plane redirect"
            )
    command = [executable]
    for value in SAFE_CONFIG_OVERRIDES:
        command.extend(("-c", value))
    command.extend(arguments)
    return command


def run_git(
    root: Path,
    *arguments: str,
    ambient: Mapping[str, str] | None = None,
    home: str = "/nonexistent",
    ssh_command: str | None = None,
    text: bool = True,
    check: bool = True,
    timeout: int = 120,
) -> subprocess.CompletedProcess[Any]:
    """Execute Git through the fixed production trust boundary."""

    try:
        return subprocess.run(
            safe_git_command(root, *arguments),
            cwd=root,
            env=safe_git_environment(
                root,
                ambient=ambient,
                home=home,
                ssh_command=ssh_command,
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
            check=check,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitSourceTrustError("trusted Git command failed") from exc


def repository_trust_evidence(
    root: Path,
    *,
    source_sha: str,
    source_tree: str,
    branch: str,
    origin: str | None,
    ambient: Mapping[str, str] | None = None,
    home: str = "/nonexistent",
    ssh_command: str | None = None,
    verify_identity: bool = True,
) -> dict[str, Any]:
    """Seal the exact filesystem/config interpretation of a checkout."""

    if (
        SHA_RE.fullmatch(source_sha) is None
        or SHA_RE.fullmatch(source_tree) is None
        or branch != "refs/heads/main"
        or origin is not None
        and (
            not isinstance(origin, str)
            or not origin
            or "\x00" in origin
            or "\n" in origin
        )
    ):
        raise GitSourceTrustError("source commit/tree/branch identity is invalid")
    root = _require_absolute_root(root)
    git_dir = root / ".git"
    _require_directory(git_dir, label="Git metadata directory")
    for relative in FORBIDDEN_MARKERS:
        marker = git_dir / relative
        if marker.exists() or marker.is_symlink():
            raise GitSourceTrustError(
                f"forbidden Git storage or policy marker exists: {relative}"
            )
    config_record = _read_control_file(
        git_dir / "config",
        label="local Git config",
        required=True,
        maximum=MAX_CONFIG_BYTES,
    )
    head_record = _read_control_file(
        git_dir / "HEAD",
        label="Git HEAD",
        required=True,
        maximum=4096,
    )
    index_record = _read_control_file(
        git_dir / "index",
        label="Git index",
        required=True,
    )
    assert config_record is not None
    assert head_record is not None
    assert index_record is not None
    config_payload, config_metadata = config_record
    head_payload, head_metadata = head_record
    index_payload, index_metadata = index_record
    if head_payload.strip() != b"ref: refs/heads/main":
        raise GitSourceTrustError("Git HEAD is not exact local main")
    canonical_config = _canonical_config(config_payload)
    index_extensions = _index_extensions(index_payload)
    object_evidence = _object_store_evidence(git_dir / "objects")
    environment = safe_git_environment(
        root,
        ambient=ambient,
        home=home,
        ssh_command=ssh_command,
    )
    if verify_identity:
        try:
            observed_branch = run_git(
                root,
                "symbolic-ref",
                "--quiet",
                "HEAD",
                ambient=ambient,
                home=home,
                ssh_command=ssh_command,
            ).stdout.strip()
            observed_sha = run_git(
                root,
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
                ambient=ambient,
                home=home,
                ssh_command=ssh_command,
            ).stdout.strip()
            observed_tree = run_git(
                root,
                "rev-parse",
                "--verify",
                "HEAD^{tree}",
                ambient=ambient,
                home=home,
                ssh_command=ssh_command,
            ).stdout.strip()
            observed_top = run_git(
                root,
                "rev-parse",
                "--show-toplevel",
                ambient=ambient,
                home=home,
                ssh_command=ssh_command,
            ).stdout.strip()
            observed_origin = (
                run_git(
                    root,
                    "remote",
                    "get-url",
                    "origin",
                    ambient=ambient,
                    home=home,
                    ssh_command=ssh_command,
                ).stdout.strip()
                if origin is not None
                else None
            )
        except Exception as exc:
            raise GitSourceTrustError(
                "cannot independently verify Git source identity"
            ) from exc
        if (
            observed_branch != branch
            or observed_sha != source_sha
            or observed_tree != source_tree
            or observed_top != str(root)
            or observed_origin != origin
        ):
            raise GitSourceTrustError(
                "Git source identity differs from sealed evidence"
            )
        object_inventory = object_evidence["inventory_sha256"]
        if object_inventory not in _VERIFIED_OBJECT_STORES:
            try:
                run_git(
                    root,
                    "fsck",
                    "--full",
                    "--strict",
                    "--no-reflogs",
                    "--no-dangling",
                    ambient=ambient,
                    home=home,
                    ssh_command=ssh_command,
                    timeout=600,
                )
            except Exception as exc:
                raise GitSourceTrustError(
                    "Git object database failed strict verification"
                ) from exc
            _VERIFIED_OBJECT_STORES.add(object_inventory)
    # The SSH command binds credential *paths*, not credential bytes.  Secret
    # material remains outside evidence and is separately mode/hash governed.
    environment_evidence = {
        key: environment[key]
        for key in sorted(environment)
    }
    evidence: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "policy": POLICY_NAME,
        "repository_root": str(root),
        "git_dir": str(git_dir),
        "object_dir": str(git_dir / "objects"),
        "index_path": str(git_dir / "index"),
        "source": {
            "sha": source_sha,
            "tree": source_tree,
            "branch": branch,
            "origin": origin,
        },
        "git_binary": _git_binary_evidence(),
        "local_config": {
            "mode": format(stat.S_IMODE(config_metadata.st_mode), "04o"),
            "size": config_metadata.st_size,
            "raw_sha256": sha256_bytes(config_payload),
            "canonical": canonical_config,
            "canonical_sha256": sha256_bytes(
                canonical_json_bytes(canonical_config)
            ),
            "includes": False,
            "conditional_includes": False,
            "external_worktree": False,
            "fsmonitor": False,
            "sparse_checkout": False,
            "promisor": False,
        },
        "head": {
            "mode": format(stat.S_IMODE(head_metadata.st_mode), "04o"),
            "sha256": sha256_bytes(head_payload),
            "symbolic_ref": "refs/heads/main",
        },
        "index": {
            "mode": format(stat.S_IMODE(index_metadata.st_mode), "04o"),
            "size": index_metadata.st_size,
            "sha256": sha256_bytes(index_payload),
            "version": struct.unpack(">I", index_payload[4:8])[0],
            "extensions": index_extensions,
            "external": False,
            "sparse": False,
            "fsmonitor": False,
            "split": False,
        },
        "refs": _refs_evidence(git_dir),
        "objects": object_evidence,
        "forbidden_markers_absent": list(FORBIDDEN_MARKERS),
        "execution_environment": {
            "keys": sorted(environment_evidence),
            "sha256": sha256_bytes(
                canonical_json_bytes(environment_evidence)
            ),
            "system_config": False,
            "global_config": False,
            "ambient_redirects": False,
            "replace_objects": False,
            "lazy_fetch": False,
        },
    }
    evidence["trust_surface_sha256"] = sha256_bytes(
        canonical_json_bytes(
            {
                key: value
                for key, value in evidence.items()
                if key != "source"
            }
        )
    )
    evidence["evidence_sha256"] = sha256_bytes(canonical_json_bytes(evidence))
    return evidence


def repository_preflight_evidence(
    root: Path,
    *,
    ambient: Mapping[str, str] | None = None,
    home: str = "/nonexistent",
    ssh_command: str | None = None,
) -> dict[str, Any]:
    """Validate the complete trust surface before the first Git invocation."""

    return repository_trust_evidence(
        root,
        source_sha="0" * 40,
        source_tree="0" * 40,
        branch="refs/heads/main",
        origin=None,
        ambient=ambient,
        home=home,
        ssh_command=ssh_command,
        verify_identity=False,
    )


def require_stable_trust_surface(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> None:
    before_digest = before.get("trust_surface_sha256")
    after_digest = after.get("trust_surface_sha256")
    if (
        not isinstance(before_digest, str)
        or DIGEST_RE.fullmatch(before_digest) is None
        or before_digest != after_digest
    ):
        raise GitSourceTrustError(
            "repository trust surface changed during verification"
        )


def require_stable_evidence(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> None:
    """Require exact evidence equality across a read-only identity operation."""

    before_digest = before.get("evidence_sha256")
    after_digest = after.get("evidence_sha256")
    if (
        not isinstance(before_digest, str)
        or DIGEST_RE.fullmatch(before_digest) is None
        or before != after
        or before_digest != after_digest
    ):
        raise GitSourceTrustError(
            "repository trust evidence changed during verification"
        )
