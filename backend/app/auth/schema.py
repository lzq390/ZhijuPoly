"""Runtime PostgreSQL identity and exact private-asset RLS contract validation."""
from __future__ import annotations

from app.services.monomer_dft_schema import probe_monomer_dft_schema

PRIVATE_SCHEMAS = ("auth", "governance", "online_knowledge", "md", "monomer_dft", "polymerization_batch", "structure_library")
OWNER_TABLES = {
    "online_knowledge.history": "history_id", "online_knowledge.jobs": "job_id",
    "md.monomer_md_jobs": "job_id", "monomer_dft.jobs": "job_id",
    "polymerization_batch.imports": "id", "polymerization_batch.jobs": "id",
}
CHILD_TABLES = {
    "monomer_dft.job_attempts": ("monomer_dft.jobs", "job_id", "job_id,attempt"),
    "monomer_dft.artifacts": ("monomer_dft.jobs", "job_id", "job_id,artifact_id"),
    "polymerization_batch.chunks": ("polymerization_batch.jobs", "id", "job_id,chunk_id"),
}


def owner_tables(connection, through: str | None = None):
    from .isolation_ledger import LIBRARY_VERSION
    if through is None:
        through = connection.execute("SELECT max(version) AS version FROM governance.schema_migrations").fetchone()["version"]
    return {**OWNER_TABLES, **({"structure_library.records": "id"} if through == LIBRARY_VERSION else {})}


def validate_runtime_role(connection, required_group: str):
    if required_group not in {"nexpoly_api", "nexpoly_service", "nexpoly_auth", "nexpoly_mutable_audit"}:
        raise ValueError("unknown runtime privilege group")
    role = connection.execute("""SELECT current_user AS role,
        pg_has_role(current_user,%s,'member') AS required_member,
        EXISTS(SELECT FROM pg_roles inherited
            WHERE pg_has_role(current_user,inherited.oid,'member')
              AND (inherited.rolsuper OR inherited.rolbypassrls OR inherited.rolcreatedb
                   OR inherited.rolcreaterole OR inherited.rolreplication)) AS privileged_membership,
        EXISTS(SELECT FROM pg_namespace n WHERE n.nspname=ANY(%s)
            AND pg_has_role(current_user,n.nspowner,'member')) AS owns_private_schema,
        EXISTS(SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=ANY(%s) AND pg_has_role(current_user,c.relowner,'member')) AS owns_private_table
        """, (required_group, list(PRIVATE_SCHEMAS), list(PRIVATE_SCHEMAS))).fetchone()
    if role is None or not role["required_member"] or any(role[name] for name in ("privileged_membership", "owns_private_schema", "owns_private_table")):
        raise ValueError(f"runtime identity must be an unprivileged non-owner member of {required_group}")
    if required_group == "nexpoly_api":
        excess = connection.execute("SELECT pg_has_role(current_user,'nexpoly_service','member') OR pg_has_role(current_user,'nexpoly_auth','member') OR pg_has_role(current_user,'nexpoly_mutable_audit','member') AS excess").fetchone()
        if excess["excess"]:
            raise ValueError("API identity inherits privileged service/auth/audit authority")
    if required_group in {"nexpoly_api", "nexpoly_mutable_audit"}:
        credentials = connection.execute("SELECT COALESCE(bool_or(has_column_privilege(current_user,c.oid,a.attnum,'SELECT')),false) AS readable FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid WHERE n.nspname='auth' AND ((c.relname='users' AND a.attname='password_hash') OR (c.relname='sessions' AND a.attname='token_hash'))").fetchone()
        if credentials["readable"]:
            raise ValueError("API/audit identity can read credential columns")
    return {"role": role["role"], "privilege_group": required_group}


def validate_isolation_policies(connection, through: str | None = None):
    tables = owner_tables(connection, through)
    rows = connection.execute("""SELECT n.nspname||'.'||c.relname AS relation,p.polname AS name,
        p.polcmd AS command,p.polpermissive AS permissive,
        ARRAY(SELECT pg_get_userbyid(role_oid)::text FROM unnest(p.polroles) role_oid ORDER BY pg_get_userbyid(role_oid)::text) AS roles,
        pg_get_expr(p.polqual,p.polrelid) AS using_expression,
        pg_get_expr(p.polwithcheck,p.polrelid) AS check_expression
        FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname||'.'||c.relname=ANY(%s) ORDER BY 1,2""", (list(tables) + list(CHILD_TABLES),)).fetchall()
    owner = "(owner_user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid)"
    expected = []
    for relation in sorted((*tables, *CHILD_TABLES)):
        owner_using, owner_check = owner, owner
        if relation in CHILD_TABLES:
            parent, parent_key, _ = CHILD_TABLES[relation]
            child = relation.split(".")[1]
            owner_using = f"(EXISTS ( SELECT FROM {parent} p WHERE ((p.{parent_key} = {child}.job_id) AND (p.owner_user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid))))"
            owner_check = None
        for name, command, roles, using, check in (
            ("audit_access", "r", ["nexpoly_mutable_audit"], "true", None),
            ("owner_access", "*", ["nexpoly_api"], owner_using, owner_check),
            ("service_access", "*", ["nexpoly_service"], "true", "true"),
        ):
            if relation == "structure_library.records" and name == "service_access":
                continue
            expected.append(dict(relation=relation, name=name, command=command, permissive=True, roles=roles, using_expression=using, check_expression=check))
    normalized = []
    for row in rows:
        row = dict(row)
        for field in ("using_expression", "check_expression"):
            if row[field] is not None:
                row[field] = " ".join(row[field].split())
        normalized.append(row)
    if normalized != expected:
        raise ValueError("private RLS policies differ from the exact ownership/service/audit contract")
    return normalized



