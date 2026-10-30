"""The standalone analysis release verifier must cover every executable byte."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import polymer_md_analysis_dev as release_tool


def fingerprint(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def replace(path, payload):
    path.parent.chmod(0o755)
    if path.exists() or path.is_symlink():
        path.unlink()
    path.write_bytes(payload)
    path.chmod(0o444)
    path.parent.chmod(0o555)


@pytest.fixture
def release(tmp_path, monkeypatch):
    versions = {name: "test-1" for name in release_tool.PACKAGES}
    monkeypatch.setattr(release_tool, "dependencies", lambda _: dict(versions))
    names = ["scripts/polymer_md_analysis_dev.py", "workers/__init__.py", "polymer_md/__init__.py",
             "polymer_md/contracts.py", "workers/polymer_md_worker/__init__.py",
             "workers/polymer_md_worker/app/__init__.py", "workers/polymer_md_worker/app/scientific_analysis.py"]
    names += [f"workers/polymer_md_analysis/{name}.py" for name in
              ("__init__", "__main__", "main", "contracts", "config", "service", "files", "child")]
    contents = {name: ("# " + name + "\n").encode() for name in names}
    lock = tmp_path / "requirements.lock"
    lock.write_text("frozen test dependencies\n")
    manifest = {"protocol_version": 1, "python": "/test/independent/python", "dependencies": versions,
                "dependency_locks": [{"path": str(lock), "sha256": release_tool.digest(lock)}],
                "files": {name: hashlib.sha256(value).hexdigest() for name, value in contents.items()}}
    root = tmp_path / fingerprint(manifest)
    source = root / "source"
    for name, value in contents.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        path.chmod(0o444)
    (root / "manifest.json").write_text(json.dumps(manifest))
    (root / "manifest.json").chmod(0o444)
    for path in sorted((p for p in source.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        path.chmod(0o555)
    source.chmod(0o555)
    yield root, manifest
    for path in tmp_path.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)


def test_valid_content_addressed_release_verifies_without_running_services(release):
    root, manifest = release
    assert release_tool.verify(root) == manifest


def test_changing_a_source_file_is_rejected(release):
    root, _ = release
    replace(root / "source/workers/polymer_md_analysis/main.py", b"unexpected code\n")
    with pytest.raises(ValueError):
        release_tool.verify(root)


def test_rewriting_source_and_manifest_cannot_change_a_release_identity(release):
    root, original = release
    manifest = deepcopy(original)
    name, payload = "workers/polymer_md_analysis/main.py", b"replacement source\n"
    replace(root / "source" / name, payload)
    manifest["files"][name] = hashlib.sha256(payload).hexdigest()
    replace(root / "manifest.json", json.dumps(manifest).encode())
    with pytest.raises(ValueError):
        release_tool.verify(root)


@pytest.mark.parametrize("extra", ["fastapi.py", "unexpected.txt", "workers/__pycache__/__init__.cpython-311.pyc"])
def test_unregistered_files_cannot_shadow_verified_imports(release, extra):
    root, _ = release
    source = root / "source"
    source.chmod(0o755)
    (source / "workers").chmod(0o755)
    path = source / extra
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"unverified executable bytes")
    for parent in (path.parent, source / "workers", source):
        parent.chmod(0o555)
    with pytest.raises(ValueError):
        release_tool.verify(root)


@pytest.mark.parametrize("target", ["manifest.json", "source/fastapi.py"])
def test_unregistered_or_manifest_symlinks_are_rejected(release, tmp_path, target):
    root, _ = release
    destination = root / target
    outside = tmp_path / "outside"
    outside.write_bytes(destination.read_bytes() if destination.exists() else b"outside import bytes")
    destination.parent.chmod(0o755)
    destination.unlink(missing_ok=True)
    destination.symlink_to(outside)
    if destination.parent.name == "source":
        destination.parent.chmod(0o555)
    with pytest.raises(ValueError):
        release_tool.verify(root)


def test_unregistered_empty_directory_is_rejected(release):
    root, _ = release
    source = root / "source"
    source.chmod(0o755)
    (source / "unexpected-package").mkdir()
    source.chmod(0o555)
    with pytest.raises(ValueError):
        release_tool.verify(root)


def test_dependency_lock_drift_is_rejected(release):
    root, manifest = release
    Path(manifest["dependency_locks"][0]["path"]).write_text("changed dependency lock\n")
    with pytest.raises(ValueError):
        release_tool.verify(root)


def test_dependency_version_drift_is_rejected(release, monkeypatch):
    root, manifest = release
    versions = dict(manifest["dependencies"], mdtraj="changed")
    monkeypatch.setattr(release_tool, "dependencies", lambda _: versions)
    with pytest.raises(ValueError):
        release_tool.verify(root)
