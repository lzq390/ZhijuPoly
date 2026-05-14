import { useState } from "react";
import { searchKnowledge } from "../services/api";
import type { KnowledgeSearchResponse } from "../types";

type KnowledgeSearchState = {
  isLoading: boolean;
  error: string | null;
  data: KnowledgeSearchResponse | null;
};

export function useKnowledgeSearch() {
  const [state, setState] = useState<KnowledgeSearchState>({
    isLoading: false,
    error: null,
    data: null
  });

  async function submit(query: string, topK: number, terms?: string[], page = 1, pageSize?: number) {
    setState((current) => ({
      ...current,
      isLoading: true,
      error: null
    }));

    try {
      const cleanedTerms = terms
        ?.map((term) => term.trim())
        .filter(
          (term, index, values) =>
            term.length > 0 &&
            values.findIndex((value) => value.toLocaleLowerCase() === term.toLocaleLowerCase()) === index
        );
      const data = await searchKnowledge({
        query,
        top_k: topK,
        page,
        ...(pageSize ? { page_size: pageSize } : {}),
        ...(cleanedTerms?.length ? { terms: cleanedTerms } : {})
      });
      setState({
        isLoading: false,
        error: null,
        data
      });
    } catch (error) {
      setState({
        isLoading: false,
        error: error instanceof Error ? error.message : "Unknown error",
        data: null
      });
    }
  }

  return {
    ...state,
    submit
  };
}
