from __future__ import annotations

import io
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from app.database import rebuild_knowledge_schema, sqlite_connection
from app.import_knowledge import import_knowledge_directory_to_sqlite, import_knowledge_zip_to_sqlite
from app.models import KnowledgeSearchRequest
from app.routers.knowledge import search_knowledge
from app.services.knowledge_search import search_knowledge_documents


def make_request(app: FastAPI) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "app": app,
        }
    )


def column_name(index: int) -> str:
    value = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        value = chr(ord("A") + remainder) + value
    return value


def make_xlsx(rows: list[list[str] | None]) -> bytes:
    shared_strings: list[str] = []
    shared_index: dict[str, int] = {}

    def shared_string_index(value: str) -> int:
        if value not in shared_index:
            shared_index[value] = len(shared_strings)
            shared_strings.append(value)
        return shared_index[value]

    sheet_rows: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        if row is None:
            continue

        cells = []
        for column_index, value in enumerate(row):
            ref = f"{column_name(column_index)}{row_index}"
            cells.append(f'<c r="{ref}" t="s"><v>{shared_string_index(value)}</v></c>')
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + "".join(f"<si><t>{escape(value)}</t></si>" for value in shared_strings)
        + "</sst>"
    )
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        + "".join(sheet_rows)
        + "</sheetData></worksheet>"
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as workbook:
        workbook.writestr("xl/sharedStrings.xml", shared_xml)
        workbook.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buffer.getvalue()


def write_knowledge_zip(path: Path) -> None:
    rows = [
        [
            "序号",
            "标题（中文）",
            "标题（英文）",
            "摘要（英文）",
            "权利要求-原文",
            "大模型分析过程",
            "判断理由",
            "聚合物 (IUPAC)",
            "配方及用量",
            "催化剂 (含用量)",
            "温度",
            "时间",
            "溶剂",
        ],
        [
            "1",
            "环氧涂层",
            "Epoxy coating",
            "A bisphenol epoxy resin coating is applied to a metal drum.",
            "claim",
            "analysis",
            "reason",
            "poly(bisphenol A epoxy resin)",
            "bisphenol A : epoxy resin",
            "",
            "235 C",
            "20 min",
            "",
        ],
        [
            "2",
            "聚酯薄膜",
            "Polyester film",
            "A polyester film is stretched and heat treated.",
            "claim",
            "analysis",
            "reason",
            "poly(ethylene terephthalate)",
            "ethylene glycol : terephthalic acid",
            "",
            "120 C",
            "5 min",
            "",
        ],
    ]

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("P1_提取结果.xlsx", make_xlsx(rows))


def test_import_knowledge_zip_to_sqlite_and_searches_abstract(tmp_path: Path) -> None:
    zip_path = tmp_path / "knowledge.zip"
    db_path = tmp_path / "knowledge.db"
    write_knowledge_zip(zip_path)

    stats = import_knowledge_zip_to_sqlite(zip_path=zip_path, db_path=db_path)

    assert stats.file_count == 1
    assert stats.document_count == 2
    assert stats.skipped_row_count == 0

    with sqlite_connection(db_path) as connection:
        total, rows = search_knowledge_documents(connection, "BISPHENOL", top_k=10)

    assert total == 1
    assert rows[0]["title_en"] == "Epoxy coating"
    assert "bisphenol epoxy resin" in rows[0]["abstract"]


def test_import_knowledge_uses_excel_row_numbers_with_blank_rows(tmp_path: Path) -> None:
    zip_path = tmp_path / "knowledge.zip"
    db_path = tmp_path / "knowledge.db"
    rows = [
        [
            "序号",
            "标题（中文）",
            "标题（英文）",
            "摘要（英文）",
        ],
        [
            "1",
            "环氧涂层",
            "Epoxy coating",
            "A bisphenol epoxy resin coating is applied to a metal drum.",
        ],
        None,
        [
            "2",
            "聚酯薄膜",
            "Polyester film",
            "A polyester film is stretched and heat treated.",
        ],
    ]
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("P1_提取结果.xlsx", make_xlsx(rows))

    import_knowledge_zip_to_sqlite(zip_path=zip_path, db_path=db_path)

    with sqlite_connection(db_path) as connection:
        source_rows = connection.execute(
            "SELECT source_row_number FROM knowledge_documents ORDER BY knowledge_id"
        ).fetchall()

    assert [row["source_row_number"] for row in source_rows] == [2, 4]


