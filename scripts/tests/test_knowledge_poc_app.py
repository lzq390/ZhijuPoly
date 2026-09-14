"""POC entrypoint checks; run with PYTHONPATH=backend in the lightweight venv."""

from fastapi.testclient import TestClient

from app.config import Settings
from app.knowledge_poc import create_app


def make_client():
    # These checks never connect to a database.
    return TestClient(create_app(Settings(
        app_postgres_dsn="postgresql://unused:unused@127.0.0.1:1/unused",
        allowed_origins="http://127.0.0.1:5173",
    )))


def test_poc_serves_health_and_knowledge_validation_without_scientific_routes():
    with make_client() as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/api/v1/knowledge/search", json={"query": ""}).status_code == 422
        assert client.get("/api/v1/gpu/status").status_code == 404


def test_poc_keeps_cross_site_browser_requests_blocked():
    with make_client() as client:
        response = client.post("/api/v1/knowledge/search", json={"query": "polyimide"},
                               headers={"Sec-Fetch-Site": "cross-site"})
        assert response.status_code == 403
        response = client.post("/api/v1/knowledge/search", json={"query": ""},
                               headers={"Sec-Fetch-Site": "same-origin"})
        assert response.status_code == 422


def test_poc_keeps_configured_development_origin_available():
    with make_client() as client:
        response = client.options("/api/v1/knowledge/search", headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "POST",
        })
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_poc_exposes_filter_endpoints_without_unrelated_database_modules():
    with make_client() as client:
        paths = client.get("/openapi.json").json()["paths"]
        assert {path for path in paths if "/database-browser/" in path} == {
            "/api/v1/database-browser/property-filter/options",
            "/api/v1/database-browser/property-filter/histogram",
            "/api/v1/database-browser/property-filter/search",
            "/api/v1/database-browser/property-filter/observations",
        }
        assert client.post("/api/v1/database-browser/property-filter/search", json={"filters": []}).status_code == 422
        assert client.get("/api/v1/database-browser/property-filter/histogram").status_code == 422
        assert client.post("/api/v1/database-browser/property-filter/search", json={"filters": []},
                           headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
