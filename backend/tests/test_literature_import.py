from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import io
import json

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from literature_test_support import install_literature_test_projection

from app import import_literature
from app.services import literature_normalizer as normalizer
from app.services import literature_publication
from app.services.literature_source import SourceDataset, build_asset_manifests, read_source, verify_extraction_inputs


@pytest.fixture(scope="module")
def literature_import_dsn(postgres_test_dsn):
    install_literature_test_projection(postgres_test_dsn)
    return postgres_test_dsn


def paper(paper_id="1", doi="10.1234/example", title="Source title"):
    return {"id": paper_id, "doi": doi, "title": title, "journal": "Journal", "year": 2020, "authors_json": '["Author, A."]', "keyword": "polymer; film", "abstract": None}


def raw_english(*, doi="10.1234/example"):
    return {"paperInfo": {"document_id": doi, "title": "Extracted title", "authors": ["A"]}, "polymers": [{"name": "PI-1", "identityCode": "PI-1", "properties": [{"name": "Tg", "value": {"singleValue": 264, "unit": "°C"}, "testCondition": {"instrument": "DSC"}, "locationIndex": {"originalText": "Tg was 264 °C", "blockId": "14"}}], "processFlows": [{"productName": "PI-1 film", "materials": [{"name": "ODA", "molarAmountMeasurement": {"singleValue": 2.19, "unit": "mmol"}}], "reactionConditions": [{"reactionTime": {"singleValue": 12, "unit": "h"}}]}]}]}


def extract(extract_id="100", paper_id="1", *, raw=None, created_at="2026-01-01T00:00:00+00:00"):
    return {"id": extract_id, "paper_id": paper_id, "status": "success", "created_at": created_at, "model_version": "model", "raw_json": raw if raw is not None else raw_english()}


@pytest.mark.parametrize("value", ["10.1234/ABC", "doi: 10.1234/ABC", "https://doi.org/10.1234/ABC", "HTTP://dx.doi.org/10.1234/ABC"])
def test_normalizes_doi_prefixes_and_keeps_sici_punctuation(value):
    assert normalizer.normalize_doi(value) == "10.1234/abc"
    assert normalizer.normalize_doi("10.1002/(SICI)123<456>;2-X") == "10.1002/(sici)123<456>;2-x"
    assert normalizer.normalize_doi("10.1234/a10.5678/reference") == "10.1234/a10.5678/reference"
    assert normalizer.normalize_doi("10.1234/a; 10.1234/b") is None


def test_snapshot_preserves_samples_values_evidence_and_does_not_infer_units():
    raw = raw_english()
    raw["polymers"].append({"name": "PI-2", "properties": [{"name": "Density", "value": {"minValue": 9, "maxValue": 18}, "newScientificField": {"x": 1}}]})
    result = normalizer.normalize_extraction(paper(), extract(raw=raw))
    samples = result["normalized_json"]["samples"]
    assert [s["name"] for s in samples] == ["PI-1", "PI-2"]
    assert samples[0]["properties"][0]["value"]["single_value"] == 264
    bounds = samples[1]["properties"][0]
    assert bounds["value"]["min"] == 9 and bounds["value"]["max"] == 18
    assert bounds["value"]["single_value"] is None and bounds["value"]["unit"] is None
    assert bounds["raw"]["newScientificField"] == {"x": 1}
    assert "property_unit_missing" in result["quality_flags"]
    assert samples[0]["properties"][0]["evidence"][0] == {"quote": "Tg was 264 °C", "block_id": "14", "md_range": None}
    assert samples[0]["properties"][0]["origin"] == {"source_extract_id": "100", "json_pointer": "/polymers/0/properties/0"}
    assert result["counts"] == {"samples": 2, "properties": 2, "processes": 1, "materials": 1, "characterizations": 0}
    assert result["capabilities"]["has_evidence"] is True
    assert result["capabilities"]["has_original"] is False


