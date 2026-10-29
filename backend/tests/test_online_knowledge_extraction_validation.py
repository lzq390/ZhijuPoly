from __future__ import annotations

import json
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.models import OnlineKnowledgeSearchRequest
from app.routers import online_knowledge as routes
from app.services import usage_provider
from app.services.ai_client import INVALID_EXTRACTION_RESULT, clean_ai_provider_error
from app.services.online_knowledge import extractor, search_service


def negative(mode: str) -> dict:
    if mode == "synthesis":
        return {"has_polymerization": False, "total_reactions": 0, "reactions": []}
    return {"has_polymer": False, "total_data_points": 0, "data_points": []}


def positive(mode: str) -> dict:
    if mode == "synthesis":
        return {"has_polymerization": True, "total_reactions": 1, "reactions": [{
            "reaction_number": 1, "product_name": "polyimide", "reactants": None,
            "reaction_type": None, "product_abbreviation": None, "properties": None,
            "reaction_conditions": {"temperature": "150 °C", "time": None},
        }]}
    return {"has_polymer": True, "total_data_points": 1, "data_points": [{
        "polymer_name": "polyimide", "polymer_type": None, "condition_name": None,
        "condition_value": None, "property_name": "Tg", "property_value": "150 °C", "relationship": None,
    }]}


@pytest.fixture
def model_responses(monkeypatch):
    responses = []
    calls = []
    clients = []

    def create(**kwargs):
        calls.append(kwargs)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if not isinstance(response, str):
            response = json.dumps(response)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=response))])

    def make_client(**_kwargs):
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=Mock())
        clients.append(client)
        return client

    monkeypatch.setattr(extractor, "create_openai_client", make_client)
    provider_boundary = Mock(side_effect=lambda callback, **kwargs: callback(**kwargs))
    monkeypatch.setattr(extractor, "provider_call", provider_boundary)
    return SimpleNamespace(responses=responses, calls=calls, clients=clients, provider_boundary=provider_boundary)


def extract_one(model_responses, response, mode):
    model_responses.responses.append(response)
    instance = extractor.PolymerExtractor("fixture-key", "https://invalid.example/v1", "fixture-model")
    try:
        return instance.extract_from_abstract("Original title", "Original abstract", "original-doi", mode)
    finally:
        instance.close()


@pytest.mark.parametrize("mode", ["synthesis", "property"])
@pytest.mark.parametrize("factory", [negative, positive])
def test_valid_positive_and_explicit_negative_allow_missing_and_null_science_fields(model_responses, mode, factory):
    result = extract_one(model_responses, factory(mode), mode)
    assert "error" not in result
    assert result["paper_id"] == "original-doi"
    assert result["title"] == "Original title"
    assert model_responses.provider_boundary.call_count == 1
    assert len(model_responses.calls) == 1
    assert model_responses.calls[0]["model"] == "fixture-model"
    assert len(model_responses.calls[0]["messages"]) == 2
    model_responses.clients[0].close.assert_called_once()


def test_polymer_without_property_relationships_is_a_valid_empty_result(model_responses):
    response = {"has_polymer": True, "total_data_points": 0, "data_points": []}
    result = extract_one(model_responses, response, "property")
    assert "error" not in result
    assert result["data_points"] == []


@pytest.mark.parametrize("mode", ["synthesis", "property"])
@pytest.mark.parametrize("placeholder", ["", "  ", "N/A", "NaN", "none", "null", "  NOT PROVIDED  "])
def test_missing_value_text_is_not_scientific_evidence(model_responses, mode, placeholder):
    response = positive(mode)
    collection = "reactions" if mode == "synthesis" else "data_points"
    field = "product_name" if mode == "synthesis" else "polymer_name"
    response[collection] = [{field: placeholder}]
    assert extract_one(model_responses, response, mode)["error"] == INVALID_EXTRACTION_RESULT


@pytest.mark.parametrize("field,value", [
    ("reactants", ["N/A", "not provided"]),
    ("reaction_conditions", {"temperature": "null", "time": "none"}),
    ("properties", [{"property_name": "N/A", "value": "not provided", "unit": "MPa"}]),
])
def test_nested_missing_value_text_cannot_create_a_synthesis_record(model_responses, field, value):
    response = positive("synthesis")
    response["reactions"] = [{"reaction_number": 1, field: value}]
    assert extract_one(model_responses, response, "synthesis")["error"] == INVALID_EXTRACTION_RESULT


