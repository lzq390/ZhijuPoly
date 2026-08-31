import { useCallback, useEffect, useRef, useState } from "react";
import {
  createPolytaoJob,
  fetchPolytaoJob,
  fetchPolytaoStatus,
  isApiRequestError
} from "../services/api";
import type {
  PolytaoGenerationRequest,
  PolytaoGenerationResponse,
  PolytaoJobStatus,
  PolytaoJobStatusResponse,
  PolytaoStatusResponse
} from "../types";
import { POLYTAO_DESCRIPTOR_NAMES, type PolytaoDescriptorMap } from "../types";

type PolytaoGenerationState = {
  isLoading: boolean;
  error: string | null;
  data: PolytaoGenerationResponse | null;
  job: PolytaoJobStatusResponse | null;
  serviceStatus: PolytaoStatusResponse | null;
  isStatusLoading: boolean;
  statusError: string | null;
};

const POLL_INTERVAL_MS = 1400;
const POLL_MAX_FAILURES = 3;
const STATUS_POLL_INTERVAL_MS = 5000;
const TERMINAL_STATUSES = new Set<PolytaoJobStatus>(["completed", "failed", "cancelled"]);
const EXPIRED_JOB_MESSAGE = "后端服务已重启或生成任务已过期，请重新提交。";

export type PolytaoRuntimeDisplayState =
  | "checking"
  | "disabled"
  | "cold"
  | "loading"
  | "ready"
  | "db_unavailable"
  | "runtime_error";

export function getPolytaoRuntimeDisplayState(
  status: PolytaoStatusResponse | null,
  statusError: string | null,
  isStatusLoading: boolean
): PolytaoRuntimeDisplayState {
  if (isStatusLoading && status === null) {
    return "checking";
  }
  if (statusError || status === null) {
    return "runtime_error";
  }
  if (!status.enabled) {
    return "disabled";
  }
  if (status.db_configured === false || status.db_ready === false) {
    return "db_unavailable";
  }
  if (status.runtime_ready === true) {
    return "ready";
  }
  if (status.runtime_error) {
    return "runtime_error";
  }

  const reportedStatus = status.worker_status?.trim().toLowerCase();
  if (reportedStatus === "loading" || status.message.toLowerCase().includes("runtime is loading")) {
    return "loading";
  }
  if (status.available) {
    return "cold";
  }
  return "runtime_error";
}

export function shouldAutoRefreshPolytaoStatus(
  status: PolytaoStatusResponse | null,
  statusError: string | null
): boolean {
  return Boolean(statusError) || status === null || status.available !== true || status.runtime_ready !== true;
}

function createEmptyDescriptors(): PolytaoDescriptorMap {
  return POLYTAO_DESCRIPTOR_NAMES.reduce((descriptors, name) => {
    descriptors[name] = Number.NaN;
    return descriptors;
  }, {} as PolytaoDescriptorMap);
}

export const DEFAULT_POLYTAO_DESCRIPTORS: PolytaoDescriptorMap = {
  MolWt: 264,
  HeavyAtomCount: 19,
  NHOHCount: 0,
  NOCount: 4,
  NumAliphaticCarbocycles: 1,
  NumAliphaticHeterocycles: 0,
  NumAliphaticRings: 1,
  NumAromaticCarbocycles: 0,
  NumAromaticHeterocycles: 0,
  NumAromaticRings: 0,
  NumHAcceptors: 4,
  NumHDonors: 0,
  NumHeteroatoms: 6,
  NumRotatableBonds: 5,
  RingCount: 1
};

export const EMPTY_POLYTAO_DESCRIPTORS: PolytaoDescriptorMap = createEmptyDescriptors();

export const DEFAULT_POLYTAO_REQUEST: PolytaoGenerationRequest = {
  descriptors: EMPTY_POLYTAO_DESCRIPTORS,
  input_smiles: null,
  candidate_count: 10,
  temperature: 1.0,
  top_k: 100,
  top_p: 0.999,
  max_length: 300
};

