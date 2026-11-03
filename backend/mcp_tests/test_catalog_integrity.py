"""Catalog identity regressions; database cases use only the isolated fixture."""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import hashlib

import pytest

from app.services.property_filter import PropertyFilterCatalogUnavailable, load_property_filter_context
from app.services.property_filter_catalog import (
    _catalog_from_row,
    _unambiguous_option_key,
    aggregate_property_filter_catalog,
    aggregate_property_filter_histograms,
    load_property_filter_catalog,
    property_filter_catalog_is_current,
)


def raw_option(name="Thermal property", unit="C"):
    return {
        "filter_type": "raw",
        "option_key": "raw:" + hashlib.md5(f"{name}|{unit or ''}".encode()).hexdigest(),
        "label": name, "property_name": name, "property_unit_clean": unit,
        "rows": 1, "unique_smiles": 1, "min_value": 1.0, "max_value": 1.0,
    }


def standard_option(key="tg", unit="C"):
    return {
        "filter_type": "standardized", "option_key": f"std:{key}:{unit or ''}",
        "label": key, "property_key": key, "canonical_unit": unit,
        "rows": 1, "unique_smiles": 1, "min_value": 1.0, "max_value": 1.0,
    }


def snapshot(options):
    return {
        "schema_version": 1, "generation": 7, "import_batch_id": 5,
        "source_sha256": "a" * 64, "generated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "total_records": len(options),
        "mapped_records": sum(option["filter_type"] == "standardized" for option in options),
        "raw_records": sum(option["filter_type"] == "raw" for option in options),
        "options": options,
    }


@pytest.mark.parametrize("option", [raw_option(), raw_option(unit=""), raw_option(unit=None),
                                   standard_option(), standard_option(unit=""), standard_option(unit=None)])
def test_ordinary_option_keys_remain_unchanged(option):
    assert _unambiguous_option_key(option) == option["option_key"]


@pytest.mark.parametrize("left,right", [
    (raw_option("A|B", "C"), raw_option("A", "B|C")),
    (standard_option("A:B", "C"), standard_option("A", "B:C")),
])
def test_delimiter_collision_uses_distinct_tuple_keys(left, right):
    assert left["option_key"] == right["option_key"]
    assert _unambiguous_option_key(left) != _unambiguous_option_key(right)
    assert _unambiguous_option_key(left) != left["option_key"]
    # Legacy snapshots with a unique key remain valid until a versioned import.
    stored = snapshot([left])
    assert _catalog_from_row(stored).options == stored["options"]
    with pytest.raises(RuntimeError, match="duplicate option key"):
        _catalog_from_row(snapshot([left, right]))


def test_new_long_standard_key_is_compact_but_existing_key_is_readable():
    option = standard_option("x" * 600)
    assert _unambiguous_option_key(option).startswith("std2:")
    assert len(_unambiguous_option_key(option)) == 69
    assert _catalog_from_row(snapshot([option])).options[0]["option_key"] == option["option_key"]


def test_histogram_rejects_duplicate_identity_before_running_sql():
    with pytest.raises(RuntimeError, match="duplicate option key"):
        aggregate_property_filter_histograms(object(), [raw_option("A|B", "C"), raw_option("A", "B|C")])


def test_snapshot_rejects_more_structures_than_measurements():
    option = raw_option()
    option["unique_smiles"] = 2
    with pytest.raises(RuntimeError, match="more structures than measurements"):
        _catalog_from_row(snapshot([option]))


@pytest.mark.parametrize("factory,field", [(raw_option, "property_name"), (standard_option, "property_key")])
def test_snapshot_requires_query_identifier_but_preserves_empty_identifier(factory, field):
    option = factory()
    option[field] = None
    with pytest.raises(RuntimeError, match="missing its query identifier"):
        _catalog_from_row(snapshot([option]))
    option[field] = ""
    assert _catalog_from_row(snapshot([option])).options[0][field] == ""


@pytest.mark.parametrize("field,value", [
    ("min_value", float("-inf")), ("max_value", float("inf")),
    ("p5_value", float("nan")), ("median_value", float("inf")), ("p95_value", float("-inf")),
])
def test_snapshot_rejects_non_finite_optional_statistics(field, value):
    option = raw_option()
    option[field] = value
    with pytest.raises(RuntimeError, match="non-finite statistic"):
        _catalog_from_row(snapshot([option]))


def test_snapshot_rejects_inverted_range_and_keeps_optional_statistics():
    option = raw_option()
    option.update(min_value=2.0, max_value=1.0)
    with pytest.raises(RuntimeError, match="inverted numeric range"):
        _catalog_from_row(snapshot([option]))
    option.update(min_value=None, max_value=None)
    assert _catalog_from_row(snapshot([option])).options == [option]


def test_snapshot_histogram_must_count_the_options_measurements():
    option = raw_option()
    option["histogram"] = {
        "domain_min": 1.0, "domain_max": 1.0, "domain_kind": "full_range",
        "bin_count": 1, "counts": [2], "underflow_count": 0, "overflow_count": 0, "total_count": 2,
    }
    with pytest.raises(RuntimeError, match="does not match its measurement count"):
        _catalog_from_row(snapshot([option]))
    option["histogram"]["counts"] = [1]
    option["histogram"]["total_count"] = 1
    assert _catalog_from_row(snapshot([option])).options == [option]


