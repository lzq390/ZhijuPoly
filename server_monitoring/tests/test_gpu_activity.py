import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from server_monitoring import gpu_activity as activity


NOW = 1_800_000_000.0
GPU0 = "GPU-00000000-1111-2222-3333-444444444444"
GPU1 = "GPU-11111111-1111-2222-3333-444444444444"
OTHER_GPU = "GPU-99999999-1111-2222-3333-444444444444"
CGROUP = "0::/user.slice/example.service"


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def lease(**changes):
    value = dict(kind="residency", component="dft", environment="dev", gpu_index=0,
                 gpu_uuid=GPU0, status="active", created_at=NOW - 200,
                 heartbeat_at=NOW - 2, owner_pid=10, owner_process_start_ticks=100,
                 owner_boot_id="test-boot", workload_pid=20, workload_process_start_ticks=200,
                 workload_cgroup=CGROUP, request_id="dft-" + "a" * 64)
    value.update(changes)
    return value


@pytest.fixture
def observation(tmp_path, monkeypatch):
    root = tmp_path / "gpu-session"
    run = root / "runs" / ("20261111T014008Z-" + "a" * 32)
    private_json(root / "controller.json", dict(schema_version=1, pid=1, start_ticks=10, run_directory=str(run)))
    state = run / "broker-state.json"
    private_json(state, dict(schema_version=1, leases=[lease()]))
    proc = tmp_path / "proc"
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("test-boot\n")
    processes = {20: activity._Process(20, 200, NOW - 1000, CGROUP, "monomer_dft")}
    ticks = {1: 10, 10: 100, 20: 200}
    raw = [f"{GPU0}, 20, 123\n"]
    monkeypatch.setattr(activity, "DEV_SESSION_ROOT", root)
    monkeypatch.setattr(activity, "PROC_ROOT", proc)
    monkeypatch.setattr(activity, "_ticks", lambda pid: ticks[pid])
    monkeypatch.setattr(activity, "_process", lambda pid, now: processes[pid])
    monkeypatch.setattr(activity, "_production_guard", lambda now: dict(
        schema_version=1, status="ready", observed_at=activity._iso(NOW - 1),
        gpu_uuid=GPU1, gpu_index=1, allowed_processes=[], authorities={}))
    def query(command, **kwargs):
        assert command == activity.GPU_COMMAND
        assert kwargs["timeout"] == 3 and kwargs["cwd"] == "/"
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        return SimpleNamespace(stdout=raw[0])
    monkeypatch.setattr(activity.subprocess, "run", query)
    return SimpleNamespace(state=state, processes=processes, ticks=ticks, raw=raw, root=root)


def collect():
    return activity.collect_activity({GPU0: 0, GPU1: 1}, clock=lambda: NOW)


def test_gpu_zero_residency_is_service_and_projection_omits_private_fields(observation):
    result = collect()
    assert result["status"] == "ready"
    item, = result["items"]
    assert item["kind"] == "service" and item["status"] == "resident"
    assert item["module"] == "monomer_dft" and item["environment"] == "development"
    assert item["memory_used_bytes"] == 123 * 1024 * 1024
    assert item["label"] == "DFT服务" and item["started_at"] == activity._iso(NOW - 1000)
    assert len(item["id"]) == 20
    assert set(item) == {"id", "gpu_uuid", "kind", "module", "environment", "label", "status", "memory_used_bytes", "started_at"}
    serialized = json.dumps(result)
    assert all(value not in serialized for value in (CGROUP, "test-boot", "request_id", "pid", "workload", "owner"))


@pytest.mark.parametrize("component,request_id,module,label", [
    ("dft", "dft-" + "a" * 64, "monomer_dft", "DFT任务"),
    ("md", "md:dev:" + "a" * 16, "monomer_md", "MD任务"),
])
def test_only_active_registered_execution_is_a_task(observation, component, request_id, module, label):
    private_json(observation.state, dict(schema_version=1, leases=[lease(kind="execution", component=component, request_id=request_id)]))
    item, = collect()["items"]
    assert (item["kind"], item["status"], item["module"], item["label"]) == ("task", "running", module, label)
    # Allocation time is not a truthful task start time.
    assert item["started_at"] is None


@pytest.mark.parametrize("changes", [
    {"kind": "execution", "component": "md", "request_id": "md:dev:probe:" + "a" * 16},
    {"kind": "execution", "component": "md", "request_id": "md:prod:" + "a" * 16},
    {"status": "reserved"}, {"status": "suspect"}, {"status": "expired"},
    {"gpu_uuid": OTHER_GPU}, {"gpu_index": 1}, {"workload_cgroup": "0::/other"},
    {"environment": "private-user-input"},
])
def test_admission_probe_inactive_or_mismatched_identity_is_not_task(observation, changes):
    private_json(observation.state, dict(schema_version=1, leases=[lease(**changes)]))
    item, = collect()["items"]
    assert (item["kind"], item["status"], item["module"]) == ("process", "observed", "other")


