from copy import deepcopy
from collections import Counter
import json
import os
from pathlib import Path
import threading
from uuid import UUID

import pytest

from server_monitoring import task_history as history
from server_monitoring.task_history_models import validate_task_history


NOW = 1_800_000_000.0
GPU = "GPU-test-1"
WORKER = "a" * 32
ATTEMPT = "b" * 64


def journal(job="private-job-value", *, finish=NOW - 10, gpu=GPU, index="1"):
    return {"journal_schema_version": 2, "enqueue_sequence_source": "backend", "enqueue_sequence": 1,
        "snapshot": {"schema_version": 2, "job_id": job, "attempt_token": ATTEMPT, "worker_instance_id": WORKER,
            "status": "completed", "enqueue_sequence": 1, "created_at": history.iso(finish - 20),
            "started_at": history.iso(finish - 10), "finished_at": history.iso(finish),
            "result": {"schema_version": 2, "calculation_type": "single_point", "input": "private-input-never-return",
                "provenance": {"worker_instance_id": WORKER, "gpu_uuid": gpu, "physical_gpu": index,
                    "gpu_physical_device": index, "logical_device": "cuda:0", "gpu_logical_device": "cuda:0",
                    "visible_gpu_count": 1, "execution_path": "primary", "lease_id": "private-lease",
                    "broker_instance_id": "private-broker", "fencing_token": 4}}}}


def write_journal(root, payload, attempt=ATTEMPT):
    job = root / payload["snapshot"]["job_id"]
    job.mkdir(mode=0o700, exist_ok=True)
    directory = job / attempt
    directory.mkdir(mode=0o700, exist_ok=True)
    payload = deepcopy(payload)
    payload["snapshot"]["attempt_token"] = attempt
    path = directory / "journal.json"
    history.atomic_json(path, payload)
    return path


@pytest.fixture
def setup(tmp_path):
    state = tmp_path / "state"; state.mkdir(mode=0o700)
    roots = {name: tmp_path / name for name in history.SOURCE_ROOTS}
    for root in roots.values(): root.mkdir(mode=0o700)
    snapshot = {"schema_version": 1, "node_id": "local-server", "collector_id": "92b0eed0-e735-4b58-863b-9f04b3a18015",
        "started_at": history.iso(NOW - 60), "captured_at": history.iso(NOW), "hostname": "test", "sequence": 1,
        "logical_cpu_count": 4, "uptime_seconds": None, "load_average": None, "cpu": None, "memory": None, "disks": [],
        "gpus": [{"index": 1, "uuid": GPU, "name": "Test GPU", "utilization_percent": None, "memory_total_bytes": None,
                  "memory_used_bytes": None, "temperature_celsius": None, "power_watts": None}],
        "components": {name: {"status": "ok", "last_success_at": history.iso(NOW), "error_code": None}
                       for name in ("cpu", "memory", "disks", "gpus")}}
    history.atomic_json(state / "latest.json", snapshot)
    def no_md(inventory, clock):
        return [{"source_id": name, "status": "ready", "matched_tasks": 0, "excluded_tasks": 0, "error_code": None}
                for name in history.SOURCE_IDS if name not in roots], []
    def no_activity(inventory, clock):
        return {"captured_at": history.iso(clock()), "status": "ready", "error_code": None, "items": []}
    return history.TaskHistoryCollector(state, sources=roots, clock=lambda: NOW,
                                       md_reader=no_md, activity_reader=no_activity), roots, state


def test_projects_only_allowlisted_fields_from_actual_execution_evidence(setup):
    collector, roots, _ = setup
    write_journal(roots["development_dft"], journal())
    result = collector.sample(force_history=True)
    assert result["status"] == "ready"
    assert result["sources"][0]["matched_tasks"] == 1
    task = result["tasks"][0]
    assert set(task) == {"id", "gpu_uuid", "module", "environment", "calculation_type", "status", "started_at", "finished_at", "duration_seconds"}
    assert task["gpu_uuid"] == GPU and task["duration_seconds"] == 10
    assert task["environment"] == "development"
    assert len(task["id"]) == 20
    assert all(private not in json.dumps(result) for private in ("private-job-value", "private-input", "private-lease", "private-broker", WORKER, ATTEMPT))


