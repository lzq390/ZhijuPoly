from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import httpx2 as httpx
import pytest

from app.mcp_security import MCPFailure
from app.polytao_mcp import API, MCPRuntime
from app.polytao_mcp_contract import CapabilitiesInput, DescriptorInput, JobInput, SubmitInput, TOOL_NAMES
from conftest import descriptor_values, job_response, session_response


def arguments(key="one", **kwargs):
    return SubmitInput(descriptors=descriptor_values(), idempotency_key=key, **kwargs)


async def dispatch(runtime, args, client_id):
    return await runtime.submit(args, client_id, await runtime.session())


def context():
    return SimpleNamespace(request=SimpleNamespace(state=SimpleNamespace(
        mcp_client=SimpleNamespace(client_id="test", tools=TOOL_NAMES), mcp_request_id="request-id")))


def mock_backend(config, callback):
    async def handler(request):
        assert request.headers["cookie"] == "nexpoly_session=PRIVATE_COOKIE"
        assert request.headers["x-session-context"] == "session-id"
        if request.method == "POST":
            assert request.headers["origin"] == config.backend_url
            assert request.headers["x-csrf-token"] == "PRIVATE_CSRF"
        if request.url.path == "/api/v1/auth/session":
            return httpx.Response(200, json=session_response(config))
        return await callback(request)
    runtime = MCPRuntime(config, transport=httpx.MockTransport(handler))
    runtime.startup_check()
    return runtime


@pytest.mark.anyio
async def test_concurrent_same_key_posts_once_and_replays(configuration):
    posts, entered, release = [], asyncio.Event(), asyncio.Event()
    async def callback(request):
        posts.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(202, json={"job_id": "polytao.instance.token", "status": "pending"})
    runtime = mock_backend(configuration, callback)
    first = asyncio.create_task(dispatch(runtime, arguments(), "test"))
    await entered.wait()
    with pytest.raises(MCPFailure) as caught:
        await dispatch(runtime, arguments(), "test")
    assert caught.value.code == "SUBMISSION_PENDING"
    release.set()
    assert (await first).replayed is False
    assert (await dispatch(runtime, arguments(), "test")).replayed is True
    assert len(posts) == 1
    assert len(posts[0].headers["idempotency-key"]) == 64
    with pytest.raises(MCPFailure) as caught:
        await dispatch(runtime, arguments(candidate_count=2), "test")
    assert caught.value.code == "IDEMPOTENCY_CONFLICT"
    await runtime.close()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["disconnect", "timeout", "500", "503", "redirect", "invalid", "usage_unknown", "bad_job"])
async def test_ambiguous_submission_never_reposts(configuration, failure):
    posts = []
    async def callback(request):
        posts.append(request)
        if failure == "disconnect":
            raise httpx.ReadError("PRIVATE_ERROR")
        if failure == "timeout":
            raise httpx.ReadTimeout("PRIVATE_ERROR")
        if failure == "invalid":
            return httpx.Response(202, content=b"invalid")
        if failure == "usage_unknown":
            return httpx.Response(409, json={"detail": {"code": "usage_outcome_unknown"}})
        if failure == "bad_job":
            return httpx.Response(202, json={"job_id": "../private", "status": "pending"})
        return httpx.Response({"500": 500, "503": 503, "redirect": 307}[failure], json={}, headers={"Location": "http://evil/"})
    runtime = mock_backend(configuration, callback)
    for _ in range(2):
        with pytest.raises(MCPFailure) as caught:
            await dispatch(runtime, arguments(), "test")
        assert caught.value.code == "SUBMISSION_OUTCOME_UNKNOWN"
    await runtime.close()
    # A restarted adapter preserves uncertainty and cannot send a second POST.
    runtime = mock_backend(configuration, callback)
    with pytest.raises(MCPFailure) as caught:
        await dispatch(runtime, arguments(), "test")
    assert caught.value.code == "SUBMISSION_OUTCOME_UNKNOWN"
    assert len(posts) == 1
    await runtime.close()


@pytest.mark.anyio
@pytest.mark.parametrize("status", [401, 403, 404, 422, 429])
async def test_definitive_rejection_replayed(configuration, status):
    posts = []
    async def callback(request):
        posts.append(request)
        return httpx.Response(status, json={"detail": "not accepted"})
    runtime = mock_backend(configuration, callback)
    codes = []
    for _ in range(2):
        with pytest.raises(MCPFailure) as caught:
            await dispatch(runtime, arguments(), "test")
        codes.append(caught.value.code)
    assert codes[0] == codes[1]
    assert len(posts) == 1
    await runtime.close()


