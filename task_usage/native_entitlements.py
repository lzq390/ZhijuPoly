"""Transaction-local adapters for the four native task repositories.

Native stores prove ownership, attempt fencing and safe terminal state first.
This module never discovers dead executors, opens a connection, or commits.
"""
from __future__ import annotations

import hashlib
import json
import re
from task_usage import entitlements as core


enabled = core.enabled
TERMINAL_STATES = frozenset({"succeeded", "released"})


def request_fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def require_idempotency_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", value):
        raise core.EntitlementConflict("A stable Idempotency-Key is required", code="idempotency_key_required")
    return value


def lookup_native(connection, *, owner_user_id, module, idempotency_key, request_fingerprint):
    if not enabled():
        return None
    # DFT and batch retain their existing native key contract; new MD/online
    # HTTP admission validates its stable key before reaching this adapter.
    return core.lookup(connection, owner_user_id=owner_user_id, module=module,
                       idempotency_key=idempotency_key, request_fingerprint=request_fingerprint)


def get_native(connection, *, owner_user_id, module, native_task_id):
    if not enabled():
        return None
    return core.get_by_native(connection, owner_user_id=owner_user_id, module=module,
                              native_task_id=str(native_task_id))


def reserve_native(connection, *, owner_user_id, module, idempotency_key,
                   request_fingerprint, native_task_id, spec):
    if not enabled():
        return {"created": False, "operation": None, "disabled": True}
    previous = lookup_native(connection, owner_user_id=owner_user_id, module=module,
                             idempotency_key=idempotency_key, request_fingerprint=request_fingerprint)
    if previous is not None:
        return {"created": False, "operation": previous}
    from task_usage.entitlement_config import configured_policy
    from task_usage.entitlement_policy import normalize_spec
    policy = configured_policy()
    normalized = normalize_spec(policy, module, spec)
    return core.reserve(connection, owner_user_id=owner_user_id, module=module,
                        idempotency_key=idempotency_key, request_fingerprint=request_fingerprint,
                        policy_version=policy["version"], spec=normalized["canonical_spec"],
                        native_task_id=str(native_task_id))


def _advance(connection, action, *, owner_user_id, module, native_task_id,
             executor_id, allow_authorized_replay=False, expected_generation=None):
    operation = get_native(connection, owner_user_id=owner_user_id, module=module,
                           native_task_id=native_task_id)
    if operation is None:
        return {"allowed": True, "legacy": True, "operation": None}
    generation = operation["generation"]
    if expected_generation is not None and generation != expected_generation:
        return {"allowed": False, "legacy": False, "operation": operation}
    # Only a native identity-fenced, already-authorized callback can replay the
    # original grant. This is never permission to invoke a generic executor again.
    if (action == "claim_running" and allow_authorized_replay
            and operation["state"] == "running" and operation["executor_id"] == executor_id):
        return {"allowed": True, "legacy": False, "replayed": True, "operation": operation}
    if operation["state"] != ("reserved" if action == "begin_dispatch" else "dispatching"):
        return {"allowed": False, "legacy": False, "operation": operation}
    result = getattr(core, action)(connection, owner_user_id=owner_user_id,
        operation_id=operation["operation_id"], generation=generation, executor_id=executor_id)
    return {**result, "allowed": bool(result["transitioned"]), "legacy": False}


def begin_native_dispatch(connection, *, owner_user_id, module, native_task_id, executor_id,
                          expected_generation=None):
    return _advance(connection, "begin_dispatch", owner_user_id=owner_user_id, module=module,
                    native_task_id=native_task_id, executor_id=executor_id,
                    expected_generation=expected_generation)


def claim_native_running(connection, *, owner_user_id, module, native_task_id, executor_id,
                         allow_authorized_replay=False, expected_generation=None):
    return _advance(connection, "claim_running", owner_user_id=owner_user_id, module=module,
                    native_task_id=native_task_id, executor_id=executor_id,
                    allow_authorized_replay=allow_authorized_replay,
                    expected_generation=expected_generation)


def mark_native_unknown(connection, *, owner_user_id, module, native_task_id, reason):
    operation = get_native(connection, owner_user_id=owner_user_id, module=module,
                           native_task_id=native_task_id)
    if operation is None or operation["state"] in TERMINAL_STATES:
        return {"transitioned": False, "operation": operation, "legacy": operation is None}
    return core.mark_unknown(connection, owner_user_id=owner_user_id,
        operation_id=operation["operation_id"], generation=operation["generation"], reason=reason)


def settle_native(connection, *, owner_user_id, module, native_task_id, status, facts,
                  result_ref=None):
    operation = get_native(connection, owner_user_id=owner_user_id, module=module,
                           native_task_id=native_task_id)
    if operation is None or operation["state"] in TERMINAL_STATES:
        return {"transitioned": False, "operation": operation, "legacy": operation is None}
    from task_usage.entitlement_policy import qualify
    policy = core.get_policy(connection, policy_version=operation["policy_version"])
    decision = qualify(policy, module, operation["spec"], facts, status)
    if decision["outcome"] == "unknown" or not decision["terminal"]:
        return core.mark_unknown(connection, owner_user_id=owner_user_id,
            operation_id=operation["operation_id"], generation=operation["generation"],
            reason=decision["reason"])
    return core.settle(connection, owner_user_id=owner_user_id,
        operation_id=operation["operation_id"], generation=operation["generation"],
        outcome=decision["outcome"], qualified=decision["qualified"],
        result_ref=result_ref or {"module": module, "native_task_id": str(native_task_id)})


def release_native(connection, *, owner_user_id, module, native_task_id, status, source,
                   never_dispatched=False, reason=None):
    operation = get_native(connection, owner_user_id=owner_user_id, module=module,
                           native_task_id=native_task_id)
    if operation is None or operation["state"] in TERMINAL_STATES:
        return {"transitioned": False, "operation": operation, "legacy": operation is None}
    proof = {"kind": "native_never_dispatched" if never_dispatched else "native_terminal",
             "module": module, "native_task_id": str(native_task_id),
             "native_status": status, "source": source}
    return core.release(connection, owner_user_id=owner_user_id,
        operation_id=operation["operation_id"], generation=operation["generation"],
        reason=reason or status, proof=proof)


def attach_native_operation(request, *, owner_user_id, module, native_task_id, operation=None):
    """Expose only the durable operation handle; middleware owns HTTP headers."""
    if not enabled():
        return
    if operation is None:
        operation = request.app.state.entitlement_repository.get_by_native(
            owner_user_id=owner_user_id, module=module, native_task_id=str(native_task_id))
    if operation is not None:
        request.state.usage_operation_id = str(operation["operation_id"])
        request.state.usage_operation_state = operation["state"]
