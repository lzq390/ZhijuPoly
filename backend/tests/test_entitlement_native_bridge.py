"""Native bridge tests: no DB, provider, worker, or scientific computation."""
from unittest.mock import Mock
import pytest
from task_usage import native_entitlements as ne
from task_usage import entitlement_config, entitlement_policy

COMMON = dict(owner_user_id="owner-a", module="online_knowledge", native_task_id="native-a")


def operation(**changes):
    value = {"operation_id": "operation-a", "generation": 2, "state": "dispatching",
             "executor_id": "executor-a", "native_task_id": "native-a", "policy_version": "frozen-v1",
             "spec": {"mode": "synthesis", "max_papers": 1}}
    value.update(changes)
    return value


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(ne, "enabled", lambda: True)


def test_disabled_all_hooks_zero_db_and_zero_configuration(monkeypatch):
    monkeypatch.setattr(ne, "enabled", lambda: False)
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled hook touched core/configuration")
    for name in ("lookup", "get_by_native", "reserve", "begin_dispatch", "claim_running", "settle", "release", "mark_unknown"):
        monkeypatch.setattr(ne.core, name, forbidden)
    monkeypatch.setattr(entitlement_config, "configured_policy", forbidden)
    assert ne.lookup_native(None, owner_user_id="bad", module="bad", idempotency_key=None, request_fingerprint=None) is None
    assert ne.reserve_native(None, **COMMON, idempotency_key=None, request_fingerprint=None, spec=None)["disabled"]
    assert ne.begin_native_dispatch(None, **COMMON, executor_id="bad")["legacy"]
    assert ne.claim_native_running(None, **COMMON, executor_id="bad")["allowed"]
    assert ne.mark_native_unknown(None, **COMMON, reason="bad")["legacy"]
    assert ne.settle_native(None, **COMMON, status="bad", facts=None)["legacy"]
    assert ne.release_native(None, **COMMON, status="bad", source="bad")["legacy"]


def test_existing_key_returned_before_current_policy_or_new_spec(enabled, monkeypatch):
    prior = operation(state="succeeded")
    monkeypatch.setattr(ne.core, "lookup", Mock(return_value=prior))
    fail = Mock(side_effect=AssertionError("must not read replacement policy or reserve"))
    monkeypatch.setattr(entitlement_config, "configured_policy", fail)
    monkeypatch.setattr(ne.core, "reserve", fail)
    assert ne.reserve_native(None, **COMMON, idempotency_key="stable-key", request_fingerprint="hash", spec=None) == {"created": False, "operation": prior}


@pytest.mark.parametrize("state,executor,replay,generation,allowed", [
    ("running", "executor-a", True, 2, True),
    ("running", "executor-a", False, 2, False),
    ("running", "executor-b", True, 2, False),
    ("running", "executor-a", True, 1, False),
    ("unknown", "executor-a", True, 2, False),
    ("succeeded", "executor-a", True, 2, False),
])
def test_only_native_fenced_original_running_grant_replays(enabled, monkeypatch, state, executor, replay, generation, allowed):
    monkeypatch.setattr(ne.core, "get_by_native", Mock(return_value=operation(state=state)))
    transition = Mock(side_effect=AssertionError("replay must not claim or increment generation"))
    monkeypatch.setattr(ne.core, "claim_running", transition)
    result = ne.claim_native_running(None, **COMMON, executor_id=executor,
        allow_authorized_replay=replay, expected_generation=generation)
    assert result["allowed"] is allowed
    transition.assert_not_called()


def test_first_claim_uses_current_cas_and_never_allows_failed_transition(enabled, monkeypatch):
    op = operation()
    monkeypatch.setattr(ne.core, "get_by_native", Mock(return_value=op))
    claim = Mock(return_value={"transitioned": False, "operation": operation(state="running")})
    monkeypatch.setattr(ne.core, "claim_running", claim)
    assert not ne.claim_native_running(None, **COMMON, executor_id="executor-a")["allowed"]
    claim.assert_called_once_with(None, owner_user_id="owner-a", operation_id="operation-a", generation=2, executor_id="executor-a")


