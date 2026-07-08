from __future__ import annotations

import csv
import os
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.config import PROJECT_ROOT, Settings
from app.postgres_database import postgres_connection
from app.postgres_migrations import apply_postgres_migrations
from app.services.fingerprint import fingerprint_to_bytes, generate


def _safe_dsn_label(dsn: str) -> str:
    try:
        parsed = urlsplit(dsn)
        if not parsed.scheme:
            return "configured APP_POSTGRES_DSN"
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        if parsed.username:
            netloc = f"{parsed.username}:***@{host}{port}"
        else:
            netloc = f"{host}{port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except ValueError:
        return "configured APP_POSTGRES_DSN"


def _fail_postgres_test_dependency(action: str, dsn: str, exc: BaseException) -> None:
    pytest.fail(
        "Backend tests require a reachable Postgres database with CREATE DATABASE "
        "and DROP DATABASE privileges. "
        f"Action failed: {action}. "
        f"APP_POSTGRES_DSN={_safe_dsn_label(dsn)}. "
        "Use screen312 with: cd backend && "
        "/home/lzq390/miniconda3/envs/screen312/bin/python -m pytest. "
        f"Original error: {type(exc).__name__}: {exc}",
        pytrace=False,
    )


def _drop_postgres_test_database(base_dsn: str, db_name: str) -> None:
    with psycopg.connect(base_dsn, autocommit=True) as admin_connection:
        admin_connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (db_name,),
        )
        admin_connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db_name)))

def write_sample_csv(path: Path) -> None:
    rows = [
        {
            "polymer_name": "polymer_a",
            "smiles": "CCO",
            "property_category": "Thermal",
            "property_name": "Tg",
            "property_value": "123.4",
            "property_unit": "C",
            "label_source": "exp",
        },
        {
            "polymer_name": "polymer_a",
            "smiles": "CCO",
            "property_category": "Thermal",
            "property_name": "Glass transition temperature",
            "property_value": "210.0",
            "property_unit": "C",
            "label_source": "exp",
        },
        {
            "polymer_name": "polymer_b",
            "smiles": "CCN",
            "property_category": "Thermal",
            "property_name": "Glass transition temperature",
            "property_value": "320.0",
            "property_unit": "C",
            "label_source": "calc",
        },
        {
            "polymer_name": "polymer_a",
            "smiles": "CCO",
            "property_category": "Electrical",
            "property_name": "Conductivity",
            "property_value": "1.5",
            "property_unit": "S/cm",
            "label_source": "exp",
        },
        {
            "polymer_name": "polymer_b",
            "smiles": "CCN",
            "property_category": "Mechanical",
            "property_name": "Strength",
            "property_value": "10",
            "property_unit": "MPa",
            "label_source": "calc",
        },
        {
            "polymer_name": "polymer_bad",
            "smiles": "not-a-smiles",
            "property_category": "Other",
            "property_name": "Misc",
            "property_value": "bad",
            "property_unit": "",
            "label_source": "exp",
        },
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture(scope="session")
def postgres_test_dsn() -> str:
    base_dsn = Settings().app_postgres_dsn
    db_name = f"zhijupoly_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    test_dsn = make_conninfo(base_dsn, dbname=db_name)

    try:
        with psycopg.connect(base_dsn, autocommit=True) as admin_connection:
            admin_connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(db_name)))
    except Exception as exc:
        _fail_postgres_test_dependency("create temporary Postgres test database", base_dsn, exc)

    try:
        try:
            apply_postgres_migrations(test_dsn)
        except Exception as exc:
            _fail_postgres_test_dependency("apply Postgres migrations to temporary test database", test_dsn, exc)
        yield test_dsn
    finally:
        try:
            _drop_postgres_test_database(base_dsn, db_name)
        except Exception as exc:
            _fail_postgres_test_dependency("drop temporary Postgres test database", base_dsn, exc)


@pytest.fixture
def postgres_dsn(postgres_test_dsn: str) -> str:
    reset_postgres_fixture(postgres_test_dsn)
    return postgres_test_dsn


def _executemany(connection, query: str, rows) -> None:
    with connection.cursor() as cursor:
        cursor.executemany(query, rows)


def reset_postgres_fixture(dsn: str) -> None:
    with postgres_connection(dsn) as connection:
        connection.execute(
            """
            TRUNCATE
              governance.import_batches,
              governance.source_files,
              core.polymer_property_filter_records,
              core.polymer_properties,
              core.polymers,
              knowledge.formulation_records,
              knowledge.documents,
              online_knowledge.history,
              online_knowledge.jobs,
              pi.tg_predictions,
              pi.polymers,
              pi.monomer_iupac,
              dft.energy_trace,
              dft.molecule_final,
              experimental.process_records,
              experimental.property_records,
              md.monomer_md_jobs,
              model_registry.assets
            RESTART IDENTITY CASCADE
            """
        )
        _seed_governance(connection)
        _seed_core_polymers(connection)
        _seed_property_filter_records(connection)
        _seed_pi_candidates(connection)
        _seed_dft(connection)


