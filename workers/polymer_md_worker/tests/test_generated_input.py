"""Independent text/coefficient mutations, with no RadonPy/engine execution."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from rdkit import Chem

from workers.polymer_md_worker.app.generated_input import (
    BONDED_STYLES, forcefield_snapshot, validate_input, segment, equilibrium_schedule, _commands,
)
from workers.polymer_md_worker.app import scientific_runner as runner


def molecule(name="GAFF2_mod"):
    mol = Chem.MolFromSmiles("CCCC")
    mol.SetProp("pair_style", "lj")
    for key, value in BONDED_STYLES[name].items():
        mol.SetProp(key + "_style", value)
    for index, atom in enumerate(mol.GetAtoms()):
        atom.SetProp("ff_type", "c")
        for key, value in {"ff_epsilon": .1, "ff_sigma": 3.5, "AtomicCharge": .125 if index % 2 == 0 else -.125}.items():
            atom.SetDoubleProp(key, value)
    for bond in mol.GetBonds():
        bond.SetProp("ff_type", "c-c"); bond.SetDoubleProp("ff_k", 300.); bond.SetDoubleProp("ff_r0", 1.5)
    mol.angles = {0: SimpleNamespace(a=0, b=1, c=2, ff=SimpleNamespace(type="angle", k=40., theta0=109.5))}
    ff = (SimpleNamespace(type="torsion", k=.25, d0=-1, n=3) if name.startswith("Dreiding")
          else SimpleNamespace(type="torsion", m=1, k=[.25], n=[3], d0=[0.]))
    mol.dihedrals = {0: SimpleNamespace(a=0, b=1, c=2, d=3, ff=ff)}
    mol.impropers = {0: SimpleNamespace(a=0, b=1, c=2, d=3, ff=SimpleNamespace(type="improper", k=1., d0=-1, n=2, x0=0.))}
    return mol


def data_text(name="GAFF2_mod"):
    # Independently specified tiny force-field reference; not serialized by the validator.
    dihedral = "1 .25 -1 3" if name.startswith("Dreiding") else "1 1 .25 3 0"
    improper = "1 1 0" if name.startswith("Dreiding") else "1 1 -1 2"
    return f"""Synthetic data, no integration
4 atoms
3 bonds
1 angles
1 dihedrals
1 impropers
1 atom types
1 bond types
1 angle types
1 dihedral types
1 improper types
0 10 xlo xhi
0 10 ylo yhi
0 10 zlo zhi

Masses
1 12.011

Pair Coeffs
1 .1 3.5

Bond Coeffs
1 300 1.5

Angle Coeffs
1 40 109.5

Dihedral Coeffs
{dihedral}

Improper Coeffs
{improper}

Atoms # full
1 1 1 .125 0 0 0
2 1 1 -.125 1 0 0
3 1 1 .125 2 0 0
4 1 1 -.125 3 0 0

Bonds
1 1 1 2
2 1 2 3
3 1 3 4

Angles
1 1 1 2 3

Dihedrals
1 1 1 2 3 4

