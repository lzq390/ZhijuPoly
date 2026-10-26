"""Pure synthetic observation regressions, never MD or material validation."""
import json

import numpy as np
import pytest

from workers.polymer_md_worker.app.scientific_analysis import analyze_equilibrium, analyze_rnemd


def write_log(path, columns, rows):
    path.write_text("LAMMPS synthetic observations\n" + " ".join(columns) + "\n" +
        "\n".join(" ".join(str(value) for value in row) for row in rows) + "\nLoop time\n")


def rnemd_fixture(root, *, offset_fs=0, duration_ps=1000, interval_ps=1,
                  first_thermo_ps=0, signed_flux=1, bad_profile_ends=(), transient_end_ps=None,
                  noisy=False):
    """Known opposing gradients; v_heatflux is already cumulative SI J/m²."""
    root.mkdir(exist_ok=True)
    gradient, conductivity = 40 / 1e-8, .2
    flux = signed_flux * gradient * conductivity
    ends = np.arange(interval_ps, duration_ps + interval_ps / 2, interval_ps)
    thermo_times = np.arange(first_thermo_ps, duration_ps + interval_ps / 2, interval_ps)
    rows = []
    for ps in thermo_times:
        perturbation = 1e-5 * np.sin(ps * .17) if noisy else 0
        rows.append([round(ps * 5000), ps * 1000 + offset_fs, 300, 100, 100, 100,
                     7 + flux * ps * 1e-12 + perturbation])
    log = root / "tc.log"
    write_log(log, ["Step", "Time", "Temp", "Lx", "Ly", "Lz", "v_heatflux"], rows)
    lines = ["# Chunk-averaged data", "# Timestep Number-of-chunks Total-count",
             "# Chunk Coord1 Ncount temp density/mass"]
    for end in ends:
        lines.append(f"{round(end * 5000)} 20 2000")
        for chunk in range(20):
            coordinate = (chunk + .5) / 20
            temperature = 310 - 40 * coordinate if chunk < 10 else 270 + 40 * coordinate
            if noisy:
                temperature += .02 * np.sin(end * .11 + chunk * .3)
            if end in bad_profile_ends:
                # Both sides have low R², bracketed by subsequent good blocks.
                temperature += 20 * (-1)**chunk
            if transient_end_ps is not None and end == transient_end_ps:
                temperature += 1e12 * (chunk + 1)**2
            lines.append(f"{chunk+1} {coordinate} 100 {temperature} {1+coordinate**2}")
    profile = root / "slab.profile"
    profile.write_text("\n".join(lines) + "\n")
    return log, profile, gradient, flux


def analyze(paths, **kwargs):
    return analyze_rnemd(paths[0], paths[1], axis="x", stage="tc_analyze", **kwargs)


@pytest.mark.parametrize("duration_ps,burn_ps,expected", [(10, 2000, (None, None, 0)), (10, 9.1, (10, 10, 1))])
def test_eq_empty_or_short_window_describes_observed_and_selected_samples(tmp_path, duration_ps, burn_ps, expected):
    path = tmp_path / "eq.log"
    columns = ["Step", "Time", "Temp", "Density", "TotEng", "E_bond", "E_angle", "E_dihed", "E_vdwl", "Volume"]
    write_log(path, columns, [[i * 1000, i * 1000, 300, 1, 1000, 100, 100, 100, -100, 10000]
                              for i in range(duration_ps + 1)])
    result = analyze_equilibrium(path, stage="analyze_0", temp=300, pressure=1, burn_in_ps=burn_ps)
    for prop in result["properties"]:
        window = prop["analysis_range"]
        assert (window["start_ps"], window["stop_ps"], window["sample_count"]) == expected
        assert window["requested_start_ps"] == burn_ps
        assert window["observed_start_ps"] == 0 and window["observed_stop_ps"] == duration_ps
    json.dumps(result, allow_nan=False)


