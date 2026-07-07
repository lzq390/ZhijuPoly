from __future__ import annotations

import http.client
import json as json_lib
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

import requests


class MonomerMdWorkerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MonomerMdWorkerSubmission:
    worker_id: str | None = None
    worker_job_id: str | None = None
    worker_version: str | None = None


@dataclass(frozen=True, slots=True)
class MonomerMdWorkerSubmitPayload:
    job_id: str
    smiles: str
    canonical_smiles: str
    steps: int

    def to_json(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "smiles": self.smiles,
            "canonical_smiles": self.canonical_smiles,
            "steps": self.steps,
        }


class MonomerMdWorkerClient:
    def __init__(self, *, base_url: str, timeout_seconds: float) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme == "http+unix" and parsed.netloc:
            self.session = _UnixSocketWorkerSession(socket_path=unquote(parsed.netloc))
        elif parsed.scheme in {"http", "https"} and parsed.netloc:
            self.session = requests.Session()
        else:
            raise MonomerMdWorkerError(
                "MONOMER_MD_WORKER_BASE_URL must be an http(s) or http+unix URL"
            )
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def get_health(self) -> dict[str, Any]:
        try:
            response = self.session.get(
                f"{self.base_url}/health",
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise MonomerMdWorkerError("monomer MD worker is not reachable") from exc

        if response.status_code >= 400:
            detail = _safe_response_detail(response)
            raise MonomerMdWorkerError(f"monomer MD worker health check failed: {detail}")
        return _response_json_object(response, "health check")

    def submit_job(self, payload: MonomerMdWorkerSubmitPayload) -> MonomerMdWorkerSubmission:
        try:
            response = self.session.post(
                f"{self.base_url}/jobs",
                json=payload.to_json(),
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise MonomerMdWorkerError("monomer MD worker is not reachable") from exc

        if response.status_code >= 400:
            detail = _safe_response_detail(response)
            raise MonomerMdWorkerError(f"monomer MD worker rejected the job: {detail}")

        data = _response_json_object(response, "job submission")
        worker_job_id = _optional_str(data.get("worker_job_id") or data.get("job_id"))
        if worker_job_id is None:
            raise MonomerMdWorkerError(
                "monomer MD worker accepted the job without returning a job id"
            )
        return MonomerMdWorkerSubmission(
            worker_id=_optional_str(data.get("worker_id")),
            worker_job_id=worker_job_id,
            worker_version=_optional_str(data.get("worker_version")),
        )


def _safe_response_detail(response: requests.Response) -> str:
    try:
        data: Any = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(detail, str) and detail.strip():
        return detail.strip()[:240]
    return f"HTTP {response.status_code}"


class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


class _UnixSocketWorkerSession:
    def __init__(self, *, socket_path: str) -> None:
        self.socket_path = socket_path

    def get(self, url: str, *, timeout: float) -> requests.Response:
        return self._request("GET", url, timeout=timeout)

    def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        timeout: float,
    ) -> requests.Response:
        body = json_lib.dumps(json).encode("utf-8")
        return self._request(
            "POST",
            url,
            timeout=timeout,
            body=body,
            headers={"Content-Type": "application/json"},
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        timeout: float,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        parsed = urlparse(url)
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"

        request_headers = {"Host": "monomer-md-worker"}
        request_headers.update(headers or {})
        if body is not None:
            request_headers["Content-Length"] = str(len(body))

        connection = _UnixSocketHTTPConnection(self.socket_path, timeout=timeout)
        try:
            connection.request(method, target, body=body, headers=request_headers)
            raw_response = connection.getresponse()
            content = raw_response.read()
        except (OSError, http.client.HTTPException) as exc:
            raise requests.ConnectionError(str(exc)) from exc
        finally:
            connection.close()

        response = requests.Response()
        response.status_code = raw_response.status
        response.reason = raw_response.reason
        response.headers.update(dict(raw_response.getheaders()))
        response._content = content
        response.url = url
        return response


def _response_json_object(response: requests.Response, context: str) -> dict[str, Any]:
    if not response.content:
        return {}
    try:
        data: Any = response.json()
    except ValueError as exc:
        raise MonomerMdWorkerError(
            f"monomer MD worker returned invalid JSON for {context}"
        ) from exc
    if not isinstance(data, dict):
        raise MonomerMdWorkerError(
            f"monomer MD worker returned a non-object JSON payload for {context}"
        )
    return data


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
