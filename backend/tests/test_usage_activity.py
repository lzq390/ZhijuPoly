"""Independent activity reads reuse observations without quota or worker writes."""
from datetime import timedelta
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import psycopg
from psycopg.conninfo import make_conninfo
import pytest

from app.auth.context import Identity, user_context
from app.auth.policy import route_policy
from app.entitlement_middleware import EntitlementAccessMiddleware
from app.routers.usage import router
from app.services.usage_statistics import UsageStatistics
from app.usage_catalog import catalog_modules, match_usage_route
from task_usage.ledger import METRICS, STATUSES, record_event
from test_usage_repository import usage_db, enabled, event, A, B, T0, ROOT
from test_usage_statistics import prepare, unique_module


@pytest.fixture
def activity_api():
    app = FastAPI()
    app.state.settings = SimpleNamespace(app_postgres_dsn="never-connect")
    app.state.owner = None

    @app.middleware("http")
    async def trusted_identity(request, call_next):
        if app.state.owner:
            with user_context(Identity(app.state.owner)):
                return await call_next(request)
        return await call_next(request)

    app.include_router(router)
    with TestClient(app) as client:
        yield app, client


def test_activity_requires_owner_and_enabled_metering_before_query(activity_api, monkeypatch):
    app, client = activity_api

    def forbidden(*args, **kwargs):
        raise AssertionError("Disabled or unauthenticated activity must not query")

    monkeypatch.setattr(UsageStatistics, "activity", forbidden)
    monkeypatch.setenv("USAGE_METERING_ENABLED", "false")
    assert client.get("/api/v1/usage/activity").status_code == 401
    app.state.owner = A
    response = client.get("/api/v1/usage/activity")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "usage_metering_disabled"


@pytest.mark.parametrize("params", [
    {"offset": -1}, {"offset": 100001}, {"limit": 0}, {"limit": 101},
    {"offset": "invalid"}, {"module": "bad.module"}, {"module": ""}, {"module": "a" * 65},
])
def test_activity_rejects_invalid_filters_without_database(activity_api, params):
    app, client = activity_api
    app.state.owner = A
    assert client.get("/api/v1/usage/activity", params=params).status_code == 422


@pytest.mark.parametrize("exception", [RuntimeError, ValueError])
def test_activity_storage_error_does_not_expose_exception(activity_api, monkeypatch, caplog, exception):
    app, client = activity_api
    app.state.owner = A

    def fail(*args, **kwargs):
        raise exception("password=synthetic-must-not-leak")

    monkeypatch.setattr(UsageStatistics, "activity", fail)
    response = client.get("/api/v1/usage/activity")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "usage_metering_unavailable"
    assert "synthetic-must-not-leak" not in response.text + caplog.text
    assert "usage_activity_unavailable" in caplog.text


def test_activity_catalog_and_warnings_are_independent_of_entitlement_flag(activity_api, monkeypatch):
    app, client = activity_api
    app.state.owner = A
    app.state.usage_collector = SimpleNamespace(snapshot=lambda: {"degraded": True})
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "false")
    monkeypatch.setattr(UsageStatistics, "activity", lambda *args, **kwargs: {
        "mode": "activity_only", "items": [], "offset": 0, "limit": 25, "has_more": False,
    })
    response = client.get("/api/v1/usage/activity")
    assert response.status_code == 200
    result = response.json()
    assert result["mode"] == "activity_only"
    assert result["modules"] == [{"id": row["id"], "label": row["label"]}
                                 for row in catalog_modules() if row["coverage"] != "external"]
    assert any(row["id"] == "polymer_md" for row in result["modules"])
    warnings = " ".join(result["warnings"])
    assert "不作为计次" in warnings and "历史未采集任务不会补记" in warnings
    assert "不保证进程已终止" in warnings and "当前数据可能不完整" in warnings
    assert "不保存完整结果" in warnings


@pytest.mark.parametrize("entitlements_enabled", ["true", "false"])
def test_activity_preserves_free_account_history_access_without_quota_admission(monkeypatch, entitlements_enabled):
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", entitlements_enabled)
    monkeypatch.setattr(UsageStatistics, "activity", lambda *args, **kwargs: {
        "mode": "activity_only", "items": [], "offset": 0, "limit": 25, "has_more": False,
    })
    app = FastAPI()
    app.state.settings = SimpleNamespace(app_postgres_dsn="never-connect")
    # Any reserve, request observation, or settle call would fail: this repository
    # only implements the existing account access check.
    repository = SimpleNamespace(resolve_access_tier=lambda **kwargs: "free")
    app.add_middleware(EntitlementAccessMiddleware, repository=repository,
                       policy={"request_rate_per_minute": 1})
    app.include_router(router)
    with user_context(Identity(A)), TestClient(app) as client:
        for _ in range(2):
            assert client.get("/api/v1/usage/activity").status_code == 200


def test_activity_numeric_retry_uses_latest_attempt_with_original_cohort(usage_db):
    module = unique_module()
    with psycopg.connect(usage_db) as connection:
        first = prepare(connection, module, attempt_id="2", submitted_at=T0)
        prepare(connection, module, attempt_id="10", status="running", at=T0 + timedelta(hours=1),
                submitted_at=T0 + timedelta(hours=1), started_at=T0 + timedelta(hours=1),
                metrics={"queue_ms": 0})
        record_event(connection, **{**first, "event_key": uuid4().hex, "status": "failed",
                                   "occurred_at": T0 + timedelta(hours=2), "finished_at": T0 + timedelta(minutes=5)})
        for kind in ("task", "execution", "provider"):
            prepare(connection, module, task="child_" + kind, kind=kind, parent_task_id=module + ":root",
                    root_task_id=module + ":root", status="completed", at=T0 + timedelta(hours=3),
                    metrics={"execution_ms": 900, "provider_total_tokens": 500})
    with user_context(Identity(A)):
        result = UsageStatistics(usage_db).activity(module=module)
    assert len(result["items"]) == 1
    row = result["items"][0]
    assert row["status"] == "running" and row["attempt_id"] == "10" and row["attempt_count"] == 2
    assert row["submitted_at"] == T0
    assert row["updated_at"] == row["started_at"] == T0 + timedelta(hours=1)
    assert row["finished_at"] is None and row["parent_task_id"] is None
    assert row["metrics"] == {name: 0 if name == "queue_ms" else None for name in METRICS}
    assert result["has_more"] is False