Impropers
1 1 1 2 3 4
"""


def input_text(name="GAFF2_mod", schedule=None, cadence=1000, xtc=True):
    style = BONDED_STYLES[name]
    text = "\n".join(["units real", "atom_style full", "boundary p p p", "pair_style lj/charmm/coul/long 8 12",
        "kspace_style pppm 1e-6", *[f"{key}_style {value}" for key, value in style.items()],
        "special_bonds amber", "pair_modify mix arithmetic", "read_data state.data", f"thermo {cadence}"])
    if xtc:
        text += f"\ndump dump1 all xtc {cadence} trajectory.xtc"
    for index, row in enumerate(schedule or [segment(5000000, 300, pressure=2.)]):
        text += f"\ntimestep {row['time_step_fs']}\nfix md{index} all {row['ensemble']} temp {row['temperature_start_k']} {row['temperature_stop_k']} 100"
        if row["ensemble"] == "npt":
            text += f" iso {row['pressure_start_atm']} {row['pressure_stop_atm']} 1000 nreset 1000"
        text += f"\nrun {row['steps']}\nunfix md{index}\n"
    return text


def fixture(tmp_path, name="GAFF2_mod", schedule=None):
    (tmp_path / "state.data").write_text(data_text(name))
    path = tmp_path / "input.in"; path.write_text(input_text(name, schedule=schedule))
    return path, forcefield_snapshot(molecule(name), name)


@pytest.mark.parametrize("name", list(BONDED_STYLES))
def test_all_five_forcefields_verify_actual_coefficients_and_schedule(tmp_path, name):
    path, snapshot = fixture(tmp_path, name)
    verified = validate_input(path, snapshot, stage="eq3", schedule=[segment(5000000, 300, pressure=2.)],
        thermo_steps=1000, trajectory_steps=1000, bulk_dump_steps=0, data_name="state.data")
    assert verified["verified"] and verified["forcefield"] == name
    assert verified["styles"]["improper"] == ("umbrella" if name.startswith("Dreiding") else "cvff")
    assert verified["data"]["coefficient_tables"]["Dihedral Coeffs"]["type_count"] == 1
    assert len(verified["data"]["sha256"]) == 64


@pytest.mark.parametrize("old,new", [(".1 3.5", ".2 3.5"), ("300 1.5", "301 1.5"), ("40 109.5", "41 109.5"),
    ("1 1 .25 3 0", "1 1 .26 3 0"), ("1 1 -1 2", "1 2 -1 2"), ("12.011", "13.011"),
    ("1 1 1 .125", "1 1 1 .126"), ("1 1 1 .125", "1 2 1 .125"),
    ("2 1 2 3", "2 1 2 4"), ("3 bonds", "2 bonds"), (".1 3.5", "nan 3.5")])
def test_changed_coefficients_charges_topology_and_chain_groups_fail_before_execution(tmp_path, old, new):
    path, snapshot = fixture(tmp_path)
    data = tmp_path / "state.data"; original = data.read_text(); assert old in original
    data.write_text(original.replace(old, new, 1))
    with pytest.raises(RuntimeError): validate_input(path, snapshot, stage="eq3")


@pytest.mark.parametrize("old,new", [("pair_style lj/charmm/coul/long 8 12", "pair_style lj/cut 12"),
    ("dihedral_style fourier", "dihedral_style harmonic"), ("kspace_style pppm 1e-6", "kspace_style none"),
    ("special_bonds amber", "special_bonds lj 0 0 1"), ("read_data state.data", "read_data other.data"),
    ("timestep 1.0", "timestep 2"), ("temp 300.0 300.0", "temp 300 310"), ("iso 2.0 2.0", "iso 1 1"),
    ("run 5000000", "run 5000001"), ("thermo 1000", "thermo 500"), ("xtc 1000", "xtc 500")])
def test_changed_real_input_controls_fail_closed(tmp_path, old, new):
    path, snapshot = fixture(tmp_path)
    original = path.read_text(); assert old in original; path.write_text(original.replace(old, new))
    with pytest.raises(RuntimeError):
        validate_input(path, snapshot, stage="eq3", schedule=[segment(5000000, 300, pressure=2.)],
            thermo_steps=1000, trajectory_steps=1000, data_name="state.data")


def test_data_atom_order_is_not_confused_with_molecular_identity(tmp_path):
    path, snapshot = fixture(tmp_path)
    data = tmp_path / "state.data"
    data.write_text(data.read_text().replace("1 1 1 .125 0 0 0\n2 1 1 -.125 1 0 0", "2 1 1 -.125 1 0 0\n1 1 1 .125 0 0 0"))
    assert validate_input(path, snapshot, stage="eq3")["verified"]


def test_locked_21_step_and_141_run_temperature_schedules_are_fully_checked(tmp_path):
    req = {"temp": 310., "pressure": 2.}
    schedules = [equilibrium_schedule(req, "eq2")]
    cooling = []
    for temperature in range(800, 100, -10):
        cooling += [segment(10000, temperature, pressure=2), segment(10000, temperature, temperature - 10, pressure=2)]
    schedules.append([*cooling, segment(10000, 100, pressure=2)])
    for schedule in schedules:
        path, snapshot = fixture(tmp_path, schedule=schedule)
        actual = validate_input(path, snapshot, stage="temperature_schedule", schedule=schedule)
        assert len(actual["run_schedule"]) in {21, 141}
        broken = path.read_text().replace("temp 600.0 600.0", "temp 600 601", 1) if len(schedule) == 21 else path.read_text().replace("temp 420.0 410.0", "temp 420 400", 1)
        assert broken != path.read_text(); path.write_text(broken)
        with pytest.raises(RuntimeError, match="schedule"): validate_input(path, snapshot, stage="temperature_schedule", schedule=schedule)


def test_verifier_rechecks_native_run_regeneration_at_the_last_exec_boundary(tmp_path, monkeypatch):
    calls = []
    class Engine:
        def __init__(self, **kwargs): pass
        def exec(self, **kwargs): calls.append("engine"); return SimpleNamespace(returncode=0)
    monkeypatch.setitem(__import__("sys").modules, "radonpy.sim", SimpleNamespace(lammps=SimpleNamespace(LAMMPS=Engine)))
    monkeypatch.setenv("LAMMPS_EXEC", "synthetic-no-execution")
    path, _ = fixture(tmp_path)
    engine = runner.checked_lammps(tmp_path)
    measured = runner.bind_input_verifier(engine, tmp_path, molecule(), {"forcefield": "GAFF2_mod"}, "eq3",
        schedule=[segment(5000000, 300, pressure=2.)], data_name="state.data")
    engine.exec(input_file="input.in")
    assert calls == ["engine"] and measured["verified"]
    assert (tmp_path / "input-verification.json").is_file()
    path.write_text(path.read_text().replace("run 5000000", "run 1"))
    with pytest.raises(RuntimeError, match="schedule"): engine.exec(input_file="input.in")
    assert calls == ["engine"]


def test_missing_or_duplicate_overrides_and_coefficient_files_are_rejected(tmp_path):
    path, snapshot = fixture(tmp_path)
    text = path.read_text()
    for command in ("bond_style harmonic", "pair_coeff * * .1 3.5", "include other.in", "shell true", "suffix omp"):
        path.write_text(text + "\n" + command)
        with pytest.raises(RuntimeError): validate_input(path, snapshot, stage="eq3")
    path.write_text(text); (tmp_path / "state.data").unlink()
    with pytest.raises(RuntimeError): validate_input(path, snapshot, stage="eq3")


def native_tc(tmp_path, axis="x"):
    # Captured byte-for-byte from the pinned native generator in the v7 real
    # failed input-only probe. Only local fixture data path and axis are changed.
    text = (Path(__file__).parent / "fixtures/native_rnemd_x.in").read_text()
    text = text.replace("axis    string x", "axis    string " + axis).replace("string nemd_TC-MP_x.data", "string state.data")
    (tmp_path / "state.data").write_text(data_text())
    path = tmp_path / "native.in"; path.write_text(text)
    return path, forcefield_snapshot(molecule(), "GAFF2_mod")


@pytest.mark.parametrize("axis,replication", [("x", "3 1 1"), ("y", "1 3 1"), ("z", "1 1 3")])
def test_full_captured_native_RNEMD_selects_one_axis_and_verifies_FF_and_real_runs(tmp_path, axis, replication):
    path, snapshot = native_tc(tmp_path, axis)
    commands = _commands(path.read_text())
    assert [line for line in commands if line.startswith("replicate ")] == ["replicate " + replication]
    assert len([line for line in commands if line.startswith("variable idx ")]) == 1
    assert not any(line.startswith("dump 1 ") for line in commands)
    assert any(line.startswith("dump 2 all xtc 1000 ") for line in commands)
    result = validate_input(path, snapshot, stage="tc", data_name="state.data", thermo_steps=1000,
        trajectory_steps=1000, bulk_dump_steps=0, schedule=[segment(10000, 300., dt=.2),
            {"ensemble": "nve", "steps": 50000, "time_step_fs": .2}])
    assert result["verified"] and result["styles"]["dihedral"] == "fourier"
    assert runner.validate_rnemd_input(path.read_text(), {"tc_axis": axis, "tc_rep": 3, "tc_ns": .01, "temp": 300})["rep"] == 3


@pytest.mark.parametrize("old,new", [("equal  3", "equal  5"), ("equal  0.200000", "equal  0.400000"),
    ("equal  50000\n", "equal  50001\n"), ("equal  300.000000", "equal  500.000000"),
    ("dihedst string fourier", "dihedst string harmonic"), ("fix             NVE all nve", "fix NVE all npt temp 300 300 100 iso 2 2 1000")])
def test_captured_RNEMD_does_not_lose_rep_T_dt_run_FF_or_pressure_tamper_protection(tmp_path, old, new):
    path, snapshot = native_tc(tmp_path)
    original = path.read_text(); assert old in original; path.write_text(original.replace(old, new, 1))
    with pytest.raises(RuntimeError):
        validate_input(path, snapshot, stage="tc", data_name="state.data",
            schedule=[segment(10000, 300., dt=.2), {"ensemble": "nve", "steps": 50000, "time_step_fs": .2}])
        runner.validate_rnemd_input(path.read_text(), {"tc_axis": "x", "tc_rep": 3, "tc_ns": .01, "temp": 300})


def test_variable_substitution_is_sequential_with_same_definition_and_explicit_delete():
    commands = _commands("variable n equal 10\nvariable n equal 10\nrun ${n}\nvariable n equal 20\nrun ${n}\nvariable n delete\nvariable n string state.data\nread_data ${n}")
    assert [line for line in commands if line.startswith("run ")] == ["run 10", "run 20"]
    assert commands[-1] == "read_data state.data"


def test_native_sampling_MSD_observation_only_allows_its_preceding_fix():
    commands = _commands("fix msd2 all ave/time 1 100 100 c_msd2[4] mode scalar\nvariable msd equal f_msd2\nrun 1000")
    assert commands[-2] == "variable msd equal f_msd2"
    with pytest.raises(RuntimeError): _commands("variable msd equal f_msd2")


@pytest.mark.parametrize("stage,run_count", [("eq1", 3), ("eq2", 21), ("eq3", 1)])
def test_complete_native_EQ_presets_validate_effective_outputs_in_every_run(tmp_path, stage, run_count):
    text = (Path(__file__).parent / ("fixtures/native_" + stage + ".in")).read_text()
    (tmp_path / "input.data").write_text(data_text()); path = tmp_path / "input.in"; path.write_text(text)
    result = validate_input(path, forcefield_snapshot(molecule(), "GAFF2_mod"), stage=stage,
        schedule=equilibrium_schedule({"temp": 300., "pressure": 1., "eq_ns": 5.}, stage),
        thermo_steps=1000, trajectory_steps=1000, bulk_dump_steps=0, data_name="input.data")
    outputs = [row for row in result["output_schedule"] if row["operation"] == "run"]
    assert len(outputs) == run_count
    assert all(row["active_outputs"][0]["every_steps"] == 1000 for row in outputs)
    assert all(row["active_outputs"][0]["file"] == "trajectory.xtc" for row in outputs)
    assert all(row["thermo_every_steps"] == 1000 for row in outputs)
    if stage == "eq1":
        assert result["output_schedule"][0] == {"operation": "minimize", "active_outputs": []}
        assert [row["active_outputs"][0]["sample_interval_ps"] for row in outputs] == [.1, 1., 1.]


@pytest.mark.parametrize("change", ["missing_undump", "active_redefinition", "missing_recreation", "changed_cadence",
    "changed_filename", "mid_run_disabled", "dump_modify_every", "different_ID_replacement"])
def test_native_EQ1_minimize_recreation_does_not_allow_silent_sampling_overrides(tmp_path, change):
    text = (Path(__file__).parent / "fixtures/native_eq1.in").read_text()
    definition = "dump xtc0 all xtc 1000 trajectory.xtc"
    first, remainder = text.split(definition, 1); middle, last = remainder.split(definition, 1)
    if change == "missing_undump": middle = middle.replace("undump xtc0", "")
    elif change == "active_redefinition": last = "\n" + definition + last
    elif change == "missing_recreation": definition = ""
    elif change == "changed_cadence": definition = definition.replace("1000", "500")
    elif change == "changed_filename": definition = definition.replace("trajectory.xtc", "other.xtc")
    elif change == "mid_run_disabled": last = last.replace("run 20000", "run 20000\nundump xtc0", 1)
    elif change == "dump_modify_every": last = "\ndump_modify xtc0 every 500\n" + last
    else:
        definition = definition.replace("xtc0", "replacement"); last = last.replace("dump_modify xtc0", "dump_modify replacement")
    text = first + "dump xtc0 all xtc 1000 trajectory.xtc" + middle + definition + last
    (tmp_path / "input.data").write_text(data_text()); path = tmp_path / "input.in"; path.write_text(text)
    with pytest.raises(RuntimeError):
        validate_input(path, forcefield_snapshot(molecule(), "GAFF2_mod"), stage="eq1",
            schedule=equilibrium_schedule({}, "eq1"), thermo_steps=1000, trajectory_steps=1000,
            bulk_dump_steps=0, data_name="input.data")


@pytest.mark.parametrize("text", ["run ${missing}", "variable x delete\nrun ${x}", "variable x equal random(0,1,2)",
    "variable x equal $(1+2)", "variable x equal unknown*2", "variable x equal 1**2", "variable x equal 1e999",
    'if "0" then "run ${missing}"', 'if "1 > 0" then "run 10"',
    "variable n string old\nvariable n string new\nread_data ${n}"])
def test_closed_parser_rejects_undefined_unsupported_dynamic_expressions_and_unsafe_redefinition(text):
    with pytest.raises(RuntimeError): _commands(text)