@pytest.mark.anyio
async def test_accept_response_write_failure_returns_unknown_and_latches(configuration):
    posts = []
    async def callback(request):
        posts.append(request)
        with sqlite3.connect(configuration.journal_file) as db:
            db.execute("CREATE TRIGGER reject_update BEFORE UPDATE ON submissions BEGIN SELECT RAISE(FAIL, 'test'); END")
        return httpx.Response(202, json={"job_id": "polytao.instance.token", "status": "pending"})
    runtime = mock_backend(configuration, callback)
    with pytest.raises(MCPFailure) as caught:
        await dispatch(runtime, arguments(), "test")
    assert caught.value.code == "SUBMISSION_OUTCOME_UNKNOWN"
    with pytest.raises(MCPFailure) as caught:
        await dispatch(runtime, arguments(), "test")
    assert caught.value.code == "JOURNAL_UNAVAILABLE"
    assert len(posts) == 1
    await runtime.close()


@pytest.mark.anyio
async def test_cancelled_submission_is_unknown_without_repost(configuration):
    entered = asyncio.Event()
    posts = []
    async def callback(request):
        posts.append(request)
        entered.set()
        await asyncio.Event().wait()
    runtime = mock_backend(configuration, callback)
    task = asyncio.create_task(dispatch(runtime, arguments(), "test"))
    await entered.wait()
    task.cancel()
    with pytest.raises(MCPFailure) as caught:
        await task
    assert caught.value.code == "SUBMISSION_OUTCOME_UNKNOWN"
    with pytest.raises(MCPFailure) as caught:
        await dispatch(runtime, arguments(), "test")
    assert caught.value.code == "SUBMISSION_OUTCOME_UNKNOWN"
    assert len(posts) == 1
    await runtime.close()


@pytest.mark.anyio
async def test_page_counts_private_fields_and_job_owner(configuration):
    requests = []
    async def callback(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "polytao.instance.token", "status": "pending"})
        return httpx.Response(200, json=job_response("polytao.instance.token"))
    runtime = mock_backend(configuration, callback)
    await dispatch(runtime, arguments(candidate_count=3), "test")
    page = await runtime.execute("get_polytao_job", JobInput(job_id="polytao.instance.token", limit=1), "test")
    assert page.returned_count == 2 and len(page.results) == 1 and page.next_offset == 1
    assert page.warnings and page.results[0].sa_score is None
    assert "PRIVATE" not in page.model_dump_json()
    page2 = await runtime.execute("get_polytao_job", JobInput(job_id="polytao.instance.token", offset=1, limit=1), "test")
    assert page2.results[0].rank == 2 and page2.next_offset is None
    prior = len(requests)
    with pytest.raises(MCPFailure) as caught:
        await runtime.execute("get_polytao_job", JobInput(job_id="polytao.instance.token"), "other")
    assert caught.value.code == "PERMISSION_DENIED" and len(requests) == prior
    await runtime.close()


@pytest.mark.anyio
async def test_zero_candidates_and_inconsistent_backend_counts(configuration):
    response = job_response("polytao.instance.token", count=0)
    async def callback(request):
        return httpx.Response(200, json=response)
    runtime = mock_backend(configuration, callback)
    runtime.journal.begin("test", configuration.account_uuid, "k", {})
    runtime.journal.finish("test", configuration.account_uuid, "k", state="accepted", job_id="polytao.instance.token", job_status="pending")
    output = await runtime.execute("get_polytao_job", JobInput(job_id="polytao.instance.token", wait_seconds=20), "test")
    assert output.status == "completed" and output.returned_count == 0 and output.warnings
    response["returned_count"] = 1
    with pytest.raises(MCPFailure) as caught:
        await runtime.execute("get_polytao_job", JobInput(job_id="polytao.instance.token"), "test")
    assert caught.value.code == "BACKEND_INVALID_RESPONSE"
    await runtime.close()


@pytest.mark.anyio
@pytest.mark.parametrize("status,code", [(404, "JOB_NOT_FOUND"), (410, "RESULT_EXPIRED"), (409, "USAGE_OUTCOME_UNKNOWN")])
async def test_lost_job_and_usage_unknown_never_replace_task(configuration, status, code):
    posts = []
    async def callback(request):
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(202, json={"job_id": "polytao.instance.token", "status": "pending"})
        return httpx.Response(status, json={"detail": {"code": "usage_outcome_unknown"}} if status == 409 else {})
    runtime = mock_backend(configuration, callback)
    await dispatch(runtime, arguments(), "test")
    with pytest.raises(MCPFailure) as caught:
        await runtime.execute("get_polytao_job", JobInput(job_id="polytao.instance.token"), "test")
    assert caught.value.code == code
    assert (await dispatch(runtime, arguments(), "test")).replayed
    assert len(posts) == 1
    await runtime.close()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["account", "session", "csrf", "member", "password", "capability"])
