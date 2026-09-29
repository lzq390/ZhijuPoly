"""Factories for existing full-app contracts using real disposable identities."""
from __future__ import annotations

from copy import copy

import pytest

from app.main import create_app
from test_auth_isolation import auth_database
from test_private_http_support import authenticated_client


@pytest.fixture
def private_app(auth_database):
    def create(settings):
        # Build every repository with the restricted application role. Merely
        # changing app.state.auth after constructing an admin-backed app would
        # leave the business connection configuration outside this contract.
        settings = copy(settings)
        for key, value in {
            "app_postgres_dsn": auth_database["api"],
            "pi_postgres_dsn": auth_database["api"],
            "lab_data_postgres_dsn": auth_database["api"],
            "allowed_origins": "http://testserver",
            "gpu_broker_enabled": False,
            "gpu_preload_mode": "lazy",
        }.items():
            setattr(settings, key, value)
        return create_app(settings)
    return create


@pytest.fixture
def private_client(auth_database):
    clients = []
    def create(app, **options):
        client = authenticated_client(app, auth_database, **options)
        clients.append(client)
        return client
    try:
        yield create
    finally:
        for client in clients:
            client.close()
