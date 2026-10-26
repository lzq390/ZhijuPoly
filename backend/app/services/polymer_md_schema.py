"""Read-only readiness for the optional 0023 development schema."""
from __future__ import annotations

from pathlib import Path
from collections import Counter
import re

VERSION = "0023_polymer_md_jobs"
CHECKSUM = "e73e00e16b8fc3c72e3716dae523d02d264a3e2e6989671f6019aca3440a1379"
TABLES = ("jobs", "job_attempts", "stage_runs", "artifacts")
COLUMNS = {
    "jobs": "job_id owner_user_id idempotency_key input_sha256 request_sha256 scientific_request enqueue_sequence attempt_token status quality_status stage results worker_instance_id snapshot_revision dispatch_started_at start_authorized_at cancel_requested_at cleanup_confirmed artifact_state delete_job_requested error_code error_message created_at updated_at started_at finished_at",
    "job_attempts": "job_id attempt attempt_token request_sha256 worker_instance_id start_authorized_at status snapshot created_at updated_at",
    "stage_runs": "job_id stage_run_id attempt_token stage attempt status quality_status results quality errors started_at finished_at",
    "artifacts": "job_id artifact_id stage_run_id attempt_token kind filename media_type size_bytes sha256 storage_ref deleted_at",
}


def apply_polymer_md_dev_migration(dsn: str, *, environment: str, migrations_dir: Path | None = None):
    """Explicit, atomic opt-in; ordinary release expansion never applies 0023."""
    if environment != "dev":
        raise ValueError("polymer MD 0023 may only be explicitly applied to a development database")
    from app.postgres_database import postgres_connection
    from app.postgres_migrations import MIGRATIONS_DIR, MigrationResult, validate_migration_manifest_entries
    from app.auth.isolation_ledger import validate_service_migration_ledger, validate_application_migration_ledger, ENTITLEMENT_VERSION
    from app.auth.schema import validate_isolation_schema
    directory=migrations_dir or MIGRATIONS_DIR
    entries=validate_migration_manifest_entries(directory)
    entry=next((item for item in entries if item.version==VERSION),None)
    if entry is None or entry.kind!="expand" or entry.epoch!=8 or entry.checksum!=CHECKSUM:
        raise ValueError("0023 manifest does not match the reviewed development migration")
    with postgres_connection(dsn) as connection:
        connection.execute("SET LOCAL lock_timeout = '5s'")
        connection.execute("SET LOCAL statement_timeout = '60s'")
        connection.execute("SELECT pg_advisory_xact_lock(742128925057022)")
        connection.execute("LOCK TABLE governance.schema_migrations IN SHARE ROW EXCLUSIVE MODE")
        observed=connection.execute("SELECT version FROM governance.schema_migrations WHERE version=%s",(VERSION,)).fetchone()
        if observed:
            validate_service_migration_ledger(connection,through=VERSION)
            validate_application_migration_ledger(connection, require_usage=True, require_entitlements=True)
            validate_isolation_schema(connection)
            validate_polymer_md_schema(connection)
            return MigrationResult(version=VERSION,checksum=CHECKSUM,applied=False)
        # A valid name alone is insufficient: require every canonical checksum,
        # the exact main22 prefix and its existing private schema contracts.
        validate_service_migration_ledger(connection,through=ENTITLEMENT_VERSION)
        validate_application_migration_ledger(connection, require_usage=True, require_entitlements=True)
        validate_isolation_schema(connection)
        if connection.execute("SELECT to_regnamespace('polymer_md') AS schema").fetchone()["schema"] is not None:
            raise ValueError("unexpected polymer_md schema exists before 0023")
        connection.execute((directory/f"{VERSION}.sql").read_text())
        connection.execute("INSERT INTO governance.schema_migrations(version,checksum) VALUES(%s,%s)",(VERSION,CHECKSUM))
        validate_service_migration_ledger(connection,through=VERSION)
        validate_polymer_md_schema(connection)
        return MigrationResult(version=VERSION,checksum=CHECKSUM,applied=True)


def schema_installed(connection) -> bool:
    row = connection.execute("SELECT to_regclass('polymer_md.jobs') IS NOT NULL AS present").fetchone()
    return bool(row and row["present"])


