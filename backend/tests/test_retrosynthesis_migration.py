"""Optional 0026 upgrade, authorization and restore contract on disposable PG16."""
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

from app.auth.backup import validate_backup_schema
from app.auth.cutover import backup_state_seal
from app.auth.isolation_ledger import expected_isolation_ledger, validate_application_migration_ledger
from app.retrosynthesis_contract import compatible_identity_ledger
from app.retrosynthesis_migrations import migrate_retrosynthesis
from app.services.retrosynthesis_schema import validate_retrosynthesis_schema
from task_usage.entitlement_access import accepted_release_ledgers, resolve_access_tier
from task_usage.billing_contract import BILLING_VERSION
from task_usage.retrosynthesis_contract import RETROSYNTHESIS_VERSION
from test_billing_migration import billing_db, auth_database


@pytest.fixture(scope="module")
def retro_schema_db(billing_db):
    assert migrate_retrosynthesis(billing_db["admin"])["written"] is False
    with psycopg.connect(billing_db["admin"]) as c:
        assert c.execute("SELECT to_regnamespace('retrosynthesis')").fetchone()[0] is None
    assert migrate_retrosynthesis(billing_db["admin"], apply=True)["written"] is True
    assert migrate_retrosynthesis(billing_db["admin"], apply=True)["written"] is False
    return billing_db


def test_only_registered_successor_is_accepted():
    ledger = expected_isolation_ledger(RETROSYNTHESIS_VERSION)
    assert ledger == accepted_release_ledgers()[-1]
    assert compatible_identity_ledger(ledger, expected_isolation_ledger(BILLING_VERSION))
    assert not compatible_identity_ledger([*ledger, {"version": "0027_unknown", "checksum": "0"*64}], ledger)
    wrong = [*ledger[:-1], {**ledger[-1], "checksum": "0"*64}]
    assert not compatible_identity_ledger(wrong, ledger)


def test_readiness_backup_and_membership_survive_new_ledger(retro_schema_db, monkeypatch):
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "false")
    with psycopg.connect(retro_schema_db["admin"], row_factory=dict_row) as c:
        assert validate_backup_schema(c)["retrosynthesis_schema"]["validated"]
        assert "retrosynthesis.jobs" in backup_state_seal(c)
        assert validate_application_migration_ledger(c, require_usage=False, require_entitlements=False)[-1]["version"] == RETROSYNTHESIS_VERSION
        owner = str(uuid4())
        c.execute("INSERT INTO auth.users(user_id,username,password_hash,must_change_password) VALUES(%s,%s,'!',false)", (owner, "retro-migration-"+owner))
    with psycopg.connect(retro_schema_db["api"], row_factory=dict_row) as c:
        c.execute("SELECT set_config('app.user_id',%s,true)", (owner,))
        assert resolve_access_tier(c, owner_user_id=owner) == "free"
        assert validate_retrosynthesis_schema(c)["validated"]


@pytest.mark.parametrize("target", ["user-isolation-current", "retrosynthesis-0026"])
def test_service_api_preflight(retro_schema_db, target):
    from app.config import Settings
    from app.postgres_preflight import run_preflight
    settings = Settings(app_postgres_dsn=retro_schema_db["api"], pi_postgres_dsn=retro_schema_db["api"])
    report = run_preflight(settings, dsn=retro_schema_db["service"], mode="schema", strict=True, schema_target=target)
    assert report["strict_ok"], report.get("strict_errors")
    assert report["schema_target"] == "retrosynthesis-0026"
    assert report["retrosynthesis_schema_ready"]
    assert report["billing_schema_ready"]


def test_privilege_drift_is_rejected(retro_schema_db):
    with psycopg.connect(retro_schema_db["admin"], row_factory=dict_row) as c:
        c.execute("GRANT UPDATE ON retrosynthesis.jobs TO nexpoly_api")
        with pytest.raises(ValueError, match="grants"):
            validate_retrosynthesis_schema(c)
        c.rollback()
