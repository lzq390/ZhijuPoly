"""Optional feature readiness; failure never closes the legacy knowledge API."""
from __future__ import annotations

from app.services.literature_normalizer import SUPPORTED_NORMALIZER_VERSIONS

TABLES = ("literature_generations", "literature_catalog", "literature_snapshots")


def _literature_schema_problem(connection, *, role_override: str | None = None) -> str | None:
    tables = connection.execute(
        "SELECT name,to_regclass('knowledge.' || name) AS relation FROM unnest(%s::text[]) AS name", (list(TABLES),)
    ).fetchall()
    if any(row["relation"] is None for row in tables):
        return "LITERATURE_SCHEMA_NOT_READY"
    role = connection.execute("SELECT oid FROM pg_roles WHERE rolname='nexpoly_api'").fetchone()
    if role is None:
        return "LITERATURE_API_ROLE_MISSING"
    identity = connection.execute(
        """SELECT rolname,rolsuper,rolbypassrls,pg_has_role(oid,'nexpoly_api','MEMBER') AS api_member
           FROM pg_roles WHERE rolname=COALESCE(%s,current_user)""", (role_override,)
    ).fetchone()
    if identity is None or identity["rolsuper"] or identity["rolbypassrls"] or not identity["api_member"]:
        return "LITERATURE_API_PRIVILEGES_INVALID"
    roles = sorted({"nexpoly_api", identity["rolname"]})
    usage = connection.execute(
        "SELECT bool_and(has_schema_privilege(role_name,'knowledge','USAGE')) AS allowed FROM unnest(%s::text[]) AS role_name", (roles,)
    ).fetchone()
    grants = connection.execute(
        """SELECT name,role_name,has_table_privilege(role_name,to_regclass('knowledge.' || name),'SELECT') AS readable,
          (has_table_privilege(role_name,to_regclass('knowledge.' || name),'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR has_any_column_privilege(role_name,to_regclass('knowledge.' || name),'INSERT,UPDATE,REFERENCES')) AS writable
          FROM unnest(%s::text[]) AS name CROSS JOIN unnest(%s::text[]) AS role_name""", (list(TABLES),roles)
    ).fetchall()
    if not usage["allowed"] or any(not row["readable"] or row["writable"] for row in grants):
        return "LITERATURE_API_PRIVILEGES_INVALID"
    return None


def validate_literature_api_schema(connection) -> None:
    """Metadata/ACL check; an empty publication catalog is a valid schema."""
    problem = _literature_schema_problem(connection)
    if problem is not None:
        raise ValueError(problem)


def check_literature_readiness(connection, *, enabled: bool = True, role_override: str | None = None) -> dict:
    """Inspect current_user; overrides are internal maintenance/test inputs only."""
    state = {"enabled": enabled, "ready": False, "total": 0, "generation_id": None,
             "published_at": None, "reason": "LITERATURE_DISABLED" if not enabled else None}
    if not enabled:
        return state
    problem = _literature_schema_problem(connection, role_override=role_override)
    if problem is not None:
        return {**state, "reason": problem}
    generation = connection.execute(
        "SELECT generation_id,normalizer_version,published_at FROM knowledge.literature_generations WHERE is_active AND status='published'"
    ).fetchone()
    if generation is None:
        return {**state, "reason": "LITERATURE_NOT_PUBLISHED"}
    state.update(generation_id=str(generation["generation_id"]),
                 published_at=generation["published_at"].isoformat() if generation["published_at"] else None)
    if generation["normalizer_version"] not in SUPPORTED_NORMALIZER_VERSIONS:
        return {**state, "reason": "LITERATURE_FORMAT_UNSUPPORTED"}
    total = connection.execute("SELECT count(*) AS total FROM knowledge.literature_catalog WHERE generation_id=%s", (generation["generation_id"],)).fetchone()["total"]
    return {**state, "ready": True, "total": int(total), "reason": None}
