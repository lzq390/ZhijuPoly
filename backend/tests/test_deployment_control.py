from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Event, Thread
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
from starlette.requests import Request

from app import deployment_drain_middleware
from app.deployment_control_cli import _build_parser
from app.deployment_drain_middleware import DeploymentDrainMiddleware
from app.postgres_database import postgres_connection
from app.routers.deployment_status import (
    DeploymentMonomerMdCanaryContinuationRequest,
    DeploymentMonomerMdCanaryRequest,
    _require_direct_loopback,
    router as deployment_status_router,
)
from app.services.analytics_snapshot_store import load_analytics_snapshot, save_analytics_snapshot
from app.services.deployment_control import (
    InflightApiWriteTracker,
    aggregate_active_jobs,
    count_active_postgres_jobs,
    count_in_memory_jobs,
    disable_drain,
    enable_drain,
    get_drain_state,
)
from app.services.gpu_runtime_registry import GpuRuntimeRegistry


def _install_zero_activity_components(app: FastAPI) -> None:
    app.state.inflight_api_writes = InflightApiWriteTracker()
    app.state.conditional_generation_job_manager = SimpleNamespace(
        active_jobs=0,
        active_executions=0,
    )
    app.state.reverse_design_job_manager = SimpleNamespace(
        active_jobs=0,
        active_executions=0,
    )
    app.state.polytao_job_manager = SimpleNamespace(active_jobs=0, active_executions=0)
    app.state.gpu_runtime_registry = SimpleNamespace(
        active_inferences=0,
        waiting_inferences=0,
    )


def _drain_test_app(postgres_dsn: str) -> FastAPI:
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        app_postgres_dsn=postgres_dsn,
        deployment_drain_enabled=True,
    )
    app.state.postgres_connection_factory = postgres_connection
    app.state.deployment_control_connection_factory = postgres_connection
    _install_zero_activity_components(app)
    app.add_middleware(DeploymentDrainMiddleware)
    app.include_router(deployment_status_router)

    @app.get("/api/v1/value")
    def read_value() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/v1/value")
    def write_value() -> dict[str, bool]:
        return {"ok": True}

    return app


def _ensure_drain_disabled(connection) -> None:
    state = get_drain_state(connection)
    if not state.enabled:
        return
    assert state.activated_by is not None
    assert state.release_sha is not None
    disable_drain(
        connection,
        expected_activated_by=state.activated_by,
        expected_release_sha=state.release_sha,
    )


def _request_from(
    host: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/internal/deployment/monomer-md-canary/cleanup",
            "raw_path": b"/internal/deployment/monomer-md-canary/cleanup",
            "query_string": b"",
            "headers": headers or [],
            "client": (host, 12345),
            "server": ("127.0.0.1", 8000),
        }
    )


