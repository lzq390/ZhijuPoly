from __future__ import annotations

import json
import os
from pathlib import Path
import socket as unix_socket
import stat
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import dev_worker_process as process_record  # noqa: E402


def _fake_process(proc_root: Path, pid: int, ticks: int, argv: list[str]) -> None:
    process = proc_root / str(pid)
    process.mkdir(parents=True)
    fields = ["S", *(["0"] * 18), str(ticks), "0"]
    (process / "stat").write_text(
        f"{pid} (uvicorn worker) " + " ".join(fields) + "\n",
        encoding="ascii",
    )
    (process / "cmdline").write_bytes(
        b"\0".join(item.encode("utf-8") for item in argv) + b"\0"
    )


def _fixture(tmp_path: Path):
    pid = 42
    python = tmp_path / "venv/bin/python"
    socket = tmp_path / "socket/worker.sock"
    record = tmp_path / "runs/worker.pid"
    record.parent.mkdir(parents=True)
    argv = [str(python), "-m", "uvicorn", "app.main:app", "--uds", str(socket)]
    proc_root = tmp_path / "proc"
    _fake_process(proc_root, pid, 987654, argv)
    return pid, python, socket, record, argv, proc_root


def test_record_binds_pid_start_command_source_lock_and_instance(tmp_path: Path) -> None:
    pid, python, socket, record, argv, proc_root = _fixture(tmp_path)
    common = {
        "python": python,
        "socket": socket,
        "source_sha": "a" * 40,
        "source_tree": "b" * 40,
        "worker_lock_sha256": "sha256:" + "c" * 64,
        "session_id": "e" * 32,
        "proc_root": proc_root,
    }
    created = process_record.create_record(
        record,
        pid=pid,
        expected_argv=argv,
        **common,
    )
    bound = process_record.bind_instance(record, "d" * 32, **common)
    verified = process_record.verify_record(
        record,
        require_instance=True,
        **common,
    )

    assert created["start_ticks"] == 987654
    assert created["worker_instance_id"] is None
    assert bound["worker_instance_id"] == "d" * 32
    assert verified == bound
    assert oct(record.stat().st_mode & 0o777) == "0o600"
    assert json.loads(record.read_text(encoding="utf-8"))["argv"] == argv


def test_record_rejects_pid_reuse_and_command_replacement(tmp_path: Path) -> None:
    pid, python, socket, record, argv, proc_root = _fixture(tmp_path)
    common = {
        "python": python,
        "socket": socket,
        "source_sha": "a" * 40,
        "source_tree": "b" * 40,
        "worker_lock_sha256": "sha256:" + "c" * 64,
        "session_id": "e" * 32,
        "proc_root": proc_root,
    }
    process_record.create_record(record, pid=pid, expected_argv=argv, **common)

    _fake_stat = proc_root / str(pid) / "stat"
    fields = ["S", *(["0"] * 18), "987655", "0"]
    _fake_stat.write_text(
        f"{pid} (uvicorn worker) " + " ".join(fields) + "\n", encoding="ascii"
    )
    with pytest.raises(process_record.WorkerProcessRecordError, match="PID was reused"):
        process_record.verify_record(record, **common)

    fields[19] = "987654"
    _fake_stat.write_text(
        f"{pid} (uvicorn worker) " + " ".join(fields) + "\n", encoding="ascii"
    )
    (proc_root / str(pid) / "cmdline").write_bytes(b"/bin/sh\0")
    with pytest.raises(process_record.WorkerProcessRecordError, match="command changed"):
        process_record.verify_record(record, **common)


def test_plain_pid_and_broad_mode_are_not_accepted(tmp_path: Path) -> None:
    record = tmp_path / "worker.pid"
    record.write_text("42\n", encoding="ascii")
    os.chmod(record, 0o600)
    with pytest.raises(process_record.WorkerProcessRecordError):
        process_record.load_record(record)

    record.write_text("{}\n", encoding="utf-8")
    os.chmod(record, 0o644)
    with pytest.raises(process_record.WorkerProcessRecordError, match="unsafe"):
        process_record.load_record(record)


def test_collect_dead_record_requires_exact_identity_and_a_dead_process(
    tmp_path: Path,
) -> None:
    pid, python, socket, record, argv, proc_root = _fixture(tmp_path)
    common = {
        "python": python,
        "socket": socket,
        "source_sha": "a" * 40,
        "source_tree": "b" * 40,
        "worker_lock_sha256": "sha256:" + "c" * 64,
        "session_id": "e" * 32,
        "proc_root": proc_root,
    }
    created = process_record.create_record(
        record,
        pid=pid,
        expected_argv=argv,
        **common,
    )

    with pytest.raises(process_record.WorkerProcessRecordError, match="still running"):
        process_record.collect_dead_record(record, **common)
    assert record.exists()

    with pytest.raises(process_record.WorkerProcessRecordError, match="source/runtime"):
        process_record.collect_dead_record(
            record,
            **{**common, "source_sha": "d" * 40},
        )
    assert record.exists()

    for child in (proc_root / str(pid)).iterdir():
        child.unlink()
    (proc_root / str(pid)).rmdir()
    assert process_record.collect_dead_record(record, **common) == created
    assert not record.exists()


def test_collect_dead_record_accepts_pid_reuse_without_touching_the_new_process(
    tmp_path: Path,
) -> None:
    pid, python, socket, record, argv, proc_root = _fixture(tmp_path)
    common = {
        "python": python,
        "socket": socket,
        "source_sha": "a" * 40,
        "source_tree": "b" * 40,
        "worker_lock_sha256": "sha256:" + "c" * 64,
        "session_id": "e" * 32,
        "proc_root": proc_root,
    }
    process_record.create_record(record, pid=pid, expected_argv=argv, **common)
    fields = ["S", *(["0"] * 18), "987655", "0"]
    (proc_root / str(pid) / "stat").write_text(
        f"{pid} (unrelated) " + " ".join(fields) + "\n",
        encoding="ascii",
    )
    (proc_root / str(pid) / "cmdline").write_bytes(
        b"/usr/bin/python3\0unrelated.py\0"
    )

    process_record.collect_dead_record(record, **common)

    assert not record.exists()
    assert (proc_root / str(pid) / "stat").exists()