def test_inventory_accepts_optional_host_info_without_copying_it_to_gpu_tasks(setup):
    collector, roots, state = setup
    write_journal(roots["development_dft"], journal())
    path = state / "latest.json"
    snapshot = json.loads(path.read_text())
    snapshot["host_info"] = {"cpu_model": "Example CPU", "cpu_socket_count": 2, "cpu_physical_core_count": 64,
                             "os_name": "Example Linux", "kernel_release": "6.8.0-test", "nvidia_driver_version": "535.288.01"}
    history.atomic_json(path, snapshot)
    result = collector.sample()
    assert result["status"] == "ready" and len(result["tasks"]) == 1
    assert result["gpu_uuids"] == [GPU] and "host_info" not in result


def test_keeps_latest_ten_per_gpu_and_namespaces_same_job_in_different_sources(setup):
    collector, roots, _ = setup
    for index in range(12): write_journal(roots["development_dft"], journal(f"job-{index}", finish=NOW - 100 + index))
    write_journal(roots["production_dft"], journal("job-11", finish=NOW - 1))
    result = collector.sample(force_history=True)
    assert len(result["tasks"]) == 10
    assert [source["matched_tasks"] for source in result["sources"][:2]] == [12, 1]
    assert result["tasks"][0]["environment"] == "production"
    assert len({task["id"] for task in result["tasks"]}) == 10
    assert Counter(task["environment"] for task in result["tasks"]) == {"development": 9, "production": 1}


def test_repeated_attempts_of_same_source_job_use_latest_completed_execution(setup):
    collector, roots, _ = setup
    write_journal(roots["development_dft"], journal(finish=NOW - 30))
    write_journal(roots["development_dft"], journal(finish=NOW - 5), attempt="c" * 64)
    result = collector.sample(force_history=True)
    assert len(result["tasks"]) == 1 and result["tasks"][0]["finished_at"] == history.iso(NOW - 5)
    assert result["sources"][0]["matched_tasks"] == 1 and result["sources"][0]["excluded_tasks"] == 1


@pytest.mark.parametrize("reason", ["cancelled", "legacy", "unmapped", "missing_uuid", "missing_lease", "boolean_count"])
def test_unverified_records_are_excluded_without_inventing_gpu_placement(setup, reason):
    collector, roots, _ = setup
    data = journal(); snapshot = data["snapshot"]; provenance = snapshot["result"]["provenance"]
    if reason == "cancelled": snapshot["status"] = "cancelled"; snapshot["result"] = None
    elif reason == "legacy": data["enqueue_sequence_source"] = "legacy_mapping"
    elif reason == "unmapped": provenance["gpu_uuid"] = "GPU-other"
    elif reason == "missing_uuid": provenance.pop("gpu_uuid")
    elif reason == "missing_lease": provenance.pop("lease_id")
    else: provenance["visible_gpu_count"] = True
    write_journal(roots["development_dft"], data)
    result = collector.sample(force_history=True)
    assert result["status"] == "ready" and result["tasks"] == []
    assert result["sources"][0]["excluded_tasks"] == 1


@pytest.mark.parametrize("reason", ["future", "inverted", "naive", "index", "worker", "sequence", "malformed", "oversize", "permissions", "symlink", "hardlink", "fifo"])
def test_invalid_or_unsafe_record_marks_partial_while_other_records_remain(setup, reason, tmp_path):
    collector, roots, _ = setup
    write_journal(roots["development_dft"], journal("good-job"))
    data = journal(); snapshot = data["snapshot"]
    if reason == "future": snapshot["finished_at"] = history.iso(NOW + 1)
    elif reason == "inverted": snapshot["started_at"] = history.iso(NOW)
    elif reason == "naive": snapshot["finished_at"] = "2026-11-11T00:00:00"
    elif reason == "index": snapshot["result"]["provenance"]["physical_gpu"] = "0"
    elif reason == "worker": snapshot["worker_instance_id"] = "c" * 32
    elif reason == "sequence": snapshot["enqueue_sequence"] = 2
    path = write_journal(roots["development_dft"], data)
    if reason == "malformed": path.write_text("{bad JSON")
    elif reason == "oversize": path.write_bytes(b" " * (history.MAX_JOURNAL_BYTES + 1))
    elif reason == "permissions": path.chmod(0o644)
    elif reason in {"symlink", "hardlink"}:
        other = tmp_path / "other.json"; history.atomic_json(other, data); path.unlink()
        path.symlink_to(other) if reason == "symlink" else os.link(other, path)
    elif reason == "fifo": path.unlink(); os.mkfifo(path, 0o600)
    result = collector.sample(force_history=True)
    assert result["status"] == "partial"
    assert len(result["tasks"]) == 1
    assert result["sources"][0]["status"] == "partial" and result["sources"][0]["error_code"]


