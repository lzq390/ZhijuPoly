"""Metadata-only PostgreSQL 16 contract for service access to authentication.

No query in this module reads authentication rows or role password verifiers.
Callers own the transaction and must separately validate the migration ledger.
"""
from __future__ import annotations

from psycopg.rows import dict_row

VERSION = "0019_service_auth_least_privilege"
LEGACY_VERSION = "0018_user_isolation_cutover"
SELECT_COLUMNS = frozenset({"user_id", "status", "is_system"})
UPDATE_COLUMNS = frozenset({"updated_at"})
AUTH_COLUMNS = {
    "users": frozenset({"user_id", "username", "password_hash", "status",
                        "must_change_password", "is_system", "created_at",
                        "updated_at", "password_changed_at"}),
    "sessions": frozenset({"session_id", "user_id", "token_hash", "created_at",
                           "expires_at", "revoked_at"}),
}
PRIVATE_SCHEMAS = ("auth", "governance", "online_knowledge", "md", "monomer_dft",
                   "polymerization_batch")
FORBIDDEN_AUTHORITY_ROLES = frozenset({
    "pg_read_server_files", "pg_write_server_files", "pg_execute_server_program",
    "pg_read_all_data", "pg_write_all_data",
    "pg_signal_backend", "pg_checkpoint", "pg_use_reserved_connections",
    "pg_create_subscription", "pg_monitor", "pg_read_all_settings",
    "pg_read_all_stats", "pg_stat_scan_tables",
})
# Match all overloads in pg_catalog, including server-side large-object file
# import/export. Direct/PUBLIC function grants can bypass the predefined roles.
SERVER_FILE_FUNCTIONS = frozenset({
    "pg_read_file", "pg_read_binary_file", "pg_stat_file", "pg_ls_dir",
    "lo_import", "lo_export",
})


