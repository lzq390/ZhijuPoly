"""Regression checks for independent MCP startup security boundaries."""
from __future__ import annotations

import json
import traceback

from psycopg import sql
import pytest
from starlette.testclient import TestClient

from app.property_filter_mcp import MCPRuntime, create_app
from app.property_filter_mcp_security import MCPConfig


def runtime_for(database, tmp_path):
    clients = tmp_path / "clients.json"
    clients.write_text('{"clients":[]}')
    clients.chmod(0o600)
    return MCPRuntime(MCPConfig(dsn=database.reader_dsn, clients_file=clients))


def test_startup_rejects_unrelated_column_read(seeded_db, tmp_path):
    role = sql.Identifier(seeded_db.reader_role)
    with seeded_db.connect() as connection:
        connection.execute(sql.SQL("GRANT SELECT (secret) ON core.private_test_data TO {}").format(role))
        privileges = connection.execute("""
            SELECT has_table_privilege(%s, 'core.private_test_data', 'SELECT') AS table_read,
                   has_any_column_privilege(%s, 'core.private_test_data', 'SELECT') AS column_read
        """, (seeded_db.reader_role, seeded_db.reader_role)).fetchone()
        assert privileges == {"table_read": False, "column_read": True}
    try:
        with pytest.raises(RuntimeError, match="SELECT only"):
            runtime_for(seeded_db, tmp_path).startup_check()
    finally:
        with seeded_db.connect() as connection:
            connection.execute(sql.SQL("REVOKE SELECT (secret) ON core.private_test_data FROM {}").format(role))
    runtime_for(seeded_db, tmp_path).startup_check()


@pytest.mark.parametrize("privilege", ["SELECT", "USAGE", "UPDATE"])
def test_startup_rejects_sequence_grants(seeded_db, tmp_path, privilege):
    role = sql.Identifier(seeded_db.reader_role)
    with seeded_db.connect() as connection:
        connection.execute("CREATE SEQUENCE core.mcp_review_sequence")
        connection.execute(sql.SQL("GRANT {} ON SEQUENCE core.mcp_review_sequence TO {}").format(sql.SQL(privilege), role))
    try:
        with pytest.raises(RuntimeError, match="sequence privileges"):
            runtime_for(seeded_db, tmp_path).startup_check()
    finally:
        with seeded_db.connect() as connection:
            connection.execute("DROP SEQUENCE core.mcp_review_sequence")
    runtime_for(seeded_db, tmp_path).startup_check()


@pytest.mark.parametrize("kind", ["TABLE", "SEQUENCE"])
def test_startup_rejects_owner_even_without_acl_privileges(seeded_db, tmp_path, kind):
    # Ownership still permits ALTER/DROP after its ordinary ACLs are revoked.
    role = sql.Identifier(seeded_db.reader_role)
    name = "core.mcp_review_owned"
    with seeded_db.connect() as connection:
        connection.execute(sql.SQL("CREATE {} {} {}").format(
            sql.SQL(kind), sql.Identifier(*name.split(".")), sql.SQL("(id int)" if kind == "TABLE" else "")))
        connection.execute(sql.SQL("ALTER {} {} OWNER TO {}").format(
            sql.SQL(kind), sql.Identifier(*name.split(".")), role))
        connection.execute(sql.SQL("REVOKE ALL ON {} {} FROM {}").format(
            sql.SQL(kind), sql.Identifier(*name.split(".")), role))
        if kind == "TABLE":
            assert not connection.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS allowed",
                (seeded_db.reader_role, name),
            ).fetchone()["allowed"]
        else:
            assert not connection.execute(
                "SELECT has_sequence_privilege(%s, %s, 'SELECT,USAGE,UPDATE') AS allowed",
                (seeded_db.reader_role, name),
            ).fetchone()["allowed"]
    try:
        with pytest.raises(RuntimeError, match="SELECT only|sequence privileges"):
            runtime_for(seeded_db, tmp_path).startup_check()
    finally:
        with seeded_db.connect() as connection:
            connection.execute(sql.SQL("DROP {} {}").format(sql.SQL(kind), sql.Identifier(*name.split("."))))
    runtime_for(seeded_db, tmp_path).startup_check()


def test_startup_rejects_schema_owner_without_create(seeded_db, tmp_path):
    role = sql.Identifier(seeded_db.reader_role)
    with seeded_db.connect() as connection:
        connection.execute(sql.SQL("CREATE SCHEMA mcp_review_schema AUTHORIZATION {}").format(role))
        connection.execute(sql.SQL("REVOKE ALL ON SCHEMA mcp_review_schema FROM {}").format(role))
        assert not connection.execute(
            "SELECT has_schema_privilege(%s, 'mcp_review_schema', 'CREATE') AS allowed",
            (seeded_db.reader_role,),
        ).fetchone()["allowed"]
    try:
        with pytest.raises(RuntimeError, match="own schemas"):
            runtime_for(seeded_db, tmp_path).startup_check()
    finally:
        with seeded_db.connect() as connection:
            connection.execute("DROP SCHEMA mcp_review_schema")
    runtime_for(seeded_db, tmp_path).startup_check()


def test_startup_rejects_database_owner_without_create(seeded_db, tmp_path):
    role = sql.Identifier(seeded_db.reader_role)
    with seeded_db.connect() as connection:
        identity = connection.execute("SELECT current_database() AS name, current_user AS owner").fetchone()
        database = sql.Identifier(identity["name"])
        connection.execute(sql.SQL("ALTER DATABASE {} OWNER TO {}").format(database, role))
        connection.execute(sql.SQL("REVOKE CREATE ON DATABASE {} FROM {}").format(database, role))
        assert not connection.execute(
            "SELECT has_database_privilege(%s, current_database(), 'CREATE') AS allowed",
            (seeded_db.reader_role,),
        ).fetchone()["allowed"]
    try:
        with pytest.raises(RuntimeError, match="own the database"):
            runtime_for(seeded_db, tmp_path).startup_check()
    finally:
        with seeded_db.connect() as connection:
            connection.execute(sql.SQL("ALTER DATABASE {} OWNER TO {}").format(database, sql.Identifier(identity["owner"])))
            connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, role))
    runtime_for(seeded_db, tmp_path).startup_check()


def test_lifespan_does_not_log_malformed_private_credential(tmp_path, caplog):
    # A token accidentally entered instead of its hash must not appear in
    # Uvicorn's startup traceback through Pydantic's input_value diagnostics.
    secret = "synthetic-private-value-not-a-sha256"
    clients = tmp_path / "clients.json"
    clients.write_text(json.dumps({"clients": [{
        "client_id": "test", "token_sha256": secret,
        "expires_at": "2030-01-01T00:00:00Z", "tools": [],
    }]}))
    clients.chmod(0o600)
    application = create_app(MCPConfig(dsn="postgresql://unused@127.0.0.1/unused", clients_file=clients))
    with pytest.raises(RuntimeError, match="MCP startup checks failed") as error:
        with TestClient(application):
            pytest.fail("Malformed credentials must prevent startup")
    logged_traceback = "".join(traceback.format_exception(error.value))
    assert secret not in logged_traceback
    assert secret not in caplog.text
    assert not application.state.mcp_runtime.ready
