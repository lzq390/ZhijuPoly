import json

import httpx
import pytest

from workers.polymer_md_worker.app import start_authorization
from test_manager import request


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_authorization_rebind_receipt_requires_exact_execution_identity(monkeypatch):
    payload = request()
    seen = []
    wrong_identity = False

    def handle(incoming):
        assert incoming.headers["authorization"] == "Bearer private-token"
        assert incoming.url.path == f"/internal/polymer-md/jobs/{payload.job_id}/authorize-start"
        value = json.loads(incoming.content)
        seen.append(value)
        receipt = {key: value[key] for key in ("protocol_version", "job_id", "attempt_token", "request_sha256", "enqueue_sequence", "worker_instance_id")}
        receipt.update(authorized=True, start_authorized_at="2026-10-15T00:00:00+00:00")
        if wrong_identity:
            receipt["worker_instance_id"] = "f" * 32
        return httpx.Response(200, json=receipt)

    original = httpx.AsyncClient
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(start_authorization.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    authorizer = start_authorization.StartAuthorizer("http://backend/internal/polymer-md", "private-token")
    result = await authorizer.authorize(payload, "b" * 32,
        replaces_worker_instance_id="a" * 32, prior_execution_cleanup_confirmed=True)
    assert result["authorized"] is True
    assert seen[-1]["replaces_worker_instance_id"] == "a" * 32
    assert seen[-1]["prior_execution_cleanup_confirmed"] is True
    wrong_identity = True
    with pytest.raises(RuntimeError, match="identity_mismatch"):
        await authorizer.authorize(payload, "b" * 32)
