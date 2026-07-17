from __future__ import annotations

import re
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "ops/config/mutable-data-audit-role.sql.example"


class MutableDataAuditRoleContractTest(unittest.TestCase):
    def test_template_has_no_inline_or_substituted_secret(self) -> None:
        payload = TEMPLATE.read_text(encoding="utf-8")
        self.assertNotRegex(
            payload,
            re.compile(r"\bPASSWORD\s+(?:'|\"|:|\$\{)", re.IGNORECASE),
        )
        self.assertNotIn("\\prompt", payload)
        self.assertNotIn("\\getenv", payload)
        self.assertIn("out of band", payload)

    def test_template_provisions_exact_read_only_role(self) -> None:
        payload = TEMPLATE.read_text(encoding="utf-8")
        required = (
            "LOGIN",
            "NOSUPERUSER",
            "NOCREATEDB",
            "NOCREATEROLE",
            "INHERIT",
            "NOREPLICATION",
            "NOBYPASSRLS",
            "SET default_transaction_read_only = on",
            "GRANT pg_read_all_data",
            "REVOKE %I FROM nexpoly_mutable_audit",
            "'pg_write_all_data'",
            "pg_catalog.lo_creat(integer)",
            "TO pg_database_owner",
            "direct membership is not exact",
            "inherited membership is unsafe",
            "effective persistent write authority",
            "role owns database objects",
            "provisioned by the cluster administrator in nexpoly",
        )
        for fragment in required:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, payload)
        self.assertNotRegex(
            payload,
            re.compile(
                r"GRANT\s+pg_write_all_data\s+TO\s+"
                r"nexpoly_mutable_audit",
                re.IGNORECASE,
            ),
        )

    def test_template_checks_relation_column_sequence_and_definer_paths(
        self,
    ) -> None:
        payload = TEMPLATE.read_text(encoding="utf-8")
        for function in (
            "has_database_privilege",
            "has_schema_privilege",
            "has_table_privilege",
            "has_column_privilege",
            "has_sequence_privilege",
            "has_function_privilege",
        ):
            with self.subTest(function=function):
                self.assertIn(function, payload)
        self.assertIn("routine.prosecdef", payload)
        self.assertIn("pg_largeobject_metadata", payload)
        self.assertIn("pg_shdepend", payload)
        self.assertIn("pg_tablespace", payload)


if __name__ == "__main__":
    unittest.main()
