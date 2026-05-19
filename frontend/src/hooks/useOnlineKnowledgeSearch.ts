import { useCallback, useState } from "react";
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
      const created = await createOnlineKnowledgeJob(payload);
      setState((current) => ({
        ...current,
        jobId: created.job_id,
        jobStatus: created.status,
        job: null
      }));
      await pollJob(created.job_id, payload.max_papers);
    } catch (error) {
      setState((current) => ({
        ...current,
        isLoading: false,
        error: error instanceof Error ? error.message : "Unknown error",
        data: null
      }));
    }
  }

  async function pollJob(jobId: string, maxPapers: number) {
    const startedAt = Date.now();
    const timeoutMs = getJobPollTimeoutMs(maxPapers);

    while (Date.now() - startedAt < timeoutMs) {
      await delay(JOB_POLL_INTERVAL_MS);
      const job = await fetchOnlineKnowledgeJob(jobId);
      setState((current) => ({
        ...current,
        jobStatus: job.status,
        job
      }));

      if (job.status === "completed") {
        setState((current) => ({
          ...current,
          isLoading: false,
          error: null,
          data: job.result
        }));
        await loadHistory();
        return;
      }

      if (job.status === "failed") {
        setState((current) => ({
          ...current,
          isLoading: false,
          error: job.error_message || "Online retrieval failed",
          data: null
        }));
        return;
      }
    }

    setState((current) => ({
      ...current,
      isLoading: false,
      error: `Online retrieval is still running after about ${Math.round(timeoutMs / 60000)} minutes. Refresh the history later or start a smaller search.`,
      data: null
    }));
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
    await deleteOnlineKnowledgeHistory(historyId);
    setState((current) => ({
      ...current,
      history: current.history.filter((item) => item.history_id !== historyId)
    }));
  }

  async function clearHistory() {
    await clearOnlineKnowledgeHistory();
    setState((current) => ({ ...current, history: [] }));
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

function delay(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function getJobPollTimeoutMs(maxPapers: number) {
  const normalizedMaxPapers = Number.isFinite(maxPapers) ? Math.max(1, Math.floor(maxPapers)) : 1;
  const extraPapers = Math.max(0, normalizedMaxPapers - BASE_TIMEOUT_PAPER_COUNT);
  const extraBlocks = Math.ceil(extraPapers / 100);
  return MIN_JOB_POLL_TIMEOUT_MS + extraBlocks * EXTRA_TIMEOUT_PER_100_PAPERS_MS;
}
