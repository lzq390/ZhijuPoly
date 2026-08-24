from __future__ import annotations

import io
import zipfile
from pathlib import Path
from threading import get_ident
from xml.sax.saxutils import escape

import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.database import sqlite_connection
from app.import_knowledge import import_knowledge_directory_to_sqlite, import_knowledge_zip_to_sqlite
from app.models import KnowledgeSearchGroup, KnowledgeSearchRequest
from app.postgres_database import postgres_connection
from app.routers import knowledge as knowledge_routes
from app.routers.knowledge import search_knowledge
from app.services.knowledge_search import KnowledgeSearchExpressionError, normalize_search_groups, parse_search_groups
from app.services.postgres_knowledge_search import _build_search_sql_parts


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


def test_postgres_knowledge_search_keeps_indexed_columns_bare() -> None:
    sql_parts = _build_search_sql_parts([["epoxy"]])
    generated_sql = " ".join(part for part in sql_parts if isinstance(part, str))

    assert "COALESCE" not in generated_sql
    assert "polymer_iupac ILIKE" in generated_sql
    assert "formulation ILIKE" in generated_sql
    assert "abstract ILIKE" in generated_sql


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("polyimide;NMP", [["polyimide"], ["NMP"]]),
        ("polyimide；NMP", [["polyimide"], ["NMP"]]),
        ("NMP|N-methyl-2-pyrrolidone；polyimide", [["NMP", "N-methyl-2-pyrrolidone"], ["polyimide"]]),
        (
            "NMP｜N-methyl-2-pyrrolidone AND thermal stability",
            [["NMP", "N-methyl-2-pyrrolidone"], ["thermal stability"]],
        ),
        ("epoxy or resin and coating", [["epoxy", "resin"], ["coating"]]),
        ("thermal stability", [["thermal stability"]]),
        ("Epoxy|epoxy;NMP", [["Epoxy"], ["NMP"]]),
        ("Epoxy;epoxy;NMP", [["Epoxy"], ["NMP"]]),
        ("A|B;b|a;C", [["A", "B"], ["C"]]),
    ],
)
def test_parse_knowledge_search_groups(query: str, expected: list[list[str]]) -> None:
    assert parse_search_groups(query) == expected


@pytest.mark.parametrize("query", [";epoxy", "epoxy；", "epoxy||resin", "epoxy OR OR resin", "epoxy;;resin"])
def test_parse_knowledge_search_groups_rejects_empty_conditions(query: str) -> None:
    with pytest.raises(KnowledgeSearchExpressionError, match="terms on both sides"):
        parse_search_groups(query)


def test_parse_knowledge_search_groups_rejects_more_than_ten_terms() -> None:
    with pytest.raises(KnowledgeSearchExpressionError, match="at most 10 terms"):
        parse_search_groups("；".join(f"term-{index}" for index in range(11)))


def test_normalize_knowledge_search_groups_rejects_more_than_twenty_four_expansions() -> None:
    groups = [[f"fragment{index}/segment{index}/portion{index}"] for index in range(7)]
    with pytest.raises(KnowledgeSearchExpressionError, match="more than 24 terms"):
        normalize_search_groups("ignored", groups=groups)


def test_normalize_knowledge_search_groups_deduplicates_equivalent_groups() -> None:
    raw_groups, expanded_groups = normalize_search_groups(
        "ignored",
        groups=[["Epoxy"], ["epoxy"], ["NMP", "solvent"], ["SOLVENT", "nmp"]],
    )

    assert raw_groups == [["Epoxy"], ["NMP", "solvent"]]
    assert expanded_groups == [["Epoxy"], ["NMP", "solvent"]]


def test_knowledge_search_request_rejects_mixed_or_empty_structured_input() -> None:
    with pytest.raises(ValidationError, match="groups and terms cannot be provided together"):
        KnowledgeSearchRequest(
            query="epoxy；NMP",
            groups=[{"terms": ["epoxy"]}],
            terms=["NMP"],
        )

    with pytest.raises(ValidationError, match="terms must not be empty"):
        KnowledgeSearchRequest(query="epoxy", groups=[{"terms": [""]}])

    execution_candidates = [f"candidate-{index}" for index in range(11)]
    assert KnowledgeSearchGroup(terms=execution_candidates).terms == execution_candidates
    with pytest.raises(ValidationError, match="at most 10 terms"):
        KnowledgeSearchRequest(query="expanded group", groups=[{"terms": execution_candidates}])


