#!/usr/bin/env python3
"""Plan or create the first immutable, content-addressed asset release."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any


ASSET_STORE = Path("/data/lzq/nexpoly-assets")
PREDECESSOR_ASSET_DIGEST = (
    "sha256:ad19a4f1cb954b3ee6999b7157c798fd887ecd3fd7ae12e40ac20a97637575e2"
)
UNCHANGED_ASSET_TREES = ("model", "database", "backend-data")
ASSET_KEYS = (*UNCHANGED_ASSET_TREES, "byteff2")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
BYTEFF2_GIT_SOURCE = "https://github.com/ByteDance-Seed/byteff2.git"
BYTEFF2_GIT_REVISION = "8f2813407ba5fbecfb5ec5c69e10b124c5b5bdc2"
BYTEFF2_RUNTIME_REQUIRED_FILES = (
    (
        "submodules/bytemol/bytemol/toolkit/infer_molecule/bond_length_ref.csv",
        "caa78ff02c7e65fb0c8bcf240382fa8d90b0dfea85a4d9888c96eab04cc4a40e",
    ),
)
BYTEFF2_AUDITED_OVERLAY_SOURCE = "https://huggingface.co/ByteDance-Seed/byteff2"
BYTEFF2_AUDITED_OVERLAY_REVISION = "b92ac49058c113625012c1f50d98a7bf9cf4e46e"
BYTEFF2_AUDITED_OVERLAY_FILES = (
    (
        "byteff2/trained_models/fftrainer_config_in_use.yaml",
        986,
        "8245a5c6ad9b4aa9d180c8bb24d6f05c210f1724ffae93aec0ef4f88e5fd7ea3",
    ),
    (
        "byteff2/trained_models/optimal.pt",
        111_892_932,
        "ae47a6e6860b563908a2e0a83d4a3f6adc1c36b48f544e2241d24066d43d539c",
    ),
)
BYTEFF2_AUDITED_OVERLAY_SOURCE_PATHS = {
    "byteff2/trained_models/fftrainer_config_in_use.yaml": (
        "trained_models/fftrainer_config_in_use.yaml"
    ),
    "byteff2/trained_models/optimal.pt": "trained_models/optimal.pt",
}
BYTEFF2_MATERIALIZED_SYMLINKS = {
    "bytemol": "submodules/bytemol/bytemol/",
}


class AssetError(RuntimeError):
    pass


def inspect_tree(
    root: Path,
    *,
    hash_files: bool,
    ignore_git_metadata: bool = False,
) -> list[dict[str, Any]]:
    if not root.is_dir() or root.is_symlink():
        raise AssetError(f"asset source must be a real directory: {root}")
    records: list[dict[str, Any]] = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        if ignore_git_metadata:
            directories[:] = sorted(name for name in directories if name != ".git")
            files = [name for name in files if name != ".git"]
        else:
            directories.sort()
        for name in sorted(directories + files):
            path = current_path / name
            metadata = path.lstat()
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(metadata.st_mode):
                raise AssetError(f"asset trees must not contain symlinks: {root.name}/{relative}")
            if stat.S_ISDIR(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise AssetError(f"asset trees must contain only files/directories: {root.name}/{relative}")
            record: dict[str, Any] = {"path": relative, "size": metadata.st_size}
            if hash_files:
                digest = hashlib.sha256()
                flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                try:
                    descriptor = os.open(path, flags)
                except OSError as exc:
                    raise AssetError(f"cannot open asset file safely: {root.name}/{relative}") from exc
                try:
                    before = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or (
                            before.st_dev,
                            before.st_ino,
                            before.st_mode,
                            before.st_size,
                            before.st_mtime_ns,
                        )
                        != (
                            metadata.st_dev,
                            metadata.st_ino,
                            metadata.st_mode,
                            metadata.st_size,
                            metadata.st_mtime_ns,
                        )
                    ):
                        raise AssetError(f"asset file changed before hashing: {root.name}/{relative}")
                    total = 0
                    while True:
                        chunk = os.read(descriptor, 1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        digest.update(chunk)
                    after = os.fstat(descriptor)
                    identity_before = (
                        before.st_dev,
                        before.st_ino,
                        before.st_mode,
                        before.st_size,
                        before.st_mtime_ns,
                    )
                    identity_after = (
                        after.st_dev,
                        after.st_ino,
                        after.st_mode,
                        after.st_size,
                        after.st_mtime_ns,
                    )
                    if identity_before != identity_after or total != after.st_size:
                        raise AssetError(f"asset file changed while hashing: {root.name}/{relative}")
                except OSError as exc:
                    raise AssetError(f"cannot hash asset file safely: {root.name}/{relative}") from exc
                finally:
                    os.close(descriptor)
                record["sha256"] = digest.hexdigest()
            records.append(record)
    return records


def git_output(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def _git_index_entries(root: Path, relative: str) -> list[bytes]:
    output = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--stage",
            "-z",
            "--",
            f":(literal){relative}",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    return [entry for entry in output.split(b"\0") if entry]


def _require_git_tracked_regular_file(root: Path, relative: str) -> None:
    """Require *relative* to be a stage-zero regular file in *root*'s index."""
    entries = _git_index_entries(root, relative)
    if len(entries) != 1:
        raise AssetError(f"required ByteFF2 runtime asset must be Git-tracked: {relative}")
    try:
        metadata, raw_path = entries[0].split(b"\t", 1)
        mode, _sha, stage = metadata.split(b" ", 2)
        indexed_path = raw_path.decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise AssetError(
            f"cannot parse required ByteFF2 runtime asset index entry: {relative}"
        ) from exc
    if indexed_path != relative or stage != b"0" or mode not in {b"100644", b"100755"}:
        raise AssetError(
            f"required ByteFF2 runtime asset must be tracked as a regular file: {relative}"
        )


