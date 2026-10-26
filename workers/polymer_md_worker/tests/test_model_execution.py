"""Native model dispatch and evidence with synthetic engines, never QM or MD."""
from copy import deepcopy
import json
import math
import os
import sys
from types import ModuleType

import numpy as np
import pytest
from rdkit import Chem

from polymer_md.contracts import ScientificRequest
from workers.polymer_md_worker.app import model_execution as models
from workers.polymer_md_worker.app import scientific_runner as runner
from workers.polymer_md_worker.app import native_execution as native


def test_h_terminal_empty_proximity_is_vacuous_but_nan_never_passes(synthetic_native):
    from radonpy.core import poly
    original = poly.check_3d_proximity
    calls = []
    def contact(coord1, coord2=None, **kwargs):
        calls.append((coord1, coord2, kwargs))
        return False
    poly.check_3d_proximity = contact
    try:
        with models.native_proximity_guard():
            assert poly.check_3d_proximity(np.empty((0, 3)), np.ones((1, 3))) is True
            assert poly.check_3d_proximity(np.ones((1, 3)), np.empty((0, 3))) is True
            assert poly.check_3d_proximity(np.empty((0, 3)), np.full((1, 3), np.nan)) is False
            assert poly.check_3d_proximity(np.ones((1, 3)), np.ones((1, 3)), dist_min=1.5) is False
            assert len(calls) == 1 and calls[0][2] == {"dist_min": 1.5}
        assert poly.check_3d_proximity is contact
    finally:
        poly.check_3d_proximity = original


def test_native_proximity_override_restores_after_construction_failure(synthetic_native):
    from radonpy.core import poly
    original = poly.check_3d_proximity
    with pytest.raises(RuntimeError):
        with models.native_proximity_guard():
            raise RuntimeError("synthetic construction failure")
    assert poly.check_3d_proximity is original


def model(mode, length=None, **extra):
    components = [{"id": "A", "smiles": "*CC*"}]
    if mode != "homopolymer": components.append({"id": "B", "smiles": "*CCO*"})
    if mode == "blend":
        components[0].update(n_chain=1, chain_length={"mode": "dp", "value": 3})
        components[1].update(n_chain=2, chain_length={"mode": "dp", "value": 5})
    value = {"version": 1, "mode": mode, "components": components}
    if mode in {"homopolymer", "alternating", "random"}:
        value["chain_length"] = length or {"mode": "dp", "value": 5}
    if mode == "random": value["ratios"] = [.4, .6]
    if mode == "block": value["blocks"] = [{"component_id": "A", "dp": 2}, {"component_id": "B", "dp": 3}, {"component_id": "A", "dp": 1}]
    value.update(extra)
    return value


def request(config, **overrides):
    values = ScientificRequest(smiles="*CC*", n_atom=100, n_chain=3 if config["mode"] == "blend" else 2,
                               seed=17, **overrides).model_dump(mode="json")
    values["model_configuration"] = config
    return values


