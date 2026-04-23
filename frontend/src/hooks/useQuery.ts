import { useState } from "react";
import { querySmiles } from "../services/api";
import type { SmilesQueryRequest, SmilesQueryResponse } from "../types";

type QueryState = {
  isLoading: boolean;
  error: string | null;
  data: SmilesQueryResponse | null;
};

const DEFAULT_REQUEST: SmilesQueryRequest = {
  smiles: "",
  match_mode: "structure",
  similarity_threshold: 0.7,
  top_k: 10
};

export function useQuery() {
  const [request, setRequest] = useState<SmilesQueryRequest>(DEFAULT_REQUEST);
  const [state, setState] = useState<QueryState>({
    isLoading: false,
    error: null,
    data: null
  });

  async function submit(nextRequest?: SmilesQueryRequest) {
    const activeRequest = nextRequest ?? request;

    setState((current) => ({
      ...current,
      isLoading: true,
      error: null
    }));

    try {
      const data = await querySmiles(activeRequest);
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
    request,
    setRequest,
    ...state,
    submit
  };
}
