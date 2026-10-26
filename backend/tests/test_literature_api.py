"""Published local literature, frozen identities, and metadata-only recordings."""
from contextlib import contextmanager
import json
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest
from literature_test_support import install_literature_test_projection

from app.auth.context import Identity, user_context
from app.config import Settings
from app.models import KnowledgeSearchRequest
from app.routers.knowledge import router, _literature_connection
from app.routers.browsing_recording import router as recording_router
from app.services.browsing_recording import BrowsingRecordingStore
from app.services.knowledge_poc_summary import build_summary_evidence
from app.services.literature_repository import literature_search_groups
from app.services.literature_preflight import check_literature_readiness
from app.postgres_database import postgres_connection, PostgresUnavailableError
from test_auth_isolation import auth_database
from test_private_http_support import authenticated_client


def test_literature_doi_precedes_boolean_parser_and_has_no_iupac_expansion():
    doi = "10.1002/(sici)1099-0518(199610)34:14<2867::aid-pola4>3.0.co;2-o"
    assert literature_search_groups(KnowledgeSearchRequest(query="https://doi.org/" + doi,
           data_source="extracted_literature"))[0] == doi
    assert literature_search_groups(KnowledgeSearchRequest(query=doi, groups=[{"terms": [doi]}],
           data_source="extracted_literature"))[0] == doi
    assert literature_search_groups(KnowledgeSearchRequest(query="benzophenone/aminobenzoate",
           data_source="extracted_literature"))[1] == [["benzophenone/aminobenzoate"]]


@pytest.mark.parametrize("cause,status,code", [
    (psycopg.errors.QueryCanceled, 504, "LITERATURE_QUERY_TIMEOUT"),
    (psycopg.errors.LockNotAvailable, 504, "LITERATURE_QUERY_TIMEOUT"),
    (psycopg.OperationalError, 503, "LITERATURE_DATABASE_UNAVAILABLE"),
])
def test_wrapped_database_errors_preserve_literature_status(cause, status, code):
    app = FastAPI()
    app.state.settings = Settings(literature_enabled=True)
    @contextmanager
    def unavailable(_dsn):
        raise PostgresUnavailableError("private transport detail") from cause("private database detail")
        yield  # pragma: no cover
    app.state.postgres_connection_factory = unavailable
    with pytest.raises(HTTPException) as captured:
        with _literature_connection(app):
            pass
    assert captured.value.status_code == status
    assert captured.value.detail["code"] == code
    assert "private" not in str(captured.value.detail)


def test_real_wrapped_statement_timeout_returns_504(literature_schema):
    app = FastAPI()
    app.state.settings = Settings(app_postgres_dsn=literature_schema, literature_enabled=True)
    app.state.postgres_connection_factory = postgres_connection
    with pytest.raises(HTTPException) as captured:
        with _literature_connection(app) as connection:
            connection.execute("SET LOCAL statement_timeout = '10ms'")
            connection.execute("SELECT pg_sleep(0.1)")
    assert captured.value.status_code == 504
    assert captured.value.detail["code"] == "LITERATURE_QUERY_TIMEOUT"
    assert isinstance(captured.value.__cause__, PostgresUnavailableError)
    assert captured.value.__cause__.__cause__.sqlstate == "57014"


@pytest.fixture(scope="session")
def literature_schema(postgres_test_dsn):
    # The shared scientific fixture intentionally stops before the explicit
    # identity cutover. Test this additive repository DDL in its disposable DB;
    # migration/identity sequencing has separate deployment-contract coverage.
    with psycopg.connect(postgres_test_dsn) as connection:
        install_literature_test_projection(postgres_test_dsn)
        connection.execute("GRANT USAGE ON SCHEMA knowledge TO nexpoly_api")
        connection.execute("GRANT SELECT ON knowledge.documents TO nexpoly_api")
    return postgres_test_dsn


