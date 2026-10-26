"""Current summaries follow one measured production segment; history stays intact."""
from copy import deepcopy

import pytest

from polymer_md.contracts import NativeConfiguration, StageRun, request_sha256
from workers.polymer_md_worker.app.journal import read_json, sha256_file
from test_manager import request, supervisor


def property_observation(key, value, stage, window, quality="passed"):
    return {"key": key, "value": value, "unit": "synthetic_test_only",
            "source_stage": stage, "analysis_range": window, "quality_status": quality}


def series_observation(key, value, stage, window):
    return {"key": key, "unit": "synthetic_test_only", "source_stage": stage,
            "points": [{"time_ps": window["start_ps"], "value": value}]}


def test_each_additional_analysis_replaces_current_basic_observations_without_mixing_windows(tmp_path):
    manager, _ = supervisor(tmp_path)
    payload = request("basic_tc")
    record = manager._new_record(payload)
    manager.records[payload.job_id] = record
    directory = manager._directory(payload)
    work_dir = directory / "science"
    work_dir.mkdir(parents=True)
    history = []
    try:
        for index, value, density_quality, stage_quality in (
                (0, 1.0, "passed", "not_converged"),
                (1, .94, "warning", "not_converged"),
                (2, 1.1, "passed", "passed")):
            stage = f"analyze_{index}"
            run = StageRun(stage_run_id=f"{stage}-1", stage=stage, status="running").model_dump(mode="json")
            record["snapshot"]["stage_runs"].append(run)
            window = {"start_ps": 2000. + index * 5000, "stop_ps": 5000. + index * 5000}
            properties = [property_observation("density", value, stage, window, density_quality)]
            series = [series_observation("density", value, stage, window)]
            # The first window has a diagnostic that later windows cannot provide.
            if index == 0:
                properties.append(property_observation("old_diagnostic", 12, stage, window))
                series.append(series_observation("old_diagnostic", 12, stage, window))
            checkpoint = work_dir / f"{stage}.json"
            checkpoint.write_text(f"synthetic stage {index}")
            result = {"stage": stage, "quality_status": stage_quality,
                "equilibrated": stage_quality == "passed", "tc_eligible": stage_quality == "passed",
                "checkpoint": {"path": checkpoint.name, "sha256": sha256_file(checkpoint)},
                "results": {"properties": properties, "series": series,
                    "quality": {"analysis_window": window}}, "artifacts": []}
            manager._accept_stage(record, run, result, work_dir, directory)
            manager._save(record)
            snapshot = manager.get(payload.job_id)
            summary = snapshot["results"]
            assert summary["properties"] == properties
            assert summary["series"] == series
            assert all(item["source_stage"] == stage and item["analysis_range"] == window
                       for item in summary["properties"])
            assert summary["quality_summary"]["basic"] == {
                "analysis_window": window, "stage_run_id": run["stage_run_id"], "quality_status": stage_quality}
            assert snapshot["quality_status"] == stage_quality
            assert summary["equilibrated"] == summary["tc_eligible"] == (index == 2)
            if index:
                assert "old_diagnostic" not in {item["key"] for item in summary["properties"]}
                assert "old_diagnostic" not in {item["key"] for item in summary["series"]}
            assert snapshot["stage_runs"][:-1] == history
            history = deepcopy(snapshot["stage_runs"])
            assert read_json(directory / "journal.json")["snapshot"]["stage_runs"] == history

        # Thermal analysis adds a different measurement to the latest base,
        # including when the thermal quality itself does not converge.
        before = deepcopy(record["snapshot"]["results"])
        thermal = property_observation("thermal_conductivity", .2, "tc_analyze",
                                       {"start_ps": 800., "stop_ps": 1000.}, "warning")
        manager._publish_stage_results(record, {"stage": "tc_analyze", "stage_run_id": "tc_analyze-1"},
            {"quality_status": "not_converged"}, {"properties": [thermal],
                "series": [{"key": "temperature_profile", "unit": "K", "points": []}]})
        summary = record["snapshot"]["results"]
        assert summary["properties"] == before["properties"] + [thermal]
        assert summary["series"] == before["series"] + [{"key": "temperature_profile", "unit": "K", "points": []}]
        assert summary["quality_summary"]["basic"] == before["quality_summary"]["basic"]
        assert record["snapshot"]["quality_status"] == "not_converged"
        assert record["snapshot"]["stage_runs"] == history
    finally:
        manager.analysis_executor.shutdown()


