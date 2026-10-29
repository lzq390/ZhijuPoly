"""Literature transport regression tests; no external HTTP or model calls."""
from __future__ import annotations

import json
from copy import deepcopy
from contextvars import ContextVar
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import requests

from app.services import usage_provider
from app.services.online_knowledge import literature_searcher, search_service


PROXY = "http://proxy.example.test:17892"
DOI = "10.5555/polymer"
TITLE = "Polymer properties"
ABSTRACT = "A polymer exhibits a measured glass transition temperature of 120 degrees Celsius."
ARXIV_XML = f"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<title>{TITLE}</title><summary>{ABSTRACT}</summary><id>https://arxiv.org/abs/1234.56789</id>
<published>2025-01-01</published></entry></feed>"""
PUBMED_XML = f"""<PubmedArticleSet><PubmedArticle><MedlineCitation><Article><ArticleTitle>{TITLE}</ArticleTitle>
<Abstract><AbstractText>{ABSTRACT}</AbstractText></Abstract></Article></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="doi">{DOI}</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle></PubmedArticleSet>"""


@pytest.fixture
def transport(monkeypatch):
    """Exercise requests preparation/environment handling up to the adapter."""
    calls = []
    sessions = []
    owner = ContextVar("literature_transport_owner", default=None)
    state = SimpleNamespace(calls=calls, sessions=sessions, owner=owner, statuses={}, redirect=False, fail=False)
    original_close = requests.Session.close

    def close(session):
        sessions.append(session)
        original_close(session)

    def netrc_forbidden(*args, **kwargs):
        raise AssertionError("Source requests must not load ambient netrc credentials")

    def send(adapter, request, **kwargs):
        parts = urlsplit(request.url)
        calls.append((request, kwargs, owner.get()))
        assert "Authorization" not in request.headers
        assert "Proxy-Authorization" not in request.headers
        assert request.headers["User-Agent"] == "PolyProp/0.1 literature retrieval"
        assert kwargs["timeout"] in (15, 30)
        assert kwargs["verify"] is True
        if state.fail:
            raise requests.Timeout("Controlled transport timeout")
        response = requests.Response()
        response.status_code = state.statuses.get(parts.hostname, 200)
        response.url = request.url
        response.request = request
        if state.redirect and "redirected=1" not in parts.query:
            response.status_code = 302
            response.headers["Location"] = "https://api.openalex.org/works?redirected=1"
            response._content = b""
            return response
        if parts.hostname == "api.semanticscholar.org":
            payload = {"data": [{"title": TITLE, "abstract": ABSTRACT, "externalIds": {"DOI": DOI}}]}
        elif parts.hostname == "api.openalex.org":
            payload = {"results": [{"title": TITLE, "abstract_inverted_index": {"Polymer": [0], "evidence": [1]}}]}
        elif parts.hostname == "export.arxiv.org":
            assert parts.scheme == "https"
            payload = ARXIV_XML
        elif parts.hostname == "eutils.ncbi.nlm.nih.gov":
            payload = {"esearchresult": {"idlist": ["12345"]}} if "esearch.fcgi" in parts.path else PUBMED_XML
        elif parts.hostname == "api.crossref.org":
            record = {"DOI": DOI, "title": [TITLE], "abstract": ABSTRACT}
            payload = {"message": {"items": [record]}} if parts.path == "/works" else {"message": record}
        else:
            raise AssertionError(f"Unexpected HTTP endpoint: {parts.hostname}")
        response._content = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    monkeypatch.setattr(requests.Session, "close", close)
    monkeypatch.setattr(requests.sessions, "get_netrc_auth", netrc_forbidden)
    monkeypatch.setattr(literature_searcher.time, "sleep", lambda _: None)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://ambient-proxy-must-not-be-used.invalid:9999")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("NETRC", "/ambient-netrc-must-not-be-read")
    return state


@pytest.mark.parametrize("proxy_url", ["", PROXY])
@pytest.mark.parametrize("operation, expected_requests", [
    ("semantic_scholar", 1), ("openalex", 1), ("arxiv", 1), ("pubmed", 2),
    ("crossref_title", 1), ("crossref_enrichment", 2),
])
def test_every_source_uses_explicit_transport_without_environment_or_credentials(
    transport, proxy_url, operation, expected_requests,
):
    searcher = literature_searcher.SimpleLiteratureSearcher(proxy_url=proxy_url)
    if operation == "crossref_title":
        assert searcher.get_doi_from_crossref(TITLE) == DOI
    elif operation == "crossref_enrichment":
        assert searcher.enrich_with_crossref({"title": TITLE})["abstract"] == ABSTRACT
    else:
        assert searcher.search_all("polymer", [operation], max_papers=1)[0]["abstract"]

    assert len(transport.calls) == expected_requests
    assert len(transport.sessions) == expected_requests
    # Hold references to prove each request used a distinct closed session.
    assert len({id(session) for session in transport.sessions}) == expected_requests
    assert all(session.trust_env is False for session in transport.sessions)
    expected_proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {}
    assert all(call[1]["proxies"] == expected_proxies for call in transport.calls)


def test_parallel_source_and_crossref_requests_keep_owner_and_provider_accounting(transport, monkeypatch):
    spans = []

    class Span:
        def __init__(self, module, kind):
            self.module, self.kind = module, kind
            self.finished = []
            spans.append(self)

        def start(self, *, bind=False):
            assert bind is False
            assert transport.owner.get() == "owner-A"

        def finish(self, *, status, metrics):
            self.finished.append((status, metrics))

    monkeypatch.setattr(usage_provider, "current_usage_context", lambda: SimpleNamespace(module="online_knowledge"))
    monkeypatch.setattr(usage_provider, "usage_span", lambda module, *, kind: Span(module, kind))
    searcher = literature_searcher.SimpleLiteratureSearcher(proxy_url=PROXY)
    token = transport.owner.set("owner-A")
    try:
        papers = searcher.search_all("private material", max_papers=1)
        assert len(papers) == 4
        assert searcher.enrich_batch_with_crossref([{"title": TITLE}])[0]["abstract"] == ABSTRACT
    finally:
        transport.owner.reset(token)

    assert len(transport.calls) == len(spans) == 7
    assert all(call[2] == "owner-A" for call in transport.calls)
    assert all(span.module == "online_knowledge" and span.kind == "provider" for span in spans)
    assert all(len(span.finished) == 1 and span.finished[0][0] == "completed" for span in spans)
    observations = [span.finished[0][1] for span in spans]
    assert all(set(metrics) == {*usage_provider._TOKEN_FIELDS, "observed_wall_ms"} for metrics in observations)
    assert all(all(metrics[field] is None for field in usage_provider._TOKEN_FIELDS) for metrics in observations)
    assert "private material" not in repr(observations) and ABSTRACT not in repr(observations)


def test_source_429_does_not_retry_or_replace_other_real_papers_with_examples(transport, monkeypatch):
    transport.statuses["api.semanticscholar.org"] = 429
    extractor_options = []
    extracted = []

    class Extractor:
        def __init__(self, **kwargs):
            extractor_options.append(kwargs)

        def process_papers(self, papers, **kwargs):
            extracted.extend(papers)
            return [{"error": None} for _ in papers]

        def convert_to_rows(self, **kwargs):
            return []

        def close(self):
            pass

    monkeypatch.setattr(search_service, "PolymerExtractor", Extractor)
    result = search_service.run_online_knowledge_search(
        material="polymer", mode="property", api_key="model-key-must-not-reach-sources",
        base_url="https://model.example.test/v1", model="fixture-model", max_papers=1,
        extraction_delay_seconds=0, proxy_url=PROXY,
    )
    assert result["exampleUsed"] is False
    assert result["totalPapers"] == len(extracted) == 1
    assert extracted[0]["source"] in {"PubMed", "OpenAlex", "arXiv"}
    assert sum(urlsplit(call[0].url).hostname == "api.semanticscholar.org" for call in transport.calls) == 1
    assert all(call[1]["proxies"] == {"http": PROXY, "https": PROXY} for call in transport.calls)
    assert extractor_options[0]["proxy_url"] == PROXY
    assert "model-key-must-not-reach-sources" not in repr([call[0].headers for call in transport.calls])


@pytest.mark.parametrize("proxy_url", ["", PROXY])
def test_source_redirects_keep_the_explicit_transport(transport, proxy_url):
    transport.redirect = True
    searcher = literature_searcher.SimpleLiteratureSearcher(proxy_url=proxy_url)
    assert searcher.search_openalex("polymer", per_page=1)[0]["abstract"] == "Polymer evidence"
    assert len(transport.calls) == 2
    assert len(transport.sessions) == 1
    expected_proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {}
    assert all(call[1]["proxies"] == expected_proxies for call in transport.calls)


def test_failed_source_closes_its_session_without_retrying(transport):
    transport.fail = True
    searcher = literature_searcher.SimpleLiteratureSearcher(proxy_url=PROXY)
    assert searcher.search_openalex("polymer", per_page=1) == []
    assert len(transport.calls) == len(transport.sessions) == 1


@pytest.mark.parametrize("complete_first", [False, True])
def test_same_doi_duplicates_keep_the_complete_source_record_in_either_arrival_order(complete_first):
    incomplete = {"title": TITLE, "doi": f" {DOI.upper()} ", "abstract": "", "source": "Semantic Scholar",
                  "authors": "Incomplete-source author"}
    complete = {"title": TITLE, "doi": DOI, "abstract": ABSTRACT, "source": "PubMed",
                "authors": "Complete-source author"}
    papers = [complete, incomplete] if complete_first else [incomplete, complete]
    before = deepcopy(papers)
    result = literature_searcher.SimpleLiteratureSearcher().deduplicate(papers)
    assert result == [complete]
    assert result[0] is complete
    assert papers == before


def test_same_title_different_dois_are_distinct_papers_and_never_mix_evidence():
    first = {"title": TITLE, "doi": DOI, "abstract": "", "source": "Semantic Scholar"}
    second = {"title": TITLE, "doi": "10.5555/another", "abstract": ABSTRACT, "source": "PubMed"}
    result = literature_searcher.SimpleLiteratureSearcher().deduplicate([first, second])
    assert result == [first, second]
    assert result[0]["abstract"] == "" and result[1]["doi"] == "10.5555/another"


@pytest.mark.parametrize("first_doi,second_doi", [("", ""), ("", DOI), (DOI, ""),
                                                  ("arXiv:1234.56789", "arXiv:9876.54321")])
def test_same_title_without_verified_doi_identity_preserves_separate_source_records(first_doi, second_doi):
    papers = [
        {"title": TITLE, "doi": first_doi, "abstract": "", "source": "First source"},
        {"title": TITLE, "doi": second_doi, "abstract": ABSTRACT, "source": "Second source"},
    ]
    assert literature_searcher.SimpleLiteratureSearcher().deduplicate(papers) == papers


@pytest.mark.parametrize("complete_first", [False, True])
def test_duplicate_arrival_order_cannot_replace_real_evidence_with_fallback_during_crossref_outage(
    monkeypatch, complete_first,
):
    incomplete = {"title": TITLE, "doi": DOI, "abstract": "", "source": "Semantic Scholar"}
    complete = {"title": TITLE, "doi": DOI, "abstract": ABSTRACT, "source": "PubMed"}
    papers = [complete, incomplete] if complete_first else [incomplete, complete]
    crossref_calls = []
    extracted = []
    closed = []

    def unavailable_crossref(*args, **kwargs):
        crossref_calls.append(True)
        raise requests.Timeout("Controlled Crossref outage; no network is used")

    class Extractor:
        def __init__(self, **kwargs):
            pass

        def process_papers(self, values, **kwargs):
            extracted.extend(values)
            return [{"has_polymer": False, "total_data_points": 0, "data_points": []} for _ in values]

        def convert_to_rows(self, **kwargs):
            return []

        def close(self):
            closed.append(True)

    monkeypatch.setattr(literature_searcher.SimpleLiteratureSearcher, "search_all", lambda *args, **kwargs: papers)
    monkeypatch.setattr(literature_searcher.SimpleLiteratureSearcher, "_get", unavailable_crossref)
    monkeypatch.setattr(search_service, "PolymerExtractor", Extractor)
    result = search_service.run_online_knowledge_search(
        material="polymer", mode="property", api_key="fixture-key", base_url="https://invalid.example/v1",
        model="fixture-model", max_papers=2, extraction_delay_seconds=0,
    )
    assert result["exampleUsed"] is False
    assert result["totalPapers"] == 1
    assert extracted == [complete]
    assert crossref_calls == []
    assert closed == [True]
