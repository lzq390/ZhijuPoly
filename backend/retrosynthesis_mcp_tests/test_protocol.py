from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx2 as httpx
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from app.retrosynthesis_mcp import create_app
from app.retrosynthesis_mcp_contract import MODEL_TOOL_NAMES, UI_TOOL_NAMES
from conftest import job_response


@asynccontextmanager
async def sdk(app, token):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers={"Authorization": f"Bearer {token}"}) as http:
        @asynccontextmanager
        async def transport():
            async with streamable_http_client("http://127.0.0.1:18211/mcp", http_client=http) as (read, write):
                yield read, write
        async with Client(transport(), read_timeout_seconds=10) as client:
            yield client


@pytest.mark.anyio
async def test_sdk_model_ui_discovery_and_confirmed_query(configuration, native):
    app = create_app(configuration, transport=httpx.MockTransport(native))
    async with app.router.lifespan_context(app):
        async with sdk(app, "test-token") as model:
            tools = (await model.list_tools()).tools
            assert {tool.name for tool in tools} == MODEL_TOOL_NAMES
            assert all(tool.input_schema and tool.output_schema for tool in tools)
            capabilities = await model.call_tool("get_retrosynthesis_capabilities", {})
            assert not capabilities.is_error and capabilities.structured_content["submission_ready"]
            resolution = await model.call_tool("resolve_retrosynthesis_target", {"conversation_id": "ses_sdk", "target": {"source": "smiles", "smiles": "CCO"}})
            assert resolution.structured_content["target"]["canonical_smiles"] == "CCO"
            prepared = await model.call_tool("prepare_monomer_retrosynthesis", {"conversation_id": "ses_sdk", "target": {"source": "smiles", "smiles": "CCO"}})
            assert not prepared.is_error and prepared.structured_content["ready"]
            assert not native.posts
            draft = prepared.structured_content
            args = {"conversation_id": "ses_sdk", "draft_id": draft["draft_id"], "revision": 1}
            denied = await model.call_tool("confirm_monomer_retrosynthesis", args)
            assert denied.is_error and denied.structured_content["error"]["code"] == "PERMISSION_DENIED"
            async with sdk(app, "ui-token") as ui:
                assert {tool.name for tool in (await ui.list_tools()).tools} == UI_TOOL_NAMES
                submitted = await ui.call_tool("confirm_monomer_retrosynthesis", args)
                assert not submitted.is_error and submitted.structured_content["status"] == "submitted"
                replayed = await ui.call_tool("confirm_monomer_retrosynthesis", args)
                assert replayed.structured_content["submission"]["replayed"]
            queried = await model.call_tool("get_retrosynthesis_job", {"conversation_id": "ses_sdk", "job_id": "retro_test"})
            assert not queried.is_error and queried.structured_content["result"]["total"] == 1
            assert "PRIVATE" not in json.dumps(queried.structured_content)
            other = await model.call_tool("get_retrosynthesis_job", {"conversation_id": "ses_other", "job_id": "retro_test"})
            assert other.is_error and other.structured_content["error"]["code"] == "JOB_NOT_FOUND"
            invalid = await model.call_tool("get_retrosynthesis_job", {"conversation_id": "ses_sdk", "job_id": "retro_test", "wait_seconds": 21})
            assert invalid.is_error and invalid.structured_content["error"]["code"] == "INVALID_ARGUMENT"
    assert len(native.posts) == 1


@pytest.mark.anyio
async def test_http_guard_and_hot_token_revocation(configuration, native):
    app = create_app(configuration, transport=httpx.MockTransport(native))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:18211") as http:
            assert (await http.get("/healthz")).status_code == 200
            assert (await http.post("/mcp")).status_code == 401
            headers = {"Authorization": "Bearer test-token", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            assert (await http.post("/mcp", headers={**headers, "Host": "evil"})).status_code == 421
            assert (await http.post("/mcp", headers={**headers, "Origin": "http://evil"})).status_code == 403
            assert (await http.post("/mcp?token=leak", headers=headers)).status_code == 400
            assert (await http.post("/mcp", headers=headers, content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "padding": "x" * 32768}))).status_code == 413
            configuration.clients_file.write_text('{"clients":[]}')
            assert (await http.post("/mcp", headers=headers)).status_code == 401


@pytest.mark.anyio
async def test_unknown_submission_is_sanitized_and_sdk_can_recover(configuration, native):
    native.lost_response = native.lookup_missing = True
    app = create_app(configuration, transport=httpx.MockTransport(native))
    async with app.router.lifespan_context(app):
        async with sdk(app, "test-token") as model:
            prepared = await model.call_tool("prepare_monomer_retrosynthesis", {"conversation_id": "ses_sdk", "target": {"source": "smiles", "smiles": "CCO"}})
            args = {"conversation_id": "ses_sdk", "draft_id": prepared.structured_content["draft_id"], "revision": 1}
            async with sdk(app, "ui-token") as ui:
                uncertain = await ui.call_tool("confirm_monomer_retrosynthesis", args)
                assert uncertain.is_error and uncertain.structured_content["error"]["code"] == "SUBMISSION_NOT_RECEIVED"
                assert "PRIVATE" not in json.dumps(uncertain.structured_content)
                native.lookup_missing = False
                recovered = await ui.call_tool("confirm_monomer_retrosynthesis", args)
                assert recovered.structured_content["job_id"] == "retro_test"
    assert len(native.posts) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("receipt,lookup_error", [(True, False), (False, False), (True, True)])