def test_missing_sources_are_distinct_from_a_successful_empty_scan(setup):
    collector, roots, _ = setup
    assert collector.sample(force_history=True)["status"] == "ready"
    roots["development_dft"].rmdir()
    assert collector.sample(force_history=True)["status"] == "partial"
    roots["production_dft"].rmdir()
    result = collector.sample(force_history=True)
    assert result["status"] == "partial" and result["tasks"] == []
    assert all(source["status"] == "partial" and source["error_code"] == "source_unavailable" for source in result["sources"][:2])


@pytest.mark.parametrize("reason", ["missing", "stale", "future", "partial_gpu"])
def test_requires_fresh_valid_current_gpu_inventory(setup, reason):
    collector, roots, state = setup
    write_journal(roots["development_dft"], journal())
    path = state / "latest.json"
    if reason == "missing": path.unlink()
    else:
        data = json.loads(path.read_text())
        if reason == "stale":
            data["started_at"] = history.iso(NOW - 100)
            data["captured_at"] = history.iso(NOW - 61)
            for component in data["components"].values(): component["last_success_at"] = data["captured_at"]
        elif reason == "future": data["captured_at"] = history.iso(NOW + 1)
        else: data["components"]["gpus"]["status"] = "unavailable"
        history.atomic_json(path, data)
    result = collector.sample(force_history=True)
    assert result["status"] == "unavailable" and result["tasks"] == [] and result["gpu_uuids"] == []
    assert {source["error_code"] for source in result["sources"]} == {"gpu_inventory_unavailable"}


def test_bounded_directory_and_journal_scans_report_partial(setup, monkeypatch):
    collector, roots, _ = setup
    for index in range(3): write_journal(roots["development_dft"], journal(f"job-{index}"))
    monkeypatch.setattr(history, "MAX_JOURNALS", 1)
    result = collector.sample(force_history=True)
    assert result["sources"][0]["error_code"] == "scan_limit" and len(result["tasks"]) == 1
    monkeypatch.setattr(history, "MAX_DIRECTORY_ENTRIES", 1)
    result = collector.sample(force_history=True)
    assert result["sources"][0]["error_code"] == "scan_limit" and result["tasks"] == []


def test_malformed_json_still_consumes_total_read_budget(setup, monkeypatch):
    collector, roots, _ = setup
    for index in range(3): write_journal(roots["development_dft"], journal(f"job-{index}")).write_text("bad" * 10)
    monkeypatch.setattr(history, "MAX_SOURCE_BYTES", 30)
    result = collector.sample(force_history=True)
    assert result["sources"][0]["error_code"] == "scan_limit"
    assert result["sources"][0]["excluded_tasks"] == 1


@pytest.mark.parametrize("replace_stat", [1, 2])
def test_atomic_replacement_keeps_old_complete_open_file(tmp_path, monkeypatch, replace_stat):
    path = tmp_path / "journal.json"; staged = tmp_path / "replacement.json"
    history.atomic_json(path, {"version": "old"}); history.atomic_json(staged, {"version": "new"})
    original = os.fstat; calls = 0
    def replace(fd):
        nonlocal calls
        calls += 1
        if calls == replace_stat: os.replace(staged, path)
        return original(fd)
    monkeypatch.setattr(history.os, "fstat", replace)
    assert history._read_json(path, 100)[0] == {"version": "old"}


def test_in_place_change_during_read_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "journal.json"; history.atomic_json(path, {"version": "old"})
    original = os.fstat; calls = 0
    def rewrite(fd):
        nonlocal calls
        calls += 1
        if calls == 2: path.write_text('{"version":"changed"}')
        return original(fd)
    monkeypatch.setattr(history.os, "fstat", rewrite)
    with pytest.raises(ValueError): history._read_json(path, 100)


