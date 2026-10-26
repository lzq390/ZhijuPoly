"""Engine-free verification shared by immutable worker releases and operators."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .journal import read_json, sha256_file


def verify_release(directory: Path) -> dict:
    """Verify the existing version-one inventory without trusting linked paths."""
    directory = Path(directory)
    if any(path.is_symlink() for path in (directory, *directory.parents)) or not directory.is_dir():
        raise ValueError("release directory must be a regular, unlinked directory")
    manifest = read_json(directory / "release.json")
    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int or manifest["version"] != 1:
        raise ValueError("release identity is invalid")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("release inventory is invalid")
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if digest != manifest.get("source_digest"):
        raise ValueError("release identity is invalid")
    names = set()
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("release inventory is invalid")
        name = entry["path"]
        relative = Path(name)
        if (not name or relative.is_absolute() or ".." in relative.parts or str(relative) != name
                or "\\" in name or name in names or name == "release.json"
                or "__pycache__" in relative.parts):
            raise ValueError("release inventory path is invalid or duplicated")
        names.add(name)
        target = directory
        for part in relative.parts:
            target = target / part
            if target.is_symlink():
                raise ValueError("release paths must not contain symbolic links")
        if (not target.is_file() or type(entry.get("size_bytes")) is not int
                or entry["size_bytes"] < 0 or target.stat().st_size != entry["size_bytes"]
                or not isinstance(entry.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
                or sha256_file(target) != entry["sha256"]):
            raise ValueError("release bytes are invalid")
    actual = set()
    for path in directory.rglob("*"):
        if "__pycache__" in path.relative_to(directory).parts:
            continue
        if path.is_symlink():
            raise ValueError("release paths must not contain symbolic links")
        if path.is_file() and path.name != "release.json":
            actual.add(path.relative_to(directory).as_posix())
        elif not path.is_file() and not path.is_dir():
            raise ValueError("release contains a nonregular entry")
    if actual != names:
        raise ValueError("release inventory contains unexpected or missing files")
    return manifest
