from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


CROSS_SITE_BLOCK_DETAIL = "cross-site browser requests are not allowed"
PROTECTED_PATH_PREFIXES = ("/api/",)
TRUSTED_FETCH_SITES = {"none", "same-origin", "same-site"}


def _origin_from_url(value: str | None) -> str | None:
    if not value:
        return None

    parsed = urlsplit(value.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _request_origin(request: Request) -> str | None:
    forwarded_proto = request.headers.get("x-forwarded-proto")
    scheme = forwarded_proto.split(",", 1)[0].strip() if forwarded_proto else request.url.scheme

    forwarded_host = request.headers.get("x-forwarded-host")
    host = forwarded_host.split(",", 1)[0].strip() if forwarded_host else request.headers.get("host", "")
    if not host:
        return None

    return f"{scheme.lower()}://{host.lower()}"


def _allowed_origins(request: Request) -> set[str]:
    settings = getattr(request.app.state, "settings", None)
    configured_origins: Iterable[str] = getattr(settings, "allowed_origins_list", [])
    origins = {
        normalized
        for normalized in (_origin_from_url(origin) for origin in configured_origins)
        if normalized is not None
    }

    request_origin = _request_origin(request)
    if request_origin is not None:
        origins.add(request_origin)
    return origins


def _is_trusted_origin(value: str | None, request: Request) -> bool:
    origin = _origin_from_url(value)
    if origin is None:
        return False
    return "*" in getattr(request.app.state.settings, "allowed_origins_list", []) or origin in _allowed_origins(request)


def _is_protected_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in PROTECTED_PATH_PREFIXES)


class BrowserCrossSiteProtectionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not _is_protected_path(request.url.path):
            return await call_next(request)

        fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
        if fetch_site == "cross-site":
            return JSONResponse(status_code=403, content={"detail": CROSS_SITE_BLOCK_DETAIL})
        if fetch_site in TRUSTED_FETCH_SITES:
            return await call_next(request)

        origin = request.headers.get("origin")
        if origin and not _is_trusted_origin(origin, request):
            return JSONResponse(status_code=403, content={"detail": CROSS_SITE_BLOCK_DETAIL})

        referer = request.headers.get("referer")
        if referer and not _is_trusted_origin(referer, request):
            return JSONResponse(status_code=403, content={"detail": CROSS_SITE_BLOCK_DETAIL})

        return await call_next(request)
