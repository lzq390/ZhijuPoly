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
import { isAbortError, pollJobWithBackoff } from "./jobPolling";

type ReverseDesignState = {
  isLoading: boolean;
  error: string | null;
  data: ReverseDesignTgResponse | null;
  job: ReverseDesignTgJobStatusResponse | null;
};

const POLL_INTERVAL_MS = 1000;
const TERMINAL_STATUSES = new Set<ReverseDesignJobStatus>(["found_enough", "exhausted", "failed", "cancelled"]);

function formatReverseDesignError(message: string) {
  const normalized = message.replace(/^\d+:\s*/, "").trim();
  if (normalized.includes("PI Postgres database is not reachable")) {
    return "PI 数据库连接不可用，请确认本地 PI 数据库已启动，或切换到已导入的本地 SQLite 候选库。";
  }
  if (normalized.includes("PI reverse-design database is not initialized")) {
    return "PI 逆向设计数据库尚未初始化，请先导入 PI 候选数据。";
  }
  if (normalized.includes("Job not found")) {
    return "Tg 搜索任务不存在或已过期。";
  }
  if (normalized.includes("Failed to fetch")) {
    return "无法连接后端服务，请确认本地后端已启动。";
  }
  return normalized || "Tg 逆向设计搜索失败。";
}

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
      fetchJob: (signal) => fetchReverseDesignTgJob(jobId, signal),
      isTerminal: (job) => TERMINAL_STATUSES.has(job.status),
      intervalMs: POLL_INTERVAL_MS,
      onExpired: () => {
        if (pollTokenRef.current === token && !controller.signal.aborted) {
          setState((current) => ({
            ...current,
            isLoading: false,
            error: formatReverseDesignError("Job not found")
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
          error: job.status === "failed" ? formatReverseDesignError(job.error ?? "Tg 逆向设计搜索失败。") : null,
          data: job.result ?? current.data,
          job
        }));
      }
    });
  }

  function reportError(message: string) {
    pollTokenRef.current += 1;
    pollAbortRef.current?.abort();
    pollAbortRef.current = null;
    setState((current) => ({
      ...current,
      isLoading: false,
      error: formatReverseDesignError(message),
      data: null,
      job: null
    }));
  }

  async function submit(nextRequest?: ReverseDesignTgRequest) {
    const activeRequest = nextRequest ?? request;
    if (activeRequest.target_tg === null || Number.isNaN(activeRequest.target_tg)) {
      setState((current) => ({
        ...current,
        isLoading: false,
        error: "请先设置目标 Tg。",
        data: null,
        job: null
      }));
      return;
    }

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

    const requestForSearch: ReverseDesignTgRequest = {
      ...activeRequest,
      smiles: normalizedSmiles
    };
    setRequest(requestForSearch);

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
      job: null
    }));

    try {
      const createdJob = await createReverseDesignTgJob(requestForSearch, controller.signal);
      if (pollTokenRef.current !== token || controller.signal.aborted) {
        return;
      }
      setState((current) => ({
        ...current,
        job: {
          job_id: createdJob.job_id,
          status: createdJob.status,
          target_tg: requestForSearch.target_tg ?? 0,
          similarity_threshold: requestForSearch.similarity_threshold,
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
      await pollJob(createdJob.job_id, token, controller);
    } catch (error) {
      if (pollTokenRef.current !== token || controller.signal.aborted || isAbortError(error)) {
        return;
      }
      setState((current) => ({
        ...current,
        isLoading: false,
        error: formatReverseDesignError(error instanceof Error ? error.message : "未知错误"),
        data: null
      }));
    } finally {
      if (pollAbortRef.current === controller) {
        pollAbortRef.current = null;
      }
    }
  }

  return {
    request,
    setRequest,
    ...state,
    submit,
    reportError
  };
}
