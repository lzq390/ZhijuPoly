"""Exercise the public MCP wire protocol against a disposable PostgreSQL."""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager, ExitStack
from datetime import datetime, timedelta, timezone
import asyncio
import hashlib
import json
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
import psycopg
from psycopg import sql
import pytest
import uvicorn

from app.property_filter_mcp import create_app
from app.property_filter_mcp_contract import TOOL_NAMES
from app.property_filter_mcp_security import MCPConfig
from app.services.property_filter_catalog import rebuild_property_filter_catalog


@asynccontextmanager
async def client_for(server, *, token=None):
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + (token or server.token)}) as http:
        @asynccontextmanager
        async def transport():
            async with streamable_http_client(server.url, http_client=http) as (read, write):
                yield read, write
        async with Client(transport(), read_timeout_seconds=10) as client:
            yield client


@pytest.fixture
def mcp_factory(seeded_db, tmp_path):
    stack = ExitStack()

    @contextmanager
    def running(**overrides):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        token = "test-token-" + str(port)
        clients_file = tmp_path / f"clients-{port}.json"
        credential = {"client_id": "test-client", "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                      "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                      "tools": sorted(TOOL_NAMES)}

        def set_clients(clients):
            clients_file.write_text(json.dumps({"clients": clients}))
            clients_file.chmod(0o600)

        set_clients([credential])
        config = MCPConfig(dsn=seeded_db.reader_dsn, clients_file=clients_file, port=port,
                           allowed_hosts=(f"127.0.0.1:{port}",), **overrides)
        application = create_app(config)
        service = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port,
                                              log_level="error", access_log=False))
        thread = threading.Thread(target=service.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 8
        while not service.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        try:
            assert service.started, "MCP server failed its startup checks"
            yield SimpleNamespace(url=f"http://127.0.0.1:{port}/mcp", token=token, config=config,
                                  app=application, credential=credential, set_clients=set_clients)
        finally:
            service.should_exit = True
            thread.join(timeout=6)
            sock.close()
            assert not thread.is_alive(), "MCP did not stop after the test"

    yield lambda **overrides: stack.enter_context(running(**overrides))
    stack.close()


async def call(client, name, arguments=None):
    result = await client.call_tool(name, arguments or {})
    return result, result.structured_content


async def catalog(client):
    result, body = await call(client, "list_filter_properties")
    assert not result.is_error, body
    return body


def search_args(revision, **extra):
    return {"dataset_revision": revision,
            "filters": [{"option_key": "std:tg:C", "min_value": 150, "max_value": 260}], **extra}


@pytest.mark.anyio
async def test_three_tools_real_protocol_and_evidence(mcp_factory):
    server = mcp_factory()
    async with client_for(server) as client:
        listed = await client.list_tools()
        assert {tool.name for tool in listed.tools} == TOOL_NAMES
        assert all(tool.input_schema and tool.output_schema for tool in listed.tools)
        properties = await catalog(client)
        assert properties["total_measurement_count"] == 13
        assert len(properties["options"]) == 3
        assert all("histogram" not in option for option in properties["options"])
        assert "PRIVATE_OPERATION_NOTE" not in json.dumps(properties)
        revision = properties["dataset_revision"]
        result, body = await call(client, "search_polymers", search_args(revision))
        assert not result.is_error, body
        assert body["matched_material_count"] == 2
        material = next(row for row in body["results"] if row["polymer_name"].startswith("POC-A"))
        evidence = material["evidence"][0]
        assert evidence["matching_measurement_count"] == 2
        assert evidence["truncated"] is True
        assert len(evidence["measurements"]) == 1
        assert evidence["unit"] == "C"
        assert evidence["measurements"][0]["value_origin"] == "synthetic_poc"
        assert revision in evidence["measurements"][0]["record_ref"]
        result, histogram = await call(client, "get_property_histogram", {
            "dataset_revision": revision, "option_key": "std:tg:C"})
        assert not result.is_error, histogram
        assert histogram["histogram"]["total_count"] == 5
        assert histogram["distribution_scope"] == "entire_dataset_for_option"


@pytest.mark.anyio
async def test_and_raw_bounds_zero_results_and_pagination(mcp_factory):
    async with client_for(mcp_factory()) as client:
        properties = await catalog(client)
        revision = properties["dataset_revision"]
        raw_key = next(option["option_key"] for option in properties["options"] if option["filter_type"] == "raw")
        result, body = await call(client, "search_polymers", {
            "dataset_revision": revision,
            "filters": [{"option_key": "std:tg:C", "min_value": 180, "max_value": 240},
                        {"option_key": raw_key, "min_value": 10}],
            "measurement_limit_per_filter": 3})
        assert not result.is_error, body
        assert body["matched_material_count"] == 1
        assert len(body["results"][0]["evidence"]) == 2
        assert body["results"][0]["evidence"][0]["truncated"] is False
        _, page1 = await call(client, "search_polymers", search_args(revision, page_size=1))
        _, page2 = await call(client, "search_polymers", search_args(revision, page_size=1, page=2))
        assert page1["has_more"] and not page2["has_more"]
        assert page1["results"][0]["material_ref"] != page2["results"][0]["material_ref"]
        _, empty = await call(client, "search_polymers", search_args(revision, q="no-such-material"))
        assert empty["ok"] and empty["matched_material_count"] == 0 and empty["results"] == []
        _, beyond = await call(client, "search_polymers", search_args(revision, page=999))
        assert beyond["matched_material_count"] == 2 and beyond["results"] == []


@pytest.mark.anyio
async def test_unknown_options_bad_bounds_and_revision(mcp_factory):
    async with client_for(mcp_factory()) as client:
        revision = (await catalog(client))["dataset_revision"]
        invalid = [
            {"filters": [{"option_key": "std:tg:C"}]},
            {"filters": [{"option_key": "std:tg:C", "min_value": 300, "max_value": 200}]},
            {"filters": [{"option_key": "std:tg:C", "min_value": 100}] * 9},
            {"page_size": 26}, {"measurement_limit_per_filter": 4}, {"sql": "SELECT 1"},
        ]
        for override in invalid:
            result, body = await call(client, "search_polymers", search_args(revision, **override))
            assert result.is_error and not body["ok"], body
        result, _ = await call(client, "list_filter_properties", {"page": 2})
        assert result.is_error
        result, body = await call(client, "search_polymers", search_args("old-revision"))
        assert result.is_error and body["error"]["code"] == "DATASET_REVISION_CHANGED"
        result, body = await call(client, "get_property_histogram", {
            "dataset_revision": revision, "option_key": "missing-option"})
        assert result.is_error and "OPTION" in body["error"]["code"]


@pytest.mark.anyio
async def test_version_change_and_invalid_snapshot_do_not_rebuild(mcp_factory, seeded_db):
    async with client_for(mcp_factory()) as client:
        revision = (await catalog(client))["dataset_revision"]
        seeded_db.refresh_catalog()
        for tool, args in (("search_polymers", search_args(revision)),
                           ("get_property_histogram", {"dataset_revision": revision, "option_key": "std:tg:C"}),
                           ("list_filter_properties", {"dataset_revision": revision, "page": 2})):
            result, body = await call(client, tool, args)
            assert result.is_error and body["error"]["code"] == "DATASET_REVISION_CHANGED"
        with seeded_db.connect() as connection:
            connection.execute("DELETE FROM governance.property_filter_options_snapshots")
        result, body = await call(client, "list_filter_properties")
        assert result.is_error and "CATALOG" in body["error"]["code"]
        with seeded_db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) AS n FROM governance.property_filter_options_snapshots").fetchone()["n"] == 0


