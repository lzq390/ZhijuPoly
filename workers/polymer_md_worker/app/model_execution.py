"""JSON-only model planning and native linear-chain construction.

The science prefix has Pydantic 1. HTTP validation belongs to the separate
Worker prefix; this boundary validates the same finite, closed JSON contract.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
import math
import re

MODES = frozenset({"homopolymer", "alternating", "random", "block", "blend"})


@contextmanager
def native_proximity_guard():
    """An H cap can leave no fragment atoms to test for nonbonded contacts.

    The pinned native routine reduces that empty distance matrix with nanmin.
    An empty finite fragment has no contacts; every nonempty test still uses
    the original routine. Invalid coordinates must never pass this exception.
    The override is local to this isolated scientific process and construction.
    """
    import numpy as np
    from radonpy.core import poly
    original = poly.check_3d_proximity

    def checked(coord1, coord2=None, **kwargs):
        arrays = [np.asarray(coord1, dtype=float)]
        if coord2 is not None:
            arrays.append(np.asarray(coord2, dtype=float))
        if any(not np.isfinite(array).all() for array in arrays):
            return False
        if any(array.size == 0 for array in arrays):
            return True
        return original(coord1, coord2=coord2, **kwargs)

    poly.check_3d_proximity = checked
    try:
        yield
    finally:
        poly.check_3d_proximity = original


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("invalid " + label)
    return value


def _length(value):
    if not isinstance(value, dict) or set(value) != {"mode", "value"}:
        raise ValueError("invalid chain length configuration")
    mode, target = value["mode"], value["value"]
    # ChainLength.value is a finite JSON number in the HTTP contract. Its
    # serializer legitimately emits 100.0 for an integer atom/DP target.
    if mode in {"atoms", "dp"} and type(target) is float and math.isfinite(target) and target.is_integer():
        target = int(target)
    if mode == "atoms":
        _integer(target, 100, 5000, "atom target")
    elif mode == "dp":
        _integer(target, 1, 5000, "polymerization degree")
    elif mode == "molecular_weight":
        if isinstance(target, bool) or not isinstance(target, (int, float)) or not math.isfinite(target) or not 100 <= target <= 1e6:
            raise ValueError("invalid molecular weight target")
    else:
        raise ValueError("unsupported chain length mode")
    return {"mode": mode, "value": target}


def model_configuration(request: dict) -> dict | None:
    value = request.get("model_configuration")
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"version", "mode", "components", "chain_length", "ratios", "blocks"}:
        raise ValueError("unsupported model configuration fields")
    value = {key: deepcopy(item) for key, item in value.items() if item is not None}
    if type(value.get("version")) is not int or value["version"] != 1 or not isinstance(value.get("mode"), str) or value["mode"] not in MODES:
        raise ValueError("unsupported model configuration version or mode")
    mode, components = value["mode"], value.get("components")
    if not isinstance(components, list) or not 1 <= len(components) <= 4 or (len(components) == 1) != (mode == "homopolymer"):
        raise ValueError("invalid model components")
    identifiers = []
    for component in components:
        if not isinstance(component, dict) or set(component) - {"id", "smiles", "n_chain", "chain_length"}:
            raise ValueError("unsupported component fields")
        identifier, smiles = component.get("id"), component.get("smiles")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,15}", identifier):
            raise ValueError("invalid model component id")
        if not isinstance(smiles, str) or not 3 <= len(smiles) <= 2048 or not smiles.isascii() or any(char.isspace() or ord(char) < 32 for char in smiles) or smiles.count("*") != 2 or "." in smiles or "," in smiles:
            raise ValueError("component requires one linear repeat unit with two linkers")
        identifiers.append(identifier)
        if mode == "blend":
            _integer(component.get("n_chain"), 1, 20, "component chain count")
            component["chain_length"] = _length(component.get("chain_length"))
        elif any(component.get(key) is not None for key in ("n_chain", "chain_length")):
            raise ValueError("chain-species sizing is available only for blends")
    if len(set(identifiers)) != len(identifiers) or request.get("smiles") != components[0]["smiles"]:
        raise ValueError("component identities or top-level smiles alias are inconsistent")
    if mode in {"homopolymer", "alternating", "random"}:
        value["chain_length"] = _length(value.get("chain_length"))
    elif "chain_length" in value:
        raise ValueError("this model does not accept a global chain length")
    if mode == "random":
        ratios = value.get("ratios")
        if not isinstance(ratios, list) or len(ratios) != len(components) or any(isinstance(r, bool) or not isinstance(r, (int, float)) or not math.isfinite(r) or r <= 0 for r in ratios) or not math.isclose(sum(ratios), 1., abs_tol=1e-8, rel_tol=0):
            raise ValueError("random copolymer requires positive normalized component ratios")
    elif "ratios" in value:
        raise ValueError("ratios are available only for random copolymers")
    if mode == "block":
        blocks = value.get("blocks")
        if not isinstance(blocks, list) or not 2 <= len(blocks) <= 16:
            raise ValueError("block copolymer requires two to sixteen explicit blocks")
        for block in blocks:
            if not isinstance(block, dict) or set(block) != {"component_id", "dp"} or block["component_id"] not in identifiers:
                raise ValueError("invalid block component")
            _integer(block["dp"], 1, 5000, "block polymerization degree")
        if {block["component_id"] for block in blocks} != set(identifiers):
            raise ValueError("every block component must be used")
    elif "blocks" in value:
        raise ValueError("blocks are available only for block copolymers")
    if mode == "blend" and sum(component["n_chain"] for component in components) != request.get("n_chain"):
        raise ValueError("blend chain count differs from the request")
    return value


def components(request: dict) -> list[dict]:
    config = model_configuration(request)
    return config["components"] if config else [{"id": "A", "smiles": request["smiles"]}]


def _random_counts(dp, ratios):
    """Match the pinned gen_monomer_array rounding without consuming RNG."""
    normalized = [ratio / sum(ratios) for ratio in ratios]
    counts = [int(dp * ratio + .5) for ratio in normalized]
    largest = max(range(len(normalized)), key=normalized.__getitem__)
    counts[largest] += dp - sum(counts)
    if min(counts) < 0:
        raise ValueError("native random composition rounding would be negative")
    return counts


def plan_model(request: dict, mols: list, head, tail) -> dict:
    from radonpy.core import poly, calc
    config = model_configuration(request)
    if config is None:
        config = {"version": 1, "mode": "homopolymer", "components": components(request),
                  "chain_length": {"mode": "atoms", "value": request["n_atom"]}}
    if len(mols) != len(config["components"]):
        raise ValueError("every model component requires its own verified QM molecule")
    unit_atoms = [poly.remove_linker_atoms(mol).GetNumAtoms() for mol in mols]
    end_atoms = sum(poly.remove_linker_atoms(mol).GetNumAtoms() for mol in (head, tail))
    if min(unit_atoms) <= 0:
        raise ValueError("empty repeat unit after linker removal")

    def length(target, indices, ratios):
        chosen = [mols[i] for i in indices]
        if target["mode"] == "dp":
            dp = target["value"]
        else:
            function = poly.calc_n_from_num_atoms if target["mode"] == "atoms" else poly.calc_n_from_mol_weight
            dp = function(chosen, target["value"], ratio=ratios, terminal1=head, terminal2=tail)
            if isinstance(dp, bool) or dp is None or not math.isfinite(float(dp)) or int(dp) != dp:
                raise ValueError("native sizing did not return an integer polymerization degree")
            dp = int(dp)
        if dp < 1 and config["mode"] != "alternating":
            raise ValueError("chain target is too small for its repeat units and termini")
        return dp

    def species(identifier, counts, chain_count, requested, native_n):
        estimate = end_atoms + sum(n * atoms for n, atoms in zip(counts, unit_atoms))
        return {"id": identifier, "component_counts": dict(zip((c["id"] for c in config["components"]), counts)),
                "total_dp": sum(counts), "n_chain": chain_count, "estimated_atoms_per_chain": estimate,
                "requested_chain_length": requested, "native_n": native_n}

    mode, rows = config["mode"], []
    size = len(mols)
    if mode == "blend":
        for i, component in enumerate(config["components"]):
            dp = length(component["chain_length"], [i], [1.])
            counts = [0] * size; counts[i] = dp
            rows.append(species(component["id"], counts, component["n_chain"], component["chain_length"], dp))
    elif mode == "block":
        counts = [0] * size
        for block in config["blocks"]:
            i = next(i for i, c in enumerate(config["components"]) if c["id"] == block["component_id"])
            counts[i] += block["dp"]
        rows.append(species("chain", counts, request["n_chain"], None, [b["dp"] for b in config["blocks"]]))
    else:
        ratios = config.get("ratios", [1. / size] * size)
        dp = length(config["chain_length"], list(range(size)), ratios)
        if mode == "alternating":
            cycles = dp if config["chain_length"]["mode"] == "dp" else max(1, round(dp / size))
            counts, native_n = [cycles] * size, cycles
        else:
            counts = [dp] if mode == "homopolymer" else _random_counts(dp, ratios)
            native_n = dp
        if mode == "random":
            # Independent sequences, never present one copied sequence as an ensemble.
            for i in range(request["n_chain"]):
                rows.append(species(f"chain_{i + 1}", counts, 1, config["chain_length"], native_n))
        else:
            rows.append(species("chain", counts, request["n_chain"], config["chain_length"], native_n))
    expected_atoms = sum(row["estimated_atoms_per_chain"] * row["n_chain"] for row in rows)
    check_atom_budget(request, expected_atoms)
    return {"configuration": config, "chain_species": rows, "estimated_atom_count": expected_atoms,
            "repeat_unit_molecular_weights_g_mol": [float(calc.molecular_weight(mol)) for mol in mols]}


def check_atom_budget(request: dict, count: int):
    if type(count) is not int or count <= 0 or count > 20000 or (request["calculation"] == "basic_tc" and count * request["tc_rep"] > 60000):
        raise ValueError("actual or planned model exceeds the equilibrium/RNEMD atom budget")


def residue_sequence(chain, labels: dict[str, str]) -> list[str]:
    residues = {}
    for atom in chain.GetAtoms():
        info = atom.GetPDBResidueInfo()
        if info is None:
            raise RuntimeError("native chain has no residue sequence evidence")
        residue, label = info.GetResidueNumber(), info.GetResidueName().strip()
        if residue in residues and residues[residue] != label:
            raise RuntimeError("inconsistent native residue labels")
        residues[residue] = label
    if Counter(residues.values())["TU0"] != 1 or Counter(residues.values())["TU1"] != 1:
        raise RuntimeError("native chain must preserve both explicit terminal residues")
    sequence = []
    for _, label in sorted(residues.items()):
        if label in {"TU0", "TU1"}:
            continue
        if label not in labels:
            raise RuntimeError("unknown component residue in native polymer")
        sequence.append(labels[label])
    return sequence


def construct_model(request: dict, mols: list, head, tail, directory, make_ff, mpi_ranks) -> tuple[object, dict]:
    with native_proximity_guard():
        return _construct_model(request, mols, head, tail, directory, make_ff, mpi_ranks)


def _construct_model(request: dict, mols: list, head, tail, directory, make_ff, mpi_ranks) -> tuple[object, dict]:
    from radonpy.core import poly, calc, utils
    plan = plan_model(request, mols, head, tail)
    config, rows = plan["configuration"], plan["chain_species"]
    mode = config["mode"]
    ids = [component["id"] for component in config["components"]]
    labels = [f"R{i:02d}" for i in range(len(ids))]
    chains, records = [], []
    for row in rows:
        work = directory / row["id"]
        # Only generated species IDs enter paths, never user component IDs.
        if mode == "blend":
            work = directory / f"species_{len(chains) + 1}"
        work.mkdir(parents=True, mode=0o700)
        kwargs = {"tacticity": request["tacticity"], "ter1": head, "ter2": tail,
                  "ff": make_ff(request), "work_dir": str(work), "omp": 0,
                  "mpi": mpi_ranks(row["estimated_atoms_per_chain"]), "gpu": 0}
        if mode in {"homopolymer", "blend"}:
            index = ids.index(row["id"]) if mode == "blend" else 0
            chain = poly.polymerize_rw(mols[index], row["native_n"], res_name=labels[index], **kwargs)
        elif mode == "alternating":
            chain = poly.copolymerize_rw(mols, row["native_n"], res_name=labels, **kwargs)
        elif mode == "random":
            normalized = [ratio / sum(config["ratios"]) for ratio in config["ratios"]]
            chain = poly.random_copolymerize_rw(mols, row["native_n"], ratio=normalized, res_name=labels, **kwargs)
        else:
            indices = [ids.index(block["component_id"]) for block in config["blocks"]]
            chain = poly.block_copolymerize_rw([mols[i] for i in indices], row["native_n"], res_name=[labels[i] for i in indices], **kwargs)
        if chain is None or not make_ff(request).ff_assign(chain):
            raise RuntimeError("native polymer construction or forcefield assignment failed")
        sequence = residue_sequence(chain, dict(zip(labels, ids)))
        observed = dict.fromkeys(ids, 0); observed.update(Counter(sequence))
        if observed != row["component_counts"] or len(sequence) != row["total_dp"]:
            raise RuntimeError("native sequence differs from the requested model composition")
        expected = ([ids[0]] * row["native_n"] if mode == "homopolymer" else
                    [row["id"]] * row["native_n"] if mode == "blend" else
                    ids * row["native_n"] if mode == "alternating" else
                    [block["component_id"] for block in config.get("blocks", []) for _ in range(block["dp"])])
        if mode != "random" and sequence != expected:
            raise RuntimeError("native sequence order differs from the requested model")
        records.append({**row, "sequence": sequence, "actual_dp": len(sequence), "atoms_per_chain": chain.GetNumAtoms(),
                        "molecular_weight_g_mol": float(calc.molecular_weight(chain)), "actual_tacticity": poly.get_tacticity(chain)})
        chains.append(chain)
    actual_atoms = sum(chain.GetNumAtoms() * row["n_chain"] for chain, row in zip(chains, records))
    check_atom_budget(request, actual_atoms)
    counts = [row["n_chain"] for row in records]
    cell = poly.amorphous_cell(chains, counts, density=.05)
    if cell is None or cell.GetNumAtoms() != actual_atoms or utils.count_mols(cell) != request["n_chain"]:
        raise RuntimeError("native packed cell does not contain the requested chain species")
    check_atom_budget(request, cell.GetNumAtoms())
    stats = poly.polymer_stats(cell)
    statistics = {"n_mol": int(stats["n_mol"]), "n_atom_mean": float(stats["n_atom_mean"]),
                  "n_atom_var": float(stats["n_atom_var"]), "Mn": float(stats["Mn"]), "Mw": float(stats["Mw"]),
                  "dispersity": float(stats["Mw/Mn"]), "molecular_weight_unit": "g/mol", "method": "radonpy.core.poly.polymer_stats"}
    if not all(math.isfinite(statistics[key]) and statistics[key] > 0 for key in ("Mn", "Mw", "dispersity")):
        raise RuntimeError("invalid native polymer molecular-weight statistics")
    composition = Counter()
    for row in records:
        for identifier, count in row["component_counts"].items():
            composition[identifier] += count * row["n_chain"]
    total_units = sum(composition.values())
    actual_composition = [{"id": component["id"], "smiles": component["smiles"],
        "repeat_unit_count": composition[component["id"]], "repeat_unit_mole_fraction": composition[component["id"]] / total_units}
        for component in config["components"]]
    single = len(records) == 1
    metadata = {"model_configuration": config, "mode": mode, "chain_species": records,
                "actual_composition": actual_composition, "polymer_statistics": statistics,
                "actual_dp": records[0]["actual_dp"] if single else None,
                "atoms_per_chain": records[0]["atoms_per_chain"] if single else None,
                "estimated_atom_count": plan["estimated_atom_count"], "actual_atom_count": actual_atoms,
                "n_chain": request["n_chain"], "initial_density_g_cm3": .05, "seed": request["seed"],
                "forcefield": request.get("forcefield", "GAFF2_mod"), "tacticity": request["tacticity"],
                "actual_tacticity": records[0]["actual_tacticity"] if single else None,
                "terminal_head": request["terminal_head"], "terminal_tail": request["terminal_tail"]}
    if mode == "random":
        metadata["effective_ratios"] = [ratio / sum(config["ratios"]) for ratio in config["ratios"]]
    return cell, metadata
