"""Display-group filters share concrete modules without changing task identity."""
from datetime import timedelta
import re
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
import pytest

from app.auth.context import Identity, user_context
from app.routers.entitlements import router as entitlement_router
from app.services.entitlement_repository import EntitlementRepository
from app.services.usage_statistics import UsageStatistics
from app.usage_catalog import catalog_modules
from app.usage_groups import MODULE_GROUPS, UsageFilterError, module_groups
from test_usage_activity import activity_api
from test_usage_repository import usage_db, enabled, A, B, T0, ROOT
from test_usage_statistics import prepare


def test_groups_match_real_sidebar_order_labels_and_never_invent_unclassified_members():
    groups = module_groups()
    source = (ROOT / "frontend/src/App.tsx").read_text()
    sidebar = source[source.index("const standaloneModules:"):source.index("const isFullBleedModule =")]
    navigation = re.findall(
        r'id: "([^"]+)",\s+label: "([^"]+)",\s+'
        r'(?:placement: "[^"]+"(?: as const)?,\s+)?description:', sidebar)
    # Overview, the tool directory and server monitoring do not submit metered
    # research tasks. Keep them visible to this check without inventing groups.
    navigation_only = {
        ("overview", "系统总览"), ("researchTools", "实验工具"),
        ("serverMonitoring", "服务器节点"),
    }
    expected_groups = [(row["id"], row["label"]) for row in groups]
    assert set(navigation) - set(expected_groups) == navigation_only
    actual_tools = [row for row in navigation if row not in navigation_only]
    assert len(actual_tools) == 16
    assert expected_groups == actual_tools
    members = [module for group in groups for module in group["modules"]]
    assert len(members) == len(set(members))
    assert {item["id"] for item in catalog_modules()} - set(members) == {
        "assistant", "recording_summary", "database_browser", "openscience"}
    assert set(members) <= {item["id"] for item in catalog_modules()}
    assert {row[0] for row in MODULE_GROUPS} == {row["id"] for row in groups}
    assert next(row for row in groups if row["id"] == "mdSimulationDemo") == {
        "id": "mdSimulationDemo", "label": "MD 模拟", "modules": ["polymer_md", "md_demo"],
    }
    assert groups[-1] == {"id": "highThroughputWorkflowDemo", "label": "高通量优化演示", "modules": []}
    assert module_groups(include_external=False) == groups
    legacy = next(item for item in catalog_modules() if item["id"] == "database_browser")
    assert legacy["label"] == "历史数据库操作（未细分）" and "未保存页面来源" in legacy["note"]


@pytest.mark.parametrize("endpoint", ["summary", "records", "activity", "operations"])
@pytest.mark.parametrize("filters", [
    {"module_group": "unknown"}, {"module_group": ""},
    {"module_group": "mdSimulationDemo", "module": "polymer_md"},
])
def test_invalid_or_conflicting_groups_are_422_before_database(activity_api, monkeypatch, endpoint, filters):
    app, client = activity_api
    app.state.owner = A
    def forbidden(*args, **kwargs):
        pytest.fail("An invalid group must not reach a repository")
    app.state.entitlement_repository = SimpleNamespace(read_saved_operations=forbidden)
    app.include_router(entitlement_router)
    for method in ("summary", "records", "activity"):
        monkeypatch.setattr(UsageStatistics, method, forbidden)
    assert client.get("/api/v1/usage/" + endpoint, params=filters).status_code == 422


def test_group_metadata_is_additive_and_external_is_excluded_only_from_activity(activity_api, monkeypatch):
    app, client = activity_api
    app.state.owner = A
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "false")
    monkeypatch.setattr(UsageStatistics, "activity", lambda *args, **kwargs: {
        "mode": "activity_only", "items": [], "offset": 0, "limit": 25, "has_more": False,
    })
    app.state.entitlement_repository = SimpleNamespace(read_saved_operations=lambda **kwargs: [])
    app.include_router(entitlement_router)
    catalog = client.get("/api/v1/usage/catalog").json()
    activity = client.get("/api/v1/usage/activity").json()
    receipts = client.get("/api/v1/usage/operations").json()
    assert catalog["modules"] == catalog_modules()
    assert catalog["module_groups"] == receipts["module_groups"] == module_groups()
    assert activity["module_groups"] == module_groups(include_external=False)
    assert activity["mode"] == "activity_only" and receipts["has_more"] is False


def test_activity_group_filters_before_pagination_and_preserves_retry_owner_and_type(activity_api, usage_db):
    app, client = activity_api
    owner, other = str(uuid4()), str(uuid4())
    with psycopg.connect(usage_db) as connection:
        for i, module in enumerate(("monomer_md", "polymer_md", "md_demo")):
            at = T0 + timedelta(minutes=i)
            prepare(connection, module, owner=owner, at=at, submitted_at=at)
        prepare(connection, "polymer_md", owner=owner, attempt_id="2", status="completed",
                at=T0 + timedelta(hours=2), submitted_at=T0 + timedelta(minutes=1))
        prepare(connection, "polymer_md", owner=owner, status="failed", at=T0 + timedelta(hours=3))
        prepare(connection, "predict", owner=owner, at=T0 + timedelta(days=1))
        prepare(connection, "polymer_md", owner=other, at=T0 + timedelta(days=2))
        before = connection.execute("SELECT count(*) FROM usage.events").fetchone()[0]
    app.state.settings.app_postgres_dsn = make_conninfo(usage_db, options="-c role=nexpoly_api")
    app.state.owner = owner
    pages = [client.get("/api/v1/usage/activity", params={"module_group": "mdSimulationDemo", "limit": 1, "offset": i,
                                                        "owner_user_id": other}).json() for i in range(3)]
    rows = [row for page in pages for row in page["items"]]
    assert [row["module"] for row in rows] == ["md_demo", "polymer_md"]
    assert [page["has_more"] for page in pages] == [True, False, False]
    assert rows[1]["attempt_id"] == "2" and rows[1]["attempt_count"] == 2 and rows[1]["status"] == "completed"
    exact = client.get("/api/v1/usage/activity", params={"module": "polymer_md"}).json()
    assert [row["task_id"] for row in exact["items"]] == ["polymer_md:root"]
    app.state.owner = other
    other_rows = client.get("/api/v1/usage/activity", params={"module_group": "mdSimulationDemo"}).json()["items"]
    assert len(other_rows) == 1 and other_rows[0]["attempt_count"] == 1
    with psycopg.connect(usage_db) as connection:
        assert connection.execute("SELECT count(*) FROM usage.events").fetchone()[0] == before