def _dead_worker_with_socket_path(tmp_path: Path):
    pid, python, socket, record, argv, proc_root = _fixture(tmp_path)
    socket = tmp_path / "w.sock"
    argv[-1] = str(socket)
    (proc_root / str(pid) / "cmdline").write_bytes(b"\0".join(s.encode() for s in argv))
    common = dict(
        python=python, socket=socket, source_sha="a" * 40, source_tree="b" * 40,
        worker_lock_sha256="sha256:" + "c" * 64, session_id="e" * 32, proc_root=proc_root,
    )
    process_record.create_record(record, pid=pid, expected_argv=argv, **common)
    for child in (proc_root / str(pid)).iterdir():
        child.unlink()
    (proc_root / str(pid)).rmdir()
    return socket, record, common


@pytest.mark.parametrize("listening", [False, True])
def test_dead_worker_collection_removes_only_an_unlistened_socket(
    tmp_path: Path, listening: bool,
) -> None:
    socket, record, common = _dead_worker_with_socket_path(tmp_path)
    with unix_socket.socket(unix_socket.AF_UNIX) as listener:
        listener.bind(str(socket))
        socket.chmod(0o600)
        if listening:
            listener.listen()
            with pytest.raises(process_record.WorkerProcessRecordError, match="listener"):
                process_record.collect_dead_record(record, collect_socket=True, **common)
            assert record.exists() and socket.exists()
        else:
            process_record.collect_dead_record(record, collect_socket=True, **common)
            assert not record.exists() and not socket.exists()


def test_dead_worker_collection_allows_only_atime_change_during_liveness_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket, record, common = _dead_worker_with_socket_path(tmp_path)
    original_lstat = Path.lstat
    observations = []

    def lstat(path):
        metadata = original_lstat(path)
        if path != socket:
            return metadata
        observations.append(metadata)
        if len(observations) == 1:
            return metadata
        before = observations[0]
        values = list(before)
        values[7] = before.st_atime + 1
        # Model an atime update deterministically, independent of mount flags
        # and whether the test happens within the socket creation clock tick.
        return os.stat_result(values, {"st_atime_ns": before.st_atime_ns + 1_000_000_000,
                                       "st_mtime_ns": before.st_mtime_ns,
                                       "st_ctime_ns": before.st_ctime_ns})

    with unix_socket.socket(unix_socket.AF_UNIX) as stale:
        stale.bind(str(socket))
        socket.chmod(0o600)
        monkeypatch.setattr(Path, "lstat", lstat)
        process_record.collect_dead_record(record, collect_socket=True, **common)
    assert len(observations) == 2
    assert not record.exists() and not socket.exists()


@pytest.mark.parametrize("change", ["replacement", "permissions", "ctime_ns"])
def test_dead_worker_collection_refuses_socket_changes_after_liveness_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    socket, record, common = _dead_worker_with_socket_path(tmp_path)
    original_load = process_record.load_record
    original_lstat = Path.lstat
    loads = 0

    with unix_socket.socket(unix_socket.AF_UNIX) as stale, unix_socket.socket(unix_socket.AF_UNIX) as replacement:
        stale.bind(str(socket))
        socket.chmod(0o600)
        original_inode = socket.lstat().st_ino

        def load(path):
            nonlocal loads
            value = original_load(path)
            loads += 1
            if loads == 2:
                if change == "replacement":
                    # Keep the first inode alive to prevent immediate reuse.
                    socket.unlink()
                    replacement.bind(str(socket))
                    socket.chmod(0o600)
                elif change == "permissions":
                    socket.chmod(0o640)
            return value

        def lstat(path):
            metadata = original_lstat(path)
            if path == socket and loads == 2 and change == "ctime_ns":
                return os.stat_result(metadata, {"st_mtime_ns": metadata.st_mtime_ns,
                                                 "st_ctime_ns": metadata.st_ctime_ns + 1})
            return metadata

        monkeypatch.setattr(process_record, "load_record", load)
        monkeypatch.setattr(Path, "lstat", lstat)
        with pytest.raises(process_record.WorkerProcessRecordError, match="socket changed"):
            process_record.collect_dead_record(record, collect_socket=True, **common)
        assert record.exists() and socket.exists()
        if change == "replacement":
            assert socket.lstat().st_ino != original_inode
        elif change == "permissions":
            assert stat.S_IMODE(socket.stat().st_mode) == 0o640


def test_dead_worker_collection_preserves_unexpected_socket_file(tmp_path: Path) -> None:
    pid, python, socket, record, argv, proc_root = _fixture(tmp_path)
    common = dict(
        python=python, socket=socket, source_sha="a" * 40, source_tree="b" * 40,
        worker_lock_sha256="sha256:" + "c" * 64, session_id="e" * 32, proc_root=proc_root,
    )
    process_record.create_record(record, pid=pid, expected_argv=argv, **common)
    for child in (proc_root / str(pid)).iterdir():
        child.unlink()
    (proc_root / str(pid)).rmdir()
    socket.parent.mkdir()
    socket.write_text("unrelated file")
    with pytest.raises(process_record.WorkerProcessRecordError, match="unsafe"):
        process_record.collect_dead_record(record, collect_socket=True, **common)
    assert record.exists() and socket.read_text() == "unrelated file"
