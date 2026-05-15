from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from time import perf_counter
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.config import Settings
from app.models import (
    MutationResponse,
    OnlineKnowledgeDefaultConfigResponse,
    OnlineKnowledgeExportRequest,
    OnlineKnowledgeExportResponse,
    OnlineKnowledgeJobCreateResponse,
    OnlineKnowledgeJobResponse,
    OnlineKnowledgeHistoryResponse,
    OnlineKnowledgeSearchRequest,
    OnlineKnowledgeSearchResponse,
)
from app.database import sqlite_connection
from app.services.online_knowledge.history_repository import (
    clear_online_history,
    create_online_job,
    delete_online_history,
    get_online_job,
    list_online_history,
    mark_online_job_completed,
    mark_online_job_failed,
    mark_online_job_running,
    save_online_history,
)
from app.services.online_knowledge.search_service import (
    OnlineKnowledgeConfigError,
    OnlineKnowledgeModelError,
    run_online_knowledge_search,
    validate_model_access,
)


router = APIRouter(prefix="/api/v1/online-knowledge", tags=["online-knowledge"])


@dataclass(frozen=True)
class OnlineModelAccess:
    api_key: str
    base_url: str
    model: str


@router.get("/default-config", response_model=OnlineKnowledgeDefaultConfigResponse)
def get_online_knowledge_default_config(request: Request) -> OnlineKnowledgeDefaultConfigResponse:
    settings = request.app.state.settings
    return OnlineKnowledgeDefaultConfigResponse(
        base_url=settings.online_knowledge_base_url,
        model=settings.online_knowledge_model,
        max_papers=settings.online_knowledge_max_papers,
        has_server_api_key=bool(settings.online_knowledge_api_key),
    )


@router.post("/search", response_model=OnlineKnowledgeSearchResponse)
def search_online_knowledge(
    request_body: OnlineKnowledgeSearchRequest,
    request: Request,
) -> OnlineKnowledgeSearchResponse:
    started_at = perf_counter()
    settings = request.app.state.settings

    try:
        model_access = resolve_online_model_access(request_body, settings)
        result_data = _run_search_from_request(request_body, model_access)
    except OnlineKnowledgeConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OnlineKnowledgeModelError as exc:
        raise HTTPException(status_code=502, detail=f"Model extraction failed: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Online retrieval failed: {exc}") from exc

    result_data["query_time_ms"] = (perf_counter() - started_at) * 1000
    response = OnlineKnowledgeSearchResponse.model_validate(result_data)

    with request.app.state.sqlite_connection_factory(settings.sqlite_db_file) as connection:
        save_online_history(
            connection,
            material=response.material,
            mode=response.mode,
            max_papers=response.max_papers,
            result_data=response.model_dump(mode="json"),
        )

    return response


