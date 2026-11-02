"""Production-copy maintenance uses real disposable PG16 and synthetic identities."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import psycopg
from psycopg.rows import dict_row
import pytest

from app import postgres_migrations as command
from app.auth.backup import validate_backup_schema
from app.auth.isolation_ledger import expected_isolation_ledger
from app.literature_contract import LITERATURE_MIGRATION
from app.services import literature_schema as maintenance
from app.services.polymer_md_schema import apply_polymer_md_dev_migration
from test_auth_isolation import auth_database
from test_literature_maintenance import (
    evidence_for, ledger, main22_database, migration_database, private_json,
)


TARGET = "a" * 40
OPERATION = "deploy-prod-literature-test"


def production_evidence(db, monkeypatch, *, suffix="before"):
    monkeypatch.setenv("BUILD_REVISION", TARGET)
    with psycopg.connect(db["dsn"], row_factory=dict_row) as connection:
        system_id = str(connection.execute("SELECT system_identifier::text AS id FROM pg_control_system()").fetchone()["id"])
        connection.execute("""UPDATE governance.deployment_control SET drain_enabled=true,
            activated_by=%s,release_sha=%s WHERE control_key='production'""", (OPERATION, TARGET))
    options = evidence_for(db, suffix=suffix)
    document = json.loads(options["maintenance_evidence"].read_text())
    original_system_id = str(int(system_id) - 1)  # Invented, distinct old source; never connected.
    document.update(purpose="literature-prod-0024", operation_id=OPERATION, target_sha=TARGET,
                    database_system_identifier=system_id,
                    production_source_system_identifier=original_system_id)
    private_json(options["maintenance_evidence"], document)
    return {**options, "environment": "prod", "operation_id": OPERATION, "target_sha": TARGET,
            "production_source_system_identifier": original_system_id}


@pytest.mark.parametrize("start", [22, 23])
def test_production_copy_exact_transition_and_restorable_noop(migration_database, monkeypatch, start):
    db = migration_database
    if start == 23:
        apply_polymer_md_dev_migration(db["dsn"], environment="dev")
    options = production_evidence(db, monkeypatch)
    result = maintenance.apply_literature_prod_migration(db["dsn"], **options)
    assert result["applied"] == (["0023_polymer_md_jobs", LITERATURE_MIGRATION]
                                 if start == 22 else [LITERATURE_MIGRATION])
    assert result["operation_id"] == OPERATION and result["target_sha"] == TARGET
    assert result["database_system_identifier"] != result["production_source_system_identifier"]
    assert ledger(db) == expected_isolation_ledger(LITERATURE_MIGRATION)
    # A new 0024 dump is actually restored before the repeat is permitted.
    repeat = production_evidence(db, monkeypatch, suffix="repeat")
    assert maintenance.apply_literature_prod_migration(db["dsn"], **repeat)["applied"] == []
    with psycopg.connect(db["dsn"], row_factory=dict_row) as connection:
        assert validate_backup_schema(connection)["literature_schema"]["validated"]
    for role in ("api", "service"):
        with psycopg.connect(db[role]) as connection:
            assert connection.execute("SELECT count(*) FROM knowledge.literature_catalog").fetchone()[0] == 0
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute("DELETE FROM knowledge.literature_catalog")


@pytest.mark.parametrize("change", ["purpose", "operation", "sha", "system", "original", "identity", "unknown", "writer"])
def test_production_bindings_reject_wrong_operation_or_database(migration_database, monkeypatch, change):
    db = migration_database
    options = production_evidence(db, monkeypatch)
    document = json.loads(options["maintenance_evidence"].read_text())
    if change == "purpose": document["purpose"] = "literature-dev-0024"
    elif change == "operation": document["operation_id"] += "-other"
    elif change == "sha": document["target_sha"] = "b" * 40
    elif change == "system": document["database_system_identifier"] = str(int(document["database_system_identifier"]) + 1)
    elif change == "original":
        options["production_source_system_identifier"] = document["database_system_identifier"]
        document["production_source_system_identifier"] = document["database_system_identifier"]
    elif change == "identity": document["database_identity"]["database_oid"] += 1
    elif change == "unknown": document["unreviewed"] = True
    else: document["writer_states"]["backend"] = "running"
    private_json(options["maintenance_evidence"], document)
    with pytest.raises(ValueError):
        maintenance.apply_literature_prod_migration(db["dsn"], **options)
    assert len(ledger(db)) == 22


@pytest.mark.parametrize("change", ["disabled", "operation", "sha"])
def test_production_requires_its_owned_persistent_drain(migration_database, monkeypatch, change):
    db = migration_database
    options = production_evidence(db, monkeypatch)
    with psycopg.connect(db["dsn"]) as connection:
        if change == "disabled": connection.execute("UPDATE governance.deployment_control SET drain_enabled=false")
        elif change == "operation": connection.execute("UPDATE governance.deployment_control SET activated_by='another-operation'")
        else: connection.execute("UPDATE governance.deployment_control SET release_sha=%s", ("b" * 40,))
    with pytest.raises(ValueError, match="persistent drain"):
        maintenance.apply_literature_prod_migration(db["dsn"], **options)
    assert len(ledger(db)) == 22


def test_production_rejects_receipt_from_original_cluster(migration_database, monkeypatch):
    db = migration_database
    options = production_evidence(db, monkeypatch)
    receipt = json.loads(options["backup_receipt"].read_text())
    receipt["source"]["system_id"] = options["production_source_system_identifier"]
    private_json(options["backup_receipt"], receipt)
    evidence = json.loads(options["maintenance_evidence"].read_text())
    evidence["backup_receipt_sha256"] = hashlib.sha256(options["backup_receipt"].read_bytes()).hexdigest()
    private_json(options["maintenance_evidence"], evidence)
    with pytest.raises(ValueError, match="different source"):
        maintenance.apply_literature_prod_migration(db["dsn"], **options)
    assert len(ledger(db)) == 22


def test_production_23_and_24_rollback_together(migration_database, monkeypatch):
    db = migration_database
    options = production_evidence(db, monkeypatch)
    def fail(_): raise ValueError("synthetic final validation failure")
    monkeypatch.setattr(maintenance, "validate_literature_schema", fail)
    with pytest.raises(ValueError, match="synthetic final"):
        maintenance.apply_literature_prod_migration(db["dsn"], **options)
    assert len(ledger(db)) == 22
    with psycopg.connect(db["dsn"]) as connection:
        assert connection.execute("SELECT to_regnamespace('polymer_md')").fetchone()[0] is None
        assert connection.execute("SELECT to_regclass('knowledge.literature_catalog')").fetchone()[0] is None
        assert not connection.execute("SELECT EXISTS(SELECT FROM pg_roles WHERE rolname=%s)", (maintenance.ROLE,)).fetchone()[0]


def test_production_rechecks_stop_evidence_before_optional_ddl(migration_database, monkeypatch):
    from app.auth import assets
    db = migration_database
    options = production_evidence(db, monkeypatch)
    validate = assets.validate_assets
    def replace_after_validation(records, *, verify_source):
        validate(records, verify_source=verify_source)
        if verify_source:
            document = json.loads(options["maintenance_evidence"].read_text())
            document["writer_states"]["backend"] = "running"
            private_json(options["maintenance_evidence"], document)
    monkeypatch.setattr(assets, "validate_assets", replace_after_validation)
    with pytest.raises(ValueError, match="inputs changed"):
        maintenance.apply_literature_prod_migration(db["dsn"], **options)
    assert len(ledger(db)) == 22


def test_production_evidence_change_during_validation_rolls_back_ddl(migration_database, monkeypatch):
    db = migration_database
    options = production_evidence(db, monkeypatch)
    validate = maintenance.validate_literature_schema
    def change_after_validation(connection):
        validate(connection)
        document = json.loads(options["maintenance_evidence"].read_text())
        document["submission_disabled"] = False
        private_json(options["maintenance_evidence"], document)
    monkeypatch.setattr(maintenance, "validate_literature_schema", change_after_validation)
    with pytest.raises(ValueError, match="before commit"):
        maintenance.apply_literature_prod_migration(db["dsn"], **options)
    assert len(ledger(db)) == 22
    with psycopg.connect(db["dsn"]) as connection:
        assert connection.execute("SELECT to_regnamespace('polymer_md')").fetchone()[0] is None


@pytest.mark.parametrize("change", ["environment", "stopped", "operation", "sha", "revision", "original"])
def test_production_opt_in_and_release_binding_precede_connections(monkeypatch, change):
    monkeypatch.setenv("BUILD_REVISION", TARGET)
    options = {"environment": "prod", "operation_id": OPERATION, "target_sha": TARGET,
               "production_source_system_identifier": "123", "writers_stopped": True,
               "backup_receipt": Path("absent"), "maintenance_evidence": Path("absent")}
    if change == "environment": options["environment"] = "dev"
    elif change == "stopped": options["writers_stopped"] = False
    elif change == "operation": options["operation_id"] = "../bad"
    elif change == "sha": options["target_sha"] = "main"
    elif change == "revision": monkeypatch.setenv("BUILD_REVISION", "b" * 40)
    else: options["production_source_system_identifier"] = "0"
    def never_connect(*args, **kwargs): pytest.fail("Rejected production input reached PostgreSQL")
    monkeypatch.setattr(maintenance.psycopg, "connect", never_connect)
    with pytest.raises(ValueError):
        maintenance.apply_literature_prod_migration("not-a-dsn", **options)


def test_duplicate_stop_evidence_keys_are_rejected(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text('{"writers_stopped":false,"writers_stopped":true}')
    path.chmod(0o600)
    with pytest.raises(ValueError, match="unique"):
        maintenance._private_json(path)


def test_production_cli_requires_bindings_and_redacts_errors(monkeypatch, tmp_path, capsys):
    args = ["migrations", "--mode", "literature-prod-0024", "--dsn-env", "SYNTHETIC_MAINTENANCE_DSN",
            "--backup-receipt", "missing", "--maintenance-evidence", "missing", "--writers-stopped",
            "--audit-output", str(tmp_path / "audit.json")]
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(SystemExit) as missing: command.main()
    assert missing.value.code == 2 and not (tmp_path / "audit.json").exists()
    monkeypatch.setattr(sys, "argv", args + ["--operation-id", OPERATION, "--target-sha", TARGET,
                        "--production-source-system-identifier", "123"])
    monkeypatch.setenv("SYNTHETIC_MAINTENANCE_DSN", "private-test-dsn-never-logged")
    monkeypatch.setattr(command, "Settings", lambda: SimpleNamespace(gpu_broker_environment="prod"))
    def fail(dsn, **bindings):
        assert dsn == "private-test-dsn-never-logged"
        assert bindings["operation_id"] == OPERATION and bindings["target_sha"] == TARGET
        assert bindings["environment"] == "prod"
        raise ValueError(dsn)
    monkeypatch.setattr(maintenance, "apply_literature_prod_migration", fail)
    with pytest.raises(SystemExit) as failed: command.main()
    assert "private-test-dsn" not in str(failed.value)
    output = capsys.readouterr()
    assert "private-test-dsn" not in output.out + output.err
    audit = tmp_path / "audit.json"
    assert json.loads(audit.read_text()) == {"status": "failed", "error_type": "ValueError"}
    assert audit.stat().st_mode & 0o777 == 0o600


def test_production_binding_arguments_cannot_enable_ordinary_expand(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["migrations", "--mode", "expand", "--operation-id", OPERATION])
    with pytest.raises(SystemExit) as invalid: command.main()
    assert invalid.value.code == 2