@pytest.mark.parametrize("state", ["succeeded", "released"])
def test_terminal_result_not_reversed_by_late_unknown_failure(enabled, monkeypatch, state):
    monkeypatch.setattr(ne.core, "get_by_native", Mock(return_value=operation(state=state)))
    forbidden = Mock(side_effect=AssertionError("terminal operation must be sticky"))
    for name in ("settle", "release", "mark_unknown", "get_policy"):
        monkeypatch.setattr(ne.core, name, forbidden)
    assert not ne.mark_native_unknown(None, **COMMON, reason="late_ack")["transitioned"]
    assert not ne.release_native(None, **COMMON, status="failed", source="online_native_terminal")["transitioned"]
    assert not ne.settle_native(None, **COMMON, status="completed", facts={})["transitioned"]


@pytest.mark.parametrize("unknown", [True, False])
def test_settlement_uses_frozen_policy_and_unknown_retains(enabled, monkeypatch, unknown):
    op = operation(state="running")
    monkeypatch.setattr(ne.core, "get_by_native", Mock(return_value=op))
    frozen = {"version": "frozen-v1"}
    get_policy = Mock(return_value=frozen)
    monkeypatch.setattr(ne.core, "get_policy", get_policy)
    monkeypatch.setattr(entitlement_config, "configured_policy", Mock(side_effect=AssertionError("new policy not relevant")))
    qualify = Mock(return_value={"terminal": not unknown, "outcome": "unknown" if unknown else "service_completed", "qualified": not unknown, "reason": "test"})
    monkeypatch.setattr(entitlement_policy, "qualify", qualify)
    settle, mark = Mock(), Mock()
    monkeypatch.setattr(ne.core, "settle", settle)
    monkeypatch.setattr(ne.core, "mark_unknown", mark)
    ne.settle_native(None, **COMMON, status="completed", facts={"actual": True})
    get_policy.assert_called_once_with(None, policy_version="frozen-v1")
    qualify.assert_called_once_with(frozen, "online_knowledge", op["spec"], {"actual": True}, "completed")
    if unknown:
        settle.assert_not_called()
        mark.assert_called_once()
    else:
        mark.assert_not_called()
        assert settle.call_args.kwargs["result_ref"] == {"module": "online_knowledge", "native_task_id": "native-a"}


def test_release_proof_keeps_raw_native_identity_and_server_source(enabled, monkeypatch):
    monkeypatch.setattr(ne.core, "get_by_native", Mock(return_value=operation()))
    release = Mock()
    monkeypatch.setattr(ne.core, "release", release)
    ne.release_native(None, **COMMON, status="failed", source="online_native_recovery")
    assert release.call_args.kwargs["proof"] == {"kind": "native_terminal", "module": "online_knowledge",
        "native_task_id": "native-a", "native_status": "failed", "source": "online_native_recovery"}


@pytest.mark.parametrize("value", [None, "", "short", "bad key with spaces", "x" * 129])
def test_stable_key_validation(value):
    with pytest.raises(ne.core.EntitlementConflict):
        ne.require_idempotency_key(value)


def test_existing_batch_short_key_contract_is_not_rewritten(enabled, monkeypatch):
    lookup = Mock(return_value=None)
    monkeypatch.setattr(ne.core, "lookup", lookup)
    assert ne.lookup_native(None, owner_user_id="owner-a", module="polymerization_batch",
        idempotency_key="a", request_fingerprint="hash") is None
    assert lookup.call_args.kwargs["idempotency_key"] == "a"


def test_operation_header_state_uses_existing_dto_without_another_read(enabled):
    from types import SimpleNamespace
    request = SimpleNamespace(state=SimpleNamespace())  # No app/connection exists.
    ne.attach_native_operation(request, **COMMON, operation=operation(state="unknown"))
    assert request.state.usage_operation_id == "operation-a"
    assert request.state.usage_operation_state == "unknown"


def test_disabled_response_handle_never_reads_request_app(monkeypatch):
    monkeypatch.setattr(ne, "enabled", lambda: False)
    ne.attach_native_operation(None, **COMMON)
