from __future__ import annotations

from collections.abc import Generator

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.models import (
    LabDataCountRead,
    LabDataProjectStatsRead,
    LabDataSampleMeasurementCreate,
    LabDataSampleMeasurementPageRead,
    LabDataSampleMeasurementRead,
    LabDataSummaryRead,
    LabDataTestProjectRead,
)
from app.postgres_database import PostgresUnavailableError
from app.services.lab_data import LabDataService
from app.services.lab_data_repository import LabDataDuplicateSampleIdError


router = APIRouter(prefix="/api/v1/lab-data", tags=["lab-data"])


def get_lab_data_service(request: Request) -> Generator[LabDataService, None, None]:
    settings = request.app.state.settings
    try:
        with request.app.state.postgres_connection_factory(settings.lab_data_postgres_dsn) as connection:
            service = LabDataService(connection)
            service.ensure_schema()
            yield service
    except PostgresUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Lab data PostgreSQL database is not reachable",
        ) from exc


@router.get("/test-projects", response_model=list[LabDataTestProjectRead], response_model_by_alias=True)
def list_test_projects(service: LabDataService = Depends(get_lab_data_service)) -> list[LabDataTestProjectRead]:
    return service.list_test_projects()


@router.post(
    "/sample-measurements",
    response_model=LabDataSampleMeasurementRead,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
def create_sample_measurement(
    payload: LabDataSampleMeasurementCreate,
    service: LabDataService = Depends(get_lab_data_service),
) -> LabDataSampleMeasurementRead:
    try:
        return service.create_measurement(payload)
    except LabDataDuplicateSampleIdError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/sample-measurements", response_model=LabDataSampleMeasurementPageRead, response_model_by_alias=True)
def list_sample_measurements(
    experiment_project: str | None = None,
    page: int = 1,
    page_size: int = 20,
    recent_days: int | None = None,
    service: LabDataService = Depends(get_lab_data_service),
) -> LabDataSampleMeasurementPageRead:
    if page < 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="page 必须大于等于 1。")
    if page_size < 1 or page_size > 10000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="pageSize 必须在 1 到 10000 之间。")
    if recent_days is not None and recent_days < 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="recentDays 必须大于等于 1。")

    return service.list_measurements(
        experiment_project=experiment_project,
        page=page,
        page_size=page_size,
        recent_days=recent_days,
    )


@router.get("/sample-measurements/count", response_model=LabDataCountRead)
def get_sample_measurements_count(service: LabDataService = Depends(get_lab_data_service)) -> LabDataCountRead:
    return service.count_measurements()


@router.get(
    "/sample-measurements/stats/by-project",
    response_model=list[LabDataProjectStatsRead],
    response_model_by_alias=True,
)
def get_sample_measurements_stats_by_project(
    service: LabDataService = Depends(get_lab_data_service),
) -> list[LabDataProjectStatsRead]:
    return service.count_by_project()


@router.get("/sample-measurements/stats/summary", response_model=LabDataSummaryRead, response_model_by_alias=True)
def get_sample_measurements_summary(service: LabDataService = Depends(get_lab_data_service)) -> LabDataSummaryRead:
    return service.summary()
