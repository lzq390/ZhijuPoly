from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings

REQUIRED_DFT_TABLES = ("dft_molecule_final", "dft_energy_trace")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_table_counts(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        counts: dict[str, int] = {}
        for table in REQUIRED_DFT_TABLES:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if exists is None:
                raise RuntimeError(f"Missing required DFT table: {table}")
            counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return counts
    finally:
        connection.close()


def append_manifest(manifest_path: Path, *, archive_path: Path, original_target: Path | None, byte_size: int, digest: str, counts: dict[str, int]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "",
        "## DFT SQLite Self-Contained Archive Repair",
        f"- repaired_at_utc: {datetime.now(timezone.utc).isoformat()}",
        f"- archive_path: {archive_path}",
        f"- original_symlink_target: {original_target if original_target is not None else 'not-a-symlink'}",
        f"- byte_size: {byte_size}",
        f"- sha256: {digest}",
    ]
    for table, count in sorted(counts.items()):
        lines.append(f"- {table}: {count}")
    manifest_path.open("a", encoding="utf-8").write("\n".join(lines) + "\n")


def repair_legacy_dft_sqlite_archive(path: Path, manifest_path: Path | None = None) -> dict[str, object]:
    if not path.exists() and not path.is_symlink():
        raise FileNotFoundError(path)

    original_target: Path | None = None
    if path.is_symlink():
        original_target = path.resolve(strict=True)
        source_path = original_target
    else:
        source_path = path

    if not source_path.is_file():
        raise FileNotFoundError(source_path)

    if path.is_symlink():
        tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        if tmp_path.exists():
            tmp_path.unlink()
        shutil.copy2(source_path, tmp_path)
        counts = sqlite_table_counts(tmp_path)
        digest = sha256_file(tmp_path)
        byte_size = tmp_path.stat().st_size
        path.unlink()
        os.replace(tmp_path, path)
    else:
        counts = sqlite_table_counts(path)
        digest = sha256_file(path)
        byte_size = path.stat().st_size

    manifest = manifest_path or (path.parent / "MANIFEST.md")
    append_manifest(manifest, archive_path=path, original_target=original_target, byte_size=byte_size, digest=digest, counts=counts)
    return {
        "archive_path": str(path),
        "original_symlink_target": str(original_target) if original_target is not None else None,
        "byte_size": byte_size,
        "sha256": digest,
        "table_counts": counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Make archived legacy DFT fumol.db self-contained.")
    parser.add_argument("--path", type=Path, default=Settings().legacy_dft_sqlite_source_file)
    parser.add_argument("--manifest", type=Path, default=None)
    args = parser.parse_args()
    result = repair_legacy_dft_sqlite_archive(args.path, args.manifest)
    for key, value in result.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