@pytest.mark.anyio
async def test_units_and_canonical_grouping_are_preserved(mcp_factory, seeded_db):
    with seeded_db.connect() as connection:
        connection.execute("""INSERT INTO core.polymer_property_filter_records
            (filter_record_id,source_file,source_row_number,polymer_name,smiles,canonical_smiles,
             property_name,property_value,property_value_num,property_key,canonical_value,canonical_unit)
            VALUES (1,'fixture',1,'Unit-K','unit-K','unit-K','Tg','200',200,'tg',200,'K'),
                   (2,'fixture',2,'Unit-empty','unit-empty','unit-empty','Tg','200',200,'tg',200,NULL)""")
        connection.execute("""UPDATE core.polymer_property_filter_records SET canonical_smiles='same-structure',
            smiles=CASE WHEN property_key='tg' THEN 'raw-a' ELSE 'raw-b' END WHERE polymer_name LIKE 'POC-A%'""")
    seeded_db.refresh_catalog()
    async with client_for(mcp_factory()) as client:
        revision = (await catalog(client))["dataset_revision"]
        _, celsius = await call(client, "search_polymers", search_args(revision))
        assert celsius["matched_material_count"] == 2
        _, empty = await call(client, "search_polymers", {
            "dataset_revision": revision, "filters": [{"option_key": "std:tg:", "min_value": 150}]})
        assert empty["matched_material_count"] == 1 and empty["results"][0]["polymer_name"] == "Unit-empty"
        _, combined = await call(client, "search_polymers", {
            "dataset_revision": revision, "filters": [
                {"option_key": "std:tg:C", "min_value": 180, "max_value": 190},
                {"option_key": "std:tensile_strength:MPa", "min_value": 60, "max_value": 60}]})
        assert combined["matched_material_count"] == 1
        assert combined["results"][0]["canonical_smiles"] == "same-structure"
        _, keyword = await call(client, "search_polymers", {
            "dataset_revision": revision, "q": "raw-a", "filters": [
                {"option_key": "std:tg:C", "min_value": 180},
                {"option_key": "std:tensile_strength:MPa", "min_value": 60}]})
        assert keyword["matched_material_count"] == 0


