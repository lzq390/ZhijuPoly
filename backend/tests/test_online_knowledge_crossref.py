from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
import requests

from app.services.online_knowledge import extractor, literature_searcher, search_service


TITLE = "Polyimide imidization at 150 °C"
DOI = "10.5555/polyimide"
ABSTRACT = "Polyimide evidence from the matching publication."


def record(*, doi=DOI, title=TITLE, abstract=ABSTRACT):
    return {"DOI": doi, "title": [title], "abstract": abstract}


def paper(**changes):
    return {"title": TITLE, "doi": "", "abstract": "", "source": "fixture", **changes}


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


@pytest.fixture
def crossref(monkeypatch):
    calls = []
    responses = []

    def get(_session, url, **kwargs):
        calls.append((url, kwargs))
        assert responses, "Unexpected HTTP call; real network is prohibited"
        response = responses.pop(0)
        if isinstance(response, requests.RequestException):
            raise response
        return Response(response)

    monkeypatch.setattr(literature_searcher.requests.Session, "get", get)
    monkeypatch.setattr(
        literature_searcher, "provider_http_request", lambda request, *args, **kwargs: request(*args, **kwargs)
    )
    return SimpleNamespace(calls=calls, responses=responses)


def test_title_lookup_ignores_first_wrong_match_and_selects_second(crossref):
    crossref.responses.extend([
        {"message": {"items": [record(doi="10.5555/marine", title="Marine ecology"), record()]}},
        {"message": record(abstract="<jats:p>Polyimide &amp; synthesis.</jats:p>")},
    ])
    result = literature_searcher.SimpleLiteratureSearcher().enrich_with_crossref(paper())
    assert result == paper(doi=DOI, abstract="Polyimide & synthesis.", abstract_source="Crossref")
    assert crossref.calls[0][1]["params"] == {"query.title": TITLE, "rows": 5}
    assert crossref.calls[1][0] == f"https://api.crossref.org/works/{DOI}"


@pytest.mark.parametrize("candidate", [
    "POLYIMIDE IMIDIZATION AT 150 °C",
    "  Polyimide\n imidization\t at\u00a0150 °C  ",
    "<i>Polyimide</i> imidization at 150 &#176;C",
])
def test_title_lookup_accepts_formatting_differences(crossref, candidate):
    crossref.responses.append({"message": {"items": [record(title=candidate)]}})
    assert literature_searcher.SimpleLiteratureSearcher().get_doi_from_crossref(TITLE) == DOI


def test_title_lookup_accepts_canonical_unicode_equivalence(crossref):
    crossref.responses.append({"message": {"items": [record(title="Cafe\u0301 polymer")]}})
    assert literature_searcher.SimpleLiteratureSearcher().get_doi_from_crossref("Café polymer") == DOI


@pytest.mark.parametrize("candidate", [
    "Polyimide imidization at 50 °C",
    "Polyamide imidization at 150 °C",
    "Imidization Polyimide at 150 °C",
    "Polyimide imidization at 150 °C: a review",
    "Polyimide imidization at 150 °C!",
    "",
    None,
])
def test_title_lookup_rejects_different_identity_regardless_of_score(crossref, candidate):
    crossref.responses.append({"message": {"items": [{**record(title=candidate), "score": 999999}]}})
    assert literature_searcher.SimpleLiteratureSearcher().get_doi_from_crossref(TITLE) is None


@pytest.mark.parametrize("items, expected", [
    ([record(), record(doi=DOI.upper())], DOI),
    ([record(), record(doi="10.5555/another")], None),
    ([record(doi="not-a-doi")], None),
    ([record(doi="arXiv:1234.56789")], None),
    ([record(doi=None)], None),
    ([None, "invalid", record()], DOI),
    ([record(title="different")] * 5 + [record()], None),
])
def test_title_lookup_requires_one_valid_unique_doi_among_five_candidates(crossref, items, expected):
    crossref.responses.append({"message": {"items": items}})
    assert literature_searcher.SimpleLiteratureSearcher().get_doi_from_crossref(TITLE) == expected


@pytest.mark.parametrize("payload", [
    None, [], {}, {"message": None}, {"message": []}, {"message": {}},
    {"message": {"items": None}}, {"message": {"items": {}}},
    {"message": {"items": [{"DOI": DOI, "title": TITLE}]}},
    ValueError("invalid JSON"), requests.Timeout("fixture timeout"),
    requests.HTTPError("fixture HTTP failure"),
])
def test_title_lookup_failure_preserves_original_paper(crossref, payload):
    crossref.responses.append(payload)
    original = paper()
    before = deepcopy(original)
    result = literature_searcher.SimpleLiteratureSearcher().enrich_with_crossref(original)
    assert result is original
    assert original == before
    assert len(crossref.calls) == 1


