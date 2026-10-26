"""Synthetic release bytes and lifecycle failures; no services or engines."""
import hashlib
import json
import sys

import pytest

from workers.polymer_md_worker.app import manager as manager_module
from workers.polymer_md_worker.app.journal import atomic_json, sha256_file
from workers.polymer_md_worker.app.main import create_app
from workers.polymer_md_worker.app.release_contract import verify_release
from test_manager import supervisor


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _seal(directory, inventory):
    manifest = {"version": 1, "files": inventory,
        "source_digest": hashlib.sha256(json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
    atomic_json(directory / "release.json", manifest)
    return manifest


def _release(tmp_path):
    directory = tmp_path / "release"
    inventory = []
    for name in ("polymer_md/__init__.py", "workers/polymer_md_worker/app/manager.py"):
        path = directory / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(b"# Synthetic frozen source fixture; never executed.\n")
        inventory.append({"path": name, "sha256": sha256_file(path), "size_bytes": path.stat().st_size})
    return directory, _seal(directory, inventory)


def test_release_preserves_exact_version_one_source_identity(tmp_path):
    directory, manifest = _release(tmp_path)
    assert verify_release(directory) == manifest


@pytest.mark.parametrize("damage", ["bytes", "missing", "unexpected", "digest", "size", "duplicate", "parent_link"])
def test_release_rejects_changed_bytes_inventory_and_linked_paths(tmp_path, damage):
    directory, manifest = _release(tmp_path)
    first = directory / manifest["files"][0]["path"]
    if damage == "bytes": first.write_bytes(b"changed")
    elif damage == "missing": first.unlink()
    elif damage == "unexpected": (directory / "unexpected.py").write_bytes(b"unexpected")
    elif damage == "digest":
        manifest["source_digest"] = "0" * 64
        atomic_json(directory / "release.json", manifest)
    elif damage == "size":
        manifest["files"][0]["size_bytes"] += 1
        _seal(directory, manifest["files"])
    elif damage == "duplicate":
        _seal(directory, [*manifest["files"], manifest["files"][0]])
    else:
        moved = tmp_path / "outside-package"
        (directory / "polymer_md").rename(moved)
        (directory / "polymer_md").symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError): verify_release(directory)


def test_release_rejects_linked_parent_of_the_release_root(tmp_path):
    directory, _ = _release(tmp_path / "real-parent")
    parent = tmp_path / "linked-parent"
    parent.symlink_to(directory.parent, target_is_directory=True)
    with pytest.raises(ValueError): verify_release(parent / directory.name)


@pytest.mark.anyio
async def test_worker_verifies_release_without_unsubmitted_operations(tmp_path, monkeypatch):
    directory, manifest = _release(tmp_path)
    monkeypatch.setattr(manager_module, "__file__", str(directory / "workers/polymer_md_worker/app/manager.py"))
    monkeypatch.setitem(sys.modules, "scripts.polymer_md_services", None)
    worker, _ = supervisor(tmp_path / "worker")
    try:
        await worker.start()
        assert worker.release_digest == manifest["source_digest"]
    finally:
        await worker.stop()


@pytest.mark.anyio
async def test_worker_rejects_dangling_release_manifest_before_start(tmp_path, monkeypatch):
    directory = tmp_path / "release"
    directory.mkdir()
    (directory / "release.json").symlink_to(tmp_path / "absent-manifest.json")
    monkeypatch.setattr(manager_module, "__file__", str(directory / "workers/polymer_md_worker/app/manager.py"))
    worker, _ = supervisor(tmp_path / "worker")
    try:
        with pytest.raises(ValueError): await worker.start()
    finally:
        await worker.stop()


@pytest.mark.anyio
async def test_worker_lifespan_stops_manager_after_body_failure():
    calls = []
    class Manager:
        async def start(self): calls.append("start")
        async def stop(self): calls.append("stop")
    app = create_app(manager=Manager())
    with pytest.raises(RuntimeError, match="synthetic lifespan failure"):
        async with app.router.lifespan_context(app):
            raise RuntimeError("synthetic lifespan failure")
    assert calls == ["start", "stop"]
