#!/usr/bin/env python3
"""Strict, stdlib-only validation for immutable Nexpoly asset releases.

The schema-v2 manifest is content addressed, but its digest alone is not a
complete deployment proof.  This module verifies the predecessor release,
every manifested byte, the unchanged-tree relationship, fixed ByteFF2
provenance, and (when requested) the builder commit from a sealed offline Git
bundle.

This module does not update an asset pointer, invoke Docker, or access a
database.  It is safe to stage with both the F and B deployment controllers.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
from typing import Any, Mapping


ASSET_RELEASES_ROOT = Path("/data/lzq/nexpoly-assets/releases")
PREDECESSOR_ASSET_DIGEST = (
    "sha256:ad19a4f1cb954b3ee6999b7157c798fd887ecd3fd7ae12e40ac20a97637575e2"
)
UNCHANGED_ASSET_TREES = ("model", "database", "backend-data")
ASSET_TREES = (*UNCHANGED_ASSET_TREES, "byteff2")
UNCHANGED_ASSET_TREE_DIGESTS = {
    "backend-data": (
        "sha256:1e8dc53143d0676753805ba7a4bf167431e59d92d227ea3aff39e679e43402e1"
    ),
    "database": (
        "sha256:e6bf224836664723124bc7201d14afbdb6dc13cebd289df8b6f86e7a0be0bdcd"
    ),
    "model": (
        "sha256:40e88b7d9d5103ab5db4cd911219dfe37c2ac62319a10824c69c0b36d9556f25"
    ),
}
ASSET_TREE_DIGESTS = {
    **UNCHANGED_ASSET_TREE_DIGESTS,
    "byteff2": (
        "sha256:4aa20c67cb0b7b0dc5d19607b229a1592d96a49f49a336e8eeb385a9edd6d188"
    ),
}
BYTEFF2_GIT_SOURCE = "https://github.com/ByteDance-Seed/byteff2.git"
BYTEFF2_GIT_REVISION = "8f2813407ba5fbecfb5ec5c69e10b124c5b5bdc2"
BYTEFF2_GIT_TREE = "2d9ab46fc185e0e830be53c0ad077100e693ce68"
BYTEFF2_SUBMODULES: dict[str, str] = {}
BYTEFF2_SUBMODULE_TREES: dict[str, str] = {}
BYTEFF2_AUDITED_OVERLAY_SOURCE = "https://huggingface.co/ByteDance-Seed/byteff2"
BYTEFF2_AUDITED_OVERLAY_REVISION = "b92ac49058c113625012c1f50d98a7bf9cf4e46e"
BYTEFF2_AUDITED_OVERLAY_FILES = [
    {
        "source_path": "trained_models/fftrainer_config_in_use.yaml",
        "path": "byteff2/trained_models/fftrainer_config_in_use.yaml",
        "size": 986,
        "sha256": (
            "8245a5c6ad9b4aa9d180c8bb24d6f05c210f1724ffae93aec0ef4f88e5fd7ea3"
        ),
    },
    {
        "source_path": "trained_models/optimal.pt",
        "path": "byteff2/trained_models/optimal.pt",
        "size": 111_892_932,
        "sha256": (
            "ae47a6e6860b563908a2e0a83d4a3f6adc1c36b48f544e2241d24066d43d539c"
        ),
    },
]
BYTEFF2_REQUIRED_RUNTIME_FILES = {
    "submodules/bytemol/bytemol/toolkit/infer_molecule/bond_length_ref.csv": (
        802,
        "caa78ff02c7e65fb0c8bcf240382fa8d90b0dfea85a4d9888c96eab04cc4a40e",
    ),
    **{
        str(record["path"]): (int(record["size"]), str(record["sha256"]))
        for record in BYTEFF2_AUDITED_OVERLAY_FILES
    },
}
BUILD_SOURCE_REPOSITORY = "https://github.com/lzq390/ZhijuPoly.git"
BUILD_SOURCE_SCRIPT = "scripts/bootstrap_asset_release.py"
BUILD_EVIDENCE = {
    "predecessor_manifest_digest": PREDECESSOR_ASSET_DIGEST,
    "predecessor_all_trees_rehashed": [
        "model",
        "database",
        "backend-data",
        "byteff2",
    ],
    "unchanged_trees_byte_identical": [
        "model",
        "database",
        "backend-data",
    ],
    "asset_tree_digest_algorithm": "canonical-manifest-inventory-v1",
    "byteff2_source_verification": "clean-recursive-commit-and-tree",
    "staging_directory_mode": "0700",
    "file_and_directory_fsync": True,
    "publication": "atomic-rename",
    "existing_target": "full-content-revalidation",
}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
V1_FIELDS = {
    "schema_version",
    "byteff2_commit",
    "byteff2_submodules",
    "assets",
}
V2_FIELDS = V1_FIELDS | {
    "predecessor_asset_digest",
    "changed_asset_trees",
    "unchanged_asset_tree_digests",
    "asset_tree_digests",
    "byteff2_tree",
    "byteff2_submodule_trees",
    "byteff2_source",
    "byteff2_audited_overlays",
    "build_provenance",
}


class AssetContractError(RuntimeError):
    """An asset release or its offline provenance proof is invalid."""


def canonical_json_bytes(value: object, *, newline: bool = False) -> bytes:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return payload + (b"\n" if newline else b"")


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise AssetContractError(f"{label} must be a full sha256 digest")
    return value


def require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise AssetContractError(f"{label} must be a full Git SHA")
    return value


def default_contract() -> dict[str, Any]:
    """Return a detached copy of the fixed schema-v2 content contract."""

    return {
        "predecessor_asset_digest": PREDECESSOR_ASSET_DIGEST,
        "unchanged_asset_tree_digests": dict(UNCHANGED_ASSET_TREE_DIGESTS),
        "asset_tree_digests": dict(ASSET_TREE_DIGESTS),
        "byteff2_commit": BYTEFF2_GIT_REVISION,
        "byteff2_tree": BYTEFF2_GIT_TREE,
        "byteff2_submodules": dict(BYTEFF2_SUBMODULES),
        "byteff2_submodule_trees": dict(BYTEFF2_SUBMODULE_TREES),
        "byteff2_source": {
            "source": BYTEFF2_GIT_SOURCE,
            "revision": BYTEFF2_GIT_REVISION,
        },
        "byteff2_audited_overlays": {
            "source": BYTEFF2_AUDITED_OVERLAY_SOURCE,
            "revision": BYTEFF2_AUDITED_OVERLAY_REVISION,
            "files": [dict(record) for record in BYTEFF2_AUDITED_OVERLAY_FILES],
        },
        "byteff2_required_runtime_files": {
            name: [size, digest]
            for name, (size, digest) in BYTEFF2_REQUIRED_RUNTIME_FILES.items()
        },
        "builder_repository": BUILD_SOURCE_REPOSITORY,
        "builder_script": BUILD_SOURCE_SCRIPT,
        "build_evidence": dict(BUILD_EVIDENCE),
    }


def _metadata_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_regular(
    path: Path,
    *,
    expected_uid: int,
    exact_mode: int | None,
    require_read_only: bool,
    maximum_bytes: int | None = None,
) -> tuple[bytes, str, os.stat_result]:
    """Read a regular file once and prove its path/inode did not change."""

    try:
        path_metadata = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise AssetContractError(f"asset file is unavailable: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(path_metadata.st_mode)
            or before.st_uid != expected_uid
            or before.st_nlink != 1
            or _metadata_identity(before) != _metadata_identity(path_metadata)
            or exact_mode is not None
            and stat.S_IMODE(before.st_mode) != exact_mode
            or require_read_only
            and before.st_mode & 0o222
        ):
            raise AssetContractError(f"asset file is unsafe: {path}")
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if maximum_bytes is not None and total > maximum_bytes:
                raise AssetContractError(f"asset file is too large: {path}")
            digest.update(chunk)
            chunks.append(chunk)
        after = os.fstat(descriptor)
        try:
            final_path = path.lstat()
        except OSError as exc:
            raise AssetContractError(f"asset file disappeared: {path}") from exc
        if (
            total != after.st_size
            or _metadata_identity(before) != _metadata_identity(after)
            or _metadata_identity(before) != _metadata_identity(final_path)
        ):
            raise AssetContractError(f"asset file changed while hashing: {path}")
        return b"".join(chunks), "sha256:" + digest.hexdigest(), before
    finally:
        os.close(descriptor)


def _hash_regular(
    path: Path,
    *,
    expected_uid: int,
    exact_mode: int | None,
    require_read_only: bool,
    target_digest: Any | None = None,
) -> tuple[str, int]:
    """Stream a regular file while retaining the same safety guarantees."""

    try:
        path_metadata = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise AssetContractError(f"asset file is unavailable: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(path_metadata.st_mode)
            or before.st_uid != expected_uid
            or before.st_nlink != 1
            or _metadata_identity(before) != _metadata_identity(path_metadata)
            or exact_mode is not None
            and stat.S_IMODE(before.st_mode) != exact_mode
            or require_read_only
            and before.st_mode & 0o222
        ):
            raise AssetContractError(f"asset file is unsafe: {path}")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
            if target_digest is not None:
                target_digest.update(chunk)
        after = os.fstat(descriptor)
        try:
            final_path = path.lstat()
        except OSError as exc:
            raise AssetContractError(f"asset file disappeared: {path}") from exc
        if (
            total != after.st_size
            or _metadata_identity(before) != _metadata_identity(after)
            or _metadata_identity(before) != _metadata_identity(final_path)
        ):
            raise AssetContractError(f"asset file changed while hashing: {path}")
        return "sha256:" + digest.hexdigest(), total
    finally:
        os.close(descriptor)


def _validate_directory(
    path: Path,
    *,
    expected_uid: int,
    exact_mode: int | None,
    require_read_only: bool,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AssetContractError(f"asset directory is unavailable: {path}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != expected_uid
        or exact_mode is not None
        and stat.S_IMODE(metadata.st_mode) != exact_mode
        or require_read_only
        and metadata.st_mode & 0o222
    ):
        raise AssetContractError(f"asset directory is unsafe: {path}")
    return metadata


def _normalized_records(
    value: object,
    *,
    label: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise AssetContractError(f"{label} inventory must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in value:
        if not isinstance(record, dict) or set(record) != {
            "path",
            "size",
            "sha256",
        }:
            raise AssetContractError(f"{label} inventory record is malformed")
        relative = record.get("path")
        pure = (
            PurePosixPath(relative)
            if isinstance(relative, str)
            else PurePosixPath(".")
        )
        size = record.get("size")
        digest = record.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or pure.is_absolute()
            or ".." in pure.parts
            or str(pure) != relative
            or relative in seen
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or HEX_DIGEST_RE.fullmatch(digest) is None
        ):
            raise AssetContractError(f"{label} inventory record is unsafe")
        seen.add(relative)
        result.append({"path": relative, "size": size, "sha256": digest})
    return result


def _tree_digest(records: list[dict[str, Any]]) -> str:
    return sha256_bytes(canonical_json_bytes({"files": records}, newline=True))


def _validate_manifested_release(
    root: Path,
    *,
    expected_digest: str,
    expected_uid: int,
    schema_version: int,
    exact_directory_mode: int | None,
    exact_file_mode: int | None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], str, int]:
    root_before = _validate_directory(
        root,
        expected_uid=expected_uid,
        exact_mode=exact_directory_mode,
        require_read_only=True,
    )
    manifest_path = root / "ASSET-MANIFEST.json"
    manifest_bytes, manifest_digest, _metadata = _read_regular(
        manifest_path,
        expected_uid=expected_uid,
        exact_mode=exact_file_mode,
        require_read_only=True,
        maximum_bytes=MAX_MANIFEST_BYTES,
    )
    if manifest_digest != expected_digest:
        raise AssetContractError("asset manifest digest differs from its release name")
    try:
        document = json.loads(manifest_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AssetContractError("asset manifest is invalid JSON") from exc
    expected_fields = V1_FIELDS if schema_version == 1 else V2_FIELDS
    if (
        not isinstance(document, dict)
        or set(document) != expected_fields
        or document.get("schema_version") != schema_version
        or canonical_json_bytes(document, newline=True) != manifest_bytes
    ):
        raise AssetContractError("asset manifest is not the exact canonical schema")
    raw_assets = document.get("assets")
    if not isinstance(raw_assets, dict) or set(raw_assets) != set(ASSET_TREES):
        raise AssetContractError("asset manifest tree set is incomplete")
    assets = {
        tree_name: _normalized_records(raw_assets[tree_name], label=tree_name)
        for tree_name in ASSET_TREES
    }
    if {entry.name for entry in root.iterdir()} != set(ASSET_TREES) | {
        "ASSET-MANIFEST.json"
    }:
        raise AssetContractError("asset release contains unmanifested root entries")

    expected_files = {"ASSET-MANIFEST.json"}
    expected_directories = set(ASSET_TREES)
    for tree_name, records in assets.items():
        _validate_directory(
            root / tree_name,
            expected_uid=expected_uid,
            exact_mode=exact_directory_mode,
            require_read_only=True,
        )
        for record in records:
            relative = PurePosixPath(str(record["path"]))
            expected_files.add(f"{tree_name}/{relative}")
            parent = PurePosixPath(tree_name) / relative.parent
            while str(parent) != ".":
                expected_directories.add(str(parent))
                parent = parent.parent
            path = root / tree_name
            for component in relative.parts:
                path /= component
            digest, size = _hash_regular(
                path,
                expected_uid=expected_uid,
                exact_mode=exact_file_mode,
                require_read_only=True,
            )
            if (
                size != record["size"]
                or digest.removeprefix("sha256:") != record["sha256"]
            ):
                raise AssetContractError(
                    f"asset file differs from manifest: {tree_name}/{relative}"
                )

    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    file_count = 0
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        current_relative = current_path.relative_to(root).as_posix()
        if current_relative != ".":
            _validate_directory(
                current_path,
                expected_uid=expected_uid,
                exact_mode=exact_directory_mode,
                require_read_only=True,
            )
            actual_directories.add(current_relative)
        directories.sort()
        for name in sorted(files):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            actual_files.add(relative)
            file_count += 1
    if (
        actual_files != expected_files
        or actual_directories != expected_directories
    ):
        raise AssetContractError("asset release contains missing or extra entries")
    full_digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            _validate_directory(
                path,
                expected_uid=expected_uid,
                exact_mode=exact_directory_mode,
                require_read_only=True,
            )
            full_digest.update(b"D\0" + relative + b"\0")
        elif stat.S_ISREG(metadata.st_mode):
            full_digest.update(b"F\0" + relative + b"\0")
            _hash_regular(
                path,
                expected_uid=expected_uid,
                exact_mode=exact_file_mode,
                require_read_only=True,
                target_digest=full_digest,
            )
            full_digest.update(b"\0")
        else:
            raise AssetContractError("asset release contains a symlink or special file")
    root_after = root.lstat()
    if _metadata_identity(root_before) != _metadata_identity(root_after):
        raise AssetContractError("asset release root changed while being inspected")
    return (
        document,
        assets,
        "sha256:" + full_digest.hexdigest(),
        file_count,
    )


def validate_schema_v1_release(
    root: Path,
    *,
    expected_digest: str = PREDECESSOR_ASSET_DIGEST,
    releases_root: Path = ASSET_RELEASES_ROOT,
    expected_uid: int | None = None,
) -> dict[str, Any]:
    """Rehash the complete immutable predecessor release."""

    expected_digest = require_digest(expected_digest, "predecessor asset digest")
    expected_uid = os.geteuid() if expected_uid is None else expected_uid
    expected_root = releases_root / expected_digest.removeprefix("sha256:")
    if root != expected_root:
        raise AssetContractError("predecessor release path differs from its digest")
    document, assets, inventory, count = _validate_manifested_release(
        root,
        expected_digest=expected_digest,
        expected_uid=expected_uid,
        schema_version=1,
        exact_directory_mode=None,
        exact_file_mode=None,
    )
    require_sha(document.get("byteff2_commit"), "predecessor ByteFF2 commit")
    submodules = document.get("byteff2_submodules")
    if not isinstance(submodules, dict) or any(
        not isinstance(path, str)
        or not path
        or str(PurePosixPath(path)) != path
        or PurePosixPath(path).is_absolute()
        or ".." in PurePosixPath(path).parts
        or SHA_RE.fullmatch(str(commit)) is None
        for path, commit in submodules.items()
    ):
        raise AssetContractError("predecessor ByteFF2 submodule map is invalid")
    return {
        "root": str(root),
        "manifest_sha256": expected_digest,
        "schema_version": 1,
        "byteff2_commit": document["byteff2_commit"],
        "assets": assets,
        "inventory_sha256": inventory,
        "file_count": count,
    }


def validate_schema_v2_release(
    root: Path,
    *,
    expected_digest: str,
    releases_root: Path = ASSET_RELEASES_ROOT,
    expected_uid: int | None = None,
    contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Deeply validate one schema-v2 release and its schema-v1 predecessor."""

    expected_digest = require_digest(expected_digest, "schema-v2 asset digest")
    expected_uid = os.geteuid() if expected_uid is None else expected_uid
    expected_root = releases_root / expected_digest.removeprefix("sha256:")
    if root != expected_root:
        raise AssetContractError("schema-v2 release path differs from its digest")
    expected = dict(default_contract() if contract is None else contract)
    predecessor_digest = require_digest(
        expected.get("predecessor_asset_digest"),
        "expected predecessor digest",
    )
    document, assets, inventory, count = _validate_manifested_release(
        root,
        expected_digest=expected_digest,
        expected_uid=expected_uid,
        schema_version=2,
        exact_directory_mode=0o500,
        exact_file_mode=0o400,
    )
    if (
        document.get("predecessor_asset_digest") != predecessor_digest
        or document.get("changed_asset_trees") != ["byteff2"]
        or document.get("unchanged_asset_tree_digests")
        != expected.get("unchanged_asset_tree_digests")
        or document.get("asset_tree_digests")
        != expected.get("asset_tree_digests")
        or document.get("byteff2_commit") != expected.get("byteff2_commit")
        or document.get("byteff2_tree") != expected.get("byteff2_tree")
        or document.get("byteff2_submodules")
        != expected.get("byteff2_submodules")
        or document.get("byteff2_submodule_trees")
        != expected.get("byteff2_submodule_trees")
        or document.get("byteff2_source") != expected.get("byteff2_source")
        or document.get("byteff2_audited_overlays")
        != expected.get("byteff2_audited_overlays")
    ):
        raise AssetContractError("schema-v2 fixed provenance contract differs")
    for tree_name, records in assets.items():
        if _tree_digest(records) != document["asset_tree_digests"][tree_name]:
            raise AssetContractError(
                f"schema-v2 tree digest differs from inventory: {tree_name}"
            )
    for tree_name in UNCHANGED_ASSET_TREES:
        if (
            document["asset_tree_digests"][tree_name]
            != document["unchanged_asset_tree_digests"][tree_name]
        ):
            raise AssetContractError(
                f"schema-v2 unchanged tree identity differs: {tree_name}"
            )

    overlay_records = {
        str(record["path"]): record
        for record in document["byteff2_audited_overlays"]["files"]
    }
    byteff2_records = {
        str(record["path"]): record for record in assets["byteff2"]
    }
    required_runtime = expected.get("byteff2_required_runtime_files")
    if not isinstance(required_runtime, dict):
        raise AssetContractError("ByteFF2 runtime contract is unavailable")
    for relative, raw_identity in required_runtime.items():
        if (
            not isinstance(relative, str)
            or not isinstance(raw_identity, list)
            or len(raw_identity) != 2
        ):
            raise AssetContractError("ByteFF2 runtime contract is malformed")
        size, digest = raw_identity
        record = byteff2_records.get(relative)
        if (
            not isinstance(size, int)
            or not isinstance(digest, str)
            or record is None
            or record.get("size") != size
            or record.get("sha256") != digest
        ):
            raise AssetContractError(
                f"ByteFF2 runtime file differs from fixed identity: {relative}"
            )
        if relative in overlay_records and overlay_records[relative] != {
            **overlay_records[relative],
            "size": size,
            "sha256": digest,
        }:
            raise AssetContractError("ByteFF2 overlay manifest is inconsistent")
    marker = root / "byteff2/BYTEFF2-COMMIT"
    marker_bytes, _marker_digest, _marker_metadata = _read_regular(
        marker,
        expected_uid=expected_uid,
        exact_mode=0o400,
        require_read_only=True,
        maximum_bytes=128,
    )
    if marker_bytes != (str(document["byteff2_commit"]) + "\n").encode("ascii"):
        raise AssetContractError("BYTEFF2-COMMIT differs from manifest")

    provenance = document.get("build_provenance")
    if (
        not isinstance(provenance, dict)
        or set(provenance) != {"schema_version", "builder_source", "evidence"}
        or provenance.get("schema_version") != 1
        or provenance.get("evidence") != expected.get("build_evidence")
    ):
        raise AssetContractError("schema-v2 build evidence differs")
    builder = provenance.get("builder_source")
    if (
        not isinstance(builder, dict)
        or set(builder)
        != {"repository", "commit", "tree", "script_path", "script_blob"}
        or builder.get("repository") != expected.get("builder_repository")
        or builder.get("script_path") != expected.get("builder_script")
    ):
        raise AssetContractError("schema-v2 builder source is invalid")
    for name in ("commit", "tree", "script_blob"):
        require_sha(builder.get(name), f"asset builder {name}")

    predecessor_root = releases_root / predecessor_digest.removeprefix("sha256:")
    predecessor = validate_schema_v1_release(
        predecessor_root,
        expected_digest=predecessor_digest,
        releases_root=releases_root,
        expected_uid=expected_uid,
    )
    predecessor_assets = predecessor.pop("assets")
    for tree_name in UNCHANGED_ASSET_TREES:
        if predecessor_assets[tree_name] != assets[tree_name]:
            raise AssetContractError(
                f"schema-v2 inherited tree differs byte-for-byte: {tree_name}"
            )

    byteff2_identity = {
        "source": document["byteff2_source"],
        "commit": document["byteff2_commit"],
        "tree": document["byteff2_tree"],
        "submodules": document["byteff2_submodules"],
        "submodule_trees": document["byteff2_submodule_trees"],
        "audited_overlays": document["byteff2_audited_overlays"],
    }
    return {
        "root": str(root),
        "manifest_path": str(root / "ASSET-MANIFEST.json"),
        "manifest_sha256": expected_digest,
        "schema_version": 2,
        "predecessor_root": predecessor["root"],
        "predecessor_manifest_sha256": predecessor["manifest_sha256"],
        "predecessor_inventory_sha256": predecessor["inventory_sha256"],
        "predecessor_file_count": predecessor["file_count"],
        "changed_asset_trees": ["byteff2"],
        "asset_tree_digests": dict(document["asset_tree_digests"]),
        "unchanged_asset_tree_digests": dict(
            document["unchanged_asset_tree_digests"]
        ),
        "byteff2_commit": document["byteff2_commit"],
        "byteff2_identity_sha256": sha256_bytes(
            canonical_json_bytes(byteff2_identity)
        ),
        "builder_source": dict(builder),
        "builder_source_identity_sha256": sha256_bytes(
            canonical_json_bytes(builder)
        ),
        "inventory_sha256": inventory,
        "file_count": count,
        "read_only": True,
    }