def _require_git_ignored_overlay(root: Path, relative: str) -> None:
    """Require an audited overlay to be ignored and absent from the Git index."""
    if _git_index_entries(root, relative):
        raise AssetError(f"audited ByteFF2 overlay must not be Git-tracked: {relative}")
    ignored = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "--quiet", "--", relative],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if ignored.returncode == 1:
        raise AssetError(f"audited ByteFF2 overlay must be explicitly ignored: {relative}")
    if ignored.returncode != 0:
        raise AssetError(f"cannot verify audited ByteFF2 overlay ignore state: {relative}")


def git_status_entries(root: Path) -> list[str]:
    output = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
            "--ignore-submodules=all",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    try:
        return [entry.decode("utf-8") for entry in output.split(b"\0") if entry]
    except UnicodeDecodeError as exc:
        raise AssetError(f"cannot parse Git status entries in {root}") from exc


def unexpected_git_status_entries(
    root: Path,
    *,
    allowed_ignored: tuple[str, ...] = (),
) -> list[str]:
    allowed = {f"!! {relative}" for relative in allowed_ignored}
    return [entry for entry in git_status_entries(root) if entry not in allowed]


def _inspect_required_regular_file(root: Path, relative: str) -> dict[str, Any]:
    """Hash one required file without following it or any parent symlink."""
    relative_path = Path(relative)
    if relative_path.is_absolute() or not relative_path.parts or ".." in relative_path.parts:
        raise AssetError(f"invalid required ByteFF2 runtime asset path: {relative}")

    current = root
    for component in relative_path.parts[:-1]:
        current /= component
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise AssetError(f"required ByteFF2 runtime asset is missing: {relative}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise AssetError(
                f"required ByteFF2 runtime asset must not traverse a symlink: {relative}"
            )

    path = root / relative_path
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AssetError(f"required ByteFF2 runtime asset is missing: {relative}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AssetError(f"required ByteFF2 runtime asset must be a regular file: {relative}")

    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AssetError(f"cannot open required ByteFF2 runtime asset safely: {relative}") from exc
    try:
        before = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
        )
        identity_from_path = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_size,
            metadata.st_mtime_ns,
        )
        if not stat.S_ISREG(before.st_mode) or identity_before != identity_from_path:
            raise AssetError(f"required ByteFF2 runtime asset changed before hashing: {relative}")
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
        )
        if identity_after != identity_before or total != after.st_size:
            raise AssetError(f"required ByteFF2 runtime asset changed while hashing: {relative}")
    except OSError as exc:
        raise AssetError(f"cannot hash required ByteFF2 runtime asset safely: {relative}") from exc
    finally:
        os.close(descriptor)
    return {"path": relative, "size": total, "sha256": digest.hexdigest()}


def inspect_required_byteff2_runtime_files(
    root: Path,
    *,
    require_git_tracking: bool,
) -> list[dict[str, Any]]:
    """Validate the audited files required to import and run ByteFF2 protocols."""
    records: list[dict[str, Any]] = []
    for relative, expected_digest in BYTEFF2_RUNTIME_REQUIRED_FILES:
        if require_git_tracking:
            _require_git_tracked_regular_file(root, relative)
        record = _inspect_required_regular_file(root, relative)
        if record["sha256"] != expected_digest:
            raise AssetError(f"required ByteFF2 runtime asset digest mismatch: {relative}")
        records.append(record)
    return records