async def test_live_auth_preflight_blocks_wrong_account_and_revoked_permissions(configuration, change):
    response = session_response(configuration)
    if change == "account": response["user"]["id"] = "other"
    if change == "session": response["session_id"] = "other"
    if change == "csrf": response["csrf_token"] = "other"
    if change == "member": response["account_type"] = "free"
    if change == "password": response["user"]["must_change_password"] = True
    if change == "capability": response["capabilities"]["polytao.use"] = False
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response)
    runtime = MCPRuntime(configuration, transport=httpx.MockTransport(handler))
    runtime.startup_check()
    with pytest.raises(MCPFailure):
        await dispatch(runtime, arguments(), "test")
    assert len(calls) == 1
    assert runtime.journal.begin("test", configuration.account_uuid, "one", arguments().backend_body())[1]
    await runtime.close()


@pytest.mark.anyio
async def test_descriptor_response_no_prompt_and_no_journal_needed(configuration):
    async def callback(request):
        return httpx.Response(200, json={"input_smiles": "*CC*", "canonical_smiles": "*CC*", "query_time_ms": 2,
            "prompt": "PRIVATE_PROMPT", "descriptors": [{"name": key, "value": value} for key, value in descriptor_values().items()]})
    runtime = mock_backend(configuration, callback)
    runtime.journal.close()
    runtime.journal = None
    output = await runtime.execute("calculate_polytao_descriptors", DescriptorInput(smiles="*CC*"), "test")
    assert len(output.descriptors.model_dump()) == 15 and "PRIVATE" not in output.model_dump_json()
    await runtime.close()


@pytest.mark.anyio
async def test_call_errors_request_id_permission_rate_and_audit_redaction(configuration, caplog):
    async def callback(request):
        return httpx.Response(200, json={"enabled": True, "available": True, "worker_base_url_configured": False,
            "message": "PRIVATE_PATH", "runtime_ready": False})
    runtime = mock_backend(replace(configuration, rate_limit=1), callback)
    caplog.set_level("INFO", logger="nexpoly.polytao_mcp.audit")
    good = SimpleNamespace(name="get_polytao_capabilities", arguments={})
    result = await runtime.call(context(), good)
    assert result.structured_content["ok"] and result.structured_content["request_id"] == "request-id"
    limited = await runtime.call(context(), good)
    assert limited.structured_content["error"]["code"] == "RATE_LIMITED"
    invalid = await runtime.call(context(), SimpleNamespace(name="prepare_polytao_generation", arguments={}))
    assert invalid.structured_content["error"]["code"] == "INVALID_ARGUMENT"
    denied_context = context()
    denied_context.request.state.mcp_client.tools = frozenset()
    denied = await runtime.call(denied_context, good)
    assert denied.structured_content["error"]["code"] == "PERMISSION_DENIED"
    assert "PRIVATE" not in caplog.text and "MolWt" not in caplog.text
    await runtime.close()


@pytest.mark.anyio
async def test_wait_polls_twice_then_returns_terminal_snapshot(configuration, monkeypatch):
    snapshots, sleeps = [], []
    async def callback(request):
        snapshots.append(request)
        if len(snapshots) == 1:
            response = job_response("polytao.instance.token", count=0, status="running")
            response["result"] = None
            response["progress_stage"] = "generation"
            response["progress_percent"] = 50
        else:
            response = job_response("polytao.instance.token")
        return httpx.Response(200, json=response)
    runtime = mock_backend(configuration, callback)
    runtime.journal.begin("test", configuration.account_uuid, "k", {})
    runtime.journal.finish("test", configuration.account_uuid, "k", state="accepted", job_id="polytao.instance.token", job_status="pending")
    async def fake_sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr("app.polytao_mcp.asyncio.sleep", fake_sleep)
    output = await runtime.execute("get_polytao_job", JobInput(job_id="polytao.instance.token", wait_seconds=20), "test")
    assert output.status == "completed" and len(snapshots) == 2 and sleeps == [2]
    await runtime.close()