def test_chinese_adapter_preserves_multiple_evidence_and_amount_kinds():
    raw = {"文献信息": {"唯一文献标识": "10.1234/example", "论文标题": "中文题名", "作者列表": ["张三"]}, "聚合物": [{"名称": "样品A", "身份标识": "A", "性质": [{"名称": "拉伸强度", "值": {"单值": 0, "单位": "MPa", "误差": "未提供"}, "测试条件": {"方法": "ASTM"}, "位置索引": [{"原文": "first", "block_id": "1"}, {"原文": "second", "block_id": "2"}]}], "工艺流程": [{"产物名称": "膜", "原料": [{"原料名称": "ODA", "单体类型": "二胺", "摩尔量": {"单值": 24, "单位": "mmol"}, "当量比": {"单值": 1, "单位": "eq"}}], "后处理步骤": ["干燥"]}], "表征": {"形貌": "多孔"}}]}
    result = normalizer.normalize_extraction(paper(), extract(raw=raw))
    sample = result["normalized_json"]["samples"][0]
    assert result["metadata"]["title"] == "中文题名"
    assert sample["properties"][0]["value"]["single_value"] == 0
    assert len(sample["properties"][0]["evidence"]) == 2
    amounts = sample["processes"][0]["materials"][0]["amounts"]
    assert {v["kind"] for v in amounts} == {"molar_amount", "equivalent_ratio"}
    assert sample["characterization"] == {"形貌": "多孔"}


@pytest.mark.parametrize("language", ["english", "chinese"])
@pytest.mark.parametrize("yield_percent", ["91%", 87, 0])
def test_yield_only_process_is_scientific_content(language, yield_percent):
    if language == "english":
        raw = {"paperInfo": {"document_id": "10.1234/example"}, "polymers": [{"name": "PI1b", "processFlows": [{"productName": "PI1b", "yieldPercent": yield_percent}]}]}
        expected_pointer = "/polymers/0/processFlows/0"
    else:
        raw = {"文献信息": {"唯一文献标识": "10.1234/example"}, "聚合物": [{"名称": "PI1b", "工艺流程": [{"产物名称": "PI1b", "产物收率百分比": yield_percent}]}]}
        expected_pointer = "/聚合物/0/工艺流程/0"
    result = normalizer.normalize_extraction(paper(), extract(raw=raw))
    process = result["normalized_json"]["samples"][0]["processes"][0]
    assert process["yield_percent"] == yield_percent
    assert process["product_name"] == "PI1b"
    assert process["materials"] == process["evidence"] == []
    assert process["origin"]["json_pointer"] == expected_pointer
    assert result["counts"]["processes"] == 1
    assert result["capabilities"]["has_processes"] is True


@pytest.mark.parametrize("yield_percent", [None, "", "unknown"])
def test_placeholder_yield_does_not_admit_empty_process(yield_percent):
    raw = {"paperInfo": {}, "polymers": [{"name": "PI1b", "processFlows": [{"productName": "PI1b", "yieldPercent": yield_percent}]}]}
    with pytest.raises(normalizer.NormalizationError, match="empty_extracted_content"):
        normalizer.normalize_extraction(paper(), extract(raw=raw))


@pytest.mark.parametrize("raw,code", [
    ([], "unsupported_schema"),
    ({"paperInfo": {}, "polymers": []}, "empty_extracted_content"),
    ({"paperInfo": {}, "polymers": [{"name": "PI-1", "identityCode": "PI-1"}]}, "empty_extracted_content"),
    ({"paperInfo": {}, "polymers": [{"name": "unknown", "properties": [{"name": "Tg", "value": {"unit": "C"}}]}]}, "empty_extracted_content"),
    ({"paperInfo": {"document_id": "10.1234/other"}, "polymers": [{"name": "PI"}]}, "doi_identity_conflict"),
    ({"paperInfo": {"document_id": "10.1234/a; 10.1234/b"}, "polymers": [{"name": "PI"}]}, "invalid_doi_identifier"),
    ({"paperInfo": {"document_id": ["10.1234/example"]}, "polymers": [{"name": "PI"}]}, "invalid_doi_identifier"),
    ({"paperInfo": {"document_id": "opaque-external-work-123"}, "polymers": [{"name": "PI"}]}, "invalid_doi_identifier"),
    ({"paperInfo": {"document_id": "unknown"}, "polymers": [{"name": "PI"}]}, "invalid_doi_identifier"),
    ({"paperInfo": {"document_id": "10.1234/a;10.1234/b"}, "polymers": [{"name": "PI"}]}, "invalid_doi_identifier"),
    ({"paperInfo": {"document_id": "", "doi": "10.1234/wrong"}, "polymers": [{"name": "PI"}]}, "doi_identity_conflict"),
])
def test_semantic_and_identity_gates(raw, code):
    with pytest.raises(normalizer.NormalizationError) as error:
        normalizer.normalize_extraction(paper(), extract(raw=raw))
    assert error.value.code == code