def _seed_governance(connection) -> None:
    _executemany(connection,
        """
        INSERT INTO governance.source_files (logical_name, path, storage_kind, status, row_count, notes)
        VALUES (%s, %s, 'fixture', %s, %s, %s)
        """,
        [
            ("experimental_process_csv", "fixture/process.csv", "ready", 0, None),
            ("experimental_property_csv", "fixture/property.csv", "ready", 0, None),
            ("property_filter_csv", "fixture/property_filter.csv", "ready", 6, None),
        ],
    )


def _seed_core_polymers(connection) -> None:
    _executemany(connection,
        """
        INSERT INTO core.polymers (polymer_id, polymer_name, smiles, canonical_smiles, rdkit_parse_ok)
        VALUES (%s, %s, %s, %s, %s)
        """,
        [
            (1, "polymer_a", "CCO", "CCO", True),
            (2, "polymer_b", "CCN", "CCN", True),
            (3, "polymer_bad", "not-a-smiles", None, False),
        ],
    )
    _executemany(connection,
        """
        INSERT INTO core.polymer_properties (
          property_id, polymer_id, property_category, property_name, property_value,
          property_value_num, property_unit, label_source, source_row_number
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (1, 1, "Thermal", "Tg", "123.4", 123.4, "\u00b0C", "exp", 1),
            (2, 1, "Thermal", "Glass transition temperature", "210.0", 210.0, "\u00b0C", "exp", 2),
            (3, 2, "Thermal", "Glass transition temperature", "320.0", 320.0, "\u00b0C", "calc", 3),
            (4, 1, "Electrical", "Conductivity", "1.5", 1.5, "S/cm", "exp", 4),
            (5, 2, "Mechanical", "Strength", "10", 10.0, "MPa", "calc", 5),
            (6, 3, "Other", "Misc", "bad", None, "", "exp", 6),
        ],
    )


def _seed_property_filter_records(connection) -> None:
    _executemany(connection,
        """
        INSERT INTO core.polymer_property_filter_records (
          filter_record_id,
          source_file,
          source_row_number,
          polymer_name,
          smiles,
          canonical_smiles,
          rdkit_parse_ok,
          property_category,
          property_name,
          property_value,
          property_value_num,
          property_unit,
          property_unit_raw,
          property_unit_clean,
          property_key,
          property_label,
          canonical_value,
          canonical_unit,
          unit_conversion_status,
          value_origin,
          label_source,
          reliable_score,
          soft_quality_flags,
          duplicate_flag
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (
                1,
                "fixture/property_filter.csv",
                2,
                "polymer_a",
                "CCO",
                "CCO",
                True,
                "Thermal",
                "Tg",
                "123.4",
                123.4,
                "C",
                "C",
                "C",
                "tg",
                "Glass transition temperature",
                123.4,
                "C",
                "already_standard",
                "observed",
                "exp",
                0.99,
                "",
                "",
            ),
            (
                2,
                "fixture/property_filter.csv",
                3,
                "polymer_a",
                "CCO",
                "CCO",
                True,
                "Electronic",
                "bandgap",
                "3.2",
                3.2,
                "eV",
                "eV",
                "eV",
                "bandgap",
                "Bandgap",
                3.2,
                "eV",
                "already_standard",
                "median",
                "sim",
                0.93,
                "",
                "",
            ),
            (
                3,
                "fixture/property_filter.csv",
                4,
                "polymer_b",
                "CCN",
                "CCN",
                True,
                "Thermal",
                "Glass transition temperature",
                "320.0",
                320.0,
                "C",
                "C",
                "C",
                "tg",
                "Glass transition temperature",
                320.0,
                "C",
                "already_standard",
                "impute",
                "calc",
                0.91,
                "iqr_extreme",
                "",
            ),
            (
                4,
                "fixture/property_filter.csv",
                5,
                "polymer_b",
                "CCN",
                "CCN",
                True,
                "Electronic",
                "bandgap",
                "5.5",
                5.5,
                "eV",
                "eV",
                "eV",
                "bandgap",
                "Bandgap",
                5.5,
                "eV",
                "already_standard",
                "observed",
                "sim",
                0.95,
                "",
                "",
            ),
            (
                5,
                "fixture/property_filter.csv",
                6,
                "polymer_a",
                "CCO",
                "CCO",
                True,
                "Thermal",
                "Cv",
                "0.28",
                0.28,
                "cal/(g*C)",
                "cal/(g*C)",
                "cal/(g*C)",
                None,
                None,
                None,
                None,
                "not_mapped",
                "observed",
                "exp",
                0.98,
                "",
                "",
            ),
            (
                6,
                "fixture/property_filter.csv",
                7,
                "polymer_b",
                "CCN",
                "CCN",
                True,
                "Thermal",
                "Cv",
                "0.35",
                0.35,
                "cal/(g*C)",
                "cal/(g*C)",
                "cal/(g*C)",
                None,
                None,
                None,
                None,
                "not_mapped",
                "impute",
                "calc",
                0.89,
                "",
                "",
            ),
        ],
    )


