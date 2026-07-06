from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE polymers (
  polymer_id INTEGER PRIMARY KEY AUTOINCREMENT,
  smiles TEXT NOT NULL,
  canonical_smiles TEXT,
  rdkit_parse_ok INTEGER NOT NULL DEFAULT 0,
  UNIQUE(smiles)
);

CREATE TABLE properties (
  property_id INTEGER PRIMARY KEY AUTOINCREMENT,
  polymer_id INTEGER NOT NULL,
  property_category TEXT NOT NULL DEFAULT 'Others',
  property_name TEXT NOT NULL,
  property_value TEXT NOT NULL,
  property_value_num REAL,
  property_unit TEXT,
  label_source TEXT,
  FOREIGN KEY (polymer_id) REFERENCES polymers(polymer_id)
);

CREATE INDEX idx_polymers_smiles ON polymers(smiles);
CREATE INDEX idx_polymers_canonical_smiles ON polymers(canonical_smiles);
CREATE INDEX idx_polymers_parse_ok ON polymers(rdkit_parse_ok);
CREATE INDEX idx_properties_polymer_id ON properties(polymer_id);
"""

KNOWLEDGE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS knowledge_documents (
  knowledge_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_file TEXT NOT NULL,
  source_row_number INTEGER NOT NULL,
  source_sequence TEXT,
  title_zh TEXT,
  title_en TEXT,
  abstract TEXT NOT NULL,
  claim TEXT,
  analysis TEXT,
  is_polymer_synthesis TEXT,
  judgement_reason TEXT,
  polymer_iupac TEXT,
  formulation TEXT,
  catalyst TEXT,
  temperature TEXT,
  reaction_time TEXT,
  solvent TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(source_file, source_row_number)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_source_file ON knowledge_documents(source_file);
CREATE INDEX IF NOT EXISTS idx_knowledge_title_en ON knowledge_documents(title_en);
CREATE INDEX IF NOT EXISTS idx_knowledge_polymer_iupac ON knowledge_documents(polymer_iupac);
"""

DROP_SQL = """
DROP TABLE IF EXISTS properties;
DROP TABLE IF EXISTS polymers;
"""

DROP_KNOWLEDGE_SQL = """
DROP TABLE IF EXISTS knowledge_documents;
"""

FUMOL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dft_molecule_final (
  mol_id TEXT PRIMARY KEY,
  range_group TEXT NOT NULL,
  final_step INTEGER NOT NULL,
  n_atoms INTEGER NOT NULL,
  coordinates TEXT NOT NULL,
  scf_energy REAL,
  zero_point_energy REAL,
  thermal_enthalpy REAL,
  gibbs_free_energy REAL,
  lowest_freq REAL,
  dipole_moment REAL,
  homo_ev REAL,
  lumo_ev REAL,
  gap_ev REAL,
  is_converged TEXT,
  pca_x REAL NOT NULL,
  pca_y REAL NOT NULL,
  pca_z REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dft_energy_trace (
  mol_id TEXT NOT NULL,
  step INTEGER NOT NULL,
  scf_energy REAL,
  homo_ev REAL,
  lumo_ev REAL,
  gap_ev REAL,
  PRIMARY KEY (mol_id, step)
);

CREATE INDEX IF NOT EXISTS idx_dft_final_pca ON dft_molecule_final(pca_x, pca_y, pca_z);
CREATE INDEX IF NOT EXISTS idx_dft_trace_mol_step ON dft_energy_trace(mol_id, step);
"""

DROP_FUMOL_SQL = """
DROP TABLE IF EXISTS dft_energy_trace;
DROP TABLE IF EXISTS dft_molecule_final;
"""


def ensure_parent_dir(db_path: str | Path) -> Path:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def connect_sqlite(db_path: str | Path) -> sqlite3.Connection:
    path = ensure_parent_dir(db_path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection


@contextmanager
def sqlite_connection(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    connection = connect_sqlite(db_path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def rebuild_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(DROP_SQL)
    connection.executescript(SCHEMA_SQL)


def ensure_knowledge_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(KNOWLEDGE_SCHEMA_SQL)


def rebuild_knowledge_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(DROP_KNOWLEDGE_SQL)
    ensure_knowledge_schema(connection)


def ensure_fumol_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(FUMOL_SCHEMA_SQL)


def rebuild_fumol_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(DROP_FUMOL_SQL)
    ensure_fumol_schema(connection)