def test_monomer_md_canary_control_is_loopback_only_and_not_public() -> None:
    _require_direct_loopback(_request_from("127.0.0.1"))
    _require_direct_loopback(_request_from("::1"))

    with pytest.raises(HTTPException) as remote:
        _require_direct_loopback(_request_from("10.0.0.8"))
    assert remote.value.status_code == 403

    with pytest.raises(HTTPException) as forwarded:
        _require_direct_loopback(
            _request_from(
                "127.0.0.1",
                headers=[(b"x-forwarded-for", b"127.0.0.1")],
            )
        )
    assert forwarded.value.status_code == 403

    canary_paths = {
        route.path
        for route in deployment_status_router.routes
        if "monomer-md-canary" in route.path
    }
    assert canary_paths == {
        "/internal/deployment/monomer-md-canary/submit",
        "/internal/deployment/monomer-md-canary/validated",
        "/internal/deployment/monomer-md-canary/cleanup",
    }
    assert all(
        "{job_id}" not in path and not path.startswith("/api/")
        for path in canary_paths
    )
    identity = {
        "operation_id": "pull-deploy-canary-0001",
        "source_sha": "a" * 40,
        "expected_byteff2_commit": "b" * 40,
    }
    with pytest.raises(ValidationError):
        DeploymentMonomerMdCanaryRequest(
            **identity,
            job_id="arbitrary-business-row",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        DeploymentMonomerMdCanaryContinuationRequest(**identity)


def test_persistent_job_count_fails_closed_when_a_required_table_is_missing() -> None:
    class MissingTableConnection:
        def execute(self, _query, _parameters):
            return self

        def fetchone(self):
            return None

    with pytest.raises(RuntimeError, match="required deployment job table"):
        count_active_postgres_jobs(MissingTableConnection())


@pytest.mark.parametrize(
    "arguments",
    [
        ["drain", "--reason", "deploy", "--actor", "release-controller"],
        ["resume", "--actor", "release-controller"],
        [
            "drain",
            "--reason",
            "deploy",
            "--actor",
            "release-controller",
            "--release-sha",
            "short",
        ],
        [
            "resume",
            "--actor",
            "release-controller",
            "--release-sha",
            "A" * 40,
        ],
    ],
)
def test_deployment_control_cli_requires_full_release_sha(arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        _build_parser("postgresql://test").parse_args(arguments)


@pytest.mark.parametrize("command", ["drain", "resume"])
def test_deployment_control_cli_accepts_full_release_sha(command: str) -> None:
    arguments = [command, "--actor", "release-controller", "--release-sha", "a" * 40]
    if command == "drain":
        arguments.extend(["--reason", "deploy"])

    parsed = _build_parser("postgresql://test").parse_args(arguments)

    assert parsed.release_sha == "a" * 40


def test_drain_state_is_persistent_and_blocks_public_writes(postgres_dsn: str) -> None:
    with postgres_connection(postgres_dsn) as connection:
        _ensure_drain_disabled(connection)
        state = enable_drain(
            connection,
            reason="deploy release",
            activated_by="pytest",
            release_sha="a" * 40,
        )

    assert state.enabled is True
    assert state.release_sha == "a" * 40

    with TestClient(_drain_test_app(postgres_dsn)) as client:
        blocked = client.post("/api/v1/value")
        readable = client.get("/api/v1/value")
        deployment = client.get("/internal/deployment/status")

    assert blocked.status_code == 503
    assert blocked.headers["retry-after"] == "60"
    assert blocked.json() == {
        "detail": "service is temporarily read-only while a deployment is in progress",
        "reason": "deploy release",
    }
    assert readable.status_code == 200
    assert deployment.status_code == 200
    assert deployment.json()["drain"]["enabled"] is True
    assert deployment.json()["active_total"] == 0

    with postgres_connection(postgres_dsn) as connection:
        resumed = disable_drain(
            connection,
            expected_activated_by="pytest",
            expected_release_sha="a" * 40,
        )
        assert get_drain_state(connection) == resumed

    with TestClient(_drain_test_app(postgres_dsn)) as client:
        assert client.post("/api/v1/value").status_code == 200


def test_drain_rejects_invalid_sha_and_conflicting_owner(postgres_dsn: str) -> None:
    with postgres_connection(postgres_dsn) as connection:
        _ensure_drain_disabled(connection)
        with pytest.raises(ValueError, match="full lowercase 40-character"):
            enable_drain(
                connection,
                reason="deploy",
                activated_by="release-a",
                release_sha="short",
            )

        enable_drain(
            connection,
            reason="deploy",
            activated_by="release-a",
            release_sha="a" * 40,
        )
        with pytest.raises(RuntimeError, match="already owned by release-a"):
            enable_drain(
                connection,
                reason="deploy",
                activated_by="release-b",
                release_sha="b" * 40,
            )
        disable_drain(
            connection,
            expected_activated_by="release-a",
            expected_release_sha="a" * 40,
        )


def test_conditional_resume_requires_matching_owner_and_sha_and_is_idempotent(
    postgres_dsn: str,
) -> None:
    release_sha = "a" * 40
    with postgres_connection(postgres_dsn) as connection:
        _ensure_drain_disabled(connection)
        enable_drain(
            connection,
            reason="deploy",
            activated_by="release-controller",
            release_sha=release_sha,
        )

        with pytest.raises(RuntimeError, match="owned by release-controller"):
            disable_drain(
                connection,
                expected_activated_by="different-controller",
                expected_release_sha=release_sha,
            )
        assert get_drain_state(connection).enabled is True

        with pytest.raises(ValueError, match="full lowercase 40-character"):
            disable_drain(
                connection,
                expected_activated_by="release-controller",
                expected_release_sha="short",
            )
        assert get_drain_state(connection).enabled is True

        with pytest.raises(RuntimeError, match="owned by release-controller"):
            disable_drain(
                connection,
                expected_activated_by="release-controller",
                expected_release_sha="b" * 40,
            )
        assert get_drain_state(connection).enabled is True

        resumed = disable_drain(
            connection,
            expected_activated_by="release-controller",
            expected_release_sha=release_sha,
        )
        assert resumed.enabled is False

        repeated = disable_drain(
            connection,
            expected_activated_by="release-controller",
            expected_release_sha=release_sha,
        )
        assert repeated == resumed


def test_drain_middleware_fails_closed_when_control_state_is_unavailable(postgres_dsn: str) -> None:
    app = _drain_test_app(postgres_dsn)

    @contextmanager
    def unavailable_factory(_dsn: str):
        raise RuntimeError("database unavailable")
        yield

    app.state.deployment_control_connection_factory = unavailable_factory
    with TestClient(app) as client:
        response = client.post("/api/v1/value")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert response.json() == {"detail": "deployment safety state is unavailable"}


def test_drain_middleware_fails_closed_when_control_table_is_missing(postgres_dsn: str) -> None:
    app = _drain_test_app(postgres_dsn)

    class UndefinedTable(RuntimeError):
        sqlstate = "42P01"

    @contextmanager
    def pre_migration_factory(_dsn: str):
        raise UndefinedTable("deployment control is not migrated yet")
        yield

    app.state.deployment_control_connection_factory = pre_migration_factory
    with TestClient(app) as client:
        response = client.post("/api/v1/value")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert response.json() == {"detail": "deployment safety state is unavailable"}


def test_drain_state_read_is_offloaded_from_the_event_loop(postgres_dsn: str, monkeypatch) -> None:
    app = _drain_test_app(postgres_dsn)
    offloaded_calls: list[tuple[object, tuple[object, ...]]] = []

    async def fake_run_in_threadpool(function, *args):
        offloaded_calls.append((function, args))
        return SimpleNamespace(enabled=False, reason=None)

    monkeypatch.setattr(deployment_drain_middleware, "run_in_threadpool", fake_run_in_threadpool)
    with TestClient(app) as client:
        response = client.post("/api/v1/value")

    assert response.status_code == 200
    assert len(offloaded_calls) == 1
    assert offloaded_calls[0][0] is deployment_drain_middleware._read_drain_state


def test_streaming_write_remains_active_until_the_final_response_body() -> None:
    app = FastAPI()
    app.state.settings = SimpleNamespace(deployment_drain_enabled=False)
    _install_zero_activity_components(app)
    sent_messages: list[dict[str, object]] = []

    async def exercise() -> None:
        first_event_sent = asyncio.Event()
        allow_stream_to_finish = asyncio.Event()

        async def streaming_sse_app(scope, receive, send) -> None:
            del scope, receive
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"event: token\ndata: first\n\n",
                    "more_body": True,
                }
            )
            await allow_stream_to_finish.wait()
            await send(
                {
                    "type": "http.response.body",
                    "body": b"event: done\ndata: {}\n\n",
                    "more_body": False,
                }
            )

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            sent_messages.append(message)
            if (
                message["type"] == "http.response.body"
                and message.get("more_body") is True
            ):
                first_event_sent.set()

        middleware = DeploymentDrainMiddleware(streaming_sse_app)
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/assistant/chat/stream",
            "raw_path": b"/api/v1/assistant/chat/stream",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "root_path": "",
            "app": app,
        }

        request_task = asyncio.create_task(middleware(scope, receive, send))
        await asyncio.wait_for(first_event_sent.wait(), timeout=1)

        summary = count_in_memory_jobs(app)
        assert summary.counts["inflight_api_writes"] == 1
        assert summary.total == 1
        assert request_task.done() is False

        allow_stream_to_finish.set()
        await asyncio.wait_for(request_task, timeout=1)

    asyncio.run(exercise())

    assert sent_messages[-1]["type"] == "http.response.body"
    assert sent_messages[-1]["more_body"] is False
    assert count_in_memory_jobs(app).counts["inflight_api_writes"] == 0