@pytest.mark.parametrize("detail", [
    {"message": record(doi="10.5555/wrong")},
    {"message": record(title="Different publication")},
    {"message": record(abstract="")},
    {"message": record(abstract="<jats:p> \n </jats:p>")},
    {"message": record(abstract=None)},
    {"message": record(abstract={"invalid": "abstract"})},
    {"message": {"abstract": ABSTRACT}},
    {"message": None}, None, [], ValueError("invalid JSON"),
    requests.Timeout("fixture timeout"), requests.HTTPError("fixture HTTP failure"),
])
def test_detail_failure_does_not_partially_write_title_derived_doi(crossref, detail):
    crossref.responses.extend([{"message": {"items": [record()]}}, detail])
    original = paper()
    before = deepcopy(original)
    result = literature_searcher.SimpleLiteratureSearcher().enrich_with_crossref(original)
    assert result is original
    assert original == before
    assert len(crossref.calls) == 2


def test_existing_doi_uses_exact_identity_without_title_search(crossref):
    crossref.responses.append({"message": record(title="A source's alternate title")})
    original = paper(doi=DOI.upper())
    result = literature_searcher.SimpleLiteratureSearcher().enrich_with_crossref(original)
    assert result == paper(doi=DOI.upper(), abstract=ABSTRACT, abstract_source="Crossref")
    assert len(crossref.calls) == 1
    assert crossref.calls[0][0] == f"https://api.crossref.org/works/{DOI}"


def test_existing_doi_rejects_wrong_detail_identity(crossref):
    crossref.responses.append({"message": record(doi="10.5555/wrong")})
    original = paper(doi=DOI)
    assert literature_searcher.SimpleLiteratureSearcher().enrich_with_crossref(original) == paper(doi=DOI)


def test_doi_url_quotes_suffix_query_and_fragment_characters(crossref):
    doi = "10.5555/article?part#section"
    crossref.responses.append({"message": record(doi=doi)})
    literature_searcher.SimpleLiteratureSearcher().enrich_with_crossref(paper(doi=doi))
    assert crossref.calls[0][0] == "https://api.crossref.org/works/10.5555/article%3Fpart%23section"


@pytest.mark.parametrize("original", [
    paper(abstract="Existing abstract"), paper(title=""), paper(title="<i> </i>"),
    paper(doi="arXiv:1234.56789"), paper(doi="invalid-doi"),
])
def test_no_lookup_for_existing_abstract_empty_title_or_non_doi_identifier(crossref, original):
    before = deepcopy(original)
    assert literature_searcher.SimpleLiteratureSearcher().enrich_with_crossref(original) == before
    assert crossref.calls == []


@pytest.mark.parametrize("include_good_paper", [True, False])
def test_service_never_sends_unrelated_crossref_evidence_to_model(crossref, monkeypatch, include_good_paper):
    unrelated = record(doi="10.5555/marine", title="Marine ecology", abstract="Wrong marine evidence")
    crossref.responses.append({"message": {"items": [unrelated]}})
    papers = [paper()]
    if include_good_paper:
        papers.append(paper(title="Known good publication", doi="10.5555/good", abstract=ABSTRACT))
    monkeypatch.setattr(literature_searcher.SimpleLiteratureSearcher, "search_all", lambda *args, **kwargs: papers)
    prompts = []
    closed = []

    def complete(**kwargs):
        prompts.append(kwargs["messages"][1]["content"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"has_polymerization": false, "total_reactions": 0, "reactions": []}'
        ))])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=complete)), close=lambda: closed.append(True)
    )
    monkeypatch.setattr(extractor, "create_openai_client", lambda **kwargs: client)
    monkeypatch.setattr(extractor, "provider_call", lambda fn, **kwargs: fn(**kwargs))
    result = search_service.run_online_knowledge_search(
        material="polyimide", mode="synthesis", api_key="fixture-key",
        base_url="https://unused.invalid/v1", model="fixture-model", max_papers=2,
        extraction_delay_seconds=0,
    )
    assert prompts
    assert all("Wrong marine evidence" not in prompt and TITLE not in prompt for prompt in prompts)
    assert result["exampleUsed"] is (not include_good_paper)
    if include_good_paper:
        assert result["totalPapers"] == 1
        assert "Known good publication" in prompts[0] and ABSTRACT in prompts[0]
    assert len(crossref.calls) == 1
    assert closed == [True]
