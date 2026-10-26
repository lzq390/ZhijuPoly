"""Selected results remain explicit across insufficient samples and failures."""
from copy import deepcopy
import json

import numpy as np
import pytest

from workers.polymer_md_worker.app.native_execution import (
    DEFAULT_METHODS, complete_requested_results, parameter_applicability, terminal_method,
)
from workers.polymer_md_worker.app.scientific_analysis import analyze_equilibrium, property_record


def request(properties, **methods):
    return {"native_configuration": {**DEFAULT_METHODS, **methods, "properties": properties}}


def density():
    return property_record("density", .95, "g/cm3", "measured_5block", "additional_1",
        {"start_ps": 2000., "stop_ps": 5000.}, .001)


def test_failed_extension_summary_keeps_valid_prior_values_and_fills_every_missing_selection():
    req = request(["density", "thermal_conductivity", "thermal_diffusivity", "tg", "sp_total", "sp_ced"])
    original = {"properties": [density()], "quality_summary": {"basic": {"quality_status": "passed"}}}
    before = deepcopy(original)
    result = complete_requested_results(req, original, reason="sp_execution_failed;later_branches_not_executed")
    records = {record["key"]: record for record in result["properties"]}
    assert original == before and records["density"] == original["properties"][0]
    assert set(records) == set(req["native_configuration"]["properties"])
    assert all(record["value"] is None and record["reason"] and record["quality_status"] == "failed"
               for key, record in records.items() if key != "density")
    assert records["thermal_conductivity"]["unit"] == "W/(m K)"
    assert records["sp_total"]["unit"] == "MPa^0.5"
    assert records["tg"]["source_stage"] == "tg"
    assert all(records[key]["analysis_range"]["start_ps"] is None and records[key]["analysis_range"]["stop_ps"] is None
               for key in records if key != "density")
    assert result["quality_summary"] == before["quality_summary"]
    json.dumps(result, allow_nan=False)


def test_missing_terminal_result_uses_its_branch_quality_reason_and_no_foreign_window():
    req = request(["density", "thermal_conductivity", "tg"])
    result = complete_requested_results(req, {"properties": [density()], "quality_summary": {
        "thermal": {"reason": "tc_ineligible"}, "glass_transition": {"reasons": ["source_not_qualified"]}}}, reason="fallback")
    records = {item["key"]: item for item in result["properties"]}
    assert records["thermal_conductivity"]["reason"] == "tc_ineligible"
    assert records["tg"]["reason"] == "source_not_qualified"
    assert records["thermal_conductivity"]["analysis_range"] == {"start_ps": None, "stop_ps": None, "sample_count": 0}


def test_phase_completion_does_not_publish_future_results_or_erase_existing_results():
    req = request(["density", "self-diffusion", "thermal_conductivity", "tg"])
    result = complete_requested_results(req, {"properties": [density()], "quality_status": "passed"}, stage="analyze_1")
    records = {record["key"]: record for record in result["properties"]}
    assert set(records) == {"density", "self-diffusion"}
    assert records["self-diffusion"]["source_stage"] == "additional_1"
    assert result["quality_status"] == "warning"
    thermal = complete_requested_results(req, {"properties": [density()]}, stage="tc_analyze")
    assert {record["key"] for record in thermal["properties"]} == {"density", "thermal_conductivity"}


def test_short_equilibrium_missing_msd_is_an_explicit_null_in_selected_results(tmp_path):
    path = tmp_path / "short.log"
    path.write_text("Step Time Temp Density TotEng E_bond E_angle E_dihed E_vdwl Volume\n"
        "0 0 300 .95 -10 1 1 1 -10 1000\n1000 1000 300 .95 -10 1 1 1 -10 1000\nLoop time\n")
    raw = analyze_equilibrium(path, stage="eq3", temp=300., pressure=1.)
    result = complete_requested_results(request(["density", "self-diffusion"]), raw, stage="analyze_0")
    record = next(record for record in result["properties"] if record["key"] == "self-diffusion")
    assert record["value"] is None and record["reason"] and record["analysis_range"]["sample_count"] == 0


@pytest.mark.parametrize("charge", ["zero", "gasteiger"])
def test_inapplicable_charge_methods_are_not_presented_as_executed_qm(charge):
    req = request(["density"], charge=charge, charge_method="wb97m-d3bj", charge_basis="6-311G(d,p)")
    evidence = parameter_applicability(req)
    assert evidence["opt_method"]["applicable"]  # Monomer DFT optimization still runs.
    assert not evidence["terminal_optimization"]["applicable"]
    for key in ("charge_method", "charge_basis", "sp_method", "sp_basis", "polar_method", "polar_basis"):
        assert evidence[key]["applicable"] is False and evidence[key]["reason"]
    assert "terminal_QM_optimization_not_applicable" in terminal_method(req["native_configuration"])


def test_refraction_activates_polarizability_without_extra_single_point_work():
    evidence = parameter_applicability(request(["density", "refractive_index"]))
    assert evidence["polar_method"]["applicable"] and evidence["polar_basis"]["applicable"]
    assert not evidence["sp_method"]["applicable"]
    assert evidence["charge_method"]["applicable"]


def test_boolean_values_and_negative_or_boolean_uncertainty_are_not_scientific_numbers():
    assert property_record("density", True, "g/cm3", "test", "eq3", {})["value"] is None
    for error in (True, -1., float("nan"), float("inf")):
        assert property_record("density", 1., "g/cm3", "test", "eq3", {}, error)["uncertainty"] is None


@pytest.mark.parametrize("value, expected", [
    (np.int32(-2), -2.), (np.uint64(2), 2.), (np.float32(.25), .25),
    (np.float64(.75), .75), (np.longdouble(.125), .125),
    (np.bool_(True), None), (np.bool_(False), None),
    (np.float64("nan"), None), (np.float32("inf"), None), (np.float64("-inf"), None),
])
def test_property_formatter_preserves_numpy_real_scalar_and_boolean_semantics(value, expected):
    result = property_record("density", value, "g/cm3", "test", "eq3", {})
    assert result["value"] == expected
    assert result["quality_status"] == ("passed" if expected is not None else "failed")
    assert result["reason"] == (None if expected is not None else "non_finite_or_missing")
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("uncertainty, expected", [
    (np.int32(2), 2.), (np.float32(.25), .25), (np.float64(0), 0.),
    (np.bool_(True), None), (np.float64(-1), None),
    (np.float64("nan"), None), (np.float32("inf"), None),
])
def test_property_formatter_preserves_numpy_uncertainty_semantics(uncertainty, expected):
    result = property_record("density", 1., "g/cm3", "test", "eq3", {}, uncertainty)
    assert result["uncertainty"] == expected
    assert result["value"] == 1. and result["quality_status"] == "passed"
    json.dumps(result, allow_nan=False)


def test_scientific_analysis_and_http_worker_share_the_same_property_formatter():
    from workers.polymer_md_worker.app.native_execution import property_record as http_property_record
    assert property_record is http_property_record
