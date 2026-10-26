"""Post-equilibrium orchestration with synthetic child processes, never MD/QM."""
from pathlib import Path
import sys
from uuid import uuid4

import pytest

from polymer_md.contracts import NativeConfiguration, ScientificRequest, SubmitRequest, request_sha256
from workers.polymer_md_worker.app.journal import read_json
from workers.polymer_md_worker.app.manager import JobManager
from test_manager import supervisor, until


@pytest.fixture
def anyio_backend():
    return "asyncio"


TARGETS = ["density", "sp_total", "sp_ced", "thermal_conductivity", "tg"]


def payload(properties=TARGETS):
    scientific = ScientificRequest(smiles="*CC*", n_atom=100, n_chain=2, seed=12,
        calculation="basic_tc" if "thermal_conductivity" in properties else "basic",
        native_configuration=NativeConfiguration(properties=properties))
    return SubmitRequest(job_id=str(uuid4()), owner_user_id=str(uuid4()), attempt_token="a" * 64,
        request_sha256=request_sha256(scientific), enqueue_sequence=1, scientific_request=scientific)


async def finished(manager, job_id, expected="completed"):
    await until(lambda: manager.get(job_id)["status"] in {"completed", "failed", "cancelled"})
    snapshot = manager.get(job_id)
    assert snapshot["status"] == expected, snapshot
    return snapshot


def posteq_supervisor(tmp_path, *, fail=None, delay=None, equilibrated=True, tc_eligible=True, warning=None):
    manager, calls = supervisor(tmp_path)
    inputs = {}

    def command(request_path, stage, work_dir, result_path):
        calls.append(stage)
        incoming = read_json(work_dir / ".input-checkpoint.json")
        inputs.setdefault(stage, []).append(incoming)
        assert incoming["stage"] == stage
        if stage != "terminal_qm":
            assert Path(incoming["checkpoint"]).is_file()
            assert "/checkpoints/" in incoming["checkpoint"]
        if fail == stage:
            return [sys.executable, "-c", "raise SystemExit(3)"]
        eq = equilibrated and stage != "analyze_0" if stage.startswith("analyze_") else True
        quality = "not_converged" if not eq else "warning" if stage == warning else "passed"
        keys = (["density"] if stage.startswith("analyze_") else
                {"sp_analyze": ["sp_total", "sp_ced"], "tc_analyze": ["thermal_conductivity"],
                 "tg_analyze": ["tg"]}.get(stage, []))
        observations = [{"key": key, "value": 1.0, "unit": "synthetic-only", "quality_status": quality,
                         "source_stage": stage.removesuffix("_analyze")} for key in keys]
        result = {"stage": stage, "quality_status": quality, "equilibrated": eq,
                  "tc_eligible": eq and tc_eligible, "artifacts": [],
                  "results": {"properties": observations, "quality": {"synthetic": True},
                              "series": [{"key": key, "unit": "synthetic-only", "points": [{"x": 1, "y": 1}]} for key in keys]}}
        script = "\n".join([
            "import hashlib,json,time", "from pathlib import Path",
            f"time.sleep({0.5 if delay == stage else 0})",
            f"p=Path({str(work_dir / (stage + '.checkpoint'))!r});p.write_text({stage!r})",
            f"result={result!r}",
            "result['checkpoint']={'path':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}",
            f"Path({str(result_path)!r}).write_text(json.dumps(result))",
        ])
        return [sys.executable, "-c", script]

    manager.command_builder = command
    return manager, calls, inputs


@pytest.mark.anyio
async def test_extensions_run_in_order_from_latest_qualified_cell_and_publish_independent_quality(tmp_path):
    manager, calls, inputs = posteq_supervisor(tmp_path, warning="sp_analyze")
    request = payload()
    await manager.start()
    try:
        await manager.submit(request)
        await finished(manager, request.job_id)
        assert calls[-6:] == ["sp", "sp_analyze", "tc", "tc_analyze", "tg", "tg_analyze"]
        for stage in ("sp", "tc", "tg"):
            assert Path(inputs[stage][0]["checkpoint"]).parent.name == "analyze_1-1"
        for stage, parent in (("sp_analyze", "sp-1"), ("tc_analyze", "tc-1"), ("tg_analyze", "tg-1")):
            assert Path(inputs[stage][0]["checkpoint"]).parent.name == parent
        snapshot = manager.get(request.job_id)
        assert snapshot["cleanup_confirmed"] and snapshot["quality_status"] == "warning"
        assert {item["key"] for item in snapshot["results"]["properties"]} == set(TARGETS)
        quality = snapshot["results"]["quality_summary"]
        assert set(quality) == {"basic", "sp", "thermal", "glass_transition"}
        assert quality["sp"]["quality_status"] == "warning"
        assert quality["basic"]["quality_status"] == quality["thermal"]["quality_status"] == quality["glass_transition"]["quality_status"] == "passed"
    finally:
        await manager.stop()


