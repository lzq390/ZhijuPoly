"""Finite, exact-owner capabilities for the approved isolated acceptance entry.

This is not an operations role. No credential or connection string belongs in
the public policy. Default deployments never load this policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from urllib.parse import urlsplit


SCOPE_ID = "acceptance-a-20261019"
ORIGIN = "http://127.0.0.2:38965"
OWNERS = ("b57ded4a-6ee7-4200-acb3-e7f86fbe1016", "f2a3e9cf-2978-4138-9c79-ced36861d7f3")
LAB_DATABASES = ("nexpoly_accept_r2_lab_a_20261019", "nexpoly_accept_r2_lab_b_20261019")
APPROVED_AT = datetime(2026, 10, 19, 2, 53, 34, tzinfo=timezone.utc)
DEADLINE = datetime(2026, 10, 20, 18, 53, 34, tzinfo=timezone.utc)
FEATURE_CAPABILITIES = {
    "lab_data": frozenset({"lab.data.read", "lab.data.write", "lab.use"}),
    "gpu_status": frozenset({"dev_gpu.status.read"}),
    "home": frozenset(),  # Reserved; no native A/B Home contract is admitted.
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class IsolatedAcceptancePolicy:
    scope_id: str
    features: frozenset[str]
    expires_at: datetime

    def __post_init__(self):
        if (self.scope_id != SCOPE_ID or type(self.features) is not frozenset
                or not self.features or not self.features <= FEATURE_CAPABILITIES.keys()
                or not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None
                or not APPROVED_AT < self.expires_at <= DEADLINE):
            raise ValueError("Invalid finite isolated acceptance policy")

    @classmethod
    def from_json(cls, raw: str):
        def strict_object(pairs):
            data = {}
            for key, value in pairs:
                if key in data:
                    raise ValueError("Duplicate isolated acceptance policy field")
                data[key] = value
            return data

        try:
            if not isinstance(raw, str) or len(raw) > 2048:
                raise ValueError
            data = json.loads(raw, object_pairs_hook=strict_object)
            if (type(data) is not dict or set(data) != {"scope_id", "features", "expires_at"}
                    or type(data["features"]) is not list
                    or any(type(item) is not str for item in data["features"])
                    or len(data["features"]) != len(set(data["features"]))
                    or type(data["expires_at"]) is not str):
                raise ValueError
            expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
            return cls(data["scope_id"], frozenset(data["features"]), expires)
        except (TypeError, ValueError, KeyError):
            raise ValueError("Invalid finite isolated acceptance policy") from None

    def allows(self, owner_id: str, capability: str, *, now: datetime | None = None) -> bool:
        observed = now if now is not None else utc_now()
        return (owner_id in OWNERS and observed.tzinfo is not None
                and APPROVED_AT <= observed < self.expires_at
                and any(capability in FEATURE_CAPABILITIES[item] for item in self.features))


def entry_matches(scope, settings) -> bool:
    """Neither forwarded headers nor a Host override select the test entry."""
    policy = getattr(settings, "isolated_ab_session_policy", None)
    if policy is None or policy.origin != ORIGIN or policy.user_ids != OWNERS:
        return False
    raw = scope.get("headers", ())
    names = [key.lower() for key, _ in raw]
    forbidden = {b"forwarded", b"x-forwarded-for", b"x-forwarded-host", b"x-forwarded-proto", b"x-real-ip"}
    if names.count(b"host") != 1 or names.count(b"origin") > 1 or forbidden.intersection(names):
        return False
    headers = {key.lower(): value for key, value in raw}
    return (scope.get("scheme") == "http"
            and (scope.get("client") or ("", 0))[0] == policy.proxy_peer
            and headers[b"host"].decode("latin-1") == urlsplit(ORIGIN).netloc
            and headers.get(b"origin", ORIGIN.encode("ascii")).decode("latin-1") == ORIGIN)
