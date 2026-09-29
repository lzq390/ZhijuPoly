"""Small service-authenticated Backend callback; the DFT Worker needs no DB access."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .engine import ComputationCancelled


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "authorization redirects are forbidden", headers, fp)


def _open(request, *, timeout):
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


class StartAuthorizer:
    def __init__(self, *, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        if self.base_url:
            url = urlsplit(self.base_url)
            if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password or url.query or url.fragment:
                raise ValueError("DFT start authorization requires a trusted HTTP(S) Backend URL")
            if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("unencrypted DFT service authorization is limited to loopback")

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def __call__(self, snapshot) -> None:
        if not self.configured:
            raise RuntimeError("DFT start authorization is not configured")
        payload = json.dumps({
            "attempt_token": snapshot.attempt_token,
            "request_sha256": snapshot.request_sha256,
            "enqueue_sequence": snapshot.enqueue_sequence,
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/internal/monomer-dft/jobs/{snapshot.job_id}/authorize-start",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token},
            method="POST",
        )
        # An ambiguous response is retried with exactly the same durable attempt.
        # No computation can run unless an explicit response is obtained.
        for retry in range(3):
            try:
                with _open(request, timeout=3) as response:
                    body = json.loads(response.read(2049))
                if body.get("start_authorization_version") != 1:
                    raise RuntimeError("DFT start authorization protocol mismatch")
                if body.get("authorized") is not True:
                    raise ComputationCancelled("DFT execution authorization denied")
                return
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if retry == 2:
                    raise RuntimeError("DFT start authorization is unavailable") from exc
