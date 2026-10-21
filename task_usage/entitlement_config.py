"""Explicit server configuration; never accepts a client-selected policy."""
from __future__ import annotations
from functools import lru_cache
from copy import deepcopy
import json
import os
from pathlib import Path

from task_usage.entitlements import enabled, EntitlementPolicyError

@lru_cache(maxsize=4)
def _load(path: str, allow_example: bool):
    from task_usage.entitlement_policy import load_policy
    source = Path(path)
    if not source.is_file() or source.stat().st_size > 1024 * 1024:
        raise EntitlementPolicyError("A bounded explicit entitlement policy file is required")
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
        policy = load_policy(document)
    except (ValueError, OSError, TypeError) as exc:
        raise EntitlementPolicyError("Configured entitlement policy is invalid") from exc
    if policy.get("example", False) and not allow_example:
        raise EntitlementPolicyError("Example policies require explicit development opt-in")
    return policy

def configured_policy():
    if not enabled():
        return None
    path = os.environ.get("USAGE_ENTITLEMENTS_POLICY_FILE", "").strip()
    if not path:
        raise EntitlementPolicyError("USAGE_ENTITLEMENTS_POLICY_FILE is required")
    allow_example = os.environ.get("USAGE_ENTITLEMENTS_ALLOW_EXAMPLE", "").lower() in {"1","true","yes","on"}
    return deepcopy(_load(path, allow_example))

def clear_configuration_cache():
    _load.cache_clear()
