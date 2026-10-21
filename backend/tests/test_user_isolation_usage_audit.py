"""Versioned audit receipts on disposable PostgreSQL, never live deployment."""
from contextlib import contextmanager
from datetime import datetime, timezone
import inspect
import json
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

from test_auth_isolation import auth_database as _historical_auth_database, _apply_historical_prefix
from scripts import user_isolation_audit as audit
from scripts.site_helper_contracts import SiteHelperContractError


@pytest.fixture(scope="module", params=[4, 5])
def audit_database(request):
    # Each version gets a separate disposable database and four unprivileged
    # synthetic identities, so testing the old prefix cannot downgrade new data.
    with contextmanager(_historical_auth_database.__wrapped__)() as database:
        version = request.param
        boundary = "0021_usage_metering" if version == 4 else "0022_usage_entitlements"
        _apply_historical_prefix(database["admin"], boundary)
        with psycopg.connect(database["admin"], row_factory=dict_row) as connection:
            owner = str(connection.execute("SELECT user_id FROM auth.users WHERE NOT is_system LIMIT 1").fetchone()["user_id"])
            # Insertion uses the ordinary ledger API and only synthetic metadata.
            from task_usage.ledger import record_event
            with pytest.MonkeyPatch.context() as monkeypatch:
                monkeypatch.setenv("USAGE_METERING_ENABLED", "true")
                record_event(connection, owner_user_id=owner, module="monomer_md",
                    task_id="private-audit-marker-"+uuid4().hex, event_key=uuid4().hex,
                    occurred_at=datetime.now(timezone.utc), status="queued")
                if version == 5:
                    from task_usage import entitlements as ent
                    from task_usage.entitlement_policy import example_policy
                    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "true")
                    policy = example_policy(); policy["version"] = "audit-"+uuid4().hex
                    ent.install_policy(connection, policy)
                    ent.reserve(connection, owner_user_id=owner, module="predict",
                        idempotency_key="private-audit-marker-"+uuid4().hex,
                        request_fingerprint="a"*64, policy_version=policy["version"], spec={"property_count":1})
        yield {**database, "version": version, "boundary": boundary}


def receipt(database, version=None):
    with psycopg.connect(database["audit"], row_factory=dict_row) as connection:
        value = audit.capture(connection, schema_version=version or database["version"])
        assert connection.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
        return value


def test_receipt_covers_new_seals_without_exporting_rows(audit_database):
    value = receipt(audit_database)
    version = audit_database["version"]
    assert value["schema_version"] == version
    assert value["migration_ledger"][-1]["version"] == audit_database["boundary"]
    assert value["backup_required_schemas"] == [*audit.BACKUP_REQUIRED_SCHEMAS,
        "structure_library", "usage", *(["usage_entitlements"] if version == 5 else [])]
    assert "structure_library.records" in value["business_tables"]
    assert value["usage_backup_seal"]["tables"]["usage.events"]["rows"] == 1
    assert value["usage_backup_seal"]["tables"]["usage.attempts"]["rows"] == 1
    if version == 5:
        assert value["entitlement_backup_seal"]["tables"]["usage_entitlements.operations"]["rows"] == 1
    else:
        assert "entitlement_backup_seal" not in value
    encoded = json.dumps(value)
    assert "private-audit-marker" not in encoded
    assert "password_hash" not in encoded and "token_hash" not in encoded
    assert value["credential_projection"] == "excluded"
    with psycopg.connect(audit_database["audit"]) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SELECT password_hash FROM auth.users")


def test_new_runtime_preflight_uses_separate_identities(audit_database):
    value = audit.preflight({"nexpoly_"+role:audit_database[role] for role in ("api","auth","service")},
                            schema_version=audit_database["version"])
    assert value["schema_version"] == audit_database["version"]
    assert value["current_readiness"] and not value["rollback_before_0018_allowed"]
    assert set(value["identities"]) == {"nexpoly_api","nexpoly_auth","nexpoly_service"}
    assert "usage" in value["backup_required_schemas"]


def test_earlier_receipt_cannot_label_a_newer_database(audit_database):
    with pytest.raises(SiteHelperContractError):
        receipt(audit_database, audit_database["version"] - 1)


def test_unknown_checksum_rejected_by_capture_and_preflight(audit_database):
    with psycopg.connect(audit_database["admin"], row_factory=dict_row) as connection:
        original = connection.execute("SELECT checksum FROM governance.schema_migrations WHERE version=%s",
            (audit_database["boundary"],)).fetchone()["checksum"]
        connection.execute("UPDATE governance.schema_migrations SET checksum=%s WHERE version=%s",
            ("0"*64, audit_database["boundary"]))
    try:
        with pytest.raises(SiteHelperContractError):
            receipt(audit_database)
        with pytest.raises(SiteHelperContractError):
            audit.preflight({"nexpoly_"+role:audit_database[role] for role in ("api","auth","service")},
                            schema_version=audit_database["version"])
    finally:
        with psycopg.connect(audit_database["admin"]) as connection:
            connection.execute("UPDATE governance.schema_migrations SET checksum=%s WHERE version=%s",
                (original, audit_database["boundary"]))


@pytest.mark.parametrize("relation", ["structure_library.records", "usage.attempts", "usage_entitlements.operations"])
def test_new_receipt_retains_all_schema_protections(audit_database, relation):
    if relation.startswith("usage_entitlements.") and audit_database["version"] == 4:
        # This prefix intentionally has no entitlement schema; assert its absence.
        with psycopg.connect(audit_database["admin"], row_factory=dict_row) as connection:
            assert connection.execute("SELECT to_regnamespace('usage_entitlements') AS ns").fetchone()["ns"] is None
        return
    from psycopg import sql
    target = sql.Identifier(*relation.split("."))
    with psycopg.connect(audit_database["admin"]) as connection:
        connection.execute(sql.SQL("ALTER TABLE {} NO FORCE ROW LEVEL SECURITY").format(target))
    try:
        with pytest.raises(ValueError):
            receipt(audit_database)
        with pytest.raises(ValueError):
            audit.preflight({"nexpoly_"+role:audit_database[role] for role in ("api","auth","service")},
                            schema_version=audit_database["version"])
    finally:
        with psycopg.connect(audit_database["admin"]) as connection:
            connection.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(target))


@pytest.mark.parametrize("invalid", [True, 1.0, "5", 0, 6, None])
def test_bad_versions_rejected_before_database_access(invalid):
    class Forbidden:
        def __getattribute__(self, name):
            raise AssertionError("invalid version touched database")
    with pytest.raises(ValueError):
        audit.capture(Forbidden(), schema_version=invalid)
    with pytest.raises(ValueError):
        audit.preflight({}, schema_version=invalid)


def test_latest_default_is_explicit_and_historical_versions_remain_opt_in():
    assert inspect.signature(audit.capture).parameters["schema_version"].default == 5
    assert inspect.signature(audit.preflight).parameters["schema_version"].default == 5
