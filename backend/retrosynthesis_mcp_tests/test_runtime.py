from __future__ import annotations

import asyncio
import json

import httpx2 as httpx
import pytest

from app.mcp_security import MCPFailure
from app.retrosynthesis_mcp import MCPRuntime, tool_result
from app.retrosynthesis_mcp_contract import INPUT_MODELS
from app.retrosynthesis_mcp_journal import DraftError
from conftest import session_response


async def execute(runtime, name, values=None, client="test"):
    return await runtime.execute(name, INPUT_MODELS[name].model_validate(values or {}), client)


async def prepare(runtime, **extra):
    return await execute(runtime, "prepare_monomer_retrosynthesis", {"conversation_id": "ses_test", "target": {"source": "smiles", "smiles": "CCO"}, **extra})


def approval(draft):
    return {"conversation_id": draft.conversation_id, "draft_id": draft.draft_id, "revision": draft.revision}


@pytest.fixture
async def runtime(configuration, native):
    runtime = MCPRuntime(configuration, transport=httpx.MockTransport(native))
    runtime.startup_check()
    try:
        yield runtime
    finally:
        await runtime.close()


@pytest.mark.anyio
async def test_prepare_edit_cancel_never_predict(runtime, native):
    incomplete = await execute(runtime, "prepare_monomer_retrosynthesis", {"conversation_id": "ses_test"})
    assert not incomplete.ready and incomplete.missing_fields == ["target"]
    with pytest.raises(MCPFailure, match="Select one validated"):
        await execute(runtime, "confirm_monomer_retrosynthesis", approval(incomplete), "test-ui")
    edited = await execute(runtime, "update_retrosynthesis_draft", {**approval(incomplete), "target": {"source": "smiles", "smiles": "CCO"}, "parameters": {"num_return_sequences": 8}}, "test-ui")
    assert edited.ready and edited.parameters.num_beams == 8 and edited.revision == 2
    with pytest.raises(DraftError) as stale:
        await execute(runtime, "update_retrosynthesis_draft", {**approval(incomplete), "parameters": {}}, "test-ui")
    assert stale.value.code == "DRAFT_REVISION_CONFLICT"
    cleared = await execute(runtime, "update_retrosynthesis_draft", {**approval(edited), "target": None}, "test-ui")
    assert not cleared.ready
    cancelled = await execute(runtime, "cancel_retrosynthesis_draft", approval(cleared), "test-ui")
    assert cancelled.status == "cancelled"
    assert not native.posts


@pytest.mark.anyio
async def test_human_confirmation_freezes_and_replays(runtime, native):
    draft = await prepare(runtime)
    with pytest.raises(MCPFailure) as denied:
        await execute(runtime, "confirm_monomer_retrosynthesis", approval(draft))
    assert denied.value.code == "PERMISSION_DENIED"
    submitted = await execute(runtime, "confirm_monomer_retrosynthesis", approval(draft), "test-ui")
    assert submitted.status == "submitted" and submitted.job_id == "retro_test"
    replayed = await execute(runtime, "confirm_monomer_retrosynthesis", approval(draft), "test-ui")
    assert replayed.submission.replayed and len(native.posts) == 1
    with pytest.raises(MCPFailure) as frozen:
        await execute(runtime, "update_retrosynthesis_draft", {**approval(draft), "parameters": {"max_new_tokens": 64}}, "test-ui")
    assert frozen.value.code == "DRAFT_LOCKED"
    queried = await execute(runtime, "get_retrosynthesis_job", {"conversation_id": "ses_test", "job_id": submitted.job_id, "wait_seconds": 20})
    assert queried.result.total == 1
    assert queried.result.candidates[0].valid_smiles
    assert native.requests[-1].url.params["wait_seconds"] == "20"
    with pytest.raises(MCPFailure) as other_conversation:
        await execute(runtime, "get_retrosynthesis_job", {"conversation_id": "ses_other", "job_id": submitted.job_id})
    assert other_conversation.value.code == "JOB_NOT_FOUND"


@pytest.mark.anyio
async def test_concurrent_double_confirmation_at_most_one_post(runtime, native):
    draft = await prepare(runtime)
    results = await asyncio.gather(*[execute(runtime, "confirm_monomer_retrosynthesis", approval(draft), "test-ui") for _ in range(2)])
    assert {result.job_id for result in results} == {"retro_test"}
    assert len(native.posts) == 1


