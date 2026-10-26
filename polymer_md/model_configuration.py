"""Pure sizing arithmetic for validated models; no engine or chemistry imports.

Repeat and terminal statistics are supplied by the API's RDKit validation.
The Worker independently checks actual constructed sizes before MD.
"""
from __future__ import annotations


def estimate_model_atoms(request: dict, components: dict[str, dict], terminals: list[dict]) -> int:
    model = request["model_configuration"]
    cap_atoms = sum(item["atoms"] for item in terminals)
    cap_mass = sum(item["mass"] for item in terminals)

    def counts_for(length, ids, ratios, alternating=False):
        target = length["value"]
        if length["mode"] == "dp":
            total = int(target) * (len(ids) if alternating else 1)
        else:
            measure = "atoms" if length["mode"] == "atoms" else "mass"
            cap = cap_atoms if measure == "atoms" else cap_mass
            per_unit = sum(components[key][measure] * ratio for key, ratio in zip(ids, ratios))
            total = round((target - cap) / per_unit)
            if alternating:
                total = max(1, round(total / len(ids))) * len(ids)
        if total < 1:
            raise ValueError("the requested chain length is smaller than one capped repeating unit")
        if alternating:
            return [total // len(ids)] * len(ids)
        if len(ids) == 1:
            return [total]
        counts = [int(total * ratio + .5) for ratio in ratios]
        largest = max(range(len(ratios)), key=ratios.__getitem__)
        counts[largest] += total - sum(counts)
        if any(value < 0 for value in counts):
            raise ValueError("composition cannot be represented at this chain length")
        return counts

    ids = [component["id"] for component in model["components"]]
    if model["mode"] == "blend":
        return sum(component["n_chain"] * (cap_atoms + counts_for(component["chain_length"],
            [component["id"]], [1.0])[0] * components[component["id"]]["atoms"])
            for component in model["components"])
    if model["mode"] == "block":
        per_chain = cap_atoms + sum(block["dp"] * components[block["component_id"]]["atoms"]
                                   for block in model["blocks"])
    else:
        ratios = model.get("ratios", [1 / len(ids)] * len(ids))
        ratios = [ratio / sum(ratios) for ratio in ratios]
        counts = counts_for(model["chain_length"], ids, ratios, model["mode"] == "alternating")
        per_chain = cap_atoms + sum(count * components[key]["atoms"] for key, count in zip(ids, counts))
    return int(per_chain * request["n_chain"])