_ACTIVE_PREDICATE = "status = ANY (ARRAY['pending','queued','running','cancel_requested'])"
_CHECKS = {
    "jobs": [
        "length(idempotency_key)>=8 AND length(idempotency_key)<=128",
        "input_sha256 ~ '^[0-9a-f]{64}$'", "request_sha256 ~ '^[0-9a-f]{64}$'",
        "jsonb_typeof(scientific_request)='object'", "attempt_token ~ '^[0-9a-f]{64}$'",
        "status = ANY (ARRAY['pending','queued','running','completed','failed','cancel_requested','cancelled'])",
        "quality_status = ANY (ARRAY['not_evaluated','passed','warning','not_converged','failed'])",
        "snapshot_revision>=0", "artifact_state = ANY (ARRAY['available','delete_requested','deleted'])",
        "status <> ALL (ARRAY['completed','failed','cancelled']) OR cleanup_confirmed"],
    "job_attempts": ["attempt>0", "attempt_token ~ '^[0-9a-f]{64}$'"],
    "stage_runs": ["stage_run_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'", "attempt>0",
        "status = ANY (ARRAY['pending','running','completed','failed','cancelled'])",
        "quality_status = ANY (ARRAY['not_evaluated','passed','warning','not_converged','failed'])"],
    "artifacts": ["artifact_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'",
        "filename ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$'", "size_bytes>=0 AND size_bytes<=21474836480",
        "sha256 ~ '^[0-9a-f]{64}$'",
        r"storage_ref IS NULL OR (length(storage_ref)>=1 AND length(storage_ref)<=1024 AND storage_ref ~ '^[A-Za-z0-9][A-Za-z0-9._/-]*$' AND storage_ref !~ '(^|/)\.\.?(/|$)' AND storage_ref LIKE job_id::text || '/' || attempt_token || '/%')"],
}


def _expression(value):
    """Normalize catalog formatting while preserving AND/OR grouping and literals."""
    value = re.sub(r"::(?:pg_catalog\.)?(?:text|bigint|integer|uuid)(?:\[\])?", "", value or "")
    tokens = re.findall(r"'(?:''|[^'])*'|<>|>=|<=|!~|\|\||[A-Za-z_][A-Za-z_0-9]*|[0-9]+|\S", value)
    tokens = [token if token.startswith("'") else token.lower() for token in tokens]
    tokens = [part for token in tokens for part in (["~", "~"] if token == "like" else [token])]
    tokens = [token[1:-1] if re.fullmatch(r"'[0-9]+'", token) else token for token in tokens]

    def normalize(parts):
        while len(parts)>1 and parts[0]=='(' and parts[-1]==')':
            depth=0
            for i,token in enumerate(parts):
                depth += token=='('
                depth -= token==')'
                if depth==0:
                    break
            if i!=len(parts)-1:
                break
            parts=parts[1:-1]
        for operator in ('or','and'):
            depth=0;groups=[];start=0
            for i,token in enumerate(parts):
                if token in ('(', '['): depth+=1
                elif token in (')', ']'): depth-=1
                elif depth==0 and token==operator:
                    groups.append(normalize(parts[start:i]));start=i+1
            if groups:
                groups.append(normalize(parts[start:]))
                # Boolean associativity permits redundant same-operator grouping.
                flattened=[]
                for group in groups:
                    flattened.extend(group[1] if group and group[0]==operator else [group])
                return operator,tuple(flattened)
        return 'atom',tuple(token for token in parts if token not in ('(',')'))
    return normalize(tokens)