@pytest.fixture
def literature_client(literature_schema, monkeypatch):
    postgres_test_dsn = literature_schema
    connection = psycopg.connect(postgres_test_dsn, row_factory=dict_row)
    generation, replacement = str(uuid4()), str(uuid4())
    doc, snap, new_snap = "literature:fixture", "snapshot:synthetic-initial", "snapshot:replacement"
    capabilities = {"has_properties": True, "has_processes": True, "has_original": False,
                    "has_fulltext": False, "has_pdf": False}
    try:
        connection.execute("UPDATE knowledge.literature_generations SET is_active=false WHERE is_active")
        for gen in (generation, replacement):
            connection.execute("INSERT INTO knowledge.literature_generations(generation_id,status,normalizer_version) VALUES(%s,'building','literature-v1')", (gen,))
        for snapshot, value in ((snap, 264), (new_snap, 999)):
            connection.execute("""INSERT INTO knowledge.literature_snapshots(snapshot_uid,document_uid,source_paper_id,
                source_extract_id,normalizer_version,content_sha256,raw_json,normalized_json,assets)
                VALUES(%s,%s,'900001','900000000000000001','literature-v1',%s,%s,%s,%s)""",
                (snapshot, doc, "a" * 64, Jsonb({"private_raw": "not-for-model"}),
                 Jsonb({"samples": [{"sample_uid": "sample:test", "name": "PI-1", "properties": [{"value": {"single_value": value, "unit": "°C"}}]}]}),
                 Jsonb([{"asset_uid": "file:test", "role": "pdf", "status": "unverified", "bucket": "private-bucket", "object_key": "private-key"}])))
        for gen, snapshot, title, year in ((generation, snap, "Polyimide evidence", 2015), (replacement, new_snap, "Replacement evidence", 2025)):
            connection.execute("""INSERT INTO knowledge.literature_catalog(generation_id,document_uid,doi,snapshot_uid,
                title,authors,journal,publication_year,keywords,keywords_text,abstract,capabilities,counts)
                VALUES(%s,%s,'10.5555/synthetic-api-001',%s,%s,%s,'Journal',%s,%s,'thermal stability','A metadata abstract',%s,%s)""",
                (gen, doc, snapshot, title, Jsonb(["Author"]), year, Jsonb(["thermal stability"]), Jsonb(capabilities),
                 Jsonb({"samples": 1, "properties": 1, "processes": 1, "materials": 2})))
            connection.execute("UPDATE knowledge.literature_generations SET status='published',published_at=now(),is_active=%s WHERE generation_id=%s", (gen == generation, gen))
        connection.execute("""INSERT INTO knowledge.documents(knowledge_id,source_file,source_row_number,title_en,abstract)
            VALUES(991991,'legacy.xlsx',1,'Polyimide legacy','A legacy abstract') ON CONFLICT(knowledge_id) DO NOTHING""")
        app = FastAPI()
        app.state.settings = Settings(app_postgres_dsn=postgres_test_dsn, literature_enabled=True)
        app.state.browsing_recording = BrowsingRecordingStore(app.state.settings)
        @contextmanager
        def connection_factory(_dsn):
            try:
                with connection.transaction():
                    connection.execute("SET LOCAL ROLE nexpoly_api")
                    yield connection
            finally:
                connection.execute("RESET ROLE")
        app.state.postgres_connection_factory = connection_factory
        app.include_router(router)
        app.include_router(recording_router)
        @app.middleware("http")
        async def actor(request, call_next):
            user = request.headers.get("x-test-user", "00000000-0000-0000-0000-000000000123")
            with user_context(Identity(user)):
                return await call_next(request)
        with TestClient(app) as client:
            yield client, app, connection, generation, replacement, doc, snap
    finally:
        connection.rollback()
        connection.close()


def search(client, **kwargs):
    return client.post("/api/v1/knowledge/search", json={"query": "polyimide", "data_source": "extracted_literature", **kwargs})


def test_source_status_default_legacy_and_literature_contract(literature_client):
    client, app, _, generation, _, doc, snap = literature_client
    sources = client.get("/api/v1/knowledge/sources").json()["sources"]
    assert sources[1]["ready"] and sources[1]["generation_id"] == generation
    assert sources[1]["published_at"]
    old = client.post("/api/v1/knowledge/search", json={"query": "Polyimide legacy"}).json()
    assert old["data_source"] == "legacy"
    assert old["results"][0]["document_uid"] == "legacy:991991"
    result = search(client).json()
    assert result["generation_id"] == generation and result["total"] == 1
    article = result["results"][0]
    assert (article["document_uid"], article["snapshot_uid"], article["knowledge_id"]) == (doc, snap, None)
    assert article["matched_fields"] == ["Title"]
    assert "samples" not in article and "normalized_json" not in article
    app.state.settings.literature_enabled = False
    assert search(client).status_code == 503
    assert client.post("/api/v1/knowledge/search", json={"query": "Polyimide legacy"}).status_code == 200