def test_streaming_write_releases_active_count_when_client_disconnects() -> None:
    app = FastAPI()
    app.state.settings = SimpleNamespace(deployment_drain_enabled=False)
    _install_zero_activity_components(app)

    async def streaming_app(scope, receive, send) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send(
            {
                "type": "http.response.body",
                "body": b"partial",
                "more_body": True,
            }
        )

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    async def disconnected_send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body":
            raise OSError("client disconnected")

    middleware = DeploymentDrainMiddleware(streaming_app)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/assistant/chat/stream",
        "raw_path": b"/api/v1/assistant/chat/stream",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "root_path": "",
        "app": app,
    }

    async def exercise() -> None:
        with pytest.raises(OSError, match="client disconnected"):
            await middleware(scope, receive, disconnected_send)

    asyncio.run(exercise())

    assert count_in_memory_jobs(app).counts["inflight_api_writes"] == 0


def test_active_job_summary_covers_persistent_job_types(postgres_dsn: str) -> None:
    with postgres_connection(postgres_dsn) as connection:
        connection.execute(
            "INSERT INTO md.monomer_md_jobs (job_id, input_smiles, canonical_smiles) VALUES ('md-active', 'CC', 'CC')"
        )
        connection.execute(
            """
            INSERT INTO online_knowledge.jobs (job_id, status, material, mode, max_papers)
            VALUES ('online-active', 'running', 'polymer', 'synthesis', 5)
            """
        )
        summary = aggregate_active_jobs(connection)

    assert summary.counts == {"monomer_md": 1, "online_knowledge": 1}
    assert summary.total == 2


