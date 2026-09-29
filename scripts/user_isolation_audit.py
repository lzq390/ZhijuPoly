#!/usr/bin/env python3
"""Read-only isolation preflight and safe evidence for the dedicated 0018 cutover.

The production v7 helper is intentionally a separate, frozen evidence protocol.
This tool never exports passwords, session token digests, or business rows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT))

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.migration_policy import validate_migration_manifest_entries
from scripts.site_helper_contracts import validate_user_isolation_ledger
from app.auth.schema import OWNER_TABLES, CHILD_TABLES, validate_runtime_role, validate_isolation_schema

# Full-database backups must retain these schemas; credential-bearing auth data
# belongs only inside the owner-private backup, never the public audit projection.
BACKUP_REQUIRED_SCHEMAS = ("auth", "governance", "online_knowledge", "md", "monomer_dft", "polymerization_batch")
AUTH_COLUMNS = {
    "auth.users": ("user_id", "username", "status", "must_change_password", "is_system", "created_at", "updated_at", "password_changed_at"),
    "auth.sessions": ("session_id", "user_id", "created_at", "expires_at", "revoked_at"),
}


def _digest_rows(connection, relation: str, *, legacy: bool = False):
    identifier = sql.Identifier(*relation.split("."))
    if relation in AUTH_COLUMNS:
        fields = sql.SQL(",").join(sql.Identifier(name) for name in AUTH_COLUMNS[relation])
        source = sql.SQL("(SELECT {} FROM {}) value").format(fields, identifier)
    else:
        source = sql.SQL("{} value").format(identifier)
    projection = sql.SQL("to_jsonb(value)")
    if legacy:
        projection += sql.SQL("-'owner_user_id'-'start_authorized_at'")
    query = sql.SQL("SELECT ({})::text AS document FROM {} ORDER BY 1").format(projection, source)
    digest = hashlib.sha256()
    count = 0
    with connection.cursor(name="isolation_audit_" + hashlib.sha256((relation + str(legacy)).encode()).hexdigest()[:16]) as cursor:
        cursor.execute(query)
        for row in cursor:
            payload = row["document"].encode("utf-8")
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
            count += 1
    return {"row_count": count, "sha256": digest.hexdigest()}



def capture(connection, *, require_audit_role: bool = True):
    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    connection.execute("SET LOCAL search_path=pg_catalog")
    connection.execute("SET LOCAL statement_timeout='5min'")
    role = validate_runtime_role(connection, "nexpoly_mutable_audit") if require_audit_role else {"role": "restore-verifier"}
    if require_audit_role:
        secrets = connection.execute("SELECT has_column_privilege(current_user,'auth.users','password_hash','SELECT') OR has_column_privilege(current_user,'auth.sessions','token_hash','SELECT') AS readable").fetchone()
        if secrets["readable"]:
            raise ValueError("audit identity can read credential columns")
    entries = validate_migration_manifest_entries(REPO_ROOT / "backend/migrations/postgres")
    ledger = [dict(row) for row in connection.execute("SELECT version,checksum FROM governance.schema_migrations ORDER BY version")]
    expected_ledger = [{"version": entry.version, "checksum": entry.checksum} for entry in entries]
    validate_user_isolation_ledger(ledger)
    if ledger != expected_ledger:
        raise ValueError("isolation audit requires the exact complete 0018 migration ledger")
    schema_proof = validate_isolation_schema(connection)
    ownership = {}
    for relation in OWNER_TABLES:
        row = connection.execute("""SELECT c.relrowsecurity,c.relforcerowsecurity,a.attnotnull
            FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid AND a.attname='owner_user_id'
            WHERE c.oid=%s::regclass""", (relation,)).fetchone()
        if row is None or not all(row.values()):
            raise ValueError("ownership/RLS contract missing for " + relation)
        counts = connection.execute(sql.SQL("SELECT count(*) AS rows,count(*) FILTER (WHERE owner_user_id IS NULL) AS unowned FROM {}").format(sql.Identifier(*relation.split('.')))).fetchone()
        if counts["unowned"]:
            raise ValueError("unowned records in " + relation)
        ownership[relation] = dict(counts)
    relations = (*AUTH_COLUMNS, *OWNER_TABLES, *CHILD_TABLES)
    return {
        "schema_version": 1,
        "database": connection.execute("SELECT current_database() AS name").fetchone()["name"],
        "audit_identity": role,
        "migration_ledger": ledger,
        "backup_required_schemas": list(BACKUP_REQUIRED_SCHEMAS),
        "credential_projection": "excluded",
        "business_tables": {relation: _digest_rows(connection, relation) for relation in relations},
        "legacy_business_projection": {relation: _digest_rows(connection, relation, legacy=True) for relation in OWNER_TABLES},
        "ownership": ownership,
        "rls_policies": schema_proof["rls_policies"],
        "dft_catalog_sha256": schema_proof["dft_catalog_sha256"],
    }


def preflight(dsns: dict[str, str]):
    result = {}
    database = None
    for group, dsn in dsns.items():
        with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as connection:
            result[group] = validate_runtime_role(connection, group)
            current = connection.execute("SELECT current_database() AS database,inet_server_addr()::text AS host,inet_server_port() AS port").fetchone()
            if database is not None and current != database:
                raise ValueError("runtime identities connect to different databases")
            database = current
    return {"database": dict(database), "identities": result, "rollback_before_0018_allowed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("capture", "preflight"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--audit-dsn-env", default="AUTH_AUDIT_POSTGRES_DSN")
    args = parser.parse_args()
    if args.operation == "capture":
        with psycopg.connect(os.environ[args.audit_dsn_env], row_factory=dict_row, connect_timeout=5) as connection:
            evidence = capture(connection)
    else:
        evidence = preflight({"nexpoly_api": os.environ["APP_POSTGRES_DSN"], "nexpoly_auth": os.environ["AUTH_POSTGRES_DSN"], "nexpoly_service": os.environ["APP_SERVICE_POSTGRES_DSN"]})
    payload = (json.dumps(evidence, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
    print(f"{args.operation}: {args.output}")


if __name__ == "__main__":
    main()
