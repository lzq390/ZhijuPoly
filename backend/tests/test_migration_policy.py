from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys

import pytest

from app import postgres_migrations
from app.config import PROJECT_ROOT
from app.migration_policy import (
    assert_pending_migrations_allowed,
    canonical_migration_checksum,
    validate_migration_manifest,
    validate_migration_manifest_entries,
)
from app.migration_compatibility import (
    FORWARD_COMPATIBLE_MIGRATION,
    FORWARD_COMPATIBLE_MIGRATIONS,
    compatible_forward_versions,
    require_known_or_exact_forward_ledger,
)
from app.postgres_migrations import (
    POLYTAO_CONTRACT_CHECKSUM,
    database_is_fresh_for_bootstrap,
)


MIGRATIONS_DIR = PROJECT_ROOT / "backend" / "migrations" / "postgres"


def _guard_digest(document: str) -> str:
    return f"sha256:{hashlib.sha256(document.encode('utf-8')).hexdigest()}"


def _contract_guard() -> tuple[str, str]:
    entries = validate_migration_manifest_entries(MIGRATIONS_DIR)
    target_index = next(
        index
        for index, entry in enumerate(entries)
        if entry.version == postgres_migrations.POLYTAO_CONTRACT_VERSION
    )
    archive_evidence = {
        "schema_version": 2,
        "row_count": 0,
        "status_counts": {},
        "rows_sha256": hashlib.sha256(b"[]").hexdigest(),
        "schema_sha256": "1" * 64,
        "structure_counts": {
            "columns": 0,
            "indexes": 0,
            "constraints": 0,
            "triggers": 0,
        },
    }
    release_sha = "a" * 40
    operation_id = "contract-0012-unit"
    document = {
        "schema_version": 1,
        "contract": {
            "version": postgres_migrations.POLYTAO_CONTRACT_VERSION,
            "checksum": postgres_migrations.POLYTAO_CONTRACT_CHECKSUM,
        },
        "maintenance": {
            "operation_id": operation_id,
            "marker_sha256": f"sha256:{'2' * 64}",
            "audit_manifest_sha256": f"sha256:{'3' * 64}",
        },
        "database": {
            "name": "nexpoly_guard_test",
            "system_identifier": "123456789",
        },
        "release_sha": release_sha,
        "ledger": [
            {"version": entry.version, "checksum": entry.checksum}
            for entry in entries[:target_index]
        ],
        "relation": {
            "qualified_name": "generation.polytao_jobs",
            "namespace_oid": 42,
            "relation_oid": 43,
            "rows_sha256": archive_evidence["rows_sha256"],
            "schema_sha256": archive_evidence["schema_sha256"],
        },
        "archive_evidence": archive_evidence,
        "archive_evidence_sha256": postgres_migrations._canonical_json_sha256(
            archive_evidence
        ),
        "deployment_control": {
            "control_key": "production",
            "drain_enabled": True,
            "reason": f"0012 maintenance {operation_id}",
            "release_sha": release_sha,
            "activated_by": "pull-contract-0012",
        },
        "active_jobs": {
            "generation.polytao_jobs": 0,
            "md.monomer_md_jobs": 0,
            "online_knowledge.jobs": 0,
        },
    }
    guard_json = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return guard_json, _guard_digest(guard_json)


def test_repository_migration_manifest_classifies_every_sql_file() -> None:
    kinds = validate_migration_manifest(MIGRATIONS_DIR)
    entries = validate_migration_manifest_entries(MIGRATIONS_DIR)

    assert kinds["0001_app_data_governance"] == "baseline"
    assert kinds["0009_monomer_md_job_leases"] == "expand"
    assert kinds["0010_deployment_control"] == "expand"
    assert kinds["0011_monomer_md_demo_steps"] == "expand"
    assert kinds["0012_drop_polytao_jobs"] == "contract"
    assert kinds["0013_monomer_dft_jobs"] == "expand"
    assert kinds["0014_monomer_md_task_queue_cancel"] == "expand"
    assert set(kinds) == {path.stem for path in MIGRATIONS_DIR.glob("*.sql")}
    assert {entry.manifest_schema_version for entry in entries} == {2}
    assert {entry.epoch for entry in entries} == {1, 2}
    contract = next(
        entry for entry in entries if entry.version == "0012_drop_polytao_jobs"
    )
    assert contract.checksum == POLYTAO_CONTRACT_CHECKSUM
    dft = next(entry for entry in entries if entry.version == "0013_monomer_dft_jobs")
    assert dft.kind == "expand"
    assert dft.epoch == 2
    assert dft.checksum == (
        "ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc"
    )
    assert [
        (requirement.version, requirement.checksum)
        for requirement in dft.requires_contracts
    ] == [("0012_drop_polytao_jobs", POLYTAO_CONTRACT_CHECKSUM)]
    md_queue = next(
        entry for entry in entries
        if entry.version == "0014_monomer_md_task_queue_cancel"
    )
    assert md_queue.epoch == 2
    assert md_queue.checksum == (
        "7d91b451371eaf10542440c8b947c9ac50b51e3d553cb205a76aca196eaf8df6"
    )


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


