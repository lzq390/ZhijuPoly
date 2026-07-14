#!/usr/bin/env python3
"""Plan or create the first immutable, content-addressed asset release."""

from __future__ import annotations

import argparse
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


PRODUCTION_SOURCE = Path("/data/lzq/gith/nexpoly")
ASSET_STORE = Path("/data/lzq/nexpoly-assets")
BYTEFF2_SOURCE = Path("/data/lzq/gith/byteff2")
MAPPINGS = (("model", "model"), ("database", "database"), ("backend/data", "backend-data"))
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


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

    def inspect_repository(repository: Path, relative: str) -> str:
        commit = git_output(repository, "rev-parse", "--verify", "HEAD^{commit}").strip()
        if not FULL_SHA.fullmatch(commit):
            raise AssetError(f"repository does not resolve to a full commit SHA: {repository}")
        dirty = git_output(
            repository,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
            "--ignore-submodules=all",
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
        final_commit = git_output(repository, "rev-parse", "--verify", "HEAD^{commit}").strip()
        final_dirty = git_output(
            repository,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
            "--ignore-submodules=all",
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
    copy_verified_tree(source, destination, ignore_git_metadata=True)
    after_commit, after_submodules = inspect_byteff2_checkout(source)
    if after_commit != expected_commit or after_submodules != expected_submodules:
        raise AssetError("ByteFF2 checkout identity changed while copying")
    remove_git_metadata(destination)
    (destination / "BYTEFF2-COMMIT").write_text(expected_commit + "\n", encoding="ascii")


def build_manifest(
    assets: dict[str, list[dict[str, Any]]],
    *,
    byteff2_commit: str,
    byteff2_submodules: dict[str, str],
) -> dict[str, Any]:
    if not FULL_SHA.fullmatch(byteff2_commit):
        raise AssetError("ByteFF2 root does not resolve to a full commit SHA")
    if any(not FULL_SHA.fullmatch(commit) for commit in byteff2_submodules.values()):
        raise AssetError("ByteFF2 submodule does not resolve to a full commit SHA")
    return {
        "schema_version": 1,
        "byteff2_commit": byteff2_commit,
        "byteff2_submodules": dict(sorted(byteff2_submodules.items())),
        "assets": assets,
    }


def canonical(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def make_read_only(root: Path) -> None:
    for current, directories, files in os.walk(root):
        for name in files:
            os.chmod(Path(current) / name, 0o444)
        for name in directories:
            os.chmod(Path(current) / name, 0o555)
    os.chmod(root, 0o555)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", default=str(PRODUCTION_SOURCE))
    parser.add_argument("--asset-store", default=str(ASSET_STORE))
    parser.add_argument("--byteff2-root", default=str(BYTEFF2_SOURCE))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-source-root")
    parser.add_argument("--confirm-asset-store")
    parser.add_argument("--confirm-byteff2-root")
    args = parser.parse_args(argv)
    source = Path(args.source_root).resolve()
    store = Path(args.asset_store).resolve()
    byteff2 = Path(args.byteff2_root).resolve()
    try:
        summary: dict[str, Any] = {}
        sources = [(source / source_name, target_name) for source_name, target_name in MAPPINGS]
        sources.append((byteff2, "byteff2"))
        byteff2_commit, byteff2_submodules = inspect_byteff2_checkout(byteff2)
        for source_path, target_name in sources:
            records = inspect_tree(
                source_path,
                hash_files=False,
                ignore_git_metadata=target_name == "byteff2",
            )
            summary[target_name] = {"files": len(records), "bytes": sum(record["size"] for record in records)}
        plan = {
            "action": "bootstrap-asset-release", "apply": args.apply,
            "source_root": str(source), "byteff2_root": str(byteff2),
            "asset_store": str(store), "byteff2_commit": byteff2_commit,
            "byteff2_submodules": byteff2_submodules, "summary": summary,
        }
        if not args.apply:
            print(json.dumps(plan, indent=2, sort_keys=True))
            return 0
        if (
            source != PRODUCTION_SOURCE or store != ASSET_STORE or byteff2 != BYTEFF2_SOURCE
            or args.confirm_source_root != str(PRODUCTION_SOURCE)
            or args.confirm_asset_store != str(ASSET_STORE)
            or args.confirm_byteff2_root != str(BYTEFF2_SOURCE)
        ):
            raise AssetError("apply requires exact production, ByteFF2, and asset-store paths plus all confirmations")
        releases = store / "releases"
        releases.mkdir(parents=True, exist_ok=True)
        os.chmod(store, 0o755)
        os.chmod(releases, 0o755)
        staging = Path(tempfile.mkdtemp(prefix=".asset-release.", dir=releases))
        try:
            for source_path, target_name in sources:
                if target_name == "byteff2":
                    copy_verified_byteff2(
                        source_path,
                        staging / target_name,
                        expected_commit=byteff2_commit,
                        expected_submodules=byteff2_submodules,
                    )
                else:
                    copy_verified_tree(source_path, staging / target_name)
            assets: dict[str, Any] = {}
            for _, target_name in sources:
                assets[target_name] = inspect_tree(staging / target_name, hash_files=True)
            manifest = build_manifest(
                assets,
                byteff2_commit=byteff2_commit,
                byteff2_submodules=byteff2_submodules,
            )
            manifest_bytes = canonical(manifest)
            digest = hashlib.sha256(manifest_bytes).hexdigest()
            manifest_path = staging / "ASSET-MANIFEST.json"
            manifest_path.write_bytes(manifest_bytes)
            destination = releases / digest
            if destination.exists():
                raise AssetError(f"asset release already exists: {destination}")
            make_read_only(staging)
            staging.replace(destination)
            plan.update({"status": "created", "asset_digest": f"sha256:{digest}", "release_path": str(destination)})
        except Exception:
            if staging.exists():
                # make_read_only may already have run before an atomic rename failed.
                for current, directories, files in os.walk(staging):
                    os.chmod(current, 0o700)
                    for name in directories:
                        os.chmod(Path(current) / name, 0o700)
                    for name in files:
                        os.chmod(Path(current) / name, 0o600)
                shutil.rmtree(staging)
            raise
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    except (AssetError, OSError, subprocess.CalledProcessError) as exc:
        print(f"bootstrap-asset-release: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
