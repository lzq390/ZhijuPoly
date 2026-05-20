import { useEffect, useRef, useState } from "react";
import { createConditionalGenerationTgJob, fetchConditionalGenerationTgJob } from "../services/api";
import type {
  ConditionalGenerationJobStatus,
  ConditionalGenerationJobStatusResponse,
  ConditionalGenerationTgRequest,
  ConditionalGenerationTgResponse
} from "../types";

type ConditionalGenerationState = {
  isLoading: boolean;
  error: string | null;
  data: ConditionalGenerationTgResponse | null;
  job: ConditionalGenerationJobStatusResponse | null;
};

const POLL_INTERVAL_MS = 1000;
const TERMINAL_STATUSES = new Set<ConditionalGenerationJobStatus>(["completed", "failed", "cancelled"]);

const DEFAULT_REQUEST: ConditionalGenerationTgRequest = {
  smiles: "",
  delta_tg: 30,
  candidate_count: 10,
  top_k: 5,
  temperature: 1.0
};

export function useConditionalGeneration() {
  const [request, setRequest] = useState<ConditionalGenerationTgRequest>(DEFAULT_REQUEST);
  const [state, setState] = useState<ConditionalGenerationState>({
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
      const job = await fetchConditionalGenerationTgJob(jobId);
      if (pollTokenRef.current !== token) {
        return;
      }

      const isTerminal = TERMINAL_STATUSES.has(job.status);
      setState((current) => ({
        ...current,
        isLoading: !isTerminal,
        error: job.status === "failed" ? job.error ?? "Conditional generation failed." : null,
        data: job.result ?? current.data,
        job
      }));

      if (isTerminal) {
        return;
      }

      await new Promise((resolve) => window.setTimeout(resolve, POLL_INTERVAL_MS));
    }
  }

  async function submit(nextRequest?: ConditionalGenerationTgRequest) {
    const activeRequest = nextRequest ?? request;
    const token = pollTokenRef.current + 1;
    pollTokenRef.current = token;
    setState({
      isLoading: true,
      error: null,
      data: null,
      job: null
    });

    try {
      const createdJob = await createConditionalGenerationTgJob(activeRequest);
      setState((current) => ({
        ...current,
        job: {
          job_id: createdJob.job_id,
          status: createdJob.status,
          delta_tg: activeRequest.delta_tg,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          started_at: null,
          finished_at: null,
          attempts: 0,
          accepted_count: 0,
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
      setState({
        isLoading: false,
        error: error instanceof Error ? error.message : "Unknown error",
        data: null,
        job: null
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