def inspect_byteff2_audited_overlays(
    root: Path,
    *,
    require_git_ignored: bool,
) -> list[dict[str, Any]]:
    """Validate fixed Hugging Face files overlaid onto the ByteFF2 checkout."""
    records: list[dict[str, Any]] = []
    for relative, expected_size, expected_digest in BYTEFF2_AUDITED_OVERLAY_FILES:
        if require_git_ignored:
            _require_git_ignored_overlay(root, relative)
        record = _inspect_required_regular_file(root, relative)
        if record["size"] != expected_size:
            raise AssetError(f"audited ByteFF2 overlay size mismatch: {relative}")
        if record["sha256"] != expected_digest:
            raise AssetError(f"audited ByteFF2 overlay digest mismatch: {relative}")
        records.append(record)
    return records


def validate_byteff2_runtime_manifest(records: list[dict[str, Any]]) -> None:
    """Require the manifest inventory to preserve every audited runtime file."""
    records_by_path: dict[str, dict[str, Any]] = {}
    required_files = tuple(
        (relative, None, digest) for relative, digest in BYTEFF2_RUNTIME_REQUIRED_FILES
    ) + BYTEFF2_AUDITED_OVERLAY_FILES
    required_paths = {relative for relative, _size, _digest in required_files}
    for record in records:
        path = record.get("path")
        if isinstance(path, str) and path in required_paths:
            if path in records_by_path:
                raise AssetError(
                    f"duplicate required ByteFF2 runtime asset manifest record: {path}"
                )
            records_by_path[path] = record
    for relative, expected_size, expected_digest in required_files:
        record = records_by_path.get(relative)
        if record is None:
            raise AssetError(f"required ByteFF2 runtime asset missing from manifest: {relative}")
        if expected_size is not None and record.get("size") != expected_size:
            raise AssetError(f"required ByteFF2 runtime asset manifest size mismatch: {relative}")
        if record.get("sha256") != expected_digest:
            raise AssetError(f"required ByteFF2 runtime asset manifest digest mismatch: {relative}")


def byteff2_audited_overlays_manifest() -> dict[str, Any]:
    if not FULL_SHA.fullmatch(BYTEFF2_AUDITED_OVERLAY_REVISION):
        raise AssetError("ByteFF2 audited overlay revision must be a full commit SHA")
    return {
        "source": BYTEFF2_AUDITED_OVERLAY_SOURCE,
        "revision": BYTEFF2_AUDITED_OVERLAY_REVISION,
        "files": [
            {
                "source_path": BYTEFF2_AUDITED_OVERLAY_SOURCE_PATHS[relative],
                "path": relative,
                "size": size,
                "sha256": digest,
            }
            for relative, size, digest in BYTEFF2_AUDITED_OVERLAY_FILES
        ],
    }


def byteff2_source_manifest(revision: str) -> dict[str, str]:
    if not FULL_SHA.fullmatch(revision):
        raise AssetError("ByteFF2 source revision must be a full commit SHA")
    return {
        "source": BYTEFF2_GIT_SOURCE,
        "revision": revision,
    }


def require_approved_byteff2_revision(revision: str) -> None:
    if revision != BYTEFF2_GIT_REVISION:
        raise AssetError(
            "ByteFF2 checkout must use the approved official v1.0.0 revision"
        )


def indexed_submodules(root: Path) -> list[tuple[str, str]]:
    """Return the path and pinned commit for each direct gitlink in *root*."""
    output = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--stage", "-z"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    result: list[tuple[str, str]] = []
    for raw_entry in output.split(b"\0"):
        if not raw_entry:
            continue
        try:
            metadata, raw_path = raw_entry.split(b"\t", 1)
            mode, raw_sha, stage = metadata.split(b" ", 2)
            path = raw_path.decode("utf-8")
            sha = raw_sha.decode("ascii")
        except (UnicodeDecodeError, ValueError) as exc:
            raise AssetError(f"cannot parse git index entry in {root}") from exc
        if mode == b"160000":
            if stage != b"0" or not FULL_SHA.fullmatch(sha):
                raise AssetError(f"invalid submodule gitlink in {root}: {path}")
            result.append((path, sha))
    return sorted(result)


