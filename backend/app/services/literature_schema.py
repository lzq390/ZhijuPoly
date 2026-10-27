"""Explicit offline development opt-in for the pinned 0023/0024 expansion.

This is never called by ordinary bootstrap, expansion or application startup.
The operator stops every writer first; locks and restored-backup seals then
recheck that assertion against the exact database being changed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.literature_contract import (LITERATURE_MIGRATION, LITERATURE_MIGRATION_SHA256,
                                     registered_literature_ledger)

WRITERS = frozenset({"backend", "batch", "monomer_md", "monomer_dft", "polymer_md", "literature_import"})
ROLE = "nexpoly_literature_import"
TABLES = ("literature_generations", "literature_catalog", "literature_snapshots")
# Catalog signature of the unchanged 0024 SQL on PostgreSQL 16. Ownership OIDs
# are deliberately excluded; columns, keys, indexes, trigger bodies and ACLs are not.
CATALOG_SHA256 = "00869e95e41d7489f1fdd1fcb01b346d626d8417f1cd143c8fc7538ca5d8655c"


def _private_json(path: Path) -> tuple[dict, str]:
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Maintenance inputs must not contain linked paths")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or info.st_size > 16 * 1024 * 1024):
            raise ValueError("Maintenance inputs must be private, owned 0600 regular files")
        data = handle.read()
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Maintenance inputs must be JSON objects")
    return value, hashlib.sha256(data).hexdigest()


def _catalog_signature(connection) -> str:
    # pg_get_* pretty-printers otherwise qualify names according to the
    # operator's search_path. Normalize only this metadata check, preserving
    # the connection settings used by historical backup seals.
    previous = connection.execute("SELECT current_setting('search_path') AS value").fetchone()["value"]
    connection.execute("SELECT set_config('search_path','pg_catalog,public',true)")
    try:
        return _catalog_contents_signature(connection)
    finally:
        connection.execute("SELECT set_config('search_path',%s,true)", (previous,))


def _catalog_contents_signature(connection) -> str:
    tables = {}
    for name in TABLES:
        relation = "knowledge." + name
        value = {}
        for key, query in (
            ("relation", "SELECT relkind,relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=%s::regclass"),
            ("columns", """SELECT attname AS name,format_type(atttypid,atttypmod) AS type,
                attnotnull AS required,pg_get_expr(d.adbin,d.adrelid) AS default_expression
                FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
                WHERE a.attrelid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum"""),
            ("constraints", "SELECT conname AS name,pg_get_constraintdef(oid) AS definition,convalidated FROM pg_constraint WHERE conrelid=%s::regclass ORDER BY conname"),
            ("indexes", "SELECT pg_get_indexdef(indexrelid) AS definition,indisvalid,indisready FROM pg_index WHERE indrelid=%s::regclass ORDER BY pg_get_indexdef(indexrelid)"),
            ("triggers", "SELECT pg_get_triggerdef(oid) AS definition,tgenabled FROM pg_trigger WHERE tgrelid=%s::regclass AND NOT tgisinternal ORDER BY tgname"),
            ("acl", """SELECT pg_get_userbyid(a.grantee) AS role,a.privilege_type,a.is_grantable
                FROM pg_class c CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a
                WHERE c.oid=%s::regclass AND a.grantee<>c.relowner ORDER BY 1,2,3"""),
            ("column_acl", "SELECT attname,attacl::text FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 AND attacl IS NOT NULL ORDER BY attname"),
        ):
            value[key] = [dict(row) for row in connection.execute(query, (relation,))]
        tables[name] = value
    functions = [dict(row) for row in connection.execute("""SELECT p.proname,p.prosrc,p.proconfig,
        p.prosecdef,p.provolatile,p.prorettype::regtype::text AS result,l.lanname
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang
        WHERE n.nspname='knowledge' AND p.proname=ANY(%s) ORDER BY p.proname,p.oid""", ([
            "reject_literature_snapshot_mutation", "protect_published_literature_catalog",
            "protect_published_literature_generation"],))]
    data = json.dumps({"tables": tables, "functions": functions}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def validate_literature_schema(connection) -> None:
    """Validate the complete new catalog, including no-op maintenance runs."""
    if _catalog_signature(connection) != CATALOG_SHA256:
        raise ValueError("Literature catalog differs from the pinned 0024 schema")
    role = connection.execute("""SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,
        rolreplication,rolbypassrls FROM pg_roles WHERE rolname=%s""", (ROLE,)).fetchone()
    if role is None or any(role.values()):
        raise ValueError("Literature maintenance group privileges differ from 0024")
    if not connection.execute("SELECT has_schema_privilege(%s,'knowledge','USAGE') AND NOT has_schema_privilege(%s,'knowledge','CREATE') AS allowed", (ROLE, ROLE)).fetchone()["allowed"]:
        raise ValueError("Literature maintenance schema privileges differ from 0024")
    if connection.execute("""SELECT EXISTS(SELECT FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member
        WHERE r.rolname=%s) AS excess""", (ROLE,)).fetchone()["excess"]:
        raise ValueError("Literature maintenance group must not inherit other roles")
    if connection.execute("""WITH owners AS (
        SELECT c.relowner AS owner FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='knowledge' AND c.relname=ANY(%s)
        UNION SELECT p.proowner FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='knowledge' AND p.proname=ANY(%s))
        SELECT EXISTS(SELECT FROM pg_roles r CROSS JOIN owners o WHERE NOT r.rolsuper
          AND (pg_has_role(r.oid,'nexpoly_api','MEMBER') OR pg_has_role(r.oid,'nexpoly_service','MEMBER')
            OR pg_has_role(r.oid,'nexpoly_mutable_audit','MEMBER') OR pg_has_role(r.oid,%s,'MEMBER'))
          AND pg_has_role(r.oid,o.owner,'MEMBER')) AS excess""", (list(TABLES), [
            "reject_literature_snapshot_mutation", "protect_published_literature_catalog",
            "protect_published_literature_generation"], ROLE)).fetchone()["excess"]:
        raise ValueError("Runtime and import identities must not own literature objects")
    for runtime in ("nexpoly_api", "nexpoly_service", "nexpoly_mutable_audit"):
        if connection.execute("SELECT pg_has_role(%s,%s,'MEMBER') AS excess", (runtime, ROLE)).fetchone()["excess"]:
            raise ValueError("Runtime identities must not inherit literature maintenance")
    from app.services.literature_preflight import _literature_schema_problem
    if _literature_schema_problem(connection, role_override="nexpoly_api") is not None:
        raise ValueError("Literature API privileges differ from 0024")


def _quiescence(connection, *, polymer_present: bool) -> None:
    jobs = {
        "online_knowledge.jobs": ("completed", "failed", "cancelled"),
        "md.monomer_md_jobs": ("completed", "failed", "cancelled"),
        "monomer_dft.jobs": ("completed", "failed", "cancelled"),
        "monomer_dft.job_attempts": ("completed", "failed", "cancelled"),
        "polymerization_batch.jobs": ("completed", "completed_with_errors", "failed", "cancelled", "expired"),
    }
    if polymer_present:
        jobs["polymer_md.jobs"] = ("completed", "failed", "cancelled")
    for relation, terminal in jobs.items():
        row = connection.execute(sql.SQL("SELECT count(*) AS n FROM {} WHERE status IS NULL OR status<>ALL(%s)")
                                 .format(sql.Identifier(*relation.split("."))), (list(terminal),)).fetchone()
        if row["n"]:
            raise ValueError("Maintenance requires zero active jobs and attempts")
    for relation in ("md.monomer_md_jobs", "monomer_dft.job_attempts", "polymerization_batch.chunks"):
        row = connection.execute(sql.SQL("SELECT count(*) AS n FROM {} WHERE lease_expires_at>clock_timestamp()")
                                 .format(sql.Identifier(*relation.split(".")))).fetchone()
        if row["n"]:
            raise ValueError("Maintenance requires zero active leases")
    if connection.execute("SELECT count(*) AS n FROM usage_entitlements.operations WHERE state IN ('reserved','dispatching','running','unknown')").fetchone()["n"]:
        raise ValueError("Maintenance requires zero active entitlement operations")


def apply_literature_dev_migration(dsn: str, *, environment: str, backup_receipt: Path,
                                  maintenance_evidence: Path, writers_stopped: bool,
                                  migrations_dir: Path | None = None) -> dict:
    if environment != "dev" or writers_stopped is not True:
        raise ValueError("Literature maintenance requires dev and explicit stopped writers")
    from app.postgres_migrations import MIGRATIONS_DIR, validate_migration_manifest_entries
    from app.auth.cutover import (validate_backup_receipt, backup_tables, backup_state_seal,
                                  private_data_seal, auth_metadata_seal)
    from app.auth.isolation_ledger import (expected_isolation_ledger, database_identity,
        validate_application_migration_ledger, validate_service_members, ENTITLEMENT_VERSION, POLYMER_VERSION)
    from app.auth.schema import validate_isolation_schema
    from app.auth.assets import validate_assets
    from app.auth.usage_backup import usage_backup_seal
    from app.auth.entitlement_backup import entitlement_backup_seal
    from app.services.polymer_md_schema import CHECKSUM, validate_polymer_md_schema

    receipt_document, receipt_sha = _private_json(backup_receipt)
    evidence, evidence_sha = _private_json(maintenance_evidence)
    if (evidence.get("version") != 1 or evidence.get("purpose") != "literature-dev-0024"
            or evidence.get("backup_receipt_sha256") != receipt_sha
            or evidence.get("writers_stopped") is not True
            or evidence.get("submission_disabled") is not True
            or type(evidence.get("active_jobs")) is not int or evidence["active_jobs"] != 0
            or type(evidence.get("active_leases")) is not int or evidence["active_leases"] != 0
            or not isinstance(evidence.get("writer_states"), dict)
            or set(evidence["writer_states"]) != WRITERS
            or any(state not in {"stopped", "not_deployed"} for state in evidence["writer_states"].values())):
        raise ValueError("Exact stopped-writer maintenance evidence is required")
    receipt = validate_backup_receipt(backup_receipt, dsn)
    if receipt != receipt_document or _private_json(backup_receipt)[1] != receipt_sha:
        raise ValueError("Backup receipt changed during validation")
    directory = migrations_dir or MIGRATIONS_DIR
    entries = validate_migration_manifest_entries(directory)
    canonical = registered_literature_ledger()
    if (expected_isolation_ledger(LITERATURE_MIGRATION) != canonical
            or [{"version": entry.version, "checksum": entry.checksum} for entry in entries] != canonical):
        raise ValueError("Maintenance requires the complete canonical release manifest")
    required = {POLYMER_VERSION: (8, CHECKSUM), LITERATURE_MIGRATION: (9, LITERATURE_MIGRATION_SHA256)}
    sources = {}
    for entry in entries:
        if entry.version in required:
            epoch, checksum = required[entry.version]
            data = (directory / (entry.version + ".sql")).read_bytes()
            if (entry.kind != "expand" or entry.epoch != epoch or entry.checksum != checksum
                    or hashlib.sha256(data).hexdigest() != checksum):
                raise ValueError("Optional migration bytes differ from the approved release")
            sources[entry.version] = data.decode()
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as connection:
        if connection.info.server_version // 10000 != 16:
            raise ValueError("Literature development maintenance requires PostgreSQL 16")
        connection.execute("SET LOCAL lock_timeout='5s'")
        connection.execute("SET LOCAL statement_timeout='10min'")
        # Share 0023's advisory lock and block concurrent ledger insertions.
        connection.execute("SELECT pg_advisory_xact_lock(742128925057022)")
        connection.execute("LOCK TABLE governance.schema_migrations IN SHARE ROW EXCLUSIVE MODE")
        identity = database_identity(connection)
        source = dict(connection.execute("SELECT current_database() name,system_identifier::text system_id FROM pg_control_system()").fetchone())
        if source != receipt["source"] or identity != evidence.get("database_identity"):
            raise ValueError("Maintenance evidence belongs to a different database identity")
        role = connection.execute("SELECT rolsuper,rolcreaterole FROM pg_roles WHERE rolname=current_user").fetchone()
        owner = connection.execute("SELECT pg_has_role(current_user,datdba,'USAGE') AS owner FROM pg_database WHERE datname=current_database()").fetchone()
        if not role["rolcreaterole"] or not owner["owner"]:
            raise ValueError("Maintenance requires a database owner with role-creation authority")
        observed = [dict(row) for row in connection.execute("SELECT version,checksum FROM governance.schema_migrations ORDER BY version")]
        if observed not in (canonical[:-2], canonical[:-1], canonical):
            raise ValueError("Maintenance requires the exact canonical 0022, 0023 or 0024 ledger")
        boundary = observed[-1]["version"]
        validate_application_migration_ledger(connection, require_usage=True, require_entitlements=True)
        validate_isolation_schema(connection, boundary)
        validate_service_members(connection)
        relations = set(backup_tables(connection)) | {"usage.events", "usage.attempts"} | {
            "usage_entitlements." + name for name in ("policy_versions", "owner_entitlements", "operations", "operation_events", "request_observations")}
        for relation in sorted(relations):
            connection.execute(sql.SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE").format(sql.Identifier(*relation.split("."))))
        _quiescence(connection, polymer_present=boundary != ENTITLEMENT_VERSION)
        for key, seal in (("backup_state", backup_state_seal), ("business_data", private_data_seal),
                          ("auth_metadata", auth_metadata_seal), ("usage_state", usage_backup_seal),
                          ("entitlement_state", entitlement_backup_seal)):
            if receipt.get(key) != seal(connection):
                raise ValueError("Database changed after the verified backup; create a fresh backup/restore receipt")
        if receipt.get("isolation_schema", {}).get("migration_ledger") != observed:
            raise ValueError("Backup migration ledger differs from the maintained database")
        # Usage is an immutable observation/projection, not execution authority.
        # A historical missing terminal observation must not be rewritten to
        # obtain a migration. Preserve its complete seal and report the count;
        # native jobs/leases and the stopped-writer evidence govern admission.
        nonterminal_usage = connection.execute("SELECT count(*) AS n FROM usage.attempts WHERE status IN ('queued','running')").fetchone()["n"]
        validate_assets(receipt["assets"], verify_source=True)
        if boundary == LITERATURE_MIGRATION:
            validate_polymer_md_schema(connection)
            validate_literature_schema(connection)
            applied = []
        else:
            if connection.execute("SELECT EXISTS(SELECT FROM pg_roles WHERE rolname=%s) AS present", (ROLE,)).fetchone()["present"]:
                raise ValueError("Literature maintenance group already exists; explicit role provenance review required")
            if connection.execute("SELECT EXISTS(SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='knowledge' AND c.relname=ANY(%s)) AS present", (list(TABLES),)).fetchone()["present"]:
                raise ValueError("Unmanaged literature objects exist before 0024")
            applied = []
            if boundary == ENTITLEMENT_VERSION:
                if connection.execute("SELECT to_regnamespace('polymer_md') AS present").fetchone()["present"] is not None:
                    raise ValueError("Unmanaged polymer MD schema exists before 0023")
                applied.append(POLYMER_VERSION)
            else:
                validate_polymer_md_schema(connection)
            applied.append(LITERATURE_MIGRATION)
            for version in applied:
                connection.execute(sources[version])
                connection.execute("INSERT INTO governance.schema_migrations(version,checksum) VALUES(%s,%s)", (version, required[version][1]))
            validate_application_migration_ledger(connection, require_usage=True, require_entitlements=True)
            validate_isolation_schema(connection, LITERATURE_MIGRATION)
            validate_service_members(connection)
            validate_polymer_md_schema(connection)
            validate_literature_schema(connection)
        return {"version": LITERATURE_MIGRATION, "checksum": LITERATURE_MIGRATION_SHA256,
                "previous_boundary": boundary, "applied": applied, "database_identity": identity,
                "nonterminal_usage_observations": nonterminal_usage,
                "backup_receipt_sha256": receipt_sha, "maintenance_evidence_sha256": evidence_sha}