def test_active_job_summary_covers_in_memory_job_managers() -> None:
    manager = SimpleNamespace(active_jobs=2, active_executions=2)
    inflight_api_writes = InflightApiWriteTracker()
    inflight_api_writes.enter()
    app = SimpleNamespace(
        state=SimpleNamespace(
            conditional_generation_job_manager=manager,
            reverse_design_job_manager=manager,
            polytao_job_manager=SimpleNamespace(active_jobs=1, active_executions=2),
            gpu_runtime_registry=SimpleNamespace(
                active_inferences=3,
                waiting_inferences=4,
            ),
            inflight_api_writes=inflight_api_writes,
        )
    )

    summary = count_in_memory_jobs(app)

    assert summary.counts == {
        "inflight_api_writes": 1,
        "conditional_generation": 2,
        "reverse_design": 2,
        "polytao": 2,
        "gpu_inference": 3,
        "gpu_waiting": 4,
    }
    assert summary.total == 14
    inflight_api_writes.exit()


@pytest.mark.parametrize(
    "component_name",
    [
        "inflight_api_writes",
        "conditional_generation_job_manager",
        "reverse_design_job_manager",
        "polytao_job_manager",
        "gpu_runtime_registry",
    ],
)
def test_deployment_status_fails_closed_when_runtime_component_is_missing(
    postgres_dsn: str,
    component_name: str,
) -> None:
    with postgres_connection(postgres_dsn) as connection:
        _ensure_drain_disabled(connection)
    app = _drain_test_app(postgres_dsn)
    delattr(app.state, component_name)

    with TestClient(app) as client:
        response = client.get("/internal/deployment/status")

    assert response.status_code == 503
    assert response.json() == {"detail": "deployment state is unavailable"}


class _ExplodingManagerCounter:
    active_executions = 0

    @property
    def active_jobs(self) -> int:
        raise RuntimeError("counter lock failed")


class _ExplodingGpuCounter:
    waiting_inferences = 0

    @property
    def active_inferences(self) -> int:
        raise RuntimeError("GPU scheduler lock failed")


class _ExplodingInflightCounter:
    @property
    def active(self) -> int:
        raise RuntimeError("tracker lock failed")


