from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.retrosynthesis_mcp_contract import (
    INPUT_MODELS, OUTPUT_MODELS, MODEL_TOOL_NAMES, UI_TOOL_NAMES, Parameters, TargetInput,
)
from app.retrosynthesis_mcp_journal import DraftError, JournalError, SubmissionJournal
from app.retrosynthesis_mcp_security import RetrosynthesisClientRegistry, RetrosynthesisMCPConfig
from app.retrosynthesis_mcp import MCPRuntime
from app.mcp_security import MCPFailure


def test_tool_scopes_and_schema_are_complete():
    assert len(INPUT_MODELS) == len(OUTPUT_MODELS) == 8
    assert len(MODEL_TOOL_NAMES) == len(UI_TOOL_NAMES) == 5
    assert "confirm_monomer_retrosynthesis" not in MODEL_TOOL_NAMES
    assert "get_retrosynthesis_job" not in UI_TOOL_NAMES
    for model in list(INPUT_MODELS.values()) + list(OUTPUT_MODELS.values()):
        assert model.model_json_schema()["additionalProperties"] is False


@pytest.mark.parametrize("name,args", [
    ("prepare_monomer_retrosynthesis", {"conversation_id": "valid", "owner_id": "forged"}),
    ("prepare_monomer_retrosynthesis", {"conversation_id": "valid", "parameters": {"num_beams": "5"}}),
    ("get_retrosynthesis_job", {"conversation_id": "valid", "job_id": "../private"}),
    ("get_retrosynthesis_job", {"conversation_id": "valid", "job_id": "ok", "wait_seconds": 21}),
    ("get_retrosynthesis_draft", {"conversation_id": "bad/id", "draft_id": "rtd_" + "a" * 32}),
])
def test_strict_input_rejects_unsafe_arguments(name, args):
    with pytest.raises(ValidationError):
        INPUT_MODELS[name].model_validate(args)


@pytest.mark.parametrize("args", [
    {"source": "smiles", "smiles": "CC", "name": "ambiguous"},
    {"source": "smiles", "smiles": "x" * 8001},
    {"source": "name"},
    {"source": "name", "name": "x" * 1001},
    {"source": "name", "name": "ethanol", "cursor": "unsupported"},
    {"source": "saved_structure", "record_id": "../../record"},
    {"source": "saved_structure", "limit": 21},
    {"source": "saved_structure", "cursor": "x" * 513},
])
def test_target_source_and_size_validation(args):
    with pytest.raises(ValidationError):
        TargetInput.model_validate(args)


def test_parameter_cross_field_validation():
    with pytest.raises(ValidationError):
        Parameters(num_return_sequences=10, num_beams=5)
    assert Parameters().max_new_tokens == 128


def test_model_credential_cannot_gain_ui_scope(configuration):
    credentials = json.loads(configuration.clients_file.read_text())
    credentials["clients"][0]["tools"].append("confirm_monomer_retrosynthesis")
    configuration.clients_file.write_text(json.dumps(credentials))
    with pytest.raises(ValueError):
        RetrosynthesisClientRegistry.read(configuration.clients_file)


def test_environment_is_independent_of_polytao(configuration, monkeypatch):
    for key in ("BACKEND_URL", "ACCOUNT_UUID", "SESSION_FILE", "CLIENTS_FILE", "JOURNAL_FILE"):
        monkeypatch.setenv("NEXPOLY_RETROSYNTHESIS_MCP_" + key, str(getattr(configuration, key.lower())))
        monkeypatch.setenv("NEXPOLY_POLYTAO_MCP_" + key, "incorrect-polytao-value")
    parsed = RetrosynthesisMCPConfig.from_env()
    assert parsed == configuration


def test_nested_native_target_source_error_has_safe_specific_guidance():
    with pytest.raises(MCPFailure) as unavailable:
        MCPRuntime.check_status(503, {"detail": {"code": "structure_library_unavailable", "message": "/private/database-details"}})
    assert unavailable.value.code == "STRUCTURE_LIBRARY_UNAVAILABLE"
    assert "SMILES" in unavailable.value.message
    assert "/private" not in unavailable.value.message


def test_journal_pins_native_account_and_fail_closed_on_replacement(configuration):
    journal = SubmissionJournal(configuration.journal_file, backend_url=configuration.backend_url, account_uuid=configuration.account_uuid)
    item = journal.create_draft("test", configuration.account_uuid, "conversation", {})
    with pytest.raises(DraftError):
        journal.read_draft("test", configuration.account_uuid, "different", item["draft_id"])
    journal.close()
    with pytest.raises(JournalError):
        SubmissionJournal(configuration.journal_file, backend_url="http://different:8000", account_uuid=configuration.account_uuid)
    original = configuration.journal_file.with_suffix(".old")
    configuration.journal_file.rename(original)
    configuration.journal_file.touch(mode=0o600)
    with pytest.raises(JournalError):
        SubmissionJournal(configuration.journal_file, backend_url=configuration.backend_url, account_uuid=configuration.account_uuid)


def test_durable_draft_receipt_cannot_be_downgraded_or_reassigned(configuration):
    journal = SubmissionJournal(configuration.journal_file, backend_url=configuration.backend_url, account_uuid=configuration.account_uuid)
    try:
        item = journal.create_draft("test", configuration.account_uuid, "conversation", {})
        scope = ("test", configuration.account_uuid, "conversation", item["draft_id"])
        journal.confirm_draft(*scope, 1)
        journal.finish_draft(*scope, state="submitted", job_id="first_job")
        delayed = journal.finish_draft(*scope, state="submission_unknown", error_code="SUBMISSION_NOT_RECEIVED")
        assert delayed["state"] == "submitted" and delayed["job_id"] == "first_job"
        with pytest.raises(DraftError):
            journal.finish_draft(*scope, state="submitted", job_id="different_job")
    finally:
        journal.close()


def test_lightweight_container_copies_only_adapter_sources():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root.parent / "Dockerfile.retrosynthesis-mcp").read_text()
    assert "COPY --chmod=644" in dockerfile
    assert "USER 1001:1001" in dockerfile
    assert "backend/app /app/backend/app" not in dockerfile
    source = (root / "app/retrosynthesis_mcp.py").read_text()
    assert "app.models" not in source
    assert not any(f"import {package}" in source for package in ("torch", "rdkit", "psycopg"))
