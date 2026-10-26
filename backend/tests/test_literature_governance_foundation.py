"""Synthetic governance contracts: no database or migration execution."""
import ast
import copy
import hashlib
import json
import unittest
from pathlib import Path

from app import literature_contract as contract
from app.auth import cutover
from app.migration_policy import validate_migration_manifest_entries


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class InventoryConnection:
    def __init__(self, literature=(), library=False, polymer=False, override=None):
        self.literature = set(literature)
        self.library = library
        self.polymer = polymer
        self.override = override
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))
        if "unnest" in query:
            rows = [{"name": name, "relation": name if name in self.literature else None} for name in params[0]]
            return Result(self.override if self.override is not None else rows)
        if "structure_library.records" in query:
            return Result([{"present": self.library}])
        if "to_regnamespace('polymer_md')" in query:
            return Result([{"present": self.polymer}])
        raise AssertionError("Unexpected synthetic inventory query")


class LiteratureGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.full = contract.registered_literature_ledger()
        self.base = self.full[:-1]
        self.names = tuple(schema + "." + table for schema, table in contract.LITERATURE_TABLES)

    def test_exact_registered_extension_is_accepted(self):
        self.assertTrue(contract.compatible_identity_ledger(self.full, self.base))
        self.assertTrue(contract.compatible_identity_ledger(self.full, self.full))

    def test_legacy_exact_boundaries_remain_exact(self):
        for boundary in range(17, 24):
            with self.subTest(boundary=boundary):
                prefix = self.full[:boundary]
                self.assertTrue(contract.compatible_identity_ledger(prefix, prefix))
                if boundary < 23:
                    self.assertFalse(contract.compatible_identity_ledger(self.full, prefix))

    def test_missing_intervening_expansion_is_rejected(self):
        for version in ("0020_structure_library", "0021_usage_metering", "0022_usage_entitlements", "0023_polymer_md_jobs"):
            with self.subTest(version=version):
                invalid = [row for row in self.full if row["version"] != version]
                self.assertFalse(contract.compatible_identity_ledger(invalid, self.base))

    def test_checksum_drift_at_every_position_is_rejected(self):
        for index in range(len(self.full)):
            with self.subTest(index=index):
                invalid = copy.deepcopy(self.full)
                invalid[index]["checksum"] = "b" * 64
                self.assertFalse(contract.compatible_identity_ledger(invalid, self.base))

    def test_unknown_duplicate_and_reordered_entries_are_rejected(self):
        for invalid in (self.full + [{"version": "0025_unknown", "checksum": "a" * 64}],
                        self.full + [self.full[-1]], list(reversed(self.full)), []):
            with self.subTest(length=len(invalid)):
                self.assertFalse(contract.compatible_identity_ledger(invalid, self.base))

    def test_untrusted_expected_cannot_make_equal_ledgers_valid(self):
        for invalid in ([], [{"version": "0019_service_auth_least_privilege", "checksum": "a" * 64}],
                        self.full + [{"version": "0025_unknown", "checksum": "a" * 64}]):
            self.assertFalse(contract.compatible_identity_ledger(invalid, invalid))

    def test_registered_records_do_not_share_mutable_state(self):
        self.full[0]["checksum"] = "b" * 64
        self.assertNotEqual(self.full, contract.registered_literature_ledger())

    def test_ledger_matches_every_packaged_migration(self):
        folder = Path(__file__).resolve().parents[1] / "migrations/postgres"
        actual = [{"version": entry.version, "checksum": entry.checksum} for entry in validate_migration_manifest_entries(folder)]
        self.assertEqual(actual, self.full)
        data = (folder / (contract.LITERATURE_MIGRATION + ".sql")).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), contract.LITERATURE_MIGRATION_SHA256)

    def test_absent_projection_preserves_historical_inventory(self):
        connection = InventoryConnection()
        self.assertEqual(contract.optional_literature_tables(connection), ())
        self.assertEqual(cutover.backup_tables(connection), cutover.BACKUP_TABLES)

    def test_complete_projection_adds_every_table_once(self):
        connection = InventoryConnection(self.names, library=True, polymer=True)
        actual = cutover.backup_tables(connection)
        self.assertEqual(actual, (*cutover.BACKUP_TABLES, "structure_library.records", *cutover.POLYMER_TABLES, *self.names))
        self.assertEqual(len(set(actual)), len(actual))

    def test_partial_projection_rejects_backup(self):
        for count in (1, 2):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    cutover.backup_tables(InventoryConnection(self.names[:count]))

    def test_missing_duplicate_or_ambiguous_inventory_rejects(self):
        for rows in ([], [{"name": self.names[0], "relation": None}] * 3,
                     [{"name": "other.table", "relation": None}] * 3):
            with self.assertRaises(ValueError):
                contract.optional_literature_tables(InventoryConnection(override=rows))

    def test_migration_is_deferred_without_enabling_runtime(self):
        module = ast.parse((Path(__file__).resolve().parents[1] / "app/postgres_migrations.py").read_text())
        values = [node.value for node in ast.walk(module) if isinstance(node, ast.AnnAssign)
                  and isinstance(node.target, ast.Name) and node.target.id == "deferred_versions"]
        self.assertEqual(len(values), 1)
        self.assertIn(contract.LITERATURE_MIGRATION, ast.literal_eval(values[0]))
        from app.auth import isolation_ledger
        current = isolation_ledger.expected_isolation_ledger(contract.LITERATURE_MIGRATION)
        self.assertEqual(current, contract.registered_literature_ledger())
        self.assertEqual(current[-2]["version"], "0023_polymer_md_jobs")
        self.assertRaises(ValueError, isolation_ledger.expected_isolation_ledger, "0025_unknown")

    def test_sql_keeps_ordered_catalog_locks_and_bounded_group(self):
        folder = Path(__file__).resolve().parents[1] / "migrations/postgres"
        sql = (folder / (contract.LITERATURE_MIGRATION + ".sql")).read_text()
        self.assertIn("ORDER BY generation_id FOR SHARE", sql)
        self.assertIn("NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS", sql)
        start = sql.index("DO $$ BEGIN\n  -- Attribute checks")
        end = sql.index("GRANT USAGE ON SCHEMA knowledge TO nexpoly_literature_import", start)
        role_block = sql[start:end]
        self.assertIn("IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexpoly_literature_import') THEN", role_block)
        self.assertNotIn("IF NOT EXISTS", role_block)
        self.assertLess(role_block.index("RAISE EXCEPTION"), role_block.index("CREATE ROLE"))
        self.assertIn("explicit role provenance review required", role_block)
        self.assertNotIn("GRANT ALL", sql)
        self.assertNotIn("SECURITY DEFINER", sql)


if __name__ == "__main__":
    unittest.main()
