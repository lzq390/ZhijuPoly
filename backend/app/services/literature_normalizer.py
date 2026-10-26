"""Deterministic, versioned adapters for the read-only literature source.

Scientific assertions are copied from one extraction.  Neither related DWD
materials nor another extraction are used to fill a missing value.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any

NORMALIZER_VERSION = "literature-v2"
# Read legacy projections without reusing their normalization identity.
SUPPORTED_NORMALIZER_VERSIONS = frozenset({"literature-v1", "literature-v1.1", NORMALIZER_VERSION})
MAX_JSON_BYTES = 2 * 1024 * 1024
_DOI = re.compile(r"^10\.\d{4,9}/[^\s]+$", re.I)
_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.I)
_MULTIPLE_IDENTIFIERS = re.compile(r"[,，;；\s]+(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?10\.\d{4,9}/", re.I)
_EMPTY = {"", "unknown", "n/a", "na", "none", "null", "not provided", "not available", "未提供", "未知", "无", "治理时补齐", "不详"}


class NormalizationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def clean_doi(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = _PREFIX.sub("", value.strip()).strip().lower()
    return text or None


def normalize_doi(value: Any) -> str | None:
    text = clean_doi(value)
    return text if text and _DOI.fullmatch(text) else None


def document_uid(doi: str) -> str:
    return "literature:" + str(uuid.uuid5(uuid.NAMESPACE_URL, "nexpoly:literature:doi:" + doi))


def _text(value: Any) -> str | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text if text.lower() not in _EMPTY else None


def _meaningful(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_meaningful(v) for k, v in value.items() if k not in {"locationIndex", "位置索引", "block_id", "blockId", "md_range", "mdRange", "raw", "unit", "单位"})
    if isinstance(value, list):
        return any(_meaningful(v) for v in value)
    return value is not None and (isinstance(value, bool) or _text(value) is not None)


def _get(obj: dict, *keys: str) -> Any:
    for key in keys:
        if key in obj and obj[key] is not None:
            return obj[key]
    return None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str) and re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value.strip()):
        result = float(value)
        return result if math.isfinite(result) else None
    return None


def _strings(value: Any, *, keywords: bool = False) -> list[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            decoded = None
        if isinstance(decoded, list):
            value = decoded
        else:
            value = re.split(r"[,;，；\n]" if keywords else r"[;；\n]", value)
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, dict):
            item = _get(item, "name", "姓名", "display_name")
        cleaned = _text(item)
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _schema(raw: Any) -> tuple[dict, list, str]:
    if not isinstance(raw, dict):
        raise NormalizationError("unsupported_schema")
    english = isinstance(raw.get("paperInfo"), dict) and isinstance(raw.get("polymers"), list)
    chinese = isinstance(raw.get("文献信息"), dict) and isinstance(raw.get("聚合物"), list)
    if english and chinese:
        raise NormalizationError("ambiguous_schema")
    if english:
        return raw["paperInfo"], raw["polymers"], "polymers"
    if chinese:
        return raw["文献信息"], raw["聚合物"], "聚合物"
    raise NormalizationError("unsupported_schema")


def _identity(info: dict, expected: str) -> list[str]:
    values = [info[k] for k in ("document_id", "doi", "DOI", "唯一文献标识") if k in info and info[k] is not None and not (isinstance(info[k], str) and not info[k].strip())]
    if not values:
        return ["doi_identifier_missing"]
    for value in values:
        actual = normalize_doi(value)
        if actual is None or _MULTIPLE_IDENTIFIERS.search(clean_doi(value) or ""):
            # Only absence may inherit identity from paper_id. Opaque, malformed
            # or multiple identifiers require explicit reconciliation.
            raise NormalizationError("invalid_doi_identifier")
        if actual != expected:
            raise NormalizationError("doi_identity_conflict")
    return []


def _metadata(paper: dict, info: dict, doi: str) -> dict:
    year = _number(_get(info, "year", "年份"))
    if year is None:
        year = _number(paper.get("year"))
    year = int(year) if year is not None and int(year) == year and 1000 <= year <= 9999 else None
    return {
        "title": _text(_get(info, "title", "论文标题")) or _text(paper.get("title")) or doi,
        "doi": doi,
        "authors": _strings(_get(info, "authors", "作者列表")) or _strings(paper.get("authors_json")),
        "journal": _text(_get(info, "journal", "期刊名称")) or _text(paper.get("journal")),
        "year": year,
        "keywords": _strings(_get(info, "keywords", "keyword", "关键词"), keywords=True) or _strings(paper.get("keyword"), keywords=True),
        "abstract": _text(_get(info, "abstract", "摘要")) or _text(paper.get("abstract")),
    }


def _pointer(path: str, key: Any) -> str:
    return path + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _uid(snapshot: str, path: str, kind: str = "entry") -> str:
    return f"literature-{kind}:" + str(uuid.uuid5(uuid.NAMESPACE_URL, snapshot + "#" + path))


def _origin(extract_id: str, path: str) -> dict:
    return {"source_extract_id": extract_id, "json_pointer": path}


def _raw_extra(obj: dict, known: set[str]) -> dict:
    return {k: v for k, v in obj.items() if k not in known}


def _evidence(value: Any) -> list[dict]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            decoded = None
        value = decoded if isinstance(decoded, (dict, list)) else {"originalText": value}
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        entry = {"quote": _text(_get(item, "originalText", "original_text", "原文", "quote")), "block_id": _text(_get(item, "blockId", "block_id")), "md_range": _get(item, "mdRange", "md_range")}
        if entry["md_range"] == "":
            entry["md_range"] = None
        if any(v is not None for v in entry.values()) and entry not in result:
            result.append(entry)
    return result


def _value(raw: Any) -> dict:
    obj = raw if isinstance(raw, dict) else {"singleValue": raw}
    return {"single_value": _number(_get(obj, "singleValue", "single_value", "单值")), "min": _number(_get(obj, "min", "minValue", "valueMin", "最小值", "下限")), "max": _number(_get(obj, "max", "maxValue", "valueMax", "最大值", "上限")), "unit": _text(_get(obj, "unit", "单位")), "uncertainty": _get(obj, "uncertainty", "error", "误差"), "raw": raw}


def _has_value(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return _meaningful(raw)
    return any(_meaningful(v) for k, v in raw.items() if k not in {"unit", "单位", "uncertainty", "error", "误差"})


def _properties(value: Any, snapshot: str, extract_id: str, path: str) -> list[dict]:
    if not isinstance(value, list):
        return []
    out = []
    known = {"name", "名称", "abbreviation", "缩写", "category", "类别", "value", "值", "testCondition", "测试条件", "locationIndex", "位置索引"}
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        name, val = _text(_get(item, "name", "名称")), _get(item, "value", "值")
        if not name or not _has_value(val):
            continue
        pointer = _pointer(path, index)
        out.append({"entry_uid": _uid(snapshot, pointer), "name": name, "abbreviation": _text(_get(item, "abbreviation", "缩写")), "category": _text(_get(item, "category", "类别")), "value": _value(val), "conditions": _get(item, "testCondition", "测试条件"), "evidence": _evidence(_get(item, "locationIndex", "位置索引")), "origin": _origin(extract_id, pointer), "raw": _raw_extra(item, known)})
    return out


_AMOUNTS = {"massMeasurement": "mass", "质量": "mass", "volumeMeasurement": "volume", "体积": "volume", "molarAmountMeasurement": "molar_amount", "摩尔量": "molar_amount", "equivalentRatioMeasurement": "equivalent_ratio", "当量比": "equivalent_ratio", "purityMeasurement": "purity", "纯度": "purity"}


def _materials(value: Any, snapshot: str, extract_id: str, path: str) -> list[dict]:
    if not isinstance(value, list):
        return []
    out = []
    known = {"name", "原料名称", "type", "原料类别", "role", "单体类型", "smiles", "SMILES", "casNumber", "CAS号", "formula", "分子式", "locationIndex", "位置索引"} | set(_AMOUNTS)
    for i, item in enumerate(value):
        if not isinstance(item, dict) or not _text(_get(item, "name", "原料名称")):
            continue
        pointer = _pointer(path, i)
        amounts = [{"kind": kind, "value": _value(item[key])} for key, kind in _AMOUNTS.items() if key in item and _has_value(item[key])]
        out.append({"entry_uid": _uid(snapshot, pointer), "name": _text(_get(item, "name", "原料名称")), "type": _text(_get(item, "type", "原料类别")), "role": _text(_get(item, "role", "单体类型")), "smiles": _text(_get(item, "smiles", "SMILES")), "cas_number": _text(_get(item, "casNumber", "CAS号")), "formula": _text(_get(item, "formula", "分子式")), "amounts": amounts, "evidence": _evidence(_get(item, "locationIndex", "位置索引")), "origin": _origin(extract_id, pointer), "raw": _raw_extra(item, known)})
    return out


def _processes(value: Any, snapshot: str, extract_id: str, path: str) -> list[dict]:
    if not isinstance(value, list):
        return []
    out = []
    known = {"productName", "产物名称", "mass", "产物质量", "unit", "产物单位", "yieldPercent", "产物收率百分比", "materials", "原料", "reactionConditions", "反应条件", "postTreatment", "后处理步骤", "locationIndex", "位置索引"}
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        pointer = _pointer(path, i)
        material_key = "materials" if "materials" in item else "原料"
        materials = _materials(item.get(material_key), snapshot, extract_id, _pointer(pointer, material_key))
        conditions = _get(item, "reactionConditions", "反应条件")
        treatment = _get(item, "postTreatment", "后处理步骤")
        evidence = _evidence(_get(item, "locationIndex", "位置索引"))
        mass = _get(item, "mass", "产物质量")
        yield_percent = _get(item, "yieldPercent", "产物收率百分比")
        has_yield = not isinstance(yield_percent, bool) and _meaningful(yield_percent)
        if not (materials or _meaningful(conditions) or _meaningful(treatment) or _meaningful(mass) or has_yield or any(e.get("quote") for e in evidence)):
            continue
        out.append({"entry_uid": _uid(snapshot, pointer), "product_name": _text(_get(item, "productName", "产物名称")), "mass": _value(mass if isinstance(mass, dict) else {"singleValue": mass, "unit": _get(item, "unit", "产物单位")}), "yield_percent": _get(item, "yieldPercent", "产物收率百分比"), "materials": materials, "conditions": conditions, "post_treatment": treatment, "evidence": evidence, "origin": _origin(extract_id, pointer), "raw": _raw_extra(item, known)})
    return out


def normalize_extraction(paper: dict, extract: dict, *, assets: list[dict] | None = None) -> dict:
    """Return the immutable snapshot/catalog payload or a stable rejection code."""
    if str(extract.get("status", "")).strip().lower() != "success":
        raise NormalizationError("not_success")
    raw = extract.get("raw_json")
    try:
        raw_bytes = canonical_json(raw).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise NormalizationError("invalid_json_value") from None
    if len(raw_bytes) > MAX_JSON_BYTES:
        raise NormalizationError("raw_json_too_large")
    doi = normalize_doi(paper.get("doi"))
    if not doi or _MULTIPLE_IDENTIFIERS.search(doi):
        raise NormalizationError("invalid_paper_doi")
    info, polymers, polymer_key = _schema(raw)
    flags = _identity(info, doi)
    metadata = _metadata(paper, info, doi)
    assets = assets or []
    raw_hash = hashlib.sha256(raw_bytes).hexdigest()
    doc_uid = document_uid(doi)
    extract_id = str(extract["id"])
    created = extract.get("created_at")
    created_identity = created.isoformat() if isinstance(created, datetime) else created
    identity = {"document_uid": doc_uid, "paper_id": str(paper["id"]), "extract_id": extract_id, "extract_created_at": created_identity, "model_version": extract.get("model_version"), "raw_sha256": raw_hash, "metadata": metadata, "assets": assets, "normalizer_version": NORMALIZER_VERSION}
    snapshot = "literature-snapshot:" + str(uuid.uuid5(uuid.NAMESPACE_URL, canonical_json(identity)))
    samples = []
    known = {"name", "名称", "identityCode", "身份标识", "polymerCategoryName", "聚合物分类名称", "polymerCategoryCode", "聚合物分类编码", "sampleForm", "样本形态", "properties", "性质", "processFlows", "工艺流程", "characterization", "表征", "locationIndex", "位置索引"}
    for i, polymer in enumerate(polymers):
        if not isinstance(polymer, dict):
            continue
        path = _pointer(_pointer("", polymer_key), i)
        prop_key = "properties" if "properties" in polymer else "性质"
        flow_key = "processFlows" if "processFlows" in polymer else "工艺流程"
        properties = _properties(polymer.get(prop_key), snapshot, extract_id, _pointer(path, prop_key))
        processes = _processes(polymer.get(flow_key), snapshot, extract_id, _pointer(path, flow_key))
        name = _text(_get(polymer, "name", "名称"))
        identity_code = _text(_get(polymer, "identityCode", "身份标识"))
        category = _text(_get(polymer, "polymerCategoryName", "聚合物分类名称", "polymerCategoryCode", "聚合物分类编码"))
        form = _text(_get(polymer, "sampleForm", "样本形态"))
        characterization = _get(polymer, "characterization", "表征")
        if not (properties or processes or _meaningful(characterization) or ((name or identity_code) and (category or form))):
            continue
        samples.append({"sample_uid": _uid(snapshot, path, "sample"), "name": name, "identity_code": identity_code, "category": category, "form": form, "properties": properties, "processes": processes, "characterization": characterization, "evidence": _evidence(_get(polymer, "locationIndex", "位置索引")), "origin": _origin(extract_id, path), "raw": _raw_extra(polymer, known)})
    if not samples:
        raise NormalizationError("empty_extracted_content")
    if any(p["value"]["unit"] is None for s in samples for p in s["properties"]):
        flags.append("property_unit_missing")
    normalized = {"metadata": metadata, "samples": samples, "quality_flags": flags, "raw": _raw_extra(raw, {"paperInfo", "文献信息", "polymers", "聚合物"})}
    if len(canonical_json(normalized).encode("utf-8")) > MAX_JSON_BYTES:
        raise NormalizationError("normalized_json_too_large")
    counts = {"samples": len(samples), "properties": sum(len(s["properties"]) for s in samples), "processes": sum(len(s["processes"]) for s in samples), "materials": sum(len(p["materials"]) for s in samples for p in s["processes"]), "characterizations": sum(_meaningful(s["characterization"]) for s in samples)}
    available = [a for a in assets if a.get("status") == "available" and a.get("etag")]
    has_fulltext = any(a.get("role") == "full_text" for a in available)
    has_pdf = any(a.get("role") == "pdf" for a in available)
    attachment_count = sum(a.get("role") == "attachment" for a in available)
    has_evidence = any(
        evidence.get("quote")
        for sample in samples
        for entry in (
            sample,
            *sample["properties"],
            *sample["processes"],
            *(material for process in sample["processes"] for material in process["materials"]),
        )
        for evidence in entry["evidence"]
    )
    capabilities = {"has_properties": counts["properties"] > 0, "has_processes": counts["processes"] > 0, "has_original": has_fulltext or has_pdf, "has_fulltext": has_fulltext, "has_pdf": has_pdf, "has_evidence": has_evidence, "has_attachments": attachment_count > 0, "attachment_count": attachment_count}
    return {"snapshot_uid": snapshot, "document_uid": doc_uid, "source_paper_id": str(paper["id"]), "source_extract_id": extract_id, "source_extract_created_at": extract.get("created_at"), "model_version": extract.get("model_version"), "normalizer_version": NORMALIZER_VERSION, "content_sha256": raw_hash, "raw_json": raw, "normalized_json": normalized, "assets": assets, "quality_flags": flags, "metadata": metadata, "counts": counts, "capabilities": capabilities}


def _sort_extract(extract: dict) -> tuple[datetime, int]:
    value = extract.get("created_at")
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        value = datetime.min.replace(tzinfo=timezone.utc)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value, int(extract["id"])


def _metadata_audit(chosen: dict, paper: dict) -> tuple[list[dict], dict | None]:
    """Complete per-document metadata diff without copying full abstracts."""
    metadata = chosen["metadata"]
    original = _metadata(paper, {}, metadata["doi"])
    differences = []
    for key in metadata:
        if key == "doi" or metadata[key] == original[key]:
            continue
        item = {"doi": metadata["doi"], "extract_id": chosen["source_extract_id"], "paper_id": str(paper["id"]), "selected_paper_id": chosen["source_paper_id"], "field": key}
        if key == "abstract":
            item.update(source_length=len(original[key] or ""), selected_length=len(metadata[key] or ""), source_sha256=json_sha256(original[key]), selected_sha256=json_sha256(metadata[key]))
        else:
            item.update(source_value=original[key], selected_value=metadata[key])
        differences.append(item)
    missing = [key for key, value in metadata.items() if not _meaningful(value)]
    gaps = {"doi": metadata["doi"], "extract_id": chosen["source_extract_id"], "metadata_fields": missing, "sample_name_missing": [], "property_unit_missing": [], "property_numeric_value_unparsed": []}
    for sample in chosen["normalized_json"]["samples"]:
        if not sample["name"]:
            gaps["sample_name_missing"].append(sample["origin"]["json_pointer"])
        for prop in sample["properties"]:
            if not prop["value"]["unit"]:
                gaps["property_unit_missing"].append(prop["origin"]["json_pointer"])
            if all(prop["value"][k] is None for k in ("single_value", "min", "max")):
                gaps["property_numeric_value_unparsed"].append(prop["origin"]["json_pointer"])
    return differences, gaps if any(gaps[k] for k in ("metadata_fields", "sample_name_missing", "property_unit_missing", "property_numeric_value_unparsed")) else None


def select_snapshots(papers: list[dict], extracts: list[dict], assets_by_doi: dict[str, list[dict]] | None = None) -> tuple[list[dict], dict]:
    """Select the newest eligible record per DOI; rejected latest records fall back."""
    assets_by_doi = assets_by_doi or {}
    paper_by_id = {str(p["id"]): p for p in papers}
    groups: dict[str, list[dict]] = {}
    invalid_papers = []
    for paper in papers:
        doi = clean_doi(paper.get("doi"))
        # Keep malformed source identity groups in the denominator/report.
        # A missing DOI cannot merge unrelated papers, so use a paper-local key.
        key = doi or "invalid-source-paper:" + str(paper["id"])
        groups.setdefault(key, []).append(paper)
        if not normalize_doi(doi) or _MULTIPLE_IDENTIFIERS.search(doi or ""):
            invalid_papers.append(str(paper["id"]))
    by_doi: dict[str, list[dict]] = {}
    unlinked = 0
    for extract in extracts:
        paper = paper_by_id.get(str(extract.get("paper_id")))
        if paper is None:
            unlinked += 1
            continue
        doi = clean_doi(paper.get("doi")) or "invalid-source-paper:" + str(paper["id"])
        by_doi.setdefault(doi, []).append(extract)
    results, failures, rejected = [], [], Counter()
    rejected_extracts, metadata_differences, missing_fields, not_selected = [], [], [], []
    unlinked_ids = [str(e["id"]) for e in extracts if str(e.get("paper_id")) not in paper_by_id]
    fallback_count = 0
    for doi in sorted(groups):
        versions = sorted(by_doi.get(doi, []), key=_sort_extract, reverse=True)
        chosen, skipped = None, []
        chosen_index = None
        for version_index, extract in enumerate(versions):
            try:
                chosen = normalize_extraction(paper_by_id[str(extract["paper_id"])], extract, assets=assets_by_doi.get(doi, []))
                chosen_index = version_index
                break
            except NormalizationError as exc:
                rejected[exc.code] += 1
                item = {"extract_id": str(extract["id"]), "paper_id": str(extract["paper_id"]), "doi": doi, "reason": exc.code}
                skipped.append(item)
                rejected_extracts.append(item)
        if chosen is None:
            failures.append({"doi": doi, "reason": skipped[0]["reason"] if skipped else "no_linked_extract", "source_paper_ids": [str(p["id"]) for p in groups[doi]], "extract_ids": [v["extract_id"] for v in skipped]})
            continue
        not_selected.extend({"extract_id": str(e["id"]), "paper_id": str(e["paper_id"]), "doi": doi, "reason": "older_than_selected_snapshot"} for e in versions[chosen_index + 1:])
        chosen["source_paper_ids"] = sorted((str(p["id"]) for p in groups[doi]), key=int)
        chosen["skipped_versions"] = skipped
        if skipped:
            fallback_count += 1
        differences, gaps = _metadata_audit(chosen, paper_by_id[chosen["source_paper_id"]])
        metadata_differences.extend(differences)
        for alias in groups[doi]:
            if str(alias["id"]) != chosen["source_paper_id"]:
                alias_differences, _ = _metadata_audit(chosen, alias)
                metadata_differences.extend(alias_differences)
        if gaps:
            missing_fields.append(gaps)
        results.append(chosen)
    report = {"candidate_paper_rows": len(papers), "candidate_doi_groups": len(groups), "duplicate_doi_groups": sum(len(v) > 1 for v in groups.values()), "invalid_paper_doi_count": len(invalid_papers), "extract_rows": len(extracts), "unlinked_extract_rows": unlinked, "ready_documents": len(results), "quarantined_documents": len(failures), "fallback_documents": fallback_count, "rejected_extract_reasons": dict(rejected), "quarantine_examples": failures[:30], "invalid_paper_id_examples": invalid_papers[:30], "selection_examples": [{"doi": r["metadata"]["doi"], "paper_id": r["source_paper_id"], "extract_id": r["source_extract_id"], "snapshot_uid": r["snapshot_uid"], "skipped_versions": r["skipped_versions"]} for r in results[:30]], "normalizer_version": NORMALIZER_VERSION}
    assert report["ready_documents"] + report["quarantined_documents"] == report["candidate_doi_groups"]
    report.update(quarantined=failures, rejected_extracts=rejected_extracts, not_selected_extracts=not_selected, rejected_extract_scope="Candidates evaluated newest first until the first eligible snapshot; not_selected_extracts lists older versions retained at source without claiming their eligibility.", metadata_differences=metadata_differences, missing_fields=missing_fields, invalid_paper_ids=invalid_papers, unmatched_candidate_extract_ids=unlinked_ids)
    report["candidate_grouping_rule"] = "Minimal trim/lower/DOI-prefix cleaning. Invalid nonempty DOI strings remain candidate groups and are quarantined; absent source DOIs use separate paper-local candidate groups."
    return results, report
