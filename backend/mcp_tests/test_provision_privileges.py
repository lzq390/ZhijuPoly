"""Provisioning must reject excess ACLs on the disposable test server only."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
import pytest


SOURCE = Path(__file__).resolve().parents[2] / "scripts/property_filter_mcp_dev.py"
SPEC = importlib.util.spec_from_file_location("mcp_provision_under_test", SOURCE)
dev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dev)


def test_provision_accepts_only_scoped_reader(seeded_db, monkeypatch):
    credentials = conninfo_to_dict(seeded_db.reader_dsn)
    monkeypatch.setattr(dev, "DATABASE", credentials["dbname"])
    monkeypatch.setattr(dev, "ROLE", seeded_db.reader_role)
    role = sql.Identifier(seeded_db.reader_role)
    with seeded_db.connect() as admin:
        admin.execute(sql.SQL("COMMENT ON ROLE {} IS {}").format(role, sql.Literal(dev.ROLE_MARKER)))
    try:
        with seeded_db.connect() as admin:
            admin.execute(dev.provision_sql(credentials["password"]))
        with psycopg.connect(seeded_db.reader_dsn) as reader:
            assert reader.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
    finally:
        with seeded_db.connect() as admin:
            admin.execute(sql.SQL("COMMENT ON ROLE {} IS NULL").format(role))


@pytest.mark.parametrize("grant,revoke", [
    ("SELECT (secret) ON core.private_test_data", "SELECT (secret) ON core.private_test_data"),
    ("UPDATE (source_file) ON core.polymer_property_filter_records",
     "UPDATE (source_file) ON core.polymer_property_filter_records"),
    ("SELECT ON SEQUENCE core.mcp_acl_test_sequence", "ALL ON SEQUENCE core.mcp_acl_test_sequence"),
    ("USAGE ON SEQUENCE core.mcp_acl_test_sequence", "ALL ON SEQUENCE core.mcp_acl_test_sequence"),
    ("UPDATE ON SEQUENCE core.mcp_acl_test_sequence", "ALL ON SEQUENCE core.mcp_acl_test_sequence"),
    ("CREATE ON DATABASE {database}", "CREATE ON DATABASE {database}"),
])
def test_provision_rolls_back_excess_privileges(seeded_db, monkeypatch, grant, revoke):
    database = conninfo_to_dict(seeded_db.admin_dsn)["dbname"]
    monkeypatch.setattr(dev, "DATABASE", database)
    monkeypatch.setattr(dev, "ROLE", seeded_db.reader_role)
    role = sql.Identifier(seeded_db.reader_role)
    substitutions = {"database": sql.Identifier(database)}
    with seeded_db.connect() as admin:
        admin.execute("CREATE SEQUENCE core.mcp_acl_test_sequence")
        admin.execute(sql.SQL("COMMENT ON ROLE {} IS {}").format(role, sql.Literal(dev.ROLE_MARKER)))
        admin.execute(sql.SQL("GRANT " + grant + " TO {}").format(role, **substitutions))
    try:
        # Real role checks run here; no Docker command or application DSN is used.
        with pytest.raises(psycopg.errors.RaiseException, match="privileges exceed read-only filter scope"):
            with seeded_db.connect() as admin:
                admin.execute(dev.provision_sql("synthetic-new-password-never-committed"))
        # The failed provisioning transaction must also roll back its password change.
        with psycopg.connect(seeded_db.reader_dsn) as reader:
            assert reader.execute("SELECT current_user").fetchone()[0] == seeded_db.reader_role
    finally:
        with seeded_db.connect() as admin:
            admin.execute(sql.SQL("REVOKE " + revoke + " FROM {}").format(role, **substitutions))
            admin.execute("DROP SEQUENCE core.mcp_acl_test_sequence")
            admin.execute(sql.SQL("COMMENT ON ROLE {} IS NULL").format(role))