def _write_v2_manifest(
    directory: Path,
    migrations: list[dict[str, object]],
) -> None:
    for migration in migrations:
        path = directory / f"{migration['version']}.sql"
        path.write_text(f"SELECT '{migration['version']}';\n", encoding="utf-8")
        migration["checksum"] = canonical_migration_checksum(path)
    (directory / "manifest.json").write_text(
        json.dumps({"schema_version": 2, "migrations": migrations}),
        encoding="utf-8",
    )


def test_v2_manifest_allows_expand_in_new_epoch_after_checksum_bound_contract(
    tmp_path: Path,
) -> None:
    migrations: list[dict[str, object]] = [
        {
            "version": "0001_first",
            "kind": "baseline",
            "epoch": 1,
            "requires_contracts": [],
        },
        {
            "version": "0002_remove_old",
            "kind": "contract",
            "epoch": 1,
            "requires_contracts": [],
        },
        {
            "version": "0003_expand_after_contract",
            "kind": "expand",
            "epoch": 2,
            "requires_contracts": [],
        },
    ]
    _write_v2_manifest(tmp_path, migrations)
    migrations[2]["requires_contracts"] = [
        {
            "version": "0002_remove_old",
            "checksum": migrations[1]["checksum"],
        }
    ]
    (tmp_path / "manifest.json").write_text(
        json.dumps({"schema_version": 2, "migrations": migrations}),
        encoding="utf-8",
    )

    entries = validate_migration_manifest_entries(tmp_path)

    assert [entry.epoch for entry in entries] == [1, 1, 2]
    assert entries[2].requires_contracts[0].version == "0002_remove_old"


def test_v2_manifest_rejects_new_epoch_without_all_prior_contracts(tmp_path: Path) -> None:
    migrations: list[dict[str, object]] = [
        {
            "version": "0001_first",
            "kind": "baseline",
            "epoch": 1,
            "requires_contracts": [],
        },
        {
            "version": "0002_remove_old",
            "kind": "contract",
            "epoch": 1,
            "requires_contracts": [],
        },
        {
            "version": "0003_expand_after_contract",
            "kind": "expand",
            "epoch": 2,
            "requires_contracts": [],
        },
    ]
    _write_v2_manifest(tmp_path, migrations)

    with pytest.raises(ValueError, match="must require every contract"):
        validate_migration_manifest_entries(tmp_path)


def test_v2_manifest_rejects_sql_checksum_drift(tmp_path: Path) -> None:
    migrations: list[dict[str, object]] = [
        {
            "version": "0001_first",
            "kind": "baseline",
            "epoch": 1,
            "requires_contracts": [],
        }
    ]
    _write_v2_manifest(tmp_path, migrations)
    (tmp_path / "0001_first.sql").write_text("SELECT 'changed';\n", encoding="utf-8")

    with pytest.raises(ValueError, match="does not match canonical SQL checksum"):
        validate_migration_manifest_entries(tmp_path)


class _LedgerResult:
    def __init__(self, rows=(), row=None):
        self._rows = list(rows)
        self._row = row

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row


class _LedgerConnection:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.queries: list[str] = []

    def execute(self, query: str, params=None):
        del params
        self.queries.append(query)
        if "SELECT version, checksum FROM governance.schema_migrations" in query:
            return _LedgerResult(self.rows)
        return _LedgerResult()


