from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.import_pi_candidates import import_pi_candidates_to_sqlite
from app.services.smiles_to_iupac import lookup_iupac_name


def write_pi_csv(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "id,mon1,mon2,polym,tg_celsius,dielectric_const_dc,created_at",
                "1,CCO,CCN,CCO,100,2.1,2026-05-11",
                "2,CCO,CCC,CCO,125,2.2,2026-05-11",
                "3,CCN,CCC,CCN,90,2.3,2026-05-11",
                "4,CCO,CCC,not-a-smiles,80,2.4,2026-05-11",
                "5,CCO,CCC,CCC,,2.5,2026-05-11",
            ]
        ),
        encoding="utf-8",
    )


def fetch_one(db_path: Path, query: str) -> sqlite3.Row:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(query).fetchone()
    finally:
        connection.close()


def test_import_pi_candidates_creates_schema_and_fingerprints(tmp_path: Path) -> None:
    csv_path = tmp_path / "pi.csv"
    db_path = tmp_path / "pi.db"
    write_pi_csv(csv_path)

    stats = import_pi_candidates_to_sqlite(csv_path=csv_path, db_path=db_path, progress_interval=0)

    assert stats.total_rows == 5
    assert stats.imported_count == 4
    assert stats.parse_ok_count == 3
    assert stats.parse_fail_count == 1
    assert stats.missing_tg_count == 1
    assert stats.skipped_required_count == 1

    counts = fetch_one(
        db_path,
        "SELECT COUNT(*) AS total, SUM(rdkit_parse_ok) AS parse_ok_total FROM pi_candidates",
    )
    assert counts["total"] == 4
    assert counts["parse_ok_total"] == 3

    parsed = fetch_one(
        db_path,
        "SELECT canonical_polym, length(morgan_fp) AS fp_len FROM pi_candidates WHERE pi_id = 1",
    )
    assert parsed["canonical_polym"] == "CCO"
    assert parsed["fp_len"] == 256

    invalid = fetch_one(
        db_path,
        "SELECT canonical_polym, morgan_fp, rdkit_parse_ok FROM pi_candidates WHERE pi_id = 4",
    )
    assert invalid["canonical_polym"] is None
    assert invalid["morgan_fp"] is None
    assert invalid["rdkit_parse_ok"] == 0


def test_import_pi_candidates_supports_limit(tmp_path: Path) -> None:
    csv_path = tmp_path / "pi.csv"
    db_path = tmp_path / "pi.db"
    write_pi_csv(csv_path)

    stats = import_pi_candidates_to_sqlite(
        csv_path=csv_path,
        db_path=db_path,
        limit=2,
        progress_interval=0,
    )

    assert stats.total_rows == 2
    assert stats.imported_count == 2

    counts = fetch_one(db_path, "SELECT COUNT(*) AS total FROM pi_candidates")
    assert counts["total"] == 2


def test_import_pi_candidates_requires_tg_column(tmp_path: Path) -> None:
    csv_path = tmp_path / "pi.csv"
    db_path = tmp_path / "pi.db"
    csv_path.write_text("id,mon1,mon2,polym\n1,CCO,CCN,CCO\n", encoding="utf-8")

    with pytest.raises(ValueError, match="tg_celsius"):
        import_pi_candidates_to_sqlite(csv_path=csv_path, db_path=db_path, progress_interval=0)


def test_import_pi_candidates_caches_optional_iupac_names(tmp_path: Path) -> None:
    csv_path = tmp_path / "pi.csv"
    db_path = tmp_path / "pi.db"
    csv_path.write_text(
        "\n".join(
            [
                "id,mon1,mon1_iupac,mon2,mon2_iupac,polym,tg_celsius",
                "1,CCO,ethanol,CCN,ethanamine,CCO,100",
                "2,CCO,ethanol,CCC,propane,CCC,110",
            ]
        ),
        encoding="utf-8",
    )

    stats = import_pi_candidates_to_sqlite(csv_path=csv_path, db_path=db_path, progress_interval=0)

    assert stats.iupac_cache_count == 3

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        assert lookup_iupac_name(connection, "CCO") == "ethanol"
        assert lookup_iupac_name(connection, "CCN") == "ethanamine"
        assert lookup_iupac_name(connection, "CCC") == "propane"
    finally:
        connection.close()
