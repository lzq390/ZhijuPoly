"""Catalog-to-query regressions without an application database or settings."""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.models import PropertyFilterCondition, PropertyFilterRecord, PropertyFilterSearchRequest
from app.property_filter_mcp import MCPRuntime, QueryControl
from app.property_filter_mcp_contract import (
    MAX_PAGE, FilterInput, HistogramInput, ListInput, SearchInput, bind_condition, public_option,
)
from app.property_filter_mcp_security import ClientCredential, MCPConfig
from app.services import property_filter as service
from app.services.postgres_database_browser import _property_filter_condition_select
from app.services.property_filter import PropertyFilterContext, resolve_property_filter_option
from app.services.property_filter_catalog import PropertyFilterCatalog


def catalog_option(**fields):
    row = {
        "filter_type": "raw", "option_key": "raw:fixture", "label": "Fixture property",
        "property_name": "Raw property", "property_unit_clean": "MPa", "rows": 1,
        "unique_smiles": 1, "min_value": 0, "max_value": 100,
        **fields,
    }
    context = PropertyFilterContext(None, [row], 1, 0, 1, "ready", None, "snapshot")
    return resolve_property_filter_option(context, row["option_key"])


@pytest.mark.parametrize("fields", [
    {"property_name": "A" * 241},
    {"property_unit_clean": "u" * 121},
    {"filter_type": "standardized", "property_key": "p" * 121, "canonical_unit": "C"},
    {"filter_type": "standardized", "property_key": "tg", "canonical_unit": "u" * 121},
    {"property_name": " raw property ", "property_unit_clean": " unit "},
    {"filter_type": "standardized", "property_key": " property ", "canonical_unit": " unit "},
    {"property_name": ""},
    {"filter_type": "standardized", "property_key": "", "canonical_unit": "C"},
])
def test_catalog_identifiers_reach_search_sql_without_rest_limits_or_normalization(fields):
    option = catalog_option(**fields)
    published = public_option(option)
    condition = bind_condition(option, FilterInput(option_key=published.option_key, min_value=0, max_value=100))
    # MCP uses the existing request model before invoking the shared query service.
    request = PropertyFilterSearchRequest(filters=[condition])
    _, params = _property_filter_condition_select(0, request.filters[0], "")
    identity = fields.get("property_key") if option.filter_type == "standardized" else fields.get("property_name", "Raw property")
    unit = fields.get("canonical_unit") if option.filter_type == "standardized" else fields.get("property_unit_clean", "MPa")
    assert params == [0, identity, unit, 0.0, 100.0]
    assert published.unit == unit
    if option.filter_type == "raw":
        assert published.property_name == identity
    else:
        assert published.property_key == identity


@pytest.mark.parametrize("filter_type,unit_field", [
    ("raw", "property_unit_clean"), ("standardized", "canonical_unit"),
])
@pytest.mark.parametrize("unit", [None, ""])
def test_catalog_empty_units_stay_exact(filter_type, unit_field, unit):
    option = catalog_option(filter_type=filter_type, property_key="tg", **{unit_field: unit})
    condition = bind_condition(option, FilterInput(option_key=option.option_key, min_value=0))
    _, params = _property_filter_condition_select(0, condition, "")
    assert getattr(condition, unit_field) == ""
    assert params[2] == ""


def test_long_opaque_catalog_key_can_be_reused_for_search_and_histogram():
    key = "std:" + "p" * 300 + ":" + "u" * 300
    option = catalog_option(filter_type="standardized", option_key=key, property_key="p" * 300,
                            canonical_unit="u" * 300)
    published = public_option(option)
    search = SearchInput.model_validate_json(json.dumps({
        "dataset_revision": "revision", "filters": [{"option_key": published.option_key, "min_value": 0}],
    }))
    histogram = HistogramInput(dataset_revision="revision", option_key=published.option_key)
    condition = bind_condition(option, search.filters[0])
    assert search.filters[0].option_key == histogram.option_key == key
    assert condition.property_key == "p" * 300
    assert condition.canonical_unit == "u" * 300