def test_import_knowledge_directory_incrementally_adds_new_workbooks(tmp_path: Path) -> None:
    zip_path = tmp_path / "knowledge.zip"
    db_path = tmp_path / "knowledge.db"
    directory_path = tmp_path / "xlsx"
    directory_path.mkdir()
    write_knowledge_zip(zip_path)
    import_knowledge_zip_to_sqlite(zip_path=zip_path, db_path=db_path)

    rows = [
        [
            "序号",
            "标题（中文）",
            "标题（英文）",
            "摘要（英文）",
        ],
        [
            "3",
            "聚酰亚胺薄膜",
            "Polyimide film",
            "A polyimide film is prepared from dianhydride and diamine monomers.",
        ],
    ]
    (directory_path / "P2_提取结果.xlsx").write_bytes(make_xlsx(rows))

    stats = import_knowledge_directory_to_sqlite(
        directory_path=directory_path,
        db_path=db_path,
        rebuild=False,
    )

    assert stats.file_count == 1
    assert stats.document_count == 1
    assert stats.skipped_row_count == 0

    with sqlite_connection(db_path) as connection:
        total, rows = search_knowledge_documents(connection, "polyimide", top_k=10)

    assert total == 1
    assert rows[0]["source_file"] == "P2_提取结果.xlsx"


@pytest.mark.asyncio
async def test_search_knowledge_api_returns_matches_from_abstract(test_app: FastAPI) -> None:
    db_path = Path(test_app.state.settings.sqlite_db_path)
    with sqlite_connection(db_path) as connection:
        rebuild_knowledge_schema(connection)
        connection.execute(
            """
            INSERT INTO knowledge_documents (
                source_file,
                source_row_number,
                title_en,
                abstract,
                polymer_iupac
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "P1_提取结果.xlsx",
                2,
                "Epoxy coating",
                "A bisphenol epoxy resin coating is applied to a metal drum.",
                "poly(bisphenol A epoxy resin)",
            ),
        )
        connection.execute(
            """
            INSERT INTO knowledge_documents (
                source_file,
                source_row_number,
                title_en,
                abstract,
                polymer_iupac
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "P1_提取结果.xlsx",
                3,
                "Polyester film",
                "A polyester film is stretched and heat treated.",
                "poly(ethylene terephthalate)",
            ),
        )

    response = await search_knowledge(
        KnowledgeSearchRequest(query="epoxy", top_k=5),
        make_request(test_app),
    )

    assert response.query == "epoxy"
    assert response.total == 1
    assert response.results[0].title_en == "Epoxy coating"
    assert "epoxy resin" in response.results[0].abstract_snippet.casefold()
    assert response.results[0].matched_terms == ["epoxy"]
    assert response.results[0].matched_fields == ["Polymer", "Title", "Abstract"]


