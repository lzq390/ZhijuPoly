import { useCallback, useEffect, useRef, useState } from "react";
import {
  clearOnlineKnowledgeHistory,
  createOnlineKnowledgeJob,
  deleteOnlineKnowledgeHistory,
  fetchOnlineKnowledgeJob,
  fetchOnlineKnowledgeHistory,
} from "../services/api";
import type {
  OnlineKnowledgeHistoryItem,
  OnlineKnowledgeJobResponse,
  OnlineKnowledgeJobStatus,
  OnlineKnowledgeSearchRequest,
  OnlineKnowledgeSearchResponse
} from "../types";
import { isAbortError, pollJobWithBackoff } from "./jobPolling";

type OnlineKnowledgeSearchState = {
  isLoading: boolean;
  isHistoryLoading: boolean;
  error: string | null;
  historyError: string | null;
  data: OnlineKnowledgeSearchResponse | null;
  history: OnlineKnowledgeHistoryItem[];
  jobId: string | null;
  jobStatus: OnlineKnowledgeJobStatus | null;
  job: OnlineKnowledgeJobResponse | null;
};

const JOB_POLL_INTERVAL_MS = 1200;
const MIN_JOB_POLL_TIMEOUT_MS = 15 * 60 * 1000;
const BASE_TIMEOUT_PAPER_COUNT = 100;
const EXTRA_TIMEOUT_PER_100_PAPERS_MS = 5 * 60 * 1000;
const TERMINAL_STATUSES = new Set<OnlineKnowledgeJobStatus>(["completed", "failed"]);

export function useOnlineKnowledgeSearch() {
  const [state, setState] = useState<OnlineKnowledgeSearchState>({
    isLoading: false,
    isHistoryLoading: false,
    error: null,
    historyError: null,
    data: null,
    history: [],
    jobId: null,
    jobStatus: null,
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

  const loadHistory = useCallback(async () => {
    setState((current) => ({ ...current, isHistoryLoading: true, historyError: null }));
    try {
      const response = await fetchOnlineKnowledgeHistory();
      setState((current) => ({
        ...current,
        isHistoryLoading: false,
        history: response.history
      }));
    } catch (error) {
      setState((current) => ({
        ...current,
        isHistoryLoading: false,
        historyError: error instanceof Error ? error.message : "Unknown error"
      }));
    }
  }, []);

  async function submit(payload: OnlineKnowledgeSearchRequest) {
    pollAbortRef.current?.abort();
    const token = pollTokenRef.current + 1;
    pollTokenRef.current = token;
    const controller = new AbortController();
    pollAbortRef.current = controller;
    setState((current) => ({
      ...current,
      isLoading: true,
      error: null,
      data: null,
      jobId: null,
      jobStatus: null,
      job: null
    }));

    try {
      const created = await createOnlineKnowledgeJob(payload, controller.signal);
      if (pollTokenRef.current !== token || controller.signal.aborted) {
        return;
      }
      setState((current) => ({
        ...current,
        jobId: created.job_id,
        jobStatus: created.status,
        job: null
      }));
      await pollJob(created.job_id, payload.max_papers, token, controller);
    } catch (error) {
      if (pollTokenRef.current !== token || controller.signal.aborted || isAbortError(error)) {
        return;
      }
      setState((current) => ({
        ...current,
        isLoading: false,
        error: error instanceof Error ? error.message : "Unknown error",
        data: null
      }));
    } finally {
      if (pollAbortRef.current === controller) {
        pollAbortRef.current = null;
      }
    }
  }

  async function pollJob(jobId: string, maxPapers: number, token: number, controller: AbortController) {
    const timeoutMs = getJobPollTimeoutMs(maxPapers);
    let completed = false;

    const outcome = await pollJobWithBackoff({
      signal: controller.signal,
      fetchJob: (signal) => fetchOnlineKnowledgeJob(jobId, signal),
      isTerminal: (job) => TERMINAL_STATUSES.has(job.status),
      intervalMs: JOB_POLL_INTERVAL_MS,
      initialDelayMs: JOB_POLL_INTERVAL_MS,
      timeoutMs,
      onExpired: () => {
        if (pollTokenRef.current === token && !controller.signal.aborted) {
          setState((current) => ({
            ...current,
            isLoading: false,
            error: "Online retrieval job was not found or has expired. Please submit it again.",
            data: null
          }));
        }
      },
      onTimeout: () => {
        if (pollTokenRef.current === token && !controller.signal.aborted) {
          setState((current) => ({
            ...current,
            isLoading: false,
            error: `Online retrieval is still running after about ${Math.round(timeoutMs / 60000)} minutes. Refresh the history later or start a smaller search.`,
            data: null
          }));
        }
      },
      onJob: (job) => {
        if (pollTokenRef.current !== token || controller.signal.aborted) {
          return;
        }
        setState((current) => ({
          ...current,
          jobStatus: job.status,
          job,
          isLoading: !TERMINAL_STATUSES.has(job.status),
          error: job.status === "failed" ? job.error_message || "Online retrieval failed" : null,
          data: job.status === "completed" ? job.result : current.data
        }));
        if (job.status === "completed") {
          completed = true;
        }
      }
    });

    if (
      outcome === "terminal" &&
      completed &&
      pollTokenRef.current === token &&
      !controller.signal.aborted
    ) {
      await loadHistory();
    }
  }

  function restoreFromHistory(item: OnlineKnowledgeHistoryItem) {
    setState((current) => ({
      ...current,
      data: item.result_data,
      error: null,
      jobId: null,
      jobStatus: null,
      job: null
    }));
  }

  async function deleteHistoryItem(historyId: number) {
    setState((current) => ({ ...current, historyError: null }));
    try {
      await deleteOnlineKnowledgeHistory(historyId);
      setState((current) => ({
        ...current,
        history: current.history.filter((item) => item.history_id !== historyId)
      }));
    } catch (error) {
      setState((current) => ({
        ...current,
        historyError: error instanceof Error ? error.message : "Failed to delete history"
      }));
      throw error;
    }
  }

  async function clearHistory() {
    setState((current) => ({ ...current, historyError: null }));
    try {
      await clearOnlineKnowledgeHistory();
      setState((current) => ({ ...current, history: [] }));
    } catch (error) {
      setState((current) => ({
        ...current,
        historyError: error instanceof Error ? error.message : "Failed to clear history"
      }));
      throw error;
    }
  }

  return {
    ...state,
    submit,
    loadHistory,
    restoreFromHistory,
    deleteHistoryItem,
    clearHistory
  };
}

function getJobPollTimeoutMs(maxPapers: number) {
  const normalizedMaxPapers = Number.isFinite(maxPapers) ? Math.max(1, Math.floor(maxPapers)) : 1;
  const extraPapers = Math.max(0, normalizedMaxPapers - BASE_TIMEOUT_PAPER_COUNT);
  const extraBlocks = Math.ceil(extraPapers / 100);
  return MIN_JOB_POLL_TIMEOUT_MS + extraBlocks * EXTRA_TIMEOUT_PER_100_PAPERS_MS;
}