@pytest.mark.anyio
async def test_lost_post_response_recovers_by_native_lookup(runtime, native):
    native.lost_response = True
    draft = await prepare(runtime)
    submitted = await execute(runtime, "confirm_monomer_retrosynthesis", approval(draft), "test-ui")
    assert submitted.job_id == "retro_test" and submitted.submission.replayed
    assert len(native.posts) == 1


@pytest.mark.anyio
async def test_unknown_read_only_recovery_never_posts_when_lookup_missing(runtime, native):
    native.lost_response = native.lookup_missing = True
    draft = await prepare(runtime)
    with pytest.raises(MCPFailure) as uncertain:
        await execute(runtime, "confirm_monomer_retrosynthesis", approval(draft), "test-ui")
    assert uncertain.value.code == "SUBMISSION_NOT_RECEIVED"
    for _ in range(2):
        snapshot = await execute(runtime, "get_retrosynthesis_draft", {"conversation_id": draft.conversation_id, "draft_id": draft.draft_id})
        assert snapshot.status == "submission_unknown" and snapshot.submission_error == "SUBMISSION_NOT_RECEIVED"
    assert len(native.posts) == 1
    native.lookup_missing = False
    recovered = await execute(runtime, "get_retrosynthesis_draft", {key: value for key, value in approval(draft).items() if key != "revision"})
    assert recovered.job_id == "retro_test" and recovered.status == "submitted"
    assert len(native.posts) == 1


@pytest.mark.anyio
async def test_explicit_retry_after_lost_first_request_reuses_exact_key_and_body(runtime, native):
    native.fail_before_receive = 1
    draft = await prepare(runtime)
    with pytest.raises(MCPFailure) as lost:
        await execute(runtime, "confirm_monomer_retrosynthesis", approval(draft), "test-ui")
    assert lost.value.code == "SUBMISSION_NOT_RECEIVED" and not native.jobs
    read = await execute(runtime, "get_retrosynthesis_draft", {"conversation_id": draft.conversation_id, "draft_id": draft.draft_id})
    assert read.status == "submission_unknown" and len(native.posts) == 1
    retried = await execute(runtime, "confirm_monomer_retrosynthesis", approval(draft), "test-ui")
    assert retried.job_id == "retro_test" and native.accepted_count == len(native.jobs) == 1
    assert len(native.posts) == 2
    assert native.posts[0].headers["idempotency-key"] == native.posts[1].headers["idempotency-key"]
    assert native.posts[0].content == native.posts[1].content


@pytest.mark.anyio
async def test_late_original_and_explicit_retry_share_one_native_job(runtime, native):
    native.first_post_entered, native.release_first_post = asyncio.Event(), asyncio.Event()
    draft = await prepare(runtime)
    original = asyncio.create_task(execute(runtime, "confirm_monomer_retrosynthesis", approval(draft), "test-ui"))
    await native.first_post_entered.wait()
    retried = await execute(runtime, "confirm_monomer_retrosynthesis", approval(draft), "test-ui")
    native.release_first_post.set()
    first = await original
    assert first.job_id == retried.job_id == "retro_test"
    assert native.accepted_count == len(native.jobs) == 1 and len(native.posts) == 2
    assert native.posts[0].headers["idempotency-key"] == native.posts[1].headers["idempotency-key"]
    assert native.posts[0].content == native.posts[1].content


@pytest.mark.anyio
async def test_late_rejection_cannot_downgrade_accepted_explicit_retry(runtime, native):
    native.first_post_entered, native.release_first_post = asyncio.Event(), asyncio.Event()
    native.first_post_status = 429
    draft = await prepare(runtime)
    original = asyncio.create_task(execute(runtime, "confirm_monomer_retrosynthesis", approval(draft), "test-ui"))
    await native.first_post_entered.wait()
    retry = await execute(runtime, "confirm_monomer_retrosynthesis", approval(draft), "test-ui")
    native.release_first_post.set()
    first = await original
    assert first.job_id == retry.job_id == "retro_test" and not runtime.get_journal().failed
    read = await execute(runtime, "get_retrosynthesis_draft", {"conversation_id": draft.conversation_id, "draft_id": draft.draft_id})
    assert read.status == "submitted" and read.job_id == "retro_test"


