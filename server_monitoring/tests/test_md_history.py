import json
import subprocess
from types import SimpleNamespace

import pytest

from server_monitoring import md_history as md
from server_monitoring.task_history import iso


NOW = 1_800_000_000.0
GPU = "GPU-test-1"


def row(**changes):
    result = {"id": "a" * 20, "created_at": iso(NOW - 30), "started_at": iso(NOW - 20), "finished_at": iso(NOW - 10),
              "run_mode": "formal", "gpu_uuid": GPU, "gpu_device": "1", "result_gpu_device": "1",
              "execution_broker": True, "same_job": True, "worker_present": True, "lease_present": True,
              "broker_present": True, "fencing_valid": True, "mode_verified": True}
    result.update(changes)
    return result


def projection(rows=None, **changes):
    result = {"version": 1, "md_rows": [row()] if rows is None else rows, "md_truncated": False, "md_available": True,
              "polymer_completed": 2, "polymer_truncated": False, "polymer_available": True}
    result.update(changes)
    return result


def runner_for(payload, calls=None):
    def run(command, **options):
        if calls is not None:
            calls.append((command, options))
        value = payload(command[-1]) if callable(payload) else payload
        return SimpleNamespace(returncode=0, stdout=json.dumps(value).encode())
    return run


def collect(payload, inventory=None):
    return md.collect_md_history({GPU: 1} if inventory is None else inventory, clock=lambda: NOW,
                                 runner=runner_for(payload))


def test_only_projects_verified_completed_identity_free_formal_and_demo():
    sources, tasks = collect(lambda environment: projection([row(id=("a" if environment == "development" else "b") * 20,
                                                                  run_mode="formal" if environment == "development" else "demo")]))
    assert len(sources) == 4 and all(source["status"] == "ready" for source in sources)
    assert [task["calculation_type"] for task in tasks] == ["md_formal", "md_demo"]
    assert [task["environment"] for task in tasks] == ["development", "production"]
    assert all(task["duration_seconds"] == 10 and task["module"] == "monomer_md" for task in tasks)
    assert set(tasks[0]) == {"id", "gpu_uuid", "module", "environment", "calculation_type", "status", "started_at", "finished_at", "duration_seconds"}
    assert all(not source["matched_tasks"] and source["excluded_tasks"] == 2 for source in sources if source["source_id"].endswith("polymer_md"))
    serialized = json.dumps(tasks)
    assert all(field not in serialized for field in ("created_at", "gpu_device", "lease", "worker", "owner", "result_data"))


def test_exact_fixed_command_read_only_sql_timeouts_and_no_sensitive_raw_result_fields():
    calls = []
    md.collect_md_history({GPU: 1}, clock=lambda: NOW, runner=runner_for(projection([]), calls))
    assert len(calls) == 2
    for command, options in calls:
        assert command[:3] == ["/usr/bin/docker", "exec", "-i"]
        assert command[3] == md.CONTAINERS[command[-1]]
        assert command[4:6] == ["python", "-c"] and command[6] == md.DATABASE_HELPER
        assert options["timeout"] == 2 and options["stderr"] == subprocess.DEVNULL
        assert options["stdin"] == subprocess.DEVNULL and "shell" not in options
    assert "READ ONLY" in md.DATABASE_HELPER and "statement_timeout='750ms'" in md.DATABASE_HELPER
    assert "hashlib.sha256" in md.DATABASE_HELPER
    assert all(fragment not in md.DATABASE_HELPER for fragment in ("import app", "owner_user_id", "input_smiles", "SELECT *", "print(os.environ"))


@pytest.mark.parametrize("change", [{"gpu_uuid": None}, {"gpu_uuid": "GPU-other"}, {"execution_broker": False},
                                   {"worker_present": False}, {"lease_present": False}, {"broker_present": False}, {"fencing_valid": False}])