@pytest.fixture
def synthetic_native(monkeypatch):
    modules = {}
    for name in ("radonpy", "radonpy.core", "radonpy.core.poly", "radonpy.core.calc", "radonpy.core.utils"):
        modules[name] = ModuleType(name); monkeypatch.setitem(sys.modules, name, modules[name])
    poly, calc, utils = (modules[name] for name in ("radonpy.core.poly", "radonpy.core.calc", "radonpy.core.utils"))
    poly.check_3d_proximity = lambda *args, **kwargs: True
    calls = []
    def clean(mol):
        value = Chem.RWMol(mol)
        for atom in reversed(list(value.GetAtoms())):
            if atom.GetAtomicNum() == 0: value.RemoveAtom(atom.GetIdx())
        return value.GetMol()
    poly.remove_linker_atoms = clean
    calc.molecular_weight = lambda mol: sum(atom.GetMass() for atom in mol.GetAtoms())
    def size(mols, target, ratio, terminal1, terminal2, *, mass=False):
        measure = calc.molecular_weight if mass else lambda m: clean(m).GetNumAtoms()
        return round((target - measure(terminal1) - measure(terminal2)) / sum(measure(mol) * fraction for mol, fraction in zip(mols, ratio)))
    poly.calc_n_from_num_atoms = lambda *args, **kwargs: size(*args, **kwargs)
    poly.calc_n_from_mol_weight = lambda *args, **kwargs: size(*args, **kwargs, mass=True)
    def chain(units, kwargs):
        # Intentionally synthetic geometry/element identities. These tests
        # verify protocol dispatch, sequence evidence and budgets, not science.
        value = Chem.RWMol()
        fragments = [("TU0", clean(kwargs["ter1"]).GetNumAtoms()), *units, ("TU1", clean(kwargs["ter2"]).GetNumAtoms())]
        for residue, (label, count) in enumerate(fragments, 1):
            for _ in range(count):
                atom = Chem.Atom("C")
                atom.SetMonomerInfo(Chem.AtomPDBResidueInfo("C", residueName=label, residueNumber=residue))
                index = value.AddAtom(atom)
                if index: value.AddBond(index - 1, index, Chem.BondType.SINGLE)
        mol = value.GetMol(); mol.AddConformer(Chem.Conformer(mol.GetNumAtoms()))
        return mol
    def homo(mol, n, res_name, **kwargs):
        calls.append(("homopolymer", n, res_name, kwargs))
        return chain([(res_name, clean(mol).GetNumAtoms())] * n, kwargs)
    def alternating(mols, n, res_name, **kwargs):
        calls.append(("alternating", n, res_name, kwargs))
        return chain([(label, clean(mol).GetNumAtoms()) for label, mol in zip(res_name, mols)] * n, kwargs)
    def random(mols, n, ratio, res_name, **kwargs):
        calls.append(("random", n, ratio, kwargs))
        counts = [int(n * r + .5) for r in ratio]; counts[int(np.argmax(ratio))] += n - sum(counts)
        units = [(label, clean(mol).GetNumAtoms()) for count, label, mol in zip(counts, res_name, mols) for _ in range(count)]
        np.random.shuffle(units)
        return chain(units, kwargs)
    def block(mols, n, res_name, **kwargs):
        calls.append(("block", n, res_name, kwargs))
        return chain([(label, clean(mol).GetNumAtoms()) for count, label, mol in zip(n, res_name, mols) for _ in range(count)], kwargs)
    poly.polymerize_rw, poly.copolymerize_rw, poly.random_copolymerize_rw, poly.block_copolymerize_rw = homo, alternating, random, block
    def pack(chains, counts, density):
        calls.append(("pack", list(counts), density))
        cell = Chem.Mol()
        for mol, n in zip(chains, counts):
            for _ in range(n): cell = Chem.CombineMols(cell, mol)
        return cell
    poly.amorphous_cell = pack
    poly.get_tacticity = lambda mol: "atactic"
    utils.count_mols = lambda mol: len(Chem.GetMolFrags(mol))
    def stats(cell):
        frags = Chem.GetMolFrags(cell, asMols=True, sanitizeFrags=False)
        masses = np.asarray([calc.molecular_weight(frag) for frag in frags])
        atoms = np.asarray([frag.GetNumAtoms() for frag in frags])
        mn, mw = masses.mean(), (masses**2).sum() / masses.sum()
        return {"n_mol": len(frags), "n_atom_mean": atoms.mean(), "n_atom_var": atoms.var(), "Mn": mn, "Mw": mw, "Mw/Mn": mw / mn}
    poly.polymer_stats = stats
    ff = type("FF", (), {"ff_assign": lambda self, mol: True})()
    mols = [Chem.AddHs(Chem.MolFromSmiles(smiles)) for smiles in ("*CC*", "*CCO*")]
    head = Chem.AddHs(Chem.MolFromSmiles("*C"))
    return poly, calc, utils, calls, mols, head, lambda request: ff