def test_missing_identifier_is_flagged_but_paper_fk_is_kept():
    raw = raw_english()
    del raw["paperInfo"]["document_id"]
    result = normalizer.normalize_extraction(paper(), extract(raw=raw))
    assert result["source_paper_id"] == "1"
    assert "doi_identifier_missing" in result["quality_flags"]


def test_identified_sample_needs_form_category_or_experiments_and_anonymous_experiment_is_kept():
    sample = {"name": "PI-1", "sampleForm": "film"}
    raw = {"paperInfo": {}, "polymers": [sample]}
    assert normalizer.normalize_extraction(paper(), extract(raw=raw))["counts"]["samples"] == 1
    raw["polymers"] = [{"properties": [{"name": "Tg", "value": {"singleValue": 264}}]}]
    result = normalizer.normalize_extraction(paper(), extract(raw=raw))
    assert result["normalized_json"]["samples"][0]["name"] is None
    assert result["counts"]["properties"] == 1


def test_latest_eligible_snapshot_grouping_bigint_order_and_fallback():
    bad = extract("90071992547409930", "2", raw=raw_english(doi="10.1234/wrong"), created_at="2026-02-01T00:00:00Z")
    lower = extract("90071992547409920", "1")
    higher = extract("90071992547409921", "2")
    results, report = normalizer.select_snapshots([paper(), paper("2", title="CO₂ notation")], [bad, lower, higher])
    assert len(results) == 1
    chosen = results[0]
    assert chosen["source_extract_id"] == "90071992547409921"
    assert chosen["source_paper_ids"] == ["1", "2"]
    assert len(chosen["normalized_json"]["samples"]) == 1
    assert report["candidate_paper_rows"] == 2 and report["candidate_doi_groups"] == 1
    assert report["fallback_documents"] == 1
    assert report["rejected_extract_reasons"] == {"doi_identity_conflict": 1}


def test_invalid_source_doi_is_retained_in_candidate_denominator_and_quarantined():
    value = "10.1126/sciadv.adr5262，10.1038/s41467-024-55293-9，10.1038/s41586-"
    rows = [paper("1", value), paper("2", "invalid-doi")]
    values = [extract("100", "1"), extract("101", "2")]
    selected, report = normalizer.select_snapshots(rows, values)
    assert selected == []
    assert report["candidate_doi_groups"] == 2
    assert report["quarantined_documents"] == 2
    assert report["invalid_paper_ids"] == ["1", "2"]
    assert all(v["reason"] == "invalid_paper_doi" for v in report["quarantined"])


def test_snapshot_ids_are_stable_and_change_for_metadata_raw_and_assets():
    original = normalizer.normalize_extraction(paper(), extract())
    assert normalizer.normalize_extraction(paper(), extract())["snapshot_uid"] == original["snapshot_uid"]
    raw = raw_english()
    raw["new_key"] = "preserved"
    assert normalizer.normalize_extraction(paper(), extract(raw=raw))["snapshot_uid"] != original["snapshot_uid"]
    changed = paper()
    changed["abstract"] = "changed source abstract"
    assert normalizer.normalize_extraction(changed, extract())["snapshot_uid"] != original["snapshot_uid"]
    assert normalizer.normalize_extraction(paper(), extract(), assets=[{"role": "pdf", "status": "available", "etag": "opaque"}])["snapshot_uid"] != original["snapshot_uid"]
    assert original["content_sha256"] == normalizer.json_sha256(original["raw_json"])


