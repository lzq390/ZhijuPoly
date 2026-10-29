#!/usr/bin/env python3
"""Freeze and verify an independent reader release; never controls the MD Worker."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("fastapi", "pydantic", "httpx", "uvicorn", "numpy", "pandas", "scipy", "mdtraj")


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def dependencies(python):
    code = "import importlib.metadata,json; print(json.dumps({n:importlib.metadata.version(n) for n in " + repr(PACKAGES) + "},sort_keys=True))"
    return json.loads(subprocess.check_output([str(python), "-I", "-B", "-c", code], text=True))


def source_names(root):
    selected = {"scripts/polymer_md_analysis_dev.py"}
    for package in ("polymer_md", "workers/polymer_md_analysis", "workers/polymer_md_worker/app"):
        for path in (root / package).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".json"} and "__pycache__" not in path.parts and "tests" not in path.relative_to(root / package).parts:
                selected.add(str(path.relative_to(root)))
    for name in ("workers/__init__.py", "workers/polymer_md_worker/__init__.py"):
        if (root / name).is_file():
            selected.add(name)
    for name in selected:
        path = root / name
        if any(p.is_symlink() for p in (path, *path.parents)) or not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("analysis release sources must be ordinary files")
    return sorted(selected)


def verify(release):
    if any(path.is_symlink() for path in (release, *release.parents)):
        raise ValueError("analysis release cannot contain symbolic links")
    if (release / "manifest.json").is_symlink() or not (release / "manifest.json").is_file():
        raise ValueError("analysis manifest must be an ordinary file")
    manifest = json.loads((release / "manifest.json").read_text())
    fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if release.name != fingerprint:
        raise ValueError("analysis release manifest identity changed")
    source = release / "source"
    expected = manifest["files"]
    actual = []
    directories = {str(parent) for name in expected for parent in Path(name).parents if str(parent) != "."}
    if source.is_symlink() or not source.is_dir():
        raise ValueError("analysis source root must be an ordinary directory")
    for path in source.rglob("*"):
        relative = str(path.relative_to(source))
        if path.is_symlink():
            raise ValueError("analysis source symbolic link rejected")
        if path.is_dir():
            if relative not in directories:
                raise ValueError("unexpected analysis source directory")
        elif not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("analysis source must be an ordinary file")
        else:
            actual.append(relative)
    if sorted(actual) != sorted(expected) or source_names(source) != sorted(expected):
        raise ValueError("analysis source inventory changed")
    if any(digest(source / name) != value for name, value in expected.items()):
        raise ValueError("analysis source integrity check failed")
    if dependencies(manifest["python"]) != manifest["dependencies"]:
        raise ValueError("analysis dependency versions changed")
    for item in manifest["dependency_locks"]:
        if digest(Path(item["path"])) != item["sha256"]:
            raise ValueError("analysis dependency lock changed")
    return manifest


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--source-root", type=Path, default=ROOT)
    prepare.add_argument("--release-base", type=Path, required=True)
    prepare.add_argument("--python", type=Path, required=True)
    prepare.add_argument("--dependency-lock", type=Path, action="append", default=[])
    for name in ("verify", "run"):
        command = commands.add_parser(name)
        command.add_argument("--release", type=Path, required=True)
        if name == "run":
            command.add_argument("--uds", required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        root = args.source_root.resolve(strict=True)
        manifest = {"protocol_version": 1, "python": str(args.python.absolute()),
            "dependencies": dependencies(args.python),
            "dependency_locks": [{"path": str(path.absolute()), "sha256": digest(path)} for path in args.dependency_lock],
            "files": {name: digest(root / name) for name in source_names(root)}}
        fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        release = args.release_base.absolute() / fingerprint
        release.mkdir(parents=True, exist_ok=False, mode=0o700)
        source = release / "source"
        for name in manifest["files"]:
            destination = source / name
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(root / name, destination)
            destination.chmod(0o444)
        (release / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        (release / "manifest.json").chmod(0o444)
        for directory in sorted((p for p in source.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            directory.chmod(0o555)
        source.chmod(0o555)
        verify(release)
        print(json.dumps({"status": "prepared", "release": str(release), "fingerprint": fingerprint, "files": len(manifest["files"])}))
    else:
        manifest = verify(args.release)
        if args.action == "verify":
            print(json.dumps({"status": "verified", "files": len(manifest["files"])}))
            return
        os.chdir(args.release / "source")
        os.environ.pop("PYTHONPATH", None)
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        os.environ["PYTHONNOUSERSITE"] = "1"
        os.execv(manifest["python"], [manifest["python"], "-B", "-s", "-m", "workers.polymer_md_analysis", "--uds", args.uds])


if __name__ == "__main__":
    main()
