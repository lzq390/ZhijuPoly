"""Log tails must stay bounded and belong to the exact authorized execution."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

import pytest

from app.services import polymer_md_logs as logs
from app.services.polymer_md_repository import PolymerMdError


@pytest.fixture
def case(tmp_path):
    job = {"job_id": "339ca785-f3d6-4d5f-b3e4-dd2d0785620b", "attempt_token": "a" * 64,
           "owner_user_id": "owner", "request_sha256": "b" * 64, "enqueue_sequence": 1,
           "worker_instance_id": "c" * 32, "status": "running", "stage": "eq1",
           "artifact_state": "available", "scientific_request": {"tc_axis": "x"},
           "stage_runs": [{"stage_run_id": "eq1-1", "stage": "eq1", "attempt": 1, "status": "completed"}],
           "artifacts": []}
    return tmp_path, job


def put(case, relative, content=b"native warning\n", *, manifest=True, kind="log"):
    root, job = case
    content = content.encode() if isinstance(content, str) else content
    ref = "/".join([job["job_id"], job["attempt_token"], relative])
    path = root / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if manifest:
        job["artifacts"].append({"artifact_id": str(len(job["artifacts"])), "stage_run_id": job["stage_runs"][0]["stage_run_id"],
                                 "attempt_token": job["attempt_token"], "storage_ref": ref,
                                 "filename": path.name, "kind": kind, "size_bytes": len(content),
                                 "sha256": hashlib.sha256(content).hexdigest()})
    return path


def running(case, *, stage="eq1", run_id=None):
    root, job = case
    run_id = run_id or stage + "-1"
    job["stage"] = stage
    job["stage_runs"] = [{"stage_run_id": run_id, "stage": stage, "attempt": 1, "status": "running",
                          "started_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}]
    value = {"request": {k: job[k] for k in ("job_id", "attempt_token", "owner_user_id", "request_sha256", "enqueue_sequence")},
             "snapshot": {**{k: job[k] for k in ("job_id", "attempt_token", "worker_instance_id", "status", "stage")},
                          "stage_runs": deepcopy(job["stage_runs"])},
             "running_scope": {"stage_run_id": run_id, "unit": "science.scope", "entry_pid": 123,
                               "entry_start_ticks": 456, "cgroup_identity": {"device": 29, "inode": 123}}}
    path = root / job["job_id"] / job["attempt_token"] / "journal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path, value


def test_completed_auto_uses_manifest_engine_and_preserves_warnings(case):
    put(case, "science/eq1/engine.log", "Warning: dangerous builds\nStep Temp\n100 300\n")
    put(case, "stages/eq1-1/runner.log", "RadonPy starting\n")
    value = logs.read_stage_logs(*case, "eq1-1")
    assert value["lines"] == ["Warning: dangerous builds", "Step Temp", "100 300"]
    assert value["resolved_source"] == "engine" and value["source_label"] == "engine.log"
    assert value["available_sources"] == [{"source": "engine", "label": "engine.log"}, {"source": "runner", "label": "runner.log"}]
    assert value["available"] and not value["truncated"]
    assert value["byte_limit"] == 262144 and value["tail_lines"] == 200
    assert value["read_at"].endswith("+00:00")


def test_explicit_source_never_silently_switches(case):
    put(case, "stages/eq1-1/runner.log", "runner\n")
    assert logs.read_stage_logs(*case, "eq1-1", source="runner")["lines"] == ["runner"]
    missing = logs.read_stage_logs(*case, "eq1-1", source="engine")
    assert not missing["available"] and missing["lines"] == []
    assert missing["resolved_source"] is None and missing["source_label"] is None


def test_completed_unregistered_live_directory_is_not_a_source(case):
    put(case, "science/eq1/engine.log", "belongs to another retry", manifest=False)
    assert not logs.read_stage_logs(*case, "eq1-1")["available"]


def test_failed_retry_reads_only_its_sealed_diagnostics(case):
    case[1]["stage_runs"][0]["status"] = "failed"
    put(case, "science/eq1/engine.log", "NEW RETRY MUST NOT LEAK\n", manifest=False)
    put(case, "stages/eq1-1/failure-diagnostics/012345abcdef/engine.log.tail.log", "old run failure\n", kind="failure_log")
    put(case, "stages/eq1-1/runner.log", "old runner\n")
    value = logs.read_stage_logs(*case, "eq1-1")
    assert value["lines"] == ["old run failure"] and value["truncated"]
    assert value["source_label"] == "engine.log.tail.log"


@pytest.mark.parametrize("stage,first,fallback", [
    ("sp", "engine.log", "sample.log"), ("tg", "engine.log", "high_temperature.log"),
    ("tc", "nemd_TC-MP_x.log", None), ("additional_2", "engine.log", None),
])
def test_protocol_log_names_and_formal_precedence(case, stage, first, fallback):
    root, job = case
    run_id = stage + "-1"
    job["stage_runs"][0].update(stage=stage, stage_run_id=run_id)
    if fallback:
        put(case, f"science/{stage}/{fallback}", "initial segment\n")
        assert logs.read_stage_logs(root, job, run_id)["source_label"] == fallback
    put(case, f"science/{stage}/{first}", "formal segment\n")
    assert logs.read_stage_logs(root, job, run_id)["source_label"] == first


@pytest.mark.parametrize("stage", ["build", "monomer_qm", "terminal_qm", "analyze_0", "tc_analyze", "sp_analyze", "tg_analyze"])
def test_non_engine_stages_use_runner_only(case, stage):
    root, job = case
    run_id = stage + "-1"
    job["stage_runs"][0].update(stage=stage, stage_run_id=run_id)
    put(case, f"science/{stage}/engine.log", "unrelated engine\n")
    put(case, f"stages/{run_id}/runner.log", "correct runner\n")
    value = logs.read_stage_logs(root, job, run_id)
    assert value["lines"] == ["correct runner"] and value["resolved_source"] == "runner"


@pytest.mark.parametrize("stage,basename", [("eq1", "engine.stdout"), ("sp", "rerun.stdout"),
                                           ("tg", "step-wise.stdout"), ("tc", "engine.stdout")])
def test_explicit_stdout(case, stage, basename):
    case[1]["stage_runs"][0].update(stage=stage, stage_run_id=stage + "-1")
    put(case, f"science/{stage}/{basename}", "native stdout\n")
    value = logs.read_stage_logs(*case, stage + "-1", source="stdout")
    assert value["lines"] == ["native stdout"] and value["source_label"] == basename


def test_running_log_allows_append_with_same_execution_identity(case, monkeypatch):
    journal, record = running(case)
    path = put(case, "science/eq1/engine.log", "start\n", manifest=False)
    read = logs._read_tail

    def appended(fd, candidate, tail_lines):
        with path.open("ab") as stream:
            stream.write(b"later\n")
        # Progress revisions are deliberately outside the execution fence.
        record["snapshot"]["revision"] = 88
        journal.write_text(json.dumps(record))
        return read(fd, candidate, tail_lines)

    monkeypatch.setattr(logs, "_read_tail", appended)
    assert logs.read_stage_logs(*case, "eq1-1")["lines"] == ["start", "later"]


@pytest.mark.parametrize("field,value", [("attempt_token", "d" * 64), ("worker_instance_id", "d" * 32),
                                         ("stage", "eq2")])
def test_running_snapshot_must_match_current_attempt_worker_and_stage(case, field, value):
    path, record = running(case)
    record["snapshot"][field] = value
    path.write_text(json.dumps(record))
    put(case, "science/eq1/engine.log", manifest=False)
    with pytest.raises(PolymerMdError, match="身份已变化") as error:
        logs.read_stage_logs(*case, "eq1-1")
    assert error.value.status == 409


def test_running_scope_change_during_read_rejects_the_tail(case, monkeypatch):
    path, record = running(case)
    put(case, "science/eq1/engine.log", manifest=False)
    read = logs._read_tail

    def changed(fd, candidate, tail_lines):
        result = read(fd, candidate, tail_lines)
        record["running_scope"]["entry_start_ticks"] += 1
        path.write_text(json.dumps(record))
        return result

    monkeypatch.setattr(logs, "_read_tail", changed)
    with pytest.raises(PolymerMdError) as error:
        logs.read_stage_logs(*case, "eq1-1")
    assert error.value.code == "stage_log_changed"


def test_old_running_retry_cannot_read_reused_science_directory(case):
    path, record = running(case)
    record["snapshot"]["stage_runs"].append({"stage_run_id": "eq1-2", "stage": "eq1", "attempt": 2, "status": "running"})
    path.write_text(json.dumps(record))
    put(case, "science/eq1/engine.log", "new retry\n", manifest=False)
    with pytest.raises(PolymerMdError) as error:
        logs.read_stage_logs(*case, "eq1-1")
    assert error.value.status == 409


def test_retry_launch_intent_does_not_expose_previous_runs_log(case):
    path = put(case, "science/eq1/engine.log", "OLD RUN\n", manifest=False)
    os.utime(path, (1000, 1000))
    running(case)
    put(case, "stages/eq1-1/runner.log", "new run starting\n", manifest=False)
    value = logs.read_stage_logs(*case, "eq1-1")
    assert value["lines"] == ["new run starting"] and value["resolved_source"] == "runner"
    assert value["available_sources"] == [{"source": "runner", "label": "runner.log"}]


@pytest.mark.parametrize("change", ["attempt", "traversal", "different_run", "size", "filename"])
def test_manifest_is_not_a_path_or_attempt_bypass(case, change):
    put(case, "science/eq1/engine.log")
    artifact = case[1]["artifacts"][0]
    if change == "attempt":
        artifact["attempt_token"] = "d" * 64
    elif change == "traversal":
        artifact["storage_ref"] = artifact["storage_ref"].replace("science/eq1/", "science/../")
    elif change == "different_run":
        artifact["storage_ref"] = artifact["storage_ref"].replace("science/eq1/", "science/eq2/")
    elif change == "size":
        artifact["size_bytes"] += 1
    else:
        artifact["storage_ref"] = artifact["storage_ref"].replace("engine.log", "private.log")
    with pytest.raises(PolymerMdError) as error:
        logs.read_stage_logs(*case, "eq1-1")
    assert error.value.status == 502


@pytest.mark.parametrize("level", ["root", "job", "attempt", "science", "stage", "file"])
def test_symlinks_are_rejected_at_every_path_component(case, level):
    root, job = case
    path = put(case, "science/eq1/engine.log", "private outside data\n")
    selected = {"root": root, "job": root / job["job_id"], "attempt": root / job["job_id"] / job["attempt_token"],
                "science": path.parent.parent, "stage": path.parent, "file": path}[level]
    original = selected.with_name(selected.name + "-real")
    selected.rename(original)
    selected.symlink_to(original, target_is_directory=original.is_dir())
    with pytest.raises(PolymerMdError) as error:
        logs.read_stage_logs(root, job, "eq1-1")
    assert error.value.status == 502


def test_fifo_is_rejected_without_blocking(case):
    path = put(case, "science/eq1/engine.log")
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(PolymerMdError) as error:
        logs.read_stage_logs(*case, "eq1-1")
    assert error.value.status == 502


def test_tail_budget_and_line_limit_do_not_read_or_hash_the_whole_log(case, monkeypatch):
    put(case, "science/eq1/engine.log", b"prefix\n" * 60000 + b"penultimate\nlast\n")
    read = logs.os.read
    requested = []

    def bounded(fd, amount):
        requested.append(amount)
        return read(fd, amount)

    monkeypatch.setattr(logs.os, "read", bounded)
    value = logs.read_stage_logs(*case, "eq1-1", tail_lines=2)
    assert value["lines"] == ["penultimate", "last"] and value["truncated"]
    assert sum(requested) <= logs.BYTE_LIMIT


@pytest.mark.parametrize("live", [False, True])
def test_append_during_read_is_only_allowed_for_live_logs(case, monkeypatch, live):
    if live:
        running(case)
    path = put(case, "science/eq1/engine.log", "original\n", manifest=not live)
    inode, read, changed = path.stat().st_ino, logs.os.read, False

    def append_once(fd, amount):
        nonlocal changed
        value = read(fd, amount)
        if not changed and os.fstat(fd).st_ino == inode:
            changed = True
            with path.open("ab") as stream:
                stream.write(b"next row\n")
        return value

    monkeypatch.setattr(logs.os, "read", append_once)
    if live:
        assert logs.read_stage_logs(*case, "eq1-1")["lines"] == ["original"]
    else:
        with pytest.raises(PolymerMdError) as error:
            logs.read_stage_logs(*case, "eq1-1")
        assert error.value.status == 502


def test_paths_credentials_and_invalid_utf8_are_sanitized_without_removing_warning(case):
    root, job = case
    put(case, "science/eq1/engine.log", f"Warning: {root}/secret {job['attempt_token']} /opt/engine/lib\nAuthorization: Bearer abc123\n".encode() + b"bad \xff\n")
    value = logs.read_stage_logs(*case, "eq1-1")
    text = "\n".join(value["lines"])
    assert text.startswith("Warning:") and "[private]" in text and "\ufffd" in text
    assert str(root) not in text and job["attempt_token"] not in text and "abc123" not in text and "/opt/engine" not in text


@pytest.mark.parametrize("kwargs", [{"source": "../../private"}, {"tail_lines": 0}, {"tail_lines": 1001}, {"tail_lines": True}])
def test_invalid_requests_are_rejected_before_reading(case, kwargs):
    with pytest.raises(PolymerMdError) as error:
        logs.read_stage_logs(*case, "eq1-1", **kwargs)
    assert error.value.status == 422


def test_deleted_or_unknown_stage_does_not_read_files(case):
    case[1]["artifact_state"] = "deleting"
    with pytest.raises(PolymerMdError) as error:
        logs.read_stage_logs(*case, "eq1-1")
    assert error.value.status == 404


def test_live_journal_has_a_bounded_read_budget(case, monkeypatch):
    path, _ = running(case)
    monkeypatch.setattr(logs, "JOURNAL_LIMIT", 10)
    with pytest.raises(PolymerMdError) as error:
        logs.read_stage_logs(*case, "eq1-1")
    assert path.stat().st_size > 10 and error.value.status == 502