def test_both_raw_and_expanded_normalized_json_have_byte_limits(monkeypatch):
    raw = raw_english()
    monkeypatch.setattr(normalizer, "MAX_JSON_BYTES", 30)
    with pytest.raises(normalizer.NormalizationError, match="raw_json_too_large"):
        normalizer.normalize_extraction(paper(), extract(raw=raw))
    monkeypatch.setattr(normalizer, "MAX_JSON_BYTES", len(normalizer.canonical_json(raw).encode()) + 30)
    with pytest.raises(normalizer.NormalizationError, match="normalized_json_too_large"):
        normalizer.normalize_extraction(paper(), extract(raw=raw))


def test_asset_manifest_deduplicates_keys_marks_ambiguity_and_never_claims_input_match():
    rows = [{"src_source_ref": "10.1234/example", "result_bucket": "pdee", "result_blocks_clean_object_key": "v1/blocks.json", "result_blocks_clean_size": 100, "result_blocks_clean_count": 2, "preprocess_id": "1", "src_bucket": "polymer-literature", "src_storage_path": "s3://polymer-literature/x.pdf", "src_file_size": 20, "convert_id": "1"}]
    files = [{"source_ref": "10.1234/example", "is_si": "false", "file_ext": "pdf", "storage_bucket": "polymer-literature", "storage_path": "x.pdf", "file_size": 20, "checksum": "checksum", "version": 1}, {"source_ref": "doi:10.1234/example", "is_si": "false", "file_ext": "pdf", "storage_bucket": "polymer-literature", "storage_path": "other.pdf", "file_size": 30, "version": 2}]
    assets = build_asset_manifests(rows, files)["10.1234/example"]
    assert len(assets) == 3
    assert [a["status"] for a in assets if a["role"] == "pdf"] == ["ambiguous", "ambiguous"]
    assert all(a["association"] == "paper" and a["exact_input_verified"] is False for a in assets)
    assert next(a for a in assets if a["object_key"] == "x.pdf")["checksum"] == "checksum"


def test_only_verified_assets_enable_original_capability():
    assets = [{"role": "full_text", "status": "unverified", "etag": "opaque-ft"}, {"role": "pdf", "status": "ambiguous", "etag": "opaque-pdf"}, {"role": "attachment", "status": "available", "etag": "opaque-att"}]
    result = normalizer.normalize_extraction(paper(), extract(), assets=assets)
    assert not result["capabilities"]["has_original"]
    assert result["capabilities"]["attachment_count"] == 1
    assets[0]["status"] = "available"
    result = normalizer.normalize_extraction(paper(), extract(), assets=assets)
    assert result["capabilities"]["has_original"] and result["capabilities"]["has_fulltext"]
    del assets[0]["etag"]
    assert not normalizer.normalize_extraction(paper(), extract(), assets=assets)["capabilities"]["has_original"]


def test_reports_keep_complete_rejections_differences_missing_fields_and_aliases():
    papers = [paper(str(i), f"10.1234/rejected-{i}") for i in range(35)]
    extracts = [extract(str(100+i), str(i), raw=[]) for i in range(35)]
    papers.append(paper("101", "10.1234/good"))
    extracts.append(extract("1001", "101", raw=raw_english(doi="10.1234/good")))
    selected, report = normalizer.select_snapshots(papers, extracts)
    assert len(report["quarantined"]) == len(report["rejected_extracts"]) == 35
    assert len(report["quarantine_examples"]) == 30
    assert any(item["field"] == "title" for item in report["metadata_differences"])
    assert "abstract" in report["missing_fields"][0]["metadata_fields"]
    assert len(selected) == 1


