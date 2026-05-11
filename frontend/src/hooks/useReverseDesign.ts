import { useState } from "react";
import { fetchReverseDesignKnowledge, searchReverseDesignByTg } from "../services/api";
import type {
  ReverseDesignKnowledgeResponse,
  ReverseDesignTgCandidate,
  ReverseDesignTgRequest,
  ReverseDesignTgResponse
} from "../types";

type ReverseDesignState = {
  isLoading: boolean;
  error: string | null;
  data: ReverseDesignTgResponse | null;
  selectedCandidate: ReverseDesignTgCandidate | null;
  knowledgeLoading: boolean;
  knowledgeError: string | null;
  knowledgeData: ReverseDesignKnowledgeResponse | null;
};

const DEFAULT_REQUEST: ReverseDesignTgRequest = {
  target_tg: null,
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
    data: null,
    selectedCandidate: null,
    knowledgeLoading: false,
    knowledgeError: null,
    knowledgeData: null
  });

  async function submit(nextRequest?: ReverseDesignTgRequest) {
    const activeRequest = nextRequest ?? request;
    if (activeRequest.target_tg === null || Number.isNaN(activeRequest.target_tg)) {
      setState((current) => ({
        ...current,
        isLoading: false,
        error: "Target Tg is required.",
        data: null,
        selectedCandidate: null,
        knowledgeData: null
      }));
      return;
    }

    setState((current) => ({
      ...current,
      isLoading: true,
      error: null,
      data: null,
      selectedCandidate: null,
      knowledgeError: null,
      knowledgeData: null
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

  async function loadKnowledge(candidate: ReverseDesignTgCandidate) {
    setState((current) => ({
      ...current,
      selectedCandidate: candidate,
      knowledgeLoading: true,
      knowledgeError: null,
      knowledgeData: null
    }));

    try {
      const knowledgeData = await fetchReverseDesignKnowledge({
        pi_id: candidate.pi_id,
        top_k: 10
      });
      setState((current) => ({
        ...current,
        knowledgeLoading: false,
        knowledgeError: null,
        knowledgeData
      }));
    } catch (error) {
      setState((current) => ({
        ...current,
        knowledgeLoading: false,
        knowledgeError: error instanceof Error ? error.message : "Unknown error",
        knowledgeData: null
      }));
    }
  }

  return {
    request,
    setRequest,
    ...state,
    submit,
    loadKnowledge
  };
}
