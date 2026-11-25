"""Initialize only the dedicated M2 PostgreSQL cluster over its private local socket."""

import json
import os
from pathlib import Path
import tempfile

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from app.auth.service import hash_password

from app.auth.cutover import apply_identity_cutover
from app.auth.isolation_ledger import expected_isolation_ledger
from app.auth.service_privileges import apply_service_auth_least_privilege
from app.billing_migrations import migrate_memberships
from app.migration_policy import validate_migration_manifest_entries
from app.postgres_migrations import MIGRATIONS_DIR, apply_postgres_migrations
from app.retrosynthesis_migrations import migrate_retrosynthesis


def initial_schema(dsn):
    through = "0017_user_isolation_prepare"
    entries = validate_migration_manifest_entries(MIGRATIONS_DIR)
    manifest = json.loads((MIGRATIONS_DIR / "manifest.json").read_text())
    manifest["migrations"] = [e for e in manifest["migrations"] if e["version"] <= through]
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp)
        for entry in entries:
            if entry.version <= through:
                filename = entry.version + ".sql"
                (prefix / filename).write_bytes((MIGRATIONS_DIR / filename).read_bytes())
        (prefix / "manifest.json").write_text(json.dumps(manifest))
        apply_postgres_migrations(
            dsn,
            migrations_dir=prefix,
            allowed_kinds={"baseline", "expand"},
            allow_contract_on_fresh_database=True,
        )


def workspace_extensions(dsn):
    directory = MIGRATIONS_DIR.parent / "openscience-dev"
    extensions = (
        ("SELECT to_regclass('openscience_m2.workspace_bindings')", "0001_workspace_access.sql"),
        (
            "SELECT to_regprocedure('openscience_m2.membership_transition()')",
            "0002_membership_revocation.sql",
        ),
        ("SELECT to_regclass('openscience_m3.delegations')", "0003_native_delegation.sql"),
        (
            "SELECT max(attname::text) FROM pg_attribute WHERE attrelid='openscience_m3.operations'::regclass "
            "AND attname='workspace_generation' AND NOT attisdropped",
            "0004_workspace_generation.sql",
        ),
        (
            "SELECT to_regclass('openscience_m3.operations_draft')",
            "0005_adapter_scopes.sql",
        ),
    )
    if os.environ.get("OPENSCIENCE_M4_ENABLED") == "1":
        extensions += (
            ("SELECT to_regclass('openscience_m4.model_requests')", "0006_m4_model.sql"),
            ("SELECT to_regclass('openscience_m4_knowledge.chunks')", "0007_m4_knowledge.sql"),
            (
                "SELECT max(attname::text) FROM pg_attribute "
                "WHERE attrelid='openscience_m4.model_requests'::regclass AND attname='request_tag' AND NOT attisdropped",
                "0008_m4_model_retry.sql",
            ),
            (
                "SELECT max(attname::text) FROM pg_attribute WHERE "
                "attrelid='openscience_m4.model_requests'::regclass "
                "AND attname='workspace_generation_fingerprint' AND NOT attisdropped",
                "0009_m4_model_generation.sql",
            ),
        )
    with psycopg.connect(dsn, autocommit=True) as conn:
        # Each extension script owns its transaction. This session lock spans them
        # and is automatically released if setup is interrupted.
        conn.execute("SELECT pg_advisory_lock(hashtext('openscience_m2_development_extensions'))")
        for check, name in extensions:
            if conn.execute(check).fetchone()[0] is None:
                conn.execute((directory / name).read_text())


def initialize():
    dsn = os.environ["OPENSCIENCE_M2_ADMIN_DSN"]
    target = conninfo_to_dict(dsn)
    if (target.get("dbname"), target.get("user"), target.get("host")) != (
        "nexpoly_m2_dev",
        "m2_admin",
        "/m2pg",
    ):
        raise ValueError("Expected the M2-only administrator and local Unix socket")
    with psycopg.connect(dsn) as conn:
        if conn.execute("SELECT to_regclass('openscience_m2.workspace_bindings')").fetchone()[0]:
            workspace_extensions(dsn)
            return
        if conn.execute("SELECT to_regnamespace('governance')").fetchone()[0]:
            # All successor steps are re-entrant. Only the runner's own database is eligible.
            fresh = False
        else:
            fresh = True
    if fresh:
        initial_schema(dsn)
    with psycopg.connect(dsn) as conn:
        # Use the retained development A identity for the empty cluster's cutover.
        owner = "10000000-0000-4000-8000-000000000001"
        password = Path(os.environ["OPENSCIENCE_M2_PASSWORD_A_FILE"]).read_text().strip()
        conn.execute(
            """INSERT INTO auth.users (user_id,username,password_hash,must_change_password)
                        VALUES (%s,'test-user-a',%s,false) ON CONFLICT DO NOTHING""",
            (owner, hash_password(password)),
        )
        for kind in ("api", "auth", "service"):
            name = "m2_" + kind
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (name,)).fetchone():
                spec = conninfo_to_dict(
                    os.environ[
                        {
                            "api": "APP_POSTGRES_DSN",
                            "auth": "AUTH_POSTGRES_DSN",
                            "service": "APP_SERVICE_POSTGRES_DSN",
                        }[kind]
                    ]
                )
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE {}").format(
                        sql.Identifier(name), sql.Literal(spec["password"]), sql.Identifier("nexpoly_" + kind)
                    )
                )
        latest = conn.execute("SELECT max(version) FROM governance.schema_migrations").fetchone()[0]
    if latest < "0018":
        apply_identity_cutover(dsn, owner)
    if latest < "0019":
        apply_service_auth_least_privilege(
            dsn, service_roles=["m2_service"], expected_database="nexpoly_m2_dev"
        )
    # Install the canonical, checksum-verified private expansions on this synthetic cluster.
    with psycopg.connect(dsn) as conn:
        latest = conn.execute("SELECT max(version) FROM governance.schema_migrations").fetchone()[0]
        for entry in expected_isolation_ledger("0024_literature_projection"):
            version = entry["version"]
            if version > latest:
                conn.execute((MIGRATIONS_DIR / (version + ".sql")).read_text())
                conn.execute(
                    "INSERT INTO governance.schema_migrations(version,checksum) VALUES (%s,%s)",
                    (version, entry["checksum"]),
                )
    if latest < "0025":
        migrate_memberships(dsn, apply=True)
    if latest < "0026":
        migrate_retrosynthesis(dsn, apply=True)
    workspace_extensions(dsn)


if __name__ == "__main__":
    initialize()