@pytest.mark.parametrize("mode", ["synthesis", "property"])
def test_real_science_and_zero_values_remain_valid_with_other_missing_text(model_responses, mode):
    response = positive(mode)
    if mode == "synthesis":
        response["reactions"] = [{"product_name": "N/A", "reaction_conditions": {"temperature": "0"}}]
    else:
        response["data_points"] = [{"polymer_name": "not provided", "property_value": "0"}]
    assert "error" not in extract_one(model_responses, response, mode)


@pytest.mark.parametrize("mode", ["synthesis", "property"])
@pytest.mark.parametrize("response", [{}, [], None, "", "not-json", '{"secret": "sk-never-disclose"}'])
def test_invalid_json_or_missing_envelope_is_a_safe_failure(model_responses, mode, response):
    result = extract_one(model_responses, response, mode)
    assert result["error"] == INVALID_EXTRACTION_RESULT
    assert "sk-never-disclose" not in json.dumps(result)


@pytest.mark.parametrize("mode", ["synthesis", "property"])
@pytest.mark.parametrize("mutation", [
    "missing_flag", "string_flag", "numeric_flag", "missing_count", "string_count", "bool_count",
    "float_count", "negative_count", "mismatch", "missing_list", "dict_list", "null_list",
    "nonobject_record", "placeholder", "null_science", "unknown_only", "false_nonempty", "wrong_mode",
    "wrong_scalar_type",
])
def test_invalid_types_counts_and_placeholder_records_fail(model_responses, mode, mutation):
    response = positive(mode)
    flag, count, collection = (("has_polymerization", "total_reactions", "reactions") if mode == "synthesis"
                               else ("has_polymer", "total_data_points", "data_points"))
    if mutation == "missing_flag": response.pop(flag)
    elif mutation == "string_flag": response[flag] = "true"
    elif mutation == "numeric_flag": response[flag] = 1
    elif mutation == "missing_count": response.pop(count)
    elif mutation == "string_count": response[count] = "1"
    elif mutation == "bool_count": response[count] = True
    elif mutation == "float_count": response[count] = 1.0
    elif mutation == "negative_count": response[count] = -1
    elif mutation == "mismatch": response[count] = 2
    elif mutation == "missing_list": response.pop(collection)
    elif mutation == "dict_list": response[collection] = {}
    elif mutation == "null_list": response[collection] = None
    elif mutation == "nonobject_record": response[collection] = ["not a record"]
    elif mutation == "placeholder": response[collection] = [{"reaction_number": 1} if mode == "synthesis" else {}]
    elif mutation == "null_science": response[collection] = [{key: None for key in response[collection][0]}]
    elif mutation == "unknown_only": response[collection] = [{"unknown": "not evidence"}]
    elif mutation == "false_nonempty": response[flag] = False
    elif mutation == "wrong_mode": response = positive("property" if mode == "synthesis" else "synthesis")
    elif mutation == "wrong_scalar_type": response[collection][0]["product_name" if mode == "synthesis" else "polymer_name"] = 42
    result = extract_one(model_responses, response, mode)
    assert result["error"] == INVALID_EXTRACTION_RESULT


@pytest.mark.parametrize("field,value", [
    ("reaction_number", True), ("reaction_number", 0), ("reaction_number", "1"),
    ("reactants", "polymer"), ("reactants", [None]), ("reactants", [{}]),
    ("properties", {}), ("properties", [None]), ("properties", [{}]),
    ("properties", [{"property_name": "Tg", "value": []}]),
    ("reaction_conditions", []), ("reaction_conditions", {"temperature": 150}),
])
def test_invalid_nested_synthesis_fields_fail_without_silently_dropping_records(model_responses, field, value):
    response = positive("synthesis")
    response["reactions"][0][field] = value
    assert extract_one(model_responses, response, "synthesis")["error"] == INVALID_EXTRACTION_RESULT


def test_positive_polymerization_requires_at_least_one_reaction(model_responses):
    response = negative("synthesis")
    response["has_polymerization"] = True
    assert extract_one(model_responses, response, "synthesis")["error"] == INVALID_EXTRACTION_RESULT


@pytest.mark.parametrize("mode", ["synthesis", "property"])
def test_model_cannot_set_error_or_override_paper_metadata(model_responses, mode):
    response = positive(mode)
    response.update(error="model supplied error", paper_id="wrong-doi", title="wrong-title", unknown="ignored")
    result = extract_one(model_responses, response, mode)
    assert "error" not in result and "unknown" not in result
    assert result["paper_id"] == "original-doi" and result["title"] == "Original title"


