from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.errors import DependentObjectsStillExist
import pytest

from app import postgres_preflight
from app.config import Settings
from app.import_postgres import (
    IMPORT_BATCH_SOURCE_LOGICAL_NAMES,
    _start_batch,
    backfill_import_batch_sources,
    import_all_to_postgres,
    import_model_registry,
    resolve_requested_datasets,
    upsert_source_registry,
)
from app.model_asset_manifest import iter_model_asset_specs
from app.postgres_database import postgres_connection
from app.postgres_migrations import (
    MIGRATIONS_DIR,
    apply_polytao_contract_migration,
    apply_postgres_migrations,
    migration_checksum,
)
from app.services.online_knowledge.postgres_history_repository import save_online_history_postgres
from app.services.postgres_database_browser import get_database_analytics_postgres


def _write_file(path: Path, content: bytes = b"source") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _write_legacy_online_sqlite(
    path: Path,
    *,
    history_id: int = 500,
    material: str = "legacy-polymer",
    mode: str = "synthesis",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE online_knowledge_history (
              history_id INTEGER PRIMARY KEY,
              material TEXT NOT NULL,
              mode TEXT NOT NULL,
              created_at TEXT,
              papers_found INTEGER,
              reactions_extracted INTEGER,
              max_papers INTEGER,
              result_json TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE online_knowledge_jobs (
              job_id TEXT PRIMARY KEY,
              status TEXT,
              material TEXT,
              mode TEXT,
              max_papers INTEGER,
              progress_stage TEXT,
              progress_message TEXT,
              processed_papers INTEGER,
              total_papers INTEGER,
              created_at TEXT,
              updated_at TEXT,
              error_message TEXT,
              result_json TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO online_knowledge_history (
              history_id, material, mode, created_at, papers_found,
              reactions_extracted, max_papers, result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                history_id,
                material,
                mode,
                "2026-01-01T00:00:00Z",
                1,
                1,
                5,
                json.dumps({"totalPapers": 1, "syntheses": [{"title": "legacy"}]}),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return path


def _governance_settings(
    tmp_path: Path,
    postgres_dsn: str,
    *,
    legacy_main_sqlite_path: Path | None = None,
) -> Settings:
    property_csv = _write_file(tmp_path / "data1.csv", b"smiles,property_name\nCCO,Tg\n")
    property_filter_csv = _write_file(
        tmp_path / "property_filter.csv",
        (
            "polymer_name,smiles,property_category,property_name,property_value,property_unit,"
            "property_unit_raw,property_unit_clean,property_key,property_label,canonical_value,"
            "canonical_unit,unit_conversion_status,value_origin,label_source,reliable_score,"
            "soft_quality_flags,duplicate_flag\n"
            "polymer_a,CCO,Thermal,Tg,123.4,C,C,C,tg,Glass transition temperature,123.4,C,"
            "already_standard,observed,exp,0.99,,\n"
            "polymer_a,CCO,Thermal,Cv,0.28,cal/(g*C),cal/(g*C),cal/(g*C),,,,"
            "not_mapped,observed,exp,0.98,,\n"
        ).encode("utf-8"),
    )
    process_csv = _write_file(tmp_path / "process.csv", b"polymer_id,process_flow_original_text\nP1,mix\n")
    property_detail_csv = _write_file(tmp_path / "property_detail.csv", b"polymer_id,property_name_en,value\nP1,Tg,123\n")
    legacy_main = legacy_main_sqlite_path or _write_file(tmp_path / "polyprop.db")
    return Settings(
        csv_source_path=str(property_csv),
        property_filter_csv_path=str(property_filter_csv),
        experimental_process_csv_path=str(process_csv),
        experimental_property_csv_path=str(property_detail_csv),
        knowledge_zip_path=str(_write_file(tmp_path / "knowledge.zip")),
        legacy_main_sqlite_source_path=str(legacy_main),
        legacy_pi_sqlite_source_path=str(_write_file(tmp_path / "pi_reverse_design.db")),
        legacy_dft_sqlite_source_path=str(_write_file(tmp_path / "fumol.db")),
        app_postgres_dsn=postgres_dsn,
    )


def test_governance_source_registry_backfills_import_batches(tmp_path: Path, postgres_dsn: str) -> None:
    settings = _governance_settings(tmp_path, postgres_dsn)

    with postgres_connection(postgres_dsn) as connection:
        source_ids = upsert_source_registry(connection, settings, postgres_dsn)
        for dataset_key in IMPORT_BATCH_SOURCE_LOGICAL_NAMES:
            _start_batch(connection, dataset_key)

        updated = backfill_import_batch_sources(connection, source_ids)

        null_count = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM governance.import_batches
            WHERE dataset_key = ANY(%s) AND source_file_id IS NULL
            """,
            (list(IMPORT_BATCH_SOURCE_LOGICAL_NAMES),),
        ).fetchone()["count"]
        legacy_rows = connection.execute(
            """
            SELECT logical_name, storage_kind, status, sha256
            FROM governance.source_files
            WHERE logical_name IN ('main_sqlite', 'pi_sqlite', 'dft_sqlite', 'lab_legacy_demo_postgres')
            ORDER BY logical_name
            """
        ).fetchall()

    assert updated == len(IMPORT_BATCH_SOURCE_LOGICAL_NAMES)
    assert null_count == 0
    assert {row["logical_name"] for row in legacy_rows} == {
        "main_sqlite",
        "pi_sqlite",
        "dft_sqlite",
        "lab_legacy_demo_postgres",
    }
    assert all(row["status"] == "archived_legacy_runtime_source" for row in legacy_rows)
    assert next(row for row in legacy_rows if row["logical_name"] == "lab_legacy_demo_postgres")["storage_kind"] == "postgres-schema"
    assert next(row for row in legacy_rows if row["logical_name"] == "dft_sqlite")["sha256"] is not None


def test_model_registry_uses_shared_asset_manifest(postgres_dsn: str) -> None:
    with postgres_connection(postgres_dsn) as connection:
        stats = import_model_registry(connection, Settings(app_postgres_dsn=postgres_dsn))
        rows = connection.execute(
            "SELECT logical_name, status, asset_type FROM model_registry.assets"
        ).fetchall()

    expected_names = {spec.resolved_logical_name for spec in iter_model_asset_specs(include_directories=True)}
    actual_names = {row["logical_name"] for row in rows}

    assert stats.row_count == len(expected_names)
    assert actual_names == expected_names
    assert {row["status"] for row in rows} <= {"ready", "missing"}


def test_migration_checksum_is_stable_across_line_endings(tmp_path: Path) -> None:
    lf_path = tmp_path / "lf.sql"
    crlf_path = tmp_path / "crlf.sql"
    bare_cr_path = tmp_path / "bare_cr.sql"
    lf_path.write_bytes(b"CREATE TABLE demo(id int);\nCREATE INDEX demo_id ON demo(id);\n")
    crlf_path.write_bytes(b"CREATE TABLE demo(id int);\r\nCREATE INDEX demo_id ON demo(id);\r\n")
    bare_cr_path.write_bytes(b"CREATE TABLE demo(id int);\rCREATE INDEX demo_id ON demo(id);\r")

    assert migration_checksum(lf_path) == migration_checksum(crlf_path)
    assert migration_checksum(lf_path) == migration_checksum(bare_cr_path)


def test_strict_preflight_blocks_known_dirty_image_0009_checksum(
    tmp_path: Path,
    postgres_dsn: str,
) -> None:
    version = "0009_monomer_md_job_leases"
    dirty_image_checksum = (
        "79a6956fc934794d61bc003f02a6b5280e9e8bd77a217b61a28d3dbdb8b7be0b"
    )
    canonical_checksum = migration_checksum(MIGRATIONS_DIR / f"{version}.sql")
    assert canonical_checksum == (
        "ef1757a81976f351459e8257bd492aa6267cbf507c4ea85506fefa2d465d2db8"
    )
    settings = _governance_settings(tmp_path, postgres_dsn)

    with postgres_connection(postgres_dsn) as connection:
        connection.execute(
            "UPDATE governance.schema_migrations SET checksum = %s WHERE version = %s",
            (dirty_image_checksum, version),
        )

    try:
        report = postgres_preflight.run_preflight(
            settings,
            dsn=postgres_dsn,
            mode="schema",
            strict=True,
        )
        assert report["status"] == "failed"
        assert report["strict_ok"] is False
        assert report["migrations"]["checksum_mismatches"] == [
            {
                "version": version,
                "expected": canonical_checksum,
                "actual": dirty_image_checksum,
            }
        ]
        assert any(version in error for error in report["strict_errors"])
    finally:
        with postgres_connection(postgres_dsn) as connection:
            connection.execute(
                "UPDATE governance.schema_migrations SET checksum = %s WHERE version = %s",
                (canonical_checksum, version),
            )


def test_unknown_migration_ledger_entry_blocks_preflight_and_runner(
    tmp_path: Path,
    postgres_dsn: str,
) -> None:
    version = "9999_unreviewed"
    settings = _governance_settings(tmp_path, postgres_dsn)
    with postgres_connection(postgres_dsn) as connection:
        connection.execute(
            "INSERT INTO governance.schema_migrations (version, checksum) VALUES (%s, %s)",
            (version, "f" * 64),
        )

    try:
        report = postgres_preflight.run_preflight(
            settings,
            dsn=postgres_dsn,
            mode="schema",
            strict=True,
        )
        assert report["status"] == "failed"
        assert report["strict_ok"] is False
        assert report["migrations"]["unknown_migrations"] == [version]
        assert any("unknown version" in error for error in report["strict_errors"])

        with pytest.raises(RuntimeError, match="absent from the canonical manifest"):
            apply_postgres_migrations(
                postgres_dsn,
                allowed_kinds={"baseline", "expand"},
                defer_trailing_contracts=True,
            )
    finally:
        with postgres_connection(postgres_dsn) as connection:
            connection.execute(
                "DELETE FROM governance.schema_migrations WHERE version = %s",
                (version,),
            )


def test_duplicate_migration_ledger_entry_blocks_preflight_and_runner(
    tmp_path: Path,
    postgres_dsn: str,
) -> None:
    version = "0008_polytao_backend_runtime"
    settings = _governance_settings(tmp_path, postgres_dsn)
    with postgres_connection(postgres_dsn) as connection:
        canonical_checksum = connection.execute(
            "SELECT checksum FROM governance.schema_migrations WHERE version = %s",
            (version,),
        ).fetchone()["checksum"]
        connection.execute(
            "ALTER TABLE governance.schema_migrations "
            "DROP CONSTRAINT schema_migrations_pkey"
        )
        connection.execute(
            "INSERT INTO governance.schema_migrations (version, checksum) VALUES (%s, %s)",
            (version, canonical_checksum),
        )

    try:
        report = postgres_preflight.run_preflight(
            settings,
            dsn=postgres_dsn,
            mode="schema",
            strict=True,
        )
        assert report["status"] == "failed"
        assert report["strict_ok"] is False
        assert report["migrations"]["duplicate_migrations"] == [
            {
                "version": version,
                "checksums": [canonical_checksum, canonical_checksum],
            }
        ]
        assert any("duplicate version" in error for error in report["strict_errors"])

        with pytest.raises(RuntimeError, match="duplicate versions"):
            apply_postgres_migrations(
                postgres_dsn,
                allowed_kinds={"baseline", "expand"},
                defer_trailing_contracts=True,
            )
    finally:
        with postgres_connection(postgres_dsn) as connection:
            connection.execute(
                "DELETE FROM governance.schema_migrations WHERE version = %s",
                (version,),
            )
            connection.execute(
                "INSERT INTO governance.schema_migrations (version, checksum) VALUES (%s, %s)",
                (version, canonical_checksum),
            )
            connection.execute(
                "ALTER TABLE governance.schema_migrations "
                "ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (version)"
            )


def test_epoch_two_migration_requires_exact_prior_contract_before_any_ddl(
    tmp_path: Path,
    postgres_dsn: str,
) -> None:
    migrations_dir = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, migrations_dir)
    epoch_two_version = "0013_epoch_bridge_probe"
    epoch_two_path = migrations_dir / f"{epoch_two_version}.sql"
    epoch_two_path.write_text(
        "CREATE TABLE governance.epoch_bridge_probe (id integer PRIMARY KEY);\n",
        encoding="utf-8",
    )
    contract_version = "0012_drop_polytao_jobs"
    contract_checksum = migration_checksum(migrations_dir / f"{contract_version}.sql")
    epoch_two_checksum = migration_checksum(epoch_two_path)
    manifest_path = migrations_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["migrations"].append(
        {
            "version": epoch_two_version,
            "kind": "expand",
            "epoch": 2,
            "checksum": epoch_two_checksum,
            "requires_contracts": [
                {"version": contract_version, "checksum": contract_checksum}
            ],
        }
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    fresh_database = f"epoch_bridge_bootstrap_{uuid.uuid4().hex[:16]}"
    fresh_dsn = make_conninfo(postgres_dsn, dbname=fresh_database)
    with psycopg.connect(postgres_dsn, autocommit=True) as admin_connection:
        admin_connection.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(fresh_database)
            )
        )
    try:
        bootstrap_results = apply_postgres_migrations(
            fresh_dsn,
            migrations_dir,
            allowed_kinds={"baseline", "expand"},
            allow_contract_on_fresh_database=True,
        )
        assert [result.version for result in bootstrap_results if result.applied] == [
            path.stem for path in sorted(migrations_dir.glob("*.sql"))
        ]
        repeated_bootstrap = apply_postgres_migrations(
            fresh_dsn,
            migrations_dir,
            allowed_kinds={"baseline", "expand"},
            allow_contract_on_fresh_database=True,
        )
        assert not any(result.applied for result in repeated_bootstrap)
        with postgres_connection(fresh_dsn) as connection:
            assert connection.execute(
                "SELECT to_regclass('governance.epoch_bridge_probe') AS probe"
            ).fetchone()["probe"] == "governance.epoch_bridge_probe"
    finally:
        with psycopg.connect(postgres_dsn, autocommit=True) as admin_connection:
            admin_connection.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (fresh_database,),
            )
            admin_connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(fresh_database)
                )
            )

    with postgres_connection(postgres_dsn) as connection:
        connection.execute(
            "DELETE FROM governance.schema_migrations WHERE version = %s",
            (contract_version,),
        )
        connection.execute("DROP TABLE IF EXISTS governance.epoch_bridge_probe")

    try:
        with pytest.raises(RuntimeError, match="requires approved contract"):
            apply_postgres_migrations(
                postgres_dsn,
                migrations_dir,
                allowed_kinds={"expand"},
                defer_trailing_contracts=True,
            )

        with postgres_connection(postgres_dsn) as connection:
            blocked_state = connection.execute(
                """
                SELECT
                  to_regclass('governance.epoch_bridge_probe') AS probe,
                  EXISTS (
                    SELECT 1 FROM governance.schema_migrations WHERE version = %s
                  ) AS migration_recorded
                """,
                (epoch_two_version,),
            ).fetchone()
            assert blocked_state["probe"] is None
            assert blocked_state["migration_recorded"] is False
            connection.execute(
                """
                INSERT INTO governance.schema_migrations (version, checksum)
                VALUES (%s, %s)
                """,
                (contract_version, contract_checksum),
            )

        first_results = apply_postgres_migrations(
            postgres_dsn,
            migrations_dir,
            allowed_kinds={"expand"},
            defer_trailing_contracts=True,
        )
        assert [result.version for result in first_results if result.applied] == [
            epoch_two_version
        ]

        repeated_results = apply_postgres_migrations(
            postgres_dsn,
            migrations_dir,
            allowed_kinds={"expand"},
            defer_trailing_contracts=True,
        )
        assert not any(result.applied for result in repeated_results)

        epoch_two_path.write_text(
            "CREATE TABLE governance.epoch_bridge_probe (id bigint PRIMARY KEY);\n",
            encoding="utf-8",
        )
        manifest["migrations"][-1]["checksum"] = migration_checksum(epoch_two_path)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="was already applied with checksum"):
            apply_postgres_migrations(
                postgres_dsn,
                migrations_dir,
                allowed_kinds={"expand"},
                defer_trailing_contracts=True,
            )
    finally:
        with postgres_connection(postgres_dsn) as connection:
            connection.execute("DROP TABLE IF EXISTS governance.epoch_bridge_probe")
            connection.execute(
                "DELETE FROM governance.schema_migrations WHERE version = %s",
                (epoch_two_version,),
            )
            connection.execute(
                """
                INSERT INTO governance.schema_migrations (version, checksum)
                VALUES (%s, %s)
                ON CONFLICT (version) DO UPDATE SET checksum = excluded.checksum
                """,
                (contract_version, contract_checksum),
            )


def test_rebuild_is_rejected_for_governance_only_import(tmp_path: Path, postgres_dsn: str) -> None:
    settings = _governance_settings(tmp_path, postgres_dsn)

    with pytest.raises(ValueError, match="--rebuild is only supported with a full import"):
        import_all_to_postgres(
            settings,
            dsn=postgres_dsn,
            datasets={"governance"},
            rebuild=True,
            apply_migrations=False,
        )

    with postgres_connection(postgres_dsn) as connection:
        polymer_count = connection.execute("SELECT COUNT(*) AS count FROM core.polymers").fetchone()["count"]

    assert polymer_count == 3


def test_all_import_selection_includes_property_filter() -> None:
    assert "property_filter" in resolve_requested_datasets({"all"})


def test_property_filter_import_replaces_only_filter_table(tmp_path: Path, postgres_dsn: str) -> None:
    settings = _governance_settings(tmp_path, postgres_dsn)

    stats = import_all_to_postgres(
        settings,
        dsn=postgres_dsn,
        datasets={"property_filter"},
        apply_migrations=False,
    )

    dataset_stats = next(item for item in stats.datasets if item.dataset_key == "property_filter")
    assert dataset_stats.row_count == 2
    assert dataset_stats.details == {"mapped_records": 1, "raw_records": 1}

    with postgres_connection(postgres_dsn) as connection:
        property_filter_rows = connection.execute(
            """
            SELECT property_name, property_key, canonical_value, canonical_unit, property_value_num
            FROM core.polymer_property_filter_records
            ORDER BY filter_record_id
            """
        ).fetchall()
        core_property_count = connection.execute("SELECT COUNT(*) AS count FROM core.polymer_properties").fetchone()["count"]

    assert core_property_count == 6
    assert len(property_filter_rows) == 2
    assert property_filter_rows[0]["property_key"] == "tg"
    assert property_filter_rows[0]["canonical_value"] == 123.4
    assert property_filter_rows[0]["canonical_unit"] == "C"
    assert property_filter_rows[1]["property_name"] == "Cv"
    assert property_filter_rows[1]["property_key"] is None
    assert property_filter_rows[1]["property_value_num"] == 0.28


def test_property_filter_import_records_missing_source_without_truncating(tmp_path: Path, postgres_dsn: str) -> None:
    settings = _governance_settings(tmp_path, postgres_dsn)
    settings.property_filter_csv_path = str(tmp_path / "missing_property_filter.csv")

    stats = import_all_to_postgres(
        settings,
        dsn=postgres_dsn,
        datasets={"property_filter"},
        apply_migrations=False,
    )

    dataset_stats = next(item for item in stats.datasets if item.dataset_key == "property_filter")
    assert dataset_stats.row_count == 0
    assert dataset_stats.details == {"missing_source": 1}

    with postgres_connection(postgres_dsn) as connection:
        batch = connection.execute(
            """
            SELECT status, row_count, error_message
            FROM governance.import_batches
            WHERE dataset_key = 'property_filter'
            ORDER BY import_batch_id DESC
            LIMIT 1
            """
        ).fetchone()
        source = connection.execute(
            """
            SELECT status
            FROM governance.source_files
            WHERE logical_name = 'property_filter_csv'
            """
        ).fetchone()
        existing_rows = connection.execute("SELECT COUNT(*) AS count FROM core.polymer_property_filter_records").fetchone()["count"]

    assert batch["status"] == "missing"
    assert batch["row_count"] == 0
    assert "missing_property_filter.csv" in batch["error_message"]
    assert source["status"] == "missing"
    assert existing_rows == 6


def test_online_history_sequence_resets_after_legacy_import_and_keeps_runtime_rows(tmp_path: Path, postgres_dsn: str) -> None:
    legacy_db = _write_legacy_online_sqlite(tmp_path / "polyprop.db", history_id=500)
    settings = _governance_settings(tmp_path, postgres_dsn, legacy_main_sqlite_path=legacy_db)

    import_all_to_postgres(settings, dsn=postgres_dsn, datasets={"online"}, apply_migrations=False)

    with postgres_connection(postgres_dsn) as connection:
        save_online_history_postgres(
            connection,
            material="runtime-polymer",
            mode="synthesis",
            max_papers=1,
            result_data={"totalPapers": 1, "syntheses": [{"title": "runtime"}]},
        )
        runtime_id = connection.execute(
            "SELECT history_id FROM online_knowledge.history WHERE material = %s",
            ("runtime-polymer",),
        ).fetchone()["history_id"]

    assert runtime_id > 500

    import_all_to_postgres(settings, dsn=postgres_dsn, datasets={"online"}, apply_migrations=False)

    with postgres_connection(postgres_dsn) as connection:
        rows = connection.execute(
            """
            SELECT material, history_id
            FROM online_knowledge.history
            WHERE material IN (%s, %s)
            """,
            ("legacy-polymer", "runtime-polymer"),
        ).fetchall()

    history_by_material = {row["material"]: row["history_id"] for row in rows}
    assert history_by_material["legacy-polymer"] == 500
    assert history_by_material["runtime-polymer"] == runtime_id


def test_strict_runtime_preflight_passes_after_migrations(tmp_path: Path, postgres_dsn: str, monkeypatch) -> None:
    settings = _governance_settings(tmp_path, postgres_dsn)
    monkeypatch.setattr(
        postgres_preflight,
        "_analytics_snapshot_report",
        lambda connection: {"generated_at": "fixture", "source": "postgres", "comparisons": {}, "warnings": []},
    )

    report = postgres_preflight.run_preflight(settings, dsn=postgres_dsn, mode="runtime", strict=True)

    assert report["status"] == "ok"
    assert report["blockers"] == []
    assert report["strict_ok"] is True
    assert report["strict_errors"] == []
    assert report["postgres"]["reachable"] is True
    assert report["migrations"]["missing"] == []


def test_strict_runtime_preflight_requires_matching_snapshot_source_sha(
    tmp_path: Path,
    postgres_dsn: str,
    monkeypatch,
) -> None:
    settings = _governance_settings(tmp_path, postgres_dsn)
    monkeypatch.setattr(
        postgres_preflight,
        "_analytics_snapshot_report",
        lambda connection: {
            "generated_at": "fixture",
            "source_sha": "a" * 40,
            "source": "postgres",
            "comparisons": {},
            "warnings": [],
        },
    )

    report = postgres_preflight.run_preflight(
        settings,
        dsn=postgres_dsn,
        mode="runtime",
        strict=True,
        expected_source_sha="b" * 40,
    )

    assert report["strict_ok"] is False
    assert "Postgres analytics snapshot source SHA does not match the running release" in report["strict_errors"]


def test_strict_runtime_preflight_requires_postgres_analytics_snapshot(
    tmp_path: Path,
    postgres_dsn: str,
) -> None:
    settings = _governance_settings(tmp_path, postgres_dsn)

    report = postgres_preflight.run_preflight(
        settings,
        dsn=postgres_dsn,
        mode="runtime",
        strict=True,
    )

    assert report["analytics_snapshot"]["source"] == "postgres-missing"
    assert report["strict_ok"] is False
    assert "Required Postgres analytics snapshot is missing or invalid" in report["strict_errors"]


def test_polytao_database_contract_is_applied(postgres_dsn: str) -> None:
    with postgres_connection(postgres_dsn) as connection:
        table = connection.execute(
            "SELECT to_regclass('generation.polytao_jobs') AS relation"
        ).fetchone()["relation"]
        schema = connection.execute(
            "SELECT to_regnamespace('generation') AS namespace"
        ).fetchone()["namespace"]

    assert table is None
    assert schema is None


def test_bootstrap_expand_defers_trailing_contract_and_schema_preflight_passes(
    tmp_path: Path,
    postgres_dsn: str,
    monkeypatch,
) -> None:
    """The first controller cutover may deploy 0009-0011 before approving 0012."""

    settings = _governance_settings(tmp_path, postgres_dsn)
    version = "0012_drop_polytao_jobs"
    monkeypatch.setattr(
        postgres_preflight,
        "_analytics_snapshot_report",
        lambda connection: {"generated_at": "fixture", "source": "postgres", "comparisons": {}, "warnings": []},
    )

    with postgres_connection(postgres_dsn) as connection:
        connection.execute(
            "DELETE FROM governance.schema_migrations WHERE version >= %s",
            ("0009_monomer_md_job_leases",),
        )

    try:
        results = apply_postgres_migrations(
            postgres_dsn,
            allowed_kinds={"baseline", "expand"},
            defer_trailing_contracts=True,
        )
        newly_applied = {result.version for result in results if result.applied}
        assert newly_applied == {
            "0009_monomer_md_job_leases",
            "0010_deployment_control",
            "0011_monomer_md_demo_steps",
        }
        deferred = next(result for result in results if result.version == version)
        assert deferred.applied is False

        with postgres_connection(postgres_dsn) as connection:
            recorded = {
                str(row["version"])
                for row in connection.execute(
                    "SELECT version FROM governance.schema_migrations WHERE version >= %s",
                    ("0009_monomer_md_job_leases",),
                ).fetchall()
            }
        assert recorded == newly_applied

        report = postgres_preflight.run_preflight(
            settings,
            dsn=postgres_dsn,
            mode="schema",
            strict=True,
        )
        assert report["status"] == "ok"
        assert report["strict_ok"] is True
        assert report["strict_errors"] == []
        assert report["migrations"]["missing"] == []
        assert report["migrations"]["pending_contracts"] == [version]
    finally:
        apply_polytao_contract_migration(postgres_dsn)


def test_polytao_contract_rolls_back_when_generation_schema_is_not_empty(
    postgres_dsn: str,
) -> None:
    version = "0012_drop_polytao_jobs"
    checksum = migration_checksum(MIGRATIONS_DIR / f"{version}.sql")

    with postgres_connection(postgres_dsn) as connection:
        connection.execute(
            "DELETE FROM governance.schema_migrations WHERE version = %s",
            (version,),
        )
        connection.execute("CREATE SCHEMA generation")
        connection.execute("CREATE TABLE generation.polytao_jobs (job_id text PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE generation.unrelated_runtime_data (id integer PRIMARY KEY)"
        )

    try:
        with pytest.raises(DependentObjectsStillExist):
            apply_polytao_contract_migration(postgres_dsn)

        with postgres_connection(postgres_dsn) as connection:
            state = connection.execute(
                """
                SELECT
                  to_regclass('generation.polytao_jobs') AS jobs_table,
                  to_regclass('generation.unrelated_runtime_data') AS unrelated_table,
                  EXISTS (
                    SELECT 1
                    FROM governance.schema_migrations
                    WHERE version = %s
                  ) AS migration_recorded
                """,
                (version,),
            ).fetchone()

        assert state["jobs_table"] == "generation.polytao_jobs"
        assert state["unrelated_table"] == "generation.unrelated_runtime_data"
        assert state["migration_recorded"] is False
    finally:
        with postgres_connection(postgres_dsn) as connection:
            connection.execute("DROP SCHEMA IF EXISTS generation CASCADE")
            connection.execute(
                """
                INSERT INTO governance.schema_migrations (version, checksum)
                VALUES (%s, %s)
                ON CONFLICT (version) DO UPDATE SET checksum = excluded.checksum
                """,
                (version, checksum),
            )


def test_strict_runtime_preflight_reports_missing_required_migration(tmp_path: Path, postgres_dsn: str, monkeypatch) -> None:
    settings = _governance_settings(tmp_path, postgres_dsn)
    version = "0003_runtime_postgres_cutover"
    monkeypatch.setattr(
        postgres_preflight,
        "_analytics_snapshot_report",
        lambda connection: {"generated_at": "fixture", "source": "postgres", "comparisons": {}, "warnings": []},
    )

    with postgres_connection(postgres_dsn) as connection:
        migration_row = connection.execute(
            "SELECT checksum FROM governance.schema_migrations WHERE version = %s",
            (version,),
        ).fetchone()
        assert migration_row is not None
        checksum = migration_row["checksum"]
        connection.execute("DELETE FROM governance.schema_migrations WHERE version = %s", (version,))

    try:
        report = postgres_preflight.run_preflight(settings, dsn=postgres_dsn, mode="runtime", strict=True)
        assert report["status"] == "failed"
        assert any(version in blocker for blocker in report["blockers"])
        assert report["strict_ok"] is False
        assert any(version in error for error in report["strict_errors"])
    finally:
        with postgres_connection(postgres_dsn) as connection:
            connection.execute(
                """
                INSERT INTO governance.schema_migrations (version, checksum)
                VALUES (%s, %s)
                ON CONFLICT (version) DO UPDATE SET checksum = excluded.checksum
                """,
                (version, checksum),
            )


def test_analytics_snapshot_failure_isolated_without_static_fallback(monkeypatch) -> None:
    class FakeTransaction:
        def __init__(self, connection) -> None:
            self.connection = connection

        def __enter__(self):
            self.connection.transaction_entered = True
            return self

        def __exit__(self, exc_type, exc, traceback) -> bool:
            if exc_type is not None:
                self.connection.aborted = False
            return False

    class FakeConnection:
        aborted = False
        transaction_entered = False

        def transaction(self):
            return FakeTransaction(self)

    connection = FakeConnection()

    def fail_snapshot_load(_connection):
        connection.aborted = True
        raise RuntimeError("snapshot table is unavailable")

    monkeypatch.setattr(
        "app.services.analytics_snapshot_store.load_analytics_snapshot",
        fail_snapshot_load,
    )
    monkeypatch.setattr(postgres_preflight, "_postgres_count", lambda *_args: 0)

    report = postgres_preflight._analytics_snapshot_report(connection)

    assert connection.transaction_entered is True
    assert connection.aborted is False
    assert report["source"] == "postgres-error"
    assert report["comparisons"] == {}
    assert report["warnings"]


def test_runtime_preflight_cli_exits_nonzero_for_strict_errors(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        postgres_preflight,
        "run_preflight",
        lambda settings, dsn=None, mode="runtime", strict=False, expected_source_sha=None: {
            "status": "failed",
            "blockers": ["Required Postgres migration is missing: 0003_runtime_postgres_cutover"],
            "strict_ok": False,
            "strict_errors": ["Required Postgres migration is missing: 0003_runtime_postgres_cutover"],
        },
    )
    monkeypatch.setattr(sys, "argv", ["postgres_preflight", "--mode", "runtime", "--strict"])

    with pytest.raises(SystemExit) as exc_info:
        postgres_preflight.main()

    assert exc_info.value.code == 1
    assert '"strict_ok": false' in capsys.readouterr().out


def test_runtime_preflight_cli_returns_zero_for_ready_report(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        postgres_preflight,
        "run_preflight",
        lambda settings, dsn=None, mode="runtime", strict=False, expected_source_sha=None: {"status": "ok", "blockers": [], "strict_ok": True, "strict_errors": []},
    )
    monkeypatch.setattr(sys, "argv", ["postgres_preflight", "--mode", "runtime", "--strict"])

    postgres_preflight.main()

    assert '"strict_ok": true' in capsys.readouterr().out


def test_formulation_analytics_counts_single_percent_symbol(postgres_dsn: str) -> None:
    with postgres_connection(postgres_dsn) as connection:
        connection.execute(
            """
            INSERT INTO knowledge.documents (
              knowledge_id, source_file, source_row_number, abstract, polymer_iupac, formulation
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (9001, "fixture", 1, "fixture abstract", "polyurethane", "monomer A 20 wt%"),
        )
        connection.execute(
            """
            INSERT INTO knowledge.formulation_records (
              knowledge_id, source_file, source_row_number, polymer_iupac, formulation,
              catalyst, temperature, reaction_time, solvent
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (9001, "fixture", 1, "polyurethane", "monomer A 20 wt%", None, None, None, None),
        )

        analytics = get_database_analytics_postgres(connection)

    ratio_types = {item["label"]: item["value"] for item in analytics["formulation"]["ratioTypes"]}
    assert ratio_types["percent"] >= 1
