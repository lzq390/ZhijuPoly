from __future__ import annotations

import re
from typing import Any


OR_QUERY_SEPARATOR = re.compile(r"\s+OR\s+", re.IGNORECASE)
IUPAC_TOKEN_SEPARATOR = re.compile(r"[\s,;:/()\[\]{}]+")
LEADING_LOCANT = re.compile(r"^\d+(?:,\d+)*(?:-\d+)*-+")
GENERIC_IUPAC_TOKENS = {
    "acid",
    "amine",
    "ester",
    "polymer",
    "resin",
    "the",
    "and",
    "or",
    "of",
    "with",
}
MAX_EXPANDED_TERMS = 24

SEARCH_FIELDS = (
    ("polymer_iupac", "Polymer", 8),
    ("formulation", "Formulation", 6),
    ("title_en", "Title", 4),
    ("title_zh", "Title", 4),
    ("claim", "Claim", 2),
    ("abstract", "Abstract", 1),
)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fallback_query_terms(query: str) -> list[str]:
    parts = [part.strip() for part in OR_QUERY_SEPARATOR.split(query.strip())]
    if len(parts) > 1 and all(parts):
        return parts
    return [query]


def _iter_iupac_fragments(term: str) -> list[str]:
    fragments: list[str] = []
    for token in IUPAC_TOKEN_SEPARATOR.split(term):
        value = token.strip("()[]{}\"'.,;:-")
        if (
            len(value) < 4
            or value.casefold() in GENERIC_IUPAC_TOKENS
            or not any(character.isalpha() for character in value)
        ):
            continue

        fragments.append(value)
        locant_free = LEADING_LOCANT.sub("", value)
        if (
            locant_free
            and locant_free != value
            and len(locant_free) >= 4
            and locant_free.casefold() not in GENERIC_IUPAC_TOKENS
            and any(character.isalpha() for character in locant_free)
        ):
            fragments.append(locant_free)

    return fragments


def normalize_search_terms(query: str, terms: list[str] | None = None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()

    source_terms = terms if terms else _fallback_query_terms(query)
    for term in source_terms:
        value = term.strip()
        if not value:
            continue

        for candidate in [value, *_iter_iupac_fragments(value)]:
            key = candidate.casefold()
            if key in seen:
                continue

            seen.add(key)
            normalized.append(candidate)
            if len(normalized) >= MAX_EXPANDED_TERMS:
                return normalized

    return normalized


def build_abstract_snippet(abstract: str, query: str, context_size: int = 130) -> str:
    normalized_abstract = abstract.casefold()
    normalized_query = query.casefold()
    match_index = normalized_abstract.find(normalized_query)
    if match_index < 0:
        return abstract[: context_size * 2].strip()

    start = max(match_index - context_size, 0)
    end = min(match_index + len(query) + context_size, len(abstract))
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(abstract) else ""
    return f"{prefix}{abstract[start:end].strip()}{suffix}"


def best_abstract_snippet_query(abstract: str, query: str, terms: list[str]) -> str:
    normalized_abstract = abstract.casefold()
    for term in terms:
        if term.casefold() in normalized_abstract:
            return term
    return query


def get_knowledge_match_metadata(row: Any, terms: list[str]) -> tuple[list[str], list[str]]:
    matched_terms: list[str] = []
    matched_fields: list[str] = []

    for term in terms:
        normalized_term = term.casefold()
        term_matched = False

        for column, label, _ in SEARCH_FIELDS:
            value = row[column]
            if value is None or normalized_term not in str(value).casefold():
                continue

            term_matched = True
            if label not in matched_fields:
                matched_fields.append(label)

        if term_matched:
            matched_terms.append(term)

    return matched_terms, matched_fields