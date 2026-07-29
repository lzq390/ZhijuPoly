import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelMonomerMdJob,
  createMonomerMdJob,
  deleteMonomerMdArtifacts,
  fetchMonomerMdJob,
  fetchMonomerMdJobs,
  fetchMonomerMdProtocols,
  fetchMonomerMdStatus
} from "../services/api";
import type {
  MonomerMdJobListQuery,
  MonomerMdJobPageResponse,
  MonomerMdJobResponse,
  MonomerMdJobStatus,
  MonomerMdProtocol,
  MonomerMdProtocolCatalogResponse,
  MonomerMdRunMode,
  MonomerMdServiceStatusResponse,
  MonomerMdSimulationResult
} from "../types";
import {
  MonomerMdStatusLoader,
  monomerMdStatusLoadError
} from "./monomerMdStatusLoader";
import { isAbortError, pollJobWithBackoff } from "./jobPolling";

type MonomerMdSimulationState = {
  isLoading: boolean;
  isSubmitting: boolean;
  isJobLoading: boolean;
  error: string | null;
  data: MonomerMdSimulationResult | null;
  job: MonomerMdJobResponse | null;
  serviceStatus: MonomerMdServiceStatusResponse | null;
  protocolCatalog: MonomerMdProtocolCatalogResponse | null;
  isStatusLoading: boolean;
  statusError: string | null;
  protocolsError: string | null;
  artifactDeleteError: string | null;
  activeJobs: MonomerMdJobResponse[];
  isActiveJobsLoading: boolean;
  activeJobsError: string | null;
  history: MonomerMdJobPageResponse | null;
  isHistoryLoading: boolean;
  historyError: string | null;
  cancellingJobIds: string[];
};

const POLL_INTERVAL_MS = 1400;
const FORMAL_LIST_POLL_INTERVAL_MS = 5000;
const TERMINAL_STATUSES = new Set<MonomerMdJobStatus>(["completed", "failed", "cancelled"]);
const DEFAULT_HISTORY_QUERY: MonomerMdJobListQuery = {
  run_mode: "formal",
  page: 1,
  page_size: 20,
  protocol: "",
  status: ""
};

export function getMonomerMdSmilesValidationError(smiles: string): string | null {
  const normalizedSmiles = smiles.trim();
  if (!normalizedSmiles) {
    return "请输入单体 SMILES。";
  }
  if (normalizedSmiles.includes("*")) {
    return "单体 MD 只接受普通单分子 SMILES，请去掉 * 重复单元标记。";
  }
  return null;
}

