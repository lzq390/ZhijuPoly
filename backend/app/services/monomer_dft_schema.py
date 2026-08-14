from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable


MONOMER_DFT_MIGRATION_VERSION = "0013_monomer_dft_jobs"
MONOMER_DFT_MIGRATION_CHECKSUM = (
    "ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc"
)
POSTGRES_MAJOR_VERSION = 16

# Generated from a pristine PostgreSQL 16 database after applying the
# checksum-exact 0013 migration.  The canonical document contains every
# user-visible object in the schema, every column/default/identity property,
# every constraint and index definition, the identity sequence ownership,
# normalized owner/ACL/storage identity, and the empty
# trigger/policy/routine/rule/security-label inventories.  Changing this value
# requires regenerating the document from the unchanged canonical migration and
# reviewing the resulting catalog diff.
MONOMER_DFT_CATALOG_FINGERPRINT_SHA256 = (
    "6dc2e6ca7e1bb052836afec2bbdd46c6aa0928e97efdbbc6669b9b220f9bf6f8"
)

_INVALID_CATALOG_ACL_PROJECTION = "<invalid-catalog-acl-projection>"
_SCHEMA_OWNER_PRIVILEGES = frozenset({"CREATE", "USAGE"})
_TABLE_OWNER_PRIVILEGES = frozenset(
    {
        "DELETE",
        "INSERT",
        "REFERENCES",
        "SELECT",
        "TRIGGER",
        "TRUNCATE",
        "UPDATE",
    }
)
_SEQUENCE_OWNER_PRIVILEGES = frozenset({"SELECT", "UPDATE", "USAGE"})


class MonomerDftSchemaState(str, Enum):
    ABSENT = "absent"
    READY = "ready"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class MonomerDftSchemaProbe:
    state: MonomerDftSchemaState
    reason: str
    catalog_sha256: str | None = None

    @property
    def ready(self) -> bool:
        return self.state is MonomerDftSchemaState.READY


def _canonical_rows(
    rows: Iterable[Any],
    fields: tuple[str, ...],
) -> list[dict[str, object]]:
    return [{field: row[field] for field in fields} for row in rows]


def _normalize_catalog_access_control(
    raw_access_control: object,
    entries: object,
    *,
    owner_oid: object,
    mutable_audit_role_oid: object,
    owner_matches_contract: object,
    owner_privileges: frozenset[str],
    mutable_audit_privilege: str,
) -> str:
    """Remove only the exact managed mutable-audit read-only ACL.

    PostgreSQL materializes an object's otherwise implicit owner ACL when the
    first explicit grant is added.  Therefore the approved representation is
    the complete default owner ACL plus one non-grantable privilege granted by
    that same owner to the exact mutable-audit role.  Returning the raw ACL for
    every other non-empty projection keeps unknown grantees, grantors, grant
    options, or privileges inside the immutable catalog fingerprint.
    """

    if not isinstance(raw_access_control, str):
        return _INVALID_CATALOG_ACL_PROJECTION
    if raw_access_control == "":
        return "" if entries == [] else _INVALID_CATALOG_ACL_PROJECTION

    if (
        owner_matches_contract is not True
        or not isinstance(owner_oid, str)
        or not owner_oid.isdigit()
        or int(owner_oid) <= 0
        or not isinstance(mutable_audit_role_oid, str)
        or not mutable_audit_role_oid.isdigit()
        or int(mutable_audit_role_oid) <= 0
        or mutable_audit_role_oid == owner_oid
        or not isinstance(entries, list)
    ):
        return raw_access_control

    normalized_entries: list[tuple[str, str, str, bool]] = []
    expected_fields = {"grantee", "grantor", "privilege", "grantable"}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != expected_fields:
            return raw_access_control
        grantee = entry.get("grantee")
        grantor = entry.get("grantor")
        privilege = entry.get("privilege")
        grantable = entry.get("grantable")
        if (
            not isinstance(grantee, str)
            or not grantee.isdigit()
            or int(grantee) <= 0
            or not isinstance(grantor, str)
            or not grantor.isdigit()
            or int(grantor) <= 0
            or not isinstance(privilege, str)
            or not privilege
            or not isinstance(grantable, bool)
        ):
            return raw_access_control
        normalized_entries.append(
            (grantee, grantor, privilege, grantable)
        )

    expected_entries = {
        (owner_oid, owner_oid, privilege, False)
        for privilege in owner_privileges
    }
    expected_entries.add(
        (
            mutable_audit_role_oid,
            owner_oid,
            mutable_audit_privilege,
            False,
        )
    )
    if (
        len(normalized_entries) == len(expected_entries)
        and set(normalized_entries) == expected_entries
    ):
        return ""
    return raw_access_control