async def test_sdk_capacity_rejection_checks_native_receipt(configuration, native, receipt, lookup_error):
    native.submit_status = 429
    native.rejected_job_receipt = receipt
    native.lookup_transport_error = lookup_error
    app = create_app(configuration, transport=httpx.MockTransport(native))
    async with app.router.lifespan_context(app), sdk(app, "test-token") as model, sdk(app, "ui-token") as ui:
        prepared = await model.call_tool("prepare_monomer_retrosynthesis", {
            "conversation_id": "ses_capacity", "target": {"source": "smiles", "smiles": "CCO"}})
        args = {"conversation_id": "ses_capacity", "draft_id": prepared.structured_content["draft_id"], "revision": 1}
        read_args = {name: value for name, value in args.items() if name != "revision"}
        confirmed = await ui.call_tool("confirm_monomer_retrosynthesis", args)
        lookups = [request for request in native.requests if request.url.path.endswith("/jobs/lookup")]
        assert len(lookups) == len(native.posts) == 1
        assert "PRIVATE" not in json.dumps(confirmed.structured_content)
        if lookup_error:
            assert confirmed.is_error and confirmed.structured_content["error"]["code"] == "SUBMISSION_OUTCOME_UNKNOWN"
            uncertain = await model.call_tool("get_retrosynthesis_draft", read_args)
            assert not uncertain.is_error and uncertain.structured_content["status"] == "submission_unknown"
            native.lookup_transport_error = False
        elif not receipt:
            assert confirmed.is_error and confirmed.structured_content["error"]["code"] == "CAPACITY_LIMITED"
        else:
            assert not confirmed.is_error and confirmed.structured_content["submission"]["status"] == "interrupted"
        current = await model.call_tool("get_retrosynthesis_draft", read_args)
        assert not current.is_error
        if receipt:
            assert current.structured_content["status"] == "submitted" and current.structured_content["job_id"] == "retro_test"
            job = await model.call_tool("get_retrosynthesis_job", {"conversation_id": "ses_capacity", "job_id": "retro_test"})
            assert not job.is_error and job.structured_content["status"] == "interrupted"
            assert job.structured_content["error_code"] == "submission_rejected" and job.structured_content["result"] is None
        else:
            assert current.structured_content["status"] == "rejected" and current.structured_content["job_id"] is None
            rejected_again = await ui.call_tool("confirm_monomer_retrosynthesis", args)
            assert rejected_again.is_error and rejected_again.structured_content["error"]["code"] == "CAPACITY_LIMITED"
        assert len(native.posts) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("receipt", [False, True])
async def test_sdk_legacy_capacity_rejection_read_recovers_without_post(configuration, native, receipt):
    app = create_app(configuration, transport=httpx.MockTransport(native))
    async with app.router.lifespan_context(app), sdk(app, "test-token") as model:
        prepared = await model.call_tool("prepare_monomer_retrosynthesis", {
            "conversation_id": "ses_legacy", "target": {"source": "smiles", "smiles": "CCO"}})
        draft_id = prepared.structured_content["draft_id"]
        runtime = app.state.mcp_runtime
        journal = runtime.get_journal()
        item = journal.confirm_draft("test", configuration.account_uuid, "ses_legacy", draft_id, 1)
        key = "draft:" + draft_id + ":1"
        journal.begin("test", configuration.account_uuid, key, runtime.submission_body(item))
        journal.finish("test", configuration.account_uuid, key, state="rejected", error_code="CAPACITY_LIMITED")
        journal.finish_draft("test", configuration.account_uuid, "ses_legacy", draft_id,
                             state="rejected", error_code="CAPACITY_LIMITED")
        if receipt:
            interrupted = job_response(status="interrupted")
            interrupted.update(stage="interrupted", result=None, error_code="submission_rejected")
            native.jobs[runtime.backend_key("test", key)] = interrupted
        read = await model.call_tool("get_retrosynthesis_draft", {"conversation_id": "ses_legacy", "draft_id": draft_id})
        assert not read.is_error
        assert read.structured_content["status"] == ("submitted" if receipt else "rejected")
        assert read.structured_content["job_id"] == ("retro_test" if receipt else None)
        assert journal.submission_record("test", configuration.account_uuid, key)["state"] == ("accepted" if receipt else "rejected")
        if receipt:
            foreign = await model.call_tool("get_retrosynthesis_job", {"conversation_id": "ses_other", "job_id": "retro_test"})
            assert foreign.is_error and foreign.structured_content["error"]["code"] == "JOB_NOT_FOUND"
        assert not native.posts and not journal.failed
