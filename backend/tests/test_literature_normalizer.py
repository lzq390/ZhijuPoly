"""Pure synthetic normalizer regressions: no database, network, or assets."""
import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.services import literature_normalizer as normalizer


DOI = "10.1234/synthetic"


def paper(identifier=1, doi=DOI):
    return {"id": identifier, "doi": doi, "title": "Synthetic regression"}


def extraction(flow=None, identifier=1, paper_id=1, created="2026-01-01T00:00:00Z", raw=None):
    if raw is None:
        raw = {"paperInfo": {}, "polymers": [{"processFlows": [flow or {"mass": 1}]}]}
    return {"id": identifier, "paper_id": paper_id, "created_at": created,
            "status": "success", "model_version": "synthetic", "raw_json": raw}


class LiteratureNormalizerTests(unittest.TestCase):
    def normalize(self, flow):
        return normalizer.normalize_extraction(paper(), extraction(flow))

    def test_yield_only_zero_keeps_process(self):
        result = self.normalize({"productName": "Synthetic product", "yieldPercent": 0})
        self.assertEqual(result["counts"]["samples"], 1)
        self.assertEqual(result["counts"]["processes"], 1)
        value = result["normalized_json"]["samples"][0]["processes"][0]["yield_percent"]
        self.assertEqual(value, 0)
        self.assertIs(type(value), int)

    def test_yield_alias_and_original_value_are_retained(self):
        for key in ("yieldPercent", "产物收率百分比"):
            for value in (0, 82.5, "82.5", "82.5%"):
                with self.subTest(key=key, value=value):
                    result = self.normalize({key: value})
                    self.assertEqual(result["normalized_json"]["samples"][0]["processes"][0]["yield_percent"], value)

    def test_placeholder_or_boolean_yield_does_not_create_process(self):
        for value in (None, "", "unknown", "not available", False, True):
            with self.subTest(value=value):
                with self.assertRaises(normalizer.NormalizationError) as caught:
                    self.normalize({"productName": "Name without observations", "yieldPercent": value})
                self.assertEqual(caught.exception.code, "empty_extracted_content")

    def test_material_quote_counts_as_evidence(self):
        result = self.normalize({"materials": [{"name": "Synthetic input", "locationIndex": {"originalText": "Synthetic quote"}}]})
        material = result["normalized_json"]["samples"][0]["processes"][0]["materials"][0]
        self.assertEqual(material["evidence"][0]["quote"], "Synthetic quote")
        self.assertTrue(result["capabilities"]["has_evidence"])

    def test_locator_without_quote_is_not_evidence(self):
        for locator in (None, {"originalText": ""}, {"originalText": "unknown"},
                        {"block_id": "synthetic-block"}, {"md_range": [1, 2]}):
            with self.subTest(locator=locator):
                result = self.normalize({"materials": [{"name": "Synthetic input", "locationIndex": locator}]})
                self.assertFalse(result["capabilities"]["has_evidence"])

    def test_existing_evidence_locations_still_count(self):
        for location in ("sample", "property", "process"):
            with self.subTest(location=location):
                sample = {"processFlows": [{"mass": 1}]}
                target = sample
                if location == "property":
                    sample["properties"] = [{"name": "Synthetic property", "value": 1}]
                    target = sample["properties"][0]
                elif location == "process":
                    target = sample["processFlows"][0]
                target["locationIndex"] = {"originalText": "Synthetic quote"}
                raw = {"paperInfo": {}, "polymers": [sample]}
                result = normalizer.normalize_extraction(paper(), extraction(raw=raw))
                self.assertTrue(result["capabilities"]["has_evidence"])

    def test_newest_yield_only_extract_does_not_fall_back(self):
        rows = [extraction({"mass": 1}, identifier=1),
                extraction({"yieldPercent": 0}, identifier=2, created="2026-01-02T00:00:00Z")]
        results, report = normalizer.select_snapshots([paper()], rows)
        self.assertEqual(results[0]["source_extract_id"], "2")
        self.assertEqual(report["fallback_documents"], 0)
        self.assertEqual(report["rejected_extracts"], [])

    def test_fallback_retains_rejection_and_denominator(self):
        older = extraction({"mass": 1}, identifier=1)
        newer = extraction(identifier=2, created="2026-01-02T00:00:00Z", raw={})
        rows, report = normalizer.select_snapshots([paper()], [older, newer])
        self.assertEqual(rows[0]["source_extract_id"], "1")
        self.assertEqual(report["fallback_documents"], 1)
        self.assertEqual(report["rejected_extract_reasons"], {"unsupported_schema": 1})
        self.assertEqual(report["ready_documents"] + report["quarantined_documents"], report["candidate_doi_groups"])

    def test_equal_timestamps_use_bigint_order(self):
        rows, report = normalizer.select_snapshots([paper()], [extraction(identifier=9), extraction(identifier=10)])
        self.assertEqual(rows[0]["source_extract_id"], "10")
        self.assertEqual(report["ready_documents"], 1)

    def test_none_and_naive_timestamps_have_stable_order(self):
        values = [extraction(identifier=1, created=None),
                  extraction(identifier=2, created=datetime(2026, 1, 1)),
                  extraction(identifier=3, created=datetime(2026, 1, 1, tzinfo=timezone.utc))]
        rows, _ = normalizer.select_snapshots([paper()], values)
        self.assertEqual(rows[0]["source_extract_id"], "3")

    def test_conflicting_doi_is_rejected(self):
        value = extraction()
        value["raw_json"]["paperInfo"]["doi"] = "10.1234/other-synthetic"
        with self.assertRaises(normalizer.NormalizationError) as caught:
            normalizer.normalize_extraction(paper(), value)
        self.assertEqual(caught.exception.code, "doi_identity_conflict")

    def test_unsupported_schema_is_rejected(self):
        with self.assertRaises(normalizer.NormalizationError) as caught:
            normalizer.normalize_extraction(paper(), extraction(raw={"unrecognized": []}))
        self.assertEqual(caught.exception.code, "unsupported_schema")

    def test_nonfinite_values_are_rejected_without_coercion(self):
        for number in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(number=number):
                with self.assertRaises(normalizer.NormalizationError) as caught:
                    self.normalize({"yieldPercent": number})
                self.assertEqual(caught.exception.code, "invalid_json_value")

    def test_raw_json_limit_is_enforced(self):
        value = extraction()
        value["raw_json"]["synthetic_padding"] = "x" * normalizer.MAX_JSON_BYTES
        with self.assertRaises(normalizer.NormalizationError) as caught:
            normalizer.normalize_extraction(paper(), value)
        self.assertEqual(caught.exception.code, "raw_json_too_large")

    def test_identity_is_deterministic_without_mutating_source(self):
        value = extraction({"materials": [{"name": "Synthetic input", "locationIndex": {"originalText": "Synthetic quote"}}]})
        saved = copy.deepcopy(value)
        a = normalizer.normalize_extraction(paper(), value)
        b = normalizer.normalize_extraction(paper(), value)
        self.assertEqual(a, b)
        self.assertEqual(value, saved)
        self.assertEqual(a["normalizer_version"], "literature-v2")

    def test_version_changes_immutable_snapshot_identity(self):
        value = extraction({"mass": 1})
        with patch.object(normalizer, "NORMALIZER_VERSION", "literature-v1"):
            old_identity = normalizer.normalize_extraction(paper(), value)
        new_identity = normalizer.normalize_extraction(paper(), value)
        self.assertNotEqual(old_identity["snapshot_uid"], new_identity["snapshot_uid"])
        self.assertEqual(old_identity["document_uid"], new_identity["document_uid"])


if __name__ == "__main__":
    unittest.main()