@contextmanager
def _catalog_search_path(connection: Any):
    """Isolate catalog deparsing in a savepoint-safe search path.

    A catalog query can fail after changing ``search_path`` and leave the
    surrounding transaction aborted.  The nested transaction guarantees that
    failure rolls back both effects before the original exception is
    re-raised.  On success the prior setting is restored explicitly so callers
    can continue using the same outer transaction.
    """

    with connection.transaction():
        search_path_row = connection.execute(
            "SELECT current_setting('search_path') AS search_path"
        ).fetchone()
        previous_search_path = str(search_path_row["search_path"])
        connection.execute(
            "SELECT set_config('search_path', 'pg_catalog', true)"
        ).fetchone()
        try:
            yield
        except BaseException:
            raise
        else:
            connection.execute(
                "SELECT set_config('search_path', %s, true)",
                (previous_search_path,),
            ).fetchone()


def monomer_dft_catalog_document(connection: Any) -> dict[str, object]:
    """Return the deterministic PG16 catalog identity for ``monomer_dft``.

    Every query is against ``pg_catalog``.  No DFT business relation is read.
    The temporary search path makes all deparsed definitions deterministic and
    is restored before returning so callers may safely reuse their connection.
    """

    with _catalog_search_path(connection):
        namespace_rows = connection.execute(
            """
            SELECT
              n.nspname AS schema_name,
              n.nspowner = (
                SELECT r.oid
                FROM pg_catalog.pg_roles AS r
                WHERE r.rolname = current_user
              ) AS owner_is_current_role,
              COALESCE(pg_catalog.array_to_string(n.nspacl, ','), '')
                AS access_control,
              n.nspowner::text AS acl_owner_oid,
              (
                SELECT r.oid::text
                FROM pg_catalog.pg_roles AS r
                WHERE r.rolname = 'nexpoly_mutable_audit'
              ) AS mutable_audit_role_oid,
              COALESCE(
                (
                  SELECT pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                      'grantee', acl.grantee::text,
                      'grantor', acl.grantor::text,
                      'privilege', acl.privilege_type,
                      'grantable', acl.is_grantable
                    )
                    ORDER BY
                      acl.grantee,
                      acl.grantor,
                      acl.privilege_type,
                      acl.is_grantable
                  )
                  FROM pg_catalog.aclexplode(n.nspacl) AS acl
                ),
                '[]'::pg_catalog.jsonb
              ) AS access_control_entries
            FROM pg_catalog.pg_namespace AS n
            WHERE n.nspname = 'monomer_dft'
            """
        ).fetchall()
        namespace = _canonical_rows(
            namespace_rows,
            (
                "schema_name",
                "owner_is_current_role",
                "access_control",
            ),
        )
        for canonical, row in zip(namespace, namespace_rows, strict=True):
            canonical["access_control"] = _normalize_catalog_access_control(
                row["access_control"],
                row["access_control_entries"],
                owner_oid=row["acl_owner_oid"],
                mutable_audit_role_oid=row["mutable_audit_role_oid"],
                owner_matches_contract=row["owner_is_current_role"],
                owner_privileges=_SCHEMA_OWNER_PRIVILEGES,
                mutable_audit_privilege="USAGE",
            )

        relation_rows = connection.execute(
            """
            SELECT
              c.relname AS relation_name,
              c.relkind::text AS relation_kind,
              c.relpersistence::text AS persistence,
              c.relreplident::text AS replica_identity,
              c.relrowsecurity AS row_security,
              c.relforcerowsecurity AS force_row_security,
              c.relispartition AS is_partition,
              COALESCE(am.amname, '') AS access_method,
              c.relowner = n.nspowner AS owner_matches_schema,
              COALESCE(pg_catalog.array_to_string(c.relacl, ','), '')
                AS access_control,
              c.relowner::text AS acl_owner_oid,
              (
                SELECT r.oid::text
                FROM pg_catalog.pg_roles AS r
                WHERE r.rolname = 'nexpoly_mutable_audit'
              ) AS mutable_audit_role_oid,
              c.relowner = n.nspowner
                AND n.nspowner = (
                  SELECT r.oid
                  FROM pg_catalog.pg_roles AS r
                  WHERE r.rolname = current_user
                ) AS acl_owner_matches_contract,
              COALESCE(
                (
                  SELECT pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                      'grantee', acl.grantee::text,
                      'grantor', acl.grantor::text,
                      'privilege', acl.privilege_type,
                      'grantable', acl.is_grantable
                    )
                    ORDER BY
                      acl.grantee,
                      acl.grantor,
                      acl.privilege_type,
                      acl.is_grantable
                  )
                  FROM pg_catalog.aclexplode(c.relacl) AS acl
                ),
                '[]'::pg_catalog.jsonb
              ) AS access_control_entries,
              COALESCE(
                (
                  SELECT pg_catalog.string_agg(option_value, ',' ORDER BY option_value)
                  FROM pg_catalog.unnest(c.reloptions) AS option_value
                ),
                ''
              ) AS relation_options,
              COALESCE(ts.spcname, '') AS tablespace
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            LEFT JOIN pg_catalog.pg_am AS am ON am.oid = c.relam
            LEFT JOIN pg_catalog.pg_tablespace AS ts ON ts.oid = c.reltablespace
            WHERE n.nspname = 'monomer_dft'
            ORDER BY c.relkind, c.relname
            """
        ).fetchall()
        relations = _canonical_rows(
            relation_rows,
            (
                "relation_name",
                "relation_kind",
                "persistence",
                "replica_identity",
                "row_security",
                "force_row_security",
                "is_partition",
                "access_method",
                "owner_matches_schema",
                "access_control",
                "relation_options",
                "tablespace",
            ),
        )
        relation_acl_contracts = {
            "r": (_TABLE_OWNER_PRIVILEGES, "SELECT"),
            "S": (_SEQUENCE_OWNER_PRIVILEGES, "SELECT"),
        }
        for canonical, row in zip(relations, relation_rows, strict=True):
            contract = relation_acl_contracts.get(str(row["relation_kind"]))
            if contract is None:
                continue
            owner_privileges, mutable_audit_privilege = contract
            canonical["access_control"] = _normalize_catalog_access_control(
                row["access_control"],
                row["access_control_entries"],
                owner_oid=row["acl_owner_oid"],
                mutable_audit_role_oid=row["mutable_audit_role_oid"],
                owner_matches_contract=row["acl_owner_matches_contract"],
                owner_privileges=owner_privileges,
                mutable_audit_privilege=mutable_audit_privilege,
            )
        columns = _canonical_rows(
            connection.execute(
                """
                SELECT
                  c.relname AS table_name,
                  a.attnum AS ordinal_position,
                  a.attname AS column_name,
                  pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                  a.attnotnull AS not_null,
                  a.attidentity::text AS identity_kind,
                  a.attgenerated::text AS generated_kind,
                  a.attstorage::text AS storage_kind,
                  a.attislocal AS is_local,
                  a.attinhcount AS inheritance_count,
                  COALESCE(pg_catalog.array_to_string(a.attacl, ','), '')
                    AS access_control,
                  CASE
                    WHEN coll.oid IS NULL THEN ''
                    ELSE pg_catalog.format('%I.%I', coll_ns.nspname, coll.collname)
                  END AS collation,
                  COALESCE(
                    pg_catalog.pg_get_expr(ad.adbin, ad.adrelid, false),
                    ''
                  ) AS default_expression
                FROM pg_catalog.pg_attribute AS a
                JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                LEFT JOIN pg_catalog.pg_attrdef AS ad
                  ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
                LEFT JOIN pg_catalog.pg_collation AS coll ON coll.oid = a.attcollation
                LEFT JOIN pg_catalog.pg_namespace AS coll_ns
                  ON coll_ns.oid = coll.collnamespace
                WHERE n.nspname = 'monomer_dft'
                  AND c.relkind IN ('r', 'p')
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                ORDER BY c.relname, a.attnum
                """
            ).fetchall(),
            (
                "table_name",
                "ordinal_position",
                "column_name",
                "data_type",
                "not_null",
                "identity_kind",
                "generated_kind",
                "storage_kind",
                "is_local",
                "inheritance_count",
                "access_control",
                "collation",
                "default_expression",
            ),
        )
        constraints = _canonical_rows(
            connection.execute(
                """
                SELECT
                  c.relname AS table_name,
                  con.conname AS constraint_name,
                  con.contype::text AS constraint_type,
                  con.condeferrable AS deferrable,
                  con.condeferred AS initially_deferred,
                  con.convalidated AS validated,
                  con.conislocal AS is_local,
                  con.coninhcount AS inheritance_count,
                  con.connoinherit AS no_inherit,
                  pg_catalog.pg_get_constraintdef(con.oid, false) AS definition
                FROM pg_catalog.pg_constraint AS con
                JOIN pg_catalog.pg_class AS c ON c.oid = con.conrelid
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'monomer_dft'
                ORDER BY c.relname, con.conname
                """
            ).fetchall(),
            (
                "table_name",
                "constraint_name",
                "constraint_type",
                "deferrable",
                "initially_deferred",
                "validated",
                "is_local",
                "inheritance_count",
                "no_inherit",
                "definition",
            ),
        )
        indexes = _canonical_rows(
            connection.execute(
                """
                SELECT
                  tbl.relname AS table_name,
                  idx.relname AS index_name,
                  i.indisunique AS is_unique,
                  i.indisprimary AS is_primary,
                  i.indisexclusion AS is_exclusion,
                  i.indimmediate AS is_immediate,
                  i.indisvalid AS is_valid,
                  i.indisready AS is_ready,
                  i.indislive AS is_live,
                  i.indnullsnotdistinct AS nulls_not_distinct,
                  i.indisreplident AS is_replica_identity,
                  pg_catalog.pg_get_indexdef(idx.oid, 0, false) AS definition,
                  COALESCE(
                    pg_catalog.pg_get_expr(i.indpred, i.indrelid, false),
                    ''
                  ) AS predicate,
                  COALESCE(
                    pg_catalog.pg_get_expr(i.indexprs, i.indrelid, false),
                    ''
                  ) AS expressions
                FROM pg_catalog.pg_index AS i
                JOIN pg_catalog.pg_class AS idx ON idx.oid = i.indexrelid
                JOIN pg_catalog.pg_class AS tbl ON tbl.oid = i.indrelid
                JOIN pg_catalog.pg_namespace AS n ON n.oid = tbl.relnamespace
                WHERE n.nspname = 'monomer_dft'
                ORDER BY tbl.relname, idx.relname
                """
            ).fetchall(),
            (
                "table_name",
                "index_name",
                "is_unique",
                "is_primary",
                "is_exclusion",
                "is_immediate",
                "is_valid",
                "is_ready",
                "is_live",
                "nulls_not_distinct",
                "is_replica_identity",
                "definition",
                "predicate",
                "expressions",
            ),
        )
        sequences = _canonical_rows(
            connection.execute(
                """
                SELECT
                  seq.relname AS sequence_name,
                  pg_catalog.format_type(s.seqtypid, NULL) AS data_type,
                  s.seqstart AS start_value,
                  s.seqincrement AS increment_by,
                  s.seqmin AS minimum_value,
                  s.seqmax AS maximum_value,
                  s.seqcache AS cache_size,
                  s.seqcycle AS cycles,
                  COALESCE(dep.deptype::text, '') AS dependency_type,
                  COALESCE(tbl.relname, '') AS owner_table,
                  COALESCE(dep.refobjsubid, 0) AS owner_column_position
                FROM pg_catalog.pg_sequence AS s
                JOIN pg_catalog.pg_class AS seq ON seq.oid = s.seqrelid
                JOIN pg_catalog.pg_namespace AS n ON n.oid = seq.relnamespace
                LEFT JOIN pg_catalog.pg_depend AS dep
                  ON dep.classid = 'pg_catalog.pg_class'::pg_catalog.regclass
                 AND dep.objid = seq.oid
                 AND dep.objsubid = 0
                 AND dep.refclassid = 'pg_catalog.pg_class'::pg_catalog.regclass
                 AND dep.deptype IN ('a', 'i')
                LEFT JOIN pg_catalog.pg_class AS tbl ON tbl.oid = dep.refobjid
                WHERE n.nspname = 'monomer_dft'
                ORDER BY seq.relname, dep.deptype, tbl.relname, dep.refobjsubid
                """
            ).fetchall(),
            (
                "sequence_name",
                "data_type",
                "start_value",
                "increment_by",
                "minimum_value",
                "maximum_value",
                "cache_size",
                "cycles",
                "dependency_type",
                "owner_table",
                "owner_column_position",
            ),
        )
        types = _canonical_rows(
            connection.execute(
                """
                SELECT
                  t.typname AS type_name,
                  t.typtype::text AS type_kind,
                  t.typcategory::text AS category,
                  t.typispreferred AS is_preferred,
                  t.typnotnull AS not_null,
                  pg_catalog.format_type(t.oid, NULL) AS formatted_type,
                  CASE
                    WHEN t.typbasetype = 0 THEN ''
                    ELSE pg_catalog.format_type(t.typbasetype, t.typtypmod)
                  END AS base_type,
                  CASE
                    WHEN t.typelem = 0 THEN ''
                    ELSE pg_catalog.format_type(t.typelem, NULL)
                  END AS element_type,
                  CASE
                    WHEN t.typarray = 0 THEN ''
                    ELSE pg_catalog.format_type(t.typarray, NULL)
                  END AS array_type,
                  COALESCE(c.relname, '') AS relation_name,
                  t.typowner = n.nspowner AS owner_matches_schema,
                  COALESCE(pg_catalog.array_to_string(t.typacl, ','), '')
                    AS access_control,
                  CASE
                    WHEN coll.oid IS NULL THEN ''
                    ELSE pg_catalog.format('%I.%I', coll_ns.nspname, coll.collname)
                  END AS collation,
                  COALESCE(t.typdefault, '') AS default_expression
                FROM pg_catalog.pg_type AS t
                JOIN pg_catalog.pg_namespace AS n ON n.oid = t.typnamespace
                LEFT JOIN pg_catalog.pg_class AS c ON c.oid = t.typrelid
                LEFT JOIN pg_catalog.pg_collation AS coll ON coll.oid = t.typcollation
                LEFT JOIN pg_catalog.pg_namespace AS coll_ns
                  ON coll_ns.oid = coll.collnamespace
                WHERE n.nspname = 'monomer_dft'
                ORDER BY t.typname
                """
            ).fetchall(),
            (
                "type_name",
                "type_kind",
                "category",
                "is_preferred",
                "not_null",
                "formatted_type",
                "base_type",
                "element_type",
                "array_type",
                "relation_name",
                "owner_matches_schema",
                "access_control",
                "collation",
                "default_expression",
            ),
        )
        triggers = _canonical_rows(
            connection.execute(
                """
                SELECT
                  c.relname AS table_name,
                  t.tgname AS trigger_name,
                  t.tgenabled::text AS enabled,
                  pg_catalog.pg_get_triggerdef(t.oid, false) AS definition
                FROM pg_catalog.pg_trigger AS t
                JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'monomer_dft'
                  AND NOT t.tgisinternal
                ORDER BY c.relname, t.tgname
                """
            ).fetchall(),
            ("table_name", "trigger_name", "enabled", "definition"),
        )
        policies = _canonical_rows(
            connection.execute(
                """
                SELECT
                  c.relname AS table_name,
                  p.polname AS policy_name,
                  p.polcmd::text AS command,
                  p.polpermissive AS permissive,
                  COALESCE(
                    pg_catalog.pg_get_expr(p.polqual, p.polrelid, false),
                    ''
                  ) AS using_expression,
                  COALESCE(
                    pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid, false),
                    ''
                  ) AS check_expression
                FROM pg_catalog.pg_policy AS p
                JOIN pg_catalog.pg_class AS c ON c.oid = p.polrelid
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'monomer_dft'
                ORDER BY c.relname, p.polname
                """
            ).fetchall(),
            (
                "table_name",
                "policy_name",
                "command",
                "permissive",
                "using_expression",
                "check_expression",
            ),
        )
        routines = _canonical_rows(
            connection.execute(
                """
                SELECT
                  p.proname AS routine_name,
                  p.prokind::text AS routine_kind,
                  pg_catalog.pg_get_function_identity_arguments(p.oid) AS arguments,
                  pg_catalog.pg_get_function_result(p.oid) AS result_type,
                  p.provolatile::text AS volatility,
                  p.prosecdef AS security_definer,
                  p.proleakproof AS leakproof,
                  p.proparallel::text AS parallel_mode,
                  p.proowner = n.nspowner AS owner_matches_schema,
                  COALESCE(pg_catalog.array_to_string(p.proacl, ','), '')
                    AS access_control
                FROM pg_catalog.pg_proc AS p
                JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
                WHERE n.nspname = 'monomer_dft'
                ORDER BY p.proname, pg_catalog.pg_get_function_identity_arguments(p.oid)
                """
            ).fetchall(),
            (
                "routine_name",
                "routine_kind",
                "arguments",
                "result_type",
                "volatility",
                "security_definer",
                "leakproof",
                "parallel_mode",
                "owner_matches_schema",
                "access_control",
            ),
        )
        rules = _canonical_rows(
            connection.execute(
                """
                SELECT
                  c.relname AS relation_name,
                  r.rulename AS rule_name,
                  r.ev_type::text AS event_type,
                  r.is_instead AS is_instead,
                  r.ev_enabled::text AS enabled,
                  pg_catalog.pg_get_ruledef(r.oid, false) AS definition
                FROM pg_catalog.pg_rewrite AS r
                JOIN pg_catalog.pg_class AS c ON c.oid = r.ev_class
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'monomer_dft'
                ORDER BY c.relname, r.rulename
                """
            ).fetchall(),
            (
                "relation_name",
                "rule_name",
                "event_type",
                "is_instead",
                "enabled",
                "definition",
            ),
        )
        security_labels = _canonical_rows(
            connection.execute(
                """
                WITH labels AS (
                  SELECT
                    'namespace'::text AS object_kind,
                    n.nspname::text AS object_name,
                    ''::text AS subobject_name,
                    sl.provider::text AS provider,
                    sl.label::text AS label
                  FROM pg_catalog.pg_seclabel AS sl
                  JOIN pg_catalog.pg_namespace AS n
                    ON sl.classoid =
                       'pg_catalog.pg_namespace'::pg_catalog.regclass
                   AND sl.objoid = n.oid
                   AND sl.objsubid = 0
                  WHERE n.nspname = 'monomer_dft'

                  UNION ALL

                  SELECT
                    CASE
                      WHEN sl.objsubid = 0 THEN 'relation'
                      ELSE 'column'
                    END,
                    c.relname::text,
                    COALESCE(a.attname, '')::text,
                    sl.provider::text,
                    sl.label::text
                  FROM pg_catalog.pg_seclabel AS sl
                  JOIN pg_catalog.pg_class AS c
                    ON sl.classoid = 'pg_catalog.pg_class'::pg_catalog.regclass
                   AND sl.objoid = c.oid
                  JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                  LEFT JOIN pg_catalog.pg_attribute AS a
                    ON a.attrelid = c.oid
                   AND a.attnum = sl.objsubid
                  WHERE n.nspname = 'monomer_dft'

                  UNION ALL

                  SELECT
                    'type',
                    t.typname::text,
                    '',
                    sl.provider::text,
                    sl.label::text
                  FROM pg_catalog.pg_seclabel AS sl
                  JOIN pg_catalog.pg_type AS t
                    ON sl.classoid = 'pg_catalog.pg_type'::pg_catalog.regclass
                   AND sl.objoid = t.oid
                   AND sl.objsubid = 0
                  JOIN pg_catalog.pg_namespace AS n ON n.oid = t.typnamespace
                  WHERE n.nspname = 'monomer_dft'

                  UNION ALL

                  SELECT
                    'routine',
                    pg_catalog.format(
                      '%s(%s)',
                      p.proname,
                      pg_catalog.pg_get_function_identity_arguments(p.oid)
                    ),
                    '',
                    sl.provider::text,
                    sl.label::text
                  FROM pg_catalog.pg_seclabel AS sl
                  JOIN pg_catalog.pg_proc AS p
                    ON sl.classoid = 'pg_catalog.pg_proc'::pg_catalog.regclass
                   AND sl.objoid = p.oid
                   AND sl.objsubid = 0
                  JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
                  WHERE n.nspname = 'monomer_dft'
                )
                SELECT
                  object_kind,
                  object_name,
                  subobject_name,
                  provider,
                  label
                FROM labels
                ORDER BY
                  object_kind,
                  object_name,
                  subobject_name,
                  provider,
                  label
                """
            ).fetchall(),
            (
                "object_kind",
                "object_name",
                "subobject_name",
                "provider",
                "label",
            ),
        )
    return {
        "schema_version": 1,
        "postgres_major": POSTGRES_MAJOR_VERSION,
        "namespace": namespace,
        "relations": relations,
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
        "sequences": sequences,
        "types": types,
        "triggers": triggers,
        "policies": policies,
        "routines": routines,
        "rules": rules,
        "security_labels": security_labels,
    }


