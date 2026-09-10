from __future__ import annotations

import hashlib
import importlib.metadata
from pathlib import Path

from app.services.monomer_polymerization import (
    _canonicalize_monomer_smiles,
    _load_smipoly_runtime,
    _smiles_key,
    parse_generated_rows,
)
from .storage import json_hash


ADAPTER_VERSION = 1


def engine_fingerprint() -> dict:
    runtime = _load_smipoly_runtime()
    rules = Path(runtime.polg.__file__).resolve().parent.parent / "rules"
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(rules.iterdir()) if path.is_file()}
    from app.services import monomer_polymerization, smiles_utils
    code_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in (*sorted(Path(__file__).parent.glob("*.py")),
                                Path(monomer_polymerization.__file__), Path(smiles_utils.__file__))}
    result = {"adapter_version": ADAPTER_VERSION, "code": code_hashes,
              "smipoly": importlib.metadata.version("smipoly"),
              "rdkit": importlib.metadata.version("rdkit"),
              "pandas": importlib.metadata.version("pandas"), "rules": hashes,
              "classification": {"minFG": 2, "maxFG": 4}}
    result["fingerprint"] = json_hash(result)
    return result


def canonicalize(value: str) -> str:
    if len(value) > 1000:
        raise ValueError("SMILES 长度不能超过 1000 个字符。")
    return _canonicalize_monomer_smiles("SMILES", value)


def classify(smiles: list[str]) -> dict:
    runtime = _load_smipoly_runtime()
    frame = runtime.pd.DataFrame([{"source_key": value, "SMILES": value} for value in smiles])
    classified = runtime.monc.moncls(frame, smiColn="SMILES", dsp_rsl=False)
    # moncls appends CO/HCHO with no source_key. Explicitly submitted molecules
    # keep their source_key, including user-supplied CO and HCHO.
    classified = classified[classified["source_key"].notna()]
    return {"rows": classified.to_dict(orient="records"), "errors": {}}


def generate(a: list[str], b: list[str], cache: dict, target: str) -> dict:
    runtime = _load_smipoly_runtime()
    errors = cache.get("errors", {})
    keys = set(a) | set(b)
    rows = [row for row in cache["rows"] if row["source_key"] in keys]
    candidates: dict[tuple[str, str], list[dict]] = {}
    seen: dict[tuple[str, str], set[tuple]] = {}
    if rows:
        classified = runtime.pd.DataFrame(rows).copy(deep=True)
        generated = runtime.polg.biplym(classified, targ=[target], dsp_rsl=False)
        if generated is None:
            raise RuntimeError("SMiPoly rejected the target class")
        a_keys, b_keys = set(a), set(b)
        for item in parse_generated_rows(generated):
            first, second = _smiles_key(item["monomer_a_smiles"]), _smiles_key(item["monomer_b_smiles"])
            if not first or not second or first == second:
                continue
            sources = set()
            if first in a_keys and second in b_keys:
                sources.add((first, second))
            if second in a_keys and first in b_keys:
                sources.add((second, first))
            key = (item["polymer_smiles"], item["polymer_class"], item["reaction_id"])
            for pair in sources:
                if key in seen.setdefault(pair, set()):
                    continue
                seen[pair].add(key)
                candidates.setdefault(pair, []).append({
                    "polymer_smiles": item["polymer_smiles"], "polymer_class": item["polymer_class"],
                    "reaction_id": item["reaction_id"], "reactset": sorted(item["reactset"]),
                    "engine_mon1_smiles": item["monomer_a_smiles"],
                    "engine_mon2_smiles": item["monomer_b_smiles"],
                })
    pairs = []
    for first in a:
        for second in b:
            products = candidates.get((first, second), [])
            products.sort(key=lambda item: (item["polymer_class"], item["reaction_id"] if item["reaction_id"] is not None else 10**9, item["polymer_smiles"]))
            error = errors.get(first) or errors.get(second)
            pairs.append({"a": first, "b": second, "candidates": products if not error else [],
                          "status": "error" if error else "success" if products else "no_match",
                          "error_code": "classification_error" if error else "identical_monomers" if first == second else "",
                          "message": error or ("当前双单体规则未生成候选。" if first == second else "")})
    return {"pairs": pairs}
