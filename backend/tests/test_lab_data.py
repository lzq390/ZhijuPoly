from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.postgres_database import PostgresUnavailableError
from app.services.lab_data_repository import DEFAULT_TEST_PROJECTS, LabDataRepository


class UniqueViolation(Exception):
    pass


class FakeCursor:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class FakeLabDataConnection:
    def __init__(self) -> None:
        self.projects: list[dict[str, Any]] = []
        self.measurements: list[dict[str, Any]] = []
        self.next_project_id = 1
        self.next_measurement_id = 1
        self.executed_sql: list[str] = []

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> FakeCursor:
        normalized = " ".join(sql.lower().split())
        self.executed_sql.append(normalized)

        if (
            normalized.startswith("create schema")
            or normalized.startswith("create table")
            or normalized.startswith("do ")
            or normalized.startswith("select setval(")
        ):
            return FakeCursor()

        if normalized.startswith("insert into lab.test_projects"):
            project_name, result_unit = params or ("", "")
            if not any(project["project_name"] == project_name for project in self.projects):
                self.projects.append(
                    {
                        "id": self.next_project_id,
                        "project_name": project_name,
                        "result_unit": result_unit,
                    }
                )
                self.next_project_id += 1
            return FakeCursor()

        if normalized.startswith("select id, project_name, result_unit from lab.test_projects"):
            return FakeCursor(sorted(self.projects, key=lambda project: project["id"]))

        if normalized.startswith("insert into lab.sample_measurements"):
            assert params is not None
            if any(measurement["sample_id"] == params[0] for measurement in self.measurements):
                raise UniqueViolation("duplicate sample_id")

            row = {
                "id": self.next_measurement_id,
                "sample_id": params[0],
                "experiment_project": params[1],
                "instrument_id": params[2],
                "operator": params[3],
                "collection_time": params[4],
                "temperature": params[5],
                "concentration": params[6],
                "result_value": params[7],
                "result_unit": params[8],
                "remarks": params[9],
            }
            self.measurements.append(row)
            self.next_measurement_id += 1
            return FakeCursor([row])

        if normalized.startswith("select count(*) as count from lab.sample_measurements"):
            rows = self._filter_measurements(normalized, params)
            return FakeCursor([{"count": len(rows)}])

        if normalized.startswith("select experiment_project, count(*) as count from lab.sample_measurements"):
            counts: dict[str, int] = {}
            for row in self.measurements:
                counts[row["experiment_project"]] = counts.get(row["experiment_project"], 0) + 1
            rows = [
                {"experiment_project": project, "count": count}
                for project, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            ]
            return FakeCursor(rows)

        if normalized.startswith("select id, sample_id"):
            rows = self._filter_measurements(normalized, params)
            limit = int(params[-2]) if params else 20
            offset = int(params[-1]) if params else 0
            rows = sorted(rows, key=lambda row: (row["collection_time"], row["id"]), reverse=True)
            return FakeCursor(rows[offset : offset + limit])

        raise AssertionError(f"Unexpected SQL in fake lab data connection: {sql}")

    def _filter_measurements(
        self,
        normalized_sql: str,
        params: tuple[Any, ...] | None,
    ) -> list[dict[str, Any]]:
        rows = list(self.measurements)
        filter_params = list(params or ())
        if "where" not in normalized_sql:
            return rows

        if "experiment_project = %s" in normalized_sql:
            project = filter_params.pop(0)
            rows = [row for row in rows if row["experiment_project"] == project]

        if "collection_time >= %s" in normalized_sql:
            threshold = filter_params.pop(0)
            rows = [row for row in rows if row["collection_time"] >= threshold]

        return rows


class SchemaCreateRaceConnection(FakeLabDataConnection):
    def __init__(self) -> None:
        super().__init__()
        self.rollback_count = 0
        self._raised_schema_race = False

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> FakeCursor:
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("create schema") and not self._raised_schema_race:
            self._raised_schema_race = True
            raise UniqueViolation("Key (nspname)=(lab) already exists.")
        return super().execute(sql, params)

    def rollback(self) -> None:
        self.rollback_count += 1


def make_client_with_fake_lab_data(fake_connection: FakeLabDataConnection) -> TestClient:
    settings = Settings(
        sqlite_db_path="backend/data/test-missing.db",
        pi_reverse_backend="postgres",
        app_postgres_dsn="postgresql://pi-user:pi-pass@example.invalid/nexpoly",
        pi_postgres_dsn="postgresql://pi-user:pi-pass@example.invalid/nexpoly",
        lab_data_postgres_dsn="",
        model_enabled=False,
    )
    app = create_app(settings)

    @contextmanager
    def fake_connection_factory(dsn: str):
        assert dsn == "postgresql://pi-user:pi-pass@example.invalid/nexpoly"
        yield fake_connection

    @contextmanager
    def fake_deployment_control_connection_factory(dsn: str):
        assert dsn == "postgresql://pi-user:pi-pass@example.invalid/nexpoly"

        class DeploymentControlConnection:
            def execute(self, sql: str, params=None) -> FakeCursor:
                assert "from governance.deployment_control" in " ".join(sql.lower().split())
                return FakeCursor(
                    [
                        {
                            "drain_enabled": False,
                            "reason": None,
                            "release_sha": None,
                            "activated_at": None,
                            "activated_by": None,
                            "updated_at": datetime.now(),
                        }
                    ]
                )

        yield DeploymentControlConnection()

    app.state.postgres_connection_factory = fake_connection_factory
    app.state.deployment_control_connection_factory = fake_deployment_control_connection_factory
    return TestClient(app)