def inspect_byteff2_checkout(root: Path) -> tuple[str, dict[str, str]]:
    """Validate a clean ByteFF2 checkout and every recursively pinned submodule."""
    root = root.resolve()
    if not root.is_dir() or root.is_symlink():
        raise AssetError(f"ByteFF2 root must be a real directory: {root}")
    top_level = Path(git_output(root, "rev-parse", "--show-toplevel").strip()).resolve()
    if top_level != root:
        raise AssetError(f"ByteFF2 root must be the Git top-level directory: {root}")

    commits: dict[str, str] = {}
    overlay_paths = tuple(relative for relative, _size, _digest in BYTEFF2_AUDITED_OVERLAY_FILES)

    def inspect_repository(repository: Path, relative: str) -> str:
        commit = git_output(repository, "rev-parse", "--verify", "HEAD^{commit}").strip()
        if not FULL_SHA.fullmatch(commit):
            raise AssetError(f"repository does not resolve to a full commit SHA: {repository}")
        if not relative:
            inspect_required_byteff2_runtime_files(root, require_git_tracking=True)
            inspect_byteff2_audited_overlays(root, require_git_ignored=True)
        dirty = unexpected_git_status_entries(
            repository,
            allowed_ignored=overlay_paths if not relative else (),
        )
        if dirty:
            label = "ByteFF2 checkout" if not relative else f"ByteFF2 submodule {relative}"
            raise AssetError(f"{label} must be clean")

        index_flags = git_output(repository, "ls-files", "-v", "-z")
        if any(entry and (entry[0] == "S" or entry[0].islower()) for entry in index_flags.split("\0")):
            label = "ByteFF2 checkout" if not relative else f"ByteFF2 submodule {relative}"
            raise AssetError(f"{label} contains hidden index state")

        direct_submodules = indexed_submodules(repository)
        for child_path, pinned_commit in direct_submodules:
            child_relative = f"{relative}/{child_path}" if relative else child_path
            child = repository / child_path
            if child.is_symlink() or not child.is_dir():
                raise AssetError(f"ByteFF2 submodule is not initialized: {child_relative}")
            if not (child / ".git").exists():
                raise AssetError(f"ByteFF2 submodule is not initialized: {child_relative}")
            resolved_child = child.resolve()
            try:
                resolved_child.relative_to(root)
            except ValueError as exc:
                raise AssetError(f"ByteFF2 submodule escapes checkout: {child_relative}") from exc
            child_top_level = Path(git_output(child, "rev-parse", "--show-toplevel").strip()).resolve()
            if child_top_level != resolved_child:
                raise AssetError(f"ByteFF2 submodule is not a Git top-level directory: {child_relative}")
            actual_commit = inspect_repository(child, child_relative)
            if actual_commit != pinned_commit:
                raise AssetError(
                    f"ByteFF2 submodule commit does not match parent gitlink: {child_relative}"
                )
            commits[child_relative] = actual_commit
        if not relative:
            inspect_required_byteff2_runtime_files(root, require_git_tracking=True)
            inspect_byteff2_audited_overlays(root, require_git_ignored=True)
        final_commit = git_output(repository, "rev-parse", "--verify", "HEAD^{commit}").strip()
        final_dirty = unexpected_git_status_entries(
            repository,
            allowed_ignored=overlay_paths if not relative else (),
        )
        if final_commit != commit or final_dirty or indexed_submodules(repository) != direct_submodules:
            label = "ByteFF2 checkout" if not relative else f"ByteFF2 submodule {relative}"
            raise AssetError(f"{label} changed while it was inspected")
        return commit

    return inspect_repository(root, ""), dict(sorted(commits.items()))


