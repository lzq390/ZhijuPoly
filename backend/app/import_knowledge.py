from __future__ import annotations

import argparse
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.database import rebuild_knowledge_schema, sqlite_connection
from app.services.xlsx_reader import CellValue, iter_xlsx_rows_with_numbers


HEADER_ALIASES = {
    "source_sequence": ("序号",),
    "title_zh": ("标题（中文）",),
    "title_en": ("标题（英文）",),
    "abstract": ("摘要（英文）",),
    "claim": ("权利要求-原文",),
    "analysis": ("大模型分析过程",),
    "is_polymer_synthesis": ("是否聚合物合成",),
    "judgement_reason": ("判断理由",),
    "polymer_iupac": ("聚合物 (IUPAC)",),
    "formulation": ("配方及用量",),
    "catalyst": ("催化剂 (含用量)",),
    "temperature": ("温度",),
    "reaction_time": ("时间",),
    "solvent": ("溶剂",),
}


INSERT_SQL = """
INSERT INTO knowledge_documents (
    source_file,
    source_row_number,
    source_sequence,
    title_zh,
    title_en,
    abstract,
    claim,
    analysis,
    is_polymer_synthesis,
    judgement_reason,
    polymer_iupac,
    formulation,
    catalyst,
    temperature,
    reaction_time,
    solvent
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


@dataclass(slots=True)
class KnowledgeImportStats:
    file_count: int
    document_count: int
    skipped_row_count: int


def _clean_text(value: CellValue) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _header_index(header_row: list[CellValue]) -> dict[str, int]:
    return {
        _clean_text(value): index
        for index, value in enumerate(header_row)
        if _clean_text(value)
    }


def _get_field(row: list[CellValue], index: dict[str, int], field_name: str) -> str | None:
    for header_name in HEADER_ALIASES[field_name]:
        column_index = index.get(header_name)
        if column_index is None or column_index >= len(row):
            continue
        value = _clean_text(row[column_index])
        return value or None
    return None


def _iter_workbook_documents(source_file: str, workbook_bytes: bytes) -> tuple[list[tuple[str | int | None, ...]], int]:
    rows = iter_xlsx_rows_with_numbers(workbook_bytes)
    try:
        _, header_row = next(rows)
    except StopIteration:
        return [], 0

    index = _header_index(header_row)
    documents: list[tuple[str | int | None, ...]] = []
    skipped_count = 0

    for source_row_number, row in rows:
        abstract = _get_field(row, index, "abstract")
        if abstract is None:
            skipped_count += 1
            continue

        documents.append(
            (
                source_file,
                source_row_number,
                _get_field(row, index, "source_sequence"),
                _get_field(row, index, "title_zh"),
                _get_field(row, index, "title_en"),
                abstract,
                _get_field(row, index, "claim"),
                _get_field(row, index, "analysis"),
                _get_field(row, index, "is_polymer_synthesis"),
                _get_field(row, index, "judgement_reason"),
                _get_field(row, index, "polymer_iupac"),
                _get_field(row, index, "formulation"),
                _get_field(row, index, "catalyst"),
                _get_field(row, index, "temperature"),
                _get_field(row, index, "reaction_time"),
                _get_field(row, index, "solvent"),
            )
        )

    return documents, skipped_count


def import_knowledge_zip_to_sqlite(zip_path: str | Path, db_path: str | Path) -> KnowledgeImportStats:
    document_count = 0
    skipped_row_count = 0
    file_count = 0

    with sqlite_connection(db_path) as connection:
        rebuild_knowledge_schema(connection)

        with zipfile.ZipFile(zip_path) as archive:
            xlsx_names = sorted(
                name
                for name in archive.namelist()
                if name.lower().endswith(".xlsx") and not Path(name).name.startswith("~$")
            )
            for name in xlsx_names:
                file_count += 1
                documents, skipped_count = _iter_workbook_documents(name, archive.read(name))
                skipped_row_count += skipped_count
                if not documents:
                    continue
                connection.executemany(INSERT_SQL, documents)
                document_count += len(documents)

    return KnowledgeImportStats(
        file_count=file_count,
        document_count=document_count,
        skipped_row_count=skipped_row_count,
    )


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Import local knowledge-base Excel files into SQLite.")
    parser.add_argument("--zip-path", default=str(settings.knowledge_zip_file))
    parser.add_argument("--db-path", default=str(settings.sqlite_db_file))
    args = parser.parse_args()

    stats = import_knowledge_zip_to_sqlite(zip_path=args.zip_path, db_path=args.db_path)
    print(
        f"Imported knowledge files={stats.file_count} documents={stats.document_count}"
        f" skipped_rows={stats.skipped_row_count}"
    )


if __name__ == "__main__":
    main()