@pytest.mark.asyncio
async def test_search_knowledge_api_matches_structured_terms_across_fields(test_app: FastAPI) -> None:
    db_path = Path(test_app.state.settings.sqlite_db_path)
    with sqlite_connection(db_path) as connection:
        rebuild_knowledge_schema(connection)
        connection.execute(
            """
            INSERT INTO knowledge_documents (
                source_file,
                source_row_number,
                title_en,
                abstract,
                formulation
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "P1_提取结果.xlsx",
                2,
                "Pair formulation",
                "This record describes a polymerization recipe.",
                "ethanol : propane",
            ),
        )
        connection.execute(
            """
            INSERT INTO knowledge_documents (
                source_file,
                source_row_number,
                title_en,
                abstract
            ) VALUES (?, ?, ?, ?)
            """,
            (
                "P1_提取结果.xlsx",
                3,
                "Single abstract hit",
                "Only ethanol is mentioned in this abstract.",
            ),
        )

    response = await search_knowledge(
        KnowledgeSearchRequest(query="monomer pair", terms=["ethanol", "propane"], top_k=5),
        make_request(test_app),
    )

    assert response.query == "monomer pair"
    assert response.terms == ["ethanol", "propane"]
    assert response.total == 2
    assert response.results[0].title_en == "Pair formulation"
    assert response.results[0].matched_terms == ["ethanol", "propane"]
    assert response.results[0].matched_fields == ["Formulation"]
    assert response.results[1].title_en == "Single abstract hit"
    assert response.results[1].matched_terms == ["ethanol"]
    assert response.results[1].matched_fields == ["Abstract"]

    fallback_response = await search_knowledge(
        KnowledgeSearchRequest(query="ethanol OR propane", top_k=5),
        make_request(test_app),
    )

    assert fallback_response.terms == ["ethanol", "propane"]
    assert fallback_response.total == 2
    assert fallback_response.results[0].title_en == "Pair formulation"


@pytest.mark.asyncio
async def test_search_knowledge_api_expands_iupac_terms_for_partial_group_matches(test_app: FastAPI) -> None:
    db_path = Path(test_app.state.settings.sqlite_db_path)
    with sqlite_connection(db_path) as connection:
        rebuild_knowledge_schema(connection)
        connection.execute(
            """
            INSERT INTO knowledge_documents (
                source_file,
                source_row_number,
                title_en,
                abstract,
                formulation
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "P1_提取结果.xlsx",
                2,
                "Aminophenyl paper",
                "The synthesis uses a 4-Aminophenyl-containing diamine.",
                "",
            ),
        )
        connection.execute(
            """
            INSERT INTO knowledge_documents (
                source_file,
                source_row_number,
                title_en,
                abstract,
                formulation
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "P1_提取结果.xlsx",
                3,
                "Aminobenzoate paper",
                "The formulation uses an aromatic ester.",
                "4-aminobenzoate derivative",
            ),
        )

    response = await search_knowledge(
        KnowledgeSearchRequest(query="monomer", terms=["4-Aminophenyl 4-aminobenzoate"], top_k=5),
        make_request(test_app),
    )

    assert response.terms == [
        "4-Aminophenyl 4-aminobenzoate",
        "4-Aminophenyl",
        "Aminophenyl",
        "4-aminobenzoate",
        "aminobenzoate",
    ]
    assert response.total == 2
    results_by_title = {result.title_en: result for result in response.results}
    assert set(results_by_title) == {"Aminophenyl paper", "Aminobenzoate paper"}
    assert results_by_title["Aminophenyl paper"].matched_terms == ["4-Aminophenyl", "Aminophenyl"]
    assert results_by_title["Aminobenzoate paper"].matched_terms == ["4-aminobenzoate", "aminobenzoate"]


@pytest.mark.asyncio
async def test_search_knowledge_api_ranks_more_matched_fragments_first(test_app: FastAPI) -> None:
    db_path = Path(test_app.state.settings.sqlite_db_path)
    with sqlite_connection(db_path) as connection:
        rebuild_knowledge_schema(connection)
        connection.execute(
            """
            INSERT INTO knowledge_documents (
                source_file,
                source_row_number,
                title_en,
                abstract,
                polymer_iupac
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "P1_提取结果.xlsx",
                2,
                "One fragment in polymer",
                "This record only has background text.",
                "poly(benzophenone imide)",
            ),
        )
        connection.execute(
            """
            INSERT INTO knowledge_documents (
                source_file,
                source_row_number,
                title_en,
                abstract,
                polymer_iupac
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "P1_提取结果.xlsx",
                3,
                "Two fragments in abstract",
                "This record mentions benzophenone and aminobenzoate fragments together.",
                "",
            ),
        )

    response = await search_knowledge(
        KnowledgeSearchRequest(query="monomer", terms=["benzophenone aminobenzoate"], top_k=5),
        make_request(test_app),
    )

    assert [result.title_en for result in response.results] == [
        "Two fragments in abstract",
        "One fragment in polymer",
    ]


@pytest.mark.asyncio
async def test_search_knowledge_api_returns_requested_result_page(test_app: FastAPI) -> None:
    db_path = Path(test_app.state.settings.sqlite_db_path)
    with sqlite_connection(db_path) as connection:
        rebuild_knowledge_schema(connection)
        for index in range(25):
            connection.execute(
                """
                INSERT INTO knowledge_documents (
                    source_file,
                    source_row_number,
                    title_en,
                    abstract
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    "P1_提取结果.xlsx",
                    index + 1,
                    f"Epoxy paper {index + 1}",
                    "This paper describes an epoxy polymer.",
                ),
            )

    response = await search_knowledge(
        KnowledgeSearchRequest(query="epoxy", top_k=100, page=2, page_size=20),
        make_request(test_app),
    )

    assert response.total == 25
    assert response.page == 2
    assert response.page_size == 20
    assert len(response.results) == 5
    assert response.results[0].title_en == "Epoxy paper 21"
