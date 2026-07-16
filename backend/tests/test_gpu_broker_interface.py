from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.main import _acquire_backend_gpu_residency
from app.services.monomer_md_protocols import validate_formal_config


class _ManagedLease:
    def __init__(self, gpu_uuid: str, *, gpu_index: int = 2) -> None:
        self.lease = SimpleNamespace(
            lease_id="lease-1",
            fencing_token=42,
            broker_instance_id="broker-1",
            kind="residency",
            placement="preferred",
            component="backend",
            environment="prod",
            client_id="backend-prod",
            gpu_index=gpu_index,
            gpu_uuid=gpu_uuid,
            memory_mib=8192,
            thread_percent=100,
            preferred=True,
            parent_lease_id=None,
            status="active",
            workload_pid=1234,
            workload_process_start_ticks=5678,
            workload_process_group_id=1234,
            workload_cgroup="0::/nexpoly-backend",
        )
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Client:
    def __init__(self, managed: _ManagedLease) -> None:
        self.managed = managed
        self.calls: list[dict[str, object]] = []

    def acquire_managed(self, **kwargs):
        self.calls.append(kwargs)
        return self.managed


def test_gpu_broker_is_default_off_with_fixed_safe_interface(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("app.config.DEFAULT_ENV_FILE", tmp_path / "missing.env")
    for name in (
        "GPU_BROKER_ENABLED",
        "GPU_BROKER_ENVIRONMENT",
        "GPU_BROKER_SOCKET_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()
    assert settings.gpu_broker_enabled is False
    assert settings.gpu_broker_environment == "dev"

    managed = _ManagedLease("GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe")
    client = _Client(managed)
    monkeypatch.setattr("app.main.GpuBrokerClient", lambda _path: client)
    expected_mps_environment = {
        "CUDA_VISIBLE_DEVICES": managed.lease.gpu_uuid,
        "CUDA_MPS_PIPE_DIRECTORY": "/private/mps-2/pipe",
        "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": "100",
        "CUDA_MPS_CLIENT_PRIORITY": "0",
        "CUDA_MPS_PINNED_DEVICE_MEM_LIMIT": f"{managed.lease.gpu_uuid}=8192M",
    }
    monkeypatch.setattr(
        "app.main.mps_client_environment",
        lambda _lease, *, pipe_root: expected_mps_environment,
    )
    for name in expected_mps_environment:
        monkeypatch.setenv(name, "before-test")
    production = Settings(
        gpu_broker_enabled=True,
        gpu_broker_environment="prod",
        gpu_broker_socket_path="/private/broker.sock",
        gpu_mps_pipe_root="/private",
    )

    assert _acquire_backend_gpu_residency(production) is managed
    assert client.calls == [
        {
            "kind": "residency",
            "placement": "preferred",
            "component": "backend",
            "environment": "prod",
            "client_id": "backend-prod",
            "memory_mib": 8192,
            "thread_percent": 100,
            "wait_timeout_seconds": 45.0,
            "heartbeat_interval_seconds": 5.0,
            "request_id": "backend:prod:residency",
        }
    ]
    assert {
        name: os.environ[name] for name in expected_mps_environment
    } == expected_mps_environment


def test_backend_releases_policy_ineligible_uuid(monkeypatch) -> None:
    managed = _ManagedLease("GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5")
    client = _Client(managed)
    monkeypatch.setattr("app.main.GpuBrokerClient", lambda _path: client)
    settings = Settings(gpu_broker_enabled=True, gpu_broker_environment="prod")

    with pytest.raises(RuntimeError, match="invalid Backend residency lease metadata"):
        _acquire_backend_gpu_residency(settings)
    assert managed.closed is True


def test_backend_rejects_formal_natoms_above_10000() -> None:
    config = {
        "protocol": "Density",
        "temperature": 298,
        "natoms": 10_001,
        "components": {"DMC": 1},
        "smiles": {"DMC": "COC(=O)OC"},
    }
    with pytest.raises(ValueError, match="natoms must be <= 10000"):
        validate_formal_config(config, "Density")
