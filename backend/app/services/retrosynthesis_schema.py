"""Read-only structural and least-privilege checks for optional 0026 jobs."""
from pathlib import Path
import hashlib

from task_usage.retrosynthesis_contract import RETROSYNTHESIS_VERSION, RETROSYNTHESIS_CHECKSUM

COLUMNS = {
    "job_id": "uuid", "owner_user_id": "uuid", "idempotency_key": "text",
    "request_sha256": "text", "request_json": "jsonb", "status": "text", "stage": "text",
    "executor_id": "text", "claim_token": "uuid", "start_authorized_at": "timestamp with time zone",
    "result_json": "jsonb", "error_code": "text", "storage_bytes": "bigint",
    "created_at": "timestamp with time zone", "updated_at": "timestamp with time zone",
    "started_at": "timestamp with time zone", "finished_at": "timestamp with time zone",
}


def validate_retrosynthesis_schema(connection):
    source = Path(__file__).resolve().parents[2] / "migrations/postgres" / (RETROSYNTHESIS_VERSION + ".sql")
    if hashlib.sha256(source.read_bytes()).hexdigest() != RETROSYNTHESIS_CHECKSUM:
        raise ValueError("Retrosynthesis SQL differs from the reviewed release")
    rows = connection.execute("""SELECT a.attname AS name,format_type(a.atttypid,a.atttypmod) AS type
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='retrosynthesis' AND c.relname='jobs' AND a.attnum>0 AND NOT a.attisdropped""").fetchall()
    if {row["name"]: row["type"] for row in rows} != COLUMNS:
        raise ValueError("Retrosynthesis columns differ from the reviewed contract")
    row = connection.execute("""SELECT c.relkind='r' AND c.relrowsecurity AND c.relforcerowsecurity AS protected
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='retrosynthesis' AND c.relname='jobs'""").fetchone()
    if row is None or not row["protected"]:
        raise ValueError("Retrosynthesis requires forced owner isolation")
    rows = connection.execute("""SELECT pg_get_userbyid(acl.grantee) AS role,acl.privilege_type AS privilege,
        acl.is_grantable AS grantable FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN LATERAL aclexplode(c.relacl) acl
        WHERE n.nspname='retrosynthesis' AND c.relname='jobs' AND acl.grantee<>c.relowner
        ORDER BY 1,2""").fetchall()
    expected = sorted([("nexpoly_api", p, False) for p in ("SELECT",)]
                      + [("nexpoly_service", p, False) for p in ("SELECT", "INSERT", "UPDATE")]
                      + [("nexpoly_mutable_audit", "SELECT", False)])
    if [(r["role"], r["privilege"], r["grantable"]) for r in rows] != expected:
        raise ValueError("Retrosynthesis grants differ from the reviewed contract")
    row = connection.execute("""SELECT EXISTS(SELECT FROM pg_constraint WHERE conrelid='retrosynthesis.jobs'::regclass
        AND contype='u' AND conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='retrosynthesis.jobs'::regclass AND attname='owner_user_id'),
        (SELECT attnum FROM pg_attribute WHERE attrelid='retrosynthesis.jobs'::regclass AND attname='idempotency_key')]) AS valid""").fetchone()
    if not row["valid"]:
        raise ValueError("Retrosynthesis requires owner-scoped persistent idempotency")
    return {"version": RETROSYNTHESIS_VERSION, "validated": True}