@pytest.mark.parametrize("filter_type", ["raw", "standardized"])
def test_runtime_search_accepts_listed_long_catalog_option(monkeypatch, tmp_path, filter_type):
    option = catalog_option(filter_type=filter_type, property_name="A" * 300,
                            property_key="p" * 300, canonical_unit="u" * 300,
                            property_unit_clean="u" * 300)
    catalog = PropertyFilterCatalog(1, 1, 1, None, datetime.now(timezone.utc), 1, 1, 0,
                                    [option.model_dump()])
    context = PropertyFilterContext(catalog, catalog.options, 1, 1, 0, "ready", None, "snapshot")
    runtime = MCPRuntime(MCPConfig(dsn="unused-test-dsn", clients_file=tmp_path / "unused"))

    @contextmanager
    def transaction(control=None):
        yield object()

    monkeypatch.setattr(runtime, "transaction", transaction)
    monkeypatch.setattr(runtime, "_context", lambda connection: context)
    monkeypatch.setattr(service, "postgres_table_exists", lambda *args: True)
    monkeypatch.setattr(service, "source_file_status", lambda *args: ("ready", None))
    executed_conditions = []

    def search_records(connection, *, conditions, **kwargs):
        condition = conditions[0]
        executed_conditions.append(condition)
        _, params = _property_filter_condition_select(0, condition, "")
        identity = option.property_key if filter_type == "standardized" else option.property_name
        assert params == [0, identity, "u" * 300, 0.0]
        record = PropertyFilterRecord(
            filter_record_id=1, source_row_number=1, smiles="CC", property_category="Test",
            property_name=option.property_name, property_value="10", property_value_num=10,
            property_key=option.property_key if filter_type == "standardized" else None,
            canonical_value=10 if filter_type == "standardized" else None,
            canonical_unit=option.canonical_unit if filter_type == "standardized" else None,
            property_unit_clean=option.property_unit_clean, filter_index=0,
        )
        return 1, 1, [{**record.model_dump(), "group_key": "CC", "matching_measurement_count": 1}]

    monkeypatch.setattr(service, "search_property_filter_records_postgres", search_records)
    credential = ClientCredential(client_id="test", token_sha256="0" * 64,
                                   expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                                   tools={"list_filter_properties", "search_polymers"})
    request_context = SimpleNamespace(
        request=SimpleNamespace(state=SimpleNamespace(mcp_client=credential, mcp_request_id="fixture")),
        request_id="fixture",
    )

    async def exercise():
        listed = await runtime.call(request_context, SimpleNamespace(name="list_filter_properties", arguments={}))
        assert not listed.is_error
        published = listed.structured_content["options"][0]
        result = await runtime.call(request_context, SimpleNamespace(name="search_polymers", arguments={
            "dataset_revision": listed.structured_content["dataset_revision"],
            "filters": [{"option_key": published["option_key"], "min_value": 0}],
        }))
        assert not result.is_error, result.structured_content
        assert result.structured_content["matched_material_count"] == 1
        assert result.structured_content["results"][0]["evidence"][0]["unit"] == "u" * 300
        for invalid in ({"page": 2**63}, {"filters": [{"option_key": published["option_key"], "min_value": True}]}):
            rejected = await runtime.call(request_context, SimpleNamespace(name="search_polymers", arguments={
                "dataset_revision": listed.structured_content["dataset_revision"],
                "filters": [{"option_key": published["option_key"], "min_value": 0}], **invalid,
            }))
            assert rejected.is_error
            assert rejected.structured_content["error"]["code"] == "INVALID_ARGUMENT"
        assert len(executed_conditions) == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("filter_type,identity,unit", [
    ("raw", "Long raw property " + "A" * 300, "raw-unit-" + "u" * 150),
    ("standardized", "long_standardized_key_" + "p" * 300, "canonical-unit-" + "u" * 300),
    ("raw", " raw property with surrounding whitespace ", " raw unit "),
    ("standardized", " standardized key with surrounding whitespace ", " canonical unit "),
    ("raw", "", "raw-empty-name-unit"),
    ("standardized", "", "standardized-empty-key-unit"),
])
def test_isolated_database_catalog_options_can_be_listed_searched_and_histogrammed(
    seeded_db, tmp_path, filter_type, identity, unit,
):
    """Use only conftest's explicit disposable mcp_test_* database and reader."""
    property_key = identity if filter_type == "standardized" else None
    property_name = identity if filter_type == "raw" else "Standardized fixture property"
    with seeded_db.connect() as connection:
        connection.execute("""
            INSERT INTO core.polymer_property_filter_records
              (filter_record_id,source_file,source_row_number,polymer_name,smiles,canonical_smiles,
               property_name,property_value,property_value_num,property_unit_clean,
               property_key,canonical_value,canonical_unit)
            VALUES (-101,'catalog_binding_fixture',1,'Exact catalog binding fixture','FCF','FCF',
                    %s,'12.5',12.5,%s,%s,%s,%s)
        """, (property_name, unit, property_key, 12.5 if property_key is not None else None,
              unit if property_key is not None else None))
    seeded_db.refresh_catalog()
    runtime = MCPRuntime(MCPConfig(dsn=seeded_db.reader_dsn, clients_file=tmp_path / "unused"))
    listed = runtime.execute("list_filter_properties", ListInput(page_size=50), QueryControl())
    option = next(option for option in listed.options if (
        option.filter_type == filter_type
        and (option.property_key if filter_type == "standardized" else option.property_name) == identity
    ))
    assert option.unit == unit
    search = runtime.execute("search_polymers", SearchInput(
        dataset_revision=listed.dataset_revision,
        filters=[{"option_key": option.option_key, "min_value": 12.5, "max_value": 12.5}],
    ), QueryControl())
    assert search.matched_material_count == 1
    assert search.results[0].polymer_name == "Exact catalog binding fixture"
    evidence = search.results[0].evidence[0]
    assert evidence.option_key == option.option_key
    assert evidence.unit == unit
    assert evidence.matching_measurement_count == 1
    assert evidence.measurements[0].filter_record_id == -101
    histogram = runtime.execute("get_property_histogram", HistogramInput(
        dataset_revision=listed.dataset_revision, option_key=option.option_key,
    ), QueryControl())
    assert histogram.option_key == option.option_key
    assert histogram.unit == unit
    assert histogram.histogram.total_count == 1
    assert histogram.histogram.domain_min == histogram.histogram.domain_max == 12.5