@pytest.mark.parametrize("mode", ["homopolymer", "alternating", "random", "block", "blend"])
def test_every_model_dispatches_to_its_native_builder_and_records_actual_composition(synthetic_native, tmp_path, mode):
    _, _, _, calls, mols, head, ff = synthetic_native
    config = model(mode)
    mols = mols[:len(config["components"])]
    req = request(config)
    cell, result = models.construct_model(req, mols, head, head, tmp_path, ff, lambda count: 1)
    assert result["actual_atom_count"] == cell.GetNumAtoms() == result["estimated_atom_count"]
    assert sum(row["n_chain"] for row in result["chain_species"]) == req["n_chain"]
    assert result["polymer_statistics"]["Mn"] > 0 and result["polymer_statistics"]["dispersity"] >= 1 - 1e-12
    counts = {item["id"]: item["repeat_unit_count"] for item in result["actual_composition"]}
    assert sum(item["repeat_unit_mole_fraction"] for item in result["actual_composition"]) == pytest.approx(1.)
    assert all(row["actual_dp"] == len(row["sequence"]) for row in result["chain_species"])
    if mode == "alternating":
        assert calls[0][0:2] == ("alternating", 5)
        assert result["actual_dp"] == 10 and result["chain_species"][0]["sequence"] == ["A", "B"] * 5
    elif mode == "random":
        assert [entry[0] for entry in calls].count("random") == req["n_chain"]
        assert counts == {"A": 4, "B": 6}
    elif mode == "block":
        assert calls[0][0:3] == ("block", [2, 3, 1], ["R00", "R01", "R00"])
        assert result["chain_species"][0]["sequence"] == ["A", "A", "B", "B", "B", "A"]
    elif mode == "blend":
        assert next(entry for entry in calls if entry[0] == "pack")[1] == [1, 2]
        assert counts == {"A": 3, "B": 10}
        masses = [row["molecular_weight_g_mol"] for row in result["chain_species"] for _ in range(row["n_chain"])]
        assert result["polymer_statistics"]["Mw"] == pytest.approx(sum(m * m for m in masses) / sum(masses))
    else:
        assert calls[0][0:2] == ("homopolymer", 5)
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("mode", ["atoms", "dp", "molecular_weight"])
def test_native_length_modes_and_http_float_serialization_are_honored(synthetic_native, mode):
    _, _, _, _, mols, head, _ = synthetic_native
    target = 5. if mode == "dp" else 100. if mode == "atoms" else 500.
    req = request(model("homopolymer", {"mode": mode, "value": target}))
    plan = models.plan_model(req, mols[:1], head, head)
    assert isinstance(plan["chain_species"][0]["native_n"], int)
    assert plan["chain_species"][0]["native_n"] == (5 if mode == "dp" else 15 if mode == "atoms" else 17)
    if mode != "molecular_weight": assert type(plan["configuration"]["chain_length"]["value"]) is int


def test_alternating_atom_target_is_converted_to_whole_cycles_not_multiplied_again(synthetic_native):
    _, _, _, _, mols, head, _ = synthetic_native
    plan = models.plan_model(request(model("alternating", {"mode": "atoms", "value": 100})), mols, head, head)
    assert plan["chain_species"][0]["native_n"] == 7
    assert plan["chain_species"][0]["total_dp"] == 14


def test_oversized_dp_model_is_rejected_before_native_construction(synthetic_native, tmp_path):
    _, _, _, calls, mols, head, ff = synthetic_native
    with pytest.raises(ValueError, match="atom budget"):
        models.construct_model(request(model("homopolymer", {"mode": "dp", "value": 5000})), mols[:1], head, head, tmp_path, ff, lambda count: 1)
    assert calls == []


def test_replication_budget_uses_actual_sizing_not_legacy_n_atom_placeholder(synthetic_native):
    _, _, _, _, mols, head, _ = synthetic_native
    req = request(model("homopolymer", {"mode": "dp", "value": 1300}), calculation="basic_tc", tc_rep=5)
    assert req["n_atom"] == 100
    with pytest.raises(ValueError, match="atom budget"):
        models.plan_model(req, mols[:1], head, head)


def test_native_sequence_mismatch_and_missing_packed_chains_are_fail_closed(synthetic_native, monkeypatch, tmp_path):
    poly, _, _, _, mols, head, ff = synthetic_native
    original = poly.copolymerize_rw
    monkeypatch.setattr(poly, "copolymerize_rw", lambda *args, **kwargs: original(*args, **{**kwargs, "res_name": list(reversed(kwargs["res_name"]))}))
    with pytest.raises(RuntimeError, match="sequence order"):
        models.construct_model(request(model("alternating")), mols, head, head, tmp_path / "missing_packed_chain", ff, lambda count: 1)
    monkeypatch.setattr(poly, "copolymerize_rw", original)
    monkeypatch.setattr(poly, "amorphous_cell", lambda chains, counts, density: chains[0])
    with pytest.raises(RuntimeError, match="packed cell"):
        models.construct_model(request(model("alternating")), mols, head, head, tmp_path, ff, lambda count: 1)


