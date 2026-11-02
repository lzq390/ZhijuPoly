"""Recreate the approved audit-before-isolation path, never inspect live data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest

from app.auth.backup import backup_and_verify, postgres_environment, validate_backup_schema
from app.auth.cli import manage_user
from app.auth.cutover import apply_identity_cutover
from app.auth.schema import validate_isolation_schema
from app.auth.service_privileges import apply_service_auth_least_privilege
from app.postgres_migrations import apply_postgres_migrations
from app.services.monomer_dft_schema import (
    ISOLATION_CATALOG_FINGERPRINTS,
    MONOMER_DFT_GOVERNED_MUTABLE_AUDIT_CATALOG_FINGERPRINT_SHA256,
    monomer_dft_catalog_document,
    monomer_dft_catalog_sha256,
    probe_monomer_dft_schema,
)
from test_auth_isolation import _apply_historical_prefix


def pg_binary(name):
    pinned = Path("/usr/lib/postgresql/16/bin") / name
    result = str(pinned) if pinned.exists() else shutil.which(name)
    if result is None:
        pytest.skip("PostgreSQL 16 dump/restore tools are required")
    version = subprocess.check_output([result, "--version"], text=True)
    assert "(PostgreSQL) 16." in version
    return result


@pytest.fixture(scope="module")
def historical_restore(tmp_path_factory):
    base = os.environ.get("ISOLATION_TEST_ADMIN_DSN")
    if not base:
        pytest.skip("ISOLATION_TEST_ADMIN_DSN must target a dedicated PostgreSQL 16 instance")
    suffix = uuid4().hex[:12]
    names = ["dft_history_" + kind + "_" + suffix for kind in ("source", "restore", "verify")]
    source, restored, verify = [make_conninfo(base, dbname=name) for name in names]
    service = "dft_history_service_" + suffix
    directory = tmp_path_factory.mktemp("dft-historical-restore")
    created_owner = False
    with psycopg.connect(base, autocommit=True, row_factory=dict_row) as admin:
        for name in names:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname='polyprop'").fetchone():
                admin.execute("CREATE ROLE polyprop NOLOGIN")
                created_owner = True
            _apply_historical_prefix(source, "0015_property_filter_performance")
            with psycopg.connect(source, row_factory=dict_row) as connection:
                if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname='nexpoly_mutable_audit'").fetchone():
                    connection.execute("CREATE ROLE nexpoly_mutable_audit NOLOGIN")
                # Reproduce the official raw ACL spelling, as the existing
                # governed-audit catalog tests do, without elevating this role.
                connection.execute("GRANT USAGE ON SCHEMA governance TO polyprop")
                connection.execute("GRANT SELECT ON governance.schema_migrations TO polyprop")
                for table in ("artifacts", "job_attempts", "jobs"):
                    connection.execute(sql.SQL("ALTER TABLE monomer_dft.{} OWNER TO polyprop").format(sql.Identifier(table)))
                connection.execute("ALTER SCHEMA monomer_dft OWNER TO polyprop")
                with connection.transaction():
                    connection.execute("SET LOCAL ROLE polyprop")
                    # Exact DFT grants from the already reviewed audit role
                    # provisioning SQL. No SQL migration is patched or skipped.
                    for statement in (
                        "REVOKE ALL PRIVILEGES ON SCHEMA monomer_dft FROM nexpoly_mutable_audit",
                        "GRANT USAGE ON SCHEMA monomer_dft TO nexpoly_mutable_audit",
                        "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA monomer_dft FROM nexpoly_mutable_audit",
                        "GRANT SELECT ON ALL TABLES IN SCHEMA monomer_dft TO nexpoly_mutable_audit",
                        "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA monomer_dft FROM nexpoly_mutable_audit",
                        "GRANT SELECT ON ALL SEQUENCES IN SCHEMA monomer_dft TO nexpoly_mutable_audit",
                    ):
                        connection.execute(statement)
                    assert monomer_dft_catalog_sha256(connection) == MONOMER_DFT_GOVERNED_MUTABLE_AUDIT_CATALOG_FINGERPRINT_SHA256
                    # The later migration runner must use the real migrator.
                connection.execute("RESET ROLE")
            dump = directory / "before-isolation.dump"
            subprocess.run([pg_binary("pg_dump"), "--format=custom", "--file", str(dump)],
                           env=postgres_environment(source), check=True, capture_output=True)
            dump.chmod(0o600)
            subprocess.run([pg_binary("pg_restore"), "--exit-on-error", "--dbname", names[1], str(dump)],
                           env=postgres_environment(restored), check=True, capture_output=True)
            _apply_historical_prefix(restored, "0017_user_isolation_prepare")
            with psycopg.connect(restored, row_factory=dict_row) as connection:
                # Prepare backups remain supported before ownership is set.
                assert len(validate_backup_schema(connection)["migration_ledger"]) == 17
                legacy = manage_user(connection, "create", username="legacy-owner", password="test-only-password")
                connection.execute(sql.SQL("CREATE ROLE {} LOGIN IN ROLE nexpoly_service").format(sql.Identifier(service)))
            apply_identity_cutover(restored, str(legacy["user_id"]))
            with psycopg.connect(restored, row_factory=dict_row) as connection:
                validate_isolation_schema(connection, "0018_user_isolation_cutover")
            apply_service_auth_least_privilege(restored, service_roles=[service], expected_database=names[1])
            with psycopg.connect(restored, row_factory=dict_row) as connection:
                validate_isolation_schema(connection, "0019_service_auth_least_privilege")
            apply_postgres_migrations(restored, allowed_kinds={"expand"})
            yield {"dsn": restored, "verify": verify, "directory": directory}
        finally:
            for name in names:
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(service)))
            if created_owner:
                admin.execute("DROP ROLE polyprop")


def test_original_audit_then_pg16_restore_and_identity_migrations_are_ready(historical_restore):
    with psycopg.connect(historical_restore["dsn"], row_factory=dict_row) as connection:
        probe = probe_monomer_dft_schema(connection)
        assert probe.ready
        assert probe.reason == "exact_0018_user_isolation_cutover"
        assert probe.catalog_sha256 == ISOLATION_CATALOG_FINGERPRINTS["0018_user_isolation_cutover"][1]
        document = monomer_dft_catalog_document(connection)
        namespace_acl = document["namespace"][0]["access_control"]
        assert namespace_acl.index("nexpoly_mutable_audit=") < namespace_acl.index("nexpoly_api=")
        sequence = next(item for item in document["relations"] if item["relation_kind"] == "S")
        assert "nexpoly_mutable_audit=r/" in sequence["access_control"]
        assert len(validate_backup_schema(connection)["migration_ledger"]) == 22


def test_historical_profile_survives_actual_backup_and_second_restore(historical_restore):
    receipt = backup_and_verify(historical_restore["dsn"], historical_restore["verify"],
                               historical_restore["directory"] / "verified-backup")
    assert json.loads(receipt.read_text())["restore_verified"] is True
    with psycopg.connect(historical_restore["verify"], row_factory=dict_row) as connection:
        assert probe_monomer_dft_schema(connection).ready
        assert len(validate_backup_schema(connection)["migration_ledger"]) == 22


@pytest.mark.parametrize("statements", [
    ("GRANT USAGE ON SCHEMA monomer_dft TO PUBLIC",),
    ("GRANT SELECT ON monomer_dft.jobs TO PUBLIC",),
    ("GRANT UPDATE ON monomer_dft.jobs TO nexpoly_mutable_audit",),
    ("GRANT SELECT(input_smiles) ON monomer_dft.jobs TO nexpoly_mutable_audit",),
    ("REVOKE SELECT ON monomer_dft.jobs_enqueue_sequence_seq FROM nexpoly_mutable_audit",),
    ("REVOKE USAGE ON SCHEMA monomer_dft FROM nexpoly_mutable_audit",
     "GRANT USAGE ON SCHEMA monomer_dft TO nexpoly_mutable_audit"),
    ("REVOKE SELECT ON monomer_dft.jobs FROM nexpoly_mutable_audit",
     "GRANT SELECT ON monomer_dft.jobs TO nexpoly_mutable_audit"),
    ("CREATE INDEX unapproved_historical_index ON monomer_dft.jobs(status)",),
])
def test_only_exact_historical_acl_and_catalog_are_accepted(historical_restore, statements):
    with psycopg.connect(historical_restore["dsn"], row_factory=dict_row) as connection:
        with connection.transaction(force_rollback=True):
            for statement in statements:
                connection.execute(statement)
            probe = probe_monomer_dft_schema(connection)
            assert not probe.ready
            assert probe.reason == "isolation_catalog_fingerprint_mismatch"
        assert probe_monomer_dft_schema(connection).ready
