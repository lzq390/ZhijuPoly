import type { KnowledgeSearchGroup } from "../types";

export const MAX_KNOWLEDGE_SEARCH_TERMS = 10;

const TEXT_AND_OPERATOR = /(^|\s)AND(?=\s|$)/gi;
const TEXT_OR_OPERATOR = /(^|\s)OR(?=\s|$)/gi;

export type KnowledgeSearchExpressionResult = {
  groups: KnowledgeSearchGroup[];
  error: string | null;
};

function normalizeTerms(terms: string[]): string[] {
  const normalized: string[] = [];
  const seen = new Set<string>();

  terms.forEach((term) => {
    const value = term.trim();
    if (!value) return;
    const key = value.toLocaleLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    normalized.push(value);
  });

  return normalized;
}

export function normalizeKnowledgeSearchGroups(groups: KnowledgeSearchGroup[]): KnowledgeSearchGroup[] {
  const normalized: KnowledgeSearchGroup[] = [];
  const seen = new Set<string>();

  groups.forEach((group) => {
    const terms = normalizeTerms(group.terms);
    if (terms.length === 0) return;
    const signature = JSON.stringify(terms.map((term) => term.toLocaleLowerCase()).sort());
    if (seen.has(signature)) return;
    seen.add(signature);
    normalized.push({ terms });
  });

  return normalized;
}

export function knowledgeSearchGroupsFromTerms(terms: string[]): KnowledgeSearchGroup[] {
  return normalizeTerms(terms).map((term) => ({ terms: [term] }));
}

export function serializeKnowledgeSearchGroups(groups: KnowledgeSearchGroup[]): string {
  return normalizeKnowledgeSearchGroups(groups)
    .map((group) => group.terms.join(" | "))
    .join("；");
}

export function parseKnowledgeSearchExpression(query: string): KnowledgeSearchExpressionResult {
  const value = query.trim();
  if (!value) return { groups: [], error: null };

  const symbolized = value
    .replace(TEXT_AND_OPERATOR, "$1;")
    .replace(TEXT_OR_OPERATOR, "$1|")
    .replaceAll("；", ";")
    .replaceAll("｜", "|");

  const rawGroups = symbolized.split(";");
  if (rawGroups.some((group) => !group.trim())) {
    return { groups: [], error: "逻辑符号前后必须有完整关键词" };
  }

  const groups: KnowledgeSearchGroup[] = [];
  for (const rawGroup of rawGroups) {
    const rawTerms = rawGroup.split("|");
    if (rawTerms.some((term) => !term.trim())) {
      return { groups: [], error: "逻辑符号前后必须有完整关键词" };
    }
    groups.push({ terms: normalizeTerms(rawTerms) });
  }

  const termCount = groups.reduce((total, group) => total + group.terms.length, 0);
  if (termCount > MAX_KNOWLEDGE_SEARCH_TERMS) {
    return { groups: [], error: `最多支持 ${MAX_KNOWLEDGE_SEARCH_TERMS} 个关键词或同义词` };
  }

  return { groups: normalizeKnowledgeSearchGroups(groups), error: null };
}