def validate_service_auth_privileges(connection, role=None, *, require_login=False,
                                     legacy=False):
    """Return safe metadata or raise ValueError on any excess/missing authority.

    ``role=None`` examines current_user. Administrators may name the exact
    service login or privilege group instead. ``legacy=True`` accepts the 0018
    users SELECT grant only; it is a migration precondition/historical audit,
    never a current-readiness result. The check includes PostgreSQL 16 SET ROLE
    paths as well as privileges immediately inherited by each usable identity.
    """
    def reject(reason):
        raise ValueError("service authentication privilege contract: " + reason)

    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT pg_catalog.current_setting('server_version_num')::integer AS version")
        if cursor.fetchone()["version"] // 10000 != 16:
            reject("PostgreSQL 16 is required")
        if role is None:
            cursor.execute("SELECT current_user=session_user AS original_identity")
            if not cursor.fetchone()["original_identity"]:
                reject("runtime current_user must equal session_user; SET ROLE identities are not accepted")
        cursor.execute("""SELECT r.oid,r.rolname,r.rolcanlogin,
            pg_catalog.pg_has_role(r.oid,g.oid,'USAGE') AS service_inherited
            FROM pg_catalog.pg_roles r LEFT JOIN pg_catalog.pg_roles g ON g.rolname='nexpoly_service'
            WHERE r.rolname=COALESCE(%s,current_user)""", (role,))
        target = cursor.fetchone()
        if target is None or not target["service_inherited"]:
            reject("identity must inherit nexpoly_service")
        if require_login and not target["rolcanlogin"]:
            reject("runtime identity must be a LOGIN role")
        role_oid = target["oid"]

        # SET follows pg_catalog.pg_auth_members.set_option in PG16. has_*_privilege below
        # also includes each identity's inherited grants and PUBLIC authority.
        cursor.execute("""SELECT r.oid,r.rolname FROM pg_catalog.pg_roles r
            WHERE r.oid=%s OR pg_catalog.pg_has_role(%s,r.oid,'SET') ORDER BY r.rolname""",
                       (role_oid, role_oid))
        identities = cursor.fetchall()
        identity_oids = [row["oid"] for row in identities]
        cursor.execute("""SELECT r.oid,r.rolname,r.rolsuper,r.rolbypassrls,
            r.rolcreatedb,r.rolcreaterole,r.rolreplication
            FROM pg_catalog.pg_roles r WHERE EXISTS (
              SELECT FROM pg_catalog.unnest(%s::oid[]) usable(oid)
              WHERE pg_catalog.pg_has_role(usable.oid,r.oid,'USAGE'))""", (identity_oids,))
        inherited = cursor.fetchall()
        authority_oids = [row["oid"] for row in inherited]
        if any(any(row[name] for name in ("rolsuper", "rolbypassrls", "rolcreatedb",
                                         "rolcreaterole", "rolreplication"))
               for row in inherited):
            reject("privileged role or inherited/SET ROLE path")
        if any(row["rolname"] in FORBIDDEN_AUTHORITY_ROLES for row in inherited):
            reject("predefined management or data-access authority role")
        if any(row["rolname"] == "nexpoly_auth" for row in inherited):
            reject("authentication and execution roles must be separate")
        cursor.execute("""SELECT EXISTS(SELECT FROM pg_catalog.pg_auth_members
            WHERE member=ANY(%s::oid[]) AND admin_option) AS admin_option""",
                       (authority_oids,))
        if cursor.fetchone()["admin_option"]:
            reject("role administration authority")

        cursor.execute("""SELECT EXISTS(SELECT FROM pg_catalog.pg_proc p
            CROSS JOIN pg_catalog.unnest(%s::oid[]) usable(oid)
            WHERE p.pronamespace='pg_catalog'::pg_catalog.regnamespace
              AND p.proname=ANY(%s) AND (p.proowner=ANY(%s::oid[])
                OR pg_catalog.has_function_privilege(usable.oid,p.oid,'EXECUTE')
                OR pg_catalog.has_function_privilege(usable.oid,p.oid,'EXECUTE WITH GRANT OPTION'))
            ) AS file_authority""", (identity_oids, sorted(SERVER_FILE_FUNCTIONS), authority_oids))
        if cursor.fetchone()["file_authority"]:
            reject("server file function authority")

        # Parameter ACLs are cluster-wide and may include parameters of an
        # extension that is not loaded (and hence absent from pg_settings).
        # Inspect grants, never parameter values. Normal, implicit session SET
        # permissions remain allowed; explicit SET/ALTER SYSTEM grants and
        # their grant options are outside the service contract.
        cursor.execute("""SELECT EXISTS(SELECT FROM pg_catalog.pg_parameter_acl p
            CROSS JOIN LATERAL pg_catalog.aclexplode(p.paracl) acl
            WHERE acl.privilege_type IN ('SET','ALTER SYSTEM')
              AND (acl.grantee=0 OR acl.grantee=ANY(%s::oid[]))
            ) AS parameter_authority""", (authority_oids,))
        if cursor.fetchone()["parameter_authority"]:
            reject("configuration parameter grant authority")

        cursor.execute("""SELECT d.datdba=ANY(%s::oid[]) AS owns_database,
            EXISTS(SELECT FROM pg_catalog.unnest(%s::oid[]) usable(oid)
              WHERE pg_catalog.has_database_privilege(usable.oid,d.oid,'CREATE')) AS creates_schema
            FROM pg_catalog.pg_database d
            WHERE d.datname=pg_catalog.current_database()""", (authority_oids, identity_oids))
        database = cursor.fetchone()
        if database is None or database["owns_database"] or database["creates_schema"]:
            reject("database ownership or CREATE authority")
        # The maintenance runner invokes this validator directly, so it must
        # enforce the same private-owner boundary as runtime schema readiness.
        # Ordinary business CRUD and the normal database TEMP grant are allowed.
        cursor.execute("""SELECT EXISTS(SELECT FROM pg_catalog.pg_namespace n
              WHERE n.nspname=ANY(%s) AND n.nspowner=ANY(%s::oid[])) AS schema_owner,
            EXISTS(SELECT FROM pg_catalog.pg_class c
              JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
              WHERE n.nspname=ANY(%s) AND c.relowner=ANY(%s::oid[])) AS relation_owner,
            EXISTS(SELECT FROM pg_catalog.pg_namespace n
              CROSS JOIN pg_catalog.unnest(%s::oid[]) usable(oid)
              WHERE n.nspname=ANY(%s)
                AND pg_catalog.has_schema_privilege(usable.oid,n.oid,'CREATE')) AS schema_create
            """, (list(PRIVATE_SCHEMAS), authority_oids, list(PRIVATE_SCHEMAS), authority_oids,
                    identity_oids, list(PRIVATE_SCHEMAS)))
        if any(cursor.fetchone().values()):
            reject("private schema/table ownership or CREATE authority")

        cursor.execute("SELECT oid,nspowner FROM pg_catalog.pg_namespace WHERE nspname='auth'")
        namespace = cursor.fetchone()
        if namespace is None:
            reject("auth schema is missing")
        if namespace["nspowner"] in authority_oids:
            reject("authentication schema ownership")
        cursor.execute("""SELECT usable.oid,
            pg_catalog.has_schema_privilege(usable.oid,%s,'USAGE') AS usage,
            pg_catalog.has_schema_privilege(usable.oid,%s,'CREATE') AS create_allowed,
            pg_catalog.has_schema_privilege(usable.oid,%s,'USAGE WITH GRANT OPTION') AS usage_grant
            FROM pg_catalog.unnest(%s::oid[]) usable(oid)""",
                       (namespace["oid"], namespace["oid"], namespace["oid"], identity_oids))
        schema_rights = cursor.fetchall()
        if not next(row["usage"] for row in schema_rights if row["oid"] == role_oid):
            reject("auth schema USAGE is required")
        if any(row["create_allowed"] or row["usage_grant"] for row in schema_rights):
            reject("auth schema CREATE or grant option")

        cursor.execute("""SELECT c.oid,c.relname,c.relkind,c.relowner
            FROM pg_catalog.pg_class c WHERE c.relnamespace=%s ORDER BY c.relname""", (namespace["oid"],))
        relations = cursor.fetchall()
        tables = {row["relname"]: row for row in relations if row["relname"] in AUTH_COLUMNS}
        if set(tables) != set(AUTH_COLUMNS) or any(row["relkind"] != "r" for row in tables.values()):
            reject("auth.users/auth.sessions must be ordinary tables")
        if any(row["relowner"] in authority_oids for row in relations):
            reject("authentication object ownership")

        cursor.execute("""SELECT c.relname,a.attname,a.attnum,c.oid
            FROM pg_catalog.pg_class c JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid
            WHERE c.relnamespace=%s AND c.relname IN ('users','sessions')
              AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum""", (namespace["oid"],))
        columns = cursor.fetchall()
        for name, expected in AUTH_COLUMNS.items():
            if {row["attname"] for row in columns if row["relname"] == name} != expected:
                reject("unexpected or missing authentication columns in " + name)

        # Column checks detect table grants too, including inherited/PUBLIC
        # access. OID/attnum avoids requiring the examined role to resolve auth.
        cursor.execute("""SELECT usable.oid AS role_oid,c.relname,a.attname,
            privilege.name AS privilege,
            pg_catalog.has_column_privilege(usable.oid,c.oid,a.attnum,privilege.name) AS allowed,
            pg_catalog.has_column_privilege(usable.oid,c.oid,a.attnum,
                privilege.name||' WITH GRANT OPTION') AS grantable
            FROM pg_catalog.pg_class c JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid
            CROSS JOIN pg_catalog.unnest(%s::oid[]) usable(oid)
            CROSS JOIN pg_catalog.unnest(ARRAY['SELECT','INSERT','UPDATE','REFERENCES']) privilege(name)
            WHERE c.relnamespace=%s AND c.relkind IN ('r','p','v','m','f')
              AND a.attnum>0 AND NOT a.attisdropped""", (identity_oids, namespace["oid"]))
        column_rights = cursor.fetchall()
        selected = AUTH_COLUMNS["users"] if legacy else SELECT_COLUMNS
        for row in column_rights:
            expected = row["relname"] == "users" and (
                row["privilege"] == "SELECT" and row["attname"] in selected
                or row["privilege"] == "UPDATE" and row["attname"] in UPDATE_COLUMNS)
            if row["grantable"] or (row["allowed"] and not expected):
                reject("excess column authority: " + row["relname"] + "." + row["attname"] + " " + row["privilege"])
            if row["role_oid"] == role_oid and expected and not row["allowed"]:
                reject("missing column authority: " + row["relname"] + "." + row["attname"] + " " + row["privilege"])

        cursor.execute("""SELECT c.relname,privilege.name AS privilege,
            pg_catalog.has_table_privilege(usable.oid,c.oid,privilege.name) AS allowed,
            pg_catalog.has_table_privilege(usable.oid,c.oid,
                privilege.name||' WITH GRANT OPTION') AS grantable
            FROM pg_catalog.pg_class c CROSS JOIN pg_catalog.unnest(%s::oid[]) usable(oid)
            CROSS JOIN pg_catalog.unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) privilege(name)
            WHERE c.relnamespace=%s AND c.relkind IN ('r','p','v','m','f')""", (identity_oids, namespace["oid"]))
        for row in cursor.fetchall():
            permitted = legacy and row["relname"] == "users" and row["privilege"] == "SELECT"
            if row["grantable"] or row["allowed"] and not permitted:
                reject("excess table authority: " + row["relname"] + " " + row["privilege"])

        cursor.execute("""SELECT EXISTS(SELECT FROM pg_catalog.pg_class c
            CROSS JOIN pg_catalog.unnest(%s::oid[]) usable(oid)
            WHERE c.relnamespace=%s AND c.relkind='S'
            AND pg_catalog.has_sequence_privilege(usable.oid,c.oid,'USAGE,SELECT,UPDATE')) AS sequence_access,
            EXISTS(SELECT FROM pg_catalog.pg_proc p CROSS JOIN pg_catalog.unnest(%s::oid[]) usable(oid)
            WHERE p.pronamespace=%s AND (p.proowner=ANY(%s::oid[])
              OR pg_catalog.has_function_privilege(usable.oid,p.oid,'EXECUTE'))) AS routine_access""",
                       (identity_oids, namespace["oid"], identity_oids, namespace["oid"], authority_oids))
        extra = cursor.fetchone()
        if extra["sequence_access"] or extra["routine_access"]:
            reject("authentication sequence or routine authority")
        cursor.execute("""SELECT EXISTS(SELECT FROM pg_catalog.pg_default_acl d
            CROSS JOIN LATERAL pg_catalog.aclexplode(d.defaclacl) acl
            WHERE d.defaclnamespace IN (0,%s) AND
              (acl.grantee=0 OR acl.grantee=ANY(%s::oid[]))) AS default_authority""",
                       (namespace["oid"], authority_oids))
        if cursor.fetchone()["default_authority"]:
            reject("authentication/global default grants")

    return {
        "version": LEGACY_VERSION if legacy else VERSION,
        "role": target["rolname"], "login": target["rolcanlogin"],
        "historical_only": bool(legacy), "current_readiness": not legacy,
        "set_role_paths": [row["rolname"] for row in identities],
        "users_select_columns": sorted(selected),
        "users_update_columns": sorted(UPDATE_COLUMNS),
        "password_select": bool(legacy), "session_token_select": False,
        "grant_options": False, "auth_other_authority": False,
    }
