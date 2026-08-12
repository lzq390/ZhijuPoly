import { useCallback, useEffect, useRef, useState } from "react";
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
  const requestTokenRef = useRef(0);
  const requestAbortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => {
      requestTokenRef.current += 1;
      requestAbortRef.current?.abort();
      requestAbortRef.current = null;
    };
  }, []);

  const submit = useCallback(async (
    query: string,
    topK: number,
    terms?: string[],
    page = 1,
    pageSize?: number
  ): Promise<KnowledgeSearchResponse | null> => {
    requestAbortRef.current?.abort();
    const token = requestTokenRef.current + 1;
    requestTokenRef.current = token;
    const controller = new AbortController();
    requestAbortRef.current = controller;

    setState({
      isLoading: true,
      error: null,
      data: null
    });

    try {
      const cleanedTerms = terms
        ?.map((term) => term.trim())
        .filter(
          (term, index, values) =>
            term.length > 0 &&
            values.findIndex((value) => value.toLocaleLowerCase() === term.toLocaleLowerCase()) === index
        );
      const data = await searchKnowledge(
        {
          query,
          top_k: topK,
          page,
          ...(pageSize ? { page_size: pageSize } : {}),
          ...(cleanedTerms?.length ? { terms: cleanedTerms } : {})
        },
        controller.signal
      );
      if (requestTokenRef.current !== token || controller.signal.aborted) {
        return null;
      }
      setState({
        isLoading: false,
        error: null,
        data
      });
      return data;
    } catch (error) {
      if (
        requestTokenRef.current !== token ||
        controller.signal.aborted ||
        (error instanceof DOMException && error.name === "AbortError")
      ) {
        return null;
      }
      setState({
        isLoading: false,
        error: error instanceof Error ? error.message : "Unknown error",
        data: null
      });
      return null;
    } finally {
      if (requestAbortRef.current === controller) {
        requestAbortRef.current = null;
      }
    }
  }, []);

  return {
    ...state,
    submit
  };
}
