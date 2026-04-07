from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE polymers (
  polymer_id INTEGER PRIMARY KEY AUTOINCREMENT,
  polymer_name TEXT NOT NULL,
  smiles TEXT NOT NULL,
  canonical_smiles TEXT,
  rdkit_parse_ok INTEGER NOT NULL DEFAULT 0,
  UNIQUE(polymer_name, smiles)
);

CREATE TABLE properties (
  property_id INTEGER PRIMARY KEY AUTOINCREMENT,
  polymer_id INTEGER NOT NULL,
  property_category TEXT NOT NULL,
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
CREATE INDEX idx_properties_category ON properties(property_category);
"""

DROP_SQL = """
DROP TABLE IF EXISTS properties;
DROP TABLE IF EXISTS polymers;
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