@pytest.mark.anyio
async def test_restart_preserves_submission_and_conversation_binding(configuration, native):
    first = MCPRuntime(configuration, transport=httpx.MockTransport(native))
    first.startup_check()
    draft = await prepare(first)
    submitted = await execute(first, "confirm_monomer_retrosynthesis", approval(draft), "test-ui")
    await first.close()
    second = MCPRuntime(configuration, transport=httpx.MockTransport(native))
    second.startup_check()
    try:
        read = await execute(second, "get_retrosynthesis_draft", {"draft_id": draft.draft_id, "conversation_id": "ses_test"})
        assert read.job_id == submitted.job_id
        queried = await execute(second, "get_retrosynthesis_job", {"job_id": submitted.job_id, "conversation_id": "ses_test"})
        assert queried.result.total == 1
        assert len(native.posts) == 1
    finally:
        await second.close()


@pytest.mark.anyio
async def test_rejected_submission_replays_error_without_post(runtime, native):
    native.submit_status = 429
    draft = await prepare(runtime)
    for _ in range(2):
        with pytest.raises(MCPFailure) as limited:
            await execute(runtime, "confirm_monomer_retrosynthesis", approval(draft), "test-ui")
        assert limited.value.code == "CAPACITY_LIMITED"
    assert len(native.posts) == 1


@pytest.mark.anyio
async def test_ambiguous_name_stays_incomplete_without_gpu(runtime, native):
    native.resolve_ambiguous = True
    draft = await prepare(runtime, target={"source": "name", "name": "ambiguous"})
    assert not draft.ready and len(draft.target_candidates) == 2
    with pytest.raises(MCPFailure):
        await execute(runtime, "confirm_monomer_retrosynthesis", approval(draft), "test-ui")
    assert not native.posts


@pytest.mark.anyio
async def test_auth_revalidated_each_call_and_errors_dont_leak(runtime, native):
    native.session_override = session_response(runtime.config)
    native.session_override["capabilities"]["retrosynthesis.use"] = False
    with pytest.raises(MCPFailure) as denied:
        await prepare(runtime)
    assert denied.value.code == "PERMISSION_DENIED"
    assert not native.posts


@pytest.mark.anyio
async def test_effective_body_canonical_target_and_source(runtime, native):
    draft = await prepare(runtime, parameters={"target_role": "diamine", "num_return_sequences": 3})
    await execute(runtime, "confirm_monomer_retrosynthesis", approval(draft), "test-ui")
    body = json.loads(native.posts[0].content)
    assert body == {"smiles": "CCO", "source": {"type": "smiles", "label": None, "record_id": None},
        "target_role": "diamine", "num_return_sequences": 3, "num_beams": 5, "max_new_tokens": 128}


@pytest.mark.anyio
async def test_native_acceptance_before_draft_receipt_is_recovered(runtime, native):
    draft = await prepare(runtime)
    journal = runtime.get_journal()
    item = journal.confirm_draft("test", runtime.config.account_uuid, "ses_test", draft.draft_id, 1)
    accepted = await runtime.submit(item, "test", await runtime.session())
    assert accepted.job_id == "retro_test"
    read = await execute(runtime, "get_retrosynthesis_draft", {"draft_id": draft.draft_id, "conversation_id": "ses_test"})
    assert read.status == "submitted" and read.job_id == "retro_test"
    assert len(native.posts) == 1


@pytest.mark.anyio
async def test_oversize_public_result_rejects_without_truncating_molecules(runtime):
    draft = await prepare(runtime)
    draft.resolved_target.preview_svg = "<svg>" + "x" * (256 * 1024) + "</svg>"
    with pytest.raises(MCPFailure) as too_large:
        tool_result(draft, "test-request")
    assert too_large.value.code == "RESPONSE_TOO_LARGE"
    assert draft.resolved_target.canonical_smiles == "CCO"


@pytest.mark.anyio
async def test_conflicting_advanced_parameters_are_invalid_arguments(runtime):
    with pytest.raises(MCPFailure) as conflict:
        await prepare(runtime, parameters={"num_beams": 2, "num_return_sequences": 5})
    assert conflict.value.code == "INVALID_ARGUMENT"
