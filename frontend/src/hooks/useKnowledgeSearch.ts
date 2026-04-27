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

  async function submit(query: string, topK: number) {
    setState((current) => ({
      ...current,
      isLoading: true,
      error: null
    }));

    try {
      const data = await searchKnowledge({ query, top_k: topK });
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