def _validate_keys_and_checks(connection):
    rows = connection.execute("""SELECT c.relname,k.contype,k.convalidated,k.condeferrable,k.condeferred,
        ARRAY(SELECT a.attname::text FROM unnest(k.conkey) WITH ORDINALITY x(attnum,n)
          JOIN pg_attribute a ON a.attrelid=k.conrelid AND a.attnum=x.attnum ORDER BY x.n) AS columns,
        tn.nspname||'.'||target.relname AS target,
        ARRAY(SELECT a.attname::text FROM unnest(k.confkey) WITH ORDINALITY x(attnum,n)
          JOIN pg_attribute a ON a.attrelid=k.confrelid AND a.attnum=x.attnum ORDER BY x.n) AS target_columns,
        k.confdeltype FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_class target ON target.oid=k.confrelid
        LEFT JOIN pg_namespace tn ON tn.oid=target.relnamespace
        WHERE n.nspname='polymer_md' AND k.contype IN ('p','f')""").fetchall()
    primary={r['relname']:tuple(r['columns']) for r in rows if r['contype']=='p'}
    expected_primary={'jobs':('job_id',),'job_attempts':('job_id','attempt'),
        'stage_runs':('job_id','stage_run_id'),'artifacts':('job_id','artifact_id')}
    expected_foreign={
        ('jobs',('owner_user_id',),'auth.users',('user_id',),'a'),
        ('job_attempts',('job_id',),'polymer_md.jobs',('job_id',),'c'),
        ('stage_runs',('job_id',),'polymer_md.jobs',('job_id',),'c'),
        ('stage_runs',('job_id','attempt_token'),'polymer_md.job_attempts',('job_id','attempt_token'),'a'),
        ('artifacts',('job_id',),'polymer_md.jobs',('job_id',),'c'),
        ('artifacts',('job_id','stage_run_id'),'polymer_md.stage_runs',('job_id','stage_run_id'),'c'),
        ('artifacts',('job_id','attempt_token'),'polymer_md.job_attempts',('job_id','attempt_token'),'a')}
    foreign={(r['relname'],tuple(r['columns']),r['target'],tuple(r['target_columns']),r['confdeltype'])
        for r in rows if r['contype']=='f'}
    if (len(rows)!=11 or primary!=expected_primary or foreign!=expected_foreign
            or any(not r['convalidated'] or r['condeferrable'] or r['condeferred'] for r in rows)):
        raise ValueError('polymer MD exact parent/attempt keys differ from 0023')
    checks=connection.execute("""SELECT c.relname,k.convalidated,pg_get_expr(k.conbin,k.conrelid) AS expression
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='polymer_md' AND k.contype='c'""").fetchall()
    expected=Counter((name,_expression(expression)) for name,expressions in _CHECKS.items() for expression in expressions)
    if any(not row['convalidated'] for row in checks) or Counter((r['relname'],_expression(r['expression'])) for r in checks)!=expected:
        raise ValueError('polymer MD safety CHECK constraints differ from 0023')


def _validate_acl_and_indexes(connection):
    roles=('nexpoly_api','nexpoly_service','nexpoly_mutable_audit')
    schema=connection.execute("""SELECT pg_get_userbyid(a.grantee) AS role,a.privilege_type,a.is_grantable
        FROM pg_namespace n CROSS JOIN LATERAL aclexplode(coalesce(n.nspacl,acldefault('n',n.nspowner))) a
        WHERE n.nspname='polymer_md' AND a.grantee<>n.nspowner""").fetchall()
    if {(r['role'],r['privilege_type'],r['is_grantable']) for r in schema}!={(r,'USAGE',False) for r in roles}:
        raise ValueError('polymer MD exact schema ACL differs from 0023')
    authority=connection.execute("""SELECT EXISTS(SELECT FROM pg_roles r JOIN pg_namespace n ON n.nspname='polymer_md'
        WHERE r.rolname=ANY(%s) AND (has_schema_privilege(r.oid,n.oid,'CREATE') OR pg_has_role(r.oid,n.nspowner,'MEMBER')
          OR EXISTS(SELECT FROM pg_class c WHERE c.relnamespace=n.oid AND pg_has_role(r.oid,c.relowner,'MEMBER')))) AS excess""",(list(roles),)).fetchone()
    if authority['excess']:
        raise ValueError('polymer MD runtime group has schema ownership or CREATE authority')
    objects=connection.execute("""SELECT c.relname,c.relkind,pg_get_userbyid(a.grantee) AS role,
        a.privilege_type,a.is_grantable FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault(CASE WHEN c.relkind='S' THEN 'S'::\"char\" ELSE 'r'::\"char\" END,c.relowner))) a
        WHERE n.nspname='polymer_md' AND c.relkind IN ('r','S') AND a.grantee<>c.relowner""").fetchall()
    expected={(name,'r',role,privilege,False) for name in TABLES for role,privileges in {
        'nexpoly_api':('SELECT',),'nexpoly_mutable_audit':('SELECT',),
        'nexpoly_service':('SELECT','INSERT','UPDATE','DELETE')}.items() for privilege in privileges}
    expected.update({('jobs_enqueue_sequence_seq','S',role,privilege,False)
        for role,privileges in {'nexpoly_service':('USAGE','SELECT'),'nexpoly_mutable_audit':('SELECT',)}.items() for privilege in privileges})
    actual={(r['relname'],r['relkind'],r['role'],r['privilege_type'],r['is_grantable']) for r in objects}
    column_grants=connection.execute("""SELECT EXISTS(SELECT FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='polymer_md' AND a.attnum>0 AND a.attacl IS NOT NULL) AS excess""").fetchone()
    sequences=connection.execute("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='polymer_md' AND c.relkind='S'""").fetchall()
    if actual!=expected or column_grants['excess'] or [r['relname'] for r in sequences]!=['jobs_enqueue_sequence_seq']:
        raise ValueError('polymer MD exact table/sequence ACL differs from 0023')
    indexes=connection.execute("""SELECT c.relname,i.indisunique,i.indisprimary,i.indisvalid,i.indisready,
        i.indimmediate,i.indnatts=i.indnkeyatts AS no_includes,i.indexprs IS NULL AS no_expressions,am.amname,
        ARRAY(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY x(attnum,n)
          JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=x.attnum ORDER BY x.n) AS columns,
        pg_get_expr(i.indpred,i.indrelid) AS predicate FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_class ix ON ix.oid=i.indexrelid
        JOIN pg_am am ON am.oid=ix.relam WHERE n.nspname='polymer_md'""").fetchall()
    expected_indexes={
        ('jobs',True,True,('job_id',),None),('jobs',True,False,('enqueue_sequence',),None),
        ('jobs',True,False,('attempt_token',),None),('jobs',True,False,('owner_user_id','idempotency_key'),None),
        ('job_attempts',True,True,('job_id','attempt'),None),('job_attempts',True,False,('attempt_token',),None),
        ('job_attempts',True,False,('job_id','attempt_token'),None),
        ('stage_runs',True,True,('job_id','stage_run_id'),None),('artifacts',True,True,('job_id','artifact_id'),None),
        ('jobs',True,False,('owner_user_id',),_expression(_ACTIVE_PREDICATE)),
        ('jobs',False,False,('enqueue_sequence',),_expression(_ACTIVE_PREDICATE))}
    actual_indexes={(r['relname'],r['indisunique'],r['indisprimary'],tuple(r['columns']),
        _expression(r['predicate']) if r['predicate'] else None) for r in indexes}
    if (len(indexes)!=len(expected_indexes) or actual_indexes!=expected_indexes or any(
            not all(r[k] for k in ('indisvalid','indisready','indimmediate','no_includes','no_expressions'))
            or r['amname']!='btree' for r in indexes)):
        raise ValueError('polymer MD safety indexes differ from 0023')