def test_postgres_knowledge_search_ands_groups_and_scores_aliases_once_per_field() -> None:
    where_sql, _, score_sql, _ = _build_search_sql_parts([["NMP", "N-methyl-2-pyrrolidone"], ["polyimide"]])

    assert ") AND (" in where_sql
    assert score_sql.count("THEN 8 ELSE 0 END") == 2


def test_import_knowledge_zip_to_sqlite_and_searches_abstract(tmp_path: Path) -> None:
    zip_path = tmp_path / "knowledge.zip"
    db_path = tmp_path / "knowledge.db"
    write_knowledge_zip(zip_path)

    stats = import_knowledge_zip_to_sqlite(zip_path=zip_path, db_path=db_path)

    assert stats.file_count == 1
    assert stats.document_count == 2
    assert stats.skipped_row_count == 0

    with sqlite_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT title_en, abstract
            FROM knowledge_documents
            WHERE lower(abstract) LIKE ?
            ORDER BY knowledge_id
            """,
            ("%bisphenol%",),
        ).fetchall()

    assert len(rows) == 1
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
        rows = connection.execute(
            """
            SELECT source_file
            FROM knowledge_documents
            WHERE lower(abstract) LIKE ?
            ORDER BY knowledge_id
            """,
            ("%polyimide%",),
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["source_file"] == "P2_提取结果.xlsx"




def _insert_knowledge_documents(app: FastAPI, rows: list[dict[str, str]]) -> None:
    with postgres_connection(app.state.settings.app_postgres_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO knowledge.documents (
                  knowledge_id,
                  source_file,
                  source_row_number,
                  title_en,
                  abstract,
                  polymer_iupac,
                  formulation
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        index,
                        "fixture.xlsx",
                        index + 1,
                        row.get("title_en", ""),
                        row.get("abstract", ""),
                        row.get("polymer_iupac", ""),
                        row.get("formulation", ""),
                    )
                    for index, row in enumerate(rows, start=1)
                ],
            )

@pytest.mark.asyncio
async def test_search_knowledge_api_returns_matches_from_abstract(test_app: FastAPI) -> None:
    _insert_knowledge_documents(
        test_app,
        [
            {
                "title_en": "Epoxy coating",
                "abstract": "A bisphenol epoxy resin coating is applied to a metal drum.",
                "polymer_iupac": "poly(bisphenol A epoxy resin)",
            },
            {
                "title_en": "Polyester film",
                "abstract": "A polyester film is stretched and heat treated.",
                "polymer_iupac": "poly(ethylene terephthalate)",
            },
        ],
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
async def test_search_knowledge_api_rejects_incomplete_boolean_expression(test_app: FastAPI) -> None:
    with pytest.raises(HTTPException) as raised:
        await search_knowledge(
            KnowledgeSearchRequest(query="epoxy；", top_k=5),
            make_request(test_app),
        )

    assert raised.value.status_code == 422
    assert raised.value.detail == "logic operators must have terms on both sides"


@pytest.mark.asyncio
async def test_search_knowledge_runs_synchronous_work_off_event_loop(
    test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_knowledge_documents(
        test_app,
        [{"title_en": "Epoxy coating", "abstract": "An epoxy resin coating."}],
    )
    event_loop_thread = get_ident()
    worker_threads: list[int] = []
    original_search = knowledge_routes._search_knowledge_sync

    def recording_search(request_body, app):
        worker_threads.append(get_ident())
        return original_search(request_body, app)

    monkeypatch.setattr(knowledge_routes, "_search_knowledge_sync", recording_search)

    response = await search_knowledge(
        KnowledgeSearchRequest(query="epoxy", top_k=5),
        make_request(test_app),
    )

    assert response.total == 1
    assert worker_threads and worker_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_search_knowledge_api_matches_structured_terms_across_fields(test_app: FastAPI) -> None:
    _insert_knowledge_documents(
        test_app,
        [
            {
                "title_en": "Pair formulation",
                "abstract": "This record describes a polymerization recipe.",
                "formulation": "ethanol : propane",
            },
            {
                "title_en": "Single abstract hit",
                "abstract": "Only ethanol is mentioned in this abstract.",
            },
            {
                "title_en": "Alias formulation",
                "abstract": "This record uses an alternative solvent name.",
                "formulation": "ethyl alcohol : propane",
            },
        ],
    )

    response = await search_knowledge(
        KnowledgeSearchRequest(query="monomer pair", terms=["ethanol", "propane"], top_k=5),
        make_request(test_app),
    )

    assert response.query == "monomer pair"
    assert [group.terms for group in response.groups] == [["ethanol"], ["propane"]]
    assert response.terms == ["ethanol", "propane"]
    assert response.total == 1
    assert response.results[0].title_en == "Pair formulation"
    assert response.results[0].matched_terms == ["ethanol", "propane"]
    assert response.results[0].matched_fields == ["Formulation"]

    fallback_response = await search_knowledge(
        KnowledgeSearchRequest(query="ethanol OR propane", top_k=5),
        make_request(test_app),
    )

    assert fallback_response.terms == ["ethanol", "propane"]
    assert [group.terms for group in fallback_response.groups] == [["ethanol", "propane"]]
    assert fallback_response.total == 3
    assert fallback_response.results[0].title_en == "Pair formulation"

    symbol_response = await search_knowledge(
        KnowledgeSearchRequest(query="ethanol；propane", top_k=5),
        make_request(test_app),
    )

    assert [group.terms for group in symbol_response.groups] == [["ethanol"], ["propane"]]
    assert symbol_response.total == 1

    grouped_response = await search_knowledge(
        KnowledgeSearchRequest(
            query="ethanol | ethyl alcohol；propane",
            groups=[{"terms": ["ethanol", "ethyl alcohol"]}, {"terms": ["propane"]}],
            top_k=5,
        ),
        make_request(test_app),
    )

    assert grouped_response.total == 2
    assert {result.title_en for result in grouped_response.results} == {"Pair formulation", "Alias formulation"}


@pytest.mark.asyncio
async def test_search_knowledge_api_keeps_space_delimited_phrases_intact(test_app: FastAPI) -> None:
    _insert_knowledge_documents(
        test_app,
        [
            {
                "title_en": "Exact phrase",
                "abstract": "The resulting polymer has excellent thermal stability.",
            },
            {
                "title_en": "Thermal only",
                "abstract": "The thermal conductivity was measured after curing.",
            },
            {
                "title_en": "Stability only",
                "abstract": "The storage stability was measured at room temperature.",
            },
        ],
    )

    response = await search_knowledge(
        KnowledgeSearchRequest(query="thermal stability", top_k=5),
        make_request(test_app),
    )

    assert response.terms == ["thermal stability"]
    assert response.total == 1
    assert [result.title_en for result in response.results] == ["Exact phrase"]


@pytest.mark.asyncio
async def test_search_knowledge_api_expands_iupac_leading_locant_variant(test_app: FastAPI) -> None:
    _insert_knowledge_documents(
        test_app,
        [
            {
                "title_en": "Aminophenyl paper",
                "abstract": "The synthesis uses a 4-Aminophenyl-containing diamine.",
                "formulation": "",
            },
            {
                "title_en": "Locant-free paper",
                "abstract": "The formulation uses an aromatic ester.",
                "formulation": "Aminophenyl derivative",
            },
        ],
    )

    response = await search_knowledge(
        KnowledgeSearchRequest(query="monomer", terms=["4-Aminophenyl"], top_k=5),
        make_request(test_app),
    )

    assert response.terms == [
        "4-Aminophenyl",
        "Aminophenyl",
    ]
    assert [group.terms for group in response.groups] == [["4-Aminophenyl", "Aminophenyl"]]
    assert response.total == 2
    results_by_title = {result.title_en: result for result in response.results}
    assert set(results_by_title) == {"Aminophenyl paper", "Locant-free paper"}
    assert results_by_title["Aminophenyl paper"].matched_terms == ["4-Aminophenyl", "Aminophenyl"]
    assert results_by_title["Locant-free paper"].matched_terms == ["Aminophenyl"]


@pytest.mark.asyncio
async def test_search_knowledge_api_does_not_double_score_group_fragments(test_app: FastAPI) -> None:
    _insert_knowledge_documents(
        test_app,
        [
            {
                "title_en": "One fragment in polymer",
                "abstract": "This record only has background text.",
                "polymer_iupac": "poly(benzophenone imide)",
            },
            {
                "title_en": "Two fragments in abstract",
                "abstract": "This record mentions benzophenone and aminobenzoate fragments together.",
                "polymer_iupac": "",
            },
        ],
    )

    response = await search_knowledge(
        KnowledgeSearchRequest(query="monomer", terms=["benzophenone/aminobenzoate"], top_k=5),
        make_request(test_app),
    )

    assert [result.title_en for result in response.results] == [
        "One fragment in polymer",
        "Two fragments in abstract",
    ]


@pytest.mark.asyncio
async def test_search_knowledge_api_returns_requested_result_page(test_app: FastAPI) -> None:
    _insert_knowledge_documents(
        test_app,
        [
            {
                "title_en": f"Epoxy paper {index + 1}",
                "abstract": "This paper describes an epoxy polymer.",
            }
            for index in range(25)
        ],
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
