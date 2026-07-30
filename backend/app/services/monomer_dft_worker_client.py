from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import anyio
import httpx
from pydantic import ValidationError

from .monomer_dft_internal_models import (
    InternalWorkerArtifactDeletionResponse,
    InternalWorkerJobList,
    InternalWorkerJobPurgeResponse,
    InternalWorkerRequest,
    InternalWorkerSnapshot,
)
from .monomer_dft_repository import sanitize_public_json, sanitize_public_text


_SAFE_JOB_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_SAFE_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_WORKER_JSON_BYTES = 16 * 1024 * 1024
MAX_WORKER_ERROR_BYTES = 64 * 1024


class MonomerDftWorkerError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 503,
        code: str = "worker_unavailable",
        retryable: bool = True,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(sanitize_public_text(message, fallback="DFT worker is unavailable"))
        self.status_code = status_code
        self.code = code
        self.retryable = retryable
        self.details = sanitize_public_json(details)


@dataclass(slots=True)
class MonomerDftWorkerStream:
    response: httpx.Response

    @property
    def body_iterator(self):
        return self.response.aiter_bytes()

    @property
    def raw_body_iterator(self):
        return self.response.aiter_raw()

    async def close(self) -> None:
        await self.response.aclose()


class MonomerDftWorkerClient:
    def __init__(
        self,
        *,
        base_url: str,
        uds_path: str = "",
        timeout_seconds: float = 30.0,
        validation_limiter: anyio.CapacityLimiter,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MonomerDftWorkerError(
                "MONOMER_DFT_WORKER_BASE_URL must be an HTTP base URL",
                status_code=500,
                code="worker_configuration_error",
                retryable=False,
            )
        if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
            raise MonomerDftWorkerError(
                "MONOMER_DFT_WORKER_BASE_URL must not contain a path, query, or fragment",
                status_code=500,
                code="worker_configuration_error",
                retryable=False,
            )
        timeout = httpx.Timeout(max(1.0, float(timeout_seconds)))
        self._owns_client = client is None and bool(uds_path)
        if client is None and uds_path:
            transport = httpx.AsyncHTTPTransport(uds=uds_path)
            client = httpx.AsyncClient(
                base_url=base_url.rstrip("/"),
                transport=transport,
                timeout=timeout,
                follow_redirects=False,
            )
        self._client = client
        self._validation_limiter = validation_limiter
        self.base_url = base_url.rstrip("/")
        self.uses_uds = bool(uds_path)

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def health(self) -> dict[str, Any]:
        return await self._json_request("GET", "/health", operation="health check")

    async def capabilities(self) -> dict[str, Any]:
        return await self._json_request("GET", "/capabilities", operation="capabilities query")

    async def drain(self) -> dict[str, Any]:
        return await self._json_request("POST", "/drain", operation="drain request")

    async def resume(self) -> dict[str, Any]:
        return await self._json_request("POST", "/resume", operation="resume request")

    async def submit_job(self, job: dict[str, Any]) -> dict[str, Any]:
        validated_request, validated_payload = self._validated_job_request(job)
        response = await self._json_request(
            "POST",
            "/jobs",
            operation="job submission",
            json=validated_payload,
        )
        return await self._validate_snapshot(
            response,
            operation="job submission",
            expected_job_id=validated_request.job_id,
            expected_attempt_token=validated_request.attempt_token,
            expected_request_sha256=validated_request.request_sha256,
            expected_enqueue_sequence=validated_request.enqueue_sequence,
        )

    @staticmethod
    def _validated_job_request(
        job: dict[str, Any],
    ) -> tuple[InternalWorkerRequest, dict[str, Any]]:
        request = job.get("request")
        if not isinstance(request, dict):
            raise MonomerDftWorkerError(
                "DFT job is missing its scientific request",
                status_code=500,
                code="invalid_backend_job",
                retryable=False,
            )
        payload = {
            **request,
            "schema_version": 2,
            "job_id": str(job.get("job_id") or ""),
            "attempt_token": str(job.get("_attempt_token") or ""),
            "request_sha256": str(job.get("request_sha256") or ""),
            "enqueue_sequence": job.get("_enqueue_sequence"),
        }
        try:
            validated_request = InternalWorkerRequest.model_validate(payload)
        except ValidationError as exc:
            raise MonomerDftWorkerError(
                "DFT backend job does not satisfy the Worker request contract",
                status_code=500,
                code="invalid_backend_job",
                retryable=False,
            ) from exc
        validated_payload = validated_request.model_dump(mode="json")
        inactive_branch = (
            "optimization"
            if validated_request.calculation_type == "single_point"
            else "single_point"
        )
        validated_payload.pop(inactive_branch, None)
        return validated_request, validated_payload

    async def list_active_jobs(self) -> dict[str, Any]:
        response = await self._json_request(
            "GET",
            "/jobs",
            operation="active job query",
            params={"state": "active"},
        )
        try:
            jobs = await anyio.to_thread.run_sync(
                InternalWorkerJobList.model_validate,
                response,
                limiter=self._validation_limiter,
            )
        except ValidationError as exc:
            raise self._invalid_response("active job query") from exc
        return jobs.model_dump(mode="json")

    async def get_job(self, job_id: str) -> dict[str, Any]:
        response = await self._json_request(
            "GET",
            f"/jobs/{self._job_segment(job_id)}",
            operation="job query",
        )
        return await self._validate_snapshot(
            response,
            operation="job query",
            expected_job_id=job_id,
        )

    async def cancel_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """Cancel one exact attempt, including an unknown-submit tombstone.

        The full canonical V2 request is deliberately repeated here.  It lets
        the Worker durably create a standard cancelled journal when a previous
        submit reached the Worker but its response was lost, while fencing a
        reused job id, attempt token, sequence, hash, or scientific payload.
        """

        validated_request, validated_payload = self._validated_job_request(job)
        response = await self._json_request(
            "POST",
            f"/jobs/{self._job_segment(validated_request.job_id)}/cancel",
            operation="job cancellation",
            json=validated_payload,
        )
        return await self._validate_snapshot(
            response,
            operation="job cancellation",
            expected_job_id=validated_request.job_id,
            expected_attempt_token=validated_request.attempt_token,
            expected_request_sha256=validated_request.request_sha256,
            expected_enqueue_sequence=validated_request.enqueue_sequence,
        )

    async def delete_artifacts(self, job_id: str) -> dict[str, Any]:
        response = await self._json_request(
            "DELETE",
            f"/jobs/{self._job_segment(job_id)}/artifacts",
            operation="artifact deletion",
        )
        try:
            deletion = InternalWorkerArtifactDeletionResponse.model_validate(response)
        except ValidationError as exc:
            raise self._invalid_response("artifact deletion") from exc
        if deletion.job_id != job_id:
            raise self._invalid_response("artifact deletion")
        return deletion.model_dump(mode="json")

    async def purge_job(self, job: dict[str, Any]) -> dict[str, Any]:
        validated_request, _ = self._validated_job_request(job)
        response = await self._json_request(
            "POST",
            f"/jobs/{self._job_segment(validated_request.job_id)}/purge",
            operation="job storage deletion",
            json={
                "attempt_token": validated_request.attempt_token,
                "request_sha256": validated_request.request_sha256,
                "enqueue_sequence": validated_request.enqueue_sequence,
            },
        )
        try:
            deletion = InternalWorkerJobPurgeResponse.model_validate(response)
        except ValidationError as exc:
            raise self._invalid_response("job storage deletion") from exc
        if (
            deletion.job_id != validated_request.job_id
            or deletion.storage_state != "absent"
        ):
            raise self._invalid_response("job storage deletion")
        return deletion.model_dump(mode="json")

    async def stream_artifact(self, job_id: str, artifact_id: str) -> MonomerDftWorkerStream:
        if _SAFE_ARTIFACT_ID.fullmatch(artifact_id) is None:
            raise MonomerDftWorkerError(
                "invalid artifact id",
                status_code=404,
                code="artifact_not_found",
                retryable=False,
            )
        return await self._stream_request(
            f"/jobs/{self._job_segment(job_id)}/artifacts/{quote(artifact_id, safe='')}",
            operation="artifact download",
        )

    async def stream_bundle(self, job_id: str) -> MonomerDftWorkerStream:
        return await self._stream_request(
            f"/jobs/{self._job_segment(job_id)}/bundle",
            operation="artifact bundle download",
        )

    @staticmethod
    def _job_segment(job_id: str) -> str:
        if _SAFE_JOB_ID.fullmatch(job_id) is None:
            raise MonomerDftWorkerError(
                "DFT job not found",
                status_code=404,
                code="job_not_found",
                retryable=False,
            )
        return quote(job_id, safe="")

    async def _json_request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self._require_uds()
        assert self._client is not None
        response: httpx.Response | None = None
        try:
            headers = dict(kwargs.pop("headers", {}) or {})
            headers.setdefault("Accept-Encoding", "identity")
            request = self._client.build_request(
                method,
                path,
                headers=headers,
                **kwargs,
            )
            response = await self._client.send(request, stream=True)
            maximum_bytes = (
                MAX_WORKER_ERROR_BYTES if response.is_error else MAX_WORKER_JSON_BYTES
            )
            body = await self._bounded_response_body(
                response,
                maximum_bytes=maximum_bytes,
                operation=operation,
            )
        except httpx.RequestError as exc:
            raise MonomerDftWorkerError(
                f"DFT worker is unavailable during {operation}",
                code="worker_unavailable",
                retryable=True,
            ) from exc
        finally:
            if response is not None:
                await response.aclose()
        assert response is not None  # successful send/read path
        if response.is_error:
            raise await self._response_error(response, operation, body)
        if not body:
            return {}
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, ValueError) as exc:
            raise MonomerDftWorkerError(
                f"DFT worker returned invalid JSON during {operation}",
                status_code=502,
                code="invalid_worker_response",
                retryable=True,
            ) from exc
        if not isinstance(value, dict):
            raise MonomerDftWorkerError(
                f"DFT worker returned an invalid response during {operation}",
                status_code=502,
                code="invalid_worker_response",
                retryable=True,
            )
        return value

    async def _stream_request(self, path: str, *, operation: str) -> MonomerDftWorkerStream:
        self._require_uds()
        assert self._client is not None
        request = self._client.build_request(
            "GET",
            path,
            headers={"Accept-Encoding": "identity"},
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.RequestError as exc:
            raise MonomerDftWorkerError(
                f"DFT worker is unavailable during {operation}",
                code="worker_unavailable",
                retryable=True,
            ) from exc
        if response.is_error:
            try:
                body = await self._bounded_response_body(
                    response,
                    maximum_bytes=MAX_WORKER_ERROR_BYTES,
                    operation=operation,
                )
                error = await self._response_error(response, operation, body)
            finally:
                await response.aclose()
            raise error
        return MonomerDftWorkerStream(response=response)

    @classmethod
    async def _bounded_response_body(
        cls,
        response: httpx.Response,
        *,
        maximum_bytes: int,
        operation: str,
    ) -> bytes:
        raw_encoding = response.headers.get("content-encoding", "").strip().lower()
        if raw_encoding not in {"", "identity"}:
            raise cls._invalid_response(operation)
        raw_length = response.headers.get("content-length")
        if raw_length is not None:
            if not raw_length.isascii() or not raw_length.isdigit():
                raise cls._invalid_response(operation)
            if int(raw_length) > maximum_bytes:
                raise cls._invalid_response(operation)
        chunks: list[bytes] = []
        actual_size = 0
        if response.is_stream_consumed:
            # Mock/in-process transports may materialize a response before
            # returning it even when ``send(stream=True)`` was requested.
            # Network/UDS responses take the streaming branch below.
            chunks.append(response.content)
            actual_size = len(response.content)
            if actual_size > maximum_bytes:
                raise cls._invalid_response(operation)
        else:
            async for chunk in response.aiter_raw():
                actual_size += len(chunk)
                if actual_size > maximum_bytes:
                    raise cls._invalid_response(operation)
                chunks.append(chunk)
        if raw_length is not None and actual_size != int(raw_length):
            raise cls._invalid_response(operation)
        return b"".join(chunks)

    def _require_uds(self) -> None:
        if not self.uses_uds:
            raise MonomerDftWorkerError(
                "monomer DFT Unix socket is not configured",
                status_code=503,
                code="worker_socket_not_configured",
                retryable=False,
            )

    @staticmethod
    def _invalid_response(operation: str) -> MonomerDftWorkerError:
        return MonomerDftWorkerError(
            f"DFT worker returned an invalid response during {operation}",
            status_code=502,
            code="invalid_worker_response",
            retryable=True,
        )

    async def _validate_snapshot(
        self,
        value: dict[str, Any],
        *,
        operation: str,
        expected_job_id: str,
        expected_attempt_token: str | None = None,
        expected_request_sha256: str | None = None,
        expected_enqueue_sequence: int | None = None,
    ) -> dict[str, Any]:
        try:
            snapshot = await anyio.to_thread.run_sync(
                InternalWorkerSnapshot.model_validate,
                value,
                limiter=self._validation_limiter,
            )
        except ValidationError as exc:
            raise self._invalid_response(operation) from exc
        if snapshot.job_id != expected_job_id:
            raise self._invalid_response(operation)
        if expected_attempt_token is not None and snapshot.attempt_token != expected_attempt_token:
            raise self._invalid_response(operation)
        if expected_request_sha256 is not None and snapshot.request_sha256 != expected_request_sha256:
            raise self._invalid_response(operation)
        if expected_enqueue_sequence is not None and snapshot.enqueue_sequence != expected_enqueue_sequence:
            raise self._invalid_response(operation)
        payload = snapshot.model_dump(mode="json")
        if snapshot.result is not None:
            # Preserve the Worker's artifact-backed result shape.  Pydantic
            # defaults must not manufacture optional null properties that are
            # absent from both the scientific artifact and durable journal.
            payload["result"] = snapshot.result.model_dump(
                mode="json",
                exclude_unset=True,
            )
        return payload

    @staticmethod
    async def _response_error(
        response: httpx.Response,
        operation: str,
        body: bytes,
    ) -> MonomerDftWorkerError:
        detail = f"HTTP {response.status_code}"
        structured_code: str | None = None
        structured_retryable: bool | None = None
        structured_details: dict[str, Any] = {}
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            value = None
        if isinstance(value, dict):
            structured_error = value.get("error")
            if isinstance(structured_error, dict):
                raw_message = structured_error.get("message")
                if isinstance(raw_message, str):
                    detail = sanitize_public_text(raw_message, fallback=detail, limit=240)
                raw_code = structured_error.get("code")
                if isinstance(raw_code, str) and _SAFE_ERROR_CODE.fullmatch(raw_code):
                    structured_code = raw_code
                if isinstance(structured_error.get("retryable"), bool):
                    structured_retryable = structured_error["retryable"]
                structured_details = sanitize_public_json(structured_error.get("details"))
            raw_detail = value.get("detail")
            if not isinstance(structured_error, dict) and isinstance(raw_detail, str):
                detail = sanitize_public_text(raw_detail, fallback=detail, limit=240)
            elif not isinstance(structured_error, dict) and isinstance(raw_detail, dict) and isinstance(raw_detail.get("message"), str):
                detail = sanitize_public_text(raw_detail["message"], fallback=detail, limit=240)

        mapping: dict[int, tuple[int, str, bool]] = {
            400: (502, "worker_rejected_request", False),
            404: (404, "worker_resource_not_found", False),
            409: (409, "worker_attempt_conflict", False),
            422: (502, "worker_contract_mismatch", False),
            429: (429, "worker_capacity_full", True),
            503: (503, "worker_unavailable", True),
        }
        public_status, code, retryable = mapping.get(
            response.status_code,
            (502, "worker_error", response.status_code >= 500),
        )
        if structured_code is not None:
            code = structured_code
        if structured_retryable is not None:
            retryable = structured_retryable
        return MonomerDftWorkerError(
            f"DFT worker rejected {operation}: {detail}",
            status_code=public_status,
            code=code,
            retryable=retryable,
            details=structured_details,
        )
