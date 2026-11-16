"""These tests never load backend settings or access a database/model/GPU."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.polytao_mcp_contract import MODEL_TOOL_ORDER, UI_TOOL_ORDER
from app.polytao_mcp_journal import SubmissionJournal
from app.polytao_mcp_security import PolytaoMCPConfig


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def configuration(tmp_path):
    tmp_path.chmod(0o700)
    config = PolytaoMCPConfig(backend_url="http://backend:8000", account_uuid=str(uuid4()),
        session_file=tmp_path / "session.json", clients_file=tmp_path / "clients.json", journal_file=tmp_path / "submissions.sqlite")
    session = {"account_uuid": config.account_uuid, "backend_url": config.backend_url,
        "cookie": "nexpoly_session=PRIVATE_COOKIE", "session_id": "session-id", "csrf_token": "PRIVATE_CSRF",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    credentials = {"clients": [{"client_id": "test", "token_sha256": hashlib.sha256(b"test-token").hexdigest(),
        "expires_at": session["expires_at"], "tools": list(MODEL_TOOL_ORDER)}, {
        "client_id": "test-ui", "owner_client_id": "test", "token_sha256": hashlib.sha256(b"ui-token").hexdigest(),
        "expires_at": session["expires_at"], "tools": list(UI_TOOL_ORDER)}]}
    for path, value in ((config.session_file, session), (config.clients_file, credentials)):
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    SubmissionJournal.initialize(config.journal_file)
    return config


def session_response(config):
    return {"authenticated": True, "user": {"id": config.account_uuid, "must_change_password": False},
            "account_type": "member", "capabilities": {"polytao.use": True},
            "session_id": "session-id", "csrf_token": "PRIVATE_CSRF"}


def descriptor_values():
    from app.models import POLYTAO_DESCRIPTOR_NAMES
    return {name: 1.0 for name in POLYTAO_DESCRIPTOR_NAMES}


def job_response(job_id, count=2, requested=3, status="completed"):
    results = [{"rank": i + 1, "generated_smiles": "*CC*", "raw_smiles": "PRIVATE_RAW",
                "structure_svg": "<svg>PRIVATE_SVG</svg>", "valid_smiles": True,
                "sa_score": None, "warnings": []} for i in range(count)]
    return {"job_id": job_id, "status": status, "prompt": "PRIVATE_PROMPT", "requested_count": requested,
        "returned_count": count, "attempts": 1, "progress_percent": 100, "progress_stage": "completed",
        "progress_message": "done", "created_at": "2026-11-16T01:00:00Z", "updated_at": "2026-11-16T01:02:00Z",
        "engine": "polytao", "worker_id": "PRIVATE_WORKER", "result": {"prompt": "PRIVATE_PROMPT", "query_time_ms": 100,
        "requested_count": requested, "returned_count": count, "attempts": 1, "filter_counter": {"invalid_smiles": 1}, "results": results}}
