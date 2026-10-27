"""Exercise the real offline entry on disposable PG16, without source/engines."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest

from app.auth.backup import backup_and_verify
from app.auth.isolation_ledger import database_identity, expected_isolation_ledger
from app.literature_contract import LITERATURE_MIGRATION
from app.postgres_migrations import apply_postgres_migrations, MIGRATIONS_DIR
from app.services import literature_schema as maintenance
from app.services.polymer_md_schema import apply_polymer_md_dev_migration
from test_auth_isolation import auth_database


def private_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


@pytest.fixture(scope="module")
def main22_database(auth_database):
    apply_postgres_migrations(auth_database["admin"], allowed_kinds={"expand"})
    return auth_database


@pytest.fixture
def migration_database(main22_database, tmp_path):
    base = os.environ["ISOLATION_TEST_ADMIN_DSN"]
    name = "auth_test_" + uuid4().hex[:12]
    restore_name = "auth_test_" + uuid4().hex[:12]
    from psycopg.conninfo import conninfo_to_dict
    template = conninfo_to_dict(main22_database["admin"])["dbname"]
    with psycopg.connect(base, autocommit=True, row_factory=dict_row) as guard:
        guard.execute("SELECT pg_advisory_lock(742128925057024)")
        # Other optional-DDL tests namespace this role. This module alone tests
        # the original production SQL and must never delete a pre-existing role.
        assert not guard.execute("SELECT EXISTS(SELECT FROM pg_roles WHERE rolname=%s) AS present", (maintenance.ROLE,)).fetchone()["present"]
        guard.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(name), sql.Identifier(template)))
        dsn = make_conninfo(base, dbname=name)
        try:
            yield {"dsn": dsn, "base": base, "name": name, "restore": restore_name,
                   "directory": tmp_path, "api": make_conninfo(main22_database["api"], dbname=name),
                   "service": make_conninfo(main22_database["service"], dbname=name)}
        finally:
            for database in (name, restore_name):
                guard.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))
            guard.execute("DROP ROLE IF EXISTS nexpoly_literature_import")
            guard.execute("SELECT pg_advisory_unlock(742128925057024)")


def evidence_for(db, *, suffix="before"):
    with psycopg.connect(db["base"], autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db["restore"])))
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db["restore"])))
    receipt = backup_and_verify(db["dsn"], make_conninfo(db["base"], dbname=db["restore"]),
                                db["directory"] / ("backup-" + suffix))
    with psycopg.connect(db["dsn"], row_factory=dict_row) as connection:
        identity = database_identity(connection)
    evidence = private_json(db["directory"] / ("maintenance-" + suffix + ".json"), {
        "version": 1, "purpose": "literature-dev-0024", "database_identity": identity,
        "backup_receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
        "writers_stopped": True, "submission_disabled": True, "active_jobs": 0, "active_leases": 0,
        "writer_states": {name: "not_deployed" for name in maintenance.WRITERS},
    })
    return {"backup_receipt": receipt, "maintenance_evidence": evidence, "writers_stopped": True}


def migrate(db, evidence):
    return maintenance.apply_literature_dev_migration(db["dsn"], environment="dev", **evidence)


def ledger(db):
    with psycopg.connect(db["dsn"], row_factory=dict_row) as connection:
        return [dict(row) for row in connection.execute("SELECT version,checksum FROM governance.schema_migrations ORDER BY version")]


@pytest.mark.parametrize("start", [22, 23])
def test_exact_prefix_to_24_and_runtime_acl(migration_database, start):
    db = migration_database
    if start == 23:
        apply_polymer_md_dev_migration(db["dsn"], environment="dev")
    result = migrate(db, evidence_for(db))
    assert result["applied"] == (["0023_polymer_md_jobs", LITERATURE_MIGRATION] if start == 22 else [LITERATURE_MIGRATION])
    assert ledger(db) == expected_isolation_ledger(LITERATURE_MIGRATION)
    with psycopg.connect(db["dsn"], row_factory=dict_row) as connection:
        connection.execute("SET LOCAL search_path=knowledge,public")
        maintenance.validate_literature_schema(connection)
        assert connection.execute("SELECT current_setting('search_path') AS value").fetchone()["value"] == "knowledge, public"
    for key in ("api", "service"):
        with psycopg.connect(db[key]) as connection:
            assert connection.execute("SELECT count(*) FROM knowledge.literature_catalog").fetchone()[0] == 0
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute("INSERT INTO knowledge.literature_generations(generation_id,status,normalizer_version) VALUES(%s,'building','literature-v2')", (uuid4(),))


def test_24_repeat_is_validated_noop(migration_database):
    db = migration_database
    migrate(db, evidence_for(db))
    before = ledger(db)
    result = migrate(db, evidence_for(db, suffix="repeat"))
    assert result["applied"] == []
    assert ledger(db) == before


def test_ordinary_expand_keeps_both_features_deferred(main22_database):
    apply_postgres_migrations(main22_database["admin"], allowed_kinds={"expand"})
    assert ledger({"dsn": main22_database["admin"]}) == expected_isolation_ledger("0022_usage_entitlements")


@pytest.mark.parametrize("change", ["identity", "writers", "active", "missing_writer", "receipt_hash"])
def test_evidence_refuses_wrong_database_or_incomplete_stop(migration_database, change):
    db = migration_database
    evidence = evidence_for(db)
    document = json.loads(evidence["maintenance_evidence"].read_text())
    if change == "identity": document["database_identity"]["database_oid"] += 1
    elif change == "writers": document["writers_stopped"] = False
    elif change == "active": document["active_jobs"] = 1
    elif change == "missing_writer": del document["writer_states"]["backend"]
    else: document["backup_receipt_sha256"] = "f" * 64
    private_json(evidence["maintenance_evidence"], document)
    with pytest.raises(ValueError): migrate(db, evidence)
    assert len(ledger(db)) == 22


@pytest.mark.parametrize("change", ["checksum", "missing", "unknown"])
def test_drifted_ledger_refused_without_optional_ddl(migration_database, change):
    db = migration_database
    evidence = evidence_for(db)
    with psycopg.connect(db["dsn"]) as connection:
        if change == "checksum": connection.execute("UPDATE governance.schema_migrations SET checksum=%s WHERE version='0002_lab_identity_defaults'", ("f" * 64,))
        elif change == "missing": connection.execute("DELETE FROM governance.schema_migrations WHERE version='0021_usage_metering'")
        else: connection.execute("INSERT INTO governance.schema_migrations(version,checksum) VALUES('0025_unknown',%s)", ("f" * 64,))
    with pytest.raises(ValueError, match="exact canonical"): migrate(db, evidence)
    with psycopg.connect(db["dsn"]) as connection:
        assert connection.execute("SELECT to_regnamespace('polymer_md')").fetchone()[0] is None


def test_preexisting_cluster_role_keeps_provenance_refusal(migration_database):
    db = migration_database
    evidence = evidence_for(db)
    with psycopg.connect(db["dsn"]) as connection:
        connection.execute("CREATE ROLE nexpoly_literature_import NOLOGIN")
    with pytest.raises(ValueError, match="provenance review"): migrate(db, evidence)
    assert len(ledger(db)) == 22


def test_runtime_login_cannot_perform_maintenance(migration_database):
    db = migration_database
    evidence = evidence_for(db)
    with pytest.raises((ValueError, psycopg.errors.InsufficientPrivilege)):
        maintenance.apply_literature_dev_migration(db["api"], environment="dev", **evidence)
    assert len(ledger(db)) == 22


def test_nonterminal_usage_observations_are_preserved_and_audited(migration_database, monkeypatch):
    from app.auth.usage_backup import usage_backup_seal
    from task_usage.ledger import record_event
    db = migration_database
    monkeypatch.setenv("USAGE_METERING_ENABLED", "true")
    with psycopg.connect(db["dsn"], row_factory=dict_row) as connection:
        owner = str(connection.execute("SELECT user_id FROM auth.users WHERE NOT is_system LIMIT 1").fetchone()["user_id"])
        connection.execute("SET LOCAL ROLE nexpoly_service")
        connection.execute("SELECT set_config('app.user_id',%s,true)", (owner,))
        record_event(connection, owner_user_id=owner, module="conditional_generation", task_id="synthetic-old-observation",
                     event_key="synthetic-start", status="running", occurred_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    with psycopg.connect(db["dsn"], row_factory=dict_row) as connection:
        before = usage_backup_seal(connection)
    result = migrate(db, evidence_for(db))
    assert result["nonterminal_usage_observations"] == 1
    with psycopg.connect(db["dsn"], row_factory=dict_row) as connection:
        assert usage_backup_seal(connection) == before
        assert connection.execute("SELECT status FROM usage.attempts WHERE task_id='synthetic-old-observation'").fetchone()["status"] == "running"


def test_failed_24_validation_rolls_back_23_role_and_ledger(migration_database, monkeypatch):
    db = migration_database
    evidence = evidence_for(db)
    def fail(_): raise ValueError("synthetic final validation failure")
    monkeypatch.setattr(maintenance, "validate_literature_schema", fail)
    with pytest.raises(ValueError, match="synthetic final"): migrate(db, evidence)
    assert len(ledger(db)) == 22
    with psycopg.connect(db["dsn"]) as connection:
        assert connection.execute("SELECT to_regnamespace('polymer_md')").fetchone()[0] is None
        assert connection.execute("SELECT to_regclass('knowledge.literature_catalog')").fetchone()[0] is None
        assert not connection.execute("SELECT EXISTS(SELECT FROM pg_roles WHERE rolname=%s)", (maintenance.ROLE,)).fetchone()[0]


@pytest.mark.parametrize("change", ["business", "usage", "backup_file", "active_job", "active_lease"])
def test_backup_drift_and_live_work_are_refused(migration_database, change):
    db = migration_database
    evidence = evidence_for(db)
    if change == "backup_file":
        document = json.loads(evidence["backup_receipt"].read_text())
        with Path(document["backup_path"]).open("ab") as handle: handle.write(b"changed")
    else:
        with psycopg.connect(db["dsn"]) as connection:
            if change == "business": connection.execute("UPDATE auth.users SET username=username||'-changed' WHERE NOT is_system")
            elif change == "usage":
                document = json.loads(evidence["backup_receipt"].read_text())
                document["usage_state"]["tables"]["usage.events"]["sha256"] = "f" * 64
                private_json(evidence["backup_receipt"], document)
                stop = json.loads(evidence["maintenance_evidence"].read_text())
                stop["backup_receipt_sha256"] = hashlib.sha256(evidence["backup_receipt"].read_bytes()).hexdigest()
                private_json(evidence["maintenance_evidence"], stop)
            else:
                connection.execute("INSERT INTO md.monomer_md_jobs(job_id,status,input_smiles,canonical_smiles,owner_user_id,lease_expires_at) SELECT 'synthetic-job',%s,'CC','CC',user_id,%s FROM auth.users WHERE NOT is_system LIMIT 1", ("pending" if change == "active_job" else "completed", None if change == "active_job" else "2100-01-01"))
    with pytest.raises(ValueError): migrate(db, evidence)
    assert len(ledger(db)) == 22


@pytest.mark.parametrize("change", ["trigger", "column", "acl", "role", "ownership"])
def test_noop_detects_schema_and_privilege_drift(migration_database, change):
    db = migration_database
    migrate(db, evidence_for(db))
    evidence = evidence_for(db, suffix="repeat")
    with psycopg.connect(db["dsn"]) as connection:
        if change == "trigger": connection.execute("ALTER TABLE knowledge.literature_snapshots DISABLE TRIGGER literature_snapshot_immutable")
        elif change == "column": connection.execute("ALTER TABLE knowledge.literature_catalog ADD COLUMN unexpected text")
        elif change == "acl": connection.execute("GRANT INSERT ON knowledge.literature_catalog TO nexpoly_api")
        elif change == "role": connection.execute("ALTER ROLE nexpoly_literature_import LOGIN")
        else: connection.execute("ALTER FUNCTION knowledge.reject_literature_snapshot_mutation() OWNER TO nexpoly_api")
    with pytest.raises(ValueError): migrate(db, evidence)


@pytest.mark.parametrize("update_manifest", [False, True])
def test_optional_sql_bytes_are_pinned(migration_database, tmp_path, update_manifest):
    db = migration_database
    evidence = evidence_for(db)
    target = tmp_path / "changed-migrations"
    shutil.copytree(MIGRATIONS_DIR, target)
    with (target / "0024_literature_projection.sql").open("a") as handle: handle.write("\n-- unreviewed bytes\n")
    if update_manifest:
        manifest = json.loads((target / "manifest.json").read_text())
        manifest["migrations"][-1]["checksum"] = hashlib.sha256((target / "0024_literature_projection.sql").read_bytes()).hexdigest()
        (target / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        maintenance.apply_literature_dev_migration(db["dsn"], environment="dev", migrations_dir=target, **evidence)
    assert len(ledger(db)) == 22


@pytest.mark.parametrize("environment,stopped", [("production", True), ("disabled", True), ("dev", False)])
def test_explicit_dev_and_stop_confirmation_precede_connections(environment, stopped):
    with pytest.raises(ValueError, match="requires dev"):
        maintenance.apply_literature_dev_migration("not-a-dsn", environment=environment,
            backup_receipt=Path("absent"), maintenance_evidence=Path("absent"), writers_stopped=stopped)


@pytest.mark.parametrize("change", ["public_mode", "symlink", "hardlink"])
def test_maintenance_inputs_require_private_regular_files(tmp_path, change):
    source = private_json(tmp_path / "input.json", {"version": 1})
    target = source
    if change == "public_mode": source.chmod(0o644)
    elif change == "symlink":
        target = tmp_path / "linked.json"
        target.symlink_to(source)
    else: os.link(source, tmp_path / "second-name.json")
    with pytest.raises(ValueError): maintenance._private_json(target)


def test_cli_requires_private_evidence_and_redacts_failures(monkeypatch, tmp_path, capsys):
    from app import postgres_migrations as command
    monkeypatch.setattr(sys, "argv", ["migrations", "--mode", "literature-dev-0024"])
    with pytest.raises(SystemExit) as missing: command.main()
    assert missing.value.code == 2
    output = tmp_path / "audit.json"
    monkeypatch.setenv("SYNTHETIC_MAINTENANCE_DSN", "secret-dsn-for-redaction-test")
    monkeypatch.setattr(sys, "argv", ["migrations", "--mode", "literature-dev-0024", "--dsn-env", "SYNTHETIC_MAINTENANCE_DSN",
        "--backup-receipt", "missing", "--maintenance-evidence", "missing", "--writers-stopped", "--audit-output", str(output)])
    def fail(*args, **kwargs): raise ValueError("secret-dsn-for-redaction-test")
    monkeypatch.setattr(maintenance, "apply_literature_dev_migration", fail)
    with pytest.raises(SystemExit) as failed: command.main()
    assert "secret-dsn" not in str(failed.value)
    assert "secret-dsn" not in capsys.readouterr().out
    assert json.loads(output.read_text()) == {"status": "failed", "error_type": "ValueError"}
    assert output.stat().st_mode & 0o777 == 0o600