def remove_git_metadata(root: Path) -> None:
    """Remove root and nested worktree metadata after copying a clean checkout."""
    candidates: list[Path] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        if ".git" in directories:
            directories.remove(".git")
            candidates.append(current_path / ".git")
        if ".git" in files:
            candidates.append(current_path / ".git")
    for path in sorted(candidates, key=lambda candidate: len(candidate.parts), reverse=True):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def copy_tree(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise AssetError(f"asset copy destination already exists: {destination}")
    subprocess.run(
        [
            "cp",
            "--archive",
            "--reflink=auto",
            "--no-preserve=ownership",
            "--",
            str(source),
            str(destination),
        ],
        check=True,
    )


def copy_verified_tree(
    source: Path,
    destination: Path,
    *,
    ignore_git_metadata: bool = False,
) -> list[dict[str, Any]]:
    """Copy one tree only if its complete file fingerprint stays unchanged."""
    source_before = inspect_tree(
        source,
        hash_files=True,
        ignore_git_metadata=ignore_git_metadata,
    )
    copy_tree(source, destination)
    source_after = inspect_tree(
        source,
        hash_files=True,
        ignore_git_metadata=ignore_git_metadata,
    )
    if source_after != source_before:
        raise AssetError(f"asset source changed while it was copied: {source}")
    copied = inspect_tree(
        destination,
        hash_files=True,
        ignore_git_metadata=ignore_git_metadata,
    )
    if copied != source_before:
        raise AssetError(f"copied asset tree does not match its source fingerprint: {source}")
    return copied


def copy_verified_byteff2(
    source: Path,
    destination: Path,
    *,
    expected_commit: str,
    expected_submodules: dict[str, str],
) -> None:
    """Copy a clean checkout and prove it kept the reviewed recursive identity."""
    before_commit, before_submodules = inspect_byteff2_checkout(source)
    if before_commit != expected_commit or before_submodules != expected_submodules:
        raise AssetError("ByteFF2 checkout identity changed before copying")
    for relative, expected_target in BYTEFF2_MATERIALIZED_SYMLINKS.items():
        entries = _git_index_entries(source, relative)
        if len(entries) != 1:
            raise AssetError(f"audited ByteFF2 symlink is not tracked: {relative}")
        metadata, raw_path = entries[0].split(b"\t", 1)
        mode, _sha, stage = metadata.split(b" ", 2)
        path = source / relative
        if (
            raw_path.decode("utf-8") != relative
            or mode != b"120000"
            or stage != b"0"
            or not path.is_symlink()
            or os.readlink(path) != expected_target
        ):
            raise AssetError(f"audited ByteFF2 symlink identity differs: {relative}")
        target = (path.parent / expected_target).resolve()
        try:
            target.relative_to(source.resolve())
        except ValueError as exc:
            raise AssetError(f"audited ByteFF2 symlink escapes checkout: {relative}") from exc
        if not target.is_dir() or target.is_symlink():
            raise AssetError(f"audited ByteFF2 symlink target is unsafe: {relative}")
    copy_tree(source, destination)
    after_commit, after_submodules = inspect_byteff2_checkout(source)
    if after_commit != expected_commit or after_submodules != expected_submodules:
        raise AssetError("ByteFF2 checkout identity changed while copying")
    remove_git_metadata(destination)
    for relative, expected_target in BYTEFF2_MATERIALIZED_SYMLINKS.items():
        link = destination / relative
        if not link.is_symlink() or os.readlink(link) != expected_target:
            raise AssetError(f"copied ByteFF2 symlink identity differs: {relative}")
        target = (link.parent / expected_target).resolve()
        try:
            target.relative_to(destination.resolve())
        except ValueError as exc:
            raise AssetError(f"copied ByteFF2 symlink escapes release: {relative}") from exc
        link.unlink()
        copy_tree(target, link)
    (destination / "BYTEFF2-COMMIT").write_text(expected_commit + "\n", encoding="ascii")
    inspect_tree(destination, hash_files=False)
    inspect_required_byteff2_runtime_files(destination, require_git_tracking=False)
    inspect_byteff2_audited_overlays(destination, require_git_ignored=False)


def build_manifest(
    assets: dict[str, list[dict[str, Any]]],
    *,
    byteff2_commit: str,
    byteff2_submodules: dict[str, str],
    predecessor_digest: str,
    predecessor_tree_digests: dict[str, str],
) -> dict[str, Any]:
    if not FULL_SHA.fullmatch(byteff2_commit):
        raise AssetError("ByteFF2 root does not resolve to a full commit SHA")
    if any(not FULL_SHA.fullmatch(commit) for commit in byteff2_submodules.values()):
        raise AssetError("ByteFF2 submodule does not resolve to a full commit SHA")
    byteff2_assets = assets.get("byteff2")
    if not isinstance(byteff2_assets, list):
        raise AssetError("asset manifest must contain a ByteFF2 inventory")
    validate_byteff2_runtime_manifest(byteff2_assets)
    if predecessor_digest != PREDECESSOR_ASSET_DIGEST:
        raise AssetError("asset manifest predecessor is not the approved schema-v1 release")
    if set(predecessor_tree_digests) != set(UNCHANGED_ASSET_TREES):
        raise AssetError("asset manifest predecessor tree evidence is incomplete")
    return {
        "schema_version": 2,
        "predecessor_asset_digest": predecessor_digest,
        "changed_asset_trees": ["byteff2"],
        "unchanged_asset_tree_digests": dict(sorted(predecessor_tree_digests.items())),
        "byteff2_commit": byteff2_commit,
        "byteff2_submodules": dict(sorted(byteff2_submodules.items())),
        "byteff2_source": byteff2_source_manifest(byteff2_commit),
        "byteff2_audited_overlays": byteff2_audited_overlays_manifest(),
        "assets": assets,
    }


def canonical(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def require_sha256_digest(value: str, *, label: str) -> str:
    match = SHA256_DIGEST.fullmatch(value)
    if match is None:
        raise AssetError(f"{label} must be a full sha256 digest")
    return match.group(1)


def tree_inventory_digest(records: list[dict[str, Any]]) -> str:
    return "sha256:" + hashlib.sha256(canonical({"files": records})).hexdigest()


def normalized_inventory(
    records: list[dict[str, Any]],
    *,
    label: str,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    paths: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
            raise AssetError(f"{label} inventory record is invalid")
        path = record.get("path")
        size = record.get("size")
        digest = record.get("sha256")
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or ".." in Path(path).parts
            or path in paths
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise AssetError(f"{label} inventory record is invalid")
        paths.add(path)
        normalized.append({"path": path, "size": size, "sha256": digest})
    return sorted(normalized, key=lambda record: str(record["path"]))


def load_verified_predecessor(
    store: Path,
    predecessor_digest: str,
) -> tuple[Path, dict[str, Any], dict[str, list[dict[str, Any]]], dict[str, str]]:
    digest = require_sha256_digest(
        predecessor_digest,
        label="predecessor asset digest",
    )
    if predecessor_digest != PREDECESSOR_ASSET_DIGEST:
        raise AssetError("only the approved schema-v1 predecessor may be upgraded")
    predecessor = store / "releases" / digest
    if (
        not predecessor.is_dir()
        or predecessor.is_symlink()
        or predecessor.resolve().parent != (store / "releases").resolve()
    ):
        raise AssetError("approved schema-v1 predecessor release is unavailable")
    manifest_path = predecessor / "ASSET-MANIFEST.json"
    try:
        manifest_metadata = manifest_path.lstat()
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AssetError("schema-v1 predecessor manifest is unavailable") from exc
    if (
        not stat.S_ISREG(manifest_metadata.st_mode)
        or manifest_path.is_symlink()
        or hashlib.sha256(manifest_bytes).hexdigest() != digest
        or canonical(manifest) != manifest_bytes
        or not isinstance(manifest, dict)
        or set(manifest)
        != {"schema_version", "byteff2_commit", "byteff2_submodules", "assets"}
        or manifest.get("schema_version") != 1
    ):
        raise AssetError("schema-v1 predecessor manifest identity is invalid")
    manifest_assets = manifest.get("assets")
    if not isinstance(manifest_assets, dict) or set(manifest_assets) != set(ASSET_KEYS):
        raise AssetError("schema-v1 predecessor asset set is invalid")
    root_entries = {entry.name for entry in predecessor.iterdir()}
    if root_entries != set(ASSET_KEYS) | {"ASSET-MANIFEST.json"}:
        raise AssetError("schema-v1 predecessor has unmanifested entries")
    verified: dict[str, list[dict[str, Any]]] = {}
    for tree_name in ASSET_KEYS:
        expected = manifest_assets.get(tree_name)
        if not isinstance(expected, list):
            raise AssetError(f"schema-v1 predecessor inventory is invalid: {tree_name}")
        actual = inspect_tree(predecessor / tree_name, hash_files=True)
        if normalized_inventory(actual, label=tree_name) != normalized_inventory(
            expected,
            label=tree_name,
        ):
            raise AssetError(f"schema-v1 predecessor tree differs from manifest: {tree_name}")
        verified[tree_name] = [dict(record) for record in expected]
    tree_digests = {
        tree_name: tree_inventory_digest(verified[tree_name])
        for tree_name in UNCHANGED_ASSET_TREES
    }
    return predecessor, manifest, verified, tree_digests


def _fsync_path(path: Path, *, directory: bool) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_tree(root: Path) -> None:
    directories: list[Path] = []
    for current, child_directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories.append(current_path)
        for name in files:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise AssetError(f"asset staging contains an unsafe file: {path}")
            _fsync_path(path, directory=False)
        for name in child_directories:
            path = current_path / name
            if path.is_symlink():
                raise AssetError(f"asset staging contains an unsafe directory: {path}")
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        _fsync_path(directory, directory=True)


def make_read_only(root: Path) -> None:
    for current, directories, files in os.walk(root):
        for name in files:
            os.chmod(Path(current) / name, 0o400)
        for name in directories:
            os.chmod(Path(current) / name, 0o500)
    os.chmod(root, 0o500)


def make_private_writable(root: Path) -> None:
    if root.is_symlink():
        raise AssetError(f"refusing to recover symlink staging path: {root}")
    for current, directories, files in os.walk(root):
        os.chmod(current, 0o700)
        for name in directories:
            os.chmod(Path(current) / name, 0o700)
        for name in files:
            os.chmod(Path(current) / name, 0o600)


def validate_existing_release(
    destination: Path,
    *,
    expected_manifest: dict[str, Any],
    expected_digest: str,
) -> None:
    manifest_path = destination / "ASSET-MANIFEST.json"
    try:
        metadata = destination.lstat()
        manifest_metadata = manifest_path.lstat()
        manifest_bytes = manifest_path.read_bytes()
        document = json.loads(manifest_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AssetError("existing asset release cannot be validated") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or destination.is_symlink()
        or metadata.st_mode & 0o222
        or not stat.S_ISREG(manifest_metadata.st_mode)
        or manifest_path.is_symlink()
        or manifest_metadata.st_mode & 0o222
        or hashlib.sha256(manifest_bytes).hexdigest() != expected_digest
        or document != expected_manifest
        or {entry.name for entry in destination.iterdir()}
        != set(ASSET_KEYS) | {"ASSET-MANIFEST.json"}
    ):
        raise AssetError("existing asset release conflicts with the requested release")
    assets = document.get("assets")
    if not isinstance(assets, dict) or set(assets) != set(ASSET_KEYS):
        raise AssetError("existing asset release inventory is invalid")
    for tree_name in ASSET_KEYS:
        if inspect_tree(destination / tree_name, hash_files=True) != assets[tree_name]:
            raise AssetError(f"existing asset release tree is invalid: {tree_name}")


@contextlib.contextmanager
def asset_store_lock(releases: Path):
    lock_path = releases / ".schema-v2-build.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise AssetError("schema-v2 asset build lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AssetError("another schema-v2 asset build is active") from exc
        yield
    finally:
        os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-store", default=str(ASSET_STORE))
    parser.add_argument("--predecessor-digest", default=PREDECESSOR_ASSET_DIGEST)
    parser.add_argument("--byteff2-root", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-asset-store")
    parser.add_argument("--confirm-predecessor-digest")
    parser.add_argument("--confirm-byteff2-root")
    args = parser.parse_args(argv)
    store = Path(args.asset_store).resolve()
    byteff2 = Path(args.byteff2_root).resolve()
    try:
        predecessor, predecessor_manifest, predecessor_assets, tree_digests = (
            load_verified_predecessor(store, args.predecessor_digest)
        )
        byteff2_commit, byteff2_submodules = inspect_byteff2_checkout(byteff2)
        require_approved_byteff2_revision(byteff2_commit)
        byteff2_tracked_entries = [
            entry
            for entry in subprocess.run(
                ["git", "-C", str(byteff2), "ls-files", "-z"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout.split(b"\0")
            if entry
        ]
        summary = {
            tree_name: {
                "files": len(predecessor_assets[tree_name]),
                "bytes": sum(
                    int(record["size"]) for record in predecessor_assets[tree_name]
                ),
                "source": "predecessor",
            }
            for tree_name in UNCHANGED_ASSET_TREES
        }
        summary["byteff2"] = {
            "tracked_entries": len(byteff2_tracked_entries),
            "audited_overlay_bytes": sum(
                size for _path, size, _digest in BYTEFF2_AUDITED_OVERLAY_FILES
            ),
            "materialized_symlinks": dict(BYTEFF2_MATERIALIZED_SYMLINKS),
            "source": "official-clean-checkout",
        }
        plan = {
            "action": "bootstrap-schema-v2-asset-release",
            "apply": args.apply,
            "predecessor_asset_digest": args.predecessor_digest,
            "predecessor_release": str(predecessor),
            "predecessor_byteff2_commit": predecessor_manifest["byteff2_commit"],
            "unchanged_asset_tree_digests": tree_digests,
            "changed_asset_trees": ["byteff2"],
            "byteff2_root": str(byteff2),
            "asset_store": str(store),
            "byteff2_commit": byteff2_commit,
            "byteff2_source": byteff2_source_manifest(byteff2_commit),
            "byteff2_submodules": byteff2_submodules,
            "byteff2_audited_overlays": byteff2_audited_overlays_manifest(),
            "summary": summary,
        }
        if not args.apply:
            print(json.dumps(plan, indent=2, sort_keys=True))
            return 0
        if (
            store != ASSET_STORE
            or args.predecessor_digest != PREDECESSOR_ASSET_DIGEST
            or args.confirm_asset_store != str(ASSET_STORE)
            or args.confirm_predecessor_digest != PREDECESSOR_ASSET_DIGEST
            or args.confirm_byteff2_root != str(byteff2)
        ):
            raise AssetError(
                "apply requires the exact asset store, approved predecessor, "
                "clean ByteFF2 path, and all confirmations"
            )
        releases = store / "releases"
        for path, label in ((store, "asset store"), (releases, "asset releases")):
            metadata = path.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or path.is_symlink()
                or metadata.st_uid not in {0, os.geteuid()}
                or metadata.st_mode & 0o022
            ):
                raise AssetError(f"{label} directory is unsafe")
        with asset_store_lock(releases):
            for stale in releases.glob(".asset-release-v2.*"):
                metadata = stale.lstat()
                if (
                    stale.is_symlink()
                    or not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                ):
                    raise AssetError(f"stale schema-v2 staging path is unsafe: {stale}")
                make_private_writable(stale)
                shutil.rmtree(stale)
            staging = Path(tempfile.mkdtemp(prefix=".asset-release-v2.", dir=releases))
            os.chmod(staging, 0o700)
            try:
                for tree_name in UNCHANGED_ASSET_TREES:
                    copied = copy_verified_tree(
                        predecessor / tree_name,
                        staging / tree_name,
                    )
                    if normalized_inventory(
                        copied,
                        label=tree_name,
                    ) != normalized_inventory(
                        predecessor_assets[tree_name],
                        label=tree_name,
                    ):
                        raise AssetError(
                            f"unchanged predecessor tree drifted while copying: {tree_name}"
                        )
                copy_verified_byteff2(
                    byteff2,
                    staging / "byteff2",
                    expected_commit=byteff2_commit,
                    expected_submodules=byteff2_submodules,
                )
                assets = {
                    tree_name: (
                        [dict(record) for record in predecessor_assets[tree_name]]
                        if tree_name in UNCHANGED_ASSET_TREES
                        else inspect_tree(
                            staging / tree_name,
                            hash_files=True,
                        )
                    )
                    for tree_name in ASSET_KEYS
                }
                for tree_name in UNCHANGED_ASSET_TREES:
                    actual = inspect_tree(staging / tree_name, hash_files=True)
                    if normalized_inventory(
                        actual,
                        label=tree_name,
                    ) != normalized_inventory(
                        predecessor_assets[tree_name],
                        label=tree_name,
                    ):
                        raise AssetError(
                            f"schema-v2 changed an inherited asset tree: {tree_name}"
                        )
                manifest = build_manifest(
                    assets,
                    byteff2_commit=byteff2_commit,
                    byteff2_submodules=byteff2_submodules,
                    predecessor_digest=args.predecessor_digest,
                    predecessor_tree_digests=tree_digests,
                )
                manifest_bytes = canonical(manifest)
                digest = hashlib.sha256(manifest_bytes).hexdigest()
                manifest_path = staging / "ASSET-MANIFEST.json"
                manifest_path.write_bytes(manifest_bytes)
                os.chmod(manifest_path, 0o600)
                fsync_tree(staging)
                make_read_only(staging)
                fsync_tree(staging)
                destination = releases / digest
                if destination.exists() or destination.is_symlink():
                    make_private_writable(staging)
                    shutil.rmtree(staging)
                    validate_existing_release(
                        destination,
                        expected_manifest=manifest,
                        expected_digest=digest,
                    )
                    status = "already-present"
                else:
                    staging.replace(destination)
                    _fsync_path(releases, directory=True)
                    status = "created"
                plan.update(
                    {
                        "status": status,
                        "asset_digest": f"sha256:{digest}",
                        "release_path": str(destination),
                    }
                )
            except Exception:
                if staging.exists():
                    make_private_writable(staging)
                    shutil.rmtree(staging)
                raise
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    except (AssetError, OSError, subprocess.CalledProcessError) as exc:
        print(f"bootstrap-asset-release: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
