from __future__ import annotations

import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from app.models import POLYTAO_DESCRIPTOR_NAMES, PolytaoGenerationRequest
from app.mcp_security import MCPFailure, read_private_file
from app.polytao_mcp import tool_result
from app.polytao_mcp_contract import DescriptorOutput, GenerationParameters, JobInput, SubmitInput
from app.polytao_mcp_security import BackendSession, backend_origin
from conftest import descriptor_values


def test_schema_uses_backend_bounds_and_explicit_descriptor_keys():
    expected = PolytaoGenerationRequest.model_json_schema()["properties"]
    actual = GenerationParameters.model_json_schema()["properties"]
    assert all(actual[name] == expected[name] for name in actual)
    descriptor_schema = SubmitInput.model_json_schema()["$defs"]["PolytaoDescriptors"]
    assert set(descriptor_schema["required"]) == set(POLYTAO_DESCRIPTOR_NAMES)
    assert descriptor_schema["additionalProperties"] is False
    assert SubmitInput(descriptors=descriptor_values(), idempotency_key="key").candidate_count == 10


@pytest.mark.parametrize("bad", [True, "1", float("inf"), float("nan"), None])
def test_strict_descriptor_values(bad):
    values = descriptor_values()
    values["MolWt"] = bad
    with pytest.raises(ValidationError):
        SubmitInput(descriptors=values, idempotency_key="key")


@pytest.mark.parametrize("kwargs", [{"candidate_count": True}, {"candidate_count": "3"}, {"candidate_count": 51},
    {"top_p": 0}, {"top_k": 0}, {"max_length": 513}, {"temperature": float("nan")}, {"input_smiles": "*CC*"}])
def test_strict_sampling_and_no_seed(kwargs):
    with pytest.raises(ValidationError):
        SubmitInput(descriptors=descriptor_values(), idempotency_key="key", **kwargs)


def test_exact_descriptors():
    for values in ({}, {**descriptor_values(), "unknown": 1}):
        with pytest.raises(ValidationError):
            SubmitInput(descriptors=values, idempotency_key="key")
    # Keep backend-compatible finite numeric range; do not invent chemistry rules.
    values = descriptor_values()
    values["HeavyAtomCount"] = -0.5
    assert SubmitInput(descriptors=values, idempotency_key="key").descriptors.HeavyAtomCount == -0.5


def test_native_job_ids_and_path_injection():
    assert JobInput(job_id="polytao.instance.token").job_id == "polytao.instance.token"
    for bad in ("../auth/session", "..", "x/y", "x?z", "x#z", "x%2fy"):
        with pytest.raises(ValidationError):
            JobInput(job_id=bad)


def test_fixed_target_and_session_binding(configuration):
    for value in ("file:///etc/passwd", "http://a/path", "http://user:secret@a", "http://a?query=x"):
        with pytest.raises(ValueError):
            backend_origin(value)
    session = BackendSession.read(configuration)
    assert session.headers(post=True)["Origin"] == configuration.backend_url
    assert session.headers()["X-Session-Context"] == "session-id"
    with pytest.raises(MCPFailure) as caught:
        BackendSession.read(replace(configuration, backend_url="http://other"))
    assert caught.value.code == "SESSION_UNAVAILABLE"


def test_private_session_expiry_and_permissions(configuration):
    data = json.loads(configuration.session_file.read_text())
    data["expires_at"] = "2020-01-01T00:00:00Z"
    configuration.session_file.write_text(json.dumps(data))
    with pytest.raises(MCPFailure) as caught:
        BackendSession.read(configuration)
    assert caught.value.code == "SESSION_EXPIRED"
    configuration.session_file.chmod(0o644)
    with pytest.raises(ValueError):
        read_private_file(configuration.session_file)


def test_result_structured_only_and_bounded():
    output = DescriptorOutput(input_smiles="*CC*", canonical_smiles="*CC*", descriptors=descriptor_values(), query_time_ms=1)
    result = tool_result(output, "request-id")
    assert result.structured_content["request_id"] == "request-id"
    assert "MolWt" not in result.content[0].text
    output.canonical_smiles = "C" * (256 * 1024)
    with pytest.raises(MCPFailure) as caught:
        tool_result(output, "id")
    assert caught.value.code == "RESPONSE_TOO_LARGE"


def test_response_bound_includes_caller_supplied_protocol_id():
    output = DescriptorOutput(input_smiles="*CC*", canonical_smiles="C" * (240 * 1024),
                              descriptors=descriptor_values(), query_time_ms=1)
    assert tool_result(output, "private-request-id").structured_content["ok"]
    with pytest.raises(MCPFailure) as caught:
        tool_result(output, "private-request-id", protocol_request_id="x" * 31000)
    assert caught.value.code == "RESPONSE_TOO_LARGE"
