from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

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
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MonomerMdWorkerError(
                "MONOMER_MD_WORKER_BASE_URL must be an http(s) URL"
            )
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def get_health(self) -> dict[str, Any]:
        try:
            response = requests.get(
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
            response = requests.post(
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
