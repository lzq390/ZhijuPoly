from __future__ import annotations

from fastapi.testclient import TestClient

from app.postgres_database import postgres_connection


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


def test_property_filter_options_report_empty_table_as_not_ready(test_app) -> None:
    with postgres_connection(test_app.state.settings.app_postgres_dsn) as connection:
        connection.execute("TRUNCATE core.polymer_property_filter_records")
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