@pytest.mark.parametrize("incoming", [{}, {"properties": [], "series": []}])
def test_new_basic_analysis_clears_missing_or_empty_observation_collections(tmp_path, incoming):
    manager, _ = supervisor(tmp_path)
    record = manager._new_record(request())
    record["snapshot"]["results"].update(properties=[{"key": "density", "quality_status": "passed"}],
                                        series=[{"key": "density", "points": []}])
    try:
        manager._publish_stage_results(record, {"stage": "analyze_1", "stage_run_id": "analyze_1-1"},
                                       {"quality_status": "not_converged"}, incoming)
        assert record["snapshot"]["results"]["properties"] == []
        assert record["snapshot"]["results"]["series"] == []
    finally:
        manager.analysis_executor.shutdown()


def test_native_stage_acceptance_filters_selection_and_preserves_qm_across_md_analysis(tmp_path):
    manager, _ = supervisor(tmp_path)
    native = NativeConfiguration(properties=["density", "qm_homo"])
    payload = request()
    scientific = payload.scientific_request.model_copy(update={"native_configuration": native})
    payload = payload.model_copy(update={"scientific_request": scientific, "request_sha256": request_sha256(scientific)})
    record = manager._new_record(payload)
    manager.records[payload.job_id] = record
    directory = manager._directory(payload)
    work_dir = directory / "science"
    work_dir.mkdir(parents=True)
    qm = property_observation("qm_homo", -8., "monomer_qm", None)
    density = property_observation("density", .94, "analyze_0", {"start_ps": 2000., "stop_ps": 5000.})
    try:
        for stage, observations in (("monomer_qm", [qm, {"key": "qm_lumo", "value": 1.}]),
                                    ("analyze_0", [density, {"key": "Cp", "value": 2.}])):
            run = StageRun(stage_run_id=f"{stage}-1", stage=stage, status="running").model_dump(mode="json")
            record["snapshot"]["stage_runs"].append(run)
            checkpoint = work_dir / f"{stage}.json"
            checkpoint.write_text("synthetic native selection checkpoint")
            result = {"stage": stage, "quality_status": "passed", "equilibrated": True,
                      "tc_eligible": True, "checkpoint": {"path": checkpoint.name, "sha256": sha256_file(checkpoint)},
                      "results": {"properties": observations}, "artifacts": []}
            manager._accept_stage(record, run, result, work_dir, directory)
            summary = record["snapshot"]["results"]
            assert summary["native_configuration"] == native.model_dump(mode="json")
            assert summary["properties"] == ([qm] if stage == "monomer_qm" else [qm, density])
        manager._save(record)
        assert read_json(directory / "journal.json")["snapshot"]["results"]["properties"] == [qm, density]
    finally:
        manager.analysis_executor.shutdown()


def test_completed_build_publishes_model_statistics_without_private_paths(tmp_path):
    manager, _ = supervisor(tmp_path)
    payload = request()
    record = manager._new_record(payload)
    directory = manager._directory(payload)
    work = directory / "science"
    work.mkdir(parents=True)
    run = StageRun(stage_run_id="build-1", stage="build", status="running").model_dump(mode="json")
    record["snapshot"]["stage_runs"].append(run)
    checkpoint = work / "build.json"
    checkpoint.write_text("synthetic model statistics checkpoint")
    model = {"mode": "blend", "actual_atom_count": 120, "polymer_statistics": {"Mn": 1000., "Mw": 1100., "dispersity": 1.1},
             "actual_composition": [{"id": "A", "repeat_unit_count": 10}], "work_dir": "/private/engine"}
    result = {"stage": "build", "quality_status": "passed", "checkpoint": {"path": checkpoint.name, "sha256": sha256_file(checkpoint)}, "results": model, "artifacts": []}
    try:
        manager._accept_stage(record, run, result, work, directory)
        assert record["snapshot"]["results"]["model"] == {key: value for key, value in model.items() if key != "work_dir"}
        manager._save(record)
        assert read_json(directory / "journal.json")["snapshot"]["results"]["model"]["polymer_statistics"]["dispersity"] == 1.1
    finally:
        manager.analysis_executor.shutdown()