@pytest.mark.parametrize("fields", [
    {"property_name": "A" * 241},
    {"property_unit_clean": "u" * 121},
    {"filter_type": "standardized", "property_key": "p" * 121},
    {"filter_type": "standardized", "property_key": "tg", "canonical_unit": "u" * 121},
    {"property_name": ""},
    {"filter_type": "standardized", "property_key": ""},
])
def test_rest_condition_retains_its_existing_identifier_boundaries(fields):
    with pytest.raises(ValidationError):
        PropertyFilterSearchRequest.model_validate({
            "filters": [{"filter_type": "raw", "property_name": "Raw property", "min_value": 0, **fields}],
        })


def test_rest_condition_retains_its_existing_normalization():
    condition = PropertyFilterCondition(filter_type="raw", property_name=" name ",
                                        property_unit_clean=" unit ", min_value=0)
    assert condition.property_name == "name"
    assert condition.property_unit_clean == "unit"


@pytest.mark.parametrize("bound", ["min_value", "max_value"])
@pytest.mark.parametrize("value", [True, False, "1", float("nan"), float("inf")])
def test_mcp_numeric_bounds_reject_boolean_string_and_nonfinite_values(bound, value):
    with pytest.raises(ValidationError):
        FilterInput(option_key="raw:fixture", **{bound: value})


@pytest.mark.parametrize("model,field,base", [
    (ListInput, "page", {}), (ListInput, "page_size", {}),
    (SearchInput, "page", {"dataset_revision": "v", "filters": [{"option_key": "x", "min_value": 0}]}),
    (SearchInput, "page_size", {"dataset_revision": "v", "filters": [{"option_key": "x", "min_value": 0}]}),
    (SearchInput, "measurement_limit_per_filter", {"dataset_revision": "v", "filters": [{"option_key": "x", "min_value": 0}]}),
])
@pytest.mark.parametrize("value", [True, "1", 1.0])
def test_mcp_pagination_and_evidence_limits_require_json_integers(model, field, base, value):
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps({**base, field: value}))


def test_mcp_json_integer_and_float_bounds_and_integer_limits_are_accepted():
    request = SearchInput.model_validate_json(json.dumps({
        "dataset_revision": "v", "filters": [{"option_key": "x", "min_value": 0, "max_value": 1.5}],
        "page": 1, "page_size": 25, "measurement_limit_per_filter": 3,
    }))
    assert request.filters[0].min_value == 0
    assert request.filters[0].max_value == 1.5
    assert request.page_size == 25
    assert ListInput.model_validate_json('{"page_size":50}').page_size == 50


@pytest.mark.parametrize("page", [999, MAX_PAGE])
def test_large_supported_pages_keep_the_sql_offset_in_bigint_range(page):
    request = SearchInput(dataset_revision="v", filters=[{"option_key": "x", "min_value": 0}],
                          page=page, page_size=25)
    assert request.page == page
    assert (request.page - 1) * request.page_size <= 2**63 - 1
    assert ListInput(page=page, dataset_revision="v").page == page


@pytest.mark.parametrize("model,base", [
    (ListInput, {"dataset_revision": "v"}),
    (SearchInput, {"dataset_revision": "v", "filters": [{"option_key": "x", "min_value": 0}]}),
])
@pytest.mark.parametrize("page", [MAX_PAGE + 1, 2**63])
def test_excessive_pages_fail_argument_validation_before_sql(model, base, page):
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps({**base, "page": page}))


def test_bound_condition_still_validates_structure_and_finite_bounds():
    option = catalog_option()
    # Even server-side construction must validate; it must not use model_construct.
    condition_type = type(bind_condition(option, FilterInput(option_key=option.option_key, min_value=0)))
    for fields in ({"min_value": float("nan")}, {"min_value": 2, "max_value": 1}, {}):
        with pytest.raises(ValidationError):
            condition_type(filter_type="raw", property_name="Raw property", **fields)
    for filter_type in ("raw", "standardized"):
        with pytest.raises(ValidationError) as error:
            condition_type(filter_type=filter_type, min_value=0)
        assert len(error.value.errors()) == 1