def test_filter_validation_doi_exact_and_no_browse(literature_client):
    client, *_ = literature_client
    assert search(client, query="   ").status_code == 422
    assert search(client, data_source="all").status_code == 422
    assert search(client, filters={"min_tg": 200}).status_code == 422
    assert search(client, filters={"year_from": 2020, "year_to": 2010}).status_code == 422
    assert search(client, filters={"year_from": 2020}).json()["total"] == 0
    assert search(client, filters={"has_original": True}).json()["total"] == 0
    assert search(client, query="https://doi.org/10.5555/SYNTHETIC-API-001").json()["results"][0]["matched_fields"] == ["DOI"]
    assert search(client, generation_id="not-a-uuid").status_code == 422
    assert search(client, generation_id="not-a-uuid").json()["detail"]["code"] == "LITERATURE_INVALID_GENERATION"
    assert search(client, generation_id=str(uuid4())).status_code == 404


def test_detail_and_paging_pin_generation_and_hide_asset_locations(literature_client):
    client, _, connection, generation, replacement, doc, snap = literature_client
    initial = search(client).json()
    connection.execute("UPDATE knowledge.literature_generations SET is_active=false WHERE is_active")
    connection.execute("UPDATE knowledge.literature_generations SET is_active=true WHERE generation_id=%s", (replacement,))
    assert search(client, generation_id=initial["generation_id"]).json()["results"][0]["snapshot_uid"] == snap
    assert search(client, query="Replacement").json()["generation_id"] == replacement
    response = client.get(f"/api/v1/knowledge/literature/{doc}", params={"generation_id": generation, "snapshot_uid": snap})
    assert response.status_code == 200
    detail = response.json()
    assert detail["samples"][0]["properties"][0]["value"]["single_value"] == 264
    assert detail["extraction"]["source_extract_id"] == "900000000000000001"
    assert "private-key" not in response.text and "private-bucket" not in response.text
    assert "raw_json" not in detail
    assert client.get(f"/api/v1/knowledge/literature/{doc}", params={"generation_id": replacement, "snapshot_uid": snap}).status_code == 404
    assert client.get(f"/api/v1/knowledge/literature/{doc}").status_code == 422


def test_recording_is_owned_frozen_and_metadata_only(literature_client):
    client, _, _, generation, _, doc, snap = literature_client
    assert client.post("/api/v1/knowledge/recordings", json={"recording_id": "one"}).status_code == 200
    result = search(client, recording_id="one").json()
    observation = {"search_id": result["search_id"], "recording_id": "one", "document_uid": doc,
                   "generation_id": generation, "snapshot_uid": snap, "source": "extracted_tab"}
    assert client.post("/api/v1/knowledge/observations", json=observation).status_code == 200
    assert client.post("/api/v1/knowledge/observations", json={**observation, "snapshot_uid": "other"}).status_code == 409
    assert client.post("/api/v1/knowledge/observations", json={**observation, "document_uid": "literature:other"}).status_code == 404
    assert client.post("/api/v1/knowledge/observations", json=observation,
                       headers={"x-test-user": "00000000-0000-0000-0000-000000000124"}).status_code == 404
    frozen = client.post("/api/v1/knowledge/recordings/one/stop").json()
    assert client.post("/api/v1/knowledge/observations", json=observation).status_code == 409
    evidence = build_summary_evidence(frozen["events"])
    article = evidence["articles"][0]
    assert article["doi"] == "10.5555/synthetic-api-001" and article["abstract"] == "A metadata abstract"
    encoded = json.dumps(evidence)
    assert all(value not in encoded for value in ("single_value", "properties", "private_raw", "private-key"))
    # Defense in depth: even a persisted malformed reaction event cannot send
    # literature experiments to the model.
    poisoned = dict(frozen["events"][1], event="article.reaction_viewed", reaction_info={"experiment": "SECRET"})
    assert "SECRET" not in json.dumps(build_summary_evidence([poisoned]))
    assert "keywords" not in frozen["events"][1]["article"]
    assert "keywords" not in article


def test_summary_preserves_numeric_legacy_sort_and_safe_mixed_identities():
    events = [{"event": "article.opened", "search_id": "s", "source": "result_card",
               "article": {"knowledge_id": number, "title_en": str(number), "abstract": "Legacy"}}
              for number in (10, 2)]
    evidence = build_summary_evidence(events)
    assert [a["knowledge_id"] for a in evidence["articles"]] == [2, 10]