def test_once_writes_private_atomic_contract_without_changing_source(setup):
    collector, roots, state = setup
    source = write_journal(roots["development_dft"], journal())
    before = source.read_bytes()
    collector.run(threading.Event(), once=True)
    target = state / "gpu-tasks.json"
    result = validate_task_history(json.loads(target.read_text()))
    assert result.status == "ready" and len(result.tasks) == 1
    assert target.stat().st_mode & 0o777 == 0o600 and source.read_bytes() == before
    assert not list(state.glob(".gpu-tasks-*"))
    assert UUID(result.collector_id)


@pytest.mark.parametrize("change", ["extra_sensitive", "unknown_gpu", "duplicate", "duration", "future", "status"])
def test_public_contract_rejects_inconsistent_or_unexpected_data(setup, change):
    collector, roots, _ = setup; write_journal(roots["development_dft"], journal())
    result = collector.sample(force_history=True)
    if change == "extra_sensitive": result["tasks"][0]["owner_user_id"] = "should-never-return"
    elif change == "unknown_gpu": result["gpu_uuids"] = []
    elif change == "duplicate": result["tasks"].append(deepcopy(result["tasks"][0]))
    elif change == "duration": result["tasks"][0]["duration_seconds"] = 1
    elif change == "future": result["captured_at"] = history.iso(NOW - 50)
    else: result["status"] = "unavailable"
    with pytest.raises(ValueError): validate_task_history(result)


def test_historical_uuid_survives_physical_index_reordering(setup):
    collector, roots, _ = setup
    write_journal(roots["development_dft"], journal(index="0"))
    result = collector.sample()
    assert result["status"] == "ready" and result["tasks"][0]["gpu_uuid"] == GPU


def test_fixed_production_cohorts_deduplicate_jobs_and_keep_latest_completion(setup, tmp_path):
    collector, roots, _ = setup
    previous = roots["production_dft"]
    cohort = tmp_path / "production_cohort"; cohort.mkdir(mode=0o700)
    write_journal(previous, journal(finish=NOW - 20))
    write_journal(cohort, journal(finish=NOW - 10))
    write_journal(cohort, journal("new-job", finish=NOW - 1))
    collector.sources["production_dft"] = (previous, cohort)
    result = collector.sample()
    source = result["sources"][1]
    assert source["matched_tasks"] == 2 and source["excluded_tasks"] == 1 and source["status"] == "ready"
    assert len(result["tasks"]) == 2 and result["tasks"][1]["finished_at"] == history.iso(NOW - 10)


def update_inventory(state, now, *, gpu=GPU, index=1):
    path = state / "latest.json"
    data = json.loads(path.read_text())
    data["started_at"] = history.iso(min(NOW - 60, now))
    data["captured_at"] = history.iso(now)
    data["gpus"][0].update(uuid=gpu, index=index)
    for component in data["components"].values(): component["last_success_at"] = data["captured_at"]
    history.atomic_json(path, data)


def test_history_refreshes_every_sixty_seconds_activity_every_sample(setup):
    collector, roots, state = setup
    now = [NOW]; mono = [0.0]; calls = Counter()
    collector.clock = lambda: now[0]; collector.monotonic = lambda: mono[0]
    original_md, original_activity = collector.md_reader, collector.activity_reader
    def md(inventory, clock):
        calls["history"] += 1
        return original_md(inventory, clock)
    def activity(inventory, clock):
        calls["activity"] += 1
        return original_activity(inventory, clock)
    collector.md_reader, collector.activity_reader = md, activity
    initial = collector.sample()
    write_journal(roots["development_dft"], journal())
    now[0] += 5; mono[0] += 5; update_inventory(state, now[0])
    cached = collector.sample()
    assert cached["tasks"] == [] and cached["history_captured_at"] == initial["history_captured_at"]
    assert cached["captured_at"] != initial["captured_at"] and calls == {"history": 1, "activity": 2}
    now[0] += 55; mono[0] += 55; update_inventory(state, now[0])
    refreshed = collector.sample()
    assert len(refreshed["tasks"]) == 1 and calls == {"history": 2, "activity": 3}
    assert refreshed["history_captured_at"] == history.iso(now[0])


def test_inventory_change_invalidates_cached_history_before_next_interval(setup):
    collector, roots, state = setup
    write_journal(roots["development_dft"], journal())
    assert len(collector.sample()["tasks"]) == 1
    update_inventory(state, NOW, gpu="GPU-replacement", index=0)
    result = collector.sample()
    assert result["tasks"] == [] and result["gpu_uuids"] == ["GPU-replacement"]
    assert result["sources"][0]["excluded_tasks"] == 1


