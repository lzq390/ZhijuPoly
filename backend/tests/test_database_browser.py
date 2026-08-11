from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.postgres_database import postgres_connection
from app.services.analytics_snapshot_store import save_analytics_snapshot
from app.services.postgres_database_browser import get_database_analytics_postgres, get_property_filter_options_postgres
from app.services.property_filter_catalog import (
    load_property_filter_catalog,
    rebuild_property_filter_catalog,
)


def test_experimental_process_browser_returns_empty_when_postgres_table_empty(test_app) -> None:
    client = TestClient(test_app)

    response = client.get("/api/v1/database-browser/experimental-process?page=1&page_size=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_records"] == 0
    assert payload["matched_records"] == 0
    assert payload["data_source"] == "postgres"
    assert payload["source_status"] == "ready"
    assert payload["source_message"] is None
    assert payload["results"] == []


def test_experimental_property_browser_returns_empty_when_postgres_table_empty(test_app) -> None:
    client = TestClient(test_app)

    response = client.get("/api/v1/database-browser/experimental-property?q=Tg&page=1&page_size=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_records"] == 0
    assert payload["matched_records"] == 0
    assert payload["data_source"] == "postgres"
    assert payload["source_status"] == "ready"
    assert payload["source_message"] is None
    assert payload["results"] == []


