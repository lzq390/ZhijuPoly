"""Authentication ACL checks against an explicitly configured disposable PG16.

These tests use synthetic metadata and never invoke a scientific executor.
"""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from app.auth.service_privilege_contract import (
    AUTH_COLUMNS, LEGACY_VERSION, PRIVATE_SCHEMAS,
    SELECT_COLUMNS, VERSION,
    validate_service_auth_privileges,
)

MIGRATION = Path(__file__).parents[1] / "migrations/postgres" / (VERSION + ".sql")

# Independent PG16 expectations: never derive negative cases from the
# implementation's denylist, which would duplicate any missing entry.
MANAGEMENT_ROLES = (
    "pg_read_server_files", "pg_write_server_files", "pg_execute_server_program",
    "pg_read_all_data", "pg_write_all_data", "pg_signal_backend", "pg_checkpoint",
    "pg_use_reserved_connections", "pg_create_subscription", "pg_monitor",
    "pg_read_all_settings", "pg_read_all_stats", "pg_stat_scan_tables",
)
FILE_FUNCTION_SIGNATURES = (
    "pg_read_file(text)", "pg_read_file(text,boolean)",
    "pg_read_file(text,bigint,bigint)", "pg_read_file(text,bigint,bigint,boolean)",
    "pg_read_binary_file(text)", "pg_read_binary_file(text,boolean)",
    "pg_read_binary_file(text,bigint,bigint)", "pg_read_binary_file(text,bigint,bigint,boolean)",
    "pg_stat_file(text)", "pg_stat_file(text,boolean)",
    "pg_ls_dir(text)", "pg_ls_dir(text,boolean,boolean)",
    "lo_import(text)", "lo_import(text,oid)", "lo_export(oid,text)",
)


@pytest.fixture
def service_acl_database():
    base = os.getenv("ISOLATION_TEST_ADMIN_DSN")
    if not base:
        pytest.skip("ISOLATION_TEST_ADMIN_DSN must name a disposable PostgreSQL 16 instance")
    suffix = uuid4().hex[:16]
    database, login = "service_acl_" + suffix, "service_login_" + suffix
    extra = "service_extra_" + suffix
    dsn = make_conninfo(base, dbname=database)
    with psycopg.connect(base, autocommit=True) as administrator:
        assert administrator.info.server_version // 10000 == 16
        administrator.execute("""DO $$ BEGIN
            IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='nexpoly_service') THEN
              CREATE ROLE nexpoly_service NOLOGIN;
            END IF;
          END $$""")
        administrator.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS IN ROLE nexpoly_service").format(sql.Identifier(login)))
        administrator.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS").format(sql.Identifier(extra)))
        administrator.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database)))
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as connection:
            connection.execute("""CREATE SCHEMA auth;
                CREATE TABLE auth.users (
                  user_id uuid PRIMARY KEY,username text,password_hash text,status text,
                  must_change_password boolean,is_system boolean,created_at timestamptz,
                  updated_at timestamptz,password_changed_at timestamptz);
                CREATE TABLE auth.sessions (
                  session_id uuid PRIMARY KEY,user_id uuid,token_hash bytea,
                  created_at timestamptz,expires_at timestamptz,revoked_at timestamptz);
                GRANT USAGE ON SCHEMA auth TO nexpoly_service;
                GRANT SELECT ON auth.users TO nexpoly_service;
                GRANT UPDATE(updated_at) ON auth.users TO nexpoly_service;
                INSERT INTO auth.users(user_id,status,is_system)
                  VALUES('00000000-0000-0000-0000-000000000002','active',false);
                CREATE SCHEMA service_business;
                CREATE TABLE service_business.jobs(id integer);
                GRANT USAGE ON SCHEMA service_business TO nexpoly_service;
                GRANT SELECT,INSERT,UPDATE,DELETE ON service_business.jobs TO nexpoly_service;
            """)
            connection.execute(MIGRATION.read_text())
            connection.commit()
            yield {"admin": connection, "dsn": dsn, "login": login, "extra": extra,
                   "service_dsn": make_conninfo(dsn, user=login)}
            connection.rollback()
    finally:
        with psycopg.connect(base, autocommit=True) as administrator:
            administrator.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))
            administrator.execute(sql.SQL("DROP ROLE IF EXISTS {},{}").format(sql.Identifier(login), sql.Identifier(extra)))