def test_lab_data_schema_initialization_tolerates_concurrent_schema_create() -> None:
    fake_connection = SchemaCreateRaceConnection()

    LabDataRepository(fake_connection).ensure_schema()

    assert fake_connection.rollback_count == 1
    assert [project["project_name"] for project in fake_connection.projects] == [
        project_name for project_name, _ in DEFAULT_TEST_PROJECTS
    ]


def test_settings_lab_data_dsn_defaults_to_app_postgres_dsn() -> None:
    settings = Settings(app_postgres_dsn="postgresql://app/nexpoly", lab_data_postgres_dsn="")
    assert settings.lab_data_postgres_dsn == "postgresql://app/nexpoly"

    dedicated_settings = Settings(
        app_postgres_dsn="postgresql://app/nexpoly",
        lab_data_postgres_dsn="postgresql://lab/lab_data",
    )
    assert dedicated_settings.lab_data_postgres_dsn == "postgresql://lab/lab_data"


def test_lab_data_collection_endpoints_create_list_and_summarize_measurements() -> None:
    fake_connection = FakeLabDataConnection()
    client = make_client_with_fake_lab_data(fake_connection)

    projects_response = client.get("/api/v1/lab-data/test-projects")

    assert projects_response.status_code == 200
    assert projects_response.json() == [
        {"id": 1, "projectName": "Tg", "resultUnit": "K"},
        {"id": 2, "projectName": "Rg", "resultUnit": "nm"},
        {"id": 3, "projectName": "FFV", "resultUnit": "None"},
        {"id": 4, "projectName": "Tm", "resultUnit": "K"},
        {"id": 5, "projectName": "Eg", "resultUnit": "eV"},
    ]

    recent_time = datetime.now().replace(microsecond=0)
    old_time = recent_time - timedelta(days=30)

    first_payload = {
        "sampleId": "SAMPLE-RECENT-001",
        "experimentProject": "Tg",
        "instrumentId": "INST-TEMP-05",
        "operator": "operator1",
        "collectionTime": recent_time.isoformat(),
        "temperature": None,
        "concentration": None,
        "resultValue": 412.35,
        "resultUnit": "K",
        "remarks": "无异常",
    }
    old_payload = {
        **first_payload,
        "sampleId": "SAMPLE-OLD-001",
        "collectionTime": old_time.isoformat(),
        "resultValue": 390.12,
        "remarks": None,
    }

    create_response = client.post("/api/v1/lab-data/sample-measurements", json=first_payload)
    old_create_response = client.post("/api/v1/lab-data/sample-measurements", json=old_payload)
    duplicate_response = client.post("/api/v1/lab-data/sample-measurements", json=first_payload)

    assert create_response.status_code == 201
    assert create_response.json()["id"] == 1
    assert old_create_response.status_code == 201
    assert duplicate_response.status_code == 409
    assert duplicate_response.json()["detail"] == "样本编号已存在，请更换 sampleId 后重试。"

    summary_response = client.get("/api/v1/lab-data/sample-measurements/stats/summary")
    assert summary_response.status_code == 200
    assert summary_response.json() == {
        "totalCount": 2,
        "byProject": [{"experimentProject": "Tg", "count": 2}],
    }

    recent_response = client.get(
        "/api/v1/lab-data/sample-measurements",
        params={"experiment_project": "Tg", "page": 1, "page_size": 20, "recent_days": 7},
    )
    assert recent_response.status_code == 200
    recent_data = recent_response.json()
    assert recent_data["total"] == 1
    assert recent_data["items"][0]["sampleId"] == "SAMPLE-RECENT-001"
    assert recent_data["pageSize"] == 20


def test_lab_data_rejects_invalid_pagination() -> None:
    client = make_client_with_fake_lab_data(FakeLabDataConnection())

    response = client.get("/api/v1/lab-data/sample-measurements", params={"page": 0})

    assert response.status_code == 400
    assert response.json()["detail"] == "page 必须大于等于 1。"


def test_lab_data_database_unavailable_is_scoped_to_lab_data_routes() -> None:
    settings = Settings(
        pi_reverse_backend="postgres",
        app_postgres_dsn="postgresql://pi-user:pi-pass@example.invalid/nexpoly",
        pi_postgres_dsn="postgresql://pi-user:pi-pass@example.invalid/nexpoly",
        lab_data_postgres_dsn="",
        model_enabled=False,
    )
    app = create_app(settings)

    @contextmanager
    def unavailable_connection_factory(dsn: str):
        raise PostgresUnavailableError("PI Postgres database is not reachable")
        yield

    app.state.postgres_connection_factory = unavailable_connection_factory
    client = TestClient(app)

    lab_response = client.get("/api/v1/lab-data/test-projects")
    health_response = client.get("/health")

    assert lab_response.status_code == 503
    assert lab_response.json()["detail"] == "Lab data PostgreSQL database is not reachable"
    assert health_response.status_code == 200
