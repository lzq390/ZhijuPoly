"""Synthetic PubMed XML regressions; never fetch papers or invoke real models."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import requests

from app.services.online_knowledge import extractor, literature_searcher, search_service


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("This regression suite must not issue external HTTP requests")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", forbidden)


def record(*, title="Synthetic polymer paper", abstract="", doi="10.5555/synthetic", extras="", authors=""):
    return f"""<PubmedArticle><MedlineCitation><Article>
<ArticleTitle>{title}</ArticleTitle>
<Journal><Title>Synthetic Journal</Title><JournalIssue><PubDate><Year>2025</Year></PubDate></JournalIssue></Journal>
{abstract}<AuthorList>{authors}</AuthorList></Article>
<OtherAbstract Language="fre"><AbstractText>Unrelated translated evidence</AbstractText></OtherAbstract>
</MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="pubmed">12345</ArticleId>
<ArticleId IdType="doi">{doi}</ArticleId></ArticleIdList>{extras}</PubmedData></PubmedArticle>"""


def parse(*records):
    return literature_searcher.SimpleLiteratureSearcher()._parse_pubmed(
        "<PubmedArticleSet>" + "".join(records) + "</PubmedArticleSet>"
    )


def test_title_and_abstract_preserve_inline_text_and_all_tails():
    paper = parse(record(
        title="T<sub>g</sub> and <i>aromatic</i> polymers in H<sub>2</sub>O",
        abstract="""<Abstract><AbstractText>A <i>synthetic</i> polymer has T<sub>g</sub> = 275 °C,
modulus 4 × 10<sup>3</sup> MPa, and <b>late</b> evidence after every inline tag.</AbstractText></Abstract>""",
    ))[0]
    assert paper["title"] == "Tg and aromatic polymers in H2O"
    assert paper["abstract"] == (
        "A synthetic polymer has Tg = 275 °C, modulus 4 × 10^(3) MPa, "
        "and late evidence after every inline tag."
    )


@pytest.mark.parametrize("xml, expected", [
    ("10<sup>3</sup>", "10^(3)"),
    ("10<sup>-3</sup>", "10^(-3)"),
    ("10<sup>−3</sup>", "10^(−3)"),
    ("10<sup>+3</sup>", "10^(+3)"),
    ("m<sup>-2</sup>", "m^(-2)"),
    ("cm<sup>-0.5</sup>", "cm^(-0.5)"),
    ("a<sup>n±1</sup>", "a^(n±1)"),
    ("a<sup><i>n</i> + 1</sup>", "a^(n + 1)"),
    ("Fe<sup>3+</sup>", "Fe^(3+)"),
    ("10<sup><b>-</b>3</sup>", "10^(-3)"),
    ("10<sup>2<sup>3</sup></sup>", "10^(2^(3))"),
    ("<i>10<sup>-3</sup> S</i> cm<sup>-1</sup>", "10^(-3) S cm^(-1)"),
    ("H<sub>2</sub>O and T<sub>g</sub>", "H2O and Tg"),
])
@pytest.mark.parametrize("field", ["title", "abstract"])
def test_superscripts_keep_grouped_scientific_meaning_and_inline_tails(xml, expected, field):
    fragment = f"Before {xml} after."
    paper = parse(record(
        title=fragment if field == "title" else "Synthetic polymer",
        abstract=f"<Abstract><AbstractText>{fragment}</AbstractText></Abstract>" if field == "abstract" else "",
    ))[0]
    assert paper[field] == f"Before {expected} after."


def test_empty_superscript_does_not_create_an_exponent_or_abstract_evidence():
    assert parse(record(abstract="<Abstract><AbstractText><sup> </sup></AbstractText></Abstract>"))[0]["abstract"] == ""


def test_structured_abstract_keeps_all_sections_labels_and_paragraph_boundaries():
    paper = parse(record(abstract="""<Abstract>
<AbstractText Label="  BACKGROUND  " NlmCategory="BACKGROUND">  Synthetic polymer evidence. </AbstractText>
<AbstractText Label="METHODS">Measurements in H<sub>2</sub>O.</AbstractText>
<AbstractText Label="RESULTS">Late T<sub>g</sub> = 275 °C and modulus = 40 MPa.</AbstractText>
<AbstractText>Final unlabelled <i>conclusion</i> and tail.</AbstractText>
</Abstract>"""))[0]
    assert paper["abstract"] == (
        "BACKGROUND: Synthetic polymer evidence.\n\n"
        "METHODS: Measurements in H2O.\n\n"
        "RESULTS: Late Tg = 275 °C and modulus = 40 MPa.\n\n"
        "Final unlabelled conclusion and tail."
    )
    assert "Unrelated translated evidence" not in paper["abstract"]


@pytest.mark.parametrize("abstract", [
    "", "<Abstract/>", "<Abstract><AbstractText/></Abstract>",
    "<Abstract><AbstractText Label='RESULTS'> \n <i/> </AbstractText></Abstract>",
    "<OtherAbstract><AbstractText>Different language</AbstractText></OtherAbstract>",
    "<Abstract><CopyrightInformation>Copyright is not an abstract</CopyrightInformation></Abstract>",
])
def test_missing_or_empty_primary_abstract_stays_empty(abstract):
    assert parse(record(abstract=abstract))[0]["abstract"] == ""


def test_inline_only_section_and_later_section_after_empty_first_are_not_missing():
    paper = parse(record(abstract="""<Abstract><AbstractText Label="EMPTY"/>