def test_activity_opaque_retry_late_old_report_does_not_promote_attempt(usage_db):
    module = unique_module()
    with psycopg.connect(usage_db) as connection:
        first = prepare(connection, module, attempt_id="instance:old:1")
        prepare(connection, module, attempt_id="instance:new:1", status="completed", at=T0 + timedelta(minutes=2))
        record_event(connection, **{**first, "event_key": uuid4().hex, "status": "failed",
                                   "occurred_at": T0 + timedelta(minutes=3)})
    with user_context(Identity(A)):
        row = UsageStatistics(usage_db).activity(module=module)["items"][0]
    assert row["attempt_id"] == "instance:new:1" and row["status"] == "completed"
    assert row["attempt_count"] == 2 and row["submitted_at"] == T0
    assert row["updated_at"] == T0 + timedelta(minutes=2)


def test_activity_keeps_same_task_reference_in_different_modules_separate(usage_db):
    modules = [unique_module(), unique_module()]
    task = "request:" + uuid4().hex
    with psycopg.connect(usage_db) as connection:
        for i, module in enumerate(modules):
            record_event(connection, **event(module=module, task_id=task, attempt_id=str(i + 1),
                                            status="completed" if i else "failed"))
    with user_context(Identity(A)):
        rows = [UsageStatistics(usage_db).activity(module=module)["items"][0] for module in modules]
    assert [row["status"] for row in rows] == ["failed", "completed"]
    assert all(row["attempt_count"] == 1 and row["task_id"] == task for row in rows)


def test_activity_pages_all_retained_root_states_without_date_filter(usage_db):
    module = unique_module()
    statuses = sorted(STATUSES)
    with psycopg.connect(usage_db) as connection:
        for i, status in enumerate(statuses):
            prepare(connection, module, task=str(i), status=status)
    with user_context(Identity(A)):
        service = UsageStatistics(usage_db)
        pages = [service.activity(module=module, offset=offset, limit=3) for offset in (0, 3, 6)]
        assert service.activity(module=module, offset=100000, limit=100)["items"] == []
        assert service.activity(module=unique_module())["items"] == []
    rows = [row for page in pages for row in page["items"]]
    assert [row["task_id"] for row in rows] == [module + ":" + str(i) for i in range(len(statuses))]
    assert {row["status"] for row in rows} == STATUSES
    assert [page["has_more"] for page in pages] == [True, True, False]
    assert [page["offset"] for page in pages] == [0, 3, 6]
    assert all(page["limit"] == 3 and "total" not in page for page in pages)


def test_activity_owner_scope_rls_and_no_observation_mutation(activity_api, usage_db):
    app, client = activity_api
    module = unique_module()
    with psycopg.connect(usage_db) as connection:
        prepare(connection, module, owner=A, task="mine")
        prepare(connection, module, owner=B, task="other")
        before = connection.execute("SELECT count(*) FROM usage.events WHERE module=%s", (module,)).fetchone()[0]
    # Run the request's actual connection as the least-privilege API role.
    app.state.settings.app_postgres_dsn = make_conninfo(usage_db, options="-c role=nexpoly_api")
    app.state.owner = A
    response = client.get("/api/v1/usage/activity", params={"module": module, "owner_user_id": B,
                                                           "start": "bad", "end": "bad"})
    assert response.status_code == 200
    assert [row["task_id"] for row in response.json()["items"]] == [module + ":mine"]
    app.state.owner = B
    assert [row["task_id"] for row in client.get("/api/v1/usage/activity", params={"module": module}).json()["items"]] == [module + ":other"]
    with psycopg.connect(usage_db) as connection:
        assert connection.execute("SELECT count(*) FROM usage.events WHERE module=%s", (module,)).fetchone()[0] == before


def test_activity_service_requires_identity_and_valid_pagination():
    with pytest.raises(HTTPException) as error:
        UsageStatistics("never-connect").activity()
    assert error.value.status_code == 401
    with user_context(Identity(A)):
        for params in ({"offset": -1}, {"offset": 100001}, {"limit": 0}, {"limit": 101}, {"module": "bad.module"}):
            with pytest.raises(ValueError):
                UsageStatistics("never-connect").activity(**params)


def test_activity_route_is_in_private_inventory_and_does_not_meter_its_own_read():
    manifest = json.loads((ROOT / "contracts/multiuser_routes.json").read_text())
    matches = [row for row in manifest["routes"] if row["path"] == "/api/v1/usage/activity"]
    assert len(matches) == 1 and matches[0]["method"] == "GET" and matches[0]["permission"] == "member"
    assert route_policy("/api/v1/usage/activity", "GET") == "member"
    matched = match_usage_route("GET", "/api/v1/usage/activity")
    assert matched.mode == "observe" and matched.module is None
    assert any(route.path == "/api/v1/usage/activity" and route.methods == {"GET"} for route in router.routes)