def test_optional_preflight_rejects_writable_api_role_and_unknown_format(literature_client):
    client, _, connection, generation, *_ = literature_client
    assert check_literature_readiness(None, enabled=False)["reason"] == "LITERATURE_DISABLED"
    assert check_literature_readiness(connection)["reason"] == "LITERATURE_API_PRIVILEGES_INVALID"
    assert check_literature_readiness(connection, role_override="nexpoly_api")["ready"]
    connection.execute("GRANT UPDATE ON knowledge.literature_catalog TO nexpoly_api")
    assert check_literature_readiness(connection, role_override="nexpoly_api")["reason"] == "LITERATURE_API_PRIVILEGES_INVALID"
    connection.execute("REVOKE UPDATE ON knowledge.literature_catalog FROM nexpoly_api")
    future = str(uuid4())
    connection.execute("UPDATE knowledge.literature_generations SET is_active=false WHERE is_active")
    connection.execute("INSERT INTO knowledge.literature_generations(generation_id,status,is_active,normalizer_version,published_at) VALUES(%s,'published',true,'future-version',now())", (future,))
    assert check_literature_readiness(connection, role_override="nexpoly_api")["reason"] == "LITERATURE_FORMAT_UNSUPPORTED"
    status = client.get("/api/v1/knowledge/sources").json()["sources"]
    assert status[0]["ready"] and not status[1]["ready"]
    assert status[1]["reason"] == "LITERATURE_FORMAT_UNSUPPORTED"
    assert search(client).json()["detail"]["code"] == "LITERATURE_FORMAT_UNSUPPORTED"
    assert client.post("/api/v1/knowledge/search", json={"query": "Polyimide legacy"}).status_code == 200


def test_unpublished_generation_is_not_searchable(literature_client):
    client, _, connection, *_ = literature_client
    connection.execute("UPDATE knowledge.literature_generations SET is_active=false WHERE is_active")
    assert client.get("/api/v1/knowledge/sources").json()["sources"][1]["reason"] == "LITERATURE_NOT_PUBLISHED"
    assert search(client).json()["detail"]["code"] == "LITERATURE_NOT_PUBLISHED"


def test_preflight_rejects_direct_login_write_grants(literature_client):
    _, _, connection, *_ = literature_client
    role = 'literature_api_fixture_' + uuid4().hex[:12]
    connection.execute(sql.SQL('CREATE ROLE {} LOGIN IN ROLE nexpoly_api').format(sql.Identifier(role)))
    connection.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
    assert check_literature_readiness(connection)["ready"]
    connection.execute('RESET ROLE')
    connection.execute(sql.SQL('GRANT INSERT ON knowledge.literature_catalog TO {}').format(sql.Identifier(role)))
    connection.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
    assert check_literature_readiness(connection)["reason"] == "LITERATURE_API_PRIVILEGES_INVALID"
    connection.execute('RESET ROLE')


def test_real_auth_and_legacy_survive_optional_literature_unready(auth_database):
    app = FastAPI()
    app.state.settings = Settings(app_postgres_dsn=auth_database["api"], literature_enabled=False)
    app.state.postgres_connection_factory = postgres_connection
    app.state.browsing_recording = BrowsingRecordingStore(app.state.settings)
    app.include_router(router)
    app.include_router(recording_router)
    with psycopg.connect(auth_database["admin"]) as connection:
        connection.execute("""INSERT INTO knowledge.documents(knowledge_id,source_file,source_row_number,title_en,abstract)
            VALUES(990190,'auth-legacy.xlsx',1,'Authenticated polyimide','Existing knowledge remains available')""")
    with authenticated_client(app, auth_database) as client:
        result = client.post("/api/v1/knowledge/search", json={"query": "Authenticated polyimide"})
        assert result.status_code == 200
        assert result.json()["results"][0]["knowledge_id"] == 990190
        assert client.get("/api/v1/knowledge/sources").json()["sources"][1]["reason"] == "LITERATURE_DISABLED"
        app.state.settings.literature_enabled = True
        states = client.get("/api/v1/knowledge/sources").json()["sources"]
        assert states[0]["ready"] and not states[1]["ready"]
        assert states[1]["reason"] == "LITERATURE_SCHEMA_NOT_READY"
        assert client.post("/api/v1/knowledge/search", json={"query": "Authenticated polyimide"}).status_code == 200
        assert search(client).status_code == 503
    with TestClient(app) as anonymous:
        assert anonymous.post("/api/v1/knowledge/search", json={"query": "Authenticated polyimide"}).status_code == 401
