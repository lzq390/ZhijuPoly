from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests


Paper = dict[str, Any]


class SimpleLiteratureSearcher:
    DEFAULT_SOURCES = ["semantic_scholar", "pubmed", "openalex", "arxiv"]

    def __init__(self, max_workers: int = 4) -> None:
        self.headers = {
            "User-Agent": "PolyProp/0.1 literature retrieval",
            "Accept": "application/json",
        }
        self.max_workers = max_workers

    def get_doi_from_crossref(self, title: str) -> str | None:
        try:
            response = requests.get(
                "https://api.crossref.org/works",
                params={"query.title": title, "rows": 1},
                headers=self.headers,
                timeout=15,
            )
            response.raise_for_status()
            items = response.json().get("message", {}).get("items", [])
            if items:
                return items[0].get("DOI")
        except requests.RequestException:
            return None
        return None

    def enrich_with_crossref(self, paper: Paper) -> Paper:
        if paper.get("abstract"):
            return paper

        doi = paper.get("doi") or self.get_doi_from_crossref(str(paper.get("title") or ""))
        if not doi:
            return paper

        paper["doi"] = doi
        try:
            response = requests.get(
                f"https://api.crossref.org/works/{doi}",
                headers=self.headers,
                timeout=15,
            )
            response.raise_for_status()
            abstract = response.json().get("message", {}).get("abstract", "")
            if abstract:
                paper["abstract"] = re.sub(r"<[^>]+>", "", abstract).strip()
                paper["abstract_source"] = "Crossref"
        except requests.RequestException:
            return paper

        return paper

    def search_semantic_scholar(self, query: str, limit: int = 200) -> list[Paper]:
        all_results: list[Paper] = []
        offset = 0

        while len(all_results) < limit:
            params = {
                "query": query,
                "limit": min(100, limit - len(all_results)),
                "offset": offset,
                "fields": "title,abstract,venue,externalIds,year,authors",
            }
            try:
                response = requests.get(
                    "https://api.semanticscholar.org/graph/v1/paper/search",
                    params=params,
                    headers=self.headers,
                    timeout=30,
                )
                if response.status_code != 200:
                    break
                papers = response.json().get("data", [])
                if not papers:
                    break

                for paper in papers:
                    external_ids = paper.get("externalIds") or {}
                    all_results.append(
                        {
                            "title": paper.get("title") or "",
                            "venue": paper.get("venue") or "",
                            "abstract": paper.get("abstract") or "",
                            "year": paper.get("year") or "",
                            "authors": ", ".join(
                                author.get("name", "") for author in (paper.get("authors") or [])[:3]
                            ),
                            "doi": external_ids.get("DOI") or "",
                            "source": "Semantic Scholar",
                        }
                    )

                offset += len(papers)
                time.sleep(0.5)
            except requests.RequestException:
                break

        return all_results

    def search_arxiv(self, query: str, max_results: int = 500) -> list[Paper]:
        all_results: list[Paper] = []
        start = 0
        batch_size = 100

        while len(all_results) < max_results:
            params = {
                "search_query": f'all:"{query}"',
                "start": start,
                "max_results": min(batch_size, max_results - len(all_results)),
                "sortBy": "relevance",
            }
            try:
                response = requests.get(
                    "http://export.arxiv.org/api/query",
                    params=params,
                    headers=self.headers,
                    timeout=30,
                )
                if response.status_code != 200:
                    break
                papers = self._parse_arxiv(response.text)
                if not papers:
                    break
                all_results.extend(papers)
                start += len(papers)
                time.sleep(1)
            except requests.RequestException:
                break

        return all_results

    def _parse_arxiv(self, xml_content: str) -> list[Paper]:
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError:
            return []

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        papers: list[Paper] = []

        for entry in root.findall("atom:entry", ns):
            title_elem = entry.find("atom:title", ns)
            summary_elem = entry.find("atom:summary", ns)
            id_elem = entry.find("atom:id", ns)
            published_elem = entry.find("atom:published", ns)
            arxiv_id = id_elem.text.split("/")[-1] if id_elem is not None and id_elem.text else ""
            authors = []
            for author in entry.findall("atom:author", ns):
                name_elem = author.find("atom:name", ns)
                if name_elem is not None and name_elem.text:
                    authors.append(name_elem.text)

            papers.append(
                {
                    "title": title_elem.text.strip() if title_elem is not None and title_elem.text else "",
                    "venue": "arXiv",
                    "abstract": summary_elem.text.strip() if summary_elem is not None and summary_elem.text else "",
                    "doi": f"arXiv:{arxiv_id}" if arxiv_id else "",
                    "year": published_elem.text[:4] if published_elem is not None and published_elem.text else "",
                    "authors": ", ".join(authors[:3]),
                    "source": "arXiv",
                }
            )

        return papers

    def search_openalex(self, query: str, per_page: int = 500) -> list[Paper]:
        all_results: list[Paper] = []
        page = 1

        while len(all_results) < per_page:
            params = {
                "search": query,
                "per-page": min(200, per_page - len(all_results)),
                "page": page,
            }
            try:
                response = requests.get(
                    "https://api.openalex.org/works",
                    params=params,
                    headers=self.headers,
                    timeout=30,
                )
                if response.status_code != 200:
                    break
                results = response.json().get("results", [])
                if not results:
                    break

                for result in results:
                    primary_location = result.get("primary_location") or {}
                    source = primary_location.get("source") or {}
                    all_results.append(
                        {
                            "title": result.get("title") or "",
                            "venue": source.get("display_name") or "",
                            "abstract": result.get("abstract") or _openalex_abstract(result),
                            "doi": (result.get("doi") or "").replace("https://doi.org/", ""),
                            "year": result.get("publication_year") or "",
                            "authors": ", ".join(
                                author.get("author", {}).get("display_name", "")
                                for author in (result.get("authorships") or [])[:3]
                            ),
                            "source": "OpenAlex",
                        }
                    )

                page += 1
                time.sleep(0.5)
            except requests.RequestException:
                break

        return all_results

    def search_pubmed(self, query: str, max_results: int = 500) -> list[Paper]:
        search_params = {
            "db": "pubmed",
            "term": query,
            "retmax": max_results,
            "retmode": "json",
        }
        try:
            search_response = requests.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                params=search_params,
                headers=self.headers,
                timeout=30,
            )
            if search_response.status_code != 200:
                return []
            id_list = search_response.json().get("esearchresult", {}).get("idlist", [])
            if not id_list:
                return []

            all_papers: list[Paper] = []
            for index in range(0, len(id_list), 200):
                batch_ids = id_list[index : index + 200]
                fetch_response = requests.get(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                    params={"db": "pubmed", "id": ",".join(batch_ids), "retmode": "xml"},
                    headers=self.headers,
                    timeout=30,
                )
                if fetch_response.status_code == 200:
                    all_papers.extend(self._parse_pubmed(fetch_response.text))
                time.sleep(0.5)
            return all_papers
        except requests.RequestException:
            return []

    def _parse_pubmed(self, xml_content: str) -> list[Paper]:
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError:
            return []

        papers: list[Paper] = []
        for article in root.findall(".//PubmedArticle"):
            title_elem = article.find(".//ArticleTitle")
            journal_elem = article.find(".//Journal/Title")
            abstract_elem = article.find(".//AbstractText")
            year_elem = article.find(".//PubDate/Year")

            doi = ""
            for article_id in article.findall(".//ArticleId"):
                if article_id.get("IdType") == "doi":
                    doi = article_id.text or ""
                    break

            authors = []
            for author in article.findall(".//Author")[:3]:
                last_name = author.find(".//LastName")
                fore_name = author.find(".//ForeName")
                if last_name is not None and last_name.text:
                    name = last_name.text
                    if fore_name is not None and fore_name.text:
                        name = f"{fore_name.text} {name}"
                    authors.append(name)

            papers.append(
                {
                    "title": title_elem.text if title_elem is not None and title_elem.text else "",
                    "venue": journal_elem.text if journal_elem is not None and journal_elem.text else "",
                    "abstract": abstract_elem.text if abstract_elem is not None and abstract_elem.text else "",
                    "doi": doi,
                    "year": year_elem.text if year_elem is not None and year_elem.text else "",
                    "authors": ", ".join(authors),
                    "source": "PubMed",
                }
            )

        return papers

    def search_all(self, query: str, sources: list[str] | None = None, max_papers: int = 500) -> list[Paper]:
        selected_sources = sources or self.DEFAULT_SOURCES
        all_papers: list[Paper] = []

        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(selected_sources))) as executor:
            futures = {}
            for source in selected_sources:
                if source == "semantic_scholar":
                    futures[executor.submit(self.search_semantic_scholar, query, max_papers)] = source
                elif source == "arxiv":
                    futures[executor.submit(self.search_arxiv, query, max_papers)] = source
                elif source == "openalex":
                    futures[executor.submit(self.search_openalex, query, max_papers)] = source
                elif source == "pubmed":
                    futures[executor.submit(self.search_pubmed, query, max_papers)] = source

            for future in as_completed(futures):
                try:
                    all_papers.extend(future.result(timeout=120))
                except Exception:
                    continue

        return all_papers

    def deduplicate(self, papers: list[Paper]) -> list[Paper]:
        seen_titles: set[str] = set()
        unique: list[Paper] = []

        for paper in papers:
            title = str(paper.get("title") or "").lower().strip()
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            unique.append(paper)

        return unique

    def enrich_batch_with_crossref(self, papers: list[Paper], max_workers: int = 5) -> list[Paper]:
        papers_to_enrich = [paper for paper in papers if not paper.get("abstract")]
        papers_with_abstract = [paper for paper in papers if paper.get("abstract")]
        enriched: list[Paper] = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self.enrich_with_crossref, paper): paper for paper in papers_to_enrich}
            for future in as_completed(futures):
                try:
                    enriched.append(future.result(timeout=20))
                except Exception:
                    enriched.append(futures[future])

        enriched.extend(papers_with_abstract)
        return enriched


def _openalex_abstract(result: Paper) -> str:
    inverted = result.get("abstract_inverted_index")
    if not isinstance(inverted, dict):
        return ""

    words: list[tuple[int, str]] = []
    for word, positions in inverted.items():
        if isinstance(positions, list):
            words.extend((int(position), str(word)) for position in positions)
    return " ".join(word for _, word in sorted(words))
