"""Real PG16 maintenance/restore checks with synthetic identities, no workers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest

from app.auth.backup import backup_and_verify
from app.auth.cli import manage_user
from app.auth.cutover import apply_identity_cutover
from app.auth.isolation_ledger import validate_service_migration_ledger
from app.auth.service_privilege_contract import VERSION, validate_service_auth_privileges
from app.auth.service_privileges import apply_service_auth_least_privilege
from app.postgres_migrations import apply_postgres_migrations
from app.services.monomer_dft_schema import probe_monomer_dft_schema


@pytest.fixture
def maintenance_database():
    base = os.environ.get("ISOLATION_TEST_ADMIN_DSN")
    if not base:
        pytest.skip("ISOLATION_TEST_ADMIN_DSN must identify a disposable PG16 instance")
    name = "acl_maintenance_" + uuid4().hex
    role = name + "_service"
    dsn = make_conninfo(base, dbname=name)
    with psycopg.connect(base, autocommit=True) as connection:
        assert connection.info.server_version // 10000 == 16
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        apply_postgres_migrations(dsn, allowed_kinds={"baseline", "expand"},
                                  allow_contract_on_fresh_database=True)
        with psycopg.connect(dsn, row_factory=dict_row) as connection:
            validate_service_migration_ledger(connection, "0017_user_isolation_prepare")
            owner = manage_user(connection, "create", username="scope-owner", password="synthetic-test-password")
            connection.execute(sql.SQL("CREATE ROLE {} LOGIN IN ROLE nexpoly_service").format(sql.Identifier(role)))
            system_id = connection.execute("SELECT system_identifier::text AS value FROM pg_control_system()").fetchone()["value"]
        yield dict(base=base, name=name, role=role, dsn=dsn, owner=str(owner["user_id"]), system_id=system_id)
    finally:
        with psycopg.connect(base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
            connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def apply(db):
    return apply_service_auth_least_privilege(
        db["dsn"], service_roles=[db["role"]], expected_database=db["name"],
        expected_system_identifier=db["system_id"],
    )


def test_explicit_cutovers_exact_retry_and_unchanged_dft_catalog(maintenance_database):
    db = maintenance_database
    with pytest.raises(ValueError, match="exact canonical ledger"):
        apply(db)
    apply_identity_cutover(db["dsn"], db["owner"])
    with psycopg.connect(db["dsn"], row_factory=dict_row) as connection:
        before = probe_monomer_dft_schema(connection).catalog_sha256
    result = apply(db)
    assert not result["already_applied"] and not result["credential_values_read"]
    assert apply(db)["already_applied"]
    retry = apply_identity_cutover(db["dsn"], db["owner"], expected_backup_state={"never-read": True})
    assert retry["already_applied"]
    with psycopg.connect(db["dsn"], row_factory=dict_row) as connection:
        assert probe_monomer_dft_schema(connection).catalog_sha256 == before
        assert not validate_service_auth_privileges(connection, db["role"])["password_select"]
        validate_service_migration_ledger(connection)
    # Generic bootstrap/expand must also leave an installed contraction intact.
    apply_postgres_migrations(db["dsn"], allowed_kinds={"baseline", "expand"},
                              allow_contract_on_fresh_database=True)
    assert apply(db)["already_applied"]


@pytest.mark.parametrize("drift", ["direct", "public", "checksum", "unknown"])
def test_drift_rolls_back_the_whole_contraction(maintenance_database, drift):
    db = maintenance_database
    apply_identity_cutover(db["dsn"], db["owner"])
    with psycopg.connect(db["dsn"]) as connection:
        if drift in {"direct", "public"}:
            recipient = sql.Identifier(db["role"]) if drift == "direct" else sql.SQL("PUBLIC")
            connection.execute(sql.SQL("GRANT SELECT(password_hash) ON auth.users TO {}").format(recipient))
        elif drift == "checksum":
            connection.execute("UPDATE governance.schema_migrations SET checksum=%s WHERE version='0018_user_isolation_cutover'", ("0" * 64,))
        else:
            connection.execute("INSERT INTO governance.schema_migrations(version,checksum) VALUES('9999_unknown',%s)", ("0" * 64,))
    with pytest.raises(ValueError):
        apply(db)
    with psycopg.connect(db["dsn"], row_factory=dict_row) as connection:
        assert connection.execute("SELECT has_table_privilege('nexpoly_service','auth.users','SELECT') AS yes").fetchone()["yes"]
        assert not connection.execute("SELECT 1 FROM governance.schema_migrations WHERE version=%s", (VERSION,)).fetchone()


def test_retry_rechecks_acl_and_target_identity(maintenance_database):
    db = maintenance_database
    apply_identity_cutover(db["dsn"], db["owner"])
    apply(db)
    for options in ({"expected_database": "wrong-target"}, {"expected_system_identifier": "1"}):
        arguments = dict(service_roles=[db["role"]], expected_database=db["name"], expected_system_identifier=db["system_id"])
        with pytest.raises(ValueError, match="target"):
            apply_service_auth_least_privilege(db["dsn"], **{**arguments, **options})
    with psycopg.connect(db["dsn"]) as connection:
        connection.execute(sql.SQL("GRANT SELECT(token_hash) ON auth.sessions TO {}").format(sql.Identifier(db["role"])))
    with pytest.raises(ValueError, match="authority"):
        apply(db)


@pytest.mark.parametrize("grant_template,revoke_template,error", [
    ("GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_binary_file(text) TO {}",
     "REVOKE EXECUTE ON FUNCTION pg_catalog.pg_read_binary_file(text) FROM {}", "server file function"),
    ("GRANT EXECUTE ON FUNCTION pg_catalog.lo_import(text,oid) TO PUBLIC",
     "REVOKE EXECUTE ON FUNCTION pg_catalog.lo_import(text,oid) FROM PUBLIC", "server file function"),
    ("GRANT pg_signal_backend TO {} WITH INHERIT FALSE, SET TRUE",
     "REVOKE pg_signal_backend FROM {}", "predefined management"),
    ("GRANT pg_checkpoint TO {}", "REVOKE pg_checkpoint FROM {}", "predefined management"),
    ("GRANT ALTER SYSTEM ON PARAMETER log_statement TO {}",
     "REVOKE ALTER SYSTEM ON PARAMETER log_statement FROM {}", "configuration parameter"),
    ("GRANT SET ON PARAMETER session_replication_role TO {} WITH GRANT OPTION",
     "REVOKE SET ON PARAMETER session_replication_role FROM {}", "configuration parameter"),
])
def test_legacy_precondition_rejects_system_authority_without_partial_migration(
        maintenance_database, grant_template, revoke_template, error):
    db = maintenance_database
    apply_identity_cutover(db["dsn"], db["owner"])
    grant = sql.SQL(grant_template).format(sql.Identifier(db["role"]))
    revoke = sql.SQL(revoke_template).format(sql.Identifier(db["role"]))
    with psycopg.connect(db["dsn"]) as connection:
        connection.execute(grant)
    try:
        with pytest.raises(ValueError, match=error):
            apply(db)
        # A failed check neither records 0019 nor partially contracts/repairs
        # authority. The same excess grant must still be visible to the check.
        with psycopg.connect(db["dsn"], row_factory=dict_row) as connection:
            validate_service_migration_ledger(connection, "0018_user_isolation_cutover")
            assert connection.execute("SELECT has_table_privilege('nexpoly_service','auth.users','SELECT') AS yes").fetchone()["yes"]
            with pytest.raises(ValueError, match=error):
                validate_service_auth_privileges(connection, db["role"], legacy=True)
    finally:
        with psycopg.connect(db["dsn"]) as connection:
            connection.execute(revoke)
    assert apply(db)["version"] == VERSION


def test_maintenance_cli_reserves_private_audit_and_redacts_failures(maintenance_database, tmp_path):
    db = maintenance_database
    apply_identity_cutover(db["dsn"], db["owner"])
    output = tmp_path / "audit.json"
    environment = {**os.environ, "SCOPE_TEST_ADMIN_DSN": db["dsn"]}
    command = [sys.executable, "-m", "app.auth.service_privileges", "--dsn-env", "SCOPE_TEST_ADMIN_DSN",
               "--expected-database", db["name"], "--expected-system-identifier", db["system_id"],
               "--service-role", db["role"], "--audit-output", str(output)]
    output.write_text("reserved by another operation")
    failure = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=30)
    assert failure.returncode != 0
    with psycopg.connect(db["dsn"]) as connection:
        assert not connection.execute("SELECT 1 FROM governance.schema_migrations WHERE version=%s", (VERSION,)).fetchone()
    output.unlink()
    completed = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text())["commit_confirmed"]
    assert db["dsn"] not in output.read_text() + completed.stdout + completed.stderr
    failed_output = tmp_path / "failed.json"
    command[-1] = str(failed_output)
    failure = subprocess.run(command, env={**environment, "SCOPE_TEST_ADMIN_DSN": "password=never-print-this host=/nonexistent"},
                             capture_output=True, text=True, timeout=30)
    assert failure.returncode != 0
    assert not json.loads(failed_output.read_text())["commit_confirmed"]
    assert "never-print-this" not in failed_output.read_text() + failure.stderr + failure.stdout


def test_successful_transaction_with_failed_audit_is_not_reported_as_rollback(monkeypatch, tmp_path):
    from app.auth import service_privileges as maintenance
    committed = []
    def apply_stub(*args, **kwargs):
        committed.append(True)
        return {"version": VERSION}
    sync_count = 0
    def failing_sync(_descriptor):
        nonlocal sync_count
        sync_count += 1
        if sync_count == 2:
            raise OSError("synthetic storage failure")
    monkeypatch.setattr(maintenance, "apply_service_auth_least_privilege", apply_stub)
    monkeypatch.setattr(maintenance.os, "fsync", failing_sync)
    monkeypatch.setenv("SCOPE_TEST_ADMIN_DSN", "unused-synthetic-dsn")
    monkeypatch.setattr(sys, "argv", ["maintenance", "--dsn-env", "SCOPE_TEST_ADMIN_DSN",
        "--expected-database", "synthetic", "--expected-system-identifier", "1",
        "--service-role", "synthetic_service", "--audit-output", str(tmp_path / "audit.json")])
    with pytest.raises(SystemExit, match="transaction returned success, but the audit write failed"):
        maintenance.main()
    assert committed == [True]


def test_driver_failure_and_failed_audit_never_emit_credentials(tmp_path):
    code = '''
import sys
from app.auth import service_privileges as maintenance
def failed_transaction(*args, **kwargs):
    raise RuntimeError("password=never-print-driver-secret")
calls = 0
def failed_sync(_descriptor):
    global calls
    calls += 1
    if calls == 2:
        raise OSError("audit-storage-failure")
maintenance.apply_service_auth_least_privilege = failed_transaction
maintenance.os.fsync = failed_sync
sys.argv = ["maintenance", "--dsn-env", "SCOPE_TEST_ADMIN_DSN", "--expected-database", "synthetic",
    "--expected-system-identifier", "1", "--service-role", "synthetic", "--audit-output", sys.argv[1]]
maintenance.main()
'''
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "audit.json")],
        env={**os.environ, "SCOPE_TEST_ADMIN_DSN": "unused"}, capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert "audit could not be written" in result.stderr
    assert "never-print-driver-secret" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("version", ["0017", "0018", "0019"])
def test_backup_restore_contract_at_each_maintenance_boundary(maintenance_database, tmp_path, version):
    db = maintenance_database
    if version >= "0018":
        apply_identity_cutover(db["dsn"], db["owner"])
    if version == "0019":
        apply(db)
    target = "acl_restore_" + uuid4().hex
    restore_dsn = make_conninfo(db["base"], dbname=target)
    with psycopg.connect(db["base"], autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
    try:
        receipt = backup_and_verify(db["dsn"], restore_dsn, tmp_path / "backup")
        assert json.loads(receipt.read_text())["restore_verified"]
        if version == "0017":
            # The first ever cutover still accepts its pre-cutover backup.
            apply_identity_cutover(restore_dsn, db["owner"])
            apply_service_auth_least_privilege(restore_dsn, service_roles=[db["role"]], expected_database=target)
        elif version == "0019":
            with psycopg.connect(restore_dsn, row_factory=dict_row) as connection:
                validate_service_auth_privileges(connection, db["role"])
                validate_service_migration_ledger(connection)
    finally:
        with psycopg.connect(db["base"], autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(target)))
