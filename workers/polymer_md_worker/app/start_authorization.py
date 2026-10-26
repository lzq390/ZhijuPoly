from __future__ import annotations

import httpx

from polymer_md.contracts import StartAuthorizationRequest, StartAuthorizationResponse, SubmitRequest


class StartAuthorizer:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    async def authorize(self, request: SubmitRequest, worker_instance_id: str, *,
                        replaces_worker_instance_id: str | None = None,
                        prior_execution_cleanup_confirmed: bool = False) -> dict:
        if not self.configured:
            raise RuntimeError("start_authorization_not_configured")
        payload = StartAuthorizationRequest(**request.model_dump(), worker_instance_id=worker_instance_id,
            replaces_worker_instance_id=replaces_worker_instance_id,
            prior_execution_cleanup_confirmed=prior_execution_cleanup_confirmed)
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            response = await client.post(f"{self.base_url}/jobs/{request.job_id}/authorize-start",
                                         headers={"Authorization": "Bearer " + self.token}, json=payload.model_dump(mode="json"))
        response.raise_for_status()
        result = StartAuthorizationResponse.model_validate(response.json())
        for key in ("job_id", "attempt_token", "request_sha256", "enqueue_sequence", "worker_instance_id"):
            if getattr(result, key) != getattr(payload, key):
                raise RuntimeError("start_authorization_identity_mismatch")
        return result.model_dump(mode="json")
