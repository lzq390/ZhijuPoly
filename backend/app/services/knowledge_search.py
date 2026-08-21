from __future__ import annotations

import re
from typing import Any


TEXT_AND_OPERATOR = re.compile(r"(^|\s)AND(?=\s|$)", re.IGNORECASE)
TEXT_OR_OPERATOR = re.compile(r"(^|\s)OR(?=\s|$)", re.IGNORECASE)
# Spaces and commas are part of a user-entered search term. Only structural
# punctuation may produce implicit IUPAC candidates; logical alternatives must
# otherwise be written explicitly with `|` / `OR`.
IUPAC_TOKEN_SEPARATOR = re.compile(r"[;:/()\[\]{}]+")
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
MAX_RAW_TERMS = 10

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


class KnowledgeSearchExpressionError(ValueError):
    pass


def _replace_text_operator(value: str, pattern: re.Pattern[str], symbol: str) -> str:
    return pattern.sub(lambda match: f"{match.group(1)}{symbol}", value)


def _normalize_group_terms(terms: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for term in terms:
        value = term.strip()
        if not value:
            raise KnowledgeSearchExpressionError("logic operators must have terms on both sides")

        key = value.casefold()
        if key in seen:
            continue

        seen.add(key)
        normalized.append(value)

    if not normalized:
        raise KnowledgeSearchExpressionError("knowledge search groups must contain at least one term")
    return normalized


def _deduplicate_search_groups(groups: list[list[str]]) -> list[list[str]]:
    normalized: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for group in groups:
        signature = tuple(sorted(term.casefold() for term in group))
        if signature in seen:
            continue
        seen.add(signature)
        normalized.append(group)
    return normalized


def parse_search_groups(query: str) -> list[list[str]]:
    value = query.strip()
    if not value:
        raise KnowledgeSearchExpressionError("knowledge search query must not be empty")

    symbolized = _replace_text_operator(value, TEXT_AND_OPERATOR, ";")
    symbolized = _replace_text_operator(symbolized, TEXT_OR_OPERATOR, "|")
    symbolized = symbolized.replace("；", ";").replace("｜", "|")

    groups: list[list[str]] = []
    for raw_group in symbolized.split(";"):
        if not raw_group.strip():
            raise KnowledgeSearchExpressionError("logic operators must have terms on both sides")
        groups.append(_normalize_group_terms(raw_group.split("|")))

    if sum(len(group) for group in groups) > MAX_RAW_TERMS:
        raise KnowledgeSearchExpressionError(f"knowledge search supports at most {MAX_RAW_TERMS} terms")
    return _deduplicate_search_groups(groups)


def _iter_iupac_fragments(term: str) -> list[str]:
    fragments: list[str] = []
    for token in IUPAC_TOKEN_SEPARATOR.split(term):
        value = token.strip().strip("()[]{}\"'.,;:-")
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


def normalize_search_groups(
    query: str,
    groups: list[list[str]] | None = None,
    terms: list[str] | None = None,
) -> tuple[list[list[str]], list[list[str]]]:
    if groups and terms:
        raise KnowledgeSearchExpressionError("groups and terms cannot be provided together")

    if groups:
        normalized_groups = [_normalize_group_terms(group) for group in groups]
    elif terms:
        normalized_groups = [_normalize_group_terms([term]) for term in terms]
    else:
        normalized_groups = parse_search_groups(query)

    if sum(len(group) for group in normalized_groups) > MAX_RAW_TERMS:
        raise KnowledgeSearchExpressionError(f"knowledge search supports at most {MAX_RAW_TERMS} terms")
    raw_groups = _deduplicate_search_groups(normalized_groups)

    expanded_groups: list[list[str]] = []
    expanded_count = 0
    for group in raw_groups:
        expanded: list[str] = []
        seen: set[str] = set()
        for term in group:
            for candidate in [term, *_iter_iupac_fragments(term)]:
                key = candidate.casefold()
                if key in seen:
                    continue

                seen.add(key)
                expanded.append(candidate)
                expanded_count += 1
                if expanded_count > MAX_EXPANDED_TERMS:
                    raise KnowledgeSearchExpressionError(
                        f"knowledge search expands to more than {MAX_EXPANDED_TERMS} terms"
                    )
        expanded_groups.append(expanded)

    return raw_groups, expanded_groups


def flatten_search_groups(groups: list[list[str]]) -> list[str]:
    flattened: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for term in group:
            key = term.casefold()
            if key in seen:
                continue
            seen.add(key)
            flattened.append(term)
    return flattened


def normalize_search_terms(query: str, terms: list[str] | None = None) -> list[str]:
    _, expanded_groups = normalize_search_groups(query, terms=terms)
    return flatten_search_groups(expanded_groups)


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
