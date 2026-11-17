"""Synthetic native backend; never load settings, RDKit, model or database."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

try:
    import httpx2 as httpx
except ImportError:  # Native fixture keeps backend dependencies in a separate process.
    import httpx
import pytest

from app.retrosynthesis_mcp_contract import MODEL_TOOL_ORDER, UI_TOOL_ORDER
from app.retrosynthesis_mcp_journal import SubmissionJournal
from app.retrosynthesis_mcp_security import RetrosynthesisMCPConfig


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def configuration(tmp_path):
    tmp_path.chmod(0o700)
    config = RetrosynthesisMCPConfig(backend_url="http://backend:8000", account_uuid=str(uuid4()),
        session_file=tmp_path / "session.json", clients_file=tmp_path / "clients.json", journal_file=tmp_path / "submissions.sqlite")
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    session = {"account_uuid": config.account_uuid, "backend_url": config.backend_url,
        "cookie": "nexpoly_session=PRIVATE_COOKIE", "session_id": "session-id", "csrf_token": "PRIVATE_CSRF", "expires_at": expiry}
    credentials = {"clients": [{"client_id": "test", "token_sha256": hashlib.sha256(b"test-token").hexdigest(),
        "expires_at": expiry, "tools": list(MODEL_TOOL_ORDER)}, {"client_id": "test-ui", "owner_client_id": "test",
        "token_sha256": hashlib.sha256(b"ui-token").hexdigest(), "expires_at": expiry, "tools": list(UI_TOOL_ORDER)}]}
    for path, payload in ((config.session_file, session), (config.clients_file, credentials)):
        path.write_text(json.dumps(payload))
        path.chmod(0o600)
    SubmissionJournal.initialize(config.journal_file)
    return config


def session_response(config):
    return {"authenticated": True, "user": {"id": config.account_uuid, "must_change_password": False},
        "account_type": "member", "capabilities": {"retrosynthesis.use": True}, "session_id": "session-id", "csrf_token": "PRIVATE_CSRF"}


def resolved_target(smiles="CCO", source="smiles"):
    return {"canonical_smiles": smiles, "input_smiles": smiles, "inferred_target_role": "other", "preview_svg": "<svg/>", "source": {"type": source}}


def job_response(job_id="retro_test", status="completed"):
    return {"job_id": job_id, "status": status, "stage": "completed" if status == "completed" else "queued",
        "created_at": "2026-11-17T01:00:00Z", "updated_at": "2026-11-17T01:01:00Z", "started_at": None, "finished_at": None,
        "error_code": None, "result": {"input_smiles": "CCO", "canonical_smiles": "CCO", "target_role": "auto", "inferred_target_role": "other",
            "model_id": "ReactionT5v2-retrosynthesis-USPTO_50k", "query_time_ms": 10.0, "total": 1,
            "candidates": [{"rank": 1, "reactants_smiles": "CC.O", "canonical_reactants_smiles": "CC.O",
                "reactants": [{"input_smiles": "CC", "canonical_smiles": "CC", "valid_smiles": True, "heavy_atom_count": 2},
                              {"input_smiles": "O", "canonical_smiles": "O", "valid_smiles": True, "heavy_atom_count": 1}],
                "valid_smiles": True, "all_reactants_smaller_than_target": True, "reaction_hint": "candidate"}]}}


class NativeBackend:
    def __init__(self, config):
        self.config, self.requests, self.jobs = config, [], {}
        self.lost_response = False
        self.lookup_missing = False
        self.resolve_ambiguous = False
        self.submit_status = 202
        self.session_override = None
        self.fail_before_receive = 0
        self.accepted_count = 0
        self.first_post_entered = self.release_first_post = None
        self.first_post_status = None

    async def __call__(self, request):
        from app.retrosynthesis_mcp import API
        self.requests.append(request)
        if request.url.path == "/api/v1/auth/session":
            return httpx.Response(200, json=self.session_override or session_response(self.config))
        assert request.headers["cookie"] == "nexpoly_session=PRIVATE_COOKIE"
        if request.method == "POST":
            assert request.headers["x-csrf-token"] == "PRIVATE_CSRF"
            assert request.headers["origin"] == self.config.backend_url
        if request.url.path == API + "/status":
            return httpx.Response(200, json={name: True for name in ("enabled", "available", "schema_ready", "accepting", "model_available", "name_resolution_available", "structure_library_available")})
        if request.url.path == API + "/targets/resolve":
            body = json.loads(request.content)
            if self.resolve_ambiguous:
                return httpx.Response(200, json={"status": "needs_clarification", "target": None, "candidates": [resolved_target("CCO", "name"), resolved_target("CCN", "name")], "next_cursor": None, "message": "Choose a target"})
            return httpx.Response(200, json={"status": "resolved", "target": resolved_target(body.get("smiles", "CCO"), body["source"]), "candidates": []})
        if request.url.path == API + "/jobs" and request.method == "POST":
            first_post = len(self.posts) == 1
            if self.fail_before_receive:
                self.fail_before_receive -= 1
                raise httpx.ConnectError("PRIVATE_CONNECT_ERROR")
            if self.first_post_entered is not None and first_post:
                self.first_post_entered.set()
                await self.release_first_post.wait()
            if first_post and self.first_post_status is not None:
                return httpx.Response(self.first_post_status, json={"detail": "PRIVATE_LATE_REJECTION"})
            if self.submit_status != 202:
                return httpx.Response(self.submit_status, json={"detail": "PRIVATE_BACKEND_ERROR"})
            key = request.headers["idempotency-key"]
            if key not in self.jobs:
                self.jobs[key] = job_response()
                self.accepted_count += 1
            if self.lost_response:
                raise httpx.ReadError("PRIVATE_NETWORK_ERROR")
            return httpx.Response(202, json={"job_id": "retro_test", "status": "pending"})
        if request.url.path == API + "/jobs/lookup":
            job = self.jobs.get(request.url.params["idempotency_key"])
            return httpx.Response(200, json=job) if job and not self.lookup_missing else httpx.Response(404, json={"detail": "not found"})
        if request.url.path == API + "/jobs/retro_test":
            return httpx.Response(200, json=job_response())
        raise AssertionError(f"Unexpected synthetic path: {request.url.path}")

    @property
    def posts(self):
        return [request for request in self.requests if request.method == "POST" and request.url.path.endswith("/jobs")]


@pytest.fixture
def native(configuration):
    return NativeBackend(configuration)