def test_markdown_json_fence_remains_accepted(model_responses):
    result = extract_one(model_responses, "```json\n" + json.dumps(negative("synthesis")) + "\n```", "synthesis")
    assert "error" not in result


@pytest.mark.parametrize("mode", ["synthesis", "property"])
def test_invalid_structure_keeps_actual_provider_token_observations(monkeypatch, mode):
    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=17, completion_tokens=9, total_tokens=26),
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
    )
    create = Mock(return_value=response)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=Mock())
    span = Mock()
    monkeypatch.setattr(usage_provider, "current_usage_context", lambda: SimpleNamespace(module="online_knowledge"))
    monkeypatch.setattr(usage_provider, "usage_span", lambda *args, **kwargs: span)
    monkeypatch.setattr(extractor, "create_openai_client", lambda **kwargs: client)
    engine = extractor.PolymerExtractor("private-key", "https://private.invalid/v1", "fixture-model")
    try:
        result = engine.extract_from_abstract("Private title", "Private abstract", "private-doi", mode)
    finally:
        engine.close()
    assert result["error"] == INVALID_EXTRACTION_RESULT
    create.assert_called_once()
    span.finish.assert_called_once()
    observation = span.finish.call_args.kwargs
    assert observation["status"] == "completed"
    assert [observation["metrics"][key] for key in (
        "provider_input_tokens", "provider_output_tokens", "provider_total_tokens"
    )] == [17, 9, 26]
    assert "Private" not in repr(observation) and "private" not in repr(observation)
    client.close.assert_called_once()


@pytest.fixture
def local_search(monkeypatch, model_responses):
    papers = [{"title": "Original title", "abstract": "Original abstract", "doi": "original-doi"}]

    class Searcher:
        DEFAULT_SOURCES = ("fixture",)
        def __init__(self, **kwargs): pass
        def search_all(self, *args, **kwargs): return deepcopy(papers)
        def deduplicate(self, values): return values
        def enrich_batch_with_crossref(self, values): return values

    monkeypatch.setattr(search_service, "SimpleLiteratureSearcher", Searcher)

    def run(responses, mode="synthesis", facts=None):
        papers[:] = [{"title": f"Paper {index}", "abstract": "Synthetic abstract", "doi": f"fixture-{index}"}
                     for index in range(len(responses))]
        model_responses.responses.extend(responses)
        return search_service.run_online_knowledge_search(
            material="polyimide", mode=mode, api_key="fixture-key", base_url="https://invalid.example/v1",
            model="fixture-model", max_papers=len(responses), extraction_delay_seconds=0, execution_facts=facts,
        )

    return run


@pytest.mark.parametrize("mode", ["synthesis", "property"])
def test_all_invalid_results_fail_with_accurate_private_execution_facts(local_search, model_responses, mode):
    facts = {}
    with pytest.raises(search_service.OnlineKnowledgeModelError, match=INVALID_EXTRACTION_RESULT):
        local_search([{}, {}], mode, facts)
    assert facts == {"successful_extractions": 0, "failed_extractions": 2}
    assert model_responses.provider_boundary.call_count == 2
    model_responses.clients[0].close.assert_called_once()


@pytest.mark.parametrize("mode", ["synthesis", "property"])
def test_placeholder_only_results_are_failures_in_execution_facts(local_search, mode):
    response = positive(mode)
    if mode == "synthesis":
        response["reactions"] = [{"product_name": "N/A"}]
    else:
        response["data_points"] = [{"polymer_name": "not provided"}]
    facts = {}
    with pytest.raises(search_service.OnlineKnowledgeModelError, match=INVALID_EXTRACTION_RESULT):
        local_search([response], mode, facts)
    assert facts == {"successful_extractions": 0, "failed_extractions": 1}


@pytest.mark.parametrize("mode", ["synthesis", "property"])
def test_mixed_results_keep_good_rows_and_count_actual_failures(local_search, mode):
    facts = {}
    result = local_search([{}, positive(mode), negative(mode)], mode, facts)
    assert facts == {"successful_extractions": 2, "failed_extractions": 1}
    assert len(result["dataframe"]) == 1
    assert "successful_extractions" not in result
    assert result["exampleUsed"] is False


@pytest.mark.parametrize("mode", ["synthesis", "property"])
def test_explicit_no_result_completes_with_zero_rows(local_search, mode):
    facts = {}
    result = local_search([negative(mode)], mode, facts)
    assert facts == {"successful_extractions": 1, "failed_extractions": 0}
    assert result["dataframe"] == []
    assert result["syntheses"] == [] and result["propertyPoints"] == []


