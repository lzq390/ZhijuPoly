"""Concurrent scratch deletion must not be mistaken for an engine failure."""
from pathlib import Path

import pytest

from workers.polymer_md_worker.app import manager as manager_module
from workers.polymer_md_worker.app.manager import JobManager
from test_manager import request, supervisor, until


@pytest.fixture
def anyio_backend():
    return "asyncio"


def delete_after_walk_listing(monkeypatch, target):
    original = manager_module.os.walk
    deleted = []

    def racing_walk(*args, **kwargs):
        for parent, directories, filenames in original(*args, **kwargs):
            if not deleted and Path(parent) == target.parent and target.name in filenames:
                target.unlink()
                deleted.append(target)
            yield parent, directories, filenames

    monkeypatch.setattr(manager_module.os, "walk", racing_walk)
    return deleted


def test_deleted_scratch_file_after_listing_is_skipped_until_next_scan(tmp_path, monkeypatch):
    scratch = tmp_path / "psi.scratch"
    scratch.write_bytes(b"temporary scratch")
    (tmp_path / "retained.log").write_bytes(b"retained")
    deleted = delete_after_walk_listing(monkeypatch, scratch)
    assert JobManager._job_size(tmp_path) == len(b"retained")
    assert deleted == [scratch]
    scratch.write_bytes(b"new scratch")
    assert JobManager._job_size(tmp_path) == len(b"retained") + len(b"new scratch")


@pytest.mark.anyio
async def test_live_stage_is_not_killed_or_retried_by_normal_scratch_deletion(tmp_path, monkeypatch):
    manager, calls = supervisor(tmp_path, delay="terminal_qm")
    payload = request()
    scratch = manager._directory(payload) / "science/terminal_qm/psi.scratch"
    original = manager.command_builder

    def command(request_path, stage, work, result_path):
        base = original(request_path, stage, work, result_path)
        if stage == "terminal_qm":
            prefix = f"from pathlib import Path\np=Path({str(scratch)!r});p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'temporary')\n"
            return [*base[:-1], prefix + base[-1]]
        return base

    manager.command_builder = command
    deleted = delete_after_walk_listing(monkeypatch, scratch)
    await manager.start()
    try:
        await manager.submit(payload)
        await until(lambda: manager.get(payload.job_id)["status"] == "completed")
        assert deleted == [scratch]
        assert calls.count("terminal_qm") == 1
        assert all(run["status"] == "completed" for run in manager.get(payload.job_id)["stage_runs"])
    finally:
        await manager.stop()


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError(5, "I/O error")])
def test_scanning_stat_permissions_and_io_errors_still_propagate(tmp_path, monkeypatch, error):
    target = tmp_path / "psi.scratch"
    target.write_bytes(b"private")
    original = Path.lstat

    def fail_stat(path, *args, **kwargs):
        if path == target:
            raise error
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", fail_stat)
    with pytest.raises(type(error)):
        JobManager._job_size(tmp_path)


@pytest.mark.parametrize("error", [PermissionError("directory denied"), OSError(5, "directory I/O error")])
def test_directory_listing_errors_are_not_silently_ignored(tmp_path, monkeypatch, error):
    def failing_walk(_root, *, followlinks, onerror=None):
        if onerror:
            onerror(error)
        return iter(())

    monkeypatch.setattr(manager_module.os, "walk", failing_walk)
    with pytest.raises(type(error)):
        JobManager._job_size(tmp_path)


@pytest.mark.parametrize("directory_link", [False, True])
def test_scanning_rejects_file_and_directory_symlinks(tmp_path, directory_link):
    root = tmp_path / "job"
    root.mkdir()
    outside = tmp_path / "external"
    outside.mkdir() if directory_link else outside.write_bytes(b"external")
    (root / "unsafe").symlink_to(outside, target_is_directory=directory_link)
    with pytest.raises(ValueError, match="symbolic links"):
        JobManager._job_size(root)