def test_unavailable_activity_does_not_erase_verified_completed_history(setup):
    collector, roots, _ = setup
    write_journal(roots["development_dft"], journal())
    collector.activity_reader = lambda inventory, clock: {"captured_at": history.iso(clock()), "status": "unavailable", "error_code": "nvidia_unavailable", "items": []}
    result = collector.sample()
    assert result["status"] == "ready" and len(result["tasks"]) == 1
    assert result["activity"]["status"] == "unavailable"


def test_unavailable_history_does_not_erase_observed_activity(setup):
    collector, roots, _ = setup
    roots["development_dft"].rmdir(); roots["production_dft"].rmdir()
    def md(inventory, clock):
        return [source for source in history._unavailable_sources("database_unavailable") if source["source_id"] not in roots], []
    collector.md_reader = md
    result = collector.sample()
    assert result["status"] == "unavailable" and result["activity"]["status"] == "ready"


def test_clock_rollback_during_activity_drops_future_history_without_relabelling_it_current(setup):
    collector, roots, state = setup
    now = [NOW]; collector.clock = lambda: now[0]
    write_journal(roots["development_dft"], journal(finish=NOW - 1))
    def rollback(inventory, clock):
        captured = clock(); now[0] -= 20
        return {"captured_at": history.iso(captured), "status": "ready", "error_code": None, "items": []}
    collector.activity_reader = rollback
    result = collector.sample()
    assert result["tasks"] == [] and result["sources"][0]["error_code"] == "clock_changed"
    assert result["activity"]["status"] == "unavailable" and result["activity"]["error_code"] == "clock_changed"
    assert result["history_captured_at"] == result["captured_at"] == history.iso(NOW - 20)
    assert result["started_at"] <= result["captured_at"]


def test_all_modules_share_latest_ten_per_gpu_in_global_completion_order(setup):
    collector, roots, _ = setup
    for index in range(12): write_journal(roots["development_dft"], journal(f"dft-{index}", finish=NOW - 100 + index))
    def md(inventory, clock):
        sources = [{"source_id": name, "status": "ready", "matched_tasks": 12 if name == "development_md" else 0,
                    "excluded_tasks": 0, "error_code": None} for name in history.SOURCE_IDS if name not in roots]
        tasks = []
        for index in range(12):
            finish = NOW - (12 - index) if index >= 9 else NOW - 1000 + index
            tasks.append({"id": f"{index:020x}", "gpu_uuid": GPU, "module": "monomer_md", "environment": "development",
                          "calculation_type": "md_demo", "status": "completed", "started_at": history.iso(finish - 1000),
                          "finished_at": history.iso(finish), "duration_seconds": 1000.0})
        return sources, tasks
    collector.md_reader = md
    result = collector.sample()
    assert Counter(task["module"] for task in result["tasks"]) == {"monomer_dft": 7, "monomer_md": 3}
    assert all(task["module"] == "monomer_md" for task in result["tasks"][:3])
    assert all(task["module"] == "monomer_dft" for task in result["tasks"][3:])
    assert result["tasks"][0]["id"] == f"{11:020x}"
    extra = deepcopy(result["tasks"][-1]); extra["id"] = "f" * 20
    result["tasks"].append(extra)
    with pytest.raises(ValueError): validate_task_history(result)


@pytest.mark.parametrize("change", ["task_service_label", "unknown_task_environment", "wrong_status", "unknown_gpu", "future_started", "secret"])
def test_activity_contract_cannot_mislabel_process_residency_as_work_or_return_private_fields(setup, change):
    collector, _, _ = setup
    result = collector.sample()
    item = {"id": "a" * 20, "gpu_uuid": GPU, "kind": "task", "module": "monomer_dft", "environment": "development",
            "label": "DFT任务", "status": "running", "memory_used_bytes": 100, "started_at": None}
    if change == "task_service_label": item["label"] = "DFT服务"
    elif change == "unknown_task_environment": item["environment"] = "unknown"
    elif change == "wrong_status": item["status"] = "resident"
    elif change == "unknown_gpu": item["gpu_uuid"] = "GPU-elsewhere"
    elif change == "future_started": item["started_at"] = history.iso(NOW + 1)
    else: item["lease_id"] = "private"
    result["activity"]["items"] = [item]
    with pytest.raises(ValueError): validate_task_history(result)


