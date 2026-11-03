from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models import PropertyFilterOption
from app.property_filter_mcp import error_result, tool_result
from app.property_filter_mcp_contract import (
    FilterInput, ListInput, ListOutput, MCPFailure, SearchInput, bind_condition, check_revision,
)
from app.property_filter_mcp_security import CallLimiter, ClientRegistry, MCPConfig, read_private_file


def config(tmp_path: Path, **kwargs):
    return MCPConfig(dsn="postgresql://readonly@localhost/test", clients_file=tmp_path / "clients.json", **kwargs)


def test_exact_empty_unit_is_bound():
    option = PropertyFilterOption(filter_type="standardized", option_key="std:x:", label="x",
                                  property_key="x", canonical_unit=None, rows=1, unique_smiles=1)
    bound = bind_condition(option, FilterInput(option_key="std:x:", min_value=0))
    assert bound.canonical_unit == ""
    assert bound.min_value == 0


@pytest.mark.parametrize("values", [{}, {"min_value": float("nan")}, {"max_value": float("inf")},
                                     {"min_value": 2, "max_value": 1}])
def test_invalid_numeric_bounds(values):
    with pytest.raises(ValidationError):
        FilterInput(option_key="std:x:", **values)


def test_later_catalog_pages_require_revision():
    with pytest.raises(ValidationError):
        ListInput(page=2)
    assert ListInput(page=2, dataset_revision="version").page == 2
    with pytest.raises(MCPFailure, match="Dataset changed"):
        check_revision("old", "new")


def test_limits_and_extra_parameters(tmp_path):
    with pytest.raises(ValidationError):
        SearchInput(dataset_revision="v", filters=[{"option_key": "x", "min_value": 0}], dsn="bad")
    with pytest.raises(ValueError):
        config(tmp_path, max_concurrency=3)
    with pytest.raises(ValueError):
        config(tmp_path, allowed_hosts=("*",))


def test_registry_revocation_expiry_and_private_mode(tmp_path):
    path = tmp_path / "clients.json"
    token = "test-secret"
    entry = {"client_id": "test", "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
             "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
             "tools": ["list_filter_properties"]}
    path.write_text(json.dumps({"clients": [entry]}))
    path.chmod(0o600)
    assert ClientRegistry.read(path).authenticate(token).client_id == "test"
    assert ClientRegistry.read(path).authenticate("invalid") is None
    entry["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    path.write_text(json.dumps({"clients": [entry]}))
    assert ClientRegistry.read(path).authenticate(token) is None
    path.write_text('{"clients": []}')
    assert ClientRegistry.read(path).authenticate(token) is None
    path.chmod(0o644)
    with pytest.raises(ValueError):
        read_private_file(path)


def test_limiter_counts_active_and_per_client_calls(tmp_path):
    limiter = CallLimiter(config(tmp_path, rate_limit=2))
    limiter.acquire("a")
    limiter.acquire("a")
    with pytest.raises(MCPFailure) as error:
        limiter.acquire("a")
    assert error.value.code == "RATE_LIMITED"
    limiter.acquire("b")
    limiter.release("b")
    limiter.acquire("b")
    limiter = CallLimiter(config(tmp_path))
    limiter.acquire("a")
    limiter.acquire("a")
    with pytest.raises(MCPFailure) as error:
        limiter.acquire("a")
    assert error.value.code == "CONCURRENCY_LIMITED"
    limiter.release("a")
    limiter.acquire("a")


def test_structured_result_does_not_duplicate_json_as_text():
    output = ListOutput(dataset_revision="v", total_measurement_count=4, total_option_count=0,
                        matched_option_count=0, page=1, page_size=25, has_more=False, options=[])
    result = tool_result(output)
    assert result.structured_content["total_measurement_count"] == 4
    assert "dataset_revision" not in result.content[0].text
    error = error_result("UNKNOWN_OPTION", "Fetch catalog")
    assert error.is_error
    assert error.structured_content["error"]["code"] == "UNKNOWN_OPTION"