@router.post("/jobs", response_model=OnlineKnowledgeJobCreateResponse, status_code=202)
def create_online_knowledge_job(
    request_body: OnlineKnowledgeSearchRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> OnlineKnowledgeJobCreateResponse:
    settings = request.app.state.settings
    try:
        model_access = resolve_online_model_access(request_body, settings)
    except OnlineKnowledgeConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    job_id = uuid4().hex
    with request.app.state.sqlite_connection_factory(settings.sqlite_db_file) as connection:
        create_online_job(
            connection,
            job_id=job_id,
            material=request_body.material,
            mode=request_body.mode,
            max_papers=request_body.max_papers,
        )

    background_tasks.add_task(
        _run_online_knowledge_job,
        job_id,
        settings.sqlite_db_file,
        request_body,
        model_access,
    )
    return OnlineKnowledgeJobCreateResponse(job_id=job_id, status="pending")


@router.get("/jobs/{job_id}", response_model=OnlineKnowledgeJobResponse)
def get_online_knowledge_job(job_id: str, request: Request) -> OnlineKnowledgeJobResponse:
    settings = request.app.state.settings
    with request.app.state.sqlite_connection_factory(settings.sqlite_db_file) as connection:
        job = get_online_job(connection, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return OnlineKnowledgeJobResponse.model_validate(job)


@router.get("/history", response_model=OnlineKnowledgeHistoryResponse)
def get_online_knowledge_history(request: Request) -> OnlineKnowledgeHistoryResponse:
    settings = request.app.state.settings
    with request.app.state.sqlite_connection_factory(settings.sqlite_db_file) as connection:
        history = list_online_history(connection)
    return OnlineKnowledgeHistoryResponse(history=history)


@router.delete("/history/{history_id}", response_model=MutationResponse)
def delete_online_knowledge_history(history_id: int, request: Request) -> MutationResponse:
    settings = request.app.state.settings
    with request.app.state.sqlite_connection_factory(settings.sqlite_db_file) as connection:
        deleted = delete_online_history(connection, history_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="History item not found")
    return MutationResponse(success=True)


@router.post("/history/clear", response_model=MutationResponse)
def clear_online_knowledge_search_history(request: Request) -> MutationResponse:
    settings = request.app.state.settings
    with request.app.state.sqlite_connection_factory(settings.sqlite_db_file) as connection:
        clear_online_history(connection)
    return MutationResponse(success=True)


@router.post("/export-csv", response_model=OnlineKnowledgeExportResponse)
def export_online_knowledge_csv(request_body: OnlineKnowledgeExportRequest) -> OnlineKnowledgeExportResponse:
    fieldnames: list[str] = []
    for row in request_body.data:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in request_body.data:
        writer.writerow({key: _safe_csv_value(row.get(key)) for key in fieldnames})

    filename = request_body.filename or f"synthesis_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return OnlineKnowledgeExportResponse(
        success=True,
        csv_content=f"\ufeff{output.getvalue()}",
        filename=filename,
    )


def _run_search_from_request(
    request_body: OnlineKnowledgeSearchRequest,
    model_access: OnlineModelAccess,
) -> dict:
    return run_online_knowledge_search(
        material=request_body.material,
        mode=request_body.mode,
        api_key=model_access.api_key,
        base_url=model_access.base_url,
        model=model_access.model,
        max_papers=request_body.max_papers,
        extraction_delay_seconds=request_body.extraction_delay_seconds,
    )


def resolve_online_model_access(
    request_body: OnlineKnowledgeSearchRequest,
    settings: Settings,
) -> OnlineModelAccess:
    api_key = request_body.api_key or ""
    if request_body.use_server_default:
        api_key = settings.online_knowledge_api_key
        if not api_key:
            raise OnlineKnowledgeConfigError("Server default API Key is not configured")

    access = OnlineModelAccess(
        api_key=api_key,
        base_url=request_body.base_url,
        model=request_body.model,
    )
    validate_model_access(
        api_key=access.api_key,
        base_url=access.base_url,
        model=access.model,
    )
    return access


def _run_online_knowledge_job(
    job_id: str,
    sqlite_db_file,
    request_body: OnlineKnowledgeSearchRequest,
    model_access: OnlineModelAccess,
) -> None:
    with sqlite_connection(sqlite_db_file) as connection:
        mark_online_job_running(connection, job_id)

    started_at = perf_counter()
    try:
        result_data = run_online_knowledge_search(
            material=request_body.material,
            mode=request_body.mode,
            api_key=model_access.api_key,
            base_url=model_access.base_url,
            model=model_access.model,
            max_papers=request_body.max_papers,
            extraction_delay_seconds=request_body.extraction_delay_seconds,
        )
        result_data["query_time_ms"] = (perf_counter() - started_at) * 1000
        response = OnlineKnowledgeSearchResponse.model_validate(result_data)
        result_json = response.model_dump(mode="json")
        with sqlite_connection(sqlite_db_file) as connection:
            mark_online_job_completed(connection, job_id, result_json)
            save_online_history(
                connection,
                material=response.material,
                mode=response.mode,
                max_papers=response.max_papers,
                result_data=result_json,
            )
    except Exception as exc:
        with sqlite_connection(sqlite_db_file) as connection:
            mark_online_job_failed(connection, job_id, _public_job_error_message(exc))


def _safe_csv_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    if value and value[0] in {"=", "+", "-", "@", "\t", "\r"}:
        return f"'{value}"
    return value


def _public_job_error_message(exc: Exception) -> str:
    if isinstance(exc, OnlineKnowledgeConfigError):
        return "Model access configuration is invalid."
    if isinstance(exc, OnlineKnowledgeModelError):
        return "Model extraction failed. Check the API key, Base URL, model, and provider access."
    return "Online retrieval failed. Check network access and provider availability."
