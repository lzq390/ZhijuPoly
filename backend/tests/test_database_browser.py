from __future__ import annotations

from fastapi.testclient import TestClient


def test_experimental_process_browser_returns_empty_when_csv_missing(test_app) -> None:
    client = TestClient(test_app)

    response = client.get("/api/v1/database-browser/experimental-process?page=1&page_size=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_records"] == 0
    assert payload["matched_records"] == 0
    assert payload["results"] == []


def test_experimental_property_browser_falls_back_to_sqlite_when_csv_missing(test_app) -> None:
    client = TestClient(test_app)

    response = client.get("/api/v1/database-browser/experimental-property?q=Tg&page=1&page_size=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_records"] == 6
    assert payload["matched_records"] == 1
    assert payload["results"][0]["source_file"] == "sqlite:properties"
    assert payload["results"][0]["source_row_number"] == 1
    assert payload["results"][0]["property_name_en"] == "Tg"
    assert payload["results"][0]["value"] == "123.4"