def test_snapshot_without_legacy_histograms_or_with_non_numeric_records_stays_valid():
    stored = snapshot([raw_option()])
    stored["total_records"] += 2
    stored["raw_records"] += 2
    parsed = _catalog_from_row(stored)
    assert parsed.total_records == 3
    assert parsed.options[0]["rows"] == 1
    assert "histogram" not in parsed.options[0]


@pytest.mark.parametrize("latest,batch,expected", [
    (None, None, True), (None, 5, False), (5, None, False),
    (5, 4, False), (5, 5, True), (5, 6, False),
])
def test_catalog_revision_must_equal_latest_completed_import(latest, batch, expected):
    class Connection:
        def execute(self, _query):
            return self

        def fetchone(self):
            return {"import_batch_id": latest}

    catalog = replace(_catalog_from_row(snapshot([raw_option()])), import_batch_id=batch)
    assert property_filter_catalog_is_current(Connection(), catalog) is expected


def aggregate_fixture(connection, options):
    connection.execute("""CREATE TEMP TABLE catalog_integrity_records (
        filter_record_id bigint, property_key text, property_name text,
        property_label text, canonical_unit text, property_unit_clean text,
        canonical_value double precision, property_value_num double precision,
        canonical_smiles text, smiles text
    ) ON COMMIT DROP""")
    for record_id, option in enumerate(options, 1):
        connection.execute("""INSERT INTO catalog_integrity_records VALUES
            (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", (
                record_id, option.get("property_key"), option.get("property_name") or "Mapped property",
                option["label"], option.get("canonical_unit"), option.get("property_unit_clean"),
                float(record_id) if option["filter_type"] == "standardized" else None,
                float(record_id), "C" * record_id, "C" * record_id,
            ))
    return aggregate_property_filter_catalog(connection, schema=None, table="catalog_integrity_records")


@pytest.mark.parametrize("options", [
    [raw_option("A|B", "C"), raw_option("A", "B|C")],
    [standard_option("A:B", "C"), standard_option("A", "B:C")],
])
def test_ambiguous_sql_catalog_keeps_histograms_separate(test_database, options):
    with test_database.connect() as connection:
        total, mapped, raw, actual = aggregate_fixture(connection, options)
    assert total == mapped + raw == 2
    assert len(actual) == 2
    assert len({option["option_key"] for option in actual}) == 2
    assert {option["min_value"] for option in actual} == {1.0, 2.0}
    for option in actual:
        assert option["rows"] == option["histogram"]["total_count"] == 1
        assert option["histogram"]["domain_min"] == option["min_value"]
        assert option["histogram"]["domain_max"] == option["max_value"]
        assert option["histogram"]["counts"] == [1]


@pytest.mark.parametrize("factory", [raw_option, standard_option])
def test_null_and_empty_units_share_one_exact_sql_option(test_database, factory):
    with test_database.connect() as connection:
        total, mapped, raw, actual = aggregate_fixture(connection, [factory(unit=None), factory(unit="")])
    assert total == mapped + raw == 2
    assert len(actual) == 1
    assert actual[0]["option_key"] == factory(unit="")["option_key"]
    assert actual[0]["rows"] == actual[0]["histogram"]["total_count"] == 2
    assert actual[0]["unique_smiles"] == 2


def test_duplicate_snapshot_is_unavailable_to_mcp_and_rest_keeps_live_fallback(seeded_db):
    with seeded_db.connect() as connection:
        connection.execute("""UPDATE governance.property_filter_options_snapshots
            SET options = options || jsonb_build_array(options -> 0)""")
        with pytest.raises(PropertyFilterCatalogUnavailable, match="current property-filter catalog"):
            load_property_filter_context(connection, allow_live_fallback=False)
        context = load_property_filter_context(connection)
        assert context.mode == "live-fallback"
        assert context.catalog is None
        assert len(context.options) == len({option["option_key"] for option in context.options}) == 3


def test_catalog_rejects_failed_or_nonexistent_batch_and_keeps_unbatched_legacy(seeded_db):
    with seeded_db.connect() as connection:
        catalog = load_property_filter_catalog(connection)
        assert property_filter_catalog_is_current(connection, catalog)
        failed_id = connection.execute("""INSERT INTO governance.import_batches
            (dataset_key, source_file_id, status, row_count, finished_at)
            SELECT 'property_filter', source_file_id, 'failed', 0, now()
            FROM governance.source_files WHERE logical_name = 'property_filter_csv'
            RETURNING import_batch_id""").fetchone()["import_batch_id"]
        assert property_filter_catalog_is_current(connection, catalog)
        assert not property_filter_catalog_is_current(connection, replace(catalog, import_batch_id=failed_id))
        assert not property_filter_catalog_is_current(connection, replace(catalog, import_batch_id=failed_id + 1000))
        # If there is no completed import, only a catalog with no batch binding
        # retains the historical unbatched compatibility behavior.
        connection.execute("UPDATE governance.import_batches SET status = 'failed'")
        assert not property_filter_catalog_is_current(connection, catalog)
        assert property_filter_catalog_is_current(connection, replace(catalog, import_batch_id=None))
