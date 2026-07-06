from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CsvBrowserRecord:
    source_file: str
    source_row_number: int
    data: dict[str, str]


def browse_csv_records(
    csv_path: str | Path,
    *,
    source_file: str,
    query: str,
    page: int,
    page_size: int,
    search_fields: tuple[str, ...] | None = None,
) -> tuple[int, int, list[CsvBrowserRecord]]:
    normalized_query = query.strip().casefold()
    offset = (page - 1) * page_size
    total_records = 0
    matched_records = 0
    results: list[CsvBrowserRecord] = []

    with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = tuple(reader.fieldnames or ())
        fields = search_fields or fieldnames

        for source_row_number, row in enumerate(reader, start=2):
            total_records += 1
            if normalized_query and not any(normalized_query in (row.get(field, "") or "").casefold() for field in fields):
                continue

            matched_records += 1
            if matched_records <= offset or len(results) >= page_size:
                continue

            results.append(
                CsvBrowserRecord(
                    source_file=source_file,
                    source_row_number=source_row_number,
                    data={field: row.get(field, "") or "" for field in fieldnames},
                )
            )

    return total_records, matched_records, results