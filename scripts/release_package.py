#!/usr/bin/env python3
"""Build and verify a deterministic NexPoly source/asset handoff archive."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import BinaryIO, Iterable, Iterator


MANIFEST_NAME = "RELEASE-MANIFEST.json"
ALLOWED_ENV_EXAMPLES = {"backend/.env.example", "frontend/.env.example"}
FORBIDDEN_DEV_PATHS = {"docker-compose.dev.yml", "scripts/dev_server_gpu.sh"}
REQUIRED_SOURCE_PATHS = (
    "Dockerfile",
    "frontend/Dockerfile",
    "docker-compose.yml",
    "nginx.conf",
    "backend/.env.example",
    "frontend/.env.example",
)
DATA_ASSETS = (
    "database/data1.csv",
    "database/data_txt.zip",
    "database/polymer_process_material_filtered_cleaned_office_utf8_bom.csv",
    "database/polymer_property_detail_cleaned_office_utf8_bom.csv",
    "database/PolymerDatabaseV2.0_reliable085_standardized.csv",
    "backend/data/polyprop.db",
    "backend/data/pi_reverse_design.db",
    "backend/data/fumol.db",
)
ENV_ASSIGNMENT = re.compile(
    rb"(?m)^[ \t]*(?:export[ \t]+)?"
    rb"([A-Z][A-Z0-9_]*)[ \t]*=[ \t]*([^\r\n]{0,16384})"
)
CREDENTIAL_KEY_SUFFIXES = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "CREDENTIALS")
MAX_ENV_FILE_SIZE = 16 * 1024 * 1024
PLACEHOLDER_MARKERS = (
    b"changeme",
    b"example",
    b"placeholder",
    b"replace-me",
    b"replace_me",
    b"your-",
    b"your_",
)


class ReleaseError(RuntimeError):
    pass


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_ATTR_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_SYSTEM"] = os.devnull
    environment.pop("GIT_CONFIG_COUNT", None)
    for key in tuple(environment):
        if key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
            environment.pop(key, None)
    return environment


def _run(
    args: list[str],
    *,
    cwd: Path,
    capture_output: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            check=True,
            capture_output=capture_output,
            text=text,
            env=_git_environment() if args and args[0] == "git" else None,
        )
    except FileNotFoundError as exc:
        raise ReleaseError(f"Required command is unavailable: {args[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise ReleaseError(f"Command failed ({' '.join(args)}){suffix}") from exc


def _git_text(root: Path, *args: str) -> str:
    return _run(["git", *args], cwd=root).stdout.strip()


def verify_clean_head(root: Path, source_commit: str) -> tuple[str, str, int]:
    try:
        top_level = Path(_git_text(root, "rev-parse", "--show-toplevel")).resolve()
    except ReleaseError as exc:
        raise ReleaseError("Release packaging must run inside a Git worktree.") from exc
    if top_level != root:
        raise ReleaseError(f"Release root must be the Git worktree root: {top_level}")

    replace_refs = _git_text(root, "replace", "-l").splitlines()
    if replace_refs:
        raise ReleaseError("Git replace refs are not allowed for release packaging.")
    for metadata_name in ("info/attributes", "info/grafts"):
        raw_path = _git_text(root, "rev-parse", "--git-path", metadata_name)
        metadata_path = Path(raw_path)
        if not metadata_path.is_absolute():
            metadata_path = root / metadata_path
        try:
            metadata = metadata_path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ReleaseError(f"Unable to inspect Git {metadata_name}.") from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 0:
            raise ReleaseError(f"Git {metadata_name} must be an empty regular file when present.")

    commit = _git_text(root, "rev-parse", "--verify", f"{source_commit}^{{commit}}")
    if commit != source_commit:
        raise ReleaseError("The requested source commit is not a canonical full commit ID.")
    current_head = _git_text(root, "rev-parse", "--verify", "HEAD^{commit}")
    if current_head != commit:
        raise ReleaseError("HEAD changed before release packaging started; retry from a stable checkout.")
    tree = _git_text(root, "rev-parse", "--verify", f"{commit}^{{tree}}")
    epoch_text = _git_text(root, "show", "-s", "--format=%ct", commit)

    index_entries = _run(["git", "ls-files", "-v", "-z"], cwd=root, text=False).stdout
    flagged_entry_count = sum(
        1
        for entry in index_entries.split(b"\0")
        if entry and (entry[:1] == b"S" or entry[:1].islower())
    )
    if flagged_entry_count:
        raise ReleaseError(
            f"Found {flagged_entry_count} tracked path(s) with assume-unchanged or skip-worktree flags."
        )

    if subprocess.run(
        ["git", "diff", "--quiet", "--ignore-submodules=none"],
        cwd=root,
        env=_git_environment(),
    ).returncode:
        raise ReleaseError("Tracked unstaged changes are present; package only a clean HEAD.")
    if subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--ignore-submodules=none"],
        cwd=root,
        env=_git_environment(),
    ).returncode:
        raise ReleaseError("Staged changes are present; package only a clean HEAD.")

    untracked = _run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=root,
        text=False,
    ).stdout
    if untracked:
        count = len([item for item in untracked.split(b"\0") if item])
        raise ReleaseError(f"Found {count} unignored untracked path(s); package only a clean HEAD.")

    submodules = [
        line
        for line in _git_text(root, "ls-files", "--stage").splitlines()
        if line.startswith("160000 ")
    ]
    if submodules:
        raise ReleaseError("Git submodules are not supported in the release archive.")

    try:
        epoch = int(epoch_text)
    except ValueError as exc:
        raise ReleaseError("HEAD has an invalid commit timestamp.") from exc
    return commit, tree, epoch


def _validated_relative(raw_path: str) -> PurePosixPath:
    path = PurePosixPath(raw_path)
    if (
        not raw_path
        or raw_path != path.as_posix()
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in raw_path)
        or "\\" in raw_path
    ):
        raise ReleaseError(f"Unsafe release path: {raw_path!r}")
    return path


def _safe_destination(base: Path, raw_path: str) -> Path:
    relative = _validated_relative(raw_path)
    destination = base.joinpath(*relative.parts)
    if base.resolve() not in destination.parent.resolve().parents and destination.parent.resolve() != base.resolve():
        raise ReleaseError(f"Release path escapes staging root: {raw_path}")
    return destination


def _extract_git_archive(root: Path, source_commit: str, destination: Path) -> None:
    process = subprocess.Popen(
        [
            "git",
            "-c",
            f"core.attributesFile={os.devnull}",
            "archive",
            "--format=tar",
            source_commit,
        ],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_environment(),
    )
    assert process.stdout is not None
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                relative = _validated_relative(member.name)
                target = destination.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(0o755)
                    continue
                if not member.isfile():
                    raise ReleaseError(f"Tracked links and special files are not allowed: {member.name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ReleaseError(f"Unable to read tracked file: {member.name}")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    finally:
        process.stdout.close()
    stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
    return_code = process.wait()
    if return_code:
        raise ReleaseError(f"git archive failed: {stderr.strip()}")


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _allowed_roots(
    repo_root: Path,
    variable_name: str,
    default_relative_roots: Iterable[str],
) -> tuple[Path, ...]:
    roots: list[Path] = []
    for relative_root in default_relative_roots:
        relative = _validated_relative(relative_root)
        candidate = repo_root.joinpath(*relative.parts)
        try:
            metadata = candidate.lstat()
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ReleaseError(f"Default asset root is unavailable: {relative_root}") from exc
        if stat.S_ISLNK(metadata.st_mode) or resolved != candidate.absolute():
            raise ReleaseError(f"Default asset root must not be a symlink: {relative_root}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ReleaseError(f"Default asset root is not a directory: {relative_root}")
        roots.append(resolved)

    for raw_root in os.environ.get(variable_name, "").split(os.pathsep):
        if not raw_root:
            continue
        candidate = Path(raw_root).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ReleaseError(f"{variable_name} contains an unavailable root: {candidate}") from exc
        if not resolved.is_dir():
            raise ReleaseError(f"{variable_name} entry is not a directory: {candidate}")
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def _is_within(path: Path, roots: Iterable[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _resolve_asset(source: Path, allowed_roots: tuple[Path, ...], display_path: str) -> Path:
    try:
        resolved = source.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ReleaseError(f"Missing, broken, or cyclic asset entry: {display_path}") from exc
    if not _is_within(resolved, allowed_roots):
        raise ReleaseError(
            f"Asset target is outside approved roots: {display_path}. "
            "Set the matching RELEASE_ALLOWED_*_ROOTS variable explicitly."
        )
    return resolved


def _copy_nonempty_file(source: Path, destination: Path, display_path: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise ReleaseError(f"Unable to safely open asset: {display_path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ReleaseError(f"Asset is not a regular file: {display_path}")
        if metadata.st_size <= 0:
            raise ReleaseError(f"Asset is empty: {display_path}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(descriptor, "rb", closefd=False) as source_file, destination.open("wb") as output:
            shutil.copyfileobj(source_file, output)
        destination.chmod(0o644)
    except OSError as exc:
        raise ReleaseError(f"Unable to copy asset: {display_path}") from exc
    finally:
        os.close(descriptor)


def _copy_tree(source: Path, destination: Path, display_path: str) -> list[Path]:
    try:
        metadata = source.stat()
    except OSError as exc:
        raise ReleaseError(f"Unable to inspect model tree: {display_path}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise ReleaseError(f"Model tree entry is not a directory: {display_path}")

    copied: list[Path] = []

    def visit(current_source: Path, current_destination: Path) -> None:
        current_destination.mkdir(parents=True, exist_ok=True)
        current_destination.chmod(0o755)
        try:
            entries = sorted(os.scandir(current_source), key=lambda item: item.name)
        except OSError as exc:
            raise ReleaseError(f"Unable to enumerate model tree: {display_path}") from exc
        for entry in entries:
            entry_source = Path(entry.path)
            entry_destination = current_destination / entry.name
            if entry.is_symlink():
                raise ReleaseError(f"Nested symlink is not allowed in model tree: {display_path}/{entry.name}")
            if entry.is_dir(follow_symlinks=False):
                visit(entry_source, entry_destination)
            elif entry.is_file(follow_symlinks=False):
                relative = entry_source.relative_to(source).as_posix()
                _copy_nonempty_file(entry_source, entry_destination, f"{display_path}/{relative}")
                copied.append(entry_destination)
            else:
                raise ReleaseError(f"Special filesystem node is not allowed in model tree: {display_path}/{entry.name}")

    visit(source, destination)
    if not copied:
        raise ReleaseError(f"Model tree contains no files: {display_path}")
    return copied


def _validate_existing_tree(source: Path, display_path: str) -> list[Path]:
    if source.is_symlink() or not source.is_dir():
        raise ReleaseError(f"Tracked model tree is unavailable in HEAD: {display_path}")
    files: list[Path] = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        if path.is_symlink():
            raise ReleaseError(f"Tracked model tree contains a symlink: {display_path}/{relative}")
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            path.chmod(0o755)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ReleaseError(f"Tracked model tree contains a special node: {display_path}/{relative}")
        if metadata.st_size <= 0:
            raise ReleaseError(f"Asset is empty: {display_path}/{relative}")
        path.chmod(0o644)
        files.append(path)
    if not files:
        raise ReleaseError(f"Model tree contains no files: {display_path}")
    return files


def _git_object_type(repo_root: Path, source_commit: str, raw_path: str) -> str | None:
    result = _run(
        ["git", "ls-tree", "-z", source_commit, "--", raw_path],
        cwd=repo_root,
        text=False,
    )
    records = [record for record in result.stdout.split(b"\0") if record]
    if not records:
        return None
    expected_path = raw_path.encode("utf-8")
    for record in records:
        try:
            header, record_path = record.split(b"\t", 1)
            _mode, object_type, _object_id = header.split(b" ", 2)
        except ValueError as exc:
            raise ReleaseError("git ls-tree returned an invalid tracked asset record.") from exc
        if record_path == expected_path:
            return object_type.decode("ascii")
    return None


def _export_git_blob(repo_root: Path, source_commit: str, raw_path: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        ["git", "cat-file", "blob", f"{source_commit}:{raw_path}"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_environment(),
    )
    assert process.stdout is not None
    try:
        with destination.open("xb") as output:
            shutil.copyfileobj(process.stdout, output)
    finally:
        process.stdout.close()
    stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
    return_code = process.wait()
    if return_code:
        destination.unlink(missing_ok=True)
        raise ReleaseError(f"Unable to materialize tracked asset from HEAD: {raw_path}: {stderr.strip()}")
    destination.chmod(0o644)


def _copy_asset(
    *,
    repo_root: Path,
    staging_root: Path,
    raw_path: str,
    kind: str,
    allowed_roots: tuple[Path, ...],
) -> list[Path]:
    relative = _validated_relative(raw_path)
    source_entry = repo_root.joinpath(*relative.parts)
    source = _resolve_asset(source_entry, allowed_roots, raw_path)
    destination = staging_root.joinpath(*relative.parts)
    if kind == "file":
        _copy_nonempty_file(source, destination, raw_path)
        return [destination]
    if kind == "tree":
        return _copy_tree(source, destination, raw_path)
    raise ReleaseError(f"Unsupported asset kind for {raw_path}: {kind}")


def _materialize_asset(
    *,
    repo_root: Path,
    source_commit: str,
    staging_root: Path,
    raw_path: str,
    kind: str,
    allowed_roots: tuple[Path, ...],
) -> list[Path]:
    relative = _validated_relative(raw_path)
    destination = staging_root.joinpath(*relative.parts)
    object_type = _git_object_type(repo_root, source_commit, raw_path)
    if object_type is not None:
        expected_type = "blob" if kind == "file" else "tree"
        if object_type != expected_type:
            raise ReleaseError(
                f"Tracked asset type differs from release contract for {raw_path}: {object_type}"
            )
        if kind == "file":
            if not destination.exists():
                _export_git_blob(repo_root, source_commit, raw_path, destination)
            if destination.is_symlink():
                raise ReleaseError(f"Tracked asset is a symlink: {raw_path}")
            metadata = destination.stat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ReleaseError(f"Tracked asset is not a regular file: {raw_path}")
            if metadata.st_size <= 0:
                raise ReleaseError(f"Asset is empty: {raw_path}")
            destination.chmod(0o644)
            return [destination]
        return _validate_existing_tree(destination, raw_path)

    return _copy_asset(
        repo_root=repo_root,
        staging_root=staging_root,
        raw_path=raw_path,
        kind=kind,
        allowed_roots=allowed_roots,
    )


def _load_release_model_manifest(source_root: Path) -> list[dict[str, str]]:
    manifest_script = source_root / "backend" / "app" / "model_asset_manifest.py"
    if manifest_script.is_symlink() or not manifest_script.is_file():
        raise ReleaseError("Tracked release model manifest is unavailable in HEAD.")
    result = _run(
        [
            sys.executable,
            str(manifest_script),
            "--profile",
            "release",
            "--format",
            "json",
        ],
        cwd=source_root,
    )
    try:
        document = json.loads(result.stdout)
        assets = document["assets"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ReleaseError("Release model manifest returned invalid JSON.") from exc
    if document.get("schema_version") != 1 or document.get("profile") != "release" or not isinstance(assets, list):
        raise ReleaseError("Release model manifest has an unsupported schema.")

    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    counts: Counter[str] = Counter()
    for asset in assets:
        if not isinstance(asset, dict):
            raise ReleaseError("Release model manifest contains a non-object entry.")
        path = asset.get("path")
        kind = asset.get("kind")
        category = asset.get("category")
        if not isinstance(path, str) or not isinstance(kind, str) or not isinstance(category, str):
            raise ReleaseError("Release model manifest entries require path, kind, and category strings.")
        _validated_relative(path)
        if not path.startswith("model/") or kind not in {"file", "tree"}:
            raise ReleaseError(f"Invalid release model manifest entry: {path}")
        if category not in {"required-model", "reactiont5", "polytao"}:
            raise ReleaseError(f"Invalid release model category: {category}")
        if path in seen:
            raise ReleaseError(f"Duplicate release model asset: {path}")
        seen.add(path)
        counts[category] += 1
        normalized.append({"path": path, "kind": kind, "category": category})

    if counts != Counter({"required-model": 21, "polytao": 4, "reactiont5": 1}):
        raise ReleaseError(f"Unexpected release model contract counts: {dict(counts)}")
    if any(asset["kind"] != "file" for asset in normalized if asset["category"] != "reactiont5"):
        raise ReleaseError("Only the ReactionT5 release asset may be a model tree.")
    reaction_assets = [asset for asset in normalized if asset["category"] == "reactiont5"]
    if reaction_assets[0]["kind"] != "tree":
        raise ReleaseError("The ReactionT5 release asset must be a tree.")
    return normalized


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_placeholder(raw_value: bytes) -> bool:
    value = raw_value.strip().strip(b"'\"").lower()
    return (
        not value
        or value.startswith((b"$", b"<"))
        or b"${" in value
        or any(marker in value for marker in PLACEHOLDER_MARKERS)
    )


def _is_credential_key(key: str) -> bool:
    return any(key == suffix or key.endswith(f"_{suffix}") for suffix in CREDENTIAL_KEY_SUFFIXES)


def _normalized_env_value(raw_value: bytes) -> bytes:
    value = raw_value.strip()
    if value[:1] in {b"'", b'"'}:
        quote = value[:1]
        closing_index = value.rfind(quote)
        if closing_index > 0:
            remainder = value[closing_index + 1 :].strip()
            if not remainder or remainder.startswith(b"#"):
                return value[1:closing_index]
    if b" #" in value:
        value = value.split(b" #", 1)[0].rstrip()
    return value


def _looks_like_inline_credential(value: bytes) -> bool:
    return (
        len(value) >= 16
        and not _is_placeholder(value)
        and not any(character in b" \t" for character in value)
        and not value.startswith((b"(", b"[", b"{"))
    )


def _local_secret_needles(root: Path) -> dict[bytes, set[str]]:
    needles: dict[bytes, set[str]] = {}
    env_paths = {
        path
        for directory in (root, root / "backend", root / "frontend")
        if directory.is_dir()
        for path in directory.glob(".env*")
    }
    for env_path in sorted(env_paths):
        if env_path.name.endswith(".example"):
            continue
        display_path = env_path.relative_to(root).as_posix()
        try:
            resolved = env_path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ReleaseError(f"Missing, broken, or cyclic local environment file: {display_path}") from exc
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(resolved, flags)
        except OSError as exc:
            raise ReleaseError(f"Unable to safely open local environment file: {display_path}") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ReleaseError(f"Local environment entry is not a regular file: {display_path}")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                content = source.read(MAX_ENV_FILE_SIZE + 1)
            if len(content) > MAX_ENV_FILE_SIZE:
                raise ReleaseError(f"Local environment file exceeds 16 MiB: {display_path}")
        except OSError as exc:
            raise ReleaseError(f"Unable to inspect local environment file: {display_path}") from exc
        finally:
            os.close(descriptor)
        for match in ENV_ASSIGNMENT.finditer(content):
            key = match.group(1).decode("ascii")
            value = _normalized_env_value(match.group(2))
            if _is_credential_key(key) and len(value) >= 16 and not _is_placeholder(value):
                needles.setdefault(value, set()).add(key)
    return needles


def _secret_keys_in_stream(stream: BinaryIO, secret_needles: dict[bytes, set[str]]) -> set[str]:
    matches: set[str] = set()
    line_carry = b""
    needle_carry = b""
    needle_overlap = max((len(value) - 1 for value in secret_needles), default=0)
    while True:
        chunk = stream.read(1024 * 1024)
        needle_data = needle_carry + chunk
        for value, keys in secret_needles.items():
            if value in needle_data:
                matches.update(keys)
        needle_carry = needle_data[-needle_overlap:] if needle_overlap else b""

        line_data = line_carry + chunk
        if not chunk:
            complete_lines = line_data
            line_carry = b""
        else:
            last_newline = max(line_data.rfind(b"\n"), line_data.rfind(b"\r"))
            if last_newline >= 0:
                complete_lines = line_data[: last_newline + 1]
                line_carry = line_data[last_newline + 1 :]
            else:
                complete_lines = b""
                line_carry = line_data[-16384:]
        for match in ENV_ASSIGNMENT.finditer(complete_lines):
            key = match.group(1).decode("ascii")
            value = _normalized_env_value(match.group(2))
            if _is_credential_key(key) and _looks_like_inline_credential(value):
                matches.add(key)
        if not chunk:
            break
    return matches


def _prune_empty_directories(staging_root: Path) -> None:
    directories = sorted(
        (path for path in staging_root.rglob("*") if path.is_dir() and not path.is_symlink()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


def _validate_payload_paths(staging_root: Path, secret_needles: dict[bytes, set[str]]) -> list[Path]:
    files: list[Path] = []
    for path in sorted(staging_root.rglob("*")):
        relative = path.relative_to(staging_root).as_posix()
        _validated_relative(relative)
        if path.is_symlink():
            raise ReleaseError(f"Symlink remains in release staging: {relative}")
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            path.chmod(0o755)
            continue
        if not stat.S_ISREG(mode):
            raise ReleaseError(f"Special filesystem node remains in release staging: {relative}")
        path.chmod(0o755 if mode & 0o111 else 0o644)
        files.append(path)

        if relative not in ALLOWED_ENV_EXAMPLES and any(part.startswith(".env") for part in PurePosixPath(relative).parts):
            raise ReleaseError(f"Environment file is forbidden in release payload: {relative}")
        if relative in FORBIDDEN_DEV_PATHS or PurePosixPath(relative).name in {"docker-compose.dev.yml", "dev_server_gpu.sh"}:
            raise ReleaseError(f"Local development overlay is forbidden in release payload: {relative}")

        with path.open("rb") as source:
            secret_keys = _secret_keys_in_stream(source, secret_needles)
        if secret_keys:
            keys = ", ".join(sorted(secret_keys))
            raise ReleaseError(f"Potential secret key(s) {keys} found in release path {relative}")
    return files


def _category_for_path(path: str, asset_categories: dict[str, str], include_data: bool) -> str:
    if path in asset_categories:
        return asset_categories[path]
    if include_data and path in DATA_ASSETS:
        return "data"
    return "source"


def _write_manifest(
    staging_root: Path,
    *,
    commit: str,
    tree: str,
    include_data: bool,
    asset_categories: dict[str, str],
    secret_needles: dict[bytes, set[str]],
) -> dict[str, object]:
    if (staging_root / MANIFEST_NAME).exists():
        raise ReleaseError(f"Tracked source reserves generated release path: {MANIFEST_NAME}")
    _prune_empty_directories(staging_root)
    files = _validate_payload_paths(staging_root, secret_needles)
    entries: list[dict[str, object]] = []
    for path in files:
        relative = path.relative_to(staging_root).as_posix()
        entries.append(
            {
                "path": relative,
                "category": _category_for_path(relative, asset_categories, include_data),
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    document: dict[str, object] = {
        "schema": "nexpoly.release-manifest.v1",
        "schema_version": 1,
        "commit": commit,
        "tree": tree,
        "include_data": include_data,
        "files": entries,
    }
    manifest_path = staging_root / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o644)
    return document


def _expected_directories(file_paths: Iterable[str]) -> set[str]:
    directories: set[str] = set()
    for raw_path in file_paths:
        current = PurePosixPath(raw_path).parent
        while current.as_posix() != ".":
            directories.add(current.as_posix())
            current = current.parent
    return directories


def _create_archive(staging_parent: Path, archive_path: Path, head_epoch: int) -> None:
    staging_root = staging_parent / "payload"
    file_list_path = staging_parent / ".release-file-list"
    top_level_names = sorted(path.name for path in staging_root.iterdir())
    if not top_level_names:
        raise ReleaseError("Release staging is empty.")
    file_list_path.write_bytes(b"\0".join(name.encode("utf-8") for name in top_level_names) + b"\0")
    tar_command = [
        "tar",
        "--sort=name",
        f"--mtime=@{head_epoch}",
        "--owner=0",
        "--group=0",
        "--numeric-owner",
        "--format=posix",
        "--pax-option=delete=atime,delete=ctime",
        f"--directory={staging_root}",
        "--verbatim-files-from",
        "--null",
        f"--files-from={file_list_path}",
        "-cf",
        "-",
    ]
    archive_environment = os.environ.copy()
    archive_environment["LC_ALL"] = "C"
    archive_environment.pop("TAR_OPTIONS", None)
    archive_environment.pop("GZIP", None)
    archive_environment.pop("GZIP_OPT", None)
    archive_environment.pop("GZIP_OPTIONS", None)
    with archive_path.open("xb") as output:
        try:
            tar_process = subprocess.Popen(
                tar_command,
                cwd=staging_parent,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=archive_environment,
            )
            assert tar_process.stdout is not None
            gzip_process = subprocess.Popen(
                ["gzip", "-n", "-9"],
                stdin=tar_process.stdout,
                stdout=output,
                stderr=subprocess.PIPE,
                env=archive_environment,
            )
        except FileNotFoundError as exc:
            raise ReleaseError(f"Required command is unavailable: {exc.filename}") from exc
        tar_process.stdout.close()
        gzip_stderr = gzip_process.communicate()[1].decode("utf-8", errors="replace")
        tar_stderr = tar_process.stderr.read().decode("utf-8", errors="replace") if tar_process.stderr else ""
        tar_return_code = tar_process.wait()
        if tar_return_code:
            raise ReleaseError(f"tar failed: {tar_stderr.strip()}")
        if gzip_process.returncode:
            raise ReleaseError(f"gzip failed: {gzip_stderr.strip()}")
        output.flush()
        os.fsync(output.fileno())
        os.fchmod(output.fileno(), 0o644)


def _validate_archive(
    archive_path: Path,
    manifest: dict[str, object],
    *,
    head_epoch: int,
) -> None:
    manifest_entries = manifest["files"]
    if not isinstance(manifest_entries, list):
        raise ReleaseError("Release manifest files must be a list.")
    manifest_paths: list[str] = []
    seen_manifest_paths: set[str] = set()
    for entry in manifest_entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ReleaseError("Release manifest contains an invalid file entry.")
        path = entry["path"]
        _validated_relative(path)
        if path == MANIFEST_NAME:
            raise ReleaseError(f"Release manifest cannot list its generated path: {MANIFEST_NAME}")
        if path in seen_manifest_paths:
            raise ReleaseError(f"Release manifest contains a duplicate path: {path}")
        seen_manifest_paths.add(path)
        manifest_paths.append(path)
    expected_relative_files = set(manifest_paths) | {MANIFEST_NAME}
    expected_files = expected_relative_files
    expected_directories = _expected_directories(expected_relative_files)

    seen_files: set[str] = set()
    seen_directories: set[str] = set()
    seen_names: set[str] = set()
    archived_manifest: dict[str, object] | None = None
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive.getmembers():
            normalized_name = member.name.rstrip("/")
            _validated_relative(normalized_name)
            if normalized_name in seen_names:
                raise ReleaseError(f"Archive contains a duplicate member: {normalized_name}")
            seen_names.add(normalized_name)
            if member.uid != 0 or member.gid != 0 or int(member.mtime) != head_epoch:
                raise ReleaseError(f"Archive metadata is not normalized: {member.name}")
            if member.isdir():
                if member.mode & 0o7777 != 0o755:
                    raise ReleaseError(f"Archive directory mode is not 0755: {member.name}")
                seen_directories.add(normalized_name)
                continue
            if not member.isfile():
                raise ReleaseError(f"Archive contains a link or special member: {member.name}")
            if member.mode & 0o7777 not in {0o644, 0o755}:
                raise ReleaseError(f"Archive file mode is not 0644 or 0755: {member.name}")
            seen_files.add(normalized_name)
            if normalized_name == MANIFEST_NAME:
                source = archive.extractfile(member)
                if source is None:
                    raise ReleaseError("Archive manifest cannot be read.")
                try:
                    archived_manifest = json.load(source)
                except json.JSONDecodeError as exc:
                    raise ReleaseError("Archive manifest is invalid JSON.") from exc

    if seen_files != expected_files:
        missing = sorted(expected_files - seen_files)
        extra = sorted(seen_files - expected_files)
        raise ReleaseError(f"Archive file set differs from manifest (missing={missing}, extra={extra}).")
    if seen_directories != expected_directories:
        missing = sorted(expected_directories - seen_directories)
        extra = sorted(seen_directories - expected_directories)
        raise ReleaseError(f"Archive directory set is not exact (missing={missing}, extra={extra}).")
    if archived_manifest != manifest:
        raise ReleaseError("Archived manifest differs from the verified staging manifest.")

    entries_by_path = {
        entry["path"]: entry
        for entry in manifest_entries
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for relative, entry in entries_by_path.items():
            member = archive.getmember(relative)
            source = archive.extractfile(member)
            if source is None:
                raise ReleaseError(f"Unable to verify archived file: {relative}")
            digest = hashlib.sha256()
            size = 0
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
            if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
                raise ReleaseError(f"Archived file does not match manifest: {relative}")


def _prepare_release_directory(root: Path) -> Path:
    release_dir = root / "release"
    try:
        metadata = release_dir.lstat()
    except FileNotFoundError:
        try:
            release_dir.mkdir(mode=0o700)
        except FileExistsError:
            pass
        metadata = release_dir.lstat()
    except OSError as exc:
        raise ReleaseError("Unable to inspect release output directory.") from exc

    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ReleaseError("Release output path must be a real directory, not a symlink or special node.")
    try:
        resolved = release_dir.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ReleaseError("Unable to resolve release output directory.") from exc
    if resolved != release_dir.absolute():
        raise ReleaseError("Release output directory must not traverse symlinks.")
    return release_dir


@contextmanager
def _release_lock(release_dir: Path) -> Iterator[None]:
    lock_path = release_dir / ".package-release.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ReleaseError("Unable to safely open the release packaging lock.") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ReleaseError("Release packaging lock is not a regular file.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReleaseError("Another release packaging process is already running.") from exc
        except OSError as exc:
            raise ReleaseError("Unable to acquire the release packaging lock.") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _write_checksum_exclusive(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ReleaseError("Unable to safely create temporary release checksum.") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", closefd=False) as output:
            output.write(content)
            output.flush()
            os.fsync(descriptor)
        os.fchmod(descriptor, 0o644)
    finally:
        os.close(descriptor)


def _hold_test_lock_if_requested(root: Path, source_commit: str) -> None:
    raw_seconds = os.environ.get("NEXPOLY_RELEASE_TEST_HOLD_LOCK_SECONDS", "")
    if not raw_seconds:
        return
    if _git_object_type(root, source_commit, ".nexpoly-release-test-fixture") != "blob":
        raise ReleaseError("The release lock hold hook is restricted to committed test fixtures.")
    try:
        seconds = float(raw_seconds)
    except ValueError as exc:
        raise ReleaseError("NEXPOLY_RELEASE_TEST_HOLD_LOCK_SECONDS must be numeric.") from exc
    if not 0 < seconds <= 5:
        raise ReleaseError("NEXPOLY_RELEASE_TEST_HOLD_LOCK_SECONDS must be greater than 0 and at most 5.")
    time.sleep(seconds)


def _existing_regular_file(path: Path, description: str) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ReleaseError(f"Unable to inspect existing {description}.") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ReleaseError(f"Existing {description} must be a real regular file.")
    return True


def _read_small_file_no_follow(path: Path, description: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReleaseError(f"Unable to safely read existing {description}.") from exc
    try:
        content = os.read(descriptor, 4097)
        if len(content) > 4096:
            raise ReleaseError(f"Existing {description} is unexpectedly large.")
        return content.decode("ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseError(f"Existing {description} is invalid.") from exc
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_release_pair(
    *,
    temporary_archive: Path,
    temporary_checksum: Path,
    final_archive: Path,
    final_checksum: Path,
    archive_digest: str,
) -> None:
    expected_checksum = f"{archive_digest}  {final_archive.name}\n"
    archive_exists = _existing_regular_file(final_archive, "release archive")
    checksum_exists = _existing_regular_file(final_checksum, "release checksum")

    if archive_exists and _sha256_file(final_archive) != archive_digest:
        raise ReleaseError("Existing release archive conflicts with the deterministic candidate.")
    if checksum_exists and _read_small_file_no_follow(final_checksum, "release checksum") != expected_checksum:
        raise ReleaseError("Existing release checksum conflicts with the deterministic candidate.")

    if archive_exists and checksum_exists:
        return
    if archive_exists:
        os.replace(temporary_checksum, final_checksum)
        _fsync_directory(final_archive.parent)
        return
    if checksum_exists:
        os.replace(temporary_archive, final_archive)
        _fsync_directory(final_archive.parent)
        return

    created_archive = False
    created_checksum = False
    try:
        os.replace(temporary_archive, final_archive)
        created_archive = True
        os.replace(temporary_checksum, final_checksum)
        created_checksum = True
        _fsync_directory(final_archive.parent)
    except BaseException:
        if created_checksum:
            final_checksum.unlink(missing_ok=True)
        if created_archive:
            final_archive.unlink(missing_ok=True)
        raise


def build_release(root: Path, source_commit: str, include_data: bool) -> tuple[Path, Path]:
    commit, tree, head_epoch = verify_clean_head(root, source_commit)
    release_dir = _prepare_release_directory(root)

    with _release_lock(release_dir):
        _hold_test_lock_if_requested(root, commit)
        secret_needles = _local_secret_needles(root)
        model_roots = _allowed_roots(root, "RELEASE_ALLOWED_MODEL_ROOTS", ("model",))
        data_roots = (
            _allowed_roots(
                root,
                "RELEASE_ALLOWED_DATA_ROOTS",
                ("database", "backend/data"),
            )
            if include_data
            else ()
        )

        with tempfile.TemporaryDirectory(prefix="nexpoly-release-") as temporary_directory:
            temporary_root = Path(temporary_directory)
            staging_root = temporary_root / "payload"
            staging_root.mkdir()
            _extract_git_archive(root, commit, staging_root)
            model_assets = _load_release_model_manifest(staging_root)

            for required_path in REQUIRED_SOURCE_PATHS:
                path = staging_root / required_path
                if not path.is_file():
                    raise ReleaseError(f"Missing tracked deployment source file: {required_path}")

            _remove_path(staging_root / "database")
            _remove_path(staging_root / "backend" / "data")

            asset_categories: dict[str, str] = {}
            for asset in model_assets:
                copied_paths = _materialize_asset(
                    repo_root=root,
                    source_commit=commit,
                    staging_root=staging_root,
                    raw_path=asset["path"],
                    kind=asset["kind"],
                    allowed_roots=model_roots,
                )
                for copied_path in copied_paths:
                    asset_categories[copied_path.relative_to(staging_root).as_posix()] = asset["category"]

            if include_data:
                for data_path in DATA_ASSETS:
                    copied_paths = _materialize_asset(
                        repo_root=root,
                        source_commit=commit,
                        staging_root=staging_root,
                        raw_path=data_path,
                        kind="file",
                        allowed_roots=data_roots,
                    )
                    asset_categories[copied_paths[0].relative_to(staging_root).as_posix()] = "data"

            manifest = _write_manifest(
                staging_root,
                commit=commit,
                tree=tree,
                include_data=include_data,
                asset_categories=asset_categories,
                secret_needles=secret_needles,
            )
            manifest_digest = _sha256_file(staging_root / MANIFEST_NAME)
            data_mode = 1 if include_data else 0
            bundle_name = (
                f"nexpoly-release-{commit[:12]}-data{data_mode}-{manifest_digest[:16]}.tar.gz"
            )
            final_archive = release_dir / bundle_name
            final_checksum = release_dir / f"{bundle_name}.sha256"

            with tempfile.TemporaryDirectory(
                prefix=".nexpoly-release-output-",
                dir=release_dir,
            ) as output_directory:
                output_root = Path(output_directory)
                output_root.chmod(0o700)
                temporary_archive = output_root / bundle_name
                temporary_checksum = output_root / f"{bundle_name}.sha256"
                _create_archive(temporary_root, temporary_archive, head_epoch)
                _validate_archive(temporary_archive, manifest, head_epoch=head_epoch)
                archive_digest = _sha256_file(temporary_archive)
                _write_checksum_exclusive(
                    temporary_checksum,
                    f"{archive_digest}  {bundle_name}\n",
                )
                _publish_release_pair(
                    temporary_archive=temporary_archive,
                    temporary_checksum=temporary_checksum,
                    final_archive=final_archive,
                    final_checksum=final_checksum,
                    archive_digest=archive_digest,
                )

    return final_archive, final_checksum


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--include-data", choices=["0", "1"], required=True)
    args = parser.parse_args()

    try:
        root = args.root.resolve(strict=True)
        archive, checksum = build_release(root, args.commit, args.include_data == "1")
    except (OSError, ReleaseError) as exc:
        print(f"[nexpoly-release] {exc}", file=sys.stderr)
        return 1

    print(f"Created {archive}")
    print(f"Created {checksum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