def test_exact_input_verification_requires_unique_equal_hash_and_precise_key_chain(monkeypatch):
    from app.services import literature_assets
    snapshot = normalizer.normalize_extraction(paper(), extract())
    candidate = {"src_source_ref": "10.1234/example", "pdee_extract_id": "12", "result_kind": "single", "result_bucket": "pdee", "result_final_object_key": "results/final.json", "result_final_size": 1, "input_bucket": "pdee", "input_blocks_clean_object_key": "old/blocks.json", "preprocess_id": "13", "convert_id": "14", "preprocess_source_ref": "10.1234/example", "convert_source_ref": "10.1234/example", "convert_md_object_key": "old/file.md", "result_blocks_clean_count": 5, "result_blocks_clean_size": 50, "src_bucket": "polymer-literature", "src_storage_path": "s3://polymer-literature/input.pdf", "src_file_size": 100}
    body = json.dumps(snapshot["raw_json"]).encode()
    closed = []
    class Response(io.BytesIO):
        def close(self):
            closed.append("closed")
            super().close()
        def release_conn(self):
            closed.append("released")
    def probe(asset):
        size = len(body) if asset["object_key"].endswith("final.json") else 50 if asset["role"] == "full_text" else 100
        return {**asset, "status": "available", "etag": "frozen", "size_bytes": size}
    monkeypatch.setattr(literature_assets, "probe_asset", probe)
    monkeypatch.setattr(literature_assets, "_get", lambda *a, **kw: Response(body))
    existing = {"10.1234/example": [{"asset_uid": "other-text", "role": "full_text", "status": "available", "etag": "other", "bucket": "pdee", "object_key": "new/blocks.json", "exact_input_verified": False}]}
    manifests, report = verify_extraction_inputs([snapshot], [candidate], existing, workers=1)
    assert report[0]["exact_input_verified"] is True
    text = next(a for a in manifests["10.1234/example"] if a.get("exact_input_verified"))
    assert text["object_key"] == "old/blocks.json" and text["association"] == "extraction_input"
    assert text["input_proof"]["result_content_sha256"] == snapshot["content_sha256"]
    assert next(a for a in manifests["10.1234/example"] if a["asset_uid"] == "other-text")["status"] == "ambiguous"
    assert closed == ["closed", "released"]
    duplicate = {**candidate, "result_final_object_key": "another/final.json"}
    _, report = verify_extraction_inputs([snapshot], [candidate, duplicate], existing, workers=1)
    assert report[0]["reason"] == "AMBIGUOUS_MATCHING_RESULTS"
    _, report = verify_extraction_inputs([snapshot], [{**candidate, "convert_id": None}], existing, workers=1)
    assert report[0]["reason"] == "INCOMPLETE_OR_AMBIGUOUS_INPUT_CHAIN"


def test_exact_input_verification_failure_remains_paper_asset_and_size_is_bounded(monkeypatch):
    from app.services import literature_assets
    snapshot = normalizer.normalize_extraction(paper(), extract())
    candidate = {"src_source_ref": "10.1234/example", "pdee_extract_id": "12", "result_bucket": "pdee", "result_final_object_key": "large.json"}
    monkeypatch.setattr(literature_assets, "probe_asset", lambda asset: {**asset, "status": "available", "etag": "frozen", "size_bytes": normalizer.MAX_JSON_BYTES + 1})
    def forbidden(*args, **kwargs):
        pytest.fail("oversized result must not be downloaded")
    monkeypatch.setattr(literature_assets, "_get", forbidden)
    result, report = verify_extraction_inputs([snapshot], [candidate], {"10.1234/example": []}, workers=1)
    assert result == {"10.1234/example": []}
    assert report[0]["exact_input_verified"] is False
    assert report[0]["verification_errors"][0]["reason"] == "RESULT_JSON_TOO_LARGE"


def test_exact_input_verification_cli_requires_asset_verification(monkeypatch, capsys):
    monkeypatch.setenv("LITERATURE_SOURCE_DSN", "readonly-source")
    assert import_literature.main(["dry-run", "--verify-extraction-input"]) == 1
    assert "VERIFY_EXTRACTION_INPUT_REQUIRES_VERIFY_ASSETS" in capsys.readouterr().err