@pytest.mark.anyio
@pytest.mark.parametrize("properties,expected", [(["density", "sp_total"], ["sp", "sp_analyze"]),
    (["density", "sp_ced"], ["sp", "sp_analyze"]), (["density", "tg"], ["tg", "tg_analyze"])])
async def test_only_requested_extensions_run_and_unrequested_shared_outputs_are_filtered(tmp_path, properties, expected):
    manager, calls, _ = posteq_supervisor(tmp_path)
    request = payload(properties)
    await manager.start()
    try:
        await manager.submit(request)
        await finished(manager, request.job_id)
        assert [stage for stage in calls if stage in {"sp", "sp_analyze", "tc", "tc_analyze", "tg", "tg_analyze"}] == expected
        assert {item["key"] for item in manager.get(request.job_id)["results"]["properties"]} == set(properties)
    finally:
        await manager.stop()


@pytest.mark.anyio
async def test_unqualified_equilibrium_never_starts_any_extension(tmp_path):
    manager, calls, _ = posteq_supervisor(tmp_path, equilibrated=False)
    request = payload()
    await manager.start()
    try:
        await manager.submit(request)
        await finished(manager, request.job_id)
        assert manager.get(request.job_id)["quality_status"] == "not_converged"
        assert not {"sp", "tc", "tg"} & set(calls)
    finally:
        await manager.stop()


@pytest.mark.anyio
async def test_orientation_blocks_only_thermal_extension_and_retains_sp_and_tg(tmp_path):
    manager, calls, _ = posteq_supervisor(tmp_path, tc_eligible=False)
    request = payload()
    await manager.start()
    try:
        await manager.submit(request)
        await finished(manager, request.job_id)
        assert "sp_analyze" in calls and "tg_analyze" in calls and "tc" not in calls
        snapshot = manager.get(request.job_id)
        assert snapshot["quality_status"] == "warning"
        assert snapshot["results"]["quality_summary"]["thermal"]["reason"] == "tc_ineligible"
        properties = {item["key"]: item for item in snapshot["results"]["properties"]}
        assert set(properties) == set(TARGETS)
        assert properties["thermal_conductivity"]["value"] is None
        assert properties["thermal_conductivity"]["reason"] == "tc_ineligible"
    finally:
        await manager.stop()


@pytest.mark.anyio
@pytest.mark.parametrize("failing,retained", [("sp", {"density"}),
    ("tg", {"density", "sp_total", "sp_ced", "thermal_conductivity"})])
async def test_extension_technical_failure_is_terminal_and_preserves_preceding_valid_observations(tmp_path, failing, retained):
    manager, calls, _ = posteq_supervisor(tmp_path, fail=failing)
    request = payload()
    await manager.start()
    try:
        await manager.submit(request)
        await finished(manager, request.job_id, "failed")
        snapshot = manager.get(request.job_id)
        assert snapshot["cleanup_confirmed"] and calls.count(failing) == 2
        assert snapshot["results"]["quality_summary"]["basic"]["quality_status"] == "passed"
        properties = {item["key"]: item for item in snapshot["results"]["properties"]}
        assert set(properties) == set(TARGETS)
        assert {key for key, prop in properties.items() if prop["value"] is not None} == retained
        assert all(properties[key]["reason"] for key in set(TARGETS) - retained)
        assert "tg_analyze" not in calls
        if failing == "sp":
            assert "tc" not in calls and "tg" not in calls
    finally:
        await manager.stop()


@pytest.mark.anyio
async def test_interrupted_tg_reuses_sp_and_tc_and_recovers_from_equilibrium_branch(tmp_path):
    manager, _, _ = posteq_supervisor(tmp_path, delay="tg")
    request = payload()
    await manager.start()
    await manager.submit(request)
    await until(lambda: manager.get(request.job_id)["stage"] == "tg")
    await manager.stop()
    recovered, calls, inputs = posteq_supervisor(tmp_path)
    await recovered.start()
    try:
        await finished(recovered, request.job_id)
        assert calls == ["tg", "tg_analyze"]
        assert Path(inputs["tg"][0]["checkpoint"]).parent.name == "analyze_1-1"
        snapshot = recovered.get(request.job_id)
        assert [stage["attempt"] for stage in snapshot["stage_runs"] if stage["stage"] == "tg"] == [1, 2]
        assert {item["key"] for item in snapshot["results"]["properties"]} == set(TARGETS)
    finally:
        await recovered.stop()


def test_extension_checkpoint_selection_requires_a_qualified_analysis_and_ignores_rnemd_or_other_extensions():
    record = {"completed_stages": {"analyze_0": {"result": {"equilibrated": True}},
        "analyze_1": {"result": {"equilibrated": False}}, "tc_analyze": {"result": {}},
        "sp_analyze": {"result": {}}, "tg_analyze": {"result": {}}}}
    for stage in ("sp", "tc", "tg"):
        assert JobManager._predecessor(record, stage) == "analyze_0"
    record["completed_stages"]["analyze_0"]["result"]["equilibrated"] = False
    for stage in ("sp", "tc", "tg"):
        with pytest.raises(ValueError, match="qualified analysis"):
            JobManager._predecessor(record, stage)