def test_summary_and_records_group_keep_zero_unknown_and_submission_cohorts(activity_api, usage_db):
    app, client = activity_api
    owner = str(uuid4())
    boundary = T0 + timedelta(hours=16)
    with psycopg.connect(usage_db) as connection:
        prepare(connection, "knowledge", owner=owner, at=boundary - timedelta(seconds=1), metrics={"execution_ms": 0})
        prepare(connection, "online_knowledge", owner=owner, at=boundary, metrics={})
        prepare(connection, "knowledge", owner=B, at=boundary)
        prepare(connection, "assistant", owner=owner, at=boundary, metrics={"execution_ms": 999})
    app.state.settings.app_postgres_dsn = make_conninfo(usage_db, options="-c role=nexpoly_api")
    app.state.owner = owner
    params = {"start": T0.isoformat(), "end": (T0 + timedelta(days=1)).isoformat(), "module_group": "knowledge"}
    response = client.get("/api/v1/usage/summary", params={**params, "timezone": "Asia/Shanghai"})
    assert response.status_code == 200
    result = response.json()
    assert result["totals"]["tasks"] == 2
    assert {row["module"] for row in result["modules"]} == {"knowledge", "online_knowledge"}
    assert result["totals"]["metrics"]["execution_ms"] == {"value": 0, "known_count": 1, "unknown_count": 1}
    assert result["totals"]["metrics"]["artifact_bytes"] == {"value": None, "known_count": 0, "unknown_count": 2}
    assert [row["date"] for row in result["daily"]] == ["2026-01-01", "2026-01-02"]
    pages = [client.get("/api/v1/usage/records", params={**params, "page": page, "page_size": 1}).json() for page in (1, 2)]
    assert [page["items"][0]["module"] for page in pages] == ["online_knowledge", "knowledge"]
    assert all(page["total"] == 2 for page in pages)
    exact_params = {key: value for key, value in params.items() if key != "module_group"}
    assert client.get("/api/v1/usage/records", params={**exact_params, "module": "knowledge"}).json()["total"] == 1
    empty = client.get("/api/v1/usage/summary", params={**params, "module_group": "highThroughputWorkflowDemo"}).json()
    assert empty["totals"]["tasks"] == 0 and empty["totals"]["metrics"]["execution_ms"]["value"] is None


def test_direct_repositories_validate_group_filters_before_connecting():
    with user_context(Identity(A)):
        service = UsageStatistics("never-connect")
        calls = [lambda **filters: service.summary(start=T0, end=T0 + timedelta(days=1), **filters),
                 lambda **filters: service.records(start=T0, end=T0 + timedelta(days=1), **filters),
                 service.activity,
                 lambda **filters: EntitlementRepository("never-connect").read_saved_operations(owner_user_id=A, **filters)]
        for call in calls:
            for filters in ({"module_group": "unknown"}, {"module": "knowledge", "module_group": "knowledge"}):
                with pytest.raises(UsageFilterError):
                    call(**filters)


@pytest.mark.parametrize("group,expected", [
    ("mdSimulationDemo", {"polymer_md", "md_demo"}),
    ("monomerMdSimulation", {"monomer_md"}),
    ("monomerDft", {"monomer_dft"}),
    ("database", {"database_analysis", "dft_history"}),
    ("databaseQuery", {"database_query"}),
    ("databaseFilter", {"database_filter"}),
    ("polytaoGeneration", {"polytao"}),
    ("conditionalGeneration", {"conditional_generation"}),
    ("highThroughputWorkflowDemo", set()),
])
def test_sidebar_scientific_and_database_groups_are_disjoint_and_legacy_stays_in_all(usage_db, group, expected):
    modules = {"polymer_md", "md_demo", "monomer_md", "monomer_dft", "dft_history",
               "database_analysis", "database_query", "database_filter", "database_browser",
               "polytao", "conditional_generation", "assistant", "recording_summary", "openscience"}
    owner = str(uuid4())
    with psycopg.connect(usage_db) as connection:
        for module in modules:
            prepare(connection, module, owner=owner)
    with user_context(Identity(owner)):
        service = UsageStatistics(make_conninfo(usage_db, options="-c role=nexpoly_api"))
        activity = service.activity(module_group=group)
        records = service.records(start=T0, end=T0 + timedelta(days=1), module_group=group)
        summary = service.summary(start=T0, end=T0 + timedelta(days=1), module_group=group)
        assert {row["module"] for row in activity["items"]} == expected
        assert activity["has_more"] is False
        assert {row["module"] for row in records["items"]} == expected and records["total"] == len(expected)
        assert {row["module"] for row in summary["modules"]} == expected and summary["totals"]["tasks"] == len(expected)
        assert summary["totals"]["metrics"]["execution_ms"]["value"] is None
        assert {row["module"] for row in service.activity()["items"]} == modules
        assert len(service.activity(module="database_browser")["items"]) == 1
