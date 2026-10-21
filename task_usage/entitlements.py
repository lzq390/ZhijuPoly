"""Worker-safe transaction-local entitlement operations (feature default off).
Callers must never execute again unless created/transitioned is explicitly true.
No function commits, opens connections, or reads identity/auth credentials.
"""
from __future__ import annotations
import os
from typing import Any

MIGRATION_VERSION = "0022_usage_entitlements"
STATES = frozenset({"reserved", "dispatching", "running", "unknown", "succeeded", "released"})
TERMINAL_STATES = frozenset({"succeeded", "released"})

class EntitlementError(RuntimeError):
    code = "entitlement_error"
    def __init__(self, message=None, *, code=None):
        self.code = code or self.code
        super().__init__(message or self.code)

class EntitlementConflict(EntitlementError):
    code = "idempotency_conflict"

class EntitlementLimitExceeded(EntitlementError):
    code = "entitlement_limit_exceeded"

class EntitlementPolicyError(EntitlementError):
    code = "entitlement_policy_invalid"

def enabled() -> bool:
    return os.environ.get("USAGE_ENTITLEMENTS_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}

entitlements_enabled = enabled

def _disabled(*, reserve=False):
    return {"disabled": True, "created" if reserve else "transitioned": False, "operation": None}

def _implementation():
    # Internal implementation is installed with migration 0022 before enabling.
    from task_usage import _entitlement_store
    return _entitlement_store

def lookup(connection, *, owner_user_id, module, idempotency_key, request_fingerprint):
    if not enabled(): return None
    return _implementation().lookup(connection, owner_user_id=owner_user_id, module=module,
        idempotency_key=idempotency_key, request_fingerprint=request_fingerprint)

def reserve(connection, *, owner_user_id, module, idempotency_key, request_fingerprint,
            policy_version, spec, native_task_id=None, now=None):
    if not enabled(): return _disabled(reserve=True)
    return _implementation().reserve(connection, owner_user_id=owner_user_id, module=module,
        idempotency_key=idempotency_key, request_fingerprint=request_fingerprint,
        policy_version=policy_version, spec=spec, native_task_id=native_task_id, now=now)

def bind_native(connection, *, owner_user_id, operation_id, generation, native_task_id):
    if not enabled(): return _disabled()
    return _implementation().transition(connection, action="bind_native",
        owner_user_id=owner_user_id, operation_id=operation_id, generation=generation,
        native_task_id=native_task_id)

def begin_dispatch(connection, *, owner_user_id, operation_id, generation, executor_id):
    if not enabled(): return _disabled()
    return _implementation().transition(connection, action="begin_dispatch",
        owner_user_id=owner_user_id, operation_id=operation_id, generation=generation,
        executor_id=executor_id)

def claim_running(connection, *, owner_user_id, operation_id, generation, executor_id):
    if not enabled(): return _disabled()
    return _implementation().transition(connection, action="claim_running",
        owner_user_id=owner_user_id, operation_id=operation_id, generation=generation,
        executor_id=executor_id)

def settle(connection, *, owner_user_id, operation_id, generation, outcome, qualified,
           result_json=None, result_ref=None):
    if not enabled(): return _disabled()
    return _implementation().transition(connection, action="settle",
        owner_user_id=owner_user_id, operation_id=operation_id, generation=generation,
        outcome=outcome, qualified=qualified, result_json=result_json, result_ref=result_ref)

def release(connection, *, owner_user_id, operation_id, generation, reason, proof):
    if not enabled(): return _disabled()
    return _implementation().transition(connection, action="release",
        owner_user_id=owner_user_id, operation_id=operation_id, generation=generation,
        reason=reason, proof=proof)

def mark_unknown(connection, *, owner_user_id, operation_id, generation, reason):
    if not enabled(): return _disabled()
    return _implementation().transition(connection, action="mark_unknown",
        owner_user_id=owner_user_id, operation_id=operation_id, generation=generation, reason=reason)

def get(connection, *, owner_user_id, operation_id):
    if not enabled(): return None
    return _implementation().get(connection, owner_user_id=owner_user_id, operation_id=operation_id)

def get_by_native(connection, *, owner_user_id, module, native_task_id):
    if not enabled(): return None
    return _implementation().get_by_native(connection, owner_user_id=owner_user_id,
        module=module, native_task_id=native_task_id)

def list_operations(connection, *, owner_user_id, module=None, limit=50, offset=0):
    if not enabled(): return []
    return _implementation().list_operations(connection, owner_user_id=owner_user_id,
        module=module, limit=limit, offset=offset)

def summary(connection, *, owner_user_id, policy_version=None, now=None):
    if not enabled(): return {"enabled": False, "pools": []}
    return _implementation().summary(connection, owner_user_id=owner_user_id, policy_version=policy_version, now=now)

def install_policy(connection, policy):
    """Explicit service/admin configuration call; never invoked by reserve."""
    if not enabled(): return {"disabled": True, "installed": False}
    return _implementation().install_policy(connection, policy)

def set_owner_tier(connection, *, owner_user_id, tier):
    """Explicit service/admin configuration of this new entitlement only."""
    if not enabled(): return {"disabled": True, "changed": False}
    return _implementation().set_owner_tier(connection, owner_user_id=owner_user_id, tier=tier)

def get_owner_tier(connection, *, owner_user_id):
    if not enabled(): return "member"
    return _implementation().get_owner_tier(connection, owner_user_id=owner_user_id)

def get_policy(connection, *, policy_version):
    if not enabled(): return None
    return _implementation().get_policy(connection, policy_version=policy_version)

def record_request(connection, *, owner_user_id, module, request_id, phase,
                   occurred_at, http_status=None, outcome=None, operation_id=None):
    if not enabled(): return {"disabled": True, "inserted": False}
    return _implementation().record_request(connection, owner_user_id=owner_user_id,
        module=module, request_id=request_id, phase=phase, occurred_at=occurred_at,
        http_status=http_status, outcome=outcome, operation_id=operation_id)

def list_request_counts(connection, *, owner_user_id, start=None, end=None, module=None, policy_version=None):
    if not enabled(): return {"enabled": False, "request_count": 0, "finished_count": 0, "by_status": {}}
    return _implementation().list_request_counts(connection, owner_user_id=owner_user_id,
        start=start, end=end, module=module, policy_version=policy_version)


def resolve_access_tier(connection, *, owner_user_id):
    """Authorization read: always on, even when quota collection is disabled."""
    from task_usage.entitlement_access import resolve_access_tier as resolve
    return resolve(connection, owner_user_id=owner_user_id)
