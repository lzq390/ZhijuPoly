from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from app import postgres_migrations
from app.config import PROJECT_ROOT
from app.migration_policy import assert_pending_migrations_allowed, validate_migration_manifest
from app.postgres_migrations import database_is_fresh_for_bootstrap


MIGRATIONS_DIR = PROJECT_ROOT / "backend" / "migrations" / "postgres"


def test_repository_migration_manifest_classifies_every_sql_file() -> None:
    kinds = validate_migration_manifest(MIGRATIONS_DIR)

    assert kinds["0001_app_data_governance"] == "baseline"
    assert kinds["0009_monomer_md_job_leases"] == "expand"
    assert kinds["0010_deployment_control"] == "expand"
    assert kinds["0011_monomer_md_demo_steps"] == "expand"
    assert kinds["0012_drop_polytao_jobs"] == "contract"
    assert set(kinds) == {path.stem for path in MIGRATIONS_DIR.glob("*.sql")}


def test_manifest_rejects_unclassified_sql_migration(tmp_path: Path) -> None:
    (tmp_path / "0001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0002_unclassified.sql").write_text("SELECT 2;", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "migrations": [{"version": "0001_first", "kind": "baseline"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unclassified SQL migrations: 0002_unclassified"):
        validate_migration_manifest(tmp_path)


@pytest.mark.parametrize(
    "migrations",
    [
        [
            {"version": "0001_first", "kind": "baseline"},
            {"version": "0002_second", "kind": "baseline"},
        ],
        [
            {"version": "0001_first", "kind": "expand"},
            {"version": "0002_second", "kind": "baseline"},
        ],
        [
            {"version": "0001_first", "kind": "expand"},
        ],
    ],
)
def test_manifest_forbids_any_post_bootstrap_baseline(
    tmp_path: Path,
    migrations: list[dict[str, str]],
) -> None:
    for migration in migrations:
        (tmp_path / f"{migration['version']}.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "migrations": migrations}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="exactly one baseline.+first migration"):
        validate_migration_manifest(tmp_path)


def test_manifest_rejects_expand_after_contract(tmp_path: Path) -> None:
    migrations = [
        {"version": "0001_first", "kind": "baseline"},
        {"version": "0002_remove_old", "kind": "contract"},
        {"version": "0003_expand_after_contract", "kind": "expand"},
    ]
    for migration in migrations:
        (tmp_path / f"{migration['version']}.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "migrations": migrations}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Contract migrations must form the trailing"):
        validate_migration_manifest(tmp_path)


def test_automated_migration_policy_rejects_baseline_and_contract() -> None:
    kinds = {"0001_baseline": "baseline", "0002_expand": "expand", "0003_contract": "contract"}

    with pytest.raises(RuntimeError, match=r"0001_baseline \(baseline\).+0003_contract \(contract\)"):
        assert_pending_migrations_allowed(list(kinds), kinds, {"expand"})

    assert_pending_migrations_allowed(["0002_expand"], kinds, {"expand"})


def test_existing_bootstrap_must_not_implicitly_apply_contract() -> None:
    kinds = {"0012_drop_polytao_jobs": "contract"}
    with pytest.raises(RuntimeError, match="0012_drop_polytao_jobs \\(contract\\)"):
        assert_pending_migrations_allowed(
            ["0012_drop_polytao_jobs"],
            kinds,
            {"baseline", "expand"},
        )


@pytest.mark.parametrize("mode", ["expand", "restore-expand"])
def test_automated_expand_cli_defers_only_trailing_contracts(monkeypatch, mode: str) -> None:
    captured: dict[str, object] = {}

    def fake_apply(dsn, migrations_dir, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(postgres_migrations, "apply_postgres_migrations", fake_apply)
    monkeypatch.setattr(sys, "argv", ["postgres_migrations", "--mode", mode])

    postgres_migrations.main()

    assert captured == {
        "allowed_kinds": {"expand"} if mode == "expand" else {"baseline", "expand"},
        "allow_contract_on_fresh_database": False,
        "defer_trailing_contracts": True,
    }


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _BootstrapConnection:
    def __init__(self, state: dict[str, object] | None, *, ledger_row=None):
        self.state = state
        self.ledger_row = ledger_row
        self.queries: list[str] = []

    def execute(self, query: str):
        self.queries.append(query)
        if "FROM governance.schema_migrations LIMIT 1" in query:
            return _Result(self.ledger_row)
        return _Result(self.state)


def _bootstrap_state(**overrides: object) -> dict[str, object]:
    state: dict[str, object] = {
        "ledger_relation": None,
        "governance_schema_exists": False,
        "unexpected_schema_exists": False,
        "application_relation_exists": False,
        "application_type_exists": False,
        "application_routine_exists": False,
    }
    state.update(overrides)
    return state


def test_fresh_bootstrap_accepts_pristine_database_and_only_an_empty_ledger() -> None:
    assert database_is_fresh_for_bootstrap(
        _BootstrapConnection(_bootstrap_state())
    ) is True
    assert database_is_fresh_for_bootstrap(
        _BootstrapConnection(
            _bootstrap_state(
                ledger_relation="governance.schema_migrations",
                governance_schema_exists=True,
            )
        )
    ) is True


def test_fresh_bootstrap_rejects_recorded_or_partial_governance_state() -> None:
    populated_ledger = _BootstrapConnection(
        _bootstrap_state(
            ledger_relation="governance.schema_migrations",
            governance_schema_exists=True,
        ),
        ledger_row={"exists": 1},
    )
    assert database_is_fresh_for_bootstrap(populated_ledger) is False

    # The exception is specifically for the empty ledger, not for an empty
    # managed governance schema left behind by a partial bootstrap.
    assert database_is_fresh_for_bootstrap(
        _BootstrapConnection(_bootstrap_state(governance_schema_exists=True))
    ) is False
    assert database_is_fresh_for_bootstrap(
        _BootstrapConnection(
            _bootstrap_state(
                ledger_relation="governance.schema_migrations",
                governance_schema_exists=True,
                application_relation_exists=True,
            )
        )
    ) is False


@pytest.mark.parametrize(
    ("scenario", "state_field"),
    [
        ("empty generation schema", "unexpected_schema_exists"),
        ("application table", "application_relation_exists"),
        ("application view", "application_relation_exists"),
        ("application sequence", "application_relation_exists"),
        ("application enum or domain", "application_type_exists"),
        ("application function", "application_routine_exists"),
    ],
)
def test_fresh_bootstrap_rejects_any_application_schema_or_object(
    scenario: str,
    state_field: str,
) -> None:
    del scenario
    assert database_is_fresh_for_bootstrap(
        _BootstrapConnection(_bootstrap_state(**{state_field: True}))
    ) is False


def test_fresh_bootstrap_catalog_query_covers_non_table_ownership_markers() -> None:
    connection = _BootstrapConnection(_bootstrap_state())

    assert database_is_fresh_for_bootstrap(connection) is True

    catalog_query = connection.queries[0]
    assert "pg_namespace" in catalog_query
    assert "c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')" in catalog_query
    assert "t.typtype IN ('c', 'd', 'e', 'r', 'm')" in catalog_query
    assert "FROM pg_proc" in catalog_query


def test_fresh_bootstrap_fails_closed_when_catalog_snapshot_is_missing() -> None:
    assert database_is_fresh_for_bootstrap(_BootstrapConnection(None)) is False


def test_contract_sql_has_bounded_lock_and_never_cascades() -> None:
    sql = (MIGRATIONS_DIR / "0012_drop_polytao_jobs.sql").read_text(encoding="utf-8")
    assert "SET LOCAL lock_timeout = '10s'" in sql
    assert "DROP TABLE IF EXISTS generation.polytao_jobs" in sql
    assert "DROP SCHEMA IF EXISTS generation" in sql
    assert "CASCADE" not in sql.upper()
    assert "dependent_objects_still_exist" not in sql
    assert "EXCEPTION" not in sql.upper()