<AbstractText Label="RESULTS"><i>Polymer</i> has T<sub>g</sub> = 275 °C.</AbstractText></Abstract>"""))[0]
    assert paper["abstract"] == "RESULTS: Polymer has Tg = 275 °C."


def test_inline_only_title_and_absent_title_are_distinguished():
    papers = parse(record(title="<i>Polymer</i>", doi="10.5555/one"), record(title="", doi="10.5555/two"))
    assert [paper["title"] for paper in papers] == ["Polymer", ""]


def test_multiple_records_keep_own_article_metadata_and_ignore_nested_references():
    nested_reference = record(title="Nested unrelated paper", doi="10.5555/reference",
                              abstract="<Abstract><AbstractText>Unrelated referenced evidence</AbstractText></Abstract>")
    first = record(title="First <i>polymer</i>", doi="10.5555/first",
                   abstract="<Abstract><AbstractText>First complete abstract.</AbstractText></Abstract>",
                   extras=f"<ReferenceList><Reference>{nested_reference}</Reference></ReferenceList>")
    second = record(title="Second polymer", doi="10.5555/second",
                    abstract="<Abstract><AbstractText>Second complete abstract.</AbstractText></Abstract>")
    papers = parse(first, second)
    assert [(paper["title"], paper["doi"], paper["abstract"]) for paper in papers] == [
        ("First polymer", "10.5555/first", "First complete abstract."),
        ("Second polymer", "10.5555/second", "Second complete abstract."),
    ]


def test_non_article_subtrees_do_not_supply_missing_primary_metadata():
    xml = """<PubmedArticleSet><PubmedArticle><MedlineCitation>
<OtherAbstract><Article><ArticleTitle>Wrong title</ArticleTitle><Abstract><AbstractText>Wrong abstract</AbstractText></Abstract></Article></OtherAbstract>
</MedlineCitation><PubmedData><ReferenceList><Article><ArticleTitle>Reference title</ArticleTitle></Article></ReferenceList></PubmedData>
</PubmedArticle></PubmedArticleSet>"""
    assert literature_searcher.SimpleLiteratureSearcher()._parse_pubmed(xml) == []


def test_metadata_contract_retains_doi_author_limit_journal_and_year():
    authors = "".join(
        f"<Author><LastName>Family{index}</LastName><ForeName>Given{index}</ForeName></Author>"
        for index in range(1, 5)
    )
    paper = parse(record(authors=authors, abstract="<Abstract><AbstractText>Complete evidence.</AbstractText></Abstract>"))[0]
    assert paper == {
        "title": "Synthetic polymer paper", "abstract": "Complete evidence.", "doi": "10.5555/synthetic",
        "venue": "Synthetic Journal", "year": "2025", "authors": "Given1 Family1, Given2 Family2, Given3 Family3",
        "source": "PubMed",
    }


def test_malformed_xml_still_returns_no_papers():
    assert literature_searcher.SimpleLiteratureSearcher()._parse_pubmed("<PubmedArticleSet>") == []


def test_complete_later_evidence_reaches_real_extractor_prompt_without_fallback(monkeypatch):
    xml = "<PubmedArticleSet>" + record(
        title="T<sub>g</sub> of a <i>synthetic</i> polymer",
        abstract="""<Abstract><AbstractText Label="BACKGROUND">No measured properties in this introductory section.</AbstractText>
<AbstractText Label="RESULTS">The polymer has T<sub>g</sub> = 275 °C after a 2 h treatment and conductivity = 4 × 10<sup>-3</sup> S cm<sup>-1</sup>.</AbstractText></Abstract>""",
    ) + "</PubmedArticleSet>"
    calls = []
    prompts = []
    closed = []

    def get(_self, url, **kwargs):
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("esearch.fcgi"):
            return SimpleNamespace(status_code=200, json=lambda: {"esearchresult": {"idlist": ["12345"]}})
        assert url.endswith("efetch.fcgi")
        return SimpleNamespace(status_code=200, text=xml)

    def complete(**kwargs):
        prompt = kwargs["messages"][1]["content"]
        prompts.append(prompt)
        assert "BACKGROUND: No measured properties in this introductory section." in prompt
        assert "RESULTS: The polymer has Tg = 275 °C after a 2 h treatment and conductivity = 4 × 10^(-3) S cm^(-1)." in prompt
        assert "4 × 10-3" not in prompt
        assert "Tg of a synthetic polymer" in prompt
        assert "Unrelated translated evidence" not in prompt
        payload = {"has_polymer": True, "total_data_points": 1, "data_points": [{
            "polymer_name": "synthetic polymer", "property_name": "Tg", "property_value": "275 °C",
        }]}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])

    def forbidden_fallback(*args, **kwargs):
        raise AssertionError("Complete PubMed evidence must not be replaced with examples")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)), close=lambda: closed.append(True))
    monkeypatch.setattr(literature_searcher.SimpleLiteratureSearcher, "DEFAULT_SOURCES", ["pubmed"])
    monkeypatch.setattr(literature_searcher.SimpleLiteratureSearcher, "_get", get)
    monkeypatch.setattr(literature_searcher.time, "sleep", lambda _: None)
    monkeypatch.setattr(extractor, "create_openai_client", lambda **_: client)
    monkeypatch.setattr(search_service, "create_example_data", forbidden_fallback)
    result = search_service.run_online_knowledge_search(
        material="synthetic polymer", mode="property", api_key="synthetic-unused",
        base_url="https://unused.invalid/v1", model="synthetic-unused", max_papers=1, extraction_delay_seconds=0,
    )
    assert calls == ["esearch.fcgi", "efetch.fcgi"]
    assert len(prompts) == 1 and closed == [True]
    assert result["exampleUsed"] is False and result["totalPapers"] == 1
    assert result["dataframe"][0]["property_value"] == "275 °C"
    assert result["dataframe"][0]["paper_title"] == "Tg of a synthetic polymer"
