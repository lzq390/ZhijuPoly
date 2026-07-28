from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.routers.dev_gpu_session import router
from app.services.dev_gpu_operator import DevGpuOperatorError


SOURCE_SHA = "a" * 40
SOURCE_TREE = "b" * 40


def _status(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "operator_available": True,
        "phase": "stopped",
        "controller_status": "stopped",
        "can_recover": True,
        "operation_id": None,
        "message": "GPU 服务未启动",
        "source_sha": SOURCE_SHA,
        "source_tree": SOURCE_TREE,
        "updated_at": "2026-07-27T00:00:00Z",
    }
    value.update(overrides)
    return value


class FakeOperatorClient:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def request(self, command: str) -> dict[str, object]:
        self.commands.append(command)
        if command == "recover":
            return _status(
                phase="starting",
                controller_status="stopped",
                can_recover=False,
                operation_id="c" * 32,
                message="正在启动 GPU1 相关服务",
            )
        return _status()


def _router_app(client: object | None) -> FastAPI:
    app = FastAPI()
    app.state.dev_gpu_operator_client = client
    app.include_router(router)
    return app


def test_status_and_recover_delegate_to_one_operator_client() -> None:
    operator = FakeOperatorClient()
    client = TestClient(_router_app(operator))

    status_response = client.get("/api/v1/dev-gpu-session/status")
    recover_response = client.post("/api/v1/dev-gpu-session/recover")

    assert status_response.status_code == 200
    assert status_response.json()["phase"] == "stopped"
    assert recover_response.status_code == 202
    assert recover_response.json()["phase"] == "starting"
    assert operator.commands == ["status", "recover"]


def test_status_degrades_but_recover_fails_closed_when_operator_is_unavailable() -> None:
    class UnavailableOperator:
        def request(self, _command: str) -> dict[str, object]:
            raise DevGpuOperatorError("socket unavailable")

    client = TestClient(_router_app(UnavailableOperator()))

    status_response = client.get("/api/v1/dev-gpu-session/status")
    recover_response = client.post("/api/v1/dev-gpu-session/recover")

    assert status_response.status_code == 200
    assert status_response.json()["phase"] == "unavailable"
    assert status_response.json()["operator_available"] is False
    assert recover_response.status_code == 503


def test_router_is_absent_unless_the_backend_setting_is_enabled() -> None:
    disabled = create_app(Settings(dev_gpu_operator_enabled=False))
    enabled = create_app(
        Settings(
            dev_gpu_operator_enabled=True,
            dev_gpu_operator_frontend_port=9001,
        )
    )

    disabled_paths = {route.path for route in disabled.routes}
    enabled_paths = {route.path for route in enabled.routes}

    assert "/api/v1/dev-gpu-session/status" not in disabled_paths
    assert "/api/v1/dev-gpu-session/recover" not in disabled_paths
    assert "/api/v1/dev-gpu-session/status" in enabled_paths
    assert "/api/v1/dev-gpu-session/recover" in enabled_paths


def test_enabled_backend_rejects_any_frontend_port_other_than_9001() -> None:
    try:
        Settings(
            dev_gpu_operator_enabled=True,
            dev_gpu_operator_frontend_port=9000,
        )
    except ValueError as exc:
        assert "exactly 9001" in str(exc)
    else:
        raise AssertionError("GPU operator accepted a non-9001 frontend port")
