import { useEffect, useRef, useState } from "react";
import { createReverseDesignTgJob, fetchReverseDesignTgJob } from "../services/api";
import {
  REVERSE_DESIGN_DEFAULT_CANDIDATE_SIZE,
  REVERSE_DESIGN_DEFAULT_SIMILARITY,
  REVERSE_DESIGN_DEFAULT_TARGET_TG
} from "../constants/reverseDesignDefaults";
import type {
  ReverseDesignJobStatus,
  ReverseDesignTgJobStatusResponse,
  ReverseDesignTgRequest,
  ReverseDesignTgResponse
} from "../types";

type ReverseDesignState = {
  isLoading: boolean;
  error: string | null;
  data: ReverseDesignTgResponse | null;
  job: ReverseDesignTgJobStatusResponse | null;
};

const POLL_INTERVAL_MS = 1000;
const TERMINAL_STATUSES = new Set<ReverseDesignJobStatus>(["found_enough", "exhausted", "failed", "cancelled"]);

const DEFAULT_REQUEST: ReverseDesignTgRequest = {
  target_tg: REVERSE_DESIGN_DEFAULT_TARGET_TG,
  smiles: "",
  similarity_threshold: REVERSE_DESIGN_DEFAULT_SIMILARITY,
  candidate_size: REVERSE_DESIGN_DEFAULT_CANDIDATE_SIZE
};

export function useReverseDesign() {
  const [request, setRequest] = useState<ReverseDesignTgRequest>(DEFAULT_REQUEST);
  const [state, setState] = useState<ReverseDesignState>({
    isLoading: false,
    error: null,
    data: null,
    job: null
  });
  const pollTokenRef = useRef(0);

  useEffect(() => {
    return () => {
      pollTokenRef.current += 1;
    };
  }, []);

  async function pollJob(jobId: string, token: number) {
    while (pollTokenRef.current === token) {
      const job = await fetchReverseDesignTgJob(jobId);
      if (pollTokenRef.current !== token) {
        return;
      }

      const isTerminal = TERMINAL_STATUSES.has(job.status);
      setState((current) => ({
        ...current,
        isLoading: !isTerminal,
        error: job.status === "failed" ? job.error ?? "Reverse design search failed." : null,
        data: job.result ?? current.data,
        job
      }));

      if (isTerminal) {
        return;
      }

      await new Promise((resolve) => window.setTimeout(resolve, POLL_INTERVAL_MS));
    }
  }

  async function submit(nextRequest?: ReverseDesignTgRequest) {
    const activeRequest = nextRequest ?? request;
    if (activeRequest.target_tg === null || Number.isNaN(activeRequest.target_tg)) {
      setState((current) => ({
        ...current,
        isLoading: false,
        error: "Target Tg is required.",
        data: null,
        job: null
      }));
      return;
    }

    const token = pollTokenRef.current + 1;
    pollTokenRef.current = token;
    setState((current) => ({
      ...current,
      isLoading: true,
      error: null,
      data: null,
      job: null
    }));

    try {
      const createdJob = await createReverseDesignTgJob(activeRequest);
      setState((current) => ({
        ...current,
        job: {
          job_id: createdJob.job_id,
          status: createdJob.status,
          target_tg: activeRequest.target_tg ?? 0,
          similarity_threshold: activeRequest.similarity_threshold,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          started_at: null,
          finished_at: null,
          scanned_rows: 0,
          matched_count: 0,
          current_tg_radius: null,
          best_similarity_score: null,
          message: null,
          error: null,
          result: null
        }
      }));
      await pollJob(createdJob.job_id, token);
    } catch (error) {
      if (pollTokenRef.current !== token) {
        return;
      }
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
