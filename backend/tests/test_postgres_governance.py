from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from threading import Event
import time
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.errors import DependentObjectsStillExist
import pytest

from app import postgres_migrations, postgres_preflight
from app.config import Settings
from app.import_postgres import (
    BUSINESS_MUTABLE_IMPORT_DATASETS,
    BUSINESS_MUTABLE_TABLES,
    IMPORT_BATCH_SOURCE_LOGICAL_NAMES,
    STATIC_IMPORT_DATASETS,
    STATIC_REBUILD_TABLES_BY_DATASET,
    _start_batch,
    backfill_import_batch_sources,
    import_all_to_postgres,
    import_lab_from_legacy_schema,
    import_model_registry,
    import_online_knowledge_from_sqlite,
    resolve_requested_datasets,
    truncate_governed_tables,
    truncate_static_import_tables,
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
from app.services.postgres_database_browser import get_database_analytics_postgres


_CONTRACT_OPERATION_ID = "contract-0012-pg-guard"
_CONTRACT_RELEASE_SHA = "a" * 40
_DFT_MIGRATION_VERSION = "0013_monomer_dft_jobs"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _contract_guard_for_connection(connection) -> tuple[str, str]:
    relation = postgres_migrations._polytao_relation_identity(connection)
    archive_evidence = postgres_migrations._polytao_contract_archive_evidence(
        connection
    )
    database = connection.execute(
        """
        SELECT current_database() AS database_name,
               system_identifier::text AS system_identifier
        FROM pg_catalog.pg_control_system()
        """
    ).fetchone()
    ledger = [
        {"version": str(row["version"]), "checksum": str(row["checksum"])}
        for row in connection.execute(
            """
            SELECT version, checksum
            FROM governance.schema_migrations
            WHERE version < %s
            ORDER BY version, checksum
            """,
            (postgres_migrations.POLYTAO_CONTRACT_VERSION,),
        ).fetchall()
    ]
    document = {
        "schema_version": 1,
        "contract": {
            "version": postgres_migrations.POLYTAO_CONTRACT_VERSION,
            "checksum": postgres_migrations.POLYTAO_CONTRACT_CHECKSUM,
        },
        "maintenance": {
            "operation_id": _CONTRACT_OPERATION_ID,
            "marker_sha256": f"sha256:{'b' * 64}",
            "audit_manifest_sha256": f"sha256:{'c' * 64}",
        },
        "database": {
            "name": str(database["database_name"]),
            "system_identifier": str(database["system_identifier"]),
        },
        "release_sha": _CONTRACT_RELEASE_SHA,
        "ledger": ledger,
        "relation": {
            "qualified_name": postgres_migrations.POLYTAO_CONTRACT_RELATION,
            "namespace_oid": relation["namespace_oid"],
            "relation_oid": relation["relation_oid"],
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
            "reason": f"0012 maintenance {_CONTRACT_OPERATION_ID}",
            "release_sha": _CONTRACT_RELEASE_SHA,
            "activated_by": postgres_migrations.POLYTAO_CONTRACT_GUARD_ACTOR,
        },
        "active_jobs": {
            "generation.polytao_jobs": 0,
            "md.monomer_md_jobs": 0,
            "online_knowledge.jobs": 0,
        },
    }
    guard_json = _canonical_json(document)
    guard_sha256 = (
        "sha256:" + hashlib.sha256(guard_json.encode("utf-8")).hexdigest()
    )
    return guard_json, guard_sha256


def _prepare_polytao_contract_state(
    postgres_dsn: str,
    *,
    completed_job: bool = False,
    unrelated_generation_table: bool = False,
) -> tuple[str, str]:
    version = postgres_migrations.POLYTAO_CONTRACT_VERSION
    with postgres_connection(postgres_dsn) as connection:
        connection.execute("DROP SCHEMA IF EXISTS monomer_dft CASCADE")
        connection.execute(
            """
            DELETE FROM governance.schema_migrations
            WHERE version = ANY(%s)
            """,
            ([version, _DFT_MIGRATION_VERSION],),
        )
        connection.execute("DROP SCHEMA IF EXISTS generation CASCADE")
        connection.execute(
            (MIGRATIONS_DIR / "0007_polytao_jobs.sql").read_text(
                encoding="utf-8"
            )
        )
        if completed_job:
            connection.execute(
                """
                INSERT INTO generation.polytao_jobs (
                  job_id, status, descriptor_prompt, progress_message
                )
                VALUES ('guard-job', 'completed', 'sealed prompt', 'sealed content')
                """
            )
        if unrelated_generation_table:
            connection.execute(
                """
                CREATE TABLE generation.unrelated_runtime_data (
                  id integer PRIMARY KEY
                )
                """
            )
        connection.execute(
            """
            UPDATE governance.deployment_control
            SET drain_enabled = true,
                reason = %s,
                release_sha = %s,
                activated_at = now(),
                activated_by = %s,
                updated_at = now()
            WHERE control_key = 'production'
            """,
            (
                f"0012 maintenance {_CONTRACT_OPERATION_ID}",
                _CONTRACT_RELEASE_SHA,
                postgres_migrations.POLYTAO_CONTRACT_GUARD_ACTOR,
            ),
        )
        return _contract_guard_for_connection(connection)


def _restore_applied_polytao_contract_state(postgres_dsn: str) -> None:
    version = postgres_migrations.POLYTAO_CONTRACT_VERSION
    with postgres_connection(postgres_dsn) as connection:
        connection.execute(
            """
            DELETE FROM governance.schema_migrations
            WHERE version = ANY(%s)
            """,
            ([version, _DFT_MIGRATION_VERSION],),
        )
        connection.execute("DROP SCHEMA IF EXISTS monomer_dft CASCADE")
        connection.execute("DROP SCHEMA IF EXISTS generation CASCADE")
        connection.execute(
            (MIGRATIONS_DIR / "0007_polytao_jobs.sql").read_text(
                encoding="utf-8"
            )
        )
        connection.execute(
            """
            UPDATE governance.deployment_control
            SET drain_enabled = true,
                reason = %s,
                release_sha = %s,
                activated_at = now(),
                activated_by = %s,
                updated_at = now()
            WHERE control_key = 'production'
            """,
            (
                f"0012 maintenance {_CONTRACT_OPERATION_ID}",
                _CONTRACT_RELEASE_SHA,
                postgres_migrations.POLYTAO_CONTRACT_GUARD_ACTOR,
            ),
        )
        guard_json, guard_sha256 = _contract_guard_for_connection(connection)

    apply_polytao_contract_migration(
        postgres_dsn,
        guard_json=guard_json,
        guard_sha256=guard_sha256,
    )
    apply_postgres_migrations(
        postgres_dsn,
        allowed_kinds={"baseline", "expand"},
    )

    with postgres_connection(postgres_dsn) as connection:
        connection.execute(
            """
            UPDATE governance.deployment_control
            SET drain_enabled = false,
                reason = NULL,
                release_sha = NULL,
                activated_at = NULL,
                activated_by = NULL,
                updated_at = now()
            WHERE control_key = 'production'
            """
        )
        ledger = {
            str(row["version"]): str(row["checksum"])
            for row in connection.execute(
                """
                SELECT version, checksum
                FROM governance.schema_migrations
                WHERE version = ANY(%s)
                """,
                ([version, _DFT_MIGRATION_VERSION],),
            ).fetchall()
        }
        schema = connection.execute(
            """
            SELECT
              to_regnamespace('generation') AS generation,
              to_regnamespace('monomer_dft') AS monomer_dft,
              to_regclass('monomer_dft.jobs') AS jobs
            """
        ).fetchone()
        sequence = connection.execute(
            """
            SELECT last_value, is_called
            FROM monomer_dft.jobs_enqueue_sequence_seq
            """
        ).fetchone()
        if ledger != {
            version: postgres_migrations.POLYTAO_CONTRACT_CHECKSUM,
            _DFT_MIGRATION_VERSION: migration_checksum(
                MIGRATIONS_DIR / f"{_DFT_MIGRATION_VERSION}.sql"
            ),
        }:
            raise AssertionError(
                "test fixture did not restore exact 0012/0013 migration records"
            )
        if (
            schema is None
            or schema["generation"] is not None
            or schema["monomer_dft"] != "monomer_dft"
            or schema["jobs"] != "monomer_dft.jobs"
        ):
            raise AssertionError(
                "test fixture did not restore the exact post-0013 schema"
            )
        if (
            sequence is None
            or sequence["last_value"] != 1
            or sequence["is_called"] is not False
        ):
            raise AssertionError(
                "test fixture did not restore the pristine 0013 identity sequence"
            )


def _wait_for_application_lock(
    postgres_dsn: str,
    application_name: str,
    *,
    timeout: float = 5.0,
) -> None:
    deadline = time.monotonic() + timeout
    with psycopg.connect(postgres_dsn, autocommit=True) as observer:
        while time.monotonic() < deadline:
            row = observer.execute(
                """
                SELECT wait_event_type
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND application_name = %s
                """,
                (application_name,),
            ).fetchone()
            if row is not None and row[0] == "Lock":
                return
            time.sleep(0.02)
    pytest.fail(
        f"{application_name} did not reach a PostgreSQL lock wait",
        pytrace=False,
    )


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


_BUSINESS_MUTABLE_TABLE_KEYS = {
    ("online_knowledge", "history"): "history_id",
    ("online_knowledge", "jobs"): "job_id",
    ("lab", "test_projects"): "id",
    ("lab", "sample_measurements"): "id",
    ("md", "monomer_md_jobs"): "job_id",
    ("monomer_dft", "jobs"): "job_id",
    ("monomer_dft", "job_attempts"): "attempt_token",
    ("monomer_dft", "artifacts"): "artifact_id",
}


def _business_mutable_snapshot(connection) -> dict[str, dict[str, object]]:
    snapshot: dict[str, dict[str, object]] = {}
    for (schema, table), key in _BUSINESS_MUTABLE_TABLE_KEYS.items():
        rows = connection.execute(
            sql.SQL("SELECT to_jsonb(item) AS payload FROM {}.{} AS item ORDER BY {}").format(
                sql.Identifier(schema),
                sql.Identifier(table),
                sql.Identifier(key),
            )
        ).fetchall()
        material = json.dumps(
            [row["payload"] for row in rows],
            default=str,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        snapshot[f"{schema}.{table}"] = {
            "row_count": len(rows),
            "sha256": hashlib.sha256(material).hexdigest(),
        }
    return snapshot


def _seed_business_mutable_rows(connection) -> None:
    connection.execute(
        """
        INSERT INTO online_knowledge.history (
          history_id, material, mode, papers_found, reactions_extracted,
          max_papers, result_data
        ) VALUES (9001, 'mutable-polymer', 'synthesis', 7, 3, 10, '{"kept": true}')
        """
    )
    connection.execute(
        """
        INSERT INTO online_knowledge.jobs (
          job_id, status, material, mode, max_papers, progress_stage,
          progress_message, processed_papers, total_papers, result_data
        ) VALUES (
          'mutable-online-job', 'running', 'mutable-polymer', 'synthesis', 10,
          'searching', 'must survive', 4, 10, '{"kept": true}'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO lab.test_projects (id, project_name, result_unit)
        VALUES (9001, 'mutable-project', 'MPa')
        """
    )
    connection.execute(
        """
        INSERT INTO lab.sample_measurements (
          id, sample_id, experiment_project, instrument_id, "operator",
          collection_time, temperature, concentration, result_value,
          result_unit, remarks
        ) VALUES (
          9001, 'mutable-sample', 'mutable-project', 'instrument-1', 'operator-1',
          '2026-07-17 00:00:00', 23.50, 1.2500, 9.7500, 'MPa',
          'must survive'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO md.monomer_md_jobs (
          job_id, status, input_smiles, canonical_smiles, requested_steps,
          completed_steps, progress_percent, progress_stage, progress_message
        ) VALUES (
          'mutable-md-job', 'running', 'CC', 'CC', 100, 40, 40,
          'integrating', 'must survive'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO monomer_dft.jobs (
          job_id, idempotency_key, request_sha256, request_json,
          calculation_type, model_name, input_smiles, multiplicity,
          status, attempt_token
        ) VALUES (
          '00000000-0000-4000-8000-000000009001',
          'mutable-dft-job',
          %s,
          '{"smiles": "O"}'::jsonb,
          'single_point',
          'aimnet2',
          'O',
          1,
          'running',
          %s
        )
        """,
        ("9" * 64, "a" * 64),
    )
    connection.execute(
        """
        INSERT INTO monomer_dft.job_attempts (
          job_id, attempt, attempt_token, request_sha256, status
        ) VALUES (
          '00000000-0000-4000-8000-000000009001',
          1,
          %s,
          %s,
          'running'
        )
        """,
        ("a" * 64, "9" * 64),
    )
    connection.execute(
        """
        INSERT INTO monomer_dft.artifacts (
          job_id, artifact_id, name, relative_location,
          media_type, size_bytes, sha256, metadata
        ) VALUES (
          '00000000-0000-4000-8000-000000009001',
          'mutable-result',
          'result.json',
          'artifacts/result.json',
          'application/json',
          2,
          %s,
          '{"kept": true}'::jsonb
        )
        """,
        ("b" * 64,),
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


def test_exact_0013_is_reported_as_forward_compatible_for_bridge_b(
    tmp_path: Path,
    postgres_dsn: str,
) -> None:
    from app.migration_compatibility import FORWARD_COMPATIBLE_MIGRATION

    version = FORWARD_COMPATIBLE_MIGRATION["version"]
    checksum = FORWARD_COMPATIBLE_MIGRATION["checksum"]
    settings = _governance_settings(tmp_path, postgres_dsn)
    inserted = False
    with postgres_connection(postgres_dsn) as connection:
        existing = connection.execute(
            "SELECT checksum FROM governance.schema_migrations WHERE version = %s",
            (version,),
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO governance.schema_migrations (version, checksum) VALUES (%s, %s)",
                (version, checksum),
            )
            inserted = True
        else:
            assert existing["checksum"] == checksum

    try:
        report = postgres_preflight.run_preflight(
            settings,
            dsn=postgres_dsn,
            mode="schema",
            strict=True,
        )
        assert report["migrations"]["unknown_migrations"] == []
        assert report["migrations"]["forward_compatible_migrations"] == (
            [] if version in postgres_preflight._MIGRATION_CHECKSUMS else [version]
        )
        assert report["strict_ok"] is True

        results = apply_postgres_migrations(
            postgres_dsn,
            allowed_kinds={"baseline", "expand"},
            defer_trailing_contracts=True,
        )
        assert results
        assert all(result.applied is False for result in results)
    finally:
        if inserted:
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
    epoch_two_version = "9999_epoch_bridge_probe"
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


def test_retired_broad_rebuild_entrypoint_fails_closed() -> None:
    with pytest.raises(ValueError, match="broad governed-table rebuild is retired"):
        truncate_governed_tables(None)


def test_retired_mutable_import_functions_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="online_knowledge.jobs/history"):
        import_online_knowledge_from_sqlite(None, tmp_path / "legacy.db", 100)
    with pytest.raises(ValueError, match="lab.test_projects/sample_measurements"):
        import_lab_from_legacy_schema(None)


def test_all_import_selection_includes_property_filter() -> None:
    requested = resolve_requested_datasets({"all"})

    assert requested == set(STATIC_IMPORT_DATASETS)
    assert requested.isdisjoint(BUSINESS_MUTABLE_IMPORT_DATASETS)


def test_static_rebuild_contract_excludes_mutable_tables_and_cascade() -> None:
    rebuild_tables = {
        table
        for tables in STATIC_REBUILD_TABLES_BY_DATASET.values()
        for table in tables
    }

    class CaptureConnection:
        query = None

        def execute(self, query) -> None:
            self.query = query

    connection = CaptureConnection()
    truncate_static_import_tables(connection, set(STATIC_IMPORT_DATASETS))

    assert set(_BUSINESS_MUTABLE_TABLE_KEYS) == set(BUSINESS_MUTABLE_TABLES)
    assert rebuild_tables.isdisjoint(BUSINESS_MUTABLE_TABLES)
    assert connection.query is not None
    assert "CASCADE" not in repr(connection.query).upper()


def test_static_rebuild_runtime_guard_rejects_mutable_table_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        STATIC_REBUILD_TABLES_BY_DATASET,
        "unsafe-probe",
        (("online_knowledge", "history"),),
    )

    with pytest.raises(
        ValueError,
        match="crossed the business-mutable boundary: online_knowledge.history",
    ):
        truncate_static_import_tables(None, {"unsafe-probe"})


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


def test_retired_online_import_fails_closed_without_touching_runtime_rows(
    tmp_path: Path,
    postgres_dsn: str,
) -> None:
    legacy_db = _write_legacy_online_sqlite(tmp_path / "polyprop.db", history_id=500)
    settings = _governance_settings(tmp_path, postgres_dsn, legacy_main_sqlite_path=legacy_db)

    with postgres_connection(postgres_dsn) as connection:
        _seed_business_mutable_rows(connection)
        before = _business_mutable_snapshot(connection)

    with pytest.raises(
        ValueError,
        match="business-mutable datasets cannot be imported or rebuilt: online",
    ):
        import_all_to_postgres(
            settings,
            dsn=postgres_dsn,
            datasets={"online"},
            apply_migrations=False,
        )

    with postgres_connection(postgres_dsn) as connection:
        assert _business_mutable_snapshot(connection) == before


def test_static_rebuild_updates_static_rows_and_preserves_mutable_digests(
    tmp_path: Path,
    postgres_dsn: str,
) -> None:
    settings = _governance_settings(tmp_path, postgres_dsn)

    with postgres_connection(postgres_dsn) as connection:
        _seed_business_mutable_rows(connection)
        mutable_before = _business_mutable_snapshot(connection)
        static_before = connection.execute(
            """
            SELECT COUNT(*) AS row_count,
                   md5(string_agg(to_jsonb(item)::text, '' ORDER BY filter_record_id))
                     AS content_digest
            FROM core.polymer_property_filter_records AS item
            """,
        ).fetchone()

    stats = import_all_to_postgres(
        settings,
        dsn=postgres_dsn,
        datasets={"property_filter"},
        rebuild=True,
        apply_migrations=False,
    )

    with postgres_connection(postgres_dsn) as connection:
        mutable_after = _business_mutable_snapshot(connection)
        static_after = connection.execute(
            """
            SELECT COUNT(*) AS row_count,
                   md5(string_agg(to_jsonb(item)::text, '' ORDER BY filter_record_id))
                     AS content_digest
            FROM core.polymer_property_filter_records AS item
            """
        ).fetchone()

    dataset_stats = next(
        item for item in stats.datasets if item.dataset_key == "property_filter"
    )
    assert dataset_stats.row_count == 2
    assert static_before["row_count"] == 6
    assert static_after["row_count"] == 2
    assert static_after["content_digest"] != static_before["content_digest"]
    assert mutable_after == mutable_before


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
    assert report["schema_target"] == postgres_preflight.SCHEMA_TARGET_FINAL
    assert report["monomer_dft_schema"] == {
        "state": "ready",
        "reason": "exact_0013",
        "catalog_sha256": (
            "6dc2e6ca7e1bb052836afec2bbdd46c6aa0928e97efdbbc6669b9b220f9bf6f8"
        ),
    }


def test_runtime_preflight_profiles_accept_exact_0012_and_reject_partial_0013(
    tmp_path: Path,
    postgres_dsn: str,
    monkeypatch,
) -> None:
    settings = _governance_settings(tmp_path, postgres_dsn)
    statements: list[tuple[str, object]] = []
    original_connection_factory = postgres_preflight.postgres_connection

    class RecordingConnection:
        def __init__(self, connection) -> None:
            self.connection = connection

        def execute(self, query, parameters=None):
            statements.append((str(query), parameters))
            if parameters is None:
                return self.connection.execute(query)
            return self.connection.execute(query, parameters)

    @contextmanager
    def recording_connection_factory(dsn: str):
        with original_connection_factory(dsn) as connection:
            yield RecordingConnection(connection)

    monkeypatch.setattr(
        postgres_preflight,
        "postgres_connection",
        recording_connection_factory,
    )
    monkeypatch.setattr(
        postgres_preflight,
        "_analytics_snapshot_report",
        lambda connection: {
            "generated_at": "fixture",
            "source": "postgres",
            "comparisons": {},
            "warnings": [],
        },
    )

    with postgres_connection(postgres_dsn) as connection:
        connection.execute("DROP SCHEMA monomer_dft CASCADE")
        connection.execute(
            """
            DELETE FROM governance.schema_migrations
            WHERE version = '0013_monomer_dft_jobs'
            """
        )

    try:
        startup = postgres_preflight.run_preflight(
            settings,
            dsn=postgres_dsn,
            mode="runtime",
            strict=True,
            schema_target=postgres_preflight.SCHEMA_TARGET_STARTUP,
        )
        final = postgres_preflight.run_preflight(
            settings,
            dsn=postgres_dsn,
            mode="runtime",
            strict=True,
            schema_target=postgres_preflight.SCHEMA_TARGET_FINAL,
        )

        assert startup["status"] == "ok"
        assert startup["strict_ok"] is True
        assert startup["migrations"]["missing"] == []
        assert startup["migrations"]["required"][-1] == "0012_drop_polytao_jobs"
        assert startup["monomer_dft_schema"] == {
            "state": "absent",
            "reason": "migration_not_applied",
            "catalog_sha256": None,
        }
        assert startup["postgres"]["tables"]["monomer_dft.jobs"] is None
        assert startup["postgres"]["tables"]["monomer_dft.job_attempts"] is None
        assert startup["postgres"]["tables"]["monomer_dft.artifacts"] is None

        assert final["status"] == "failed"
        assert final["strict_ok"] is False
        assert final["migrations"]["missing"] == ["0013_monomer_dft_jobs"]
        assert any(
            "checksum-exact 0013" in error for error in final["strict_errors"]
        )

        with postgres_connection(postgres_dsn) as connection:
            connection.execute("CREATE SCHEMA monomer_dft")
        partial = postgres_preflight.run_preflight(
            settings,
            dsn=postgres_dsn,
            mode="runtime",
            strict=True,
            schema_target=postgres_preflight.SCHEMA_TARGET_STARTUP,
        )
        assert partial["status"] == "failed"
        assert partial["monomer_dft_schema"] == {
            "state": "invalid",
            "reason": "unmanaged_or_partial_schema",
            "catalog_sha256": None,
        }
        assert any(
            "partial or invalid" in error for error in partial["strict_errors"]
        )
    finally:
        with postgres_connection(postgres_dsn) as connection:
            connection.execute("DROP SCHEMA IF EXISTS monomer_dft CASCADE")
            connection.execute(
                """
                DELETE FROM governance.schema_migrations
                WHERE version = '0013_monomer_dft_jobs'
                """
            )
        apply_postgres_migrations(
            postgres_dsn,
            allowed_kinds={"baseline", "expand"},
            allow_contract_on_fresh_database=True,
        )

    assert not any(
        "from monomer_dft." in " ".join(query.lower().split())
        or 'from "monomer_dft".' in " ".join(query.lower().split())
        or (
            isinstance(parameters, (tuple, list))
            and bool(parameters)
            and parameters[0] == "monomer_dft"
        )
        for query, parameters in statements
    )


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


def test_historical_expand_defers_0012_but_f_startup_rejects_that_state(
    tmp_path: Path,
    postgres_dsn: str,
    monkeypatch,
) -> None:
    """The first controller cutover may deploy 0009-0011 before approving 0012."""

    settings = _governance_settings(tmp_path, postgres_dsn)
    version = "0012_drop_polytao_jobs"
    dft_version = _DFT_MIGRATION_VERSION
    monkeypatch.setattr(
        postgres_preflight,
        "_analytics_snapshot_report",
        lambda connection: {
            "generated_at": "fixture",
            "source": "postgres",
            "comparisons": {},
            "warnings": [],
        },
    )
    historical_dir = tmp_path / "migrations-through-0012"
    historical_dir.mkdir()
    for source in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if source.stem <= version:
            shutil.copy2(source, historical_dir / source.name)
    historical_manifest = json.loads(
        (MIGRATIONS_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    historical_manifest["migrations"] = [
        entry
        for entry in historical_manifest["migrations"]
        if entry["version"] <= version
    ]
    (historical_dir / "manifest.json").write_text(
        json.dumps(historical_manifest, indent=2) + "\n",
        encoding="utf-8",
    )

    with postgres_connection(postgres_dsn) as connection:
        connection.execute(
            """
            DELETE FROM governance.schema_migrations
            WHERE version = ANY(%s)
            """,
            ([
                "0009_monomer_md_job_leases",
                "0010_deployment_control",
                "0011_monomer_md_demo_steps",
                version,
                dft_version,
            ],),
        )
        connection.execute("DROP SCHEMA IF EXISTS generation CASCADE")
        connection.execute("DROP SCHEMA IF EXISTS monomer_dft CASCADE")
        connection.execute(
            (MIGRATIONS_DIR / "0007_polytao_jobs.sql").read_text(
                encoding="utf-8"
            )
        )

    try:
        results = apply_postgres_migrations(
            postgres_dsn,
            historical_dir,
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
            schema_target=postgres_preflight.SCHEMA_TARGET_STARTUP,
        )
        assert report["status"] == "failed"
        assert report["strict_ok"] is False
        assert report["migrations"]["missing"] == [version]
        assert report["migrations"]["pending_contracts"] == [version]
        assert any(
            version in error for error in report["strict_errors"]
        )
    finally:
        with postgres_connection(postgres_dsn) as connection:
            connection.execute(
                """
                UPDATE governance.deployment_control
                SET drain_enabled = true,
                    reason = %s,
                    release_sha = %s,
                    activated_at = now(),
                    activated_by = %s,
                    updated_at = now()
                WHERE control_key = 'production'
                """,
                (
                    f"0012 maintenance {_CONTRACT_OPERATION_ID}",
                    _CONTRACT_RELEASE_SHA,
                    postgres_migrations.POLYTAO_CONTRACT_GUARD_ACTOR,
                ),
            )
            guard_json, guard_sha256 = _contract_guard_for_connection(
                connection
            )
        try:
            apply_polytao_contract_migration(
                postgres_dsn,
                guard_json=guard_json,
                guard_sha256=guard_sha256,
            )
        finally:
            _restore_applied_polytao_contract_state(postgres_dsn)


def test_polytao_contract_rolls_back_when_generation_schema_is_not_empty(
    postgres_dsn: str,
) -> None:
    version = "0012_drop_polytao_jobs"
    guard_json, guard_sha256 = _prepare_polytao_contract_state(
        postgres_dsn,
        unrelated_generation_table=True,
    )

    try:
        with pytest.raises(DependentObjectsStillExist):
            apply_polytao_contract_migration(
                postgres_dsn,
                guard_json=guard_json,
                guard_sha256=guard_sha256,
            )

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
        _restore_applied_polytao_contract_state(postgres_dsn)


def test_polytao_contract_guard_applies_and_supports_exact_post_state_retry(
    postgres_dsn: str,
) -> None:
    guard_json, guard_sha256 = _prepare_polytao_contract_state(
        postgres_dsn,
        completed_job=True,
    )

    try:
        first = apply_polytao_contract_migration(
            postgres_dsn,
            guard_json=guard_json,
            guard_sha256=guard_sha256,
        )
        second = apply_polytao_contract_migration(
            postgres_dsn,
            guard_json=guard_json,
            guard_sha256=guard_sha256,
        )

        first_target = next(
            result
            for result in first
            if result.version == postgres_migrations.POLYTAO_CONTRACT_VERSION
        )
        second_target = next(
            result
            for result in second
            if result.version == postgres_migrations.POLYTAO_CONTRACT_VERSION
        )
        assert first_target.applied is True
        assert second_target.applied is False
        assert first_target.checksum == postgres_migrations.POLYTAO_CONTRACT_CHECKSUM
        assert second_target.checksum == postgres_migrations.POLYTAO_CONTRACT_CHECKSUM

        with postgres_connection(postgres_dsn) as connection:
            state = connection.execute(
                """
                SELECT to_regclass('generation.polytao_jobs') AS relation,
                       to_regnamespace('generation') AS namespace,
                       checksum
                FROM governance.schema_migrations
                WHERE version = %s
                """,
                (postgres_migrations.POLYTAO_CONTRACT_VERSION,),
            ).fetchone()
        assert state["relation"] is None
        assert state["namespace"] is None
        assert state["checksum"] == postgres_migrations.POLYTAO_CONTRACT_CHECKSUM
    finally:
        _restore_applied_polytao_contract_state(postgres_dsn)


def test_polytao_contract_guard_rejects_event_trigger_side_effects(
    postgres_dsn: str,
) -> None:
    guard_json, guard_sha256 = _prepare_polytao_contract_state(
        postgres_dsn,
        completed_job=True,
    )
    event_trigger = "nexpoly_test_recreate_generation"
    trigger_function = "public.nexpoly_test_recreate_generation"
    with postgres_connection(postgres_dsn) as connection:
        connection.execute(
            f"""
            CREATE OR REPLACE FUNCTION {trigger_function}()
            RETURNS event_trigger
            LANGUAGE plpgsql
            AS $function$
            BEGIN
              IF TG_TAG = 'DROP SCHEMA' THEN
                EXECUTE 'CREATE SCHEMA generation';
              END IF;
            END;
            $function$
            """
        )
        connection.execute(
            f"""
            CREATE EVENT TRIGGER {event_trigger}
            ON ddl_command_end
            WHEN TAG IN ('DROP SCHEMA')
            EXECUTE FUNCTION {trigger_function}()
            """
        )

    try:
        with pytest.raises(
            RuntimeError,
            match="event-trigger inventory",
        ):
            apply_polytao_contract_migration(
                postgres_dsn,
                guard_json=guard_json,
                guard_sha256=guard_sha256,
            )

        with postgres_connection(postgres_dsn) as connection:
            state = connection.execute(
                """
                SELECT to_regclass('generation.polytao_jobs') AS relation,
                       EXISTS (
                         SELECT 1
                         FROM governance.schema_migrations
                         WHERE version = %s
                       ) AS migration_recorded
                """,
                (postgres_migrations.POLYTAO_CONTRACT_VERSION,),
            ).fetchone()
        assert state["relation"] == "generation.polytao_jobs"
        assert state["migration_recorded"] is False
    finally:
        with postgres_connection(postgres_dsn) as connection:
            connection.execute(
                f"DROP EVENT TRIGGER IF EXISTS {event_trigger}"
            )
            connection.execute(
                f"DROP FUNCTION IF EXISTS {trigger_function}()"
            )
        _restore_applied_polytao_contract_state(postgres_dsn)


def test_polytao_contract_post_verifier_failure_rolls_back_transaction(
    postgres_dsn: str,
    monkeypatch,
) -> None:
    guard_json, guard_sha256 = _prepare_polytao_contract_state(
        postgres_dsn,
        completed_job=True,
    )
    original_verify = (
        postgres_migrations._verify_applied_polytao_contract_guard
    )

    def reject_after_exact_post_state(
        connection,
        guard,
        *,
        expected_ledger,
    ) -> None:
        original_verify(
            connection,
            guard,
            expected_ledger=expected_ledger,
        )
        raise RuntimeError("forced post-state rejection")

    monkeypatch.setattr(
        postgres_migrations,
        "_verify_applied_polytao_contract_guard",
        reject_after_exact_post_state,
    )
    try:
        with pytest.raises(
            RuntimeError,
            match="forced post-state rejection",
        ):
            apply_polytao_contract_migration(
                postgres_dsn,
                guard_json=guard_json,
                guard_sha256=guard_sha256,
            )

        with postgres_connection(postgres_dsn) as connection:
            state = connection.execute(
                """
                SELECT to_regclass('generation.polytao_jobs') AS relation,
                       EXISTS (
                         SELECT 1
                         FROM governance.schema_migrations
                         WHERE version = %s
                       ) AS migration_recorded
                """,
                (postgres_migrations.POLYTAO_CONTRACT_VERSION,),
            ).fetchone()
        assert state["relation"] == "generation.polytao_jobs"
        assert state["migration_recorded"] is False
    finally:
        monkeypatch.undo()
        _restore_applied_polytao_contract_state(postgres_dsn)


def test_polytao_contract_guard_rejects_same_count_update_committed_while_waiting(
    postgres_dsn: str,
) -> None:
    guard_json, guard_sha256 = _prepare_polytao_contract_state(
        postgres_dsn,
        completed_job=True,
    )
    application_name = "contract-guard-content-race"
    migration_dsn = make_conninfo(
        postgres_dsn,
        application_name=application_name,
    )
    writer = psycopg.connect(postgres_dsn)

    try:
        writer.execute(
            """
            UPDATE generation.polytao_jobs
            SET progress_message = 'committed after archival'
            WHERE job_id = 'guard-job'
            """
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                apply_polytao_contract_migration,
                migration_dsn,
                guard_json=guard_json,
                guard_sha256=guard_sha256,
            )
            _wait_for_application_lock(postgres_dsn, application_name)
            writer.commit()
            with pytest.raises(RuntimeError, match="business-row content changed"):
                future.result(timeout=10)

        with postgres_connection(postgres_dsn) as connection:
            state = connection.execute(
                """
                SELECT progress_message,
                       EXISTS (
                         SELECT 1
                         FROM governance.schema_migrations
                         WHERE version = %s
                       ) AS migration_recorded
                FROM generation.polytao_jobs
                WHERE job_id = 'guard-job'
                """,
                (postgres_migrations.POLYTAO_CONTRACT_VERSION,),
            ).fetchone()
        assert state["progress_message"] == "committed after archival"
        assert state["migration_recorded"] is False
    finally:
        writer.rollback()
        writer.close()
        _restore_applied_polytao_contract_state(postgres_dsn)


def test_polytao_contract_guard_access_exclusive_blocks_late_writer(
    postgres_dsn: str,
    monkeypatch,
) -> None:
    guard_json, guard_sha256 = _prepare_polytao_contract_state(
        postgres_dsn,
        completed_job=True,
    )
    guard_verified = Event()
    allow_contract = Event()
    original_verify = postgres_migrations._verify_polytao_contract_guard

    def hold_after_verification(connection, guard) -> None:
        original_verify(connection, guard)
        guard_verified.set()
        if not allow_contract.wait(timeout=10):
            raise RuntimeError("test did not release the guarded transaction")

    monkeypatch.setattr(
        postgres_migrations,
        "_verify_polytao_contract_guard",
        hold_after_verification,
    )
    writer_name = "contract-guard-late-writer"
    writer_dsn = make_conninfo(postgres_dsn, application_name=writer_name)

    def late_writer() -> None:
        with psycopg.connect(writer_dsn) as connection:
            connection.execute(
                """
                UPDATE generation.polytao_jobs
                SET progress_message = 'must not commit'
                WHERE job_id = 'guard-job'
                """
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            migration = executor.submit(
                apply_polytao_contract_migration,
                postgres_dsn,
                guard_json=guard_json,
                guard_sha256=guard_sha256,
            )
            assert guard_verified.wait(timeout=5)
            writer = executor.submit(late_writer)
            _wait_for_application_lock(postgres_dsn, writer_name)
            allow_contract.set()
            results = migration.result(timeout=10)
            with pytest.raises(psycopg.Error):
                writer.result(timeout=10)

        target = next(
            result
            for result in results
            if result.version == postgres_migrations.POLYTAO_CONTRACT_VERSION
        )
        assert target.applied is True
        with postgres_connection(postgres_dsn) as connection:
            assert (
                connection.execute(
                    "SELECT to_regclass('generation.polytao_jobs') AS relation"
                ).fetchone()["relation"]
                is None
            )
    finally:
        allow_contract.set()
        monkeypatch.undo()
        _restore_applied_polytao_contract_state(postgres_dsn)


def test_polytao_contract_guard_rejects_active_persistent_job_before_sql(
    postgres_dsn: str,
) -> None:
    guard_json, guard_sha256 = _prepare_polytao_contract_state(postgres_dsn)
    with postgres_connection(postgres_dsn) as connection:
        connection.execute(
            """
            INSERT INTO md.monomer_md_jobs (
              job_id, status, input_smiles, canonical_smiles
            )
            VALUES ('guard-active-md', 'running', 'CC', 'CC')
            """
        )

    try:
        with pytest.raises(RuntimeError, match="active database jobs"):
            apply_polytao_contract_migration(
                postgres_dsn,
                guard_json=guard_json,
                guard_sha256=guard_sha256,
            )
        with postgres_connection(postgres_dsn) as connection:
            state = connection.execute(
                """
                SELECT to_regclass('generation.polytao_jobs') AS relation,
                       EXISTS (
                         SELECT 1
                         FROM governance.schema_migrations
                         WHERE version = %s
                       ) AS migration_recorded
                """,
                (postgres_migrations.POLYTAO_CONTRACT_VERSION,),
            ).fetchone()
        assert state["relation"] == "generation.polytao_jobs"
        assert state["migration_recorded"] is False
    finally:
        with postgres_connection(postgres_dsn) as connection:
            connection.execute(
                "DELETE FROM md.monomer_md_jobs WHERE job_id = 'guard-active-md'"
            )
        _restore_applied_polytao_contract_state(postgres_dsn)


@pytest.mark.parametrize("replacement", ["oid", "schema"])
def test_polytao_contract_guard_rejects_relation_or_schema_replacement_before_sql(
    postgres_dsn: str,
    replacement: str,
) -> None:
    guard_json, guard_sha256 = _prepare_polytao_contract_state(
        postgres_dsn,
        completed_job=True,
    )
    with postgres_connection(postgres_dsn) as connection:
        if replacement == "oid":
            connection.execute("DROP SCHEMA generation CASCADE")
            connection.execute(
                (MIGRATIONS_DIR / "0007_polytao_jobs.sql").read_text(
                    encoding="utf-8"
                )
            )
            expected_message = "relation or namespace OID changed"
        else:
            connection.execute(
                "ALTER TABLE generation.polytao_jobs ADD COLUMN guard_drift text"
            )
            expected_message = "schema or business-row content changed"

    try:
        with pytest.raises(RuntimeError, match=expected_message):
            apply_polytao_contract_migration(
                postgres_dsn,
                guard_json=guard_json,
                guard_sha256=guard_sha256,
            )
        with postgres_connection(postgres_dsn) as connection:
            state = connection.execute(
                """
                SELECT to_regclass('generation.polytao_jobs') AS relation,
                       EXISTS (
                         SELECT 1
                         FROM governance.schema_migrations
                         WHERE version = %s
                       ) AS migration_recorded
                """,
                (postgres_migrations.POLYTAO_CONTRACT_VERSION,),
            ).fetchone()
        assert state["relation"] == "generation.polytao_jobs"
        assert state["migration_recorded"] is False
    finally:
        _restore_applied_polytao_contract_state(postgres_dsn)


def test_polytao_contract_guard_revalidates_ledger_after_waiting_for_writer(
    postgres_dsn: str,
) -> None:
    guard_json, guard_sha256 = _prepare_polytao_contract_state(postgres_dsn)
    application_name = "contract-guard-ledger-race"
    migration_dsn = make_conninfo(
        postgres_dsn,
        application_name=application_name,
    )
    predecessor = "0011_monomer_md_demo_steps"
    predecessor_checksum = migration_checksum(
        MIGRATIONS_DIR / f"{predecessor}.sql"
    )
    writer = psycopg.connect(postgres_dsn)

    try:
        writer.execute(
            """
            UPDATE governance.schema_migrations
            SET checksum = %s
            WHERE version = %s
            """,
            ("f" * 64, predecessor),
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                apply_polytao_contract_migration,
                migration_dsn,
                guard_json=guard_json,
                guard_sha256=guard_sha256,
            )
            _wait_for_application_lock(postgres_dsn, application_name)
            writer.commit()
            with pytest.raises(RuntimeError, match="ledger changed"):
                future.result(timeout=10)

        with postgres_connection(postgres_dsn) as connection:
            state = connection.execute(
                """
                SELECT to_regclass('generation.polytao_jobs') AS relation,
                       EXISTS (
                         SELECT 1
                         FROM governance.schema_migrations
                         WHERE version = %s
                       ) AS migration_recorded
                """,
                (postgres_migrations.POLYTAO_CONTRACT_VERSION,),
            ).fetchone()
        assert state["relation"] == "generation.polytao_jobs"
        assert state["migration_recorded"] is False
    finally:
        writer.rollback()
        writer.close()
        with postgres_connection(postgres_dsn) as connection:
            connection.execute(
                """
                UPDATE governance.schema_migrations
                SET checksum = %s
                WHERE version = %s
                """,
                (predecessor_checksum, predecessor),
            )
        _restore_applied_polytao_contract_state(postgres_dsn)


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
        lambda settings,
        dsn=None,
        mode="runtime",
        strict=False,
        expected_source_sha=None,
        schema_target=postgres_preflight.SCHEMA_TARGET_FINAL: {
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
        lambda settings,
        dsn=None,
        mode="runtime",
        strict=False,
        expected_source_sha=None,
        schema_target=postgres_preflight.SCHEMA_TARGET_FINAL: {
            "status": "ok",
            "blockers": [],
            "strict_ok": True,
            "strict_errors": [],
        },
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