def _private_file_digest(path: Path, *, expected_uid: int) -> str:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise AssetContractError("offline Git bundle is not a private regular file")
    digest, _size = _hash_regular(
        path,
        expected_uid=expected_uid,
        exact_mode=0o600,
        require_read_only=False,
    )
    return digest


def _git_environment(home: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "file",
        "http_proxy": "",
        "https_proxy": "",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
        "NO_PROXY": "*",
    }


def _git_run(
    arguments: list[str],
    *,
    cwd: Path | None,
    environment: Mapping[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        env=dict(environment),
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )


def verify_builder_from_bundle(
    bundle_path: Path,
    *,
    expected_bundle_sha256: str,
    builder_source: Mapping[str, Any],
    target: Mapping[str, Any],
    authority: Mapping[str, Any],
    expected_uid: int | None = None,
) -> dict[str, Any]:
    """Prove B0 -> B1 -> F entirely from the sealed, full-history bundle."""

    expected_uid = os.geteuid() if expected_uid is None else expected_uid
    expected_bundle_sha256 = require_digest(
        expected_bundle_sha256,
        "offline Git bundle",
    )
    if _private_file_digest(bundle_path, expected_uid=expected_uid) != (
        expected_bundle_sha256
    ):
        raise AssetContractError("offline Git bundle digest differs")
    builder = dict(builder_source)
    if (
        set(builder)
        != {"repository", "commit", "tree", "script_path", "script_blob"}
        or builder.get("repository") != BUILD_SOURCE_REPOSITORY
        or builder.get("script_path") != BUILD_SOURCE_SCRIPT
    ):
        raise AssetContractError("asset builder source is not canonical")
    builder_sha = require_sha(builder.get("commit"), "builder commit")
    builder_tree = require_sha(builder.get("tree"), "builder tree")
    builder_blob = require_sha(builder.get("script_blob"), "builder script blob")
    target_sha = require_sha(target.get("sha"), "bridge target SHA")
    target_tree = require_sha(target.get("tree"), "bridge target tree")
    authority_sha = require_sha(authority.get("sha"), "authority SHA")
    authority_tree = require_sha(authority.get("tree"), "authority tree")
    if len({builder_sha, target_sha, authority_sha}) != 3:
        raise AssetContractError("B0, B1, and F must be distinct commits")

    parent = bundle_path.parent
    _validate_directory(
        parent,
        expected_uid=expected_uid,
        exact_mode=0o700,
        require_read_only=False,
    )
    with tempfile.TemporaryDirectory(prefix=".asset-proof-", dir=parent) as raw:
        scratch = Path(raw)
        os.chmod(scratch, 0o700)
        environment = _git_environment(scratch)
        verifier = scratch / "verify.git"
        _git_run(
            ["git", "init", "--bare", "--quiet", str(verifier)],
            cwd=None,
            environment=environment,
        )
        _git_run(
            ["git", "-C", str(verifier), "bundle", "verify", str(bundle_path)],
            cwd=None,
            environment=environment,
        )
        heads = _git_run(
            ["git", "bundle", "list-heads", str(bundle_path)],
            cwd=None,
            environment=environment,
        ).stdout.splitlines()
        if heads != [f"{authority_sha} refs/heads/main"]:
            raise AssetContractError("offline bundle advertises another F main")
        clone = scratch / "clone"
        _git_run(
            [
                "git",
                "-c",
                "protocol.file.allow=always",
                "clone",
                "--quiet",
                "--no-checkout",
                "--template=/dev/null",
                str(bundle_path),
                str(clone),
            ],
            cwd=None,
            environment=environment,
        )
        common = _git_run(
            ["git", "-C", str(clone), "rev-parse", "--git-common-dir"],
            cwd=None,
            environment=environment,
        ).stdout.strip()
        if (
            common not in {".git", str(clone / ".git")}
            or (clone / ".git/objects/info/alternates").exists()
        ):
            raise AssetContractError("offline proof clone uses external Git objects")
        partial = _git_run(
            [
                "git",
                "-C",
                str(clone),
                "config",
                "--get-regexp",
                r"^(extensions\\.partialClone|remote\\..*\\.promisor)$",
            ],
            cwd=None,
            environment=environment,
            check=False,
        )
        if partial.returncode not in {1} or partial.stdout:
            raise AssetContractError(
                "offline proof clone is partial or promisor-backed"
            )
        _git_run(
            ["git", "-C", str(clone), "fsck", "--full", "--strict"],
            cwd=None,
            environment=environment,
        )
        for sha, tree, label in (
            (builder_sha, builder_tree, "B0 builder"),
            (target_sha, target_tree, "B1 target"),
            (authority_sha, authority_tree, "F authority"),
        ):
            observed = _git_run(
                ["git", "-C", str(clone), "rev-parse", f"{sha}^{{tree}}"],
                cwd=None,
                environment=environment,
            ).stdout.strip()
            if observed != tree:
                raise AssetContractError(f"{label} tree differs in offline bundle")
        observed_blob = _git_run(
            [
                "git",
                "-C",
                str(clone),
                "rev-parse",
                f"{builder_sha}:{BUILD_SOURCE_SCRIPT}",
            ],
            cwd=None,
            environment=environment,
        ).stdout.strip()
        if observed_blob != builder_blob:
            raise AssetContractError("B0 builder script blob differs in offline bundle")
        for ancestor, descendant, label in (
            (builder_sha, target_sha, "B0 -> B1"),
            (target_sha, authority_sha, "B1 -> F"),
            (builder_sha, authority_sha, "B0 -> F"),
        ):
            relation = _git_run(
                [
                    "git",
                    "-C",
                    str(clone),
                    "merge-base",
                    "--is-ancestor",
                    ancestor,
                    descendant,
                ],
                cwd=None,
                environment=environment,
                check=False,
            )
            if relation.returncode != 0:
                raise AssetContractError(
                    f"offline bundle does not prove strict ancestry: {label}"
                )
    identity = {
        "schema_version": 1,
        "bundle_sha256": expected_bundle_sha256,
        "builder": builder,
        "target": {"sha": target_sha, "tree": target_tree},
        "authority": {"sha": authority_sha, "tree": authority_tree},
        "ancestry": {
            "builder_to_target": True,
            "target_to_authority": True,
            "builder_to_authority": True,
        },
        "network_used": False,
        "temporary_clone_fsck": True,
    }
    return {
        **identity,
        "proof_sha256": sha256_bytes(canonical_json_bytes(identity)),
    }


def snapshot_live_asset_pointer(
    pointer: Path,
    *,
    expected_uid: int | None = None,
    releases_root: Path = ASSET_RELEASES_ROOT,
) -> dict[str, Any]:
    """Capture an inode-bound snapshot without changing the managed pointer."""

    expected_uid = os.geteuid() if expected_uid is None else expected_uid
    try:
        metadata = pointer.lstat()
    except FileNotFoundError:
        return {"path": str(pointer), "present": False}
    except OSError as exc:
        raise AssetContractError("live asset pointer cannot be inspected") from exc
    if (
        not stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or metadata.st_nlink != 1
    ):
        raise AssetContractError("live asset pointer is not a deploy-user symlink")
    raw_target = os.readlink(pointer)
    if not Path(raw_target).is_absolute():
        raise AssetContractError("live asset pointer target must be absolute")
    try:
        resolved = pointer.resolve(strict=True)
        target_metadata = resolved.lstat()
    except OSError as exc:
        raise AssetContractError("live asset pointer target is unavailable") from exc
    if (
        resolved.parent != releases_root
        or HEX_DIGEST_RE.fullmatch(resolved.name) is None
        or not stat.S_ISDIR(target_metadata.st_mode)
        or resolved.is_symlink()
        or target_metadata.st_uid != expected_uid
        or target_metadata.st_mode & 0o222
    ):
        raise AssetContractError("live asset pointer target is unsafe")
    manifest = resolved / "ASSET-MANIFEST.json"
    _bytes, manifest_sha256, _manifest_metadata = _read_regular(
        manifest,
        expected_uid=expected_uid,
        exact_mode=None,
        require_read_only=True,
        maximum_bytes=MAX_MANIFEST_BYTES,
    )
    if manifest_sha256 != "sha256:" + resolved.name:
        raise AssetContractError(
            "live asset pointer target is not content-addressed"
        )
    return {
        "path": str(pointer),
        "present": True,
        "target": raw_target,
        "resolved_target": str(resolved),
        "manifest_sha256": manifest_sha256,
        "target_identity": {
            "device": target_metadata.st_dev,
            "inode": target_metadata.st_ino,
            "mode": stat.S_IMODE(target_metadata.st_mode),
            "uid": target_metadata.st_uid,
            "gid": target_metadata.st_gid,
            "mtime_ns": target_metadata.st_mtime_ns,
            "ctime_ns": target_metadata.st_ctime_ns,
        },
        "pointer_identity": {
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "mode": stat.S_IMODE(metadata.st_mode),
            "uid": metadata.st_uid,
            "gid": metadata.st_gid,
            "size": metadata.st_size,
            "mtime_ns": metadata.st_mtime_ns,
            "ctime_ns": metadata.st_ctime_ns,
        },
    }


def build_asset_evidence(
    *,
    expected_digest: str,
    bundle_path: Path,
    expected_bundle_sha256: str,
    target: Mapping[str, Any],
    authority: Mapping[str, Any],
    live_pointer_start: Mapping[str, Any],
    live_pointer_end: Mapping[str, Any],
    datasets_on_asset_change: object,
    releases_root: Path = ASSET_RELEASES_ROOT,
    expected_uid: int | None = None,
    contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build exact evidence for an inactive release without side effects."""

    if datasets_on_asset_change != []:
        raise AssetContractError("schema-v2 assets must not rebuild database datasets")
    if dict(live_pointer_start) != dict(live_pointer_end):
        raise AssetContractError("live asset pointer changed during prefetch")
    expected_digest = require_digest(expected_digest, "schema-v2 asset digest")
    root = releases_root / expected_digest.removeprefix("sha256:")
    release = validate_schema_v2_release(
        root,
        expected_digest=expected_digest,
        releases_root=releases_root,
        expected_uid=expected_uid,
        contract=contract,
    )
    proof = verify_builder_from_bundle(
        bundle_path,
        expected_bundle_sha256=expected_bundle_sha256,
        builder_source=release["builder_source"],
        target=target,
        authority=authority,
        expected_uid=expected_uid,
    )
    identity = {
        "schema_version": 1,
        **release,
        "builder_proof": proof,
        "datasets_on_asset_change": [],
        "database_effect": "none",
        "mutable_data_seal": {
            "schema_version": 1,
            "required": True,
            "authority": "pull-descriptor",
            "included_in_asset_evidence": False,
        },
        "live_pointer_start": dict(live_pointer_start),
        "live_pointer_end": dict(live_pointer_end),
    }
    return {
        **identity,
        "identity_sha256": sha256_bytes(canonical_json_bytes(identity)),
    }