function delay(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function jobErrorMessage(job: PolytaoJobStatusResponse) {
  if (job.status === "cancelled") {
    return job.progress_message || "PolyTAO 生成任务已取消。";
  }
  return job.error_message || job.progress_message || "PolyTAO 生成任务执行失败。";
}

function cloneRequest(request: PolytaoGenerationRequest): PolytaoGenerationRequest {
  return {
    ...request,
    descriptors: { ...request.descriptors }
  };
}

export function polytaoDescriptorMapFromEntries(entries: { name: string; value: number }[]): PolytaoDescriptorMap {
  const next = { ...EMPTY_POLYTAO_DESCRIPTORS };
  for (const entry of entries) {
    if (POLYTAO_DESCRIPTOR_NAMES.includes(entry.name as (typeof POLYTAO_DESCRIPTOR_NAMES)[number])) {
      next[entry.name as keyof PolytaoDescriptorMap] = entry.value;
    }
  }
  return next;
}

export function usePolytaoGeneration() {
  const [request, setRequest] = useState<PolytaoGenerationRequest>(cloneRequest(DEFAULT_POLYTAO_REQUEST));
  const [state, setState] = useState<PolytaoGenerationState>({
    isLoading: false,
    error: null,
    data: null,
    job: null,
    serviceStatus: null,
    isStatusLoading: true,
    statusError: null
  });
  const pollTokenRef = useRef(0);
  const statusTokenRef = useRef(0);

  const refreshStatus = useCallback(async () => {
    const token = statusTokenRef.current + 1;
    statusTokenRef.current = token;
    setState((current) => ({ ...current, isStatusLoading: true, statusError: null }));
    try {
      const serviceStatus = await fetchPolytaoStatus();
      if (statusTokenRef.current !== token) {
        return;
      }
      setState((current) => ({ ...current, serviceStatus, isStatusLoading: false, statusError: null }));
    } catch (error) {
      if (statusTokenRef.current !== token) {
        return;
      }
      setState((current) => ({
        ...current,
        serviceStatus: null,
        isStatusLoading: false,
        statusError: error instanceof Error ? error.message : "检查 PolyTAO 服务状态失败。"
      }));
    }
  }, []);

  useEffect(() => {
    void refreshStatus();
    return () => {
      pollTokenRef.current += 1;
      statusTokenRef.current += 1;
    };
  }, [refreshStatus]);

  useEffect(() => {
    if (
      state.isStatusLoading ||
      !shouldAutoRefreshPolytaoStatus(state.serviceStatus, state.statusError)
    ) {
      return;
    }
    const interval = window.setInterval(() => {
      void refreshStatus();
    }, STATUS_POLL_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [refreshStatus, state.isStatusLoading, state.serviceStatus, state.statusError]);

  useEffect(() => {
    const refreshOnFocus = () => {
      void refreshStatus();
    };
    window.addEventListener("focus", refreshOnFocus);
    return () => window.removeEventListener("focus", refreshOnFocus);
  }, [refreshStatus]);

  async function pollJob(jobId: string, token: number) {
    let failureCount = 0;
    while (pollTokenRef.current === token) {
      let job: PolytaoJobStatusResponse;
      try {
        job = await fetchPolytaoJob(jobId);
        failureCount = 0;
      } catch (error) {
        if (pollTokenRef.current !== token) {
          return;
        }
        if (isApiRequestError(error, 410)) {
          setState((current) => ({
            ...current,
            isLoading: false,
            error: EXPIRED_JOB_MESSAGE
          }));
          return;
        }
        failureCount += 1;
        if (failureCount >= POLL_MAX_FAILURES) {
          setState((current) => ({
            ...current,
            isLoading: false,
            error: error instanceof Error ? error.message : "刷新 PolyTAO 任务状态失败。"
          }));
          return;
        }
        await delay(POLL_INTERVAL_MS);
        continue;
      }
      if (pollTokenRef.current !== token) {
        return;
      }
      const isTerminal = TERMINAL_STATUSES.has(job.status);
      setState((current) => ({
        ...current,
        isLoading: !isTerminal,
        error: job.status === "failed" || job.status === "cancelled" ? jobErrorMessage(job) : null,
        data: job.result ?? current.data,
        job
      }));
      if (isTerminal) {
        void refreshStatus();
        return;
      }
      await delay(POLL_INTERVAL_MS);
    }
  }

  async function submit(nextRequest?: PolytaoGenerationRequest) {
    const activeRequest = cloneRequest(nextRequest ?? request);
    const token = pollTokenRef.current + 1;
    pollTokenRef.current = token;
    setRequest(activeRequest);
    setState((current) => ({ ...current, isLoading: true, error: null, data: null, job: null }));
    try {
      const createdJob = await createPolytaoJob(activeRequest);
      if (pollTokenRef.current !== token) {
        return;
      }
      setState((current) => ({
        ...current,
        job: {
          job_id: createdJob.job_id,
          status: createdJob.status,
          input_smiles: activeRequest.input_smiles ?? null,
          canonical_smiles: null,
          prompt: "",
          requested_count: activeRequest.candidate_count,
          returned_count: 0,
          attempts: 0,
          progress_percent: 0,
          progress_stage: createdJob.status,
          progress_message: "任务已提交至 PolyTAO 后端运行时。",
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          started_at: null,
          finished_at: null,
          worker_id: null,
          worker_job_id: null,
          worker_version: null,
          engine: "polytao-backend",
          error_message: null,
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
        error: error instanceof Error ? error.message : "提交 PolyTAO 生成任务失败。"
      }));
      void refreshStatus();
    }
  }

  function reset() {
    pollTokenRef.current += 1;
    setState((current) => ({ ...current, isLoading: false, error: null, data: null, job: null }));
  }

  return {
    request,
    setRequest,
    ...state,
    submit,
    reset,
    refreshStatus
  };
}
