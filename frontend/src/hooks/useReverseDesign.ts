import { useState } from "react";
import { searchReverseDesignByTg } from "../services/api";
import type {
  ReverseDesignTgRequest,
  ReverseDesignTgResponse
} from "../types";

type ReverseDesignState = {
  isLoading: boolean;
  error: string | null;
  data: ReverseDesignTgResponse | null;
};

const DEFAULT_REQUEST: ReverseDesignTgRequest = {
  target_tg: 120,
  smiles: "",
  similarity_threshold: 0.7,
  candidate_sample_size: 200,
  top_k: 50,
  random_seed: null
};

export function useReverseDesign() {
  const [request, setRequest] = useState<ReverseDesignTgRequest>(DEFAULT_REQUEST);
  const [state, setState] = useState<ReverseDesignState>({
    isLoading: false,
    error: null,
    data: null
  });

  async function submit(nextRequest?: ReverseDesignTgRequest) {
    const activeRequest = nextRequest ?? request;
    if (activeRequest.target_tg === null || Number.isNaN(activeRequest.target_tg)) {
      setState((current) => ({
        ...current,
        isLoading: false,
        error: "Target Tg is required.",
        data: null
      }));
      return;
    }

    setState((current) => ({
      ...current,
      isLoading: true,
      error: null,
      data: null
    }));

    try {
      const data = await searchReverseDesignByTg(activeRequest);
      setState((current) => ({
        ...current,
        isLoading: false,
        error: null,
        data
      }));
    } catch (error) {
      setState((current) => ({
        ...current,
        isLoading: false,
        error: error instanceof Error ? error.message : "Unknown error",
        data: null
      }));
    }
  }

  return {
    request,
    setRequest,
    ...state,
    submit
  };
}