@pytest.fixture
def isolated_routes(monkeypatch, local_search):
    owner = "11111111-1111-1111-1111-111111111111"
    settings = SimpleNamespace(
        structured_data_backend="postgres", app_postgres_dsn="unused", online_knowledge_api_key="fixture-key",
        online_knowledge_base_url="https://invalid.example/v1", online_knowledge_model="fixture-model",
        online_knowledge_proxy_url="", online_knowledge_max_papers=1,
    )

    @contextmanager
    def connection(*args):
        yield object()

    monkeypatch.setattr(routes, "current_owner_id", lambda: owner)
    monkeypatch.setattr(routes, "current_identity", lambda: SimpleNamespace(user_id=owner, request_id="fixture-request"))
    monkeypatch.setattr(routes, "admission", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(routes, "authorize_start", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "postgres_connection", connection)
    monkeypatch.setattr(routes, "log_task_event", Mock())
    monkeypatch.setattr(routes.ne, "enabled", lambda: False)
    mocks = {}
    for name in ("mark_online_job_running_postgres", "mark_online_job_failed_postgres", "mark_online_job_completed_postgres",
                 "save_online_history_postgres", "update_online_job_progress_postgres"):
        mocks[name] = Mock()
        monkeypatch.setattr(routes, name, mocks[name])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings, postgres_connection_factory=connection)),
                              state=SimpleNamespace())
    body = OnlineKnowledgeSearchRequest(material="polyimide", max_papers=1, extraction_delay_seconds=0)
    return SimpleNamespace(request=request, body=body, settings=settings, mocks=mocks)


def test_sync_invalid_output_returns_502_without_saving_success_history(isolated_routes, model_responses, monkeypatch):
    monkeypatch.setattr(routes.ne, "enabled", lambda: True)
    model_responses.responses.append({})
    with pytest.raises(HTTPException) as caught:
        routes._search_online_knowledge_under_admission(isolated_routes.body, isolated_routes.request)
    assert caught.value.status_code == 502
    assert caught.value.detail == f"Model extraction failed: {INVALID_EXTRACTION_RESULT}"
    assert isolated_routes.request.state.online_entitlement_facts == {"successful_extractions": 0, "failed_extractions": 1}
    isolated_routes.mocks["save_online_history_postgres"].assert_not_called()


@pytest.mark.parametrize("mode", ["synthesis", "property"])
def test_sync_explicit_negative_completes_and_saves_zero_result(isolated_routes, model_responses, monkeypatch, mode):
    monkeypatch.setattr(routes.ne, "enabled", lambda: True)
    model_responses.responses.append(negative(mode))
    body = isolated_routes.body.model_copy(update={"mode": mode})
    result = routes._search_online_knowledge_under_admission(body, isolated_routes.request)
    assert result.dataframe == [] and result.syntheses == [] and result.propertyPoints == []
    assert isolated_routes.request.state.online_entitlement_facts == {
        "successful_extractions": 1, "failed_extractions": 0, "validated": True, "completion_proven": True,
    }
    isolated_routes.mocks["save_online_history_postgres"].assert_called_once()


def test_async_invalid_output_fails_without_completion_or_success_history(isolated_routes, model_responses):
    model_responses.responses.append({})
    access = routes.resolve_online_model_access(isolated_routes.body, isolated_routes.settings)
    routes._execute_online_knowledge_job("fixture-job", "unused", isolated_routes.body, access)
    mocks = isolated_routes.mocks
    mocks["mark_online_job_failed_postgres"].assert_called_once()
    assert mocks["mark_online_job_failed_postgres"].call_args.args[2] == f"Model extraction failed: {INVALID_EXTRACTION_RESULT}"
    mocks["mark_online_job_completed_postgres"].assert_not_called()
    mocks["save_online_history_postgres"].assert_not_called()


def test_safe_error_survives_repeated_sanitization_without_leaking_arbitrary_output():
    message = clean_ai_provider_error(extractor.InvalidExtractionResult())
    assert clean_ai_provider_error(search_service.OnlineKnowledgeModelError(message)) == INVALID_EXTRACTION_RESULT
    assert routes._public_job_error_message(search_service.OnlineKnowledgeModelError(message)) == f"Model extraction failed: {message}"
    assert clean_ai_provider_error(ValueError("sk-secret https://private.example/response")) == "AI provider request failed."
