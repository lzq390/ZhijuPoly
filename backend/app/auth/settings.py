from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class AuthSettings:
    application_dsn: str
    auth_dsn: str
    service_dsn: str
    cookie_name: str = "nexpoly_session"
    cookie_secure: bool = True
    session_hours: int = 12
    allowed_origins: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, settings):
        origins = tuple(settings.allowed_origins_list)
        if "*" in origins:
            raise ValueError("Cookie authentication requires explicit allowed origins")
        secure = os.getenv("AUTH_COOKIE_SECURE", "true").lower() not in {"0", "false"}
        cookie_name = os.getenv("AUTH_COOKIE_NAME", "nexpoly_session" if secure else "nexpoly_dev_session")
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
        if not secure and any(urlsplit(origin).hostname not in {"localhost", "127.0.0.1", "::1", "testserver"} and origin not in dev_http_origins for origin in origins):
            raise ValueError("Non-Secure authentication cookies are restricted to loopback development origins")
        dsn = str(settings.app_postgres_dsn)
        return cls(
            application_dsn=dsn,
            auth_dsn=os.getenv("AUTH_POSTGRES_DSN", dsn),
            service_dsn=os.getenv("APP_SERVICE_POSTGRES_DSN", dsn),
            cookie_name=cookie_name,
            cookie_secure=secure,
            allowed_origins=origins,
        )