def test_empty_rnemd_has_null_production_window_and_preserves_offset(tmp_path):
    result = analyze(rnemd_fixture(tmp_path, duration_ps=10, offset_fs=2000))
    window = result["properties"][0]["analysis_range"]
    assert window == {"start_ps": None, "stop_ps": None, "requested_start_ps": 800.,
                      "observed_start_ps": 0., "observed_stop_ps": 10., "sample_count": 0}
    assert result["quality"]["production_profile_window"]["sample_count"] == 0
    assert result["quality"]["production_profile_support"]["start_ps"] is None
    assert result["quality"]["clock"]["native_time_offset_fs"] == 2000
    assert result["properties"][0]["value"] is None
    json.dumps(result, allow_nan=False)


def test_constant_native_offset_changes_no_windows_conductivity_or_block_uncertainty(tmp_path):
    zero = analyze(rnemd_fixture(tmp_path / "zero", noisy=True))
    shifted = analyze(rnemd_fixture(tmp_path / "shifted", offset_fs=2000, noisy=True))
    assert zero["quality_status"] == shifted["quality_status"] == "passed"
    for field in ("value", "uncertainty", "analysis_range"):
        assert zero["properties"][0][field] == shifted["properties"][0][field]
    assert zero["properties"][0]["uncertainty"] > 0
    for field in ("conductivity_block_estimates", "production_profile_window", "production_profile_support", "exchange_energy_fit"):
        assert zero["quality"][field] == shifted["quality"][field]
    assert zero["quality"]["clock"]["native_time_offset_fs"] == 0
    assert shifted["quality"]["clock"]["native_time_offset_fs"] == 2000


def test_absolute_burn_does_not_shift_with_late_first_thermo_observation(tmp_path):
    result = analyze(rnemd_fixture(tmp_path, first_thermo_ps=1, offset_fs=2000))
    window = result["properties"][0]["analysis_range"]
    assert window["requested_start_ps"] == window["start_ps"] == 800
    assert window["observed_start_ps"] == 1
    assert window["sample_count"] == 201
    assert result["quality"]["production_profile_window"]["start_ps"] == 801
    assert result["quality"]["production_profile_support"] == {
        "start_ps": 800., "stop_ps": 1000., "requested_start_ps": 800., "block_count": 200}


def test_nonempty_rnemd_windows_use_actual_sample_boundaries_at_coarse_cadence(tmp_path):
    result = analyze(rnemd_fixture(tmp_path, interval_ps=3, duration_ps=900))
    window = result["properties"][0]["analysis_range"]
    assert window == {"start_ps": 801., "stop_ps": 900., "requested_start_ps": 800.,
                      "observed_start_ps": 0., "observed_stop_ps": 900., "sample_count": 34}
    profile_window = result["quality"]["production_profile_window"]
    assert profile_window["start_ps"] == 804 and profile_window["stop_ps"] == 900
    assert profile_window["sample_count"] == 33
    assert result["quality"]["production_profile_support"]["start_ps"] == 801


def test_nonempty_short_rnemd_preserves_actual_bounds_without_a_conductivity(tmp_path):
    result = analyze(rnemd_fixture(tmp_path, duration_ps=810))
    window = result["properties"][0]["analysis_range"]
    assert window["start_ps"] == 800 and window["stop_ps"] == 810 and window["sample_count"] == 11
    profile_window = result["quality"]["production_profile_window"]
    assert profile_window["start_ps"] == 801 and profile_window["stop_ps"] == 810
    assert profile_window["sample_count"] == 10
    assert result["properties"][0]["value"] is None and result["quality_status"] == "not_converged"


def test_averaged_profile_ending_at_burn_is_fully_discarded(tmp_path):
    ordinary = analyze(rnemd_fixture(tmp_path / "ordinary"))
    transient = analyze(rnemd_fixture(tmp_path / "transient", transient_end_ps=800))
    assert transient["quality_status"] == "passed"
    assert transient["properties"] == ordinary["properties"]
    assert transient["series"] == ordinary["series"]
    assert transient["quality"] == ordinary["quality"]
    assert transient["quality"]["production_profile_blocks"] == 200
    assert transient["quality"]["profile_averaging"] == {
        "interval_steps": 5000, "interval_ps": 1., "fixed_interval_verified": True,
        "first_block_start_ps": 0., "block_support": "(previous_profile_end, profile_end]"}


