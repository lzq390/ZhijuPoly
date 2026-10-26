"""Synthetic transaction/lock contracts; imports the actual publication module.

No AST extraction, source database, object-store client, or real SQL execution.
The lock scheduler is a deterministic regression model, not PostgreSQL proof.
"""
from copy import deepcopy
import unittest

from app.services import literature_publication as implementation
from app.services.literature_normalizer import NORMALIZER_VERSION

CURRENT_FORMAT = NORMALIZER_VERSION
ACTIVE = "00000000-0000-0000-0000-000000000001"
TARGET = "00000000-0000-0000-0000-000000000002"


class InvertedParentLocks(RuntimeError):
    pass

class Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row

class PublicationConnection:
    def __init__(self, *, status="ready", normalizer=CURRENT_FORMAT, partial=False,
                 actual_count=1, ready_count=1, target_exists=True, crossing_catalog=False):
        self.generations = {
            ACTIVE: {"generation_id": ACTIVE, "status": "published", "is_active": True,
                     "normalizer_version": CURRENT_FORMAT, "published_at": "previous",
                     "source_summary": {}, "report": {"ready_documents": 1}},
        }
        if target_exists:
            self.generations[TARGET] = {
                "generation_id": TARGET, "status": status, "is_active": False,
                "normalizer_version": normalizer,
                "published_at": "previous-target" if status == "published" else None,
                "source_summary": {"partial": partial},
                "report": {"ready_documents": ready_count},
            }
        self.actual_count = actual_count
        self.catalog_holds_active = crossing_catalog
        self.held = set()
        self.lock_order = []
        self.calls = []
        self.advisory_seen = False

    def transaction(self):
        return self

    def __enter__(self):
        self.before = deepcopy(self.generations)
        self.held.clear()
        self.lock_order.clear()
        return self

    def __exit__(self, exception_type, exception, traceback):
        if exception_type:
            self.generations = self.before
        self.held.clear()
        return False

    def _lock(self, generation_id):
        if not self.advisory_seen:
            raise AssertionError("Parent row locked before publisher advisory lock")
        if generation_id in self.held:
            return
        if generation_id == ACTIVE and self.catalog_holds_active:
            # A catalog UPDATE holds the lower parent's SHARE lock and next
            # needs SHARE on TARGET. If publication already owns TARGET, each
            # transaction waits on the other. With ascending parent locks the
            # catalog can finish first and release its lower parent.
            if TARGET in self.held:
                raise InvertedParentLocks("catalog lower->higher; publisher higher->lower")
            self.catalog_holds_active = False
        self.held.add(generation_id)
        self.lock_order.append(generation_id)

    def execute(self, sql, params=None):
        query = " ".join(sql.lower().split())
        self.calls.append((query, params))
        if "pg_advisory_xact_lock" in query:
            self.advisory_seen = True
            return Result()
        if query.startswith("select generation_id from knowledge.literature_generations"):
            requested = params[0]
            identities = [key for key, value in self.generations.items()
                          if key == requested or ("or is_active" in query and value["is_active"])]
            if "order by generation_id" in query:
                identities.sort(reverse="order by generation_id desc" in query)
            if "for update" in query:
                for identity in identities:
                    self._lock(identity)
            return Result()
        if query.startswith("select * from knowledge.literature_generations"):
            identity = params[0]
            if identity in self.generations and "for update" in query:
                self._lock(identity)
            return Result(deepcopy(self.generations.get(identity)))
        if query.startswith("select count(*) n from knowledge.literature_catalog"):
            return Result({"n": self.actual_count})
        if query.startswith("update knowledge.literature_generations set is_active=false"):
            for identity, generation in self.generations.items():
                if generation["is_active"]:
                    self._lock(identity)
                    generation["is_active"] = False
            return Result()
        if query.startswith("update knowledge.literature_generations set is_active=true"):
            identity = params[0]
            self._lock(identity)
            generation = self.generations[identity]
            generation.update(is_active=True, status="published",
                              published_at=generation["published_at"] or "published-now")
            return Result()
        raise AssertionError("Unexpected fake publication query: " + query)

    def active_ids(self):
        return sorted(key for key, value in self.generations.items() if value["is_active"])

    def mutation_calls(self):
        return [query for query, _params in self.calls if query.startswith("update ")]

