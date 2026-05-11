from __future__ import annotations

import sqlite3


PI_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pi_candidates (
  pi_id INTEGER PRIMARY KEY,
  mon1 TEXT NOT NULL,
  mon2 TEXT NOT NULL,
  polym TEXT NOT NULL,
  canonical_polym TEXT,
  rdkit_parse_ok INTEGER NOT NULL DEFAULT 0,
  tg_celsius REAL NOT NULL,
  dielectric_const_dc REAL,
  static_dielectric_const REAL,
  dipole_debye REAL,
  electrophilicity_index REAL,
  homo_lumo_gap_ev REAL,
  hardness REAL,
  mulliken_electronegativity REAL,
  redox_window_v REAL,
  linear_expansion REAL,
  refractive_index REAL,
  morgan_fp BLOB,
  created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_pi_candidates_tg ON pi_candidates(tg_celsius);
CREATE INDEX IF NOT EXISTS idx_pi_candidates_parse_ok ON pi_candidates(rdkit_parse_ok);

CREATE TABLE IF NOT EXISTS smiles_iupac_cache (
  smiles TEXT PRIMARY KEY,
  iupac_name TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

DROP_PI_SCHEMA_SQL = """
DROP TABLE IF EXISTS smiles_iupac_cache;
DROP TABLE IF EXISTS pi_candidates;
"""


def ensure_pi_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(PI_SCHEMA_SQL)


def rebuild_pi_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(DROP_PI_SCHEMA_SQL)
    ensure_pi_schema(connection)