def test_cli_uses_only_explicit_environment_and_redacts_network_errors(monkeypatch, capsys):
    monkeypatch.delenv("LITERATURE_SOURCE_DSN", raising=False)
    assert import_literature.main(["dry-run"]) == 1
    assert "LITERATURE_SOURCE_DSN_REQUIRED" in capsys.readouterr().err
    monkeypatch.setenv("LITERATURE_SOURCE_DSN", "postgresql://user:very-secret@host/db")
    def fail(*args, **kwargs):
        raise RuntimeError("postgresql://user:very-secret@host/db")
    monkeypatch.setattr(import_literature, "build_import", fail)
    assert import_literature.main(["dry-run"]) == 1
    output = capsys.readouterr()
    assert "very-secret" not in output.err + output.out
    assert "RuntimeError" in output.err


def test_generation_prepare_publish_rollback_and_snapshot_immutability(literature_import_dsn, monkeypatch):
    postgres_test_dsn = literature_import_dsn
    data = SourceDataset([paper()], [extract(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))], {}, {"partial": False, "source_read_only": True})
    monkeypatch.setattr(import_literature, "read_source", lambda *a, **kw: data)
    first = import_literature.prepare_generation("readonly-source", postgres_test_dsn)
    second = import_literature.prepare_generation("readonly-source", postgres_test_dsn)
    assert first["status"] == second["status"] == "ready"
    assert second["report"]["reused_snapshots"] == 1
    import_literature.activate_generation(postgres_test_dsn, first["generation_id"])
    import_literature.activate_generation(postgres_test_dsn, second["generation_id"])
    import_literature.activate_generation(postgres_test_dsn, first["generation_id"], rollback=True)
    with psycopg.connect(postgres_test_dsn, row_factory=dict_row) as connection:
        active = connection.execute("SELECT generation_id FROM knowledge.literature_generations WHERE is_active").fetchall()
        assert [str(row["generation_id"]) for row in active] == [first["generation_id"]]
        snapshot = connection.execute("SELECT s.* FROM knowledge.literature_snapshots s JOIN knowledge.literature_catalog c USING(snapshot_uid,document_uid) WHERE c.generation_id=%s", (first["generation_id"],)).fetchone()
        assert snapshot["source_extract_id"] == "100"
        assert snapshot["normalized_json"]["samples"][0]["properties"][0]["value"]["single_value"] == 264
        with pytest.raises(psycopg.Error, match="immutable"):
            connection.execute("UPDATE knowledge.literature_snapshots SET model_version='tampered' WHERE snapshot_uid=%s", (snapshot["snapshot_uid"],))
        connection.rollback()
    data.summary["partial"] = True
    partial = import_literature.prepare_generation("readonly-source", postgres_test_dsn, limit=1)
    with pytest.raises(import_literature.ImportStateError, match="PARTIAL_GENERATION"):
        import_literature.activate_generation(postgres_test_dsn, partial["generation_id"])
    import_literature.activate_generation(postgres_test_dsn, partial["generation_id"], allow_partial=True)