def test_bad_middle_profile_does_not_contaminate_later_good_block_rate(tmp_path):
    result = analyze(rnemd_fixture(tmp_path, bad_profile_ends=(850,)))
    quality = result["quality"]
    assert quality["production_profile_blocks"] == 200
    assert quality["qualified_profile_blocks"] == 199
    assert quality["qualified_block_rate"] == pytest.approx(199 / 200)


def test_signed_gradient_and_exchange_fits_retain_units_intercepts_errors_and_windows(tmp_path):
    paths = rnemd_fixture(tmp_path, signed_flux=-1)
    result = analyze(paths)
    quality = result["quality"]
    left, right = (quality["temperature_gradient_fits"][side] for side in ("left", "right"))
    for fit, slope, intercept in ((left, -paths[2], 310), (right, paths[2], 270)):
        assert fit["slope_k_m"] == pytest.approx(slope)
        assert fit["intercept_k"] == pytest.approx(intercept)
        assert fit["r2"] == pytest.approx(1)
        assert fit["slope_standard_error_k_m"] >= 0
        assert fit["intercept_standard_error_k"] >= 0
        assert fit["fit_window"]["sample_count"] == 9
        assert fit["profile_time_window"]["start_ps"] == 801
    exchange = quality["exchange_energy_fit"]
    assert exchange["slope_w_m2"] == pytest.approx(paths[3]) and exchange["slope_w_m2"] < 0
    assert exchange["intercept_j_m2"] == pytest.approx(7)
    assert exchange["r2"] == pytest.approx(1)
    assert exchange["slope_standard_error_w_m2"] >= 0
    assert exchange["intercept_standard_error_j_m2"] >= 0
    assert exchange["fit_window"] == result["properties"][0]["analysis_range"]
    assert result["properties"][0]["value"] == pytest.approx(.2)
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("time_step_fs", [0, -.2, float("nan"), float("inf"), True])
def test_invalid_explicit_timestep_rejected_even_for_short_data(tmp_path, time_step_fs):
    with pytest.raises(ValueError, match="timestep"):
        analyze(rnemd_fixture(tmp_path, duration_ps=10), time_step_fs=time_step_fs)


@pytest.mark.parametrize("column,index,new_value", [
    ("Time", 5, 7000.1),  # +0.1 fs single-row offset change, monotonic native clock.
    ("Time", 5, 6000),    # repeated native Time with 2000 fs offset.
    ("Time", 5, float("nan")),
    ("Time", 5, float("inf")),
    ("Step", 5, 20000),   # duplicate production Step.
    ("Step", 5, 19999),   # backward production Step.
    ("Step", 5, 25000.5),
    ("Step", 5, float("nan")),
])
def test_invalid_native_clock_rejected_before_short_data_early_return(tmp_path, column, index, new_value):
    paths = rnemd_fixture(tmp_path, duration_ps=10, offset_fs=2000)
    lines = paths[0].read_text().splitlines()
    fields = lines[index + 2].split()
    fields[0 if column == "Step" else 1] = str(new_value)
    lines[index + 2] = " ".join(fields)
    paths[0].write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="RNEMD (Time|Step)"):
        analyze(paths)


def test_timestep_inconsistent_with_native_clock_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="constant finite offset"):
        analyze(rnemd_fixture(tmp_path), time_step_fs=.25)


@pytest.mark.parametrize("replacement", ["0 20 2000", "10001 20 2000", "5000 20 2000"])
def test_irregular_or_duplicate_profile_endpoints_fail_closed_even_when_short(tmp_path, replacement):
    paths = rnemd_fixture(tmp_path, duration_ps=10)
    lines = paths[1].read_text().splitlines()
    lines[3 + 21] = replacement
    paths[1].write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="profile (timesteps|averaging interval)"):
        analyze(paths)