def test_current_privileges_and_real_for_update_do_not_need_credential_columns(service_acl_database):
    fixture = service_acl_database
    report = validate_service_auth_privileges(fixture["admin"], fixture["login"], require_login=True)
    assert report["version"] == VERSION
    assert report["users_select_columns"] == sorted(SELECT_COLUMNS)
    assert report["users_update_columns"] == ["updated_at"]
    assert report["current_readiness"] and not report["historical_only"]
    assert not report["password_select"] and not report["session_token_select"]
    with psycopg.connect(fixture["service_dsn"], row_factory=dict_row) as service:
        assert validate_service_auth_privileges(service, require_login=True)["role"] == fixture["login"]
        service.execute("CREATE TEMPORARY TABLE allowed_scratch(id integer)")
        row = service.execute("SELECT status,is_system FROM auth.users WHERE user_id=%s FOR UPDATE",
                              ("00000000-0000-0000-0000-000000000002",)).fetchone()
        assert row == {"status": "active", "is_system": False}
        # Unrelated service business permissions survive the narrow migration.
        service.execute("INSERT INTO service_business.jobs VALUES(1)")
        assert service.execute("SELECT id FROM service_business.jobs").fetchone()["id"] == 1
        service.rollback()
        for statement in ("SELECT password_hash FROM auth.users LIMIT 0",
                          "SELECT token_hash FROM auth.sessions LIMIT 0",
                          "UPDATE auth.users SET status='disabled' WHERE false"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with service.transaction():
                    service.execute(statement)


def test_legacy_precondition_is_explicit_and_not_current_readiness(service_acl_database):
    fixture = service_acl_database
    connection = fixture["admin"]
    connection.execute("GRANT SELECT ON auth.users TO nexpoly_service")
    report = validate_service_auth_privileges(connection, fixture["login"], legacy=True)
    assert report["version"] == LEGACY_VERSION
    assert report["historical_only"] and not report["current_readiness"]
    assert report["users_select_columns"] == sorted(AUTH_COLUMNS["users"])
    with pytest.raises(ValueError, match="excess"):
        validate_service_auth_privileges(connection, fixture["login"])
    connection.execute(MIGRATION.read_text())
    assert validate_service_auth_privileges(connection, fixture["login"])["current_readiness"]


def test_default_identity_cannot_hide_administrator_with_set_role(service_acl_database):
    connection = service_acl_database["admin"]
    connection.execute("SET LOCAL ROLE nexpoly_service")
    with pytest.raises(ValueError, match="current_user must equal session_user"):
        validate_service_auth_privileges(connection)


def test_connection_role_option_cannot_hide_original_login(service_acl_database):
    dsn = make_conninfo(service_acl_database["service_dsn"], options="-c role=nexpoly_service")
    with psycopg.connect(dsn) as connection:
        with pytest.raises(ValueError, match="current_user must equal session_user"):
            validate_service_auth_privileges(connection, require_login=True)


@pytest.mark.parametrize("drift", [
    "GRANT SELECT(password_hash) ON auth.users TO nexpoly_service",
    "GRANT SELECT(username) ON auth.users TO nexpoly_service",
    "GRANT SELECT ON auth.users TO nexpoly_service",
    "GRANT SELECT(token_hash) ON auth.sessions TO nexpoly_service",
    "GRANT SELECT(password_hash) ON auth.users TO PUBLIC",
    "GRANT UPDATE(status) ON auth.users TO nexpoly_service",
    "GRANT INSERT(user_id) ON auth.users TO nexpoly_service",
    "GRANT REFERENCES(user_id) ON auth.users TO nexpoly_service",
    "GRANT DELETE ON auth.users TO nexpoly_service",
    "GRANT TRUNCATE ON auth.sessions TO nexpoly_service",
    "GRANT CREATE ON SCHEMA auth TO nexpoly_service",
    "GRANT SELECT(user_id) ON auth.users TO nexpoly_service WITH GRANT OPTION",
    "GRANT USAGE ON SCHEMA auth TO nexpoly_service WITH GRANT OPTION",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA auth GRANT SELECT ON TABLES TO nexpoly_service",
    "ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO nexpoly_service",
    "CREATE FUNCTION auth.extra() RETURNS integer LANGUAGE sql AS 'SELECT 1'",
    "CREATE SEQUENCE auth.extra; GRANT USAGE ON SEQUENCE auth.extra TO nexpoly_service",
    "CREATE TABLE auth.extra(id integer); GRANT SELECT ON auth.extra TO nexpoly_service",
    "ALTER TABLE auth.users OWNER TO nexpoly_service",
    "ALTER SCHEMA auth OWNER TO nexpoly_service",
])
def test_effective_auth_drift_fails_without_repair(service_acl_database, drift):
    fixture = service_acl_database
    fixture["admin"].execute(drift)
    with pytest.raises(ValueError, match="service authentication privilege contract"):
        validate_service_auth_privileges(fixture["admin"], fixture["login"])


@pytest.mark.parametrize("column", ["password_hash", "token_hash", "user_id", "updated_at"])
def test_missing_columns_never_pass_an_empty_aggregate(service_acl_database, column):
    fixture = service_acl_database
    table = "sessions" if column == "token_hash" else "users"
    fixture["admin"].execute(sql.SQL("ALTER TABLE auth.{} DROP COLUMN {} CASCADE").format(sql.Identifier(table), sql.Identifier(column)))
    with pytest.raises(ValueError, match="missing authentication columns"):
        validate_service_auth_privileges(fixture["admin"], fixture["login"])


@pytest.mark.parametrize("table", ["users", "sessions"])
def test_missing_tables_are_rejected(service_acl_database, table):
    fixture = service_acl_database
    fixture["admin"].execute(sql.SQL("DROP TABLE auth.{}").format(sql.Identifier(table)))
    with pytest.raises(ValueError, match="ordinary tables"):
        validate_service_auth_privileges(fixture["admin"], fixture["login"])


@pytest.mark.parametrize("inherit,set_role,rejected", [(True, False, True), (False, True, True), (False, False, False)])
def test_pg16_inherit_and_set_role_edges_are_distinguished(service_acl_database, inherit, set_role, rejected):
    fixture = service_acl_database
    connection = fixture["admin"]
    connection.execute(sql.SQL("GRANT SELECT(password_hash) ON auth.users TO {}").format(sql.Identifier(fixture["extra"])))
    connection.execute(sql.SQL("GRANT {} TO {} WITH INHERIT {}, SET {}").format(
        sql.Identifier(fixture["extra"]), sql.Identifier(fixture["login"]),
        sql.SQL("TRUE" if inherit else "FALSE"), sql.SQL("TRUE" if set_role else "FALSE")))
    if rejected:
        with pytest.raises(ValueError, match="excess"):
            validate_service_auth_privileges(connection, fixture["login"])
    else:
        assert validate_service_auth_privileges(connection, fixture["login"])["current_readiness"]


def test_transitive_set_role_path_is_checked(service_acl_database):
    fixture = service_acl_database
    connection = fixture["admin"]
    # Login -> service group -> extra, with the final grant not inherited.
    connection.execute(sql.SQL("GRANT SELECT(password_hash) ON auth.users TO {}").format(sql.Identifier(fixture["extra"])))
    connection.execute(sql.SQL("GRANT {} TO nexpoly_service WITH INHERIT FALSE, SET TRUE").format(sql.Identifier(fixture["extra"])))
    with pytest.raises(ValueError, match="excess"):
        validate_service_auth_privileges(connection, fixture["login"])


def test_direct_login_grants_and_role_administration_are_rejected(service_acl_database):
    fixture = service_acl_database
    connection = fixture["admin"]
    with connection.transaction(force_rollback=True):
        connection.execute(sql.SQL("GRANT SELECT(password_hash) ON auth.users TO {}").format(sql.Identifier(fixture["login"])))
        with pytest.raises(ValueError, match="excess"):
            validate_service_auth_privileges(connection, fixture["login"])
    connection.execute(sql.SQL("GRANT {} TO {} WITH ADMIN TRUE, INHERIT FALSE, SET FALSE").format(
        sql.Identifier(fixture["extra"]), sql.Identifier(fixture["login"])))
    with pytest.raises(ValueError, match="role administration"):
        validate_service_auth_privileges(connection, fixture["login"])


def test_required_privileges_and_login_are_enforced(service_acl_database):
    fixture = service_acl_database
    connection = fixture["admin"]
    assert validate_service_auth_privileges(connection, "nexpoly_service")["role"] == "nexpoly_service"
    with pytest.raises(ValueError, match="LOGIN"):
        validate_service_auth_privileges(connection, "nexpoly_service", require_login=True)
    with pytest.raises(ValueError, match="inherit nexpoly_service"):
        validate_service_auth_privileges(connection, fixture["extra"])
    with pytest.raises(ValueError, match="inherit nexpoly_service"):
        validate_service_auth_privileges(connection, "absent_service_role")
    connection.execute("REVOKE UPDATE(updated_at) ON auth.users FROM nexpoly_service")
    with pytest.raises(ValueError, match="missing column authority"):
        validate_service_auth_privileges(connection, fixture["login"])


@pytest.mark.parametrize("attribute", ["SUPERUSER", "BYPASSRLS", "CREATEDB", "CREATEROLE", "REPLICATION"])
def test_privileged_runtime_role_is_rejected(service_acl_database, attribute):
    fixture = service_acl_database
    fixture["admin"].execute(sql.SQL("ALTER ROLE {} {}").format(
        sql.Identifier(fixture["login"]), sql.SQL(attribute)))
    with pytest.raises(ValueError, match="privileged role"):
        validate_service_auth_privileges(fixture["admin"], fixture["login"])


@pytest.mark.parametrize("builtin_role", MANAGEMENT_ROLES)
@pytest.mark.parametrize("inherit,set_role", [(True, False), (False, True)])
def test_builtin_server_authority_is_rejected_without_invoking_it(service_acl_database, builtin_role, inherit, set_role):
    fixture = service_acl_database
    fixture["admin"].execute(sql.SQL("GRANT {} TO {} WITH INHERIT {}, SET {}").format(
        sql.Identifier(builtin_role), sql.Identifier(fixture["login"]),
        sql.SQL("TRUE" if inherit else "FALSE"), sql.SQL("TRUE" if set_role else "FALSE")))
    with pytest.raises(ValueError, match="predefined management or data-access authority"):
        validate_service_auth_privileges(fixture["admin"], fixture["login"])


def authority_recipient(fixture, path):
    if path == "public":
        return sql.SQL("PUBLIC")
    if path == "direct":
        return sql.Identifier(fixture["login"])
    fixture["admin"].execute(sql.SQL("GRANT {} TO {} WITH INHERIT {}, SET {}").format(
        sql.Identifier(fixture["extra"]), sql.Identifier(fixture["login"]),
        sql.SQL("TRUE" if path == "inherited" else "FALSE"),
        sql.SQL("TRUE" if path == "set_role" else "FALSE")))
    return sql.Identifier(fixture["extra"])


@pytest.mark.parametrize("path,grant_option", [
    ("direct", False), ("public", False), ("inherited", False), ("set_role", False),
    ("direct", True), ("inherited", True), ("set_role", True), ("unreachable", False),
])
def test_every_server_file_overload_checks_effective_paths_without_execution(service_acl_database, path, grant_option):
    fixture = service_acl_database
    connection = fixture["admin"]
    recipient = authority_recipient(fixture, path)
    for signature in FILE_FUNCTION_SIGNATURES:
        with connection.transaction(force_rollback=True):
            connection.execute(sql.SQL("GRANT EXECUTE ON FUNCTION pg_catalog.{} TO {}{}").format(
                sql.SQL(signature), recipient, sql.SQL(" WITH GRANT OPTION" if grant_option else "")))
            if path == "unreachable":
                assert validate_service_auth_privileges(connection, fixture["login"])["current_readiness"]
            else:
                with pytest.raises(ValueError, match="server file function authority"):
                    validate_service_auth_privileges(connection, fixture["login"])


@pytest.mark.parametrize("path,grant_option", [
    ("direct", False), ("public", False), ("inherited", False), ("set_role", False),
    ("direct", True), ("inherited", True), ("set_role", True), ("unreachable", False),
])
@pytest.mark.parametrize("parameter", ["log_statement", "scope_test.unloaded_extension"])
@pytest.mark.parametrize("privilege", ["SET", "ALTER SYSTEM"])
def test_parameter_acl_paths_include_unloaded_parameters_without_reading_values(
        service_acl_database, path, grant_option, parameter, privilege):
    fixture = service_acl_database
    connection = fixture["admin"]
    # Roll back these cluster-wide grants before fixture role cleanup, even
    # when an assertion fails. No SET or ALTER SYSTEM command is executed.
    with connection.transaction(force_rollback=True):
        recipient = authority_recipient(fixture, path)
        connection.execute(sql.SQL("GRANT {} ON PARAMETER {} TO {}{}").format(
            sql.SQL(privilege), sql.Identifier(parameter), recipient,
            sql.SQL(" WITH GRANT OPTION" if grant_option else "")))
        if path == "unreachable":
            assert validate_service_auth_privileges(connection, fixture["login"])["current_readiness"]
        else:
            with pytest.raises(ValueError, match="configuration parameter grant authority"):
                validate_service_auth_privileges(connection, fixture["login"])


def test_normal_session_settings_and_unreachable_management_membership_remain_allowed(service_acl_database):
    fixture = service_acl_database
    fixture["admin"].execute(sql.SQL(
        "GRANT pg_signal_backend TO {} WITH INHERIT FALSE, SET FALSE").format(sql.Identifier(fixture["login"])))
    fixture["admin"].commit()
    with psycopg.connect(fixture["service_dsn"]) as service:
        service.execute("SET LOCAL statement_timeout='1s'")
        service.execute("SET LOCAL work_mem='4MB'")
        service.execute("SELECT pg_catalog.set_config('app.user_id',%s,true)",
                        ("00000000-0000-0000-0000-000000000002",))
        assert validate_service_auth_privileges(service, require_login=True)["current_readiness"]


@pytest.mark.parametrize("schema", [name for name in PRIVATE_SCHEMAS if name != "auth"])
@pytest.mark.parametrize("authority", ["schema_owner", "table_owner", "schema_create"])
def test_private_management_authority_is_rejected(service_acl_database, schema, authority):
    fixture = service_acl_database
    connection = fixture["admin"]
    connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    if authority == "schema_owner":
        statement = sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(sql.Identifier(schema), sql.Identifier(fixture["login"]))
    elif authority == "table_owner":
        connection.execute(sql.SQL("CREATE TABLE {}.owned(id integer)").format(sql.Identifier(schema)))
        statement = sql.SQL("ALTER TABLE {}.owned OWNER TO {}").format(sql.Identifier(schema), sql.Identifier(fixture["login"]))
    else:
        statement = sql.SQL("GRANT CREATE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(fixture["login"]))
    connection.execute(statement)
    with pytest.raises(ValueError, match="private schema/table"):
        validate_service_auth_privileges(connection, fixture["login"])


@pytest.mark.parametrize("authority", ["owner", "create", "public_create"])
def test_database_management_authority_is_rejected(service_acl_database, authority):
    fixture = service_acl_database
    connection = fixture["admin"]
    name = connection.info.dbname
    if authority == "owner":
        statement = sql.SQL("ALTER DATABASE {} OWNER TO {}").format(sql.Identifier(name), sql.Identifier(fixture["login"]))
    else:
        recipient = sql.SQL("PUBLIC") if authority == "public_create" else sql.Identifier(fixture["login"])
        statement = sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(sql.Identifier(name), recipient)
    connection.execute(statement)
    with pytest.raises(ValueError, match="database ownership or CREATE"):
        validate_service_auth_privileges(connection, fixture["login"])


def test_private_owner_scope_stays_consistent_with_runtime_validator():
    from app.auth.schema import PRIVATE_SCHEMAS as runtime_scope
    assert PRIVATE_SCHEMAS == runtime_scope


@pytest.mark.parametrize("column", sorted(SELECT_COLUMNS))
def test_each_required_read_column_is_enforced(service_acl_database, column):
    fixture = service_acl_database
    fixture["admin"].execute(sql.SQL("REVOKE SELECT({}) ON auth.users FROM nexpoly_service").format(sql.Identifier(column)))
    with pytest.raises(ValueError, match="missing column authority"):
        validate_service_auth_privileges(fixture["admin"], fixture["login"])


def test_legacy_does_not_hide_unrelated_auth_drift(service_acl_database):
    fixture = service_acl_database
    fixture["admin"].execute("GRANT SELECT ON auth.users TO nexpoly_service; GRANT SELECT ON auth.sessions TO nexpoly_service")
    with pytest.raises(ValueError, match="excess"):
        validate_service_auth_privileges(fixture["admin"], fixture["login"], legacy=True)