class PublicationContracts(unittest.TestCase):
    def test_publisher_and_catalog_follow_one_parent_lock_order(self):
        connection = PublicationConnection(crossing_catalog=True)
        result = implementation.activate_generation(connection, TARGET)
        self.assertEqual(connection.lock_order, [ACTIVE, TARGET])
        self.assertEqual(connection.active_ids(), [TARGET])
        self.assertEqual(result["action"], "publish")

    def test_publish_rejects_unsupported_format_before_any_update(self):
        connection = PublicationConnection(normalizer="unsupported-format")
        with self.assertRaisesRegex(implementation.ImportStateError, "GENERATION_FORMAT_UNSUPPORTED"):
            implementation.activate_generation(connection, TARGET)
        self.assertEqual(connection.active_ids(), [ACTIVE])
        self.assertEqual(connection.mutation_calls(), [])

    def test_rollback_rejects_unsupported_format_before_any_update(self):
        connection = PublicationConnection(status="published", normalizer="unsupported-format")
        with self.assertRaisesRegex(implementation.ImportStateError, "GENERATION_FORMAT_UNSUPPORTED"):
            implementation.activate_generation(connection, TARGET, rollback=True)
        self.assertEqual(connection.active_ids(), [ACTIVE])
        self.assertEqual(connection.mutation_calls(), [])

    def test_republishing_active_generation_preserves_metadata(self):
        connection = PublicationConnection(status="published")
        connection.generations[ACTIVE]["is_active"] = False
        connection.generations[TARGET]["is_active"] = True
        before = deepcopy(connection.generations[TARGET])
        for _ in range(2):
            result = implementation.activate_generation(connection, TARGET)
            self.assertEqual(result["documents"], 1)
            self.assertEqual(connection.active_ids(), [TARGET])
            self.assertEqual(connection.generations[TARGET], before)

    def test_rollback_requires_previous_publication_and_can_restore_it(self):
        connection = PublicationConnection()
        with self.assertRaisesRegex(implementation.ImportStateError, "ROLLBACK_REQUIRES_PUBLISHED_GENERATION"):
            implementation.activate_generation(connection, TARGET, rollback=True)
        self.assertEqual(connection.active_ids(), [ACTIVE])
        connection.generations[TARGET]["status"] = "published"
        connection.generations[TARGET]["published_at"] = "previous-target"
        result = implementation.activate_generation(connection, TARGET, rollback=True)
        self.assertEqual(result["action"], "rollback")
        self.assertEqual(connection.generations[TARGET]["published_at"], "previous-target")
        self.assertEqual(connection.active_ids(), [TARGET])

    def test_partial_generation_still_needs_explicit_override(self):
        connection = PublicationConnection(partial=True)
        with self.assertRaisesRegex(implementation.ImportStateError, "PARTIAL_GENERATION_REQUIRES_ALLOW_PARTIAL"):
            implementation.activate_generation(connection, TARGET)
        self.assertEqual(connection.active_ids(), [ACTIVE])
        implementation.activate_generation(connection, TARGET, allow_partial=True)
        self.assertEqual(connection.active_ids(), [TARGET])

    def test_catalog_count_mismatch_does_not_change_active_generation(self):
        connection = PublicationConnection(actual_count=2)
        with self.assertRaisesRegex(implementation.ImportStateError, "GENERATION_CATALOG_COUNT_MISMATCH"):
            implementation.activate_generation(connection, TARGET)
        self.assertEqual(connection.active_ids(), [ACTIVE])
        self.assertEqual(connection.mutation_calls(), [])

    def test_missing_generation_does_not_change_active_generation(self):
        connection = PublicationConnection(target_exists=False)
        with self.assertRaisesRegex(implementation.ImportStateError, "GENERATION_NOT_FOUND"):
            implementation.activate_generation(connection, TARGET)
        self.assertEqual(connection.active_ids(), [ACTIVE])
        self.assertEqual(connection.mutation_calls(), [])
    def test_invalid_uuid_never_starts_transaction_or_query(self):
        connection = PublicationConnection()
        with self.assertRaises(ValueError):
            implementation.activate_generation(connection, "invalid-id")
        self.assertEqual(connection.calls, [])
        self.assertEqual(connection.active_ids(), [ACTIVE])

    def test_previous_normalizer_version_is_rejected(self):
        connection = PublicationConnection(status="published", normalizer="literature-v1")
        with self.assertRaisesRegex(implementation.ImportStateError, "GENERATION_FORMAT_UNSUPPORTED"):
            implementation.activate_generation(connection, TARGET, rollback=True)
        self.assertEqual(connection.mutation_calls(), [])
        self.assertEqual(connection.active_ids(), [ACTIVE])

    def test_empty_catalog_is_rejected(self):
        connection = PublicationConnection(actual_count=0, ready_count=0)
        with self.assertRaisesRegex(implementation.ImportStateError, "GENERATION_CATALOG_COUNT_MISMATCH"):
            implementation.activate_generation(connection, TARGET)
        self.assertEqual(connection.mutation_calls(), [])

    def test_building_generation_is_rejected(self):
        connection = PublicationConnection(status="building")
        with self.assertRaisesRegex(implementation.ImportStateError, "GENERATION_NOT_READY"):
            implementation.activate_generation(connection, TARGET)
        self.assertEqual(connection.mutation_calls(), [])

    def test_update_failure_rolls_back_active_generation(self):
        connection = PublicationConnection()
        real_execute = connection.execute
        def fail_activation(sql, params=None):
            if sql.startswith("UPDATE knowledge.literature_generations SET is_active=true"):
                raise RuntimeError("synthetic activation failure")
            return real_execute(sql, params)
        connection.execute = fail_activation
        with self.assertRaisesRegex(RuntimeError, "synthetic activation failure"):
            implementation.activate_generation(connection, TARGET)
        self.assertEqual(connection.active_ids(), [ACTIVE])
        self.assertEqual(connection.generations[TARGET]["status"], "ready")


if __name__ == "__main__":
    unittest.main()