@pytest.mark.parametrize(
    ("component_name", "invalid_component"),
    [
        (
            "conditional_generation_job_manager",
            SimpleNamespace(active_jobs=-1, active_executions=0),
        ),
        (
            "reverse_design_job_manager",
            SimpleNamespace(active_jobs="0", active_executions=0),
        ),
        (
            "polytao_job_manager",
            SimpleNamespace(active_jobs=0),
        ),
        ("conditional_generation_job_manager", _ExplodingManagerCounter()),
        (
            "gpu_runtime_registry",
            SimpleNamespace(active_inferences=0, waiting_inferences=-1),
        ),
        (
            "gpu_runtime_registry",
            SimpleNamespace(active_inferences=0),
        ),
        ("gpu_runtime_registry", _ExplodingGpuCounter()),
        ("inflight_api_writes", SimpleNamespace(active=True)),
        ("inflight_api_writes", _ExplodingInflightCounter()),
    ],
)
def test_deployment_status_fails_closed_when_runtime_counter_is_invalid(
    postgres_dsn: str,
    component_name: str,
    invalid_component: object,
) -> None:
    with postgres_connection(postgres_dsn) as connection:
        _ensure_drain_disabled(connection)
    app = _drain_test_app(postgres_dsn)
    setattr(app.state, component_name, invalid_component)

    with TestClient(app) as client:
        response = client.get("/internal/deployment/status")

    assert response.status_code == 503
    assert response.json() == {"detail": "deployment state is unavailable"}


def test_deployment_status_remains_available_when_all_gpu_features_are_disabled(
    postgres_dsn: str,
) -> None:
    with postgres_connection(postgres_dsn) as connection:
        _ensure_drain_disabled(connection)
    app = _drain_test_app(postgres_dsn)
    registry = GpuRuntimeRegistry()
    registry.register("disabled-runtime", enabled=False, loader=lambda: object())
    app.state.gpu_runtime_registry = registry

    with TestClient(app) as client:
        response = client.get("/internal/deployment/status")

    assert response.status_code == 200
    assert response.json()["active_jobs_schema_version"] == 1
    assert response.json()["active_jobs"] == {
        "monomer_md": 0,
        "online_knowledge": 0,
        "inflight_api_writes": 0,
        "conditional_generation": 0,
        "reverse_design": 0,
        "polytao": 0,
        "gpu_inference": 0,
        "gpu_waiting": 0,
    }
    assert response.json()["active_total"] == 0


def test_write_is_counted_before_drain_state_is_read(postgres_dsn: str) -> None:
    with postgres_connection(postgres_dsn) as connection:
        _ensure_drain_disabled(connection)

    app = _drain_test_app(postgres_dsn)
    drain_read_started = Event()
    allow_drain_read = Event()

    @contextmanager
    def pausing_factory(dsn: str):
        drain_read_started.set()
        assert allow_drain_read.wait(timeout=3)
        with postgres_connection(dsn) as connection:
            yield connection

    app.state.deployment_control_connection_factory = pausing_factory
    with TestClient(app) as client:
        responses = []
        request_thread = Thread(target=lambda: responses.append(client.post("/api/v1/value")))
        request_thread.start()
        assert drain_read_started.wait(timeout=3)

        summary = count_in_memory_jobs(app)
        assert summary.counts["inflight_api_writes"] == 1
        assert summary.total == 1

        with postgres_connection(postgres_dsn) as connection:
            enable_drain(
                connection,
                reason="race test",
                activated_by="pytest",
                release_sha="c" * 40,
            )
        allow_drain_read.set()
        request_thread.join(timeout=3)

    assert len(responses) == 1
    assert responses[0].status_code == 503
    assert count_in_memory_jobs(app).counts["inflight_api_writes"] == 0
    with postgres_connection(postgres_dsn) as connection:
        disable_drain(
            connection,
            expected_activated_by="pytest",
            expected_release_sha="c" * 40,
        )


def test_analytics_snapshot_round_trips_through_postgres_jsonb(postgres_dsn: str) -> None:
    generated_at = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    datasets = {
        "process": {"rows": 0},
        "property": {"rows": 42},
        "structureEffect": {"rows": 1},
        "propertyFilter": {"rows": 2},
        "dft": {"rows": 7},
        "formulation": {"rows": 3},
    }

    with postgres_connection(postgres_dsn) as connection:
        stored = save_analytics_snapshot(
            connection,
            datasets,
            generated_at=generated_at,
            source_sha="b" * 40,
        )
        loaded = load_analytics_snapshot(connection)
        connection.execute(
            "DELETE FROM governance.database_analytics_snapshots WHERE snapshot_key = 'database-browser'"
        )

    assert loaded == stored
    assert loaded is not None
    assert loaded.datasets == datasets
    assert loaded.generated_at == generated_at
    assert loaded.source_sha == "b" * 40