def test_bad_models_and_legacy_alias_mismatch_cannot_reach_a_native_call():
    req = request(model("random"))
    bad = []
    alias = deepcopy(req); alias["smiles"] = "*CCO*"; bad.append(alias)
    ratios = deepcopy(req); ratios["model_configuration"]["ratios"] = [.5, .6]; bad.append(ratios)
    id_value = deepcopy(req); id_value["model_configuration"]["components"][1]["id"] = "../escape"; bad.append(id_value)
    unknown = deepcopy(req); unknown["model_configuration"]["unexpected"] = True; bad.append(unknown)
    for item in bad:
        with pytest.raises(ValueError): models.model_configuration(item)
    assert models.model_configuration({"smiles": "*CC*"}) is None


@pytest.mark.parametrize("mode", ["homopolymer", "alternating", "random", "block", "blend"])
def test_real_http_model_dump_is_accepted_by_the_scientific_json_boundary(mode):
    values = ScientificRequest.model_validate(request(model(mode))).model_dump(mode="json")
    config = models.model_configuration(values)
    assert config["mode"] == mode
    if mode in {"homopolymer", "alternating", "random"}:
        assert type(config["chain_length"]["value"]) is int
    if mode == "blend":
        assert all(type(item["chain_length"]["value"]) is int for item in config["components"])


def test_runner_build_binds_all_verified_component_checkpoints_and_actual_optical_counts(synthetic_native, monkeypatch, tmp_path):
    _, _, utils, _, mols, head, ff = synthetic_native
    req = request(model("random"))
    definitions = req["model_configuration"]["components"]
    state = {"molecules": {"monomer_1": mols[0], "monomer_2": mols[1], "terminal_CH3": head},
             "qm_components": [{**component, "molecule_checkpoint": f"monomer_{i + 1}",
                                 "qm": {"qm_polarizability": float(i + 2), "mol_weight": float(28 + i * 16)}} for i, component in enumerate(definitions)]}
    monkeypatch.setattr(runner, "molecule", lambda state, key: state["molecules"][key])
    monkeypatch.setattr(runner, "store_molecule", lambda state, key, mol: state["molecules"].update({key: mol}))
    monkeypatch.setattr(runner, "make_forcefield", ff)
    utils.MolToPDBFile = lambda mol, path: None
    result = runner.build(req, state, tmp_path)
    assert [item["qm_polarizability"] for item in state["qm"]["components"]] == [2., 3.]
    assert [item["repeat_unit_count"] for item in state["qm"]["components"]] == [4, 6]
    assert result["actual_atom_count"] == state["molecules"]["cell"].GetNumAtoms()
    state["qm_components"][1]["smiles"] = "*CC*"
    with pytest.raises(RuntimeError, match="verified QM checkpoint"):
        runner.build(req, state, tmp_path)


def test_multicomponent_qm_uses_every_repeat_unit_and_own_directory(monkeypatch, tmp_path):
    seen = []
    def compute(req, state, directory):
        seen.append((req["smiles"], directory.name))
        state["molecules"]["monomer"] = {"smiles": req["smiles"]}
        state["qm"] = {"mol_weight": 28. if req["smiles"] == "*CC*" else 44., "qm_polarizability": 2. if req["smiles"] == "*CC*" else 4.}
        return {"convergence": [{"conformer": 0, "converged": True}]}
    monkeypatch.setattr(runner, "_qm_monomer_single", compute)
    state = {"molecules": {}}
    result = runner.qm_monomer(request(model("random")), state, tmp_path)
    assert seen == [("*CC*", "component_1"), ("*CCO*", "component_2")]
    assert state["molecules"]["monomer_2"]["smiles"] == "*CCO*"
    assert "qm_polarizability" not in state["qm"]
    assert [item["id"] for item in result["component_qm"]] == ["A", "B"]
    assert all(item["component_id"] in {"A", "B"} for item in result["convergence"])


def test_multicomponent_electronic_outputs_never_average_molecular_values():
    req = request(model("random"))
    req["native_configuration"] = {**native.DEFAULT_METHODS, "properties": ["qm_homo"]}
    result = native.qm_records(req, {"components": [{"id": "A", "smiles": "*CC*", "qm_homo": -4.}, {"id": "B", "smiles": "*CCO*", "qm_homo": -7.}]})[0]
    assert result["value"] is None and "per_component" in result["reason"]
    assert [item["value"] for item in result["conditions"]["components"]] == [-4., -7.]
    assert native.molecular_only(req) is False