@pytest.mark.anyio
async def test_wait_budget_exhaustion_returns_last_snapshot_without_extra_get(configuration, monkeypatch):
    import app.polytao_mcp as module
    clock, reads = [0.0], []
    async def callback(request):
        reads.append(request)
        clock[0] += 19
        response = job_response("polytao.instance.token", count=0, status="running")
        response["result"] = None
        return httpx.Response(200, json=response)
    runtime = mock_backend(configuration, callback)
    runtime.journal.begin("test", configuration.account_uuid, "k", {})
    runtime.journal.finish("test", configuration.account_uuid, "k", state="accepted", job_id="polytao.instance.token", job_status="pending")
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    async def fake_sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    output = await runtime.execute("get_polytao_job", JobInput(job_id="polytao.instance.token", wait_seconds=20), "test")
    assert output.status == "running" and len(reads) == 1 and clock[0] == 20
    await runtime.close()


@pytest.mark.anyio
async def test_query_deadline_leaves_original_submission_accepted(configuration):
    async def callback(request):
        response = job_response("polytao.instance.token", count=0, status="running")
        response["result"] = None
        return httpx.Response(200, json=response)
    runtime = mock_backend(replace(configuration, tool_timeout_seconds=0.02), callback)
    runtime.journal.begin("test", configuration.account_uuid, "k", {})
    runtime.journal.finish("test", configuration.account_uuid, "k", state="accepted", job_id="polytao.instance.token", job_status="pending")
    output = await runtime.call(context(), SimpleNamespace(name="get_polytao_job", arguments={"job_id": "polytao.instance.token", "wait_seconds": 20}))
    assert output.structured_content["error"]["code"] == "QUERY_TIMEOUT"
    assert runtime.journal.owns_job("test", configuration.account_uuid, "polytao.instance.token")
    assert runtime.limiter.active["test"] == 0
    await runtime.close()


@pytest.mark.anyio
async def test_confirm_deadline_is_explicit_unknown_and_not_query_timeout(configuration):
    posts = []
    async def callback(request):
        posts.append(request)
        await asyncio.Event().wait()
    runtime = mock_backend(replace(configuration, tool_timeout_seconds=0.02), callback)
    from app.polytao_mcp_contract import PrepareInput
    draft = await runtime.execute("prepare_polytao_generation", PrepareInput(conversation_id="ses_test", descriptors=descriptor_values()), "test")
    ui = context()
    ui.request.state.mcp_client.client_id = "test-ui"
    confirm = SimpleNamespace(name="confirm_polytao_generation", arguments={"draft_id": draft.draft_id, "conversation_id": "ses_test", "revision": 1})
    output = await runtime.call(ui, confirm)
    assert output.structured_content["error"]["code"] == "SUBMISSION_OUTCOME_UNKNOWN"
    replay = await runtime.call(ui, confirm)
    assert replay.structured_content["error"]["code"] == "SUBMISSION_OUTCOME_UNKNOWN" and len(posts) == 1
    await runtime.close()


@pytest.mark.anyio
async def test_backend_response_limit_and_failed_job_error_redaction(configuration, monkeypatch):
    import app.polytao_mcp as module
    async def callback(request):
        return httpx.Response(200, json={"data": "x" * 2048})
    runtime = mock_backend(configuration, callback)
    monkeypatch.setattr(module, "MAX_BACKEND_BYTES", 1024)
    with pytest.raises(MCPFailure) as caught:
        await runtime.execute("calculate_polytao_descriptors", DescriptorInput(smiles="*CC*"), "test")
    assert caught.value.code == "BACKEND_RESPONSE_TOO_LARGE"
    await runtime.close()


@pytest.mark.anyio
async def test_failed_task_is_read_success_with_sanitized_error(configuration):
    response = job_response("polytao.instance.token", count=0, status="failed")
    response["result"] = None
    response["error_message"] = "PRIVATE_PATH failed model load"
    async def callback(request):
        return httpx.Response(200, json=response)
    runtime = mock_backend(configuration, callback)
    runtime.journal.begin("test", configuration.account_uuid, "k", {})
    runtime.journal.finish("test", configuration.account_uuid, "k", state="accepted", job_id="polytao.instance.token", job_status="pending")
    output = await runtime.call(context(), SimpleNamespace(name="get_polytao_job", arguments={"job_id": "polytao.instance.token"}))
    assert not output.is_error and output.structured_content["status"] == "failed"
    assert output.structured_content["error_code"] == "GENERATION_FAILED"
    assert "PRIVATE" not in output.model_dump_json()
    await runtime.close()
