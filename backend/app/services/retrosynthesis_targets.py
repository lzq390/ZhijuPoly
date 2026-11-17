"""Read-only target resolution. Never loads model weights or starts a job."""
from functools import lru_cache
from fastapi import HTTPException
from rdkit import Chem
from app.services.monomer_retrosynthesis import _infer_target_role
from app.services.smiles_to_iupac import normalize_iupac_name
from app.services.structure_2d import generate_2d_svg
from app.services import structure_library
from app.utils.exceptions import ModelArtifactError


def canonical_target(smiles):
    if not isinstance(smiles, str) or not smiles.strip() or len(smiles) > 8000:
        raise HTTPException(422, detail={"code": "invalid_retrosynthesis_target"})
    mol = Chem.MolFromSmiles(smiles.strip())
    if mol is None or not mol.GetNumAtoms() or any(a.GetAtomicNum() == 0 for a in mol.GetAtoms()) or len(Chem.GetMolFrags(mol)) != 1:
        raise HTTPException(422, detail={"code": "invalid_retrosynthesis_target", "message": "Provide one complete molecule without wildcard atoms."})
    return Chem.MolToSmiles(mol, canonical=True), mol


def target(smiles, source):
    canonical, mol = canonical_target(smiles)
    return {"input_smiles": smiles, "canonical_smiles": canonical,
            "inferred_target_role": _infer_target_role(mol),
            "preview_svg": generate_2d_svg(canonical), "source": source}


@lru_cache(maxsize=4)
def _tokenizer(model_id):
    try:
        from transformers import AutoTokenizer
        return AutoTokenizer.from_pretrained(model_id, local_files_only=True)
    except Exception as exc:
        raise ModelArtifactError("retrosynthesis tokenizer is unavailable locally") from exc


def validate_token_budget(canonical_smiles, model_id):
    ids = _tokenizer(model_id)(canonical_smiles, truncation=False, add_special_tokens=True)["input_ids"]
    if len(ids) > 512:
        raise HTTPException(422, detail={"code": "retrosynthesis_input_tokens_exceeded", "max_tokens": 512})


def resolve_target(connection, owner, body, *, structure_library_enabled):
    if body.source == "smiles":
        resolved = target(body.smiles, {"type": "smiles"})
        return {"status": "resolved", "target": resolved, "candidates": []}
    if body.source == "saved_structure":
        if not structure_library_enabled:
            raise HTTPException(503, detail={"code": "structure_library_unavailable"})
        if not connection.execute("SELECT to_regclass('structure_library.records') IS NOT NULL AS ready").fetchone()["ready"]:
            raise HTTPException(503,detail={"code":"structure_library_unavailable"})
        if body.record_id:
            row = structure_library.get_record(connection, owner, body.record_id)
            resolved = target(row["smiles"], {"type": "saved_structure", "record_id": str(row["id"]), "label": row["name"]})
            return {"status": "resolved", "target": resolved, "candidates": []}
        page = structure_library.list_records(connection, owner, body.query, body.limit, body.cursor)
        candidates = []
        for row in page["items"]:
            try:
                candidates.append(target(row["smiles"], {"type": "saved_structure", "record_id": str(row["id"]), "label": row["name"]}))
            except HTTPException:
                continue
        return {"status": "needs_clarification", "target": None, "candidates": candidates,
                "next_cursor": page.get("next_cursor"), "message": "Select a saved target structure."}
    # The legacy name normalizer performs Unicode and punctuation normalization.
    # Match its normalized exact name with a streaming server cursor; never send
    # the full dictionary to a model and never guess an unrecognized structure.
    normalized = normalize_iupac_name(body.name)
    if not connection.execute("SELECT to_regclass('pi.monomer_iupac') IS NOT NULL AS ready").fetchone()["ready"]:
        raise HTTPException(503,detail={"code":"name_resolution_unavailable"})
    if not normalized:
        return {"status":"needs_clarification","target":None,"candidates":[],"message":"Supply a local chemical name, SMILES or a saved structure."}
    candidates, seen = [], set()
    with connection.cursor(name="retrosynthesis_name_lookup") as cursor:
        cursor.execute("SELECT smiles,iupac_name FROM pi.monomer_iupac WHERE iupac_name IS NOT NULL ORDER BY smiles")
        for row in cursor:
            if normalize_iupac_name(row["iupac_name"]) != normalized:
                continue
            try:
                resolved = target(row["smiles"], {"type": "name", "label": row["iupac_name"]})
            except HTTPException:
                continue
            canonical = resolved["canonical_smiles"]
            if canonical not in seen:
                seen.add(canonical)
                candidates.append(resolved)
            if len(candidates) > body.limit:
                break
    if len(candidates) == 1:
        return {"status": "resolved", "target": candidates[0], "candidates": []}
    return {"status": "needs_clarification", "target": None, "candidates": candidates[:body.limit],
            "message": "Choose the intended structure or supply SMILES." if candidates else "No local exact name match; supply SMILES or select a saved structure."}
