from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from app.config import PROJECT_ROOT, Settings
from app.migration_policy import (
    MIGRATION_CHECKSUM_PATTERN,
    MIGRATION_VERSION_PATTERN,
    MigrationKind,
    assert_pending_migrations_allowed,
    canonical_migration_checksum,
    validate_migration_manifest_entries,
)
from app.migration_compatibility import require_known_or_exact_forward_ledger
from app.postgres_database import postgres_connection

MIGRATIONS_DIR = PROJECT_ROOT / "backend" / "migrations" / "postgres"
POLYTAO_CONTRACT_VERSION = "0012_drop_polytao_jobs"
POLYTAO_CONTRACT_CHECKSUM = "c59b6f1efe9f926ad135379bd1a7141a7920730fa93c0e802646b1b913511728"
POLYTAO_CONTRACT_GUARD_SCHEMA_VERSION = 1
POLYTAO_CONTRACT_GUARD_ACTOR = "pull-contract-0012"
POLYTAO_CONTRACT_RELATION = "generation.polytao_jobs"
POLYTAO_ACTIVE_STATUSES = frozenset({"pending", "submitted", "running"})
POLYTAO_BUSINESS_STATUSES = frozenset(
    {"pending", "submitted", "running", "completed", "failed", "cancelled"}
)
POLYTAO_GUARDED_JOB_TABLES = (
    "generation.polytao_jobs",
    "md.monomer_md_jobs",
    "online_knowledge.jobs",
)
EXPAND_MIGRATION_LOCK_TIMEOUT = "30s"
EXPAND_MIGRATION_STATEMENT_TIMEOUT = "15min"
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_BARE_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_FULL_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_OPERATION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
_SYSTEM_IDENTIFIER_PATTERN = re.compile(r"^[0-9]{1,20}$")
_MAX_CONTRACT_GUARD_JSON_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class MigrationResult:
    version: str
    checksum: str
    applied: bool


@dataclass(frozen=True, slots=True)
class PolytaoContractGuard:
    document: dict[str, Any]
    operation_id: str
    database_name: str
    system_identifier: str
    release_sha: str
    namespace_oid: int
    relation_oid: int
    ledger: tuple[tuple[str, str], ...]
    archive_evidence: dict[str, Any]
    marker_sha256: str
    audit_manifest_sha256: str


def migration_checksum(path: Path) -> str:
    return canonical_migration_checksum(path)


def _canonical_json_bytes(value: object, *, default_to_string: bool = False) -> bytes:
    kwargs: dict[str, Any] = {
        "sort_keys": True,
        "separators": (",", ":"),
        "ensure_ascii": True,
        "allow_nan": False,
    }
    if default_to_string:
        kwargs["default"] = str
    return json.dumps(value, **kwargs).encode("utf-8")


def _canonical_json_sha256(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json_bytes(value)).hexdigest()}"


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Contract guard JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _require_exact_fields(
    value: object,
    fields: set[str],
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label} has an invalid shape")
    return value


def _require_prefixed_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase sha256 digest")
    return value


def _require_bare_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _BARE_SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 checksum")
    return value