@pytest.mark.parametrize("changes", [
    {"heartbeat_at": NOW - 16}, {"heartbeat_at": NOW + 1}, {"created_at": NOW + 1},
    {"owner_boot_id": "previous-boot"}, {"owner_process_start_ticks": 99},
    {"workload_process_start_ticks": 199},
])
def test_stale_or_reused_process_lease_degrades_to_observation(observation, changes):
    private_json(observation.state, dict(schema_version=1, leases=[lease(**changes)]))
    result = collect()
    assert result["status"] == "partial" and result["error_code"] == "gpu_activity_incomplete"
    assert result["items"][0]["kind"] == "process"


def test_execution_and_parent_residency_do_not_duplicate_process_memory(observation):
    private_json(observation.state, dict(schema_version=1, leases=[lease(), lease(kind="execution")]))
    result = collect()
    assert len(result["items"]) == 1 and result["items"][0]["kind"] == "task"
    assert result["items"][0]["memory_used_bytes"] == 123 * 1024 * 1024


def test_many_processes_same_gpu_and_mps_remain_separate_services(observation):
    observation.raw[0] += f"{GPU0}, 21, 28\n{GPU0}, 22, 512\n"
    observation.processes[21] = activity._Process(21, 201, NOW - 1000, CGROUP, "nvidia_mps")
    observation.processes[22] = activity._Process(22, 202, NOW - 1000, CGROUP, "other")
    observation.ticks.update({21: 201, 22: 202})
    items = collect()["items"]
    assert len(items) == 3
    assert {item["label"] for item in items} == {"DFT服务", "NVIDIA MPS服务", "GPU进程"}
    assert all(item["kind"] != "task" for item in items)


def test_inventory_change_never_reuses_old_gpu_association(observation):
    observation.raw[0] = f"{OTHER_GPU}, 20, 123\n"
    changed = activity.collect_activity({OTHER_GPU: 0}, clock=lambda: NOW)
    assert changed["items"][0]["kind"] == "process"
    old_inventory = collect()
    assert old_inventory["status"] == "partial" and not old_inventory["items"]


def test_pid_reused_during_collection_drops_attribution(observation, monkeypatch):
    original = activity._ticks
    calls = {20: 0}
    def ticks(pid):
        if pid == 20:
            calls[20] += 1
            return 200 if calls[20] == 1 else 999
        return original(pid)
    monkeypatch.setattr(activity, "_ticks", ticks)
    result = collect()
    assert result["status"] == "partial" and result["items"][0]["kind"] == "process"


def test_unreadable_process_retains_observed_row_and_marks_partial(observation):
    observation.processes.clear()
    result = collect()
    assert result["status"] == "partial" and len(result["items"]) == 1
    assert result["items"][0]["started_at"] is None


def test_missing_authority_file_does_not_claim_no_tasks(observation):
    observation.state.unlink()
    result = collect()
    assert result["status"] == "partial" and result["items"][0]["status"] == "observed"


def test_empty_successful_query_is_ready(observation):
    observation.raw[0] = ""
    observation.state.unlink()
    result = collect()
    assert result["status"] == "ready" and result["items"] == []


@pytest.mark.parametrize("failure,code", [
    (subprocess.TimeoutExpired("private-command", 3), "gpu_process_query_timeout"),
    (subprocess.CalledProcessError(1, "private-command", stderr="private-driver-error"), "gpu_process_query_failed"),
    (FileNotFoundError("private-path"), "gpu_process_query_failed"),
])
def test_query_failures_are_unavailable_without_diagnostic_leak(observation, monkeypatch, failure, code):
    def query(*args, **kwargs):
        raise failure
    monkeypatch.setattr(activity.subprocess, "run", query)
    result = collect()
    assert result["status"] == "unavailable" and result["error_code"] == code
    assert not result["items"] and "private" not in json.dumps(result)


def test_unsupported_memory_and_bad_query_rows_are_not_false_zero(observation):
    observation.raw[0] = f"{GPU0}, 20, [N/A]\nunsupported,field\n"
    result = collect()
    assert result["status"] == "partial"
    assert result["items"][0]["memory_used_bytes"] is None


def production_guard():
    return dict(schema_version=1, status="ready", observed_at=activity._iso(NOW - 1),
                gpu_uuid=GPU0, gpu_index="0", allowed_processes=[dict(pid=20, gpu_uuid=GPU0,
                    authority=activity.PRODUCTION_DFT_UNIT)], authorities={"systemd_user_units": {
                    activity.PRODUCTION_DFT_UNIT: dict(pid=30, cgroup="/user.slice/" + activity.PRODUCTION_DFT_UNIT)}})