@pytest.mark.anyio
async def test_many_measurements_are_sampled_without_changing_counts(mcp_factory, seeded_db):
    with seeded_db.connect() as connection:
        connection.execute("""INSERT INTO core.polymer_property_filter_records
            (filter_record_id,source_file,source_row_number,polymer_name,smiles,property_name,property_value,
             property_value_num,property_key,canonical_value,canonical_unit,value_origin,reliable_score)
            SELECT n,'fixture',n,'POC-A 演示材料','*CC*','Tg','185',185,'tg',185,'C','observed',0.99
            FROM generate_series(1,2000) n""")
    seeded_db.refresh_catalog()
    async with client_for(mcp_factory()) as client:
        revision = (await catalog(client))["dataset_revision"]
        result, body = await call(client, "search_polymers", search_args(revision, measurement_limit_per_filter=3))
        assert not result.is_error and body["matched_material_count"] == 2
        evidence = next(row for row in body["results"] if row["smiles"] == "*CC*")["evidence"][0]
        assert evidence["matching_measurement_count"] == 2002
        assert len(evidence["measurements"]) == 3 and evidence["truncated"]
        assert all(row["value_origin"] == "observed" for row in evidence["measurements"])
        assert len(result.model_dump_json().encode()) < 256 * 1024


@pytest.mark.anyio
async def test_response_budget_returns_explicit_error(mcp_factory, seeded_db):
    with seeded_db.connect() as connection:
        connection.execute("UPDATE core.polymer_property_filter_records SET soft_quality_flags=%s WHERE property_key='tg'", ("x" * 270000,))
    async with client_for(mcp_factory()) as client:
        revision = (await catalog(client))["dataset_revision"]
        result, body = await call(client, "search_polymers", search_args(revision))
        assert result.is_error and body["error"]["code"] == "RESPONSE_TOO_LARGE"
        assert len(result.model_dump_json().encode()) < 256 * 1024


@pytest.mark.anyio
async def test_scope_rate_limit_and_revocation(mcp_factory):
    server = mcp_factory(rate_limit=2)
    async with client_for(server) as client:
        await catalog(client)
        await catalog(client)
        result, body = await call(client, "list_filter_properties")
        assert result.is_error and body["error"]["code"] == "RATE_LIMITED"
    limited = mcp_factory()
    limited.set_clients([{**limited.credential, "tools": ["list_filter_properties"]}])
    async with client_for(limited) as client:
        revision = (await catalog(client))["dataset_revision"]
        result, body = await call(client, "search_polymers", search_args(revision))
        assert result.is_error and "PERMISSION" in body["error"]["code"]
    with httpx.Client() as http:
        headers = {"Authorization": "Bearer " + server.token}
        assert http.post(server.url, headers=headers, json={}).status_code != 401
        server.set_clients([])
        assert http.post(server.url, headers=headers, json={}).status_code == 401
        limited.set_clients([{**limited.credential, "expires_at": "2000-01-01T00:00:00Z"}])
        assert http.post(limited.url, headers={"Authorization": "Bearer " + limited.token}, json={}).status_code == 401


def test_request_security_boundaries_and_health(mcp_factory):
    server = mcp_factory()
    with httpx.Client() as http:
        assert http.get(server.url.replace("/mcp", "/healthz")).status_code == 200
        assert http.post(server.url, json={}).status_code == 401
        headers = {"Authorization": "Bearer " + server.token}
        assert http.post(server.url, headers={**headers, "Origin": "https://untrusted.example"}, json={}).status_code == 403
        assert http.post(server.url, headers={**headers, "Host": "untrusted.example"}, json={}).status_code in {400, 421}
        assert http.post(server.url + "?token=forbidden", headers=headers, json={}).status_code == 400
        assert http.post(server.url, headers={**headers, "Content-Type": "application/json"}, content="x" * 32769).status_code == 413


def test_reader_cannot_write_or_read_unrelated_data(seeded_db):
    for statement in ("INSERT INTO core.polymer_property_filter_records (filter_record_id) VALUES (-1)",
                      "DELETE FROM core.polymer_property_filter_records", "CREATE TABLE core.unauthorized (id int)",
                      "SELECT * FROM core.private_test_data"):
        with psycopg.connect(seeded_db.reader_dsn) as connection:
            with pytest.raises((psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)):
                connection.execute(statement)
    # ACLs must also prohibit persistent writes if read-only transaction defaults are changed.
    with psycopg.connect(seeded_db.reader_dsn) as connection:
        connection.execute("SET TRANSACTION READ WRITE")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("DELETE FROM core.polymer_property_filter_records")


