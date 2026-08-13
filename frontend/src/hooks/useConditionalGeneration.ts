import { useEffect, useRef, useState } from "react";
import {
  createConditionalGenerationTgJob,
  fetchConditionalGenerationTgJob
} from "../services/api";
import type {
  ConditionalGenerationJobStatus,
  ConditionalGenerationJobStatusResponse,
  ConditionalGenerationTgRequest,
  ConditionalGenerationTgResponse
} from "../types";
import { isAbortError, pollJobWithBackoff } from "./jobPolling";

type ConditionalGenerationState = {
  isLoading: boolean;
  error: string | null;
  data: ConditionalGenerationTgResponse | null;
  job: ConditionalGenerationJobStatusResponse | null;
};

const POLL_INTERVAL_MS = 1000;
const TERMINAL_STATUSES = new Set<ConditionalGenerationJobStatus>(["completed", "failed", "cancelled"]);
const EXPIRED_JOB_MESSAGE = "Backend restarted or the job expired. Please resubmit.";

const DEFAULT_REQUEST: ConditionalGenerationTgRequest = {
  smiles: "",
  delta_tg: 30,
  candidate_count: 10,
  top_k: 5,
  temperature: 1.0
};

export function useConditionalGeneration() {
  const [request, setRequest] = useState<ConditionalGenerationTgRequest>(DEFAULT_REQUEST);
  const [submittedRequest, setSubmittedRequest] = useState<ConditionalGenerationTgRequest | null>(null);
  const [state, setState] = useState<ConditionalGenerationState>({
    isLoading: false,
    error: null,
    data: null,
    job: null
  });
  const pollTokenRef = useRef(0);
  const pollAbortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => {
      pollTokenRef.current += 1;
      pollAbortRef.current?.abort();
      pollAbortRef.current = null;
    };
  }, []);

  async function pollJob(jobId: string, token: number, controller: AbortController) {
    await pollJobWithBackoff({
      signal: controller.signal,
      fetchJob: (signal) => fetchConditionalGenerationTgJob(jobId, signal),
      isTerminal: (job) => TERMINAL_STATUSES.has(job.status),
      intervalMs: POLL_INTERVAL_MS,
      onExpired: () => {
        if (pollTokenRef.current === token && !controller.signal.aborted) {
          setState((current) => ({
            ...current,
            isLoading: false,
            error: EXPIRED_JOB_MESSAGE
          }));
        }
      },
      onJob: (job) => {
        if (pollTokenRef.current !== token || controller.signal.aborted) {
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
      }
    });
  }

  async function submit(nextRequest?: ConditionalGenerationTgRequest) {
    const activeRequest = nextRequest ?? request;
    const normalizedSmiles = activeRequest.smiles.trim();
    if (!normalizedSmiles) {
      setState((current) => ({
        ...current,
        isLoading: false,
        error: "请先在画布中绘制或输入聚合物结构。",
        data: null,
        job: null
      }));
      return;
    }

    const requestForGeneration: ConditionalGenerationTgRequest = {
      ...activeRequest,
      smiles: normalizedSmiles
    };
    setRequest(requestForGeneration);
    setSubmittedRequest(requestForGeneration);
    pollAbortRef.current?.abort();
    const token = pollTokenRef.current + 1;
    pollTokenRef.current = token;
    const controller = new AbortController();
    pollAbortRef.current = controller;
    setState({
      isLoading: true,
      error: null,
      data: null,
      job: null
    });

    try {
      const createdJob = await createConditionalGenerationTgJob(requestForGeneration, controller.signal);
      if (pollTokenRef.current !== token || controller.signal.aborted) {
        return;
      }
      setState((current) => ({
        ...current,
        job: {
          job_id: createdJob.job_id,
          status: createdJob.status,
          delta_tg: requestForGeneration.delta_tg,
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
      await pollJob(createdJob.job_id, token, controller);
    } catch (error) {
      if (pollTokenRef.current !== token || controller.signal.aborted || isAbortError(error)) {
        return;
      }
      setState({
        isLoading: false,
        error: error instanceof Error ? error.message : "Unknown error",
        data: null,
        job: null
      });
    } finally {
      if (pollAbortRef.current === controller) {
        pollAbortRef.current = null;
      }
    }
  }

  function reset() {
    pollTokenRef.current += 1;
    pollAbortRef.current?.abort();
    pollAbortRef.current = null;
    setSubmittedRequest(null);
    setState({
      isLoading: false,
      error: null,
      data: null,
      job: null
    });
  }

  return {
    request,
    setRequest,
    submittedRequest,
    ...state,
    submit,
    reset
  };
}