def _seed_pi_candidates(connection) -> None:
    connection.execute(
        """
        INSERT INTO pi.polymers (id, mon1, mon2, polym, canonical_polym, smiles_valid, morgan_fp, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, now())
        """,
        (7, "NCCN", "O=C=O", "CCO", "CCO", True, fingerprint_to_bytes(generate("CCO"))),
    )
    connection.execute(
        """
        INSERT INTO pi.tg_predictions (
          id, tg_celsius, smiles_valid, dielectric_const_dc, static_dielectric_const,
          dipole_debye, electrophilicity_index, homo_lumo_gap_ev, hardness,
          mulliken_electronegativity, redox_window_v, linear_expansion, refractive_index, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        """,
        (7, 215.0, True, None, None, None, None, None, None, None, None, None, None),
    )
    _executemany(connection,
        """
        INSERT INTO pi.monomer_iupac (smiles, iupac_name)
        VALUES (%s, %s)
        """,
        [("NCCN", "ethane-1,2-diamine"), ("O=C=O", "carbon dioxide")],
    )


def _seed_dft(connection) -> None:
    _executemany(connection,
        """
        INSERT INTO dft.molecule_final (
          mol_id, range_group, final_step, n_atoms, coordinates, scf_energy,
          zero_point_energy, thermal_enthalpy, gibbs_free_energy, lowest_freq,
          dipole_moment, homo_ev, lumo_ev, gap_ev, is_converged, pca_x, pca_y, pca_z
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (
                "000001_Conf01",
                "small",
                2,
                12,
                "[[6, 0, 0, 0]]",
                -100.1,
                -99.0,
                -98.5,
                -98.2,
                12.5,
                1.2,
                -6.1,
                94.1,
                100.2,
                "44",
                0.1,
                0.2,
                0.3,
            ),
            (
                "000002_Conf02",
                "large",
                1,
                24,
                "[[8, 0, 0, 0]]",
                -200.2,
                -198.0,
                -197.5,
                -197.2,
                8.5,
                2.4,
                -7.2,
                95.3,
                102.5,
                "34",
                1.1,
                1.2,
                1.3,
            ),
        ],
    )
    _executemany(connection,
        """
        INSERT INTO dft.energy_trace (mol_id, step, scf_energy, homo_ev, lumo_ev, gap_ev)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        [
            ("000001_Conf01", 0, -99.5, -6.0, 93.8, 99.8),
            ("000001_Conf01", 1, -100.0, -6.05, 94.0, 100.05),
            ("000001_Conf01", 2, -100.1, -6.1, 94.1, 100.2),
            ("000002_Conf02", 0, -199.9, -7.0, 95.0, 102.0),
            ("000002_Conf02", 1, -200.2, -7.2, 95.3, 102.5),
        ],
    )


@pytest.fixture
def test_app(postgres_dsn: str):
    from app.main import create_app

    settings = Settings(
        app_postgres_dsn=postgres_dsn,
        pi_postgres_dsn=postgres_dsn,
        lab_data_postgres_dsn=postgres_dsn,
        csv_source_path="database/data1.csv",
        experimental_process_csv_path="database/missing_process.csv",
        experimental_property_csv_path="database/missing_property.csv",
        allowed_origins="http://localhost:5173",
        structured_data_backend="postgres",
        pi_reverse_backend="postgres",
        model_enabled=False,
    )
    return create_app(settings)


@pytest.fixture
def predict_enabled_app(postgres_dsn: str, tmp_path: Path):
    from app.main import create_app

    model_dir = tmp_path / "models"
    model_dir.mkdir()

    for model_name in [
        "rf_Glass transition temperature_exp.pkl",
        "rf_Tensile stress strength at break_exp.pkl",
    ]:
        shutil.copy2(PROJECT_ROOT / "model" / model_name, model_dir / model_name)

    settings = Settings(
        app_postgres_dsn=postgres_dsn,
        pi_postgres_dsn=postgres_dsn,
        lab_data_postgres_dsn=postgres_dsn,
        csv_source_path="database/data1.csv",
        experimental_process_csv_path="database/missing_process.csv",
        experimental_property_csv_path="database/missing_property.csv",
        allowed_origins="http://localhost:5173",
        structured_data_backend="postgres",
        pi_reverse_backend="postgres",
        model_enabled=True,
        model_dir=str(model_dir),
    )
    return create_app(settings)