def test_native_mixture_refraction_uses_actual_composition_and_all_QM_inputs(synthetic_native):
    _, calc, _, calls, _, _, _ = synthetic_native
    def refractive(alpha, density, mass, ratio):
        calls.append(("ri", alpha, density, mass, ratio.tolist()))
        phi = 4 * math.pi * density * 6.02214076e23 / 3 * sum(a * r for a, r in zip(alpha, ratio)) * 1e-24 / sum(m * r for m, r in zip(mass, ratio))
        return math.sqrt((1 + 2 * phi) / (1 - phi))
    calc.refractive_index = refractive
    density = {"key": "density", "value": 1., "unit": "g/cm3", "quality_status": "passed"}
    qm = {"components": [{"id": "A", "repeat_unit_count": 3, "qm_polarizability": 2., "mol_weight": 28.}, {"id": "B", "repeat_unit_count": 9, "qm_polarizability": 4., "mol_weight": 44.}]}
    result = runner.derive_optical_properties({"density": density}, qm, "eq3", {}, native=True)[0]
    assert result["value"] > 1 and calls[-1] == ("ri", [2., 4.], 1., [28., 44.], [.25, .75])
    assert result["conditions"]["qm"]["composition_source"] == "actual_constructed_repeat_unit_counts"
    qm["components"][1]["qm_polarizability"] = None
    failed = runner.derive_optical_properties({"density": density}, qm, "eq3", {}, native=True)[0]
    assert failed["value"] is None and "component_QM" in failed["reason"]


def test_native_thermal_diffusivity_calls_the_original_function_only_with_passed_dependencies(synthetic_native):
    _, calc, _, calls, _, _, _ = synthetic_native
    def compute(kappa, density, cp):
        calls.append(("diffusivity", kappa, density, cp)); return kappa / (density * 1000 * cp)
    calc.thermal_diffusivity = compute
    thermal = {"thermal_conductivity": {"value": .2, "unit": "W/(m K)", "quality_status": "passed"}}
    basic = {"density": {"value": 1., "unit": "g/cm3", "quality_status": "passed"}, "Cp": {"value": 2000., "unit": "J/(kg K)", "quality_status": "passed"}}
    result = runner.derive_thermal_diffusivity(thermal, basic, "tc", {}, native=True)
    assert result["value"] == pytest.approx(1e-7) and result["method"] == "radonpy.core.calc.thermal_diffusivity"
    assert calls == [("diffusivity", .2, 1., 2000.)]
    basic["Cp"]["quality_status"] = "warning"
    assert runner.derive_thermal_diffusivity(thermal, basic, "tc", {}, native=True)["value"] is None
    assert len(calls) == 1


@pytest.mark.parametrize("symlink", [False, True])
def test_stage_manifest_includes_component_evidence_and_rejects_symlinks(synthetic_native, monkeypatch, tmp_path, symlink):
    (tmp_path / "science").mkdir()
    monkeypatch.setenv("PATH", os.environ["PATH"])
    monkeypatch.setenv("LAMMPS_EXEC", "synthetic-no-engine")
    const = ModuleType("radonpy.core.const")
    monkeypatch.setitem(sys.modules, "radonpy.core.const", const)
    monkeypatch.setattr(runner, "probe_runtime", lambda: {"ready": True, "python": sys.executable,
        "lammps": "synthetic-no-engine", "mpi": "synthetic-no-mpi", "runtime_fingerprint": "f" * 64})
    monkeypatch.setattr(runner, "seed_engines", lambda seed: None)
    monkeypatch.setattr(runner, "load_state", lambda *args: {"completed": [], "molecules": {}})
    def compute(request, state, directory):
        component = directory / "component_2"; component.mkdir()
        (component / "psi4.log").write_text("synthetic convergence evidence\n")
        (component / "input.in").write_text("synthetic input, never executed\n")
        (component / "scratch.npy").write_bytes(b"excluded scratch")
        if symlink:
            outside = tmp_path / "outside"; outside.write_text("outside")
            (component / "external.log").symlink_to(outside)
        return {"properties": []}
    monkeypatch.setattr(runner, "qm_monomer", compute)
    if symlink:
        with pytest.raises(RuntimeError, match="symlink artifacts"):
            runner.execute({"seed": 17}, "monomer_qm", tmp_path / "science")
    else:
        result = runner.execute({"seed": 17}, "monomer_qm", tmp_path / "science")
        paths = {item["path"] for item in result["artifacts"]}
        assert {"monomer_qm/component_2/psi4.log", "monomer_qm/component_2/input.in", "monomer_qm/checkpoint.json", "monomer_qm/analysis.json"} <= paths
        assert "monomer_qm/component_2/scratch.npy" not in paths
