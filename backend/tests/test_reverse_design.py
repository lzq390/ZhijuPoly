from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from app.config import Settings
from app.database import sqlite_connection
from app.import_pi_candidates import import_pi_candidates_to_sqlite
from app.main import create_app
from app.models import ReverseDesignKnowledgeRequest, ReverseDesignTgRequest
from app.routers.reverse_design import search_by_tg, search_candidate_knowledge
from app.services.reverse_design import search_reverse_design_by_tg


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


def write_pi_csv(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "id,mon1,mon2,polym,tg_celsius",
                "1,CCO,CCN,CCO,100",
                "2,CCO,CCC,CCO,125",
                "3,CCN,CCC,CCN,90",
                "4,CCO,CCC,not-a-smiles,80",
                "5,CCC,CCO,CCC,130",
            ]
        ),
        encoding="utf-8",
    )


def build_reverse_design_app(tmp_path: Path) -> FastAPI:
    csv_path = tmp_path / "pi.csv"
    main_db_path = tmp_path / "polyprop.db"
    pi_db_path = tmp_path / "pi.db"
    write_pi_csv(csv_path)
    import_pi_candidates_to_sqlite(csv_path=csv_path, db_path=pi_db_path, progress_interval=0)

    settings = Settings(
        sqlite_db_path=str(main_db_path),
        csv_source_path=str(tmp_path / "source.csv"),
        pi_reverse_db_path=str(pi_db_path),
        pi_reverse_csv_path=str(csv_path),
        allowed_origins="http://localhost:5173",
        model_enabled=False,
    )
    return create_app(settings)


def test_reverse_design_service_sorts_sample_by_tg_difference(tmp_path: Path) -> None:
    csv_path = tmp_path / "pi.csv"
    db_path = tmp_path / "pi.db"
    write_pi_csv(csv_path)
    import_pi_candidates_to_sqlite(csv_path=csv_path, db_path=db_path, progress_interval=0)

    with sqlite_connection(db_path) as connection:
        result = search_reverse_design_by_tg(
            connection,
            "CCO",
            120,
            similarity_threshold=0.0,
            candidate_sample_size=10,
            top_k=3,
            random_seed=1,
        )

    assert result.candidate_pool_size == 4
    assert result.sampled_candidate_count == 4
    assert [candidate.pi_id for candidate in result.results] == [2, 5, 1]
    assert result.results[0].tg_difference == 5


def test_reverse_design_service_random_seed_is_reproducible(tmp_path: Path) -> None:
    csv_path = tmp_path / "pi.csv"
    db_path = tmp_path / "pi.db"
    write_pi_csv(csv_path)
    import_pi_candidates_to_sqlite(csv_path=csv_path, db_path=db_path, progress_interval=0)

    with sqlite_connection(db_path) as connection:
        first = search_reverse_design_by_tg(
            connection,
            "CCO",
            120,
            similarity_threshold=0.0,
            candidate_sample_size=2,
            top_k=2,
            random_seed=42,
        )
        second = search_reverse_design_by_tg(
            connection,
            "CCO",
            120,
            similarity_threshold=0.0,
            candidate_sample_size=2,
            top_k=2,
            random_seed=42,
        )

    assert first.candidate_pool_size == 4
    assert first.sampled_candidate_count == 2
    assert [candidate.pi_id for candidate in first.results] == [candidate.pi_id for candidate in second.results]


@pytest.mark.asyncio
async def test_reverse_design_api_returns_candidates(tmp_path: Path) -> None:
    app = build_reverse_design_app(tmp_path)
    request = make_request(app)

    response = await search_by_tg(
        ReverseDesignTgRequest(
            smiles="CCO",
            target_tg=120,
            similarity_threshold=0.0,
            candidate_sample_size=10,
            top_k=2,
            random_seed=1,
        ),
        request,
    )

    assert response.target_tg == 120
    assert response.candidate_pool_size == 4
    assert response.sampled_candidate_count == 4
    assert response.total == 2
    assert response.results[0].rank == 1
    assert response.results[0].pi_id == 2
    assert response.results[0].structure_svg is not None


@pytest.mark.asyncio
async def test_reverse_design_api_rejects_invalid_smiles(tmp_path: Path) -> None:
    app = build_reverse_design_app(tmp_path)
    request = make_request(app)

    with pytest.raises(HTTPException) as exc_info:
        await search_by_tg(
            ReverseDesignTgRequest(
                smiles="not-a-smiles",
                target_tg=120,
            ),
            request,
        )

    assert exc_info.value.status_code == 422
    assert "invalid smiles" in exc_info.value.detail


@pytest.mark.asyncio
async def test_reverse_design_api_reports_uninitialized_database(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_db_path=str(tmp_path / "polyprop.db"),
        csv_source_path=str(tmp_path / "source.csv"),
        pi_reverse_db_path=str(tmp_path / "empty_pi.db"),
        allowed_origins="http://localhost:5173",
        model_enabled=False,
    )
    app = create_app(settings)
    request = make_request(app)

    with pytest.raises(HTTPException) as exc_info:
        await search_by_tg(
            ReverseDesignTgRequest(
                smiles="CCO",
                target_tg=120,
            ),
            request,
        )

    assert exc_info.value.status_code == 503
    assert "not initialized" in exc_info.value.detail


@pytest.mark.asyncio
async def test_reverse_design_knowledge_returns_null_when_iupac_is_unavailable(tmp_path: Path) -> None:
    app = build_reverse_design_app(tmp_path)
    request = make_request(app)

    response = await search_candidate_knowledge(
        ReverseDesignKnowledgeRequest(pi_id=1, top_k=10),
        request,
    )

    assert response.pi_id == 1
    assert response.monomer_a_smiles == "CCO"
    assert response.monomer_b_smiles == "CCN"
    assert response.monomer_a_iupac is None
    assert response.monomer_b_iupac is None
    assert response.knowledge_query is None
    assert response.knowledge is None