def validate_polymer_md_schema(connection) -> None:
    row = connection.execute("SELECT checksum FROM governance.schema_migrations WHERE version=%s", (VERSION,)).fetchone()
    if row is None or row["checksum"] != CHECKSUM:
        raise ValueError("polymer MD requires exact 0023 migration")
    rows = connection.execute("""SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,c.relkind
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='polymer_md' AND c.relkind IN ('r','p','v','m','f') ORDER BY c.relname""").fetchall()
    if {r["relname"] for r in rows} != set(TABLES) or any(not r["relrowsecurity"] or not r["relforcerowsecurity"] or r["relkind"] != 'r' for r in rows):
        raise ValueError("polymer MD relation/RLS inventory is invalid")
    columns = connection.execute("""SELECT c.relname,array_agg(a.attname::text ORDER BY a.attnum) AS columns
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid
        WHERE n.nspname='polymer_md' AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped GROUP BY c.relname""").fetchall()
    if {r['relname']:r['columns'] for r in columns} != {name:fields.split() for name,fields in COLUMNS.items()}:
        raise ValueError("polymer MD column inventory differs from 0023")
    protected = connection.execute("""SELECT count(*) FILTER(WHERE k.contype='f' AND k.convalidated) AS foreign_keys,
        count(*) FILTER(WHERE k.contype='p') AS primary_keys FROM pg_constraint k
        JOIN pg_namespace n ON n.oid=k.connamespace WHERE n.nspname='polymer_md'""").fetchone()
    if protected['foreign_keys'] != 7 or protected['primary_keys'] != 4:
        raise ValueError("polymer MD parent/attempt keys differ from 0023")
    _validate_keys_and_checks(connection)
    _validate_acl_and_indexes(connection)
    if connection.execute("""SELECT EXISTS(SELECT FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='polymer_md' AND NOT t.tgisinternal)
        OR EXISTS(SELECT FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='polymer_md') AS unexpected""").fetchone()['unexpected']:
        raise ValueError("polymer MD contains unexpected executable schema objects")
    from app.auth.schema import validate_isolation_policies
    validate_isolation_policies(connection)
    # An API login must never gain a write route around the domain service.
    grants = connection.execute("""SELECT EXISTS(SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='polymer_md' AND c.relkind='r' AND
        (has_table_privilege('nexpoly_api',c.oid,'INSERT') OR has_table_privilege('nexpoly_api',c.oid,'UPDATE')
         OR has_table_privilege('nexpoly_api',c.oid,'DELETE') OR has_table_privilege('nexpoly_api',c.oid,'TRUNCATE'))) AS excess""").fetchone()
    if grants["excess"]:
        raise ValueError("polymer MD API has excess mutation privileges")
