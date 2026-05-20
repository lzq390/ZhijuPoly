from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.models import LabDataSampleMeasurementCreate


DEFAULT_TEST_PROJECTS: tuple[tuple[str, str], ...] = (
    ("Tg", "K"),
    ("Rg", "nm"),
    ("FFV", "None"),
    ("Tm", "K"),
    ("Eg", "eV"),
)


class LabDataDuplicateSampleIdError(Exception):
    pass


def _is_unique_violation(exc: Exception) -> bool:
    return exc.__class__.__name__ == "UniqueViolation"


def _is_schema_already_exists_race(exc: Exception) -> bool:
    return exc.__class__.__name__ in {"DuplicateSchema", "UniqueViolation"}


def _rollback_if_available(connection: Any) -> None:
    rollback = getattr(connection, "rollback", None)
    if callable(rollback):
        rollback()


def _row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row)


class LabDataRepository:
    def __init__(self, connection: Any):
        self.connection = connection

    def ensure_schema(self) -> None:
        try:
            self.connection.execute("CREATE SCHEMA IF NOT EXISTS data_collection_demo")
        except Exception as exc:
            if not _is_schema_already_exists_race(exc):
                raise
            _rollback_if_available(self.connection)

        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS data_collection_demo.test_projects (
              id serial4 NOT NULL,
              project_name varchar(100) NOT NULL,
              result_unit varchar(20) NOT NULL,
              CONSTRAINT test_projects_pkey PRIMARY KEY (id),
              CONSTRAINT test_projects_project_name_key UNIQUE (project_name)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS data_collection_demo.sample_measurements (
              id serial4 NOT NULL,
              sample_id varchar(50) NOT NULL,
              experiment_project varchar(100) NOT NULL,
              instrument_id varchar(50) NOT NULL,
              "operator" varchar(100) NOT NULL,
              collection_time timestamp NOT NULL,
              temperature numeric(5, 2) NULL,
              concentration numeric(10, 4) NULL,
              result_value numeric(10, 4) NOT NULL,
              result_unit varchar(20) NOT NULL,
              remarks text NULL,
              CONSTRAINT sample_measurements_pkey PRIMARY KEY (id),
              CONSTRAINT sample_measurements_sample_id_key UNIQUE (sample_id)
            )
            """
        )

        for project_name, result_unit in DEFAULT_TEST_PROJECTS:
            self.connection.execute(
                """
                INSERT INTO data_collection_demo.test_projects (project_name, result_unit)
                VALUES (%s, %s)
                ON CONFLICT (project_name) DO NOTHING
                """,
                (project_name, result_unit),
            )

    def list_test_projects(self) -> list[dict[str, Any]]:
        cursor = self.connection.execute(
            """
            SELECT id, project_name, result_unit
            FROM data_collection_demo.test_projects
            ORDER BY id ASC
            """
        )
        return [_row_to_dict(row) for row in cursor.fetchall()]

    def create_measurement(self, payload: LabDataSampleMeasurementCreate) -> dict[str, Any]:
        values = (
            payload.sample_id,
            payload.experiment_project,
            payload.instrument_id,
            payload.operator,
            payload.collection_time,
            payload.temperature,
            payload.concentration,
            payload.result_value,
            payload.result_unit,
            payload.remarks,
        )

        try:
            cursor = self.connection.execute(
                """
                INSERT INTO data_collection_demo.sample_measurements (
                  sample_id,
                  experiment_project,
                  instrument_id,
                  "operator",
                  collection_time,
                  temperature,
                  concentration,
                  result_value,
                  result_unit,
                  remarks
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING
                  id,
                  sample_id,
                  experiment_project,
                  instrument_id,
                  "operator" AS operator,
                  collection_time,
                  temperature,
                  concentration,
                  result_value,
                  result_unit,
                  remarks
                """,
                values,
            )
        except Exception as exc:
            if _is_unique_violation(exc):
                raise LabDataDuplicateSampleIdError("样本编号已存在，请更换 sampleId 后重试。") from exc
            raise

        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("sample measurement insert did not return a row")
        return _row_to_dict(row)

    def count_measurements(
        self,
        *,
        experiment_project: str | None = None,
        recent_days: int | None = None,
    ) -> int:
        where_clause, params = self._measurement_filters(
            experiment_project=experiment_project,
            recent_days=recent_days,
        )
        cursor = self.connection.execute(
            f"""
            SELECT count(*) AS count
            FROM data_collection_demo.sample_measurements
            {where_clause}
            """,
            tuple(params),
        )
        row = cursor.fetchone()
        return int(row["count"]) if row is not None else 0

    def count_by_project(self) -> list[dict[str, Any]]:
        cursor = self.connection.execute(
            """
            SELECT experiment_project, count(*) AS count
            FROM data_collection_demo.sample_measurements
            GROUP BY experiment_project
            ORDER BY count(*) DESC, experiment_project ASC
            """
        )
        return [_row_to_dict(row) for row in cursor.fetchall()]

    def list_measurements(
        self,
        *,
        experiment_project: str | None = None,
        page: int = 1,
        page_size: int = 20,
        recent_days: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        where_clause, params = self._measurement_filters(
            experiment_project=experiment_project,
            recent_days=recent_days,
        )
        total = self.count_measurements(experiment_project=experiment_project, recent_days=recent_days)
        cursor = self.connection.execute(
            f"""
            SELECT
              id,
              sample_id,
              experiment_project,
              instrument_id,
              "operator" AS operator,
              collection_time,
              temperature,
              concentration,
              result_value,
              result_unit,
              remarks
            FROM data_collection_demo.sample_measurements
            {where_clause}
            ORDER BY collection_time DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            (*params, page_size, (page - 1) * page_size),
        )
        return [_row_to_dict(row) for row in cursor.fetchall()], total

    def _measurement_filters(
        self,
        *,
        experiment_project: str | None,
        recent_days: int | None,
    ) -> tuple[str, list[Any]]:
        filters: list[str] = []
        params: list[Any] = []
        if experiment_project:
            filters.append("experiment_project = %s")
            params.append(experiment_project)
        if recent_days is not None:
            filters.append("collection_time >= %s")
            params.append(datetime.now() - timedelta(days=recent_days))

        if not filters:
            return "", params
        return f"WHERE {' AND '.join(filters)}", params