@pytest.mark.anyio
async def test_concurrency_and_total_timeout_cancel_a_real_locked_query(mcp_factory, seeded_db):
    server = mcp_factory(max_concurrency=1, tool_timeout_seconds=0.3, lock_timeout_ms=1500)
    async with client_for(server) as client:
        with seeded_db.connect() as blocker:
            blocker.execute("LOCK TABLE core.polymer_property_filter_records IN ACCESS EXCLUSIVE MODE")
            first = asyncio.create_task(call(client, "list_filter_properties"))
            deadline = time.monotonic() + 2
            while not server.app.state.mcp_runtime.limiter.active.get("test-client"):
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
            result, body = await call(client, "list_filter_properties")
            assert result.is_error and body["error"]["code"] == "CONCURRENCY_LIMITED"
            result, body = await first
            assert result.is_error and body["error"]["code"] == "QUERY_TIMEOUT"
            deadline = time.monotonic() + 3
            while server.app.state.mcp_runtime.limiter.active.get("test-client"):
                assert time.monotonic() < deadline, "Timed-out SQL worker did not release its slot"
                await asyncio.sleep(0.01)
            # PostgreSQL must no longer have the timed-out call waiting on our lock.
            active = blocker.execute("""SELECT COUNT(*) AS n FROM pg_stat_activity
                WHERE application_name='nexpoly_property_filter_mcp' AND state='active'""").fetchone()["n"]
            assert active == 0
        assert (await catalog(client))["total_measurement_count"] == 13


@pytest.mark.anyio
async def test_import_truncate_cannot_mix_catalog_revision_and_rows(mcp_factory, seeded_db):
    server = mcp_factory()
    async with client_for(server) as client:
        old_revision = (await catalog(client))["dataset_revision"]
        with seeded_db.connect() as importer:
            importer.execute("LOCK TABLE core.polymer_property_filter_records IN ACCESS EXCLUSIVE MODE")
            pending = asyncio.create_task(call(client, "list_filter_properties"))
            deadline = time.monotonic() + 2
            while not server.app.state.mcp_runtime.limiter.active.get("test-client"):
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            assert not pending.done()
            importer.execute("CREATE TEMP TABLE replacement AS SELECT * FROM core.polymer_property_filter_records WHERE filter_record_id=9000001")
            importer.execute("TRUNCATE core.polymer_property_filter_records")
            importer.execute("INSERT INTO core.polymer_property_filter_records SELECT * FROM replacement")
            batch = importer.execute("""INSERT INTO governance.import_batches (dataset_key,status,row_count,finished_at)
                VALUES ('property_filter','completed',1,now()) RETURNING import_batch_id""").fetchone()["import_batch_id"]
            rebuild_property_filter_catalog(importer, import_batch_id=batch, source_sha256="b" * 64)
        result, body = await pending
        assert not result.is_error and body["dataset_revision"] != old_revision
        assert body["total_measurement_count"] == 1
        _, search = await call(client, "search_polymers", search_args(body["dataset_revision"]))
        assert search["total_measurement_count"] == search["matched_material_count"] == 1


def test_startup_rejects_overprivileged_reader(seeded_db, tmp_path):
    from app.property_filter_mcp import MCPRuntime
    credentials = tmp_path / "clients.json"
    credentials.write_text('{"clients":[]}')
    credentials.chmod(0o600)
    config = MCPConfig(dsn=seeded_db.reader_dsn, clients_file=credentials)
    with seeded_db.connect() as connection:
        connection.execute(sql.SQL("GRANT SELECT ON core.private_test_data TO {}").format(sql.Identifier(seeded_db.reader_role)))
    try:
        with pytest.raises(RuntimeError, match="SELECT only"):
            MCPRuntime(config).startup_check()
    finally:
        with seeded_db.connect() as connection:
            connection.execute(sql.SQL("REVOKE SELECT ON core.private_test_data FROM {}").format(sql.Identifier(seeded_db.reader_role)))


@pytest.mark.anyio
async def test_nonfinite_stored_evidence_is_a_data_error(mcp_factory, seeded_db):
    with seeded_db.connect() as connection:
        connection.execute("UPDATE core.polymer_property_filter_records SET reliable_score='NaN'::float8 WHERE property_key='tg'")
    async with client_for(mcp_factory()) as client:
        revision = (await catalog(client))["dataset_revision"]
        result, body = await call(client, "search_polymers", search_args(revision))
        assert result.is_error and body["error"]["code"] == "DATA_UNAVAILABLE"