def test_real_cli_publishes_and_rolls_back_current_v2(literature_import_dsn, monkeypatch, capsys):
    data = SourceDataset([paper("701", "10.1234/cli-current")],
                         [extract("702", "701", raw=raw_english(doi="10.1234/cli-current"))], {}, {"partial": False})
    monkeypatch.setattr(import_literature, "read_source", lambda *args, **kwargs: data)
    monkeypatch.setenv("LITERATURE_IMPORT_DSN", literature_import_dsn)
    generations = [import_literature.prepare_generation("synthetic-readonly-source", literature_import_dsn) for _ in range(2)]
    assert import_literature.ImportStateError is literature_publication.ImportStateError
    for generation in generations:
        assert import_literature.main(["publish", generation["generation_id"]]) == 0
        published = json.loads(capsys.readouterr().out)
        assert published == {"generation_id": generation["generation_id"], "status": "published", "is_active": True,
                             "documents": 1, "action": "publish"}
    assert import_literature.main(["rollback", generations[0]["generation_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "rollback"


@pytest.mark.parametrize("version", ["unsupported-format", "literature-v1", "literature-v1.1"])
def test_real_cli_rejects_noncurrent_generation_without_replacing_active(literature_import_dsn, monkeypatch, capsys, version):
    data = SourceDataset([paper("711", "10.1234/cli-format")],
                         [extract("712", "711", raw=raw_english(doi="10.1234/cli-format"))], {}, {"partial": False})
    monkeypatch.setattr(import_literature, "read_source", lambda *args, **kwargs: data)
    monkeypatch.setenv("LITERATURE_IMPORT_DSN", literature_import_dsn)
    active = import_literature.prepare_generation("synthetic-readonly-source", literature_import_dsn)
    assert import_literature.main(["publish", active["generation_id"]]) == 0
    capsys.readouterr()
    rejected = import_literature.prepare_generation("synthetic-readonly-source", literature_import_dsn)
    with psycopg.connect(literature_import_dsn) as connection:
        connection.execute("UPDATE knowledge.literature_generations SET normalizer_version=%s WHERE generation_id=%s", (version, rejected["generation_id"]))
    assert import_literature.main(["publish", rejected["generation_id"]]) == 1
    assert json.loads(capsys.readouterr().err)["error_code"] == "GENERATION_FORMAT_UNSUPPORTED"
    with psycopg.connect(literature_import_dsn) as connection:
        assert str(connection.execute("SELECT generation_id FROM knowledge.literature_generations WHERE is_active").fetchone()[0]) == active["generation_id"]


@pytest.mark.parametrize("legacy_version", ["literature-v1", "literature-v1.1"])
def test_legacy_and_current_snapshots_coexist_and_remain_readable(literature_import_dsn, monkeypatch, legacy_version):
    from app.services.literature_preflight import check_literature_readiness
    from app.services.literature_repository import literature_detail, published_generation

    current_version = normalizer.NORMALIZER_VERSION
    assert current_version == "literature-v2"
    assert {"literature-v1", "literature-v1.1", current_version} == normalizer.SUPPORTED_NORMALIZER_VERSIONS
    data = SourceDataset([paper("881", "10.1234/version-compat-" + legacy_version)], [extract("882", "881", raw=raw_english(doi="10.1234/version-compat-" + legacy_version))], {}, {"partial": False})
    monkeypatch.setattr(import_literature, "read_source", lambda *a, **kw: data)
    # The fixture has no yield-only entries, so the historical adapter would
    # produce this same shape. Build its immutable identity with the old label.
    with monkeypatch.context() as old_version:
        old_version.setattr(normalizer, "NORMALIZER_VERSION", legacy_version)
        old_version.setattr(import_literature, "NORMALIZER_VERSION", legacy_version)
        old_version.setattr(literature_publication, "NORMALIZER_VERSION", legacy_version)
        old = import_literature.prepare_generation("readonly-source", literature_import_dsn)
        # This generation was published by the historical release, not newly
        # admitted through the current v2 publisher.
        import_literature.activate_generation(literature_import_dsn, old["generation_id"])
    new = import_literature.prepare_generation("readonly-source", literature_import_dsn)
    assert new["report"]["new_snapshots"] == 1
    assert new["report"]["reused_snapshots"] == 0
    with psycopg.connect(literature_import_dsn, row_factory=dict_row) as connection:
        snapshots = connection.execute("""SELECT c.generation_id,c.document_uid,c.snapshot_uid,s.normalizer_version,s.content_sha256
            FROM knowledge.literature_catalog c JOIN knowledge.literature_snapshots s USING(document_uid,snapshot_uid)
            WHERE c.generation_id=ANY(%s::uuid[])""", ([old["generation_id"], new["generation_id"]],)).fetchall()
    by_version = {s["normalizer_version"]: s for s in snapshots}
    previous, current = by_version[legacy_version], by_version[current_version]
    assert previous["document_uid"] == current["document_uid"]
    assert previous["content_sha256"] == current["content_sha256"]
    assert previous["snapshot_uid"] != current["snapshot_uid"]
    import_literature.activate_generation(literature_import_dsn, new["generation_id"])
    with psycopg.connect(literature_import_dsn, row_factory=dict_row) as connection:
        assert check_literature_readiness(connection, role_override="nexpoly_api")["ready"] is True
    # A newer active generation must not invalidate fixed historical citations.
    with psycopg.connect(literature_import_dsn, row_factory=dict_row) as connection:
        for snapshot in snapshots:
            generation = str(snapshot["generation_id"])
            assert published_generation(connection, generation) == generation
            detail = literature_detail(connection, snapshot["document_uid"], generation, snapshot["snapshot_uid"])
            assert detail["normalizer_version"] == snapshot["normalizer_version"]


def test_failed_prepare_does_not_change_active_generation(literature_import_dsn, monkeypatch):
    postgres_test_dsn = literature_import_dsn
    data = SourceDataset([paper("91", "10.1234/failure-test")], [extract("991", "91", raw=raw_english(doi="10.1234/failure-test"))], {}, {"partial": False})
    monkeypatch.setattr(import_literature, "read_source", lambda *a, **kw: data)
    ready = import_literature.prepare_generation("readonly-source", postgres_test_dsn)
    import_literature.activate_generation(postgres_test_dsn, ready["generation_id"])
    data.extracts.clear()
    with pytest.raises(import_literature.ImportStateError, match="NO_ELIGIBLE"):
        import_literature.prepare_generation("readonly-source", postgres_test_dsn)
    with psycopg.connect(postgres_test_dsn, row_factory=dict_row) as connection:
        assert str(connection.execute("SELECT generation_id FROM knowledge.literature_generations WHERE is_active").fetchone()["generation_id"]) == ready["generation_id"]
        assert connection.execute("SELECT count(*) n FROM knowledge.literature_generations WHERE status='failed'").fetchone()["n"] >= 1


def test_source_reader_really_uses_read_only_repeatable_snapshot(postgres_test_dsn):
    # All writes below target the disposable test database, never the external
    # source. Real source-reader SQL then exercises the immutable source session.
    with psycopg.connect(postgres_test_dsn) as connection:
        connection.execute("""
          CREATE TABLE public.dwd_paper(id bigint PRIMARY KEY,doi text,title text,journal text,year int,authors_json text,keyword text,abstract text);
          CREATE TABLE public.dwd_paper_extract_record(id bigint PRIMARY KEY,paper_id bigint,status text,model_version text,created_at timestamp,raw_json jsonb);
          CREATE TABLE public.ods_pdee_file_record_preprocess(id bigint,src_source_ref text,result_bucket text,result_blocks_clean_object_key text,result_blocks_clean_size bigint,result_blocks_clean_count int,convert_md_object_key text,is_latest bool,status text);
          CREATE TABLE public.ods_pdee_file_record_convert(id bigint,result_md_object_key text,status text,src_bucket text,src_storage_path text,src_file_size bigint,src_file_name text);
          CREATE TABLE public.ods_file_object(id bigint,file_name text,file_ext text,storage_bucket text,storage_path text,file_size bigint,checksum text,version int,source_ref text,is_si text,status text,is_latest int);
        """)
        connection.execute("INSERT INTO public.dwd_paper(id,doi,title) VALUES(1,'10.1234/example','test')")
        connection.execute("INSERT INTO public.dwd_paper_extract_record VALUES(100,1,'success','model','2026-01-01',%s)", (Jsonb(raw_english()),))
    data = read_source(postgres_test_dsn)
    assert data.summary["source_read_only"] is True
    assert data.summary["source_isolation"] == "repeatable read"
    assert data.summary["partial"] is False
    assert data.extracts[0]["created_at"].tzinfo is not None
    assert normalizer.select_snapshots(data.papers, data.extracts)[1]["ready_documents"] == 1