def active_item(identifier="a"):
    return {"id": identifier * 20, "gpu_uuid": GPU, "kind": "task", "module": "monomer_dft", "environment": "development",
            "label": "DFT任务", "status": "running", "memory_used_bytes": 100, "started_at": None}


def activity_reader(status="ready", items=None, error=None):
    return lambda inventory, clock: {"captured_at": history.iso(clock()), "status": status,
                                     "error_code": error, "items": deepcopy(items or [])}


@pytest.mark.parametrize("status", ["partial", "unavailable"])
def test_activity_failure_returns_last_complete_group_and_its_original_time(setup, status):
    collector, roots, state = setup
    now = [NOW]; collector.clock = lambda: now[0]
    collector.activity_reader = activity_reader(items=[active_item()])
    first = collector.sample()
    now[0] += 5; update_inventory(state, now[0])
    collector.activity_reader = activity_reader(status, [active_item("b")] if status == "partial" else [], "process_query_failed")
    latest = collector.sample(force_history=True)
    assert latest["activity"]["items"] == first["activity"]["items"]
    assert latest["activity"]["captured_at"] == first["activity"]["captured_at"]
    assert latest["activity"]["status"] == "partial" and latest["activity"]["error_code"] == "process_query_failed"
    assert latest["history_captured_at"] == history.iso(now[0]) and latest["status"] == "ready"


def test_history_failure_returns_last_complete_coverage_while_activity_advances(setup):
    collector, roots, state = setup
    now = [NOW]; collector.clock = lambda: now[0]
    path = write_journal(roots["development_dft"], journal())
    first = collector.sample()
    path.write_text("{bad JSON")
    now[0] += 5; update_inventory(state, now[0])
    collector.activity_reader = activity_reader(items=[active_item()])
    latest = collector.sample(force_history=True)
    assert latest["tasks"] == first["tasks"] and latest["history_captured_at"] == first["history_captured_at"]
    assert latest["status"] == "partial" and all(source["status"] == "partial" for source in latest["sources"])
    assert latest["sources"][0]["matched_tasks"] == 1 and latest["sources"][0]["excluded_tasks"] == 0
    assert latest["sources"][0]["error_code"] == "invalid_journal"
    assert latest["sources"][1]["error_code"] == "cache_fallback"
    assert latest["activity"]["status"] == "ready" and latest["activity"]["captured_at"] == history.iso(now[0])


def test_successful_empty_groups_replace_prior_records_and_are_valid_last_good(setup):
    collector, roots, state = setup
    now = [NOW]; collector.clock = lambda: now[0]
    path = write_journal(roots["development_dft"], journal())
    collector.activity_reader = activity_reader(items=[active_item()])
    assert collector.sample()["tasks"]
    cancelled = journal(); cancelled["snapshot"].update(status="cancelled", result=None)
    history.atomic_json(path, cancelled)
    now[0] += 5; update_inventory(state, now[0])
    collector.activity_reader = activity_reader()
    empty = collector.sample(force_history=True)
    assert empty["tasks"] == [] and empty["activity"]["items"] == [] and empty["status"] == "ready"
    path.write_text("invalid JSON")
    now[0] += 5; update_inventory(state, now[0])
    collector.activity_reader = activity_reader("partial", [active_item("b")], "verification_failed")
    fallback = collector.sample(force_history=True)
    assert fallback["tasks"] == [] and fallback["activity"]["items"] == []
    assert fallback["status"] == fallback["activity"]["status"] == "partial"
    assert fallback["history_captured_at"] == empty["history_captured_at"]
    assert fallback["activity"]["captured_at"] == empty["activity"]["captured_at"]


def test_first_partial_observation_is_usable_but_never_promoted_to_last_good(setup):
    collector, roots, state = setup
    write_journal(roots["development_dft"], journal("good-job"))
    path = write_journal(roots["development_dft"], journal("bad-job")); path.write_text("bad JSON")
    collector.activity_reader = activity_reader("partial", [active_item()], "verification_failed")
    first = collector.sample()
    assert first["status"] == "partial" and len(first["tasks"]) == 1
    assert first["activity"]["status"] == "partial" and first["activity"]["items"]
    assert collector._last_good_history is None and collector._last_good_activity is None
    write_journal(roots["development_dft"], journal("good-job")).write_text("bad JSON")
    collector.activity_reader = activity_reader("unavailable", error="process_query_failed")
    second = collector.sample(force_history=True)
    assert second["tasks"] == [] and second["activity"]["items"] == []
    assert second["activity"]["status"] == "unavailable"
    assert not (state / history.LAST_GOOD_FILE).exists()  # sample() remains read-only.