def test_epoch_dependency_failure_precedes_any_migration_sql(
    tmp_path: Path,
    monkeypatch,
) -> None:
    migrations: list[dict[str, object]] = [
        {
            "version": "0001_first",
            "kind": "baseline",
            "epoch": 1,
            "requires_contracts": [],
        },
        {
            "version": "0002_remove_old",
            "kind": "contract",
            "epoch": 1,
            "requires_contracts": [],
        },
        {
            "version": "0003_next_epoch",
            "kind": "expand",
            "epoch": 2,
            "requires_contracts": [],
        },
    ]
    _write_v2_manifest(tmp_path, migrations)
    migrations[2]["requires_contracts"] = [
        {
            "version": "0002_remove_old",
            "checksum": migrations[1]["checksum"],
        }
    ]
    (tmp_path / "manifest.json").write_text(
        json.dumps({"schema_version": 2, "migrations": migrations}),
        encoding="utf-8",
    )
    connection = _LedgerConnection()

    @contextmanager
    def fake_connection(_dsn):
        yield connection

    monkeypatch.setattr(postgres_migrations, "postgres_connection", fake_connection)

    with pytest.raises(RuntimeError, match="requires approved contract 0002_remove_old"):
        postgres_migrations.apply_postgres_migrations(
            "postgresql://fixture",
            tmp_path,
            allowed_kinds={"baseline", "expand"},
            defer_trailing_contracts=True,
        )

    # Creating/verifying the governance ledger is permitted, but no migration
    # body (including the epoch-1 baseline) may run after planning fails.
    assert all("SELECT '0001_first'" not in query for query in connection.queries)


def test_dirty_dev_image_0009_checksum_is_not_silently_accepted(monkeypatch) -> None:
    dirty_image_checksum = (
        "79a6956fc934794d61bc003f02a6b5280e9e8bd77a217b61a28d3dbdb8b7be0b"
    )
    assert canonical_migration_checksum(
        MIGRATIONS_DIR / "0009_monomer_md_job_leases.sql"
    ) == "ef1757a81976f351459e8257bd492aa6267cbf507c4ea85506fefa2d465d2db8"
    connection = _LedgerConnection(
        [
            {
                "version": "0009_monomer_md_job_leases",
                "checksum": dirty_image_checksum,
            }
        ]
    )

    @contextmanager
    def fake_connection(_dsn):
        yield connection

    monkeypatch.setattr(postgres_migrations, "postgres_connection", fake_connection)

    with pytest.raises(
        RuntimeError,
        match=(
            "0009_monomer_md_job_leases was already applied with checksum "
            + dirty_image_checksum
        ),
    ):
        postgres_migrations.apply_postgres_migrations(
            "postgresql://fixture",
            MIGRATIONS_DIR,
            allowed_kinds={"expand"},
            defer_trailing_contracts=True,
        )

    migration_bodies = [
        path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
    ]
    assert not any(body in query for body in migration_bodies for query in connection.queries)


def test_unknown_ledger_version_is_rejected_before_any_migration_sql(
    monkeypatch,
) -> None:
    connection = _LedgerConnection(
        [{"version": "9999_unreviewed", "checksum": "f" * 64}]
    )

    @contextmanager
    def fake_connection(_dsn):
        yield connection

    monkeypatch.setattr(postgres_migrations, "postgres_connection", fake_connection)

    with pytest.raises(RuntimeError, match="absent from the canonical manifest"):
        postgres_migrations.apply_postgres_migrations(
            "postgresql://fixture",
            MIGRATIONS_DIR,
            allowed_kinds={"expand"},
            defer_trailing_contracts=True,
        )

    migration_bodies = [
        path.read_text(encoding="utf-8")
        for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
    ]
    assert not any(body in query for body in migration_bodies for query in connection.queries)


