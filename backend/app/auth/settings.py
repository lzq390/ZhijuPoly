from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID


ISOLATED_AB_COOKIE_NAME = "nexpoly_dev_isolated_ab_session"


@dataclass(frozen=True)
class IsolatedABSessionPolicy:
    """Public identity configuration for one separate loopback acceptance entry."""

    enabled: bool
    origin: str
    user_ids: tuple[str, str]
    proxy_peer: str

    def __post_init__(self):
        if self.enabled is not True:
            raise ValueError("Isolated A/B sessions require explicit enablement")
        try:
            parsed = urlsplit(self.origin)
            port = parsed.port
        except (TypeError, ValueError):
            raise ValueError("Invalid isolated A/B origin") from None
        # Cookies do not isolate ports. A separate numeric loopback host keeps
        # this cookie away from the existing 127.0.0.1 / localhost entry.
        if (not isinstance(self.origin, str) or port is None or not 1024 <= port <= 65535
                or port == 36965 or self.origin != f"http://127.0.0.2:{port}"):
            raise ValueError("Isolated A/B origin must use a separate 127.0.0.2 HTTP entry")
        if type(self.user_ids) is not tuple or len(self.user_ids) != 2:
            raise ValueError("Isolated A/B sessions require exactly two distinct ordinary user IDs")
        try:
            valid = all(isinstance(value, str) and str(UUID(value)) == value
                        and UUID(value).int not in {0, 1} for value in self.user_ids)
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid or len(set(self.user_ids)) != 2:
            raise ValueError("Isolated A/B sessions require exactly two distinct ordinary user IDs")
        try:
            address = ipaddress.ip_address(self.proxy_peer)
            local_proxy = address.is_loopback or (
                address.version == 4 and any(address in ipaddress.ip_network(network)
                    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
        except (TypeError, ValueError):
            local_proxy = False
        if not local_proxy or not isinstance(self.proxy_peer, str) or str(address) != self.proxy_peer:
            raise ValueError("Isolated A/B sessions require one exact local proxy peer address")

    @classmethod
    def from_json(cls, raw: str):
        def strict_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate isolated A/B policy fields")
                result[key] = value
            return result

        if len(raw) > 4096:
            raise ValueError("Invalid isolated A/B session policy")
        try:
            data = json.loads(raw, object_pairs_hook=strict_object)
        except (TypeError, ValueError):
            raise ValueError("Invalid isolated A/B session policy") from None
        if (type(data) is not dict or set(data) != {"enabled", "origin", "user_ids", "proxy_peer"}
                or type(data["user_ids"]) is not list):
            raise ValueError("Isolated A/B policy permits only enabled, origin, user_ids and proxy_peer")
        return cls(enabled=data["enabled"], origin=data["origin"], user_ids=tuple(data["user_ids"]),
                   proxy_peer=data["proxy_peer"])


@dataclass(frozen=True)
class AuthSettings:
    application_dsn: str
    auth_dsn: str
    service_dsn: str
    cookie_name: str = "nexpoly_session"
    cookie_secure: bool = True
    session_hours: int = 12
    allowed_origins: tuple[str, ...] = ()
    runtime_environment: str = ""
    isolated_ab_session_policy: IsolatedABSessionPolicy | None = None

    def __post_init__(self):
        policy = self.isolated_ab_session_policy
        if policy is not None and (
            type(self.session_hours) is not int or self.session_hours != 12
            or type(policy) is not IsolatedABSessionPolicy or self.runtime_environment != "dev"
            or self.cookie_name != ISOLATED_AB_COOKIE_NAME or self.cookie_secure is not False
            or self.allowed_origins != (policy.origin,)
        ):
            raise ValueError("Isolated A/B sessions require a separate development entry and cookie")

    def new_session_max_age(self, user_id: str, *, peer: str, isolated_origin: str | None = None) -> int:
        policy = self.isolated_ab_session_policy
        if (policy is not None and isolated_origin == policy.origin and user_id in policy.user_ids
                and peer == policy.proxy_peer):
            return 36 * 3600
        return self.session_hours * 3600

    @classmethod
    def from_settings(cls, settings):
        origins = tuple(settings.allowed_origins_list)
        if "*" in origins:
            raise ValueError("Cookie authentication requires explicit allowed origins")
        secure = os.getenv("AUTH_COOKIE_SECURE", "true").lower() not in {"0", "false"}
        cookie_name = os.getenv("AUTH_COOKIE_NAME", "nexpoly_session" if secure else "nexpoly_dev_session")
        raw_policy = os.getenv("AUTH_ISOLATED_AB_SESSION_POLICY", "")
        policy = IsolatedABSessionPolicy.from_json(raw_policy) if raw_policy else None
        if policy is not None and os.getenv("FORWARDED_ALLOW_IPS") != "":
            raise ValueError("Isolated A/B sessions require explicitly disabled proxy-header trust")
        dev_http_origins = frozenset(value.strip() for value in os.getenv("AUTH_DEV_HTTP_ORIGINS", "").split(",") if value.strip())
        if dev_http_origins:
            # Explicitly provisioned 9001 development access only. Production
            # keeps Secure cookies and never inherits a permissive wildcard.
            if secure or getattr(settings, "gpu_broker_environment", None) != "dev" or not cookie_name.startswith("nexpoly_dev_"):
                raise ValueError("HTTP origin exceptions require the development runtime and development cookie")
            for origin in dev_http_origins:
                parsed = urlsplit(origin)
                if (parsed.scheme != "http" or not parsed.hostname or "*" in origin
                        or parsed.port != 9001 or parsed.username or parsed.password
                        or parsed.path or parsed.query or parsed.fragment
                        or any(character.isspace() for character in origin)
                        or origin not in origins):
                    raise ValueError("Development HTTP origins must be exact allowed HTTP origins on port 9001")
        if not secure and any(urlsplit(origin).hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}
                              and origin not in dev_http_origins
                              and (policy is None or origin != policy.origin) for origin in origins):
            raise ValueError("Non-Secure authentication cookies are restricted to loopback development origins")
        dsn = str(settings.app_postgres_dsn)
        return cls(
            application_dsn=dsn,
            auth_dsn=os.getenv("AUTH_POSTGRES_DSN", dsn),
            service_dsn=os.getenv("APP_SERVICE_POSTGRES_DSN", dsn),
            cookie_name=cookie_name,
            cookie_secure=secure,
            allowed_origins=origins,
            runtime_environment=getattr(settings, "gpu_broker_environment", ""),
            isolated_ab_session_policy=policy,
        )
