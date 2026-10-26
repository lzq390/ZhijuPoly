"""Synthetic measured-data and protocol checks; no QM or MD integration."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import numpy as np
import pytest

from workers.polymer_md_worker.app import posteq_execution as posteq
from workers.polymer_md_worker.app import scientific_runner as runner


def sp_log(path, *, interval_ps=1., early=10000., invalid=None):
    times = np.arange(0, 500. + interval_ps / 2, interval_ps)
    ced = np.where(times < 100., early, 100. + 30. * np.sin((times - 100) / 30))
    volume = 1000000. + np.sin(times / 20) * 10000.
    # Analytically independent unit conversion: 1 kcal/mol per box in 1 A³
    # becomes 4184/(NA*1e-24) J/cm³.
    inter = -ced * volume * 1e-24 * 6.02214076e23 / 4184.
    pair = inter + 5.  # long_total=-10, sum_chain_self=-5
    if invalid is not None:
        pair[-5] = invalid
    lines = ["Step Time Temp Volume E_long c_sp_pair_inter c_mol0_kspace c_mol1_kspace"]
    lines += [f"{t * 1000:g} {t * 1000:g} 300 {v:.15g} -10 {p:.15g} -2 -3" for t, v, p in zip(times, volume, pair)]
    lines += ["Loop time of 1 on 1 procs"]
    path.write_text("\n".join(lines) + "\n")
    return times, ced


def test_sp_native_averaging_order_units_discard_and_time_block_error(tmp_path):
    path = tmp_path / "sp.log"
    times, ced = sp_log(path)
    result = posteq.analyze_sp(path, n_chain=2, conditions={"pressure_atm": 1.})
    records = {item["key"]: item for item in result["properties"]}
    selected = ced[times >= 100.]
    assert records["sp_ced"]["value"] == pytest.approx(selected.mean())
    assert records["sp_total"]["value"] == pytest.approx(np.sqrt(selected).mean())
    assert records["sp_total"]["value"] != pytest.approx(np.sqrt(selected.mean()), rel=1e-5)
    expected_se = np.std([np.sqrt(chunk).mean() for chunk in np.array_split(selected, 5)], ddof=1) / np.sqrt(5)
    assert records["sp_total"]["uncertainty"] == pytest.approx(expected_se)
    assert records["sp_total"]["unit"] == "MPa^0.5" and records["sp_ced"]["unit"] == "J/cm3"
    assert records["sp_total"]["analysis_range"]["start_ps"] == 100.
    assert records["sp_total"]["analysis_range"]["stop_ps"] == 500.
    assert records["sp_total"]["conditions"]["block_count"] == 5
    assert {item["source_stage"] for item in result["properties"]} == {"sp"}
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), 1e8])
def test_invalid_or_negative_cohesion_frames_fail_instead_of_silently_dropping_samples(tmp_path, invalid):
    path = tmp_path / "sp.log"; sp_log(path, invalid=invalid)
    result = posteq.analyze_sp(path, n_chain=2)
    assert result["quality_status"] == "not_converged"
    assert all(item["value"] is None and item["reason"] for item in result["properties"])
    json.dumps(result, allow_nan=False)


def test_sp_window_uses_physical_time_when_output_frequency_changes(tmp_path):
    windows = []
    for cadence in (1., 2.):
        path = tmp_path / f"sp-{cadence}.log"; sp_log(path, interval_ps=cadence)
        window = posteq.analyze_sp(path, n_chain=2)["properties"][0]["analysis_range"]
        windows.append((window["start_ps"], window["stop_ps"]))
    assert windows == [(100., 500.), (100., 500.)]


def test_sp_rejects_a_sampling_temperature_that_did_not_reach_the_recorded_condition(tmp_path):
    path = tmp_path / "sp.log"; sp_log(path)
    result = posteq.analyze_sp(path, n_chain=2, conditions={"temperature_k": 800.})
    assert all(prop["value"] is None and "temperature_not_reached" in prop["reason"] for prop in result["properties"])


def test_total_sp_adapter_removes_TALLY_and_never_fabricates_decomposition_columns():
    md = SimpleNamespace(wf=[SimpleNamespace(add=["compute old all pe/mol/tally all"], thermo_style=[])])
    result = posteq.total_sp_workflow(md, 3)
    commands = "\n".join(result.wf[0].add)
    assert "pe/mol/tally" not in commands
    assert "compute sp_pair_inter all group/group all pair yes kspace no molecule inter" in commands
    assert result.wf[0].thermo_style == ["c_sp_pair_inter", "c_mol0_kspace", "c_mol1_kspace", "c_mol2_kspace"]
    assert all("vdw" not in column and "coul" not in column for column in result.wf[0].thermo_style)
    assert result.wf[0].rerun_keyword == "dump x y z ix iy iz vx vy vz"
    with pytest.raises(ValueError): posteq.total_sp_workflow(md, 1)
    with pytest.raises(ValueError): posteq.total_sp_workflow(md, 21)


def tg_log(path, *, transition=350., invalid=None, temperature_scale=1., cadence_fs=100):
    lines, origin = [], 0
    def density(temperature):
        return 1.15 - .00012 * (min(temperature, transition) - 100) - .00042 * max(temperature - transition, 0)
    targets = np.arange(800., 99., -10.)
    for index, target in enumerate(targets):
        for cooling in ([False, True] if index < len(targets) - 1 else [False]):
            lines.append("Step Time Temp Density")
            for offset in range(0, 10001, cadence_fs):
                temperature = target - (10 * offset / 10000 if cooling else 0)
                value = density(temperature) + np.sin(offset / 700) * .00002
                if invalid is not None and index == 20 and not cooling and offset == 5000:
                    value = invalid
                lines.append(f"{origin + offset} {origin + offset} {temperature * temperature_scale:.12g} {value:.15g}")
            lines.append("Loop time of 1 on 1 procs")
            origin += 10000
    path.write_text("\n".join(lines) + "\n")


def test_tg_supported_step_wise_density_transition_has_real_temperature_axis_and_block_uncertainty(tmp_path):
    path = tmp_path / "tg.log"; tg_log(path)
    result = posteq.analyze_tg(path, conditions={"temperature_max_k": 800., "temperature_min_k": 100.})
    prop = result["properties"][0]
    assert prop["key"] == "tg" and prop["unit"] == "K" and prop["source_stage"] == "tg"
    assert prop["value"] == pytest.approx(350., abs=.1)
    assert prop["uncertainty"] is not None and prop["uncertainty"] >= 0
    assert prop["quality_status"] == "warning" and "sensitivity_not_evaluated" in prop["reason"]
    assert result["quality"]["density_curve_point_count"] == 71
    assert result["quality"]["fit_r2"] > .99
    assert result["quality"]["low_temperature_slope"] > result["quality"]["high_temperature_slope"]
    points = result["series"][0]["points"]
    assert result["series"][0]["key"] == "tg_density"
    assert len(points) == 71 and points[0]["temperature_k"] == 800. and points[-1]["temperature_k"] == 100.
    assert all("time_ps" not in point and point["value"] > .45 for point in points)
    assert result["quality"]["plateau_analysis_windows"][0]["start_ps"] == 2.
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("transition", [50., 900.])
def test_tg_does_not_extrapolate_a_transition_outside_the_sampled_range(tmp_path, transition):
    path = tmp_path / "tg.log"; tg_log(path, transition=transition)
    result = posteq.analyze_tg(path)
    assert result["properties"][0]["value"] is None
    assert result["quality_status"] == "not_converged"
    assert "no_supported_in_range" in result["properties"][0]["reason"]


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1.])
def test_tg_invalid_plateau_does_not_pass_finite_fit_or_silently_drop_the_plateau(tmp_path, invalid):
    path = tmp_path / "tg.log"; tg_log(path, invalid=invalid)
    result = posteq.analyze_tg(path)
    assert result["properties"][0]["value"] is None and result["quality_status"] == "not_converged"
    assert "invalid_Tg_plateau" in result["properties"][0]["reason"]
    json.dumps(result, allow_nan=False)


def test_tg_uses_physical_plateau_discard_window_and_checks_actual_temperature(tmp_path):
    starts = []
    for cadence in (100, 125):
        path = tmp_path / f"tg-{cadence}.log"; tg_log(path, cadence_fs=cadence)
        result = posteq.analyze_tg(path)
        starts.append(result["quality"]["plateau_analysis_windows"][0]["start_ps"])
    assert starts == [2., 2.]
    path = tmp_path / "bad-temperature.log"; tg_log(path, temperature_scale=.8)
    assert "temperature_not_reached" in posteq.analyze_tg(path)["properties"][0]["reason"]


def test_posteq_requires_qualified_source_and_hash_bound_measured_files(tmp_path):
    path = tmp_path / "eq.log"; path.write_text("original observations")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    state = {"analysis": {"equilibrated": True}, "completed": ["analyze_0", "additional_1", "analyze_1", "sp_analyze", "tc_analyze"],
             "stages": {"additional_1": {"measured_files": {"eq.log": digest}}}}
    assert posteq.qualified_source(state, tmp_path) == "additional_1"
    path.write_text("tampered observations")
    with pytest.raises(RuntimeError, match="integrity mismatch"): posteq.qualified_source(state, tmp_path)
    state["analysis"]["equilibrated"] = False
    with pytest.raises(RuntimeError, match="qualified equilibrium"): posteq.qualified_source(state, tmp_path)


def test_runner_checkpoint_allows_serial_branches_without_losing_the_latest_equilibrium(tmp_path):
    request = {"smiles": "*CC*", "seed": 17}
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    for previous, following in (("analyze_1", "sp"), ("sp", "sp_analyze"), ("sp_analyze", "tc"), ("tc_analyze", "tg"), ("tg", "tg_analyze")):
        path = tmp_path / "checkpoint.json"; path.write_text(json.dumps({"completed": [previous], "request_sha256": digest}))
        assert runner.load_state(following, path, hashlib.sha256(path.read_bytes()).hexdigest(), request)["completed"][-1] == previous
    path.write_text(json.dumps({"completed": ["eq2"], "request_sha256": digest}))
    with pytest.raises(RuntimeError, match="stage identity mismatch"):
        runner.load_state("tg", path, hashlib.sha256(path.read_bytes()).hexdigest(), request)