def test_bridge_b_accepts_only_complete_checksum_exact_0013_0014_prefixes(
    monkeypatch,
) -> None:
    entries = validate_migration_manifest_entries(MIGRATIONS_DIR)
    local_canonical = {
        entry.version: str(entry.checksum)
        for entry in entries
    }
    canonical = {
        version: checksum
        for version, checksum in local_canonical.items()
        if version not in {
            record["version"] for record in FORWARD_COMPATIBLE_MIGRATIONS
        }
    }
    applied = {
        **canonical,
        FORWARD_COMPATIBLE_MIGRATION["version"]: (
            FORWARD_COMPATIBLE_MIGRATION["checksum"]
        ),
    }
    assert compatible_forward_versions(applied, canonical) == {
        FORWARD_COMPATIBLE_MIGRATION["version"]
    }
    assert require_known_or_exact_forward_ledger(applied, canonical) == {
        FORWARD_COMPATIBLE_MIGRATION["version"]
    }
    fully_applied = {
        **canonical,
        **{
            record["version"]: record["checksum"]
            for record in FORWARD_COMPATIBLE_MIGRATIONS
        },
    }
    assert compatible_forward_versions(fully_applied, canonical) == {
        record["version"] for record in FORWARD_COMPATIBLE_MIGRATIONS
    }
    assert require_known_or_exact_forward_ledger(
        fully_applied,
        canonical,
    ) == {record["version"] for record in FORWARD_COMPATIBLE_MIGRATIONS}

    connection = _LedgerConnection(
        [
            {"version": version, "checksum": checksum}
            for version, checksum in sorted(fully_applied.items())
        ]
    )

    @contextmanager
    def fake_connection(_dsn):
        yield connection

    monkeypatch.setattr(postgres_migrations, "postgres_connection", fake_connection)
    results = postgres_migrations.apply_postgres_migrations(
        "postgresql://fixture",
        MIGRATIONS_DIR,
        allowed_kinds={"expand"},
        defer_trailing_contracts=True,
    )
    assert len(results) == len(entries)
    assert all(result.applied is False for result in results)

    for changed in (
        {
            **applied,
            FORWARD_COMPATIBLE_MIGRATION["version"]: "f" * 64,
        },
        {
            key: value
            for key, value in applied.items()
            if key != "0012_drop_polytao_jobs"
        },
        {
            **fully_applied,
            FORWARD_COMPATIBLE_MIGRATIONS[-1]["version"]: "f" * 64,
        },
        {**fully_applied, "0015_future": "e" * 64},
    ):
        assert compatible_forward_versions(changed, canonical) == set()
        with pytest.raises(RuntimeError, match="absent from the canonical manifest"):
            require_known_or_exact_forward_ledger(changed, canonical)


@pytest.mark.parametrize(
    "duplicate_checksum",
    ["same", "conflicting"],
)
def test_duplicate_ledger_version_is_rejected_before_any_migration_sql(
    monkeypatch,
    duplicate_checksum: str,
) -> None:
    entries = validate_migration_manifest_entries(MIGRATIONS_DIR)
    first = entries[0]
    conflicting = "0" * 64
    connection = _LedgerConnection(
        [
            {"version": first.version, "checksum": first.checksum},
            {
                "version": first.version,
                "checksum": (
                    first.checksum
                    if duplicate_checksum == "same"
                    else conflicting
                ),
            },
        ]
    )

    @contextmanager
    def fake_connection(_dsn):
        yield connection

    monkeypatch.setattr(postgres_migrations, "postgres_connection", fake_connection)

    with pytest.raises(RuntimeError, match="duplicate versions"):
        postgres_migrations.apply_postgres_migrations(
            "postgresql://fixture",
            MIGRATIONS_DIR,
            allowed_kinds={"expand"},
            defer_trailing_contracts=True,
        )

    migration_bodies = [
        path.read_text(encoding="utf-8")
        for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
    ]
    assert not any(body in query for body in migration_bodies for query in connection.queries)


def test_restricted_contract_requires_exact_canonical_ledger_prefix(
    monkeypatch,
) -> None:
    entries = validate_migration_manifest_entries(MIGRATIONS_DIR)
    target_index = next(
        index
        for index, entry in enumerate(entries)
        if entry.version == postgres_migrations.POLYTAO_CONTRACT_VERSION
    )
    rows = [
        {"version": entry.version, "checksum": entry.checksum}
        for entry in entries[:target_index]
    ]
    rows.pop(-1)
    rows.append(
        {
            "version": entries[target_index].version,
            "checksum": entries[target_index].checksum,
        }
    )
    connection = _LedgerConnection(rows)

    @contextmanager
    def fake_connection(_dsn):
        yield connection

    monkeypatch.setattr(postgres_migrations, "postgres_connection", fake_connection)
    guard_json, guard_sha256 = _contract_guard()

    with pytest.raises(RuntimeError, match="exact canonical ledger prefix"):
        postgres_migrations.apply_polytao_contract_migration(
            "postgresql://fixture",
            MIGRATIONS_DIR,
            guard_json=guard_json,
            guard_sha256=guard_sha256,
        )
    contract_sql = (
        MIGRATIONS_DIR
        / f"{postgres_migrations.POLYTAO_CONTRACT_VERSION}.sql"
    ).read_text(encoding="utf-8")
    assert contract_sql not in connection.queries


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


