from __future__ import annotations

import json
import os
from pathlib import Path
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
