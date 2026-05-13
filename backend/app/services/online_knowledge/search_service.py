from __future__ import annotations

import math
from typing import Any
from urllib.parse import urlparse

from app.services.online_knowledge.analytics import (
    build_property_points,
    build_stats,
    build_syntheses,
    catalyst_table,
    condition_summary,
    create_example_data,
    polymer_type_distribution,
    property_condition_distribution,
    property_name_distribution,
    reaction_type_table,
    relationship_distribution,
    solvent_distribution,
    temperature_distribution,
    temperature_labels,
)
from app.services.online_knowledge.extractor import PolymerExtractor
from app.services.online_knowledge.literature_searcher import SimpleLiteratureSearcher


class OnlineKnowledgeConfigError(RuntimeError):
    pass


class OnlineKnowledgeModelError(RuntimeError):
    pass


def run_online_knowledge_search(
    *,
    material: str,
    mode: str,
    api_key: str,
    base_url: str,
    model: str,
    max_papers: int,
    extraction_delay_seconds: float,
) -> dict[str, Any]:
    validate_model_access(api_key=api_key, base_url=base_url, model=model)
    searcher = SimpleLiteratureSearcher(max_workers=4)
    source_count = len(SimpleLiteratureSearcher.DEFAULT_SOURCES)
    per_source_limit = max(1, math.ceil(max_papers / source_count))
    raw_papers = searcher.search_all(material, max_papers=per_source_limit)
    papers = searcher.deduplicate(raw_papers)
    papers = searcher.enrich_batch_with_crossref(papers)
    papers = [paper for paper in papers if paper.get("abstract")][:max_papers]

    if papers:
        example_used = False
    else:
        papers = create_example_data(material, mode)
        example_used = True

    extractor = PolymerExtractor(api_key=api_key, base_url=base_url, model_name=model)
    extraction_results = extractor.process_papers(papers, mode=mode, delay=extraction_delay_seconds)
    rows = extractor.convert_to_rows(mode=mode)
    if extraction_results and all(result.get("error") for result in extraction_results):
        first_error = str(extraction_results[0].get("error") or "model call failed")
        raise OnlineKnowledgeModelError(first_error)
    syntheses = build_syntheses(rows) if mode == "synthesis" else []
    property_points = build_property_points(rows) if mode == "property" else []

    response_data = {
        "stats": build_stats(papers, rows, example_used, mode=mode),
        "syntheses": syntheses,
        "propertyPoints": property_points,
        "material": material,
        "mode": mode,
        "totalPapers": len(papers),
        "max_papers": max_papers,
        "exampleUsed": example_used,
        "temperatureDistribution": temperature_distribution(rows),
        "solventDistribution": solvent_distribution(rows),
        "catalystTable": catalyst_table(rows),
        "tempLabels": temperature_labels(rows),
        "conditionSummary": condition_summary(rows, mode=mode),
        "reactionTypeTable": reaction_type_table(rows),
        "propertyNameDistribution": property_name_distribution(rows),
        "conditionDistribution": property_condition_distribution(rows),
        "polymerTypeDistribution": polymer_type_distribution(rows),
        "relationshipDistribution": relationship_distribution(rows),
        "dataframe": rows,
    }
    return response_data


def validate_model_access(*, api_key: str, base_url: str, model: str) -> None:
    if not api_key:
        raise OnlineKnowledgeConfigError("API Key is required")
    if not model:
        raise OnlineKnowledgeConfigError("Model is required")
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.hostname:
        raise OnlineKnowledgeConfigError("Base URL must include protocol and host")