def test_database_browser_dataset_summary_reports_all_dataset_keys(test_app) -> None:
    client = TestClient(test_app)

    response = client.get("/api/v1/database-browser/datasets/summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["backend"] == "postgres"
    by_key = {item["key"]: item for item in payload["datasets"]}
    assert set(by_key) == {"process", "property", "structureEffect", "propertyFilter", "dft", "formulation"}
    assert by_key["process"]["source_status"] == "ready"
    assert by_key["process"]["total_records"] == 0
    assert by_key["property"]["total_records"] == 0
    assert by_key["property"]["source_status"] == "ready"
    assert by_key["structureEffect"]["total_records"] == 6
    assert by_key["propertyFilter"]["total_records"] == 6
    assert by_key["propertyFilter"]["source_status"] == "ready"
    assert by_key["dft"]["total_records"] == 5


def test_database_browser_live_analytics_includes_property_filter_counts(test_app) -> None:
    client = TestClient(test_app)

    response = client.get("/api/v1/database-browser/datasets/analytics?refresh=true")

    assert response.status_code == 200
    payload = response.json()
    property_filter = payload["datasets"]["propertyFilter"]
    assert payload["source"] == "live"
    assert property_filter["rows"] == 6
    assert property_filter["mappedRows"] == 4
    assert property_filter["rawRows"] == 2
    assert property_filter["standardizedProperties"] == 2
    assert property_filter["rawProperties"] == 1
    assert property_filter["uniqueSmiles"] == 2


def test_database_browser_refresh_reuses_unchanged_snapshot(test_app, monkeypatch) -> None:
    with postgres_connection(test_app.state.settings.app_postgres_dsn) as connection:
        datasets = get_database_analytics_postgres(connection)
        stored = save_analytics_snapshot(connection, datasets)

    def fail_if_recomputed(_connection):
        raise AssertionError("unchanged analytics snapshot must not be recomputed")

    monkeypatch.setattr(
        "app.routers.database_browser.get_database_analytics_postgres",
        fail_if_recomputed,
    )
    response = TestClient(test_app).get("/api/v1/database-browser/datasets/analytics?refresh=true")

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "snapshot"
    assert payload["refresh_status"] == "unchanged"
    assert payload["generated_at"] == stored.generated_at.isoformat()
    assert payload["datasets"] == datasets


def test_database_browser_refresh_recomputes_new_import_once(test_app, monkeypatch) -> None:
    with postgres_connection(test_app.state.settings.app_postgres_dsn) as connection:
        datasets = get_database_analytics_postgres(connection)
        save_analytics_snapshot(
            connection,
            datasets,
            generated_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        connection.execute(
            """
            INSERT INTO governance.import_batches (
              dataset_key, finished_at, status, row_count
            ) VALUES ('core', now(), 'completed', 6)
            """
        )

    original = get_database_analytics_postgres
    recompute_calls = 0

    def count_recompute(connection):
        nonlocal recompute_calls
        recompute_calls += 1
        return original(connection)

    monkeypatch.setattr(
        "app.routers.database_browser.get_database_analytics_postgres",
        count_recompute,
    )
    client = TestClient(test_app)
    refreshed = client.get("/api/v1/database-browser/datasets/analytics?refresh=true")
    unchanged = client.get("/api/v1/database-browser/datasets/analytics?refresh=true")

    assert refreshed.status_code == 200
    assert refreshed.json()["source"] == "live"
    assert refreshed.json()["refresh_status"] == "recomputed"
    assert unchanged.status_code == 200
    assert unchanged.json()["source"] == "snapshot"
    assert unchanged.json()["refresh_status"] == "unchanged"
    assert recompute_calls == 1


def test_database_browser_refresh_detects_direct_row_count_change(test_app) -> None:
    with postgres_connection(test_app.state.settings.app_postgres_dsn) as connection:
        datasets = get_database_analytics_postgres(connection)
        save_analytics_snapshot(connection, datasets)
        connection.execute(
            """
            INSERT INTO experimental.process_records (
              source_file, source_row_number, polymer_name,
              process_flow_original_text, material_original_text
            ) VALUES ('manual-change.csv', 1, 'Poly A', 'heated and stirred', 'ODA')
            """
        )

    response = TestClient(test_app).get("/api/v1/database-browser/datasets/analytics?refresh=true")

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "live"
    assert payload["refresh_status"] == "recomputed"
    assert payload["datasets"]["process"]["rows"] == datasets["process"]["rows"] + 1


def test_database_browser_refresh_replaces_invalid_snapshot(test_app) -> None:
    with postgres_connection(test_app.state.settings.app_postgres_dsn) as connection:
        connection.execute(
            """
            INSERT INTO governance.database_analytics_snapshots (
              snapshot_key, generated_at, datasets
            ) VALUES ('database-browser', now(), '{"process": {"rows": 0}}'::jsonb)
            """
        )

    client = TestClient(test_app)
    refreshed = client.get("/api/v1/database-browser/datasets/analytics?refresh=true")
    stored = client.get("/api/v1/database-browser/datasets/analytics")

    assert refreshed.status_code == 200
    assert refreshed.json()["source"] == "live"
    assert refreshed.json()["refresh_status"] == "recomputed"
    assert stored.status_code == 200
    assert stored.json()["source"] == "snapshot"
    assert set(stored.json()["datasets"]) == {
        "process",
        "property",
        "structureEffect",
        "propertyFilter",
        "dft",
        "formulation",
    }


def test_database_browser_snapshot_never_falls_back_to_checked_in_python(test_app) -> None:
    client = TestClient(test_app)

    missing = client.get("/api/v1/database-browser/datasets/analytics")

    assert missing.status_code == 503
    assert missing.json()["detail"] == "Postgres analytics snapshot is missing"

    datasets = {
        key: {"rows": index}
        for index, key in enumerate(
            ("process", "property", "structureEffect", "propertyFilter", "dft", "formulation")
        )
    }
    generated_at = datetime(2026, 7, 14, tzinfo=timezone.utc)
    with postgres_connection(test_app.state.settings.app_postgres_dsn) as connection:
        save_analytics_snapshot(connection, datasets, generated_at=generated_at, source_sha="a" * 40)

    stored = client.get("/api/v1/database-browser/datasets/analytics")

    assert stored.status_code == 200
    payload = stored.json()
    assert payload["source"] == "snapshot"
    assert payload["generated_at"] == generated_at.isoformat()
    assert payload["datasets"] == datasets


def test_property_filter_options_include_standardized_and_raw_properties(test_app) -> None:
    client = TestClient(test_app)

    response = client.get("/api/v1/database-browser/property-filter/options")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_records"] == 6
    assert payload["mapped_records"] == 4
    assert payload["raw_records"] == 2
    options = payload["options"]
    tg_option = next(item for item in options if item["filter_type"] == "standardized" and item["property_key"] == "tg")
    raw_option = next(item for item in options if item["filter_type"] == "raw" and item["property_name"] == "Cv")
    assert tg_option["canonical_unit"] == "C"
    assert tg_option["rows"] == 2
    assert raw_option["property_unit_clean"] == "cal/(g*C)"
    assert raw_option["rows"] == 2
    assert response.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert response.headers["etag"].startswith('W/"pf-options-v1-')
    assert "catalog;dur=" in response.headers["server-timing"]

    conditional = client.get(
        "/api/v1/database-browser/property-filter/options",
        headers={"If-None-Match": response.headers["etag"]},
    )
    assert conditional.status_code == 304
    assert conditional.content == b""


def test_property_filter_options_fall_back_when_snapshot_is_missing(test_app) -> None:
    with postgres_connection(test_app.state.settings.app_postgres_dsn) as connection:
        connection.execute(
            "DELETE FROM governance.property_filter_options_snapshots WHERE snapshot_key = 'current'"
        )

    response = TestClient(test_app).get("/api/v1/database-browser/property-filter/options")

    assert response.status_code == 200
    assert response.json()["total_records"] == 6
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers


def test_property_filter_options_fall_back_when_snapshot_payload_is_invalid(test_app) -> None:
    with postgres_connection(test_app.state.settings.app_postgres_dsn) as connection:
        connection.execute(
            """
            UPDATE governance.property_filter_options_snapshots
            SET options = '[{}]'::jsonb
            WHERE snapshot_key = 'current'
            """
        )

    response = TestClient(test_app).get("/api/v1/database-browser/property-filter/options")

    assert response.status_code == 200
    assert response.json()["total_records"] == 6
    assert len(response.json()["options"]) == 3
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers


def test_property_filter_options_fall_back_when_newer_import_has_no_snapshot(test_app) -> None:
    with postgres_connection(test_app.state.settings.app_postgres_dsn) as connection:
        connection.execute(
            """
            INSERT INTO governance.import_batches (
              dataset_key, source_file_id, finished_at, status, row_count
            )
            SELECT 'property_filter', source_file_id, now(), 'completed', 6
            FROM governance.source_files
            WHERE logical_name = 'property_filter_csv'
            """
        )

    response = TestClient(test_app).get(
        "/api/v1/database-browser/property-filter/options"
    )

    assert response.status_code == 200
    assert response.json()["total_records"] == 6
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers


def test_property_filter_snapshot_matches_live_aggregation_field_for_field(test_app) -> None:
    with postgres_connection(test_app.state.settings.app_postgres_dsn) as connection:
        catalog = load_property_filter_catalog(connection)
        live_total, live_mapped, live_raw, live_options = (
            get_property_filter_options_postgres(connection)
        )

    assert catalog is not None
    assert (
        catalog.total_records,
        catalog.mapped_records,
        catalog.raw_records,
        catalog.options,
    ) == (live_total, live_mapped, live_raw, live_options)


def test_property_filter_statistics_group_by_canonical_smiles_first(test_app) -> None:
    with postgres_connection(test_app.state.settings.app_postgres_dsn) as connection:
        connection.execute(
            """
            INSERT INTO core.polymer_property_filter_records (
              filter_record_id, source_file, source_row_number, polymer_name,
              smiles, canonical_smiles, rdkit_parse_ok, property_category,
              property_name, property_value, property_value_num,
              property_unit_raw, property_unit_clean, property_key,
              property_label, canonical_value, canonical_unit,
              unit_conversion_status, value_origin, reliable_score
            ) VALUES
              (101, 'canonical-stats.csv', 1, 'canonical polymer',
               'raw-form-one', 'canonical-shared', false, 'Thermal',
               'Tg', '150', 150, 'C', 'C', 'tg',
               'Glass transition temperature', 150, 'C',
               'already_standard', 'observed', 0.99),
              (102, 'canonical-stats.csv', 2, 'canonical polymer',
               'raw-form-two', 'canonical-shared', false, 'Thermal',
               'Tg', '151', 151, 'C', 'C', 'tg',
               'Glass transition temperature', 151, 'C',
               'already_standard', 'observed', 0.98)
            """
        )
        rebuild_property_filter_catalog(connection)

    client = TestClient(test_app)
    options_response = client.get("/api/v1/database-browser/property-filter/options")
    analytics_response = client.get("/api/v1/database-browser/datasets/analytics?refresh=true")

    assert options_response.status_code == 200
    tg_option = next(
        option
        for option in options_response.json()["options"]
        if option["filter_type"] == "standardized" and option["property_key"] == "tg"
    )
    assert tg_option["unique_smiles"] == 3
    assert analytics_response.status_code == 200
    assert analytics_response.json()["datasets"]["propertyFilter"]["uniqueSmiles"] == 3


def test_property_filter_options_report_empty_table_as_not_ready(test_app) -> None:
    with postgres_connection(test_app.state.settings.app_postgres_dsn) as connection:
        connection.execute("TRUNCATE core.polymer_property_filter_records")
        rebuild_property_filter_catalog(connection)
    client = TestClient(test_app)

    response = client.get("/api/v1/database-browser/property-filter/options")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_records"] == 0
    assert payload["source_status"] == "empty"
    assert "no records" in payload["source_message"]
    assert payload["options"] == []


def test_property_filter_search_filters_standardized_property_range(test_app) -> None:
    client = TestClient(test_app)

    response = client.post(
        "/api/v1/database-browser/property-filter/search",
        json={
            "filters": [
                {
                    "filter_type": "standardized",
                    "property_key": "tg",
                    "canonical_unit": "C",
                    "min_value": 100,
                    "max_value": 200,
                }
            ],
            "page": 1,
            "page_size": 10,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_records"] == 6
    assert payload["matched_records"] == 1
    assert payload["results"][0]["smiles"] == "CCO"
    assert payload["results"][0]["records"][0]["property_key"] == "tg"
    assert payload["results"][0]["records"][0]["canonical_value"] == 123.4
    assert response.headers["cache-control"] == "no-store"
    assert "search;dur=" in response.headers["server-timing"]


def test_property_filter_search_ands_multiple_conditions_by_smiles(test_app) -> None:
    client = TestClient(test_app)

    response = client.post(
        "/api/v1/database-browser/property-filter/search",
        json={
            "filters": [
                {
                    "filter_type": "standardized",
                    "property_key": "tg",
                    "canonical_unit": "C",
                    "min_value": 100,
                    "max_value": 200,
                },
                {
                    "filter_type": "standardized",
                    "property_key": "bandgap",
                    "canonical_unit": "eV",
                    "max_value": 4,
                },
            ],
            "page": 1,
            "page_size": 10,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["matched_records"] == 1
    assert payload["results"][0]["smiles"] == "CCO"
    assert payload["results"][0]["matched_filters"] == 2
    assert {record["property_key"] for record in payload["results"][0]["records"]} == {"tg", "bandgap"}


def test_property_filter_search_preserves_counts_on_out_of_range_page(test_app) -> None:
    response = TestClient(test_app).post(
        "/api/v1/database-browser/property-filter/search",
        json={
            "filters": [
                {
                    "filter_type": "standardized",
                    "property_key": "tg",
                    "canonical_unit": "C",
                    "min_value": 100,
                    "max_value": 200,
                }
            ],
            "page": 999,
            "page_size": 10,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_records"] == 6
    assert payload["matched_records"] == 1
    assert payload["results"] == []


def test_property_filter_keyword_remains_scoped_to_each_and_branch(test_app) -> None:
    response = TestClient(test_app).post(
        "/api/v1/database-browser/property-filter/search",
        json={
            "filters": [
                {
                    "filter_type": "standardized",
                    "property_key": "tg",
                    "canonical_unit": "C",
                    "min_value": 100,
                    "max_value": 200,
                },
                {
                    "filter_type": "standardized",
                    "property_key": "bandgap",
                    "canonical_unit": "eV",
                    "max_value": 4,
                },
            ],
            "q": "polymer_a",
            "page": 1,
            "page_size": 10,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["matched_records"] == 1
    assert payload["results"][0]["polymer_name"] == "polymer_a"


def test_property_filter_search_groups_by_canonical_smiles_before_raw_smiles(test_app) -> None:
    with postgres_connection(test_app.state.settings.app_postgres_dsn) as connection:
        connection.execute(
            """
            INSERT INTO core.polymer_property_filter_records (
              filter_record_id, source_file, source_row_number, polymer_name,
              smiles, canonical_smiles, rdkit_parse_ok, property_category,
              property_name, property_value, property_value_num,
              property_unit_raw, property_unit_clean, property_key,
              property_label, canonical_value, canonical_unit,
              unit_conversion_status, value_origin, reliable_score
            ) VALUES
              (101, 'canonical-regression.csv', 1, 'canonical polymer',
               'raw-tg-form', 'canonical-shared', false, 'Thermal',
               'Tg', '150', 150, 'C', 'C', 'tg',
               'Glass transition temperature', 150, 'C',
               'already_standard', 'observed', 0.99),
              (102, 'canonical-regression.csv', 2, 'canonical polymer',
               'raw-bandgap-form', 'canonical-shared', false, 'Electronic',
               'Bandgap', '2.5', 2.5, 'eV', 'eV', 'bandgap',
               'Bandgap', 2.5, 'eV',
               'already_standard', 'observed', 0.98)
            """
        )

    client = TestClient(test_app)
    response = client.post(
        "/api/v1/database-browser/property-filter/search",
        json={
            "filters": [
                {
                    "filter_type": "standardized",
                    "property_key": "tg",
                    "canonical_unit": "C",
                    "min_value": 149,
                    "max_value": 151,
                },
                {
                    "filter_type": "standardized",
                    "property_key": "bandgap",
                    "canonical_unit": "eV",
                    "min_value": 2.4,
                    "max_value": 2.6,
                },
            ],
            "page": 1,
            "page_size": 10,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["matched_records"] == 1
    assert payload["results"][0]["canonical_smiles"] == "canonical-shared"
    assert payload["results"][0]["matched_filters"] == 2
    assert {record["smiles"] for record in payload["results"][0]["records"]} == {
        "raw-tg-form",
        "raw-bandgap-form",
    }


def test_property_filter_search_supports_raw_property_range(test_app) -> None:
    client = TestClient(test_app)

    response = client.post(
        "/api/v1/database-browser/property-filter/search",
        json={
            "filters": [
                {
                    "filter_type": "raw",
                    "property_name": "Cv",
                    "property_unit_clean": "cal/(g*C)",
                    "min_value": 0.3,
                    "max_value": 0.4,
                }
            ],
            "page": 1,
            "page_size": 10,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["matched_records"] == 1
    assert payload["results"][0]["smiles"] == "CCN"
    assert payload["results"][0]["records"][0]["property_name"] == "Cv"
    assert payload["results"][0]["records"][0]["property_key"] is None


def test_smiles_lookup_properties_returns_all_matching_rows(test_app) -> None:
    extra_rows = [
        (
            1000 + index,
            1,
            "Mechanical",
            f"Bulk modulus {index:02d}",
            str(index),
            float(index),
            "GPa",
            "exp",
            1000 + index,
        )
        for index in range(60)
    ]
    with postgres_connection(test_app.state.settings.app_postgres_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO core.polymer_properties (
                  property_id, polymer_id, property_category, property_name,
                  property_value, property_value_num, property_unit, label_source,
                  source_row_number
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                extra_rows,
            )

    client = TestClient(test_app)
    response = client.post("/api/v1/database-browser/smiles-lookup", json={"smiles": "CCO", "table": "properties"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["table"] == "properties"
    assert payload["total"] == 63
    assert len(payload["results"]) == payload["total"]
    assert payload["results"][-1]["fields"]["property_name"] == "Bulk modulus 59"


def test_formulation_browser_reads_postgres_formulation_records(test_app) -> None:
    with postgres_connection(test_app.state.settings.app_postgres_dsn) as connection:
        connection.execute(
            """
            INSERT INTO knowledge.documents (
              knowledge_id,
              source_file,
              source_row_number,
              title_en,
              abstract,
              polymer_iupac,
              formulation,
              catalyst,
              temperature,
              reaction_time,
              solvent
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                10,
                "fixture.xlsx",
                2,
                "Formulation record",
                "abstract",
                "polymer A",
                "monomer A:monomer B = 1:1",
                "TEA",
                "80 C",
                "4 h",
                "DMF",
            ),
        )
        connection.execute(
            """
            INSERT INTO knowledge.formulation_records (
              knowledge_id,
              source_file,
              source_row_number,
              polymer_iupac,
              formulation,
              catalyst,
              temperature,
              reaction_time,
              solvent
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                10,
                "fixture.xlsx",
                2,
                "polymer A",
                "monomer A:monomer B = 1:1",
                "TEA",
                "80 C",
                "4 h",
                "DMF",
            ),
        )

    client = TestClient(test_app)
    response = client.get("/api/v1/database-browser/formulation", params={"q": "DMF", "page": 1, "page_size": 10})

    assert response.status_code == 200
    payload = response.json()
    assert payload["data_source"] == "postgres"
    assert payload["source_status"] == "ready"
    assert payload["total_records"] == 1
    assert payload["matched_records"] == 1
    assert payload["results"][0]["formulation"] == "monomer A:monomer B = 1:1"
    assert payload["results"][0]["solvent"] == "DMF"