export function getMonomerMdJobResult(job: MonomerMdJobResponse | null): MonomerMdSimulationResult | null {
  if (!job) {
    return null;
  }
  if (job.result) {
    return job.result;
  }
  if (job.density_series && job.temperature_series && job.energy_series && job.summary) {
    return {
      density_series: job.density_series,
      temperature_series: job.temperature_series,
      energy_series: job.energy_series,
      trajectory_preview: job.trajectory_preview ?? null,
      summary: job.summary,
      artifacts: job.artifacts ?? []
    };
  }
  return null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function validateFormalConfig(configText: string, selectedProtocol: MonomerMdProtocol): Record<string, unknown> | string {
  let config: unknown;
  try {
    config = JSON.parse(configText);
  } catch {
    return "ByteFF2 config JSON 格式无效。";
  }
  if (!isRecord(config)) {
    return "ByteFF2 config JSON 必须是对象。";
  }
  if (config.protocol !== selectedProtocol) {
    return "ByteFF2 config JSON 的 protocol 必须与当前选择的模块一致。";
  }
  if (!isRecord(config.components) || Object.keys(config.components).length === 0) {
    return "ByteFF2 config JSON 必须包含非空 components 对象。";
  }
  if (!isRecord(config.smiles) || Object.keys(config.smiles).length === 0) {
    return "ByteFF2 config JSON 必须包含非空 smiles 对象。";
  }
  return config;
}

function jobErrorMessage(job: MonomerMdJobResponse) {
  if (job.status === "cancelled") {
    return job.message ?? "单体 MD 模拟已取消。";
  }
  return job.error ?? job.message ?? "单体 MD 模拟失败。";
}

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export function useMonomerMdSimulation() {
  const [smiles, setSmiles] = useState("");
  const [runMode, setRunMode] = useState<MonomerMdRunMode>("demo");
  const [selectedProtocol, setSelectedProtocol] = useState<MonomerMdProtocol>("Density");
  const [configText, setConfigText] = useState("");
  const [historyQuery, setHistoryQuery] = useState<MonomerMdJobListQuery>(DEFAULT_HISTORY_QUERY);
  const [state, setState] = useState<MonomerMdSimulationState>({
    isLoading: false,
    isSubmitting: false,
    isJobLoading: false,
    error: null,
    data: null,
    job: null,
    serviceStatus: null,
    protocolCatalog: null,
    isStatusLoading: false,
    statusError: null,
    protocolsError: null,
    artifactDeleteError: null,
    activeJobs: [],
    isActiveJobsLoading: false,
    activeJobsError: null,
    history: null,
    isHistoryLoading: false,
    historyError: null,
    cancellingJobIds: []
  });
  const pollTokenRef = useRef(0);
  const pollAbortRef = useRef<AbortController | null>(null);
  const submitRequestRef = useRef(0);
  const activeRequestRef = useRef(0);
  const historyRequestRef = useRef(0);
  const historyQueryRef = useRef(historyQuery);
  const selectedJobIdRef = useRef<string | null>(state.job?.job_id ?? null);
  historyQueryRef.current = historyQuery;
  selectedJobIdRef.current = state.job?.job_id ?? null;
  const statusLoaderRef = useRef<MonomerMdStatusLoader | null>(null);
  if (statusLoaderRef.current === null) {
    statusLoaderRef.current = new MonomerMdStatusLoader(fetchMonomerMdStatus, fetchMonomerMdProtocols);
  }

  const refreshStatus = useCallback(async () => {
    setState((current) => ({ ...current, isStatusLoading: true, statusError: null, protocolsError: null }));
    const result = await statusLoaderRef.current?.load();
    if (!result) return;
    setState((current) => ({
      ...current,
      serviceStatus: result.status.status === "fulfilled" ? result.status.value : current.serviceStatus,
      protocolCatalog: result.protocols.status === "fulfilled" ? result.protocols.value : current.protocolCatalog,
      isStatusLoading: false,
      statusError: monomerMdStatusLoadError(
        result.status,
        result.timedOut,
        "检查单体 MD 服务状态失败。",
        "检查单体 MD 服务状态超时（10 秒）。"
      ),
      protocolsError: monomerMdStatusLoadError(
        result.protocols,
        result.timedOut,
        "检查 ByteFF2 协议列表失败。",
        "检查 ByteFF2 协议列表超时（10 秒）。"
      )
    }));
  }, []);

  const refreshActiveJobs = useCallback(async () => {
    const requestId = activeRequestRef.current + 1;
    activeRequestRef.current = requestId;
    setState((current) => ({ ...current, isActiveJobsLoading: true, activeJobsError: null }));
    try {
      const page = await fetchMonomerMdJobs({
        run_mode: "formal",
        active_only: true,
        page: 1,
        page_size: 3
      });
      if (activeRequestRef.current !== requestId) return;
      setState((current) => ({
        ...current,
        activeJobs: page.items,
        isActiveJobsLoading: false,
        activeJobsError: null
      }));
    } catch (error) {
      if (activeRequestRef.current !== requestId) return;
      setState((current) => ({
        ...current,
        isActiveJobsLoading: false,
        activeJobsError: errorText(error, "读取正式任务队列失败。")
      }));
    }
  }, []);

  const refreshHistory = useCallback(async (query?: MonomerMdJobListQuery) => {
    const effectiveQuery = query ?? historyQueryRef.current;
    const requestId = historyRequestRef.current + 1;
    historyRequestRef.current = requestId;
    setState((current) => ({ ...current, isHistoryLoading: true, historyError: null }));
    try {
      const page = await fetchMonomerMdJobs({ ...effectiveQuery, run_mode: "formal", active_only: false });
      if (historyRequestRef.current !== requestId) return;
      setState((current) => ({
        ...current,
        history: page,
        isHistoryLoading: false,
        historyError: null
      }));
    } catch (error) {
      if (historyRequestRef.current !== requestId) return;
      setState((current) => ({
        ...current,
        isHistoryLoading: false,
        historyError: errorText(error, "读取正式任务历史失败。")
      }));
    }
  }, []);

  useEffect(() => {
    if (configText.trim()) return;
    const protocolInfo = state.protocolCatalog?.protocols.find((item) => item.protocol === selectedProtocol);
    if (protocolInfo?.default_config) {
      setConfigText(JSON.stringify(protocolInfo.default_config, null, 2));
    }
  }, [configText, selectedProtocol, state.protocolCatalog]);

  function loadProtocolTemplate(protocol = selectedProtocol) {
    const protocolInfo = state.protocolCatalog?.protocols.find((item) => item.protocol === protocol);
    if (protocolInfo?.default_config) {
      setConfigText(JSON.stringify(protocolInfo.default_config, null, 2));
    }
  }

  useEffect(() => {
    void refreshStatus();
    return () => {
      pollTokenRef.current += 1;
      pollAbortRef.current?.abort();
      statusLoaderRef.current?.cancel();
      activeRequestRef.current += 1;
      historyRequestRef.current += 1;
    };
  }, [refreshStatus]);

  useEffect(() => {
    const shouldPollStatus =
      state.serviceStatus?.busy === true ||
      state.serviceStatus?.draining === true ||
      (state.serviceStatus?.database_active_jobs ?? 0) > 0;
    if (!shouldPollStatus) return;
    let cancelled = false;
    let timer: number | null = null;
    const scheduleRefresh = () => {
      timer = window.setTimeout(() => {
        void refreshStatus().finally(() => {
          if (!cancelled) scheduleRefresh();
        });
      }, FORMAL_LIST_POLL_INTERVAL_MS);
    };
    scheduleRefresh();
    return () => {
      cancelled = true;
      if (timer != null) window.clearTimeout(timer);
    };
  }, [
    refreshStatus,
    state.serviceStatus?.busy,
    state.serviceStatus?.database_active_jobs,
    state.serviceStatus?.draining
  ]);

  useEffect(() => {
    if (runMode !== "formal") return;
    void refreshActiveJobs();
    void refreshHistory(historyQuery);
    const timer = window.setInterval(() => {
      void refreshActiveJobs();
      void refreshHistory(historyQuery);
      void refreshStatus();
    }, FORMAL_LIST_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [historyQuery, refreshActiveJobs, refreshHistory, refreshStatus, runMode]);

  const pollJob = useCallback(async (jobId: string, token: number, controller: AbortController) => {
    try {
      await pollJobWithBackoff({
        signal: controller.signal,
        fetchJob: (signal) => fetchMonomerMdJob(jobId, signal),
        isTerminal: (job) => TERMINAL_STATUSES.has(job.status),
        intervalMs: POLL_INTERVAL_MS,
        onExpired: () => {
          if (pollTokenRef.current === token && !controller.signal.aborted) {
            setState((current) => ({
              ...current,
              isLoading: false,
              isJobLoading: false,
              error: "单体 MD 任务不存在或已过期，请重新提交。"
            }));
          }
        },
        onJob: (job) => {
          if (pollTokenRef.current !== token || controller.signal.aborted) return;
          const result = getMonomerMdJobResult(job);
          const isTerminal = TERMINAL_STATUSES.has(job.status);
          setState((current) => ({
            ...current,
            isLoading: job.run_mode === "demo" && !isTerminal,
            isJobLoading: !isTerminal,
            error: job.status === "failed" || job.status === "cancelled" ? jobErrorMessage(job) : null,
            data: result ?? (isTerminal ? null : current.data),
            job
          }));
          if (isTerminal && job.run_mode === "formal") {
            void refreshActiveJobs();
            void refreshHistory();
            void refreshStatus();
          }
        }
      });
    } finally {
      if (pollAbortRef.current === controller) {
        pollAbortRef.current = null;
      }
    }
  }, [refreshActiveJobs, refreshHistory, refreshStatus]);

  const loadJob = useCallback(async (
    jobId: string,
    pollingContext?: { token: number; controller: AbortController }
  ) => {
    let token: number;
    let controller: AbortController;
    if (pollingContext) {
      ({ token, controller } = pollingContext);
    } else {
      pollAbortRef.current?.abort();
      token = pollTokenRef.current + 1;
      pollTokenRef.current = token;
      controller = new AbortController();
      pollAbortRef.current = controller;
    }
    setState((current) => ({ ...current, isJobLoading: true, error: null, data: null }));
    try {
      const job = await fetchMonomerMdJob(jobId, controller.signal);
      if (pollTokenRef.current !== token || controller.signal.aborted) return;
      const terminal = TERMINAL_STATUSES.has(job.status);
      setState((current) => ({
        ...current,
        job,
        data: getMonomerMdJobResult(job),
        isLoading: job.run_mode === "demo" && !terminal,
        isJobLoading: !terminal,
        error: job.status === "failed" || job.status === "cancelled" ? jobErrorMessage(job) : null
      }));
      if (!terminal) {
        void pollJob(jobId, token, controller);
      } else if (pollAbortRef.current === controller) {
        pollAbortRef.current = null;
      }
    } catch (error) {
      if (pollTokenRef.current !== token || controller.signal.aborted || isAbortError(error)) return;
      setState((current) => ({
        ...current,
        isLoading: false,
        isJobLoading: false,
        error: errorText(error, "读取单体 MD 任务失败。")
      }));
    }
  }, [pollJob]);

  async function submit(nextSmiles = smiles) {
    const submitRequestId = submitRequestRef.current + 1;
    submitRequestRef.current = submitRequestId;
    const normalizedSmiles = nextSmiles.trim();
    const validationError = runMode === "demo" ? getMonomerMdSmilesValidationError(normalizedSmiles) : null;
    if (validationError) {
      setState((current) => ({ ...current, isLoading: false, isSubmitting: false, error: validationError }));
      return;
    }
    let payload;
    if (runMode === "formal") {
      const config = validateFormalConfig(configText, selectedProtocol);
      if (typeof config === "string") {
        setState((current) => ({ ...current, isLoading: false, isSubmitting: false, error: config }));
        return;
      }
      payload = { protocol: selectedProtocol, run_mode: "formal" as const, config_json: config };
    } else {
      payload = { smiles: normalizedSmiles };
    }
    setSmiles(normalizedSmiles);
    pollAbortRef.current?.abort();
    const token = pollTokenRef.current + 1;
    pollTokenRef.current = token;
    const controller = new AbortController();
    pollAbortRef.current = controller;
    setState((current) => ({
      ...current,
      isLoading: true,
      isSubmitting: true,
      error: null,
      artifactDeleteError: null
    }));
    try {
      const createdJob = await createMonomerMdJob(payload, controller.signal);
      if (
        submitRequestRef.current !== submitRequestId ||
        controller.signal.aborted
      ) return;
      setState((current) => ({
        ...current,
        isLoading: runMode === "demo",
        isSubmitting: false,
        job: {
          job_id: createdJob.job_id,
          status: createdJob.status,
          smiles: normalizedSmiles,
          protocol: runMode === "formal" ? selectedProtocol : "DensityDemo",
          run_mode: runMode
        },
        data: null
      }));
      void Promise.allSettled([
        refreshActiveJobs(),
        refreshHistory(),
        refreshStatus()
      ]);
      void loadJob(createdJob.job_id, { token, controller });
    } catch (error) {
      if (
        submitRequestRef.current !== submitRequestId ||
        controller.signal.aborted ||
        isAbortError(error)
      ) return;
      if (pollAbortRef.current === controller) {
        pollAbortRef.current = null;
      }
      setState((current) => ({
        ...current,
        isLoading: false,
        isSubmitting: false,
        error: errorText(error, "提交单体 MD 模拟失败。")
      }));
    }
  }

  function reset() {
    submitRequestRef.current += 1;
    pollTokenRef.current += 1;
    pollAbortRef.current?.abort();
    pollAbortRef.current = null;
    setState((current) => ({
      ...current,
      isLoading: false,
      isSubmitting: false,
      isJobLoading: false,
      error: null,
      data: null,
      job: null
    }));
  }

  async function cancelJob(job: MonomerMdJobResponse) {
    if (TERMINAL_STATUSES.has(job.status) || job.status === "cancel_requested") return;
    setState((current) => ({
      ...current,
      cancellingJobIds: current.cancellingJobIds.includes(job.job_id)
        ? current.cancellingJobIds
        : [...current.cancellingJobIds, job.job_id]
    }));
    try {
      const updated = await cancelMonomerMdJob(job.job_id);
      setState((current) => ({
        ...current,
        job: current.job?.job_id === job.job_id ? updated : current.job,
        cancellingJobIds: current.cancellingJobIds.filter((id) => id !== job.job_id)
      }));
      await Promise.allSettled([refreshActiveJobs(), refreshHistory(), refreshStatus()]);
      if (selectedJobIdRef.current === job.job_id) {
        void loadJob(job.job_id);
      }
    } catch (error) {
      setState((current) => ({
        ...current,
        error:
          current.job?.job_id === job.job_id
            ? errorText(error, "取消单体 MD 任务失败。")
            : current.error,
        activeJobsError:
          current.job?.job_id === job.job_id
            ? current.activeJobsError
            : errorText(error, "取消单体 MD 任务失败。"),
        cancellingJobIds: current.cancellingJobIds.filter((id) => id !== job.job_id)
      }));
    }
  }

  function changeHistoryQuery(patch: Partial<MonomerMdJobListQuery>) {
    setHistoryQuery((current) => ({ ...current, ...patch, run_mode: "formal", page_size: 20 }));
  }

  async function deleteArtifacts() {
    if (!state.job?.job_id) return;
    setState((current) => ({ ...current, artifactDeleteError: null }));
    try {
      const job = await deleteMonomerMdArtifacts(state.job.job_id);
      setState((current) => ({
        ...current,
        job,
        data: getMonomerMdJobResult(job) ?? current.data,
        artifactDeleteError: null
      }));
    } catch (error) {
      setState((current) => ({
        ...current,
        artifactDeleteError: errorText(error, "删除输出文件失败。")
      }));
    }
  }

  return {
    smiles,
    setSmiles,
    runMode,
    setRunMode,
    selectedProtocol,
    setSelectedProtocol,
    configText,
    setConfigText,
    historyQuery,
    ...state,
    submit,
    reset,
    refreshStatus,
    refreshActiveJobs,
    refreshHistory,
    loadJob,
    cancelJob,
    changeHistoryQuery,
    loadProtocolTemplate,
    deleteArtifacts
  };
}