def test_production_service_requires_fresh_guard_exact_group_and_preexisting_process(observation, monkeypatch):
    private_json(observation.state, dict(schema_version=1, leases=[]))
    cgroup = "0::/user.slice/" + activity.PRODUCTION_DFT_UNIT
    observation.processes[20] = activity._Process(20, 200, NOW - 1000, cgroup, "monomer_dft")
    observation.processes[30] = activity._Process(30, 300, NOW - 1000, cgroup, "other")
    guard = production_guard()
    monkeypatch.setattr(activity, "_production_guard", lambda now: guard)
    item, = collect()["items"]
    assert item["kind"] == "service" and item["environment"] == "production"
    for replacement in [activity._Process(20, 200, NOW, cgroup, "monomer_dft"),
                        activity._Process(20, 200, NOW - 1000, "0::/wrong", "monomer_dft")]:
        observation.processes[20] = replacement
        assert collect()["items"][0]["kind"] == "process"


def test_production_backend_requires_guard_container_group_not_process_name(observation, monkeypatch):
    private_json(observation.state, dict(schema_version=1, leases=[]))
    container = "a" * 64
    guard = production_guard()
    guard["allowed_processes"][0]["authority"] = "production-backend"
    guard["authorities"] = {"backend_container": container}
    monkeypatch.setattr(activity, "_production_guard", lambda now: guard)
    observation.processes[20] = activity._Process(20, 200, NOW - 1000, "0::/docker/" + container, "platform")
    item, = collect()["items"]
    assert (item["label"], item["environment"]) == ("平台服务", "production")
    guard["authorities"]["backend_container"] = "b" * 64
    assert collect()["items"][0]["kind"] == "process"


@pytest.mark.parametrize("age", [-1, 46])
def test_guard_future_or_stale_is_rejected(tmp_path, monkeypatch, age):
    path = tmp_path / "guard.json"
    guard = production_guard(); guard["observed_at"] = activity._iso(NOW - age)
    private_json(path, guard)
    monkeypatch.setattr(activity, "PRODUCTION_GUARD", path)
    with pytest.raises(ValueError):
        activity._production_guard(NOW)


def test_private_state_rejects_symlinks_permissions_and_duplicate_keys(tmp_path):
    target = tmp_path / "state.json"; private_json(target, {"ready": True})
    link = tmp_path / "linked.json"; link.symlink_to(target)
    with pytest.raises(ValueError): activity._read_private_json(link)
    target.chmod(0o644)
    with pytest.raises(ValueError): activity._read_private_json(target)
    target.chmod(0o600); target.write_text('{"ready":true,"ready":false}')
    with pytest.raises(ValueError): activity._read_private_json(target)


def test_controller_cannot_select_an_arbitrary_state_directory(observation):
    value = json.loads((observation.root / "controller.json").read_text())
    value["run_directory"] = "/private/untrusted"
    private_json(observation.root / "controller.json", value)
    result = collect()
    assert result["status"] == "partial" and result["items"][0]["kind"] == "process"


@pytest.mark.parametrize("inventory", [{}, {GPU0: True}, {GPU0: -1}, {GPU0: 0, GPU1: 0}])
def test_invalid_inventory_cannot_misattribute_processes(inventory):
    result = activity.collect_activity(inventory, clock=lambda: NOW)
    assert result["status"] == "unavailable" and result["error_code"] == "gpu_inventory_unavailable"


def test_bounded_proc_parser_uses_exact_module_and_handles_spaces_in_stat(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "PROC_ROOT", tmp_path)
    root = tmp_path / "20"; root.mkdir()
    hz = os.sysconf("SC_CLK_TCK")
    # stat field 22 is starttime; the comm field may contain spaces and ')'.
    fields = ["S", "10"] + ["0"] * 17 + [str(100 * hz)]
    (root / "stat").write_text("20 (name with ) spaces) " + " ".join(fields))
    (root / "cmdline").write_bytes(b"python3\0-m\0workers.monomer_dft_worker.app.executor_process\0--private\0secret\0")
    (root / "cgroup").write_text(CGROUP)
    (tmp_path / "stat").write_text(f"btime {int(NOW - 1000)}\n")
    process = activity._process(20, NOW)
    assert process.signature == "monomer_dft" and process.started == NOW - 900
    (root / "cmdline").write_bytes(b"python3\0--note\0workers.monomer_dft_worker.app.executor_process\0")
    assert activity._process(20, NOW).signature == "other"
