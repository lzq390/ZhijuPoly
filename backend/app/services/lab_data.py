from __future__ import annotations

from typing import Any

from app.models import (
    LabDataCountRead,
    LabDataProjectStatsRead,
    LabDataSampleMeasurementCreate,
    LabDataSampleMeasurementPageRead,
    LabDataSampleMeasurementRead,
    LabDataSummaryRead,
    LabDataTestProjectRead,
)
from app.services.lab_data_repository import LabDataRepository


class LabDataService:
    def __init__(self, connection: Any):
        self.repository = LabDataRepository(connection)

    def ensure_schema(self) -> None:
        self.repository.ensure_schema()

    def list_test_projects(self) -> list[LabDataTestProjectRead]:
        return [LabDataTestProjectRead.model_validate(row) for row in self.repository.list_test_projects()]

    def create_measurement(self, payload: LabDataSampleMeasurementCreate) -> LabDataSampleMeasurementRead:
        row = self.repository.create_measurement(payload)
        return LabDataSampleMeasurementRead.model_validate(row)

    def count_measurements(self) -> LabDataCountRead:
        return LabDataCountRead(count=self.repository.count_measurements())

    def count_by_project(self) -> list[LabDataProjectStatsRead]:
        return [
            LabDataProjectStatsRead(experimentProject=row["experiment_project"], count=row["count"])
            for row in self.repository.count_by_project()
        ]

    def summary(self) -> LabDataSummaryRead:
        return LabDataSummaryRead(
            totalCount=self.repository.count_measurements(),
            byProject=self.count_by_project(),
        )

    def list_measurements(
        self,
        *,
        experiment_project: str | None = None,
        page: int = 1,
        page_size: int = 20,
        recent_days: int | None = None,
    ) -> LabDataSampleMeasurementPageRead:
        rows, total = self.repository.list_measurements(
            experiment_project=experiment_project,
            page=page,
            page_size=page_size,
            recent_days=recent_days,
        )
        return LabDataSampleMeasurementPageRead(
            items=[LabDataSampleMeasurementRead.model_validate(row) for row in rows],
            total=total,
            page=page,
            pageSize=page_size,
        )
