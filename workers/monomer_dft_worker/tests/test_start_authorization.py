from __future__ import annotations

import io
import json
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from workers.monomer_dft_worker.app.engine import ComputationCancelled
from workers.monomer_dft_worker.app.start_authorization import StartAuthorizer


def test_ambiguous_response_retries_same_attempt(monkeypatch):
    calls = []

    def send(request, *, timeout):
        calls.append((request.full_url, request.data))
        if len(calls) == 1:
            raise URLError("response lost")
        return io.BytesIO(json.dumps({"authorized": True, "start_authorization_version": 1}).encode())

    monkeypatch.setattr("workers.monomer_dft_worker.app.start_authorization._open", send)
    StartAuthorizer(base_url="http://127.0.0.1:8000", token="test-only")(
        SimpleNamespace(job_id="task", attempt_token="a" * 64, request_sha256="b" * 64, enqueue_sequence=1)
    )
    assert len(calls) == 2
    assert calls[0] == calls[1]


def test_denial_and_unconfigured_fail_closed(monkeypatch):
    monkeypatch.setattr("workers.monomer_dft_worker.app.start_authorization._open", lambda *a, **k: io.BytesIO(b'{"authorized": false, "start_authorization_version": 1}'))
    snapshot = SimpleNamespace(job_id="task", attempt_token="a" * 64, request_sha256="b" * 64, enqueue_sequence=1)
    with pytest.raises(ComputationCancelled):
        StartAuthorizer(base_url="http://localhost:8000", token="test-only")(snapshot)
    with pytest.raises(RuntimeError):
        StartAuthorizer(base_url="", token="")(snapshot)
    with pytest.raises(ValueError):
        StartAuthorizer(base_url="http://external.example", token="test-only")
