from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterator
from xml.etree import ElementTree


CellValue = str | int | float | bool | None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _cell_ref_to_column_index(ref: str) -> int:
    match = re.match(r"([A-Z]+)", ref.upper())
    if match is None:
        return 0

    index = 0
    for char in match.group(1):
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _read_shared_strings(package: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in package.namelist():
        return []

    root = ElementTree.fromstring(package.read("xl/sharedStrings.xml"))
    shared_strings: list[str] = []
    for item in root:
        if _local_name(item.tag) != "si":
            continue
        parts = [node.text or "" for node in item.iter() if _local_name(node.tag) == "t"]
        shared_strings.append("".join(parts))
    return shared_strings


def _first_worksheet_path(package: zipfile.ZipFile) -> str:
    worksheet_paths = sorted(
        name
        for name in package.namelist()
        if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
    )
    if not worksheet_paths:
        raise ValueError("xlsx workbook does not contain worksheets")
    return worksheet_paths[0]


def _parse_number(value: str) -> int | float | str:
    try:
        if re.fullmatch(r"[+-]?\d+", value):
            return int(value)
        return float(value)
    except ValueError:
        return value


def _text_from_descendants(element: ElementTree.Element) -> str:
    return "".join(node.text or "" for node in element.iter() if _local_name(node.tag) == "t")


def _cell_value(cell: ElementTree.Element, shared_strings: list[str]) -> CellValue:
    cell_type = cell.attrib.get("t")

    if cell_type == "inlineStr":
        return _text_from_descendants(cell)

    value_text: str | None = None
    for child in cell:
        if _local_name(child.tag) == "v":
            value_text = child.text
            break

    if value_text is None:
        return None

    if cell_type == "s":
        try:
            return shared_strings[int(value_text)]
        except (IndexError, ValueError):
            return ""
    if cell_type == "b":
        return value_text == "1"
    if cell_type in {"str", "e"}:
        return value_text
    return _parse_number(value_text)


def iter_xlsx_rows_with_numbers(workbook_bytes: bytes) -> Iterator[tuple[int, list[CellValue]]]:
    with zipfile.ZipFile(io.BytesIO(workbook_bytes)) as package:
        shared_strings = _read_shared_strings(package)
        worksheet_path = _first_worksheet_path(package)

        with package.open(worksheet_path) as worksheet:
            fallback_row_number = 0
            for event, row in ElementTree.iterparse(worksheet, events=("end",)):
                if _local_name(row.tag) != "row":
                    continue

                fallback_row_number += 1
                row_number_text = row.attrib.get("r")
                try:
                    row_number = int(row_number_text) if row_number_text else fallback_row_number
                except ValueError:
                    row_number = fallback_row_number

                values: list[CellValue] = []
                next_column_index = 0
                for cell in row:
                    if _local_name(cell.tag) != "c":
                        continue
                    ref = cell.attrib.get("r")
                    column_index = _cell_ref_to_column_index(ref) if ref else next_column_index
                    while len(values) <= column_index:
                        values.append(None)
                    values[column_index] = _cell_value(cell, shared_strings)
                    next_column_index = column_index + 1

                yield row_number, values
                row.clear()


def iter_xlsx_rows(workbook_bytes: bytes) -> Iterator[list[CellValue]]:
    for _, values in iter_xlsx_rows_with_numbers(workbook_bytes):
        yield values