def restart_collector(collector, roots, state, now):
    return history.TaskHistoryCollector(state, sources=roots, clock=lambda: now,
                                        md_reader=collector.md_reader,
                                        activity_reader=activity_reader("unavailable", error="process_query_failed"))


def test_private_sidecar_preserves_independent_good_groups_across_restart_during_failure(setup):
    collector, roots, state = setup
    path = write_journal(roots["development_dft"], journal())
    collector.activity_reader = activity_reader(items=[active_item()])
    collector.run(threading.Event(), once=True)
    sidecar = state / history.LAST_GOOD_FILE
    saved = json.loads(sidecar.read_text()); before = sidecar.read_bytes()
    assert sidecar.stat().st_mode & 0o777 == 0o600
    assert validate_task_history(saved).status == "ready"
    path.write_text("bad JSON")
    collector.activity_reader = activity_reader("unavailable", error="process_query_failed")
    collector.clock = lambda: NOW + 5; update_inventory(state, NOW + 5)
    collector.sample(force_history=True)  # Refresh latest collection health.
    collector.run(threading.Event(), once=True)
    current = json.loads((state / "gpu-tasks.json").read_text())
    assert current["status"] == current["activity"]["status"] == "partial"
    assert sidecar.read_bytes() == before
    update_inventory(state, NOW + 10)
    restarted = restart_collector(collector, roots, state, NOW + 10).sample()
    assert restarted["collector_id"] != saved["collector_id"]
    assert restarted["tasks"] == saved["tasks"] and restarted["activity"]["items"] == saved["activity"]["items"]
    assert restarted["history_captured_at"] == saved["history_captured_at"]
    assert restarted["activity"]["captured_at"] == saved["activity"]["captured_at"]
    assert restarted["status"] == restarted["activity"]["status"] == "partial"


@pytest.mark.parametrize("only_good", ["history", "activity"])
def test_sidecar_can_restore_only_one_independently_complete_group(setup, only_good):
    collector, roots, state = setup
    path = write_journal(roots["development_dft"], journal())
    if only_good == "history":
        collector.activity_reader = activity_reader("partial", [active_item()], "verification_failed")
    else:
        path.write_text("bad JSON")
        collector.activity_reader = activity_reader(items=[active_item()])
    collector.run(threading.Event(), once=True)
    saved = json.loads((state / history.LAST_GOOD_FILE).read_text())
    assert (saved["status"] == "ready") == (only_good == "history")
    assert (saved["activity"]["status"] == "ready") == (only_good == "activity")
    path.write_text("bad JSON"); update_inventory(state, NOW + 10)
    result = restart_collector(collector, roots, state, NOW + 10).sample()
    assert bool(result["tasks"]) == (only_good == "history")
    assert bool(result["activity"]["items"]) == (only_good == "activity")


@pytest.mark.parametrize("invalid", ["malformed", "foreign_node", "foreign_gpu", "extra_gpu", "future", "partial", "extra_sensitive", "permissions", "symlink", "hardlink", "fifo", "oversize"])
def test_recovery_rejects_unsafe_mismatched_or_unverified_cache(setup, invalid, tmp_path):
    collector, roots, state = setup
    path = write_journal(roots["development_dft"], journal())
    collector.activity_reader = activity_reader(items=[active_item()])
    saved = collector.sample()
    if invalid == "foreign_node": saved["node_id"] = "other-server"
    elif invalid == "foreign_gpu":
        saved["gpu_uuids"] = ["GPU-other"]
        for task in saved["tasks"]: task["gpu_uuid"] = "GPU-other"
        for item in saved["activity"]["items"]: item["gpu_uuid"] = "GPU-other"
    elif invalid == "extra_gpu": saved["gpu_uuids"].append("GPU-other")
    elif invalid == "future": saved["captured_at"] = history.iso(NOW + 100)
    elif invalid == "partial":
        saved["status"] = "partial"; saved["sources"][0].update(status="partial", error_code="verification_failed")
        saved["activity"].update(status="partial", error_code="verification_failed")
    elif invalid == "extra_sensitive": saved["owner"] = "must-not-return"
    sidecar = state / history.LAST_GOOD_FILE
    history.atomic_json(sidecar, saved)
    if invalid == "malformed": sidecar.write_text("invalid JSON")
    elif invalid == "permissions": sidecar.chmod(0o644)
    elif invalid == "oversize": sidecar.write_bytes(b" " * (history.MAX_LAST_GOOD_BYTES + 1))
    elif invalid in {"symlink", "hardlink"}:
        other = tmp_path / "other.json"; history.atomic_json(other, saved); sidecar.unlink()
        sidecar.symlink_to(other) if invalid == "symlink" else os.link(other, sidecar)
    elif invalid == "fifo": sidecar.unlink(); os.mkfifo(sidecar, 0o600)
    path.write_text("invalid JSON"); update_inventory(state, NOW + 10)
    result = restart_collector(collector, roots, state, NOW + 10).sample()
    assert result["tasks"] == [] and result["activity"]["items"] == []
    assert result["activity"]["status"] == "unavailable"


