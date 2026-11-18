from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx2 as httpx
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from app.polytao_mcp import API, create_app
from app.polytao_mcp_contract import MODEL_TOOL_NAMES, UI_TOOL_NAMES
from conftest import descriptor_values, job_response, session_response


def backend(config, requests):
    async def handler(request):
        requests.append(request)
        if request.url.path == "/api/v1/auth/session":
            return httpx.Response(200, json=session_response(config))
        if request.url.path == API + "/status":
            return httpx.Response(200, json={"enabled": True, "available": True,
                "worker_base_url_configured": False, "message": "ready", "runtime_ready": True})
        if request.url.path == API + "/descriptors":
            return httpx.Response(200, json={"input_smiles": "*CC*", "canonical_smiles": "*CC*", "prompt": "private",
                "descriptors": [{"name": key, "value": value} for key, value in descriptor_values().items()], "query_time_ms": 1})
        if request.url.path == API + "/jobs":
            return httpx.Response(202, json={"job_id": "polytao.instance.token", "status": "pending"})
        return httpx.Response(200, json=job_response("polytao.instance.token"))
    return httpx.MockTransport(handler)


@pytest.mark.anyio
async def test_real_sdk_protocol_discovery_submit_query_and_output_schema(configuration):
    requests = []
    app = create_app(configuration, transport=backend(configuration, requests))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                headers={"Authorization": "Bearer test-token"}) as http:
            @asynccontextmanager
            async def transport():
                async with streamable_http_client("http://127.0.0.1:18111/mcp", http_client=http) as (read, write):
                    yield read, write
            async with Client(transport(), read_timeout_seconds=10) as client:
                listed = await client.list_tools()
                assert {tool.name for tool in listed.tools} == MODEL_TOOL_NAMES
                for tool in listed.tools:
                    assert tool.input_schema and tool.output_schema
                    assert tool.annotations.read_only_hint == (tool.name != "prepare_polytao_generation")
                caps = await client.call_tool("get_polytao_capabilities", {})
                assert caps.structured_content["submission_ready"]
                extracted = await client.call_tool("calculate_polytao_descriptors", {"smiles": "*CC*"})
                assert len(extracted.structured_content["descriptors"]) == 15
                prepared = await client.call_tool("prepare_polytao_generation", {"conversation_id": "ses_protocol", "descriptors": descriptor_values(), "candidate_count": 3})
                assert not prepared.is_error and prepared.structured_content["ready"]
                assert not any(request.url.path == API + "/jobs" for request in requests)
                args = {"draft_id": prepared.structured_content["draft_id"], "conversation_id": "ses_protocol", "revision": 1}
                denied = await client.call_tool("confirm_polytao_generation", args)
                assert denied.is_error and denied.structured_content["error"]["code"] == "PERMISSION_DENIED"
                raw = await client.call_tool("submit_polytao_generation", {"descriptors": descriptor_values(), "idempotency_key": "sdk"})
                assert raw.is_error and raw.structured_content["error"]["code"] == "UNKNOWN_TOOL"
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers={"Authorization": "Bearer ui-token"}) as ui_http:
                    @asynccontextmanager
                    async def ui_transport():
                        async with streamable_http_client("http://127.0.0.1:18111/mcp", http_client=ui_http) as (read, write):
                            yield read, write
                    async with Client(ui_transport(), read_timeout_seconds=10) as ui:
                        assert {tool.name for tool in (await ui.list_tools()).tools} == UI_TOOL_NAMES
                        submitted = await ui.call_tool("confirm_polytao_generation", args)
                        assert not submitted.is_error and submitted.structured_content["status"] == "submitted"
                        replayed = await ui.call_tool("confirm_polytao_generation", args)
                        assert replayed.structured_content["submission"]["replayed"]
                queried = await client.call_tool("get_polytao_job", {"job_id": submitted.structured_content["job_id"]})
                assert not queried.is_error and queried.structured_content["returned_count"] == 2
                invalid = await client.call_tool("prepare_polytao_generation", {"conversation_id": "ses_protocol", "candidate_count": "3"})
                assert invalid.is_error and invalid.structured_content["error"]["code"] == "INVALID_ARGUMENT"
                # Hot revocation affects existing SDK connections.
                configuration.clients_file.write_text('{"clients":[]}')
                response = await http.post("http://127.0.0.1:18111/mcp", json={"jsonrpc": "2.0", "id": 99, "method": "tools/list"})
                assert response.status_code == 401
    assert len([request for request in requests if request.method == "POST" and request.url.path.endswith("/jobs")]) == 1


@pytest.mark.anyio
async def test_http_guard_hosts_origins_tokens_query_and_body_bound(configuration):
    app = create_app(configuration, transport=backend(configuration, []))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:18111") as http:
            assert (await http.get("/healthz")).status_code == 200
            assert (await http.post("/mcp")).status_code == 401
            headers = {"Authorization": "Bearer test-token", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            assert (await http.post("/mcp", headers={**headers, "Host": "evil"})).status_code == 421
            assert (await http.post("/mcp", headers={**headers, "Origin": "http://evil"})).status_code == 403
            assert (await http.post("/mcp?token=leak", headers=headers)).status_code == 400
            too_big = await http.post("/mcp", headers=headers, content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "padding": "x" * 32768}))
            assert too_big.status_code == 413
            credentials = json.loads(configuration.clients_file.read_text())
            credentials["clients"][0]["tools"] = ["get_polytao_capabilities"]
            configuration.clients_file.write_text(json.dumps(credentials))
            listed = await http.post("/mcp", headers={**headers, "MCP-Protocol-Version": "2025-11-25"},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            assert [tool["name"] for tool in listed.json()["result"]["tools"]] == ["get_polytao_capabilities"]
