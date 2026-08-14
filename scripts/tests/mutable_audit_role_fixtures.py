"""Canonical explicit-grant mutable-audit role evidence for unit tests."""

from __future__ import annotations

from typing import Any


def role_security_evidence(
    contracts: Any, *, include_generation: bool = True
) -> dict[str, object]:
    schemas = [
        schema
        for schema in contracts.MUTABLE_AUDIT_GOVERNED_SCHEMAS
        if include_generation or schema != "generation"
    ]
    return {
        "role": "nexpoly_mutable_audit",
        "can_login": True,
        "superuser": False,
        "create_db": False,
        "create_role": False,
        "inherit": True,
        "replication": False,
        "bypass_rls": False,
        "role_settings": [
            {
                "database": "*",
                "settings": ["default_transaction_read_only=on"],
            }
        ],
        "direct_memberships": [],
        "effective_memberships": [],
        "has_pg_read_all_data": False,
        "has_pg_write_all_data": False,
        "database_privileges": {
            "database": "nexpoly",
            "connect": True,
            "create": False,
        },
        "governed_schemas": [
            {
                "schema": schema,
                "oid": str(100 + index),
                "usage": True,
                "create": False,
            }
            for index, schema in enumerate(schemas)
        ],
        "governed_relations": [
            {
                "relation": relation,
                "oid": str(200 + index),
                "kind": "r",
                "owner": "polyprop",
                "select": True,
                "insert": False,
                "update": False,
                "delete": False,
                "truncate": False,
                "references": False,
                "trigger": False,
            }
            for index, relation in enumerate(
                ["core.polymers", "governance.schema_migrations"]
            )
        ],
        "governed_sequences": [
            {
                "sequence": "governance.source_files_source_file_id_seq",
                "oid": "300",
                "owner": "polyprop",
                "select": True,
                "usage": False,
                "update": False,
            }
        ],
        "column_write_grants": [],
        "outside_governed_privileges": [],
        "default_privileges": sorted(
            [
                {
                    "owner": "polyprop",
                    "schema": schema,
                    "object_type": object_type,
                    "privilege": "SELECT",
                    "grantable": False,
                }
                for schema in schemas
                for object_type in ("S", "r")
            ],
            key=lambda record: (
                record["owner"],
                record["schema"],
                record["object_type"],
                record["privilege"],
            ),
        ),
        "owned_objects": [],
        "security_definer_execute": [],
        "large_object_update_count": 0,
        "large_object_mutators": [
            {
                "routine": routine,
                "oid": str(400 + index),
                "owner": "polyprop",
                "public_execute": False,
                "database_owner_execute": True,
                "audit_execute": False,
            }
            for index, routine in enumerate(
                contracts.MUTABLE_AUDIT_LO_MUTATORS
            )
        ],
    }