def _relation_oid(connection, relation):
    # Metadata inspection must not require Worker USAGE on private library data.
    row = connection.execute("SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname||'.'||c.relname=%s", (relation,)).fetchone()
    if row is None:
        raise ValueError("private table missing: " + relation)
    return row["oid"]


def validate_structure_library_privileges(connection):
    """The new immutable store has no Worker grant or content UPDATE grant."""
    relation_oid = _relation_oid(connection, "structure_library.records")
    rows = connection.execute("""SELECT pg_get_userbyid(acl.grantee) AS role,
        acl.privilege_type AS privilege,acl.is_grantable AS grantable,'' AS column_name
        FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) acl
        WHERE c.oid=%s AND acl.grantee<>c.relowner
        UNION ALL
        SELECT pg_get_userbyid(acl.grantee),acl.privilege_type,acl.is_grantable,a.attname
        FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
        CROSS JOIN LATERAL aclexplode(a.attacl) acl
        WHERE c.oid=%s AND acl.grantee<>c.relowner
        ORDER BY 1,2,4""", (relation_oid, relation_oid)).fetchall()
    expected = sorted([
        ("nexpoly_api", privilege, False, "") for privilege in ("SELECT", "INSERT", "DELETE")
    ] + [("nexpoly_api", "UPDATE", False, column) for column in ("name", "metadata_revision", "updated_at")]
        + [("nexpoly_mutable_audit", "SELECT", False, "")], key=lambda row: (row[0], row[1], row[3]))
    if [tuple(row.values()) for row in rows] != expected:
        raise ValueError("structure library grants differ from the immutable API/audit contract")
    rows = connection.execute("""SELECT pg_get_userbyid(acl.grantee) AS role,
        acl.privilege_type AS privilege,acl.is_grantable AS grantable
        FROM pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) acl
        WHERE n.nspname='structure_library' AND acl.grantee<>n.nspowner ORDER BY 1,2""").fetchall()
    if [tuple(row.values()) for row in rows] != [("nexpoly_api", "USAGE", False), ("nexpoly_mutable_audit", "USAGE", False)]:
        raise ValueError("structure library schema grants differ from the API/audit contract")


def validate_isolation_schema(connection, through: str | None = None):
    """Fail closed on missing ownership, a relaxed policy, or a drifted DFT catalog."""
    with connection.transaction():
        previous = connection.execute("SELECT current_setting('search_path') AS value").fetchone()["value"]
        connection.execute("SET LOCAL search_path=pg_catalog")
        for relation in owner_tables(connection, through):
            relation_oid = _relation_oid(connection, relation)
            row = connection.execute("""SELECT c.relrowsecurity,c.relforcerowsecurity,a.attnotnull
                FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid AND a.attname='owner_user_id'
                WHERE c.oid=%s""", (relation_oid,)).fetchone()
            if row is None or not all(row.values()):
                raise ValueError("ownership/RLS contract missing for " + relation)
            foreign_key = connection.execute("""SELECT EXISTS(SELECT FROM pg_constraint k
                JOIN pg_attribute a ON a.attrelid=k.conrelid AND a.attname='owner_user_id'
                JOIN pg_attribute target ON target.attrelid=k.confrelid AND target.attname='user_id'
                WHERE k.conrelid=%s AND k.contype='f' AND k.confrelid=(SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='auth' AND c.relname='users')
                  AND k.conkey=ARRAY[a.attnum] AND k.confkey=ARRAY[target.attnum]
                  AND k.convalidated AND k.confdeltype='a' AND a.atttypid='uuid'::regtype) AS valid""", (relation_oid,)).fetchone()
            if not foreign_key["valid"]:
                raise ValueError("owner foreign-key contract missing for " + relation)
        for relation in CHILD_TABLES:
            row = connection.execute("SELECT relrowsecurity AND relforcerowsecurity AS protected FROM pg_class WHERE oid=%s::regclass", (relation,)).fetchone()
            if row is None or not row["protected"]:
                raise ValueError("child RLS contract missing for " + relation)
        policies = validate_isolation_policies(connection, through)
        if "structure_library.records" in owner_tables(connection, through):
            validate_structure_library_privileges(connection)
        dft = probe_monomer_dft_schema(connection)
        if not dft.ready:
            raise ValueError("DFT schema is not canonical: " + dft.reason)
        connection.execute("SELECT set_config('search_path',%s,true)", (previous,))
        return {"dft_catalog_sha256": dft.catalog_sha256, "rls_policies": policies}
