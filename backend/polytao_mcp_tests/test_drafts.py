"""Real HTTP MCP confirmation gates with synthetic native backend; no GPU jobs."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx2 as httpx
import pytest
from pydantic import ValidationError

from app.mcp_security import MCPFailure
from app.polytao_mcp_contract import (ApprovedSubmitInput, DraftInput, MODEL_TOOL_NAMES,
    PrepareInput, TOOL_NAMES, UpdateDraftInput)
from app.polytao_mcp_journal import DraftError, SubmissionJournal
from app.polytao_mcp_security import PolytaoClientRegistry
from conftest import descriptor_values
from test_runtime import mock_backend


def form(draft, **kwargs):
    return {"draft_id": draft.draft_id, "conversation_id": draft.conversation_id,
            "revision": draft.revision, **kwargs}


def handler(posts, descriptor_calls=None):
    async def callback(request):
        if request.url.path.endswith('/descriptors'):
            if descriptor_calls is not None:
                descriptor_calls.append(request)
            return httpx.Response(200, json={"input_smiles": "*CC*", "canonical_smiles": "*CC*", "query_time_ms": 1,
                "prompt": "PRIVATE", "descriptors": [{"name": name, "value": value} for name, value in descriptor_values().items()]})
        posts.append(request)
        return httpx.Response(202, json={"job_id": "polytao.instance.token", "status": "pending"})
    return callback


@pytest.mark.anyio
async def test_partial_form_never_fabricates_descriptors_and_requires_confirmation(configuration):
    posts = []
    runtime = mock_backend(configuration, handler(posts))
    draft = await runtime.execute("prepare_polytao_generation", PrepareInput(conversation_id="ses_one", descriptors={"MolWt": 300}), "test")
    assert draft.descriptors == {"MolWt": 300}
    assert draft.descriptor_sources == {"MolWt": "user"}
    assert len(draft.missing_fields) == 14 and len(draft.descriptor_fields) == 15
    assert draft.parameters.candidate_count == 10 and not draft.ready and draft.editable
    with pytest.raises(MCPFailure) as caught:
        await runtime.execute("confirm_polytao_generation", ApprovedSubmitInput(**form(draft)), "test")
    assert caught.value.code == "PERMISSION_DENIED"
    with pytest.raises(MCPFailure) as caught:
        await runtime.execute("confirm_polytao_generation", ApprovedSubmitInput(**form(draft)), "test-ui")
    assert caught.value.code == "DRAFT_INCOMPLETE" and not posts
    assert not runtime.journal.failed
    await runtime.close()


@pytest.mark.anyio
async def test_form_reference_and_explicit_edits_sources_clear_and_reset(configuration):
    posts, calculates = [], []
    runtime = mock_backend(configuration, handler(posts, calculates))
    draft = await runtime.execute("prepare_polytao_generation", PrepareInput(conversation_id="ses_ref", reference_smiles="*CC*",
        descriptors={"MolWt": 200, "NOCount": None}, candidate_count=3), "test")
    assert not draft.ready and draft.missing_fields == ["NOCount"]
    assert draft.descriptor_sources["MolWt"] == "user" and draft.descriptor_sources["HeavyAtomCount"] == "reference"
    draft = await runtime.execute("update_polytao_draft", UpdateDraftInput(**form(draft),
        descriptors={"NOCount": 0}, parameters={"temperature": 0.8}), "test-ui")
    assert draft.ready and draft.revision == 2 and draft.parameters.temperature == 0.8
    draft = await runtime.execute("update_polytao_draft", UpdateDraftInput(**form(draft),
        reference_smiles=None, parameters={"temperature": None}), "test-ui")
    assert draft.descriptors == {"MolWt": 200, "NOCount": 0} and draft.reference_smiles is None
    assert draft.parameters.temperature == 1.0 and len(draft.missing_fields) == 13
    draft = await runtime.execute("update_polytao_draft", UpdateDraftInput(**form(draft),
        reference_smiles="*CC*", descriptors={"MolWt": 300}), "test-ui")
    assert draft.ready and draft.descriptors["MolWt"] == 300 and draft.revision == 4
    assert len(calculates) == 2 and not posts
    await runtime.close()


@pytest.mark.anyio
async def test_revision_conversation_owner_and_cancel_fail_without_poisoning_journal(configuration):
    posts = []
    runtime = mock_backend(configuration, handler(posts))
    draft = await runtime.execute("prepare_polytao_generation", PrepareInput(conversation_id="ses_edit"), "test")
    changed = await runtime.execute("update_polytao_draft", UpdateDraftInput(**form(draft), descriptors={"MolWt": 300}), "test-ui")
    for name in ("update_polytao_draft", "confirm_polytao_generation", "cancel_polytao_draft"):
        with pytest.raises(DraftError) as caught:
            await runtime.execute(name, UpdateDraftInput(**form(draft)), "test-ui")
        assert caught.value.code == "DRAFT_REVISION_CONFLICT"
    with pytest.raises(DraftError) as caught:
        await runtime.execute("get_polytao_draft", DraftInput(draft_id=draft.draft_id, conversation_id="ses_other"), "test-ui")
    assert caught.value.code == "DRAFT_NOT_FOUND"
    with pytest.raises(DraftError):
        runtime.journal.read_draft("other", configuration.account_uuid, draft.conversation_id, draft.draft_id)
    cancelled = await runtime.execute("cancel_polytao_draft", ApprovedSubmitInput(**form(changed)), "test-ui")
    assert cancelled.status == "cancelled" and not cancelled.editable
    assert not runtime.journal.failed and not posts
    with pytest.raises(MCPFailure) as caught:
        await runtime.execute("update_polytao_draft", UpdateDraftInput(**form(cancelled)), "test-ui")
    assert caught.value.code == "DRAFT_LOCKED"
    await runtime.close()


@pytest.mark.anyio
async def test_confirm_freezes_exact_revision_one_post_and_persists_restart(configuration):
    posts = []
    runtime = mock_backend(configuration, handler(posts))
    draft = await runtime.execute("prepare_polytao_generation", PrepareInput(conversation_id="ses_confirm", descriptors=descriptor_values(), candidate_count=3), "test")
    confirm = ApprovedSubmitInput(**form(draft))
    submitted = await runtime.execute("confirm_polytao_generation", confirm, "test-ui")
    assert submitted.status == "submitted" and not submitted.editable and submitted.job_id == "polytao.instance.token"
    assert not submitted.submission.replayed and json.loads(posts[0].content)["candidate_count"] == 3
    assert runtime.journal.owns_job("test", configuration.account_uuid, submitted.job_id)
    with pytest.raises(MCPFailure) as caught:
        await runtime.execute("update_polytao_draft", UpdateDraftInput(**form(submitted)), "test-ui")
    assert caught.value.code == "DRAFT_LOCKED"
    await runtime.close()
    runtime = mock_backend(configuration, handler(posts))
    saved = await runtime.execute("get_polytao_draft", DraftInput(draft_id=draft.draft_id, conversation_id="ses_confirm"), "test")
    assert saved.job_id == submitted.job_id and saved.status == "submitted" and saved.parameters.candidate_count == 3
    replay = await runtime.execute("confirm_polytao_generation", confirm, "test-ui")
    assert replay.submission.replayed and len(posts) == 1
    with pytest.raises(DraftError) as caught:
        await runtime.execute("cancel_polytao_draft", confirm, "test-ui")
    assert caught.value.code == "DRAFT_LOCKED"
    await runtime.close()


@pytest.mark.anyio
async def test_simultaneous_confirmation_cannot_duplicate_dispatch(configuration):
    posts, entered, release = [], asyncio.Event(), asyncio.Event()
    async def callback(request):
        posts.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(202, json={"job_id": "polytao.instance.token", "status": "pending"})
    runtime = mock_backend(configuration, callback)
    draft = await runtime.execute("prepare_polytao_generation", PrepareInput(conversation_id="ses_race", descriptors=descriptor_values()), "test")
    confirm = ApprovedSubmitInput(**form(draft))
    first = asyncio.create_task(runtime.execute("confirm_polytao_generation", confirm, "test-ui"))
    await entered.wait()
    with pytest.raises(MCPFailure) as caught:
        await runtime.execute("confirm_polytao_generation", confirm, "test-ui")
    assert caught.value.code == "SUBMISSION_PENDING"
    release.set()
    assert (await first).job_id == "polytao.instance.token" and len(posts) == 1
    await runtime.close()


@pytest.mark.anyio
async def test_frozen_before_post_recovers_but_ambiguous_intent_never_reposts(configuration):
    posts = []
    runtime = mock_backend(configuration, handler(posts))
    draft = await runtime.execute("prepare_polytao_generation", PrepareInput(conversation_id="ses_crash", descriptors=descriptor_values()), "test")
    # Persisted approval, process died before a submission intent was recorded.
    runtime.journal.confirm_draft("test", configuration.account_uuid, draft.conversation_id, draft.draft_id, 1)
    await runtime.close()
    runtime = mock_backend(configuration, handler(posts))
    assert (await runtime.execute("confirm_polytao_generation", ApprovedSubmitInput(**form(draft)), "test-ui")).job_id
    assert len(posts) == 1
    other = await runtime.execute("prepare_polytao_generation", PrepareInput(conversation_id="ses_unknown", descriptors=descriptor_values()), "test")
    item = runtime.journal.confirm_draft("test", configuration.account_uuid, other.conversation_id, other.draft_id, 1)
    body = {"descriptors": item["payload"]["descriptors"], **item["payload"]["parameters"]}
    runtime.journal.begin("test", configuration.account_uuid, "draft:" + other.draft_id + ":1", body)
    await runtime.close()
    runtime = mock_backend(configuration, handler(posts))
    for _ in range(2):
        with pytest.raises(MCPFailure) as caught:
            await runtime.execute("confirm_polytao_generation", ApprovedSubmitInput(**form(other)), "test-ui")
        assert caught.value.code == "SUBMISSION_OUTCOME_UNKNOWN"
    assert len(posts) == 1
    saved = await runtime.execute("get_polytao_draft", DraftInput(draft_id=other.draft_id, conversation_id=other.conversation_id), "test-ui")
    assert saved.status == "submission_unknown" and not saved.editable
    await runtime.close()


@pytest.mark.parametrize("field,value", [("candidate_count", 0), ("candidate_count", True), ("temperature", 3),
    ("top_k", 501), ("top_p", 0), ("max_length", "16")])
def test_patch_parameter_schema_preserves_strict_bounds(field, value):
    with pytest.raises(ValidationError):
        UpdateDraftInput(draft_id="ptd_" + "a" * 32, conversation_id="ses_test", revision=1, parameters={field: value})


def test_confirmation_client_grants_are_fixed_server_configuration(configuration):
    registry = json.loads(configuration.clients_file.read_text())
    assert PolytaoClientRegistry.model_validate(registry).owner_for("test-ui") == "test"
    registry["clients"][0]["tools"].append("confirm_polytao_generation")
    with pytest.raises(ValidationError):
        PolytaoClientRegistry.model_validate(registry)
    registry = json.loads(configuration.clients_file.read_text())
    registry["clients"][1]["owner_client_id"] = "arbitrary"
    with pytest.raises(ValidationError):
        PolytaoClientRegistry.model_validate(registry)
    registry["clients"][1]["owner_client_id"] = "test"
    registry["clients"][1]["tools"].append("prepare_polytao_generation")
    with pytest.raises(ValidationError):
        PolytaoClientRegistry.model_validate(registry)


@pytest.mark.anyio
async def test_get_recovers_accepted_receipt_after_crash_before_draft_write_without_post(configuration):
    posts = []
    runtime = mock_backend(configuration, handler(posts))
    draft = await runtime.execute("prepare_polytao_generation", PrepareInput(conversation_id="ses_receipt", descriptors=descriptor_values(), candidate_count=3), "test")
    item = runtime.journal.confirm_draft("test", configuration.account_uuid, draft.conversation_id, draft.draft_id, 1)
    key = "draft:" + draft.draft_id + ":1"
    body = {"descriptors": item["payload"]["descriptors"], **item["payload"]["parameters"]}
    runtime.journal.begin("test", configuration.account_uuid, key, body)
    runtime.journal.finish("test", configuration.account_uuid, key, state="accepted", job_id="polytao.instance.token", job_status="pending")
    await runtime.close()
    runtime = mock_backend(configuration, handler(posts))
    saved = await runtime.execute("get_polytao_draft", DraftInput(draft_id=draft.draft_id, conversation_id=draft.conversation_id), "test-ui")
    assert saved.status == "submitted" and saved.job_id == "polytao.instance.token"
    assert saved.submission.replayed and saved.submission.effective_parameters == body and not posts
    replay = await runtime.execute("confirm_polytao_generation", ApprovedSubmitInput(**form(saved)), "test-ui")
    assert replay.submission.replayed and not posts
    await runtime.close()


@pytest.mark.anyio
async def test_get_recovers_unknown_intent_and_cancelled_draft_cannot_confirm(configuration):
    posts = []
    runtime = mock_backend(configuration, handler(posts))
    draft = await runtime.execute("prepare_polytao_generation", PrepareInput(conversation_id="ses_read_unknown", descriptors=descriptor_values()), "test")
    item = runtime.journal.confirm_draft("test", configuration.account_uuid, draft.conversation_id, draft.draft_id, 1)
    body = {"descriptors": item["payload"]["descriptors"], **item["payload"]["parameters"]}
    runtime.journal.begin("test", configuration.account_uuid, "draft:" + draft.draft_id + ":1", body)
    await runtime.close()
    runtime = mock_backend(configuration, handler(posts))
    saved = await runtime.execute("get_polytao_draft", DraftInput(draft_id=draft.draft_id, conversation_id=draft.conversation_id), "test-ui")
    assert saved.status == "submission_unknown" and saved.submission_error == "SUBMISSION_OUTCOME_UNKNOWN" and not posts
    other = await runtime.execute("prepare_polytao_generation", PrepareInput(conversation_id="ses_cancel"), "test")
    await runtime.execute("cancel_polytao_draft", ApprovedSubmitInput(**form(other)), "test-ui")
    with pytest.raises(MCPFailure) as caught:
        await runtime.execute("confirm_polytao_generation", ApprovedSubmitInput(**form(other)), "test-ui")
    assert caught.value.code == "DRAFT_CANCELLED" and not runtime.journal.failed and not posts
    await runtime.close()


@pytest.mark.anyio
async def test_confirm_freeze_wins_over_slow_reference_update_without_changing_approved_body(configuration):
    posts, entered, release = [], asyncio.Event(), asyncio.Event()
    original = handler(posts)
    async def callback(request):
        if request.url.path.endswith('/descriptors'):
            entered.set()
            await release.wait()
        return await original(request)
    runtime = mock_backend(configuration, callback)
    draft = await runtime.execute("prepare_polytao_generation", PrepareInput(conversation_id="ses_update_race", descriptors=descriptor_values()), "test")
    update = asyncio.create_task(runtime.execute("update_polytao_draft", UpdateDraftInput(**form(draft), reference_smiles="*CC*", descriptors={"MolWt": 500}), "test-ui"))
    await entered.wait()
    confirmed = await runtime.execute("confirm_polytao_generation", ApprovedSubmitInput(**form(draft)), "test-ui")
    release.set()
    with pytest.raises(DraftError) as caught:
        await update
    assert caught.value.code == "DRAFT_LOCKED"
    saved = await runtime.execute("get_polytao_draft", DraftInput(draft_id=draft.draft_id, conversation_id=draft.conversation_id), "test")
    assert saved.descriptors["MolWt"] == 1 and saved.status == "submitted"
    assert json.loads(posts[0].content)["descriptors"]["MolWt"] == 1 and len(posts) == 1
    assert not runtime.journal.failed and confirmed.job_id
    await runtime.close()