def test_same_uuid_index_change_keeps_good_data_but_device_replacement_clears_it(setup):
    collector, roots, state = setup
    path = write_journal(roots["development_dft"], journal())
    collector.activity_reader = activity_reader(items=[active_item()])
    first = collector.sample()
    path.write_text("bad JSON")
    collector.activity_reader = activity_reader("unavailable", error="process_query_failed")
    update_inventory(state, NOW, index=2)
    reordered = collector.sample(force_history=True)
    assert reordered["tasks"] == first["tasks"] and reordered["activity"]["items"] == first["activity"]["items"]
    update_inventory(state, NOW, gpu="GPU-replacement", index=2)
    replaced = collector.sample(force_history=True)
    assert replaced["tasks"] == [] and replaced["activity"]["items"] == []
    assert collector._last_good_history is None and collector._last_good_activity is None


def test_clock_rollback_discards_future_good_groups_instead_of_retiming_cached_data(setup):
    collector, roots, state = setup
    path = write_journal(roots["development_dft"], journal())
    collector.activity_reader = activity_reader(items=[active_item()])
    collector.sample()
    path.write_text("bad JSON")
    collector.clock = lambda: NOW - 5; update_inventory(state, NOW - 5)
    collector.activity_reader = activity_reader("unavailable", error="process_query_failed")
    result = collector.sample(force_history=True)
    assert result["tasks"] == [] and result["activity"]["items"] == []
    assert collector._last_good_history is None and collector._last_good_activity is None
    assert result["activity"]["status"] == "unavailable"


def test_clock_rollback_retires_persisted_future_groups_even_when_inventory_temporarily_fails(setup):
    collector, roots, state = setup
    write_journal(roots["development_dft"], journal())
    collector.activity_reader = activity_reader(items=[active_item()])
    collector.run(threading.Event(), once=True)
    assert json.loads((state / history.LAST_GOOD_FILE).read_text())["tasks"]
    (state / "latest.json").unlink()
    collector.clock = lambda: NOW - 5
    collector.run(threading.Event(), once=True)
    retired = json.loads((state / history.LAST_GOOD_FILE).read_text())
    assert retired["tasks"] == [] and retired["activity"]["items"] == []
    assert retired["status"] == retired["activity"]["status"] == "unavailable"
    assert retired["gpu_uuids"] == [GPU]
    assert retired["captured_at"] == history.iso(NOW - 5)


def test_sidecar_write_failure_does_not_interrupt_current_telemetry_and_is_retried(setup, monkeypatch):
    collector, roots, state = setup
    write_journal(roots["development_dft"], journal())
    original = history.atomic_json
    fail_cache = [True]
    def write(path, payload):
        if path.name == history.LAST_GOOD_FILE and fail_cache[0]:
            raise OSError("cache unavailable")
        return original(path, payload)
    monkeypatch.setattr(history, "atomic_json", write)
    collector.run(threading.Event(), once=True)
    assert json.loads((state / "gpu-tasks.json").read_text())["status"] == "ready"
    assert collector._last_good_changed and not (state / history.LAST_GOOD_FILE).exists()
    fail_cache[0] = False
    collector.run(threading.Event(), once=True)
    assert (state / history.LAST_GOOD_FILE).exists() and not collector._last_good_changed