def test_legacy_or_unverifiable_gpu_records_are_excluded_not_inferred(change):
    sources, tasks = collect(projection([row(**change)]))
    assert tasks == [] and all(source["status"] == "ready" for source in sources)
    assert all(source["excluded_tasks"] == 1 for source in sources if not source["source_id"].endswith("polymer_md"))


@pytest.mark.parametrize("change", [{"gpu_device": "0"}, {"result_gpu_device": "0"}, {"same_job": False},
                                   {"mode_verified": False}, {"started_at": iso(NOW)}, {"finished_at": iso(NOW + 1)},
                                   {"finished_at": "2026-11-11T00:00:00"}, {"started_at": None},
                                   {"worker_present": 1}, {"owner_user_id": "private"}, {"id": "raw-task-id"}])
def test_invalid_evidence_is_partial_while_other_verified_rows_remain(change):
    sources, tasks = collect(projection([row(**change), row(id="b" * 20)]))
    assert len(tasks) == 2
    for source in sources:
        if not source["source_id"].endswith("polymer_md"):
            assert source["status"] == "partial" and source["matched_tasks"] == 1 and source["excluded_tasks"] == 1


def test_duplicate_database_projection_does_not_duplicate_completed_task():
    sources, tasks = collect(projection([row(), row()]))
    assert len(tasks) == 2
    assert sources[0]["status"] == "partial" and sources[0]["matched_tasks"] == 1


def test_historical_uuid_survives_physical_index_reordering():
    sources, tasks = collect(projection([row(gpu_device="0", result_gpu_device="0")]))
    assert len(tasks) == 2 and all(source["status"] == "ready" for source in sources)
    assert {task["gpu_uuid"] for task in tasks} == {GPU}


def test_polymer_query_failure_does_not_hide_successfully_verified_md_records():
    sources, tasks = collect(projection(polymer_completed=0, polymer_available=False))
    assert len(tasks) == 2
    assert all(source["status"] == "ready" and source["matched_tasks"] == 1 for source in sources if not source["source_id"].endswith("polymer_md"))
    assert all(source["status"] == "unavailable" for source in sources if source["source_id"].endswith("polymer_md"))


def test_md_query_failure_does_not_misrepresent_polymer_coverage_as_unavailable():
    sources, tasks = collect(projection([], md_available=False))
    assert tasks == []
    assert all(source["status"] == "unavailable" for source in sources if not source["source_id"].endswith("polymer_md"))
    assert all(source["status"] == "ready" and source["excluded_tasks"] == 2 for source in sources if source["source_id"].endswith("polymer_md"))


def test_coverage_limits_remain_explicit():
    sources, tasks = collect(projection(md_truncated=True, polymer_truncated=True, polymer_completed=16384))
    assert len(tasks) == 2 and all(source["status"] == "partial" and source["error_code"] == "scan_limit" for source in sources)


@pytest.mark.parametrize("failure", ["timeout", "oserror", "exit", "oversize", "malformed", "unknown_field", "boolean_count", "helper_error"])
def test_database_failure_cannot_be_presented_as_empty_success(failure):
    def run(*args, **kwargs):
        if failure == "timeout": raise subprocess.TimeoutExpired("docker", 2)
        if failure == "oserror": raise OSError()
        output = json.dumps(projection()).encode(); code = 0
        if failure == "exit": code = 1
        elif failure == "oversize": output = b" " * (md.MAX_OUTPUT_BYTES + 1)
        elif failure == "malformed": output = b"{bad"
        elif failure == "unknown_field": output = json.dumps(projection(password="private")).encode()
        elif failure == "boolean_count": output = json.dumps(projection(polymer_completed=True)).encode()
        elif failure == "helper_error": output = b'{"unavailable":true}'
        return SimpleNamespace(returncode=code, stdout=output)
    sources, tasks = md.collect_md_history({GPU: 1}, clock=lambda: NOW, runner=run)
    assert tasks == [] and all(source["status"] == "unavailable" and source["error_code"] == "database_unavailable" for source in sources)