def _require_oid(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**32:
        raise ValueError(f"{label} must be a positive PostgreSQL OID")
    return value


def _validate_polytao_archive_evidence(value: object) -> dict[str, Any]:
    evidence = _require_exact_fields(
        value,
        {
            "schema_version",
            "row_count",
            "status_counts",
            "rows_sha256",
            "schema_sha256",
            "structure_counts",
        },
        label="Contract guard archive_evidence",
    )
    row_count = evidence["row_count"]
    status_counts = evidence["status_counts"]
    if (
        evidence["schema_version"] != 2
        or isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count < 0
        or not isinstance(status_counts, dict)
        or any(
            not isinstance(status, str)
            or status not in POLYTAO_BUSINESS_STATUSES
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
            for status, count in status_counts.items()
        )
        or sum(status_counts.values()) != row_count
        or any(status in POLYTAO_ACTIVE_STATUSES for status in status_counts)
    ):
        raise ValueError(
            "Contract guard archive_evidence must seal the complete active-zero "
            "PolyTAO business-row set"
        )
    _require_bare_sha256(
        evidence["rows_sha256"],
        label="Contract guard archive_evidence rows_sha256",
    )
    _require_bare_sha256(
        evidence["schema_sha256"],
        label="Contract guard archive_evidence schema_sha256",
    )
    structure_counts = evidence["structure_counts"]
    if (
        not isinstance(structure_counts, dict)
        or set(structure_counts)
        != {"columns", "indexes", "constraints", "triggers"}
        or any(
            isinstance(count, bool) or not isinstance(count, int) or count < 0
            for count in structure_counts.values()
        )
    ):
        raise ValueError(
            "Contract guard archive_evidence has invalid structure counts"
        )
    return evidence


def _load_polytao_contract_guard(
    guard_json: str | None,
    guard_sha256: str | None,
    *,
    expected_ledger: list[tuple[str, str]],
) -> PolytaoContractGuard:
    if guard_json is None or guard_sha256 is None:
        raise ValueError(
            "The checksum-pinned 0012 contract requires a sealed transaction guard"
        )
    if not isinstance(guard_json, str):
        raise ValueError("Contract guard JSON must be text")
    encoded = guard_json.encode("utf-8")
    if not encoded or len(encoded) > _MAX_CONTRACT_GUARD_JSON_BYTES:
        raise ValueError("Contract guard JSON has an invalid size")
    expected_guard_sha256 = _require_prefixed_sha256(
        guard_sha256,
        label="Contract guard detached digest",
    )
    actual_guard_sha256 = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    if actual_guard_sha256 != expected_guard_sha256:
        raise ValueError("Contract guard detached digest does not match its JSON")
    try:
        document = json.loads(guard_json, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("Contract guard is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("Contract guard must be a JSON object")
    if _canonical_json_bytes(document) != encoded:
        raise ValueError("Contract guard JSON must use exact canonical encoding")

    guard = _require_exact_fields(
        document,
        {
            "schema_version",
            "contract",
            "maintenance",
            "database",
            "release_sha",
            "ledger",
            "relation",
            "archive_evidence",
            "archive_evidence_sha256",
            "deployment_control",
            "active_jobs",
        },
        label="Contract guard",
    )
    if guard["schema_version"] != POLYTAO_CONTRACT_GUARD_SCHEMA_VERSION:
        raise ValueError("Contract guard schema_version is unsupported")

    contract = _require_exact_fields(
        guard["contract"],
        {"version", "checksum"},
        label="Contract guard contract identity",
    )
    if contract != {
        "version": POLYTAO_CONTRACT_VERSION,
        "checksum": POLYTAO_CONTRACT_CHECKSUM,
    }:
        raise ValueError("Contract guard identifies a different migration contract")

    maintenance = _require_exact_fields(
        guard["maintenance"],
        {"operation_id", "marker_sha256", "audit_manifest_sha256"},
        label="Contract guard maintenance identity",
    )
    operation_id = maintenance["operation_id"]
    if (
        not isinstance(operation_id, str)
        or _OPERATION_ID_PATTERN.fullmatch(operation_id) is None
    ):
        raise ValueError("Contract guard operation_id is invalid")
    marker_sha256 = _require_prefixed_sha256(
        maintenance["marker_sha256"],
        label="Contract guard marker_sha256",
    )
    audit_manifest_sha256 = _require_prefixed_sha256(
        maintenance["audit_manifest_sha256"],
        label="Contract guard audit_manifest_sha256",
    )

    database = _require_exact_fields(
        guard["database"],
        {"name", "system_identifier"},
        label="Contract guard database identity",
    )
    database_name = database["name"]
    system_identifier = database["system_identifier"]
    if (
        not isinstance(database_name, str)
        or not database_name
        or len(database_name.encode("utf-8")) > 63
        or "\x00" in database_name
    ):
        raise ValueError("Contract guard database name is invalid")
    if (
        not isinstance(system_identifier, str)
        or _SYSTEM_IDENTIFIER_PATTERN.fullmatch(system_identifier) is None
    ):
        raise ValueError("Contract guard database system_identifier is invalid")

    release_sha = guard["release_sha"]
    if not isinstance(release_sha, str) or _FULL_SHA_PATTERN.fullmatch(release_sha) is None:
        raise ValueError("Contract guard release_sha must be a full lowercase Git SHA")

    raw_ledger = guard["ledger"]
    if not isinstance(raw_ledger, list):
        raise ValueError("Contract guard ledger must be a list")
    normalized_ledger: list[tuple[str, str]] = []
    for record in raw_ledger:
        ledger_record = _require_exact_fields(
            record,
            {"version", "checksum"},
            label="Contract guard ledger record",
        )
        version = ledger_record["version"]
        checksum = ledger_record["checksum"]
        if (
            not isinstance(version, str)
            or MIGRATION_VERSION_PATTERN.fullmatch(version) is None
            or not isinstance(checksum, str)
            or MIGRATION_CHECKSUM_PATTERN.fullmatch(checksum) is None
        ):
            raise ValueError("Contract guard ledger record is invalid")
        normalized_ledger.append((version, checksum))
    if normalized_ledger != expected_ledger:
        raise ValueError(
            "Contract guard ledger is not the exact canonical 0012 predecessor chain"
        )

    archive_evidence = _validate_polytao_archive_evidence(
        guard["archive_evidence"]
    )
    archive_evidence_sha256 = _require_prefixed_sha256(
        guard["archive_evidence_sha256"],
        label="Contract guard archive_evidence_sha256",
    )
    if _canonical_json_sha256(archive_evidence) != archive_evidence_sha256:
        raise ValueError(
            "Contract guard archive_evidence_sha256 does not match archive_evidence"
        )

    relation = _require_exact_fields(
        guard["relation"],
        {
            "qualified_name",
            "namespace_oid",
            "relation_oid",
            "rows_sha256",
            "schema_sha256",
        },
        label="Contract guard relation identity",
    )
    namespace_oid = _require_oid(
        relation["namespace_oid"],
        label="Contract guard relation namespace_oid",
    )
    relation_oid = _require_oid(
        relation["relation_oid"],
        label="Contract guard relation relation_oid",
    )
    if (
        relation["qualified_name"] != POLYTAO_CONTRACT_RELATION
        or relation["rows_sha256"] != archive_evidence["rows_sha256"]
        or relation["schema_sha256"] != archive_evidence["schema_sha256"]
    ):
        raise ValueError(
            "Contract guard relation identity differs from its archive evidence"
        )

    deployment_control = _require_exact_fields(
        guard["deployment_control"],
        {
            "control_key",
            "drain_enabled",
            "reason",
            "release_sha",
            "activated_by",
        },
        label="Contract guard deployment_control",
    )
    if deployment_control != {
        "control_key": "production",
        "drain_enabled": True,
        "reason": f"0012 maintenance {operation_id}",
        "release_sha": release_sha,
        "activated_by": POLYTAO_CONTRACT_GUARD_ACTOR,
    }:
        raise ValueError(
            "Contract guard is not bound to the current operation-owned drain"
        )

    active_jobs = _require_exact_fields(
        guard["active_jobs"],
        set(POLYTAO_GUARDED_JOB_TABLES),
        label="Contract guard active_jobs",
    )
    if any(
        isinstance(count, bool) or not isinstance(count, int) or count != 0
        for count in active_jobs.values()
    ):
        raise ValueError("Contract guard active_jobs must prove exact zero")

    return PolytaoContractGuard(
        document=guard,
        operation_id=operation_id,
        database_name=database_name,
        system_identifier=system_identifier,
        release_sha=release_sha,
        namespace_oid=namespace_oid,
        relation_oid=relation_oid,
        ledger=tuple(normalized_ledger),
        archive_evidence=archive_evidence,
        marker_sha256=marker_sha256,
        audit_manifest_sha256=audit_manifest_sha256,
    )


def ensure_migration_table(connection) -> None:
    connection.execute("CREATE SCHEMA IF NOT EXISTS governance")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS governance.schema_migrations (
          version text PRIMARY KEY,
          checksum text NOT NULL,
          applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def applied_migrations(connection) -> dict[str, str]:
    ensure_migration_table(connection)
    rows = connection.execute(
        "SELECT version, checksum FROM governance.schema_migrations ORDER BY version, checksum"
    ).fetchall()
    applied: dict[str, str] = {}
    duplicates: set[str] = set()
    for row in rows:
        version = str(row["version"])
        checksum = str(row["checksum"])
        if MIGRATION_VERSION_PATTERN.fullmatch(version) is None:
            raise RuntimeError(
                f"Migration ledger contains an invalid version identifier: {version!r}"
            )
        if MIGRATION_CHECKSUM_PATTERN.fullmatch(checksum) is None:
            raise RuntimeError(
                f"Migration ledger contains an invalid checksum for {version}"
            )
        if version in applied:
            duplicates.add(version)
            continue
        applied[version] = checksum
    if duplicates:
        raise RuntimeError(
            "Migration ledger contains duplicate versions: "
            + ", ".join(sorted(duplicates))
        )
    return applied


def _polytao_contract_archive_evidence(connection) -> dict[str, Any]:
    rows = [
        row["payload"]
        for row in connection.execute(
            """
            SELECT to_jsonb(jobs) AS payload
            FROM generation.polytao_jobs AS jobs
            ORDER BY job_id::text
            """
        ).fetchall()
    ]
    statuses = {
        str(row["status"]): int(row["count"])
        for row in connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM generation.polytao_jobs
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
    }
    columns = [
        dict(row)
        for row in connection.execute(
            """
            SELECT column_name, ordinal_position, data_type, udt_schema, udt_name,
                   is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'generation' AND table_name = 'polytao_jobs'
            ORDER BY ordinal_position
            """
        ).fetchall()
    ]
    indexes = [
        dict(row)
        for row in connection.execute(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'generation' AND tablename = 'polytao_jobs'
            ORDER BY indexname
            """
        ).fetchall()
    ]
    constraints = [
        dict(row)
        for row in connection.execute(
            """
            SELECT constraint_row.conname AS name,
                   constraint_row.contype AS type,
                   constraint_row.condeferrable AS deferrable,
                   constraint_row.condeferred AS initially_deferred,
                   constraint_row.convalidated AS validated,
                   pg_get_constraintdef(constraint_row.oid, true) AS definition
            FROM pg_constraint AS constraint_row
            JOIN pg_class AS relation ON relation.oid = constraint_row.conrelid
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'generation'
              AND relation.relname = 'polytao_jobs'
            ORDER BY constraint_row.conname
            """
        ).fetchall()
    ]
    triggers = [
        dict(row)
        for row in connection.execute(
            """
            SELECT trigger_row.tgname AS name,
                   trigger_row.tgenabled AS enabled,
                   pg_get_triggerdef(trigger_row.oid, true) AS definition
            FROM pg_trigger AS trigger_row
            JOIN pg_class AS relation ON relation.oid = trigger_row.tgrelid
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'generation'
              AND relation.relname = 'polytao_jobs'
              AND NOT trigger_row.tgisinternal
            ORDER BY trigger_row.tgname
            """
        ).fetchall()
    ]
    structure = {
        "columns": columns,
        "indexes": indexes,
        "constraints": constraints,
        "triggers": triggers,
    }
    return {
        "schema_version": 2,
        "row_count": len(rows),
        "status_counts": statuses,
        "rows_sha256": hashlib.sha256(
            _canonical_json_bytes(rows, default_to_string=True)
        ).hexdigest(),
        "schema_sha256": hashlib.sha256(
            _canonical_json_bytes(structure, default_to_string=True)
        ).hexdigest(),
        "structure_counts": {
            name: len(records) for name, records in structure.items()
        },
    }


def _polytao_relation_identity(connection) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT namespace.oid::bigint AS namespace_oid,
               relation.oid::bigint AS relation_oid,
               relation.relkind
        FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'generation'
          AND relation.relname = 'polytao_jobs'
        """
    ).fetchone()
    if row is None or row["relkind"] != "r":
        raise RuntimeError(
            "Contract guard requires generation.polytao_jobs to be the sealed regular table"
        )
    return {
        "namespace_oid": int(row["namespace_oid"]),
        "relation_oid": int(row["relation_oid"]),
    }


def _lock_polytao_contract_state(connection, *, target_present: bool) -> None:
    """Acquire every writer-conflicting lock in one documented fixed order."""

    connection.execute("SET LOCAL lock_timeout = '10s'")
    connection.execute(
        "LOCK TABLE governance.schema_migrations IN SHARE ROW EXCLUSIVE MODE"
    )
    connection.execute(
        "LOCK TABLE governance.deployment_control IN SHARE ROW EXCLUSIVE MODE"
    )
    if target_present:
        if (
            connection.execute(
                "SELECT to_regclass('generation.polytao_jobs') AS relation"
            ).fetchone()["relation"]
            is None
        ):
            raise RuntimeError(
                "Contract guard target generation.polytao_jobs is missing"
            )
        connection.execute(
            "LOCK TABLE generation.polytao_jobs IN ACCESS EXCLUSIVE MODE"
        )
    connection.execute(
        "LOCK TABLE md.monomer_md_jobs IN SHARE ROW EXCLUSIVE MODE"
    )
    connection.execute(
        "LOCK TABLE online_knowledge.jobs IN SHARE ROW EXCLUSIVE MODE"
    )


def _locked_contract_ledger(connection) -> list[tuple[str, str]]:
    return [
        (str(row["version"]), str(row["checksum"]))
        for row in connection.execute(
            """
            SELECT version, checksum
            FROM governance.schema_migrations
            ORDER BY version, checksum
            """
        ).fetchall()
    ]


def _require_no_event_triggers(connection) -> None:
    count = int(
        connection.execute(
            "SELECT COUNT(*) AS count FROM pg_catalog.pg_event_trigger"
        ).fetchone()["count"]
    )
    if count != 0:
        raise RuntimeError(
            "Contract guard requires an empty PostgreSQL event-trigger inventory"
        )


def _verify_polytao_contract_database_identity(
    connection,
    guard: PolytaoContractGuard,
) -> None:
    row = connection.execute(
        """
        SELECT current_database() AS database_name,
               system_identifier::text AS system_identifier
        FROM pg_catalog.pg_control_system()
        """
    ).fetchone()
    if (
        row is None
        or str(row["database_name"]) != guard.database_name
        or str(row["system_identifier"]) != guard.system_identifier
    ):
        raise RuntimeError(
            "Contract guard database name or cluster system identifier changed"
        )


def _verify_polytao_contract_drain(
    connection,
    guard: PolytaoContractGuard,
) -> None:
    row = connection.execute(
        """
        SELECT control_key, drain_enabled, reason, release_sha, activated_by
        FROM governance.deployment_control
        WHERE control_key = 'production'
        """
    ).fetchone()
    expected = guard.document["deployment_control"]
    if row is None or {
        "control_key": str(row["control_key"]),
        "drain_enabled": bool(row["drain_enabled"]),
        "reason": row["reason"],
        "release_sha": row["release_sha"],
        "activated_by": row["activated_by"],
    } != expected:
        raise RuntimeError(
            "Contract guard operation-owned deployment drain changed"
        )


def _active_job_counts(connection, *, target_present: bool) -> dict[str, int]:
    active_statuses = sorted(POLYTAO_ACTIVE_STATUSES)
    counts: dict[str, int] = {
        "generation.polytao_jobs": 0,
        "md.monomer_md_jobs": 0,
        "online_knowledge.jobs": 0,
    }
    if target_present:
        counts["generation.polytao_jobs"] = int(
            connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM generation.polytao_jobs
                WHERE status = ANY(%s)
                """,
                (active_statuses,),
            ).fetchone()["count"]
        )
    counts["md.monomer_md_jobs"] = int(
        connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM md.monomer_md_jobs
            WHERE status = ANY(%s)
            """,
            (active_statuses,),
        ).fetchone()["count"]
    )
    counts["online_knowledge.jobs"] = int(
        connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM online_knowledge.jobs
            WHERE status = ANY(%s)
            """,
            (active_statuses,),
        ).fetchone()["count"]
    )
    return counts


def _verify_polytao_contract_guard(
    connection,
    guard: PolytaoContractGuard,
) -> None:
    _require_no_event_triggers(connection)
    _lock_polytao_contract_state(connection, target_present=True)
    _verify_polytao_contract_database_identity(connection, guard)
    if _locked_contract_ledger(connection) != list(guard.ledger):
        raise RuntimeError(
            "Contract guard migration ledger changed before 0012 execution"
        )
    _verify_polytao_contract_drain(connection, guard)
    relation = _polytao_relation_identity(connection)
    if (
        relation["namespace_oid"] != guard.namespace_oid
        or relation["relation_oid"] != guard.relation_oid
    ):
        raise RuntimeError(
            "Contract guard relation or namespace OID changed before 0012 execution"
        )
    current_evidence = _polytao_contract_archive_evidence(connection)
    if current_evidence != guard.archive_evidence:
        raise RuntimeError(
            "Contract guard PolyTAO schema or business-row content changed "
            "after archival"
        )
    if _active_job_counts(connection, target_present=True) != guard.document[
        "active_jobs"
    ]:
        raise RuntimeError(
            "Contract guard observed active database jobs before 0012 execution"
        )


def _verify_applied_polytao_contract_guard(
    connection,
    guard: PolytaoContractGuard,
    *,
    expected_ledger: list[tuple[str, str]],
) -> None:
    """Verify the exact post-state for an idempotent response-loss retry."""

    _lock_polytao_contract_state(connection, target_present=False)
    _require_no_event_triggers(connection)
    _verify_polytao_contract_database_identity(connection, guard)
    if _locked_contract_ledger(connection) != expected_ledger:
        raise RuntimeError(
            "Contract guard migration ledger changed after 0012 execution"
        )
    _verify_polytao_contract_drain(connection, guard)
    relation_state = connection.execute(
        """
        SELECT to_regclass('generation.polytao_jobs') AS relation,
               to_regnamespace('generation') AS namespace
        """
    ).fetchone()
    if (
        relation_state is None
        or relation_state["relation"] is not None
        or relation_state["namespace"] is not None
    ):
        raise RuntimeError(
            "Applied 0012 ledger does not match the contract's absent generation schema"
        )
    if _active_job_counts(connection, target_present=False) != guard.document[
        "active_jobs"
    ]:
        raise RuntimeError(
            "Contract guard observed active database jobs after 0012 execution"
        )


def database_is_fresh_for_bootstrap(connection) -> bool:
    """Return true only for a pristine cluster or the explicit empty-ledger case.

    Merely having no tables is insufficient: a partially initialized schema,
    view, sequence, custom type, or routine is evidence that the database is
    already owned.  ``public`` may remain empty, while ``governance`` is only
    tolerated when its sole application relation is an empty, regular
    ``schema_migrations`` table.
    """

    state = connection.execute(
        """
        SELECT
          to_regclass('governance.schema_migrations') AS ledger_relation,
          EXISTS (
            SELECT 1
            FROM pg_namespace
            WHERE nspname = 'governance'
          ) AS governance_schema_exists,
          EXISTS (
            SELECT 1
            FROM pg_namespace
            WHERE nspname NOT IN ('pg_catalog', 'information_schema', 'public', 'governance')
              AND nspname !~ '^pg_(toast|temp)(_|$)'
          ) AS unexpected_schema_exists,
          EXISTS (
            SELECT 1
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname IN ('public', 'governance')
              AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
              AND NOT (
                n.nspname = 'governance'
                AND c.relname = 'schema_migrations'
                AND c.relkind = 'r'
              )
          ) AS application_relation_exists,
          EXISTS (
            SELECT 1
            FROM pg_type AS t
            JOIN pg_namespace AS n ON n.oid = t.typnamespace
            WHERE n.nspname IN ('public', 'governance')
              AND t.typtype IN ('c', 'd', 'e', 'r', 'm')
              AND NOT EXISTS (
                SELECT 1
                FROM pg_class AS ledger
                WHERE ledger.oid = t.typrelid
                  AND n.nspname = 'governance'
                  AND ledger.relname = 'schema_migrations'
                  AND ledger.relkind = 'r'
              )
          ) AS application_type_exists,
          EXISTS (
            SELECT 1
            FROM pg_proc AS p
            JOIN pg_namespace AS n ON n.oid = p.pronamespace
            WHERE n.nspname IN ('public', 'governance')
          ) AS application_routine_exists
        """
    ).fetchone()
    if state is None:
        return False

    ledger_relation = state["ledger_relation"]
    governance_schema_exists = bool(state["governance_schema_exists"])
    if governance_schema_exists != (ledger_relation is not None):
        # An empty governance schema is still a partially initialized managed
        # schema; conversely, a relation cannot validly exist without it.
        return False

    if ledger_relation is not None:
        recorded = connection.execute(
            "SELECT 1 FROM governance.schema_migrations LIMIT 1"
        ).fetchone()
        if recorded is not None:
            return False

    return not any(
        bool(state[field])
        for field in (
            "unexpected_schema_exists",
            "application_relation_exists",
            "application_type_exists",
            "application_routine_exists",
        )
    )


def _set_expand_migration_timeouts(connection) -> None:
    """Bound one migration transaction without changing connection defaults."""

    connection.execute(
        f"SET LOCAL lock_timeout = '{EXPAND_MIGRATION_LOCK_TIMEOUT}'"
    )
    connection.execute(
        f"SET LOCAL statement_timeout = '{EXPAND_MIGRATION_STATEMENT_TIMEOUT}'"
    )


def apply_postgres_migrations(
    dsn: str,
    migrations_dir: Path = MIGRATIONS_DIR,
    *,
    allowed_kinds: set[MigrationKind] | None = None,
    allow_contract_on_fresh_database: bool = False,
    defer_trailing_contracts: bool = False,
    restricted_contract: tuple[str, str] | None = None,
    contract_guard_json: str | None = None,
    contract_guard_sha256: str | None = None,
) -> list[MigrationResult]:
    if allowed_kinds is None:
        raise ValueError(
            "Migration callers must select an explicit policy; use CLI --mode bootstrap for a fresh database"
        )
    if restricted_contract is None and (
        contract_guard_json is not None or contract_guard_sha256 is not None
    ):
        raise ValueError(
            "A transaction guard may be supplied only to the restricted 0012 contract"
        )
    migration_paths = sorted(migrations_dir.glob("*.sql"))
    if not migration_paths:
        raise FileNotFoundError(f"No Postgres migrations found in {migrations_dir}")
    entries = validate_migration_manifest_entries(migrations_dir)
    entries_by_version = {entry.version: entry for entry in entries}
    migration_kinds = {entry.version: entry.kind for entry in entries}
    if [path.stem for path in migration_paths] != [entry.version for entry in entries]:
        raise RuntimeError("Validated migration entries do not match migration SQL ordering")

    restricted_version: str | None = None
    restricted_target_index: int | None = None
    expected_guard_ledger: list[tuple[str, str]] = []
    contract_guard: PolytaoContractGuard | None = None
    if restricted_contract is not None:
        if restricted_contract != (
            POLYTAO_CONTRACT_VERSION,
            POLYTAO_CONTRACT_CHECKSUM,
        ):
            raise ValueError("Only the checksum-pinned 0012 contract operation is supported")
        if allow_contract_on_fresh_database or defer_trailing_contracts:
            raise ValueError("A restricted contract cannot use bootstrap or deferred-contract policy")
        restricted_version, expected_checksum = restricted_contract
        entry = entries_by_version.get(restricted_version)
        if (
            entry is None
            or entry.kind != "contract"
            or entry.checksum != expected_checksum
        ):
            raise RuntimeError(
                f"Restricted contract {restricted_version} is absent or has an unexpected canonical checksum"
            )
        if allowed_kinds != {"contract"}:
            raise ValueError("A restricted contract operation may allow only contract migrations")
        restricted_target_index = next(
            index
            for index, manifest_entry in enumerate(entries)
            if manifest_entry.version == restricted_version
        )
        expected_guard_ledger = [
            (manifest_entry.version, str(manifest_entry.checksum))
            for manifest_entry in entries[:restricted_target_index]
        ]
        contract_guard = _load_polytao_contract_guard(
            contract_guard_json,
            contract_guard_sha256,
            expected_ledger=expected_guard_ledger,
        )

    results: list[MigrationResult] = []
    with postgres_connection(dsn) as connection:
        fresh_bootstrap = (
            allow_contract_on_fresh_database
            and database_is_fresh_for_bootstrap(connection)
        )
        if (
            restricted_contract is None
            and "contract" in allowed_kinds
            and not fresh_bootstrap
        ):
            raise ValueError(
                "Generic contract execution is forbidden; use the checksum-pinned 0012 operation"
            )
        applied = applied_migrations(connection)

        require_known_or_exact_forward_ledger(
            applied,
            {
                entry.version: str(entry.checksum)
                for entry in entries
            },
        )

        # Validate the entire known ledger before planning any migration SQL.
        # In particular, a database cannot enter a later epoch using only a
        # matching migration name: every dependency is checksum-bound.
        for entry in entries:
            existing_checksum = applied.get(entry.version)
            if existing_checksum is not None and existing_checksum != entry.checksum:
                raise RuntimeError(
                    f"Migration {entry.version} was already applied with checksum {existing_checksum}, "
                    f"but local checksum is {entry.checksum}."
                )

        if restricted_version is not None:
            if restricted_target_index is None:  # pragma: no cover - defensive
                raise RuntimeError("Restricted contract target index is unavailable")
            canonical_prefix = [
                entry.version for entry in entries[:restricted_target_index]
            ]
            expected_ledger = set(canonical_prefix)
            if restricted_version in applied:
                expected_ledger.add(restricted_version)
            actual_ledger = set(applied)
            if actual_ledger != expected_ledger:
                missing_prerequisites = sorted(expected_ledger.difference(actual_ledger))
                unexpected_versions = sorted(actual_ledger.difference(expected_ledger))
                detail = []
                if missing_prerequisites:
                    detail.append("missing " + ", ".join(missing_prerequisites))
                if unexpected_versions:
                    detail.append("unexpected " + ", ".join(unexpected_versions))
                raise RuntimeError(
                    f"Restricted contract {restricted_version} requires the exact canonical "
                    f"ledger prefix through {'itself' if restricted_version in applied else 'its predecessor'}: "
                    + "; ".join(detail)
                )

        effective_allowed_kinds: set[MigrationKind] | None = None
        deferred_versions: set[str] = set()
        if allowed_kinds is not None:
            effective_allowed_kinds = set(allowed_kinds)
            if fresh_bootstrap:
                effective_allowed_kinds.add("contract")
            pending_versions = [
                entry.version
                for entry in entries
                if entry.version not in applied
                and (restricted_version is None or entry.version == restricted_version)
            ]
            if defer_trailing_contracts:
                deferred = [
                    version
                    for version in pending_versions
                    if migration_kinds[version] not in effective_allowed_kinds
                ]
                if any(migration_kinds[version] != "contract" for version in deferred):
                    raise RuntimeError("Only contract migrations may be deferred")
                deferred_versions.update(deferred)
                manifest_schema_version = entries[0].manifest_schema_version if entries else 1
                if deferred and manifest_schema_version == 1:
                    first_deferred = pending_versions.index(deferred[0])
                    if any(
                        migration_kinds[version] in effective_allowed_kinds
                        for version in pending_versions[first_deferred + 1 :]
                    ):
                        raise RuntimeError(
                            "Deferred contract migrations must form the trailing pending migration suffix"
                        )
            else:
                assert_pending_migrations_allowed(
                    pending_versions,
                    migration_kinds,
                    effective_allowed_kinds,
                )

        # Plan every action and dependency against a simulated ledger before
        # executing the first migration. This guarantees that an epoch-2
        # expansion cannot partly mutate a database whose required contract is
        # absent or checksum-mismatched.
        simulated = dict(applied)
        will_apply: set[str] = set()
        for entry in entries:
            if entry.version in simulated:
                pass
            elif restricted_version is not None and entry.version != restricted_version:
                continue
            elif entry.version in deferred_versions:
                continue
            elif effective_allowed_kinds is not None and entry.kind not in effective_allowed_kinds:
                continue
            else:
                will_apply.add(entry.version)

            if entry.version in simulated or entry.version in will_apply:
                for requirement in entry.requires_contracts:
                    actual = simulated.get(requirement.version)
                    if actual != requirement.checksum:
                        detail = "missing" if actual is None else f"checksum {actual}"
                        raise RuntimeError(
                            f"Migration {entry.version} requires approved contract "
                            f"{requirement.version} at checksum {requirement.checksum}; found {detail}."
                        )
                if entry.version in will_apply:
                    simulated[entry.version] = str(entry.checksum)

        if restricted_version is not None and restricted_version in applied:
            if contract_guard is None:  # pragma: no cover - validated before connect
                raise RuntimeError("Restricted contract guard is unavailable")
            expected_applied_ledger = [
                *expected_guard_ledger,
                (restricted_version, POLYTAO_CONTRACT_CHECKSUM),
            ]
            # End the read-only planning transaction so that the verification,
            # locks, exact post-state check, and response-loss retry decision
            # have one explicit top-level transaction boundary.
            connection.commit()
            with connection.transaction():
                _verify_applied_polytao_contract_guard(
                    connection,
                    contract_guard,
                    expected_ledger=expected_applied_ledger,
                )

        for path in migration_paths:
            version = path.stem
            checksum = str(entries_by_version[version].checksum)
            existing_checksum = applied.get(version)
            if existing_checksum is not None:
                results.append(MigrationResult(version=version, checksum=checksum, applied=False))
                continue

            if version not in will_apply:
                results.append(MigrationResult(version=version, checksum=checksum, applied=False))
                continue

            sql = path.read_text(encoding="utf-8")
            if version == restricted_version:
                if contract_guard is None:  # pragma: no cover - validated before connect
                    raise RuntimeError("Restricted contract guard is unavailable")
                # Planning above is deliberately read-only. Commit it before
                # opening the sole destructive transaction so ACCESS
                # EXCLUSIVE, all fresh guard checks, the unchanged canonical
                # SQL, and the ledger INSERT commit or roll back together.
                connection.commit()
                with connection.transaction():
                    _set_expand_migration_timeouts(connection)
                    _verify_polytao_contract_guard(connection, contract_guard)
                    connection.execute(sql)
                    connection.execute(
                        """
                        INSERT INTO governance.schema_migrations (version, checksum)
                        VALUES (%s, %s)
                        """,
                        (version, checksum),
                    )
                    _verify_applied_polytao_contract_guard(
                        connection,
                        contract_guard,
                        expected_ledger=[
                            *expected_guard_ledger,
                            (version, checksum),
                        ],
                    )
            else:
                with connection.transaction():
                    _set_expand_migration_timeouts(connection)
                    connection.execute(sql)
                    connection.execute(
                        """
                        INSERT INTO governance.schema_migrations (version, checksum)
                        VALUES (%s, %s)
                        """,
                        (version, checksum),
                    )
            results.append(MigrationResult(version=version, checksum=checksum, applied=True))
    return results


def apply_polytao_contract_migration(
    dsn: str,
    migrations_dir: Path = MIGRATIONS_DIR,
    *,
    guard_json: str | None = None,
    guard_sha256: str | None = None,
) -> list[MigrationResult]:
    """Apply only the reviewed 0012 contract at its immutable checksum."""

    return apply_postgres_migrations(
        dsn,
        migrations_dir,
        allowed_kinds={"contract"},
        restricted_contract=(POLYTAO_CONTRACT_VERSION, POLYTAO_CONTRACT_CHECKSUM),
        contract_guard_json=guard_json,
        contract_guard_sha256=guard_sha256,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply PolyProp Postgres governance migrations.")
    parser.add_argument("--dsn", default=Settings().app_postgres_dsn)
    parser.add_argument("--migrations-dir", type=Path, default=MIGRATIONS_DIR)
    parser.add_argument(
        "--contract-guard-json",
        help="Exact canonical, non-secret JSON evidence for the 0012 transaction guard.",
    )
    parser.add_argument(
        "--contract-guard-sha256",
        help="Detached sha256:<hex> digest of --contract-guard-json.",
    )
    parser.add_argument(
        "--mode",
        choices=(
            "bootstrap",
            "bootstrap-expand",
            "restore-expand",
            "expand",
            "contract-0012",
        ),
        default="expand",
        help=(
            "bootstrap initializes a fresh database; bootstrap-expand is the one-time "
            "controller cutover mode that defers a trailing contract suffix; restore-expand "
            "uses the same policy only for isolated backup verification; automated releases "
            "use expand and also defer a trailing contract suffix; contract-0012 is the only "
            "exposed destructive operation and is checksum-pinned."
        ),
    )
    args = parser.parse_args()

    if args.mode == "contract-0012":
        if args.contract_guard_json is None or args.contract_guard_sha256 is None:
            parser.error(
                "--mode contract-0012 requires --contract-guard-json and "
                "--contract-guard-sha256"
            )
        results = apply_polytao_contract_migration(
            args.dsn,
            args.migrations_dir,
            guard_json=args.contract_guard_json,
            guard_sha256=args.contract_guard_sha256,
        )
        for result in results:
            status = "applied" if result.applied else "skipped"
            print(f"{result.version}\t{status}\t{result.checksum}")
        return
    if args.contract_guard_json is not None or args.contract_guard_sha256 is not None:
        parser.error("Contract guard arguments require --mode contract-0012")

    allowed_kinds: set[MigrationKind]
    if args.mode in {"bootstrap", "bootstrap-expand", "restore-expand"}:
        allowed_kinds = {"baseline", "expand"}
    else:
        allowed_kinds = {"expand"}
    results = apply_postgres_migrations(
        args.dsn,
        args.migrations_dir,
        allowed_kinds=allowed_kinds,
        allow_contract_on_fresh_database=args.mode == "bootstrap",
        defer_trailing_contracts=args.mode in {
            "bootstrap-expand",
            "restore-expand",
            "expand",
        },
    )
    for result in results:
        status = "applied" if result.applied else "skipped"
        print(f"{result.version}\t{status}\t{result.checksum}")


if __name__ == "__main__":
    main()