def monomer_dft_catalog_sha256(connection: Any) -> str:
    document = monomer_dft_catalog_document(connection)
    payload = json.dumps(
        document,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def probe_monomer_dft_schema(connection: Any) -> MonomerDftSchemaProbe:
    """Classify the only safe pre/post-0013 database states.

    ``ABSENT`` is deliberately narrow: PostgreSQL 16, a visible migration
    ledger, no 0013 ledger row, and no ``monomer_dft`` namespace.  Every
    partial, checksum-mismatched, or catalog-mismatched state is ``INVALID`` so
    deployment drain logic cannot silently fall back to the V1 job inventory.
    """

    state_row = connection.execute(
        """
        SELECT
          current_setting('server_version_num')::integer AS server_version_num,
          to_regclass('governance.schema_migrations') AS migration_ledger,
          (
            SELECT n.oid
            FROM pg_catalog.pg_namespace AS n
            WHERE n.nspname = 'monomer_dft'
          ) AS dft_namespace
        """
    ).fetchone()
    if state_row is None:
        return MonomerDftSchemaProbe(
            MonomerDftSchemaState.INVALID,
            "catalog_state_unavailable",
        )
    server_version_num = int(state_row["server_version_num"])
    if server_version_num // 10_000 != POSTGRES_MAJOR_VERSION:
        return MonomerDftSchemaProbe(
            MonomerDftSchemaState.INVALID,
            "postgres_major_mismatch",
        )
    if state_row["migration_ledger"] is None:
        return MonomerDftSchemaProbe(
            MonomerDftSchemaState.INVALID,
            "migration_ledger_unavailable",
        )

    ledger_rows = connection.execute(
        """
        SELECT checksum
        FROM governance.schema_migrations
        WHERE version = %s
        ORDER BY checksum
        """,
        (MONOMER_DFT_MIGRATION_VERSION,),
    ).fetchall()
    namespace_exists = state_row["dft_namespace"] is not None
    if not ledger_rows:
        if namespace_exists:
            return MonomerDftSchemaProbe(
                MonomerDftSchemaState.INVALID,
                "unmanaged_or_partial_schema",
            )
        return MonomerDftSchemaProbe(
            MonomerDftSchemaState.ABSENT,
            "migration_not_applied",
        )
    if (
        len(ledger_rows) != 1
        or str(ledger_rows[0]["checksum"]) != MONOMER_DFT_MIGRATION_CHECKSUM
    ):
        return MonomerDftSchemaProbe(
            MonomerDftSchemaState.INVALID,
            "migration_checksum_mismatch",
        )
    if not namespace_exists:
        return MonomerDftSchemaProbe(
            MonomerDftSchemaState.INVALID,
            "schema_missing_after_migration",
        )

    catalog_sha256 = monomer_dft_catalog_sha256(connection)
    if catalog_sha256 != MONOMER_DFT_CATALOG_FINGERPRINT_SHA256:
        return MonomerDftSchemaProbe(
            MonomerDftSchemaState.INVALID,
            "catalog_fingerprint_mismatch",
            catalog_sha256,
        )
    return MonomerDftSchemaProbe(
        MonomerDftSchemaState.READY,
        "exact_0013",
        catalog_sha256,
    )