def test_migration_library_requires_an_explicit_execution_policy() -> None:
    with pytest.raises(ValueError, match="explicit policy"):
        postgres_migrations.apply_postgres_migrations("postgresql://unused")


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


def test_contract_0012_cli_rejects_missing_guard(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["postgres_migrations", "--mode", "contract-0012"],
    )

    with pytest.raises(SystemExit):
        postgres_migrations.main()


def test_only_checksum_pinned_0012_contract_is_exposed_by_cli(monkeypatch) -> None:
    captured: dict[str, object] = {}
    guard_json, guard_sha256 = _contract_guard()

    def fake_apply(dsn, migrations_dir, **kwargs):
        captured.update(
            {"dsn": dsn, "migrations_dir": migrations_dir, **kwargs}
        )
        return []

    monkeypatch.setattr(postgres_migrations, "apply_polytao_contract_migration", fake_apply)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "postgres_migrations",
            "--mode",
            "contract-0012",
            "--contract-guard-json",
            guard_json,
            "--contract-guard-sha256",
            guard_sha256,
        ],
    )

    postgres_migrations.main()

    assert captured["migrations_dir"] == postgres_migrations.MIGRATIONS_DIR
    assert captured["guard_json"] == guard_json
    assert captured["guard_sha256"] == guard_sha256
    with pytest.raises(SystemExit):
        monkeypatch.setattr(sys, "argv", ["postgres_migrations", "--mode", "contract"])
        postgres_migrations.main()

    with pytest.raises(ValueError, match="Only the checksum-pinned 0012"):
        postgres_migrations.apply_postgres_migrations(
            "postgresql://unused",
            MIGRATIONS_DIR,
            allowed_kinds={"contract"},
            restricted_contract=("0013_future_contract", "f" * 64),
        )


def test_contract_0012_library_rejects_missing_or_wrong_guard_before_connect(
    monkeypatch,
) -> None:
    connected = False

    @contextmanager
    def fake_connection(_dsn):
        nonlocal connected
        connected = True
        yield _LedgerConnection()

    monkeypatch.setattr(postgres_migrations, "postgres_connection", fake_connection)
    with pytest.raises(ValueError, match="requires a sealed transaction guard"):
        postgres_migrations.apply_polytao_contract_migration(
            "postgresql://fixture",
            MIGRATIONS_DIR,
        )

    guard_json, _ = _contract_guard()
    with pytest.raises(ValueError, match="detached digest does not match"):
        postgres_migrations.apply_polytao_contract_migration(
            "postgresql://fixture",
            MIGRATIONS_DIR,
            guard_json=guard_json,
            guard_sha256=f"sha256:{'f' * 64}",
        )
    assert connected is False


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda raw: json.dumps(
                {**json.loads(raw), "unknown": True},
                sort_keys=True,
                separators=(",", ":"),
            ),
            "invalid shape",
        ),
        (lambda raw: raw.replace(":", ": ", 1), "exact canonical encoding"),
        (
            lambda raw: raw[:-1] + ',"schema_version":1}',
            "duplicate key",
        ),
    ],
)
def test_contract_0012_guard_rejects_unknown_duplicate_or_noncanonical_json(
    monkeypatch,
    mutate,
    message: str,
) -> None:
    connected = False

    @contextmanager
    def fake_connection(_dsn):
        nonlocal connected
        connected = True
        yield _LedgerConnection()

    monkeypatch.setattr(postgres_migrations, "postgres_connection", fake_connection)
    guard_json, _ = _contract_guard()
    invalid_json = mutate(guard_json)

    with pytest.raises(ValueError, match=message):
        postgres_migrations.apply_polytao_contract_migration(
            "postgresql://fixture",
            MIGRATIONS_DIR,
            guard_json=invalid_json,
            guard_sha256=_guard_digest(invalid_json),
        )
    assert connected is False


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
