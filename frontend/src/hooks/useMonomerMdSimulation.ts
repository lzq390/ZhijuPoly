import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelMonomerMdJob,
  createMonomerMdJob,
  deleteMonomerMdArtifacts,
  deleteMonomerMdJob,
  fetchMonomerMdJob,
  fetchMonomerMdJobs,
  fetchMonomerMdProtocols,
  fetchMonomerMdStatus
} from "../services/api";
import type {
  MonomerMdJobCreateRequest,
  MonomerMdJobListQuery,
  MonomerMdJobPageResponse,
  MonomerMdJobResponse,
  MonomerMdJobStatus,
  MonomerMdProtocolCatalogResponse,
  MonomerMdServiceStatusResponse,
  MonomerMdSimulationResult
} from "../types";
import { isAbortError } from "./jobPolling";
import { useTaskEvents } from "./useTaskEvents";
import {
  MonomerMdStatusLoader,
  monomerMdStatusLoadError
} from "./monomerMdStatusLoader";

type MonomerMdJobLoadErrorKind = "not_found" | "request" | null;

type MonomerMdSimulationState = {
  isSubmitting: boolean;
  isJobLoading: boolean;
  error: string | null;
  jobLoadErrorKind: MonomerMdJobLoadErrorKind;
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
  deletingJobIds: string[];
  deleteJobErrors: Record<string, string>;
};

export type UseMonomerMdSimulationOptions = {
  enabled?: boolean;
  initialJobId?: string | null;
  onJobIdChange?: (jobId: string | null) => void;
  taskCenterActive?: boolean;
};

const HISTORY_PAGE_SIZE = 10;
const TERMINAL_STATUSES = new Set<MonomerMdJobStatus>([
  "completed",
  "failed",
  "cancelled"
]);
const DEFAULT_HISTORY_QUERY: MonomerMdJobListQuery = {
  run_mode: "formal",
  page: 1,
  page_size: HISTORY_PAGE_SIZE,
  protocol: "",
  status: ""
};

export function getMonomerMdSmilesValidationError(smiles: string): string | null {
  const normalized = smiles.trim();
  if (!normalized) return "请输入单体 SMILES。";
  if (normalized.length > 1000) return "单体 SMILES 最多 1000 个字符。";
  if (normalized.includes("*")) {
    return "单体 MD 只接受普通单分子 SMILES，请去掉 * 重复单元标记。";
  }
  return null;
}

export function getMonomerMdJobResult(
  job: MonomerMdJobResponse | null
): MonomerMdSimulationResult | null {
  if (!job) return null;
  if (job.result) return job.result;
  const summary = job.result_summary;
  if (summary || job.artifacts) {
    return {
      density_series: job.density_series,
      temperature_series: job.temperature_series,
      energy_series: job.energy_series,
      trajectory_preview: job.trajectory_preview ?? null,
      summary: summary ?? {},
      artifacts: job.artifacts ?? []
    };
  }
  return null;
}

function jobErrorMessage(job: MonomerMdJobResponse) {
  if (job.status === "cancelled") return job.message ?? "单体 MD 模拟已取消。";
  return job.error ?? job.message ?? "单体 MD 模拟失败。";
}

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim() ? error.message : fallback;
}

function isNotFoundError(error: unknown): boolean {
  const message = errorText(error, "").toLowerCase();
  return /(^|\s)404(\s|$)|not found|不存在|已到期/.test(message);
}

function isTerminal(job: MonomerMdJobResponse | null): boolean {
  return job != null && TERMINAL_STATUSES.has(job.status);
}

export function useMonomerMdSimulation(
  options: UseMonomerMdSimulationOptions = {}
) {
  const { initialJobId = null, taskCenterActive = false, enabled = true } = options;
  const [historyQuery, setHistoryQuery] = useState<MonomerMdJobListQuery>(
    DEFAULT_HISTORY_QUERY
  );
  const [state, setState] = useState<MonomerMdSimulationState>({
    isSubmitting: false,
    isJobLoading: false,
    error: null,
    jobLoadErrorKind: null,
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
    cancellingJobIds: [],
    deletingJobIds: [],
    deleteJobErrors: {}
  });

  const eventHandler = useRef<(change: { ids: string[]; resync: boolean }) => Promise<unknown>>(async () => {});
  const { connectionState: eventConnectionState, reconnect } =
    useTaskEvents("md", enabled, change => eventHandler.current(change));

  const mountedRef = useRef(true);
  const deferredEventRefresh = useRef(false);
  const pollRevisionRef = useRef(0);
  const pollAbortRef = useRef<AbortController | null>(null);
  const submitRevisionRef = useRef(0);
  const submitAbortRef = useRef<AbortController | null>(null);
  const selectedJobIdRef = useRef<string | null>(null);
  const activeRevisionRef = useRef(0);
  const activeAbortRef = useRef<AbortController | null>(null);
  const historyRevisionRef = useRef(0);
  const historyAbortRef = useRef<AbortController | null>(null);
  const historyQueryRef = useRef(historyQuery);
  const actionControllersRef = useRef(new Map<string, AbortController>());
  const actionRevisionsRef = useRef(new Map<string, number>());
  const artifactRevisionRef = useRef(0);
  const artifactAbortRef = useRef<AbortController | null>(null);
  const onJobIdChangeRef = useRef(options.onJobIdChange);
  historyQueryRef.current = historyQuery;
  onJobIdChangeRef.current = options.onJobIdChange;

  const statusLoaderRef = useRef<MonomerMdStatusLoader | null>(null);
  if (statusLoaderRef.current === null) {
    statusLoaderRef.current = new MonomerMdStatusLoader(
      fetchMonomerMdStatus,
      fetchMonomerMdProtocols
    );
  }

  const refreshStatus = useCallback(async () => {
    if (!mountedRef.current) return;
    setState((current) => ({
      ...current,
      isStatusLoading: true,
      statusError: null,
      protocolsError: null
    }));
    const result = await statusLoaderRef.current?.load();
    if (!result || !mountedRef.current) return;
    setState((current) => ({
      ...current,
      serviceStatus:
        result.status.status === "fulfilled"
          ? result.status.value
          : current.serviceStatus,
      protocolCatalog:
        result.protocols.status === "fulfilled"
          ? result.protocols.value
          : current.protocolCatalog,
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
    const revision = activeRevisionRef.current + 1;
    activeRevisionRef.current = revision;
    activeAbortRef.current?.abort();
    const controller = new AbortController();
    activeAbortRef.current = controller;
    setState((current) => ({
      ...current,
      isActiveJobsLoading: true,
      activeJobsError: null
    }));
    try {
      const page = await fetchMonomerMdJobs(
        { run_mode: "formal", active_only: true, include_result: false, page: 1, page_size: 20 },
        controller.signal
      );
      if (!mountedRef.current || controller.signal.aborted || activeRevisionRef.current !== revision) return;
      setState((current) => ({
        ...current,
        activeJobs: page.items,
        isActiveJobsLoading: false
      }));
    } catch (error) {
      if (!mountedRef.current || controller.signal.aborted || isAbortError(error)) return;
      setState((current) => ({
        ...current,
        isActiveJobsLoading: false,
        activeJobsError: errorText(error, "读取正式任务队列失败。")
      }));
    } finally {
      if (activeAbortRef.current === controller) activeAbortRef.current = null;
    }
  }, []);

  const refreshHistory = useCallback(async (query?: MonomerMdJobListQuery) => {
    const effectiveQuery = {
      ...(query ?? historyQueryRef.current),
      page_size: HISTORY_PAGE_SIZE
    };
    const revision = historyRevisionRef.current + 1;
    historyRevisionRef.current = revision;
    historyAbortRef.current?.abort();
    const controller = new AbortController();
    historyAbortRef.current = controller;
    setState((current) => ({ ...current, isHistoryLoading: true, historyError: null }));
    try {
      let page = await fetchMonomerMdJobs(
        { ...effectiveQuery, run_mode: "formal", active_only: false, include_result: false },
        controller.signal
      );
      if (!mountedRef.current || controller.signal.aborted || historyRevisionRef.current !== revision) return;
      const pageSize = HISTORY_PAGE_SIZE;
      const requestedPage = effectiveQuery.page ?? 1;
      const lastPage = Math.max(1, Math.ceil(page.total / pageSize));
      if (requestedPage > lastPage) {
        const corrected = { ...effectiveQuery, page: lastPage, page_size: pageSize };
        historyQueryRef.current = corrected;
        setHistoryQuery(corrected);
        page = await fetchMonomerMdJobs(
          { ...corrected, run_mode: "formal", active_only: false, include_result: false },
          controller.signal
        );
      }
      if (!mountedRef.current || controller.signal.aborted || historyRevisionRef.current !== revision) return;
      setState((current) => ({ ...current, history: page, isHistoryLoading: false }));
    } catch (error) {
      if (!mountedRef.current || controller.signal.aborted || isAbortError(error)) return;
      setState((current) => ({
        ...current,
        isHistoryLoading: false,
        historyError: errorText(error, "读取正式任务历史失败。")
      }));
    } finally {
      if (historyAbortRef.current === controller) historyAbortRef.current = null;
    }
  }, []);

  const loadJob = useCallback(async (jobId: string) => {
    selectedJobIdRef.current = jobId;
    pollRevisionRef.current += 1;
    const revision = pollRevisionRef.current;
    pollAbortRef.current?.abort();
    const controller = new AbortController();
    pollAbortRef.current = controller;
    setState((current) => ({
      ...current,
      isJobLoading: true,
      error: null,
      jobLoadErrorKind: null,
      data: current.job?.job_id === jobId ? current.data : null,
      job: current.job?.job_id === jobId ? current.job : null
    }));
    try {
      const job = await fetchMonomerMdJob(jobId, controller.signal);
      if (!mountedRef.current || controller.signal.aborted || pollRevisionRef.current !== revision) return;
      setState((current) => ({
        ...current,
        job,
        data: getMonomerMdJobResult(job),
        isJobLoading: false,
        error:
          job.status === "failed" || job.status === "cancelled"
            ? jobErrorMessage(job)
            : null,
        jobLoadErrorKind: null
      }));
      if (pollAbortRef.current === controller) pollAbortRef.current = null;
    } catch (error) {
      if (!mountedRef.current || controller.signal.aborted || isAbortError(error) || pollRevisionRef.current !== revision) return;
      const notFound = isNotFoundError(error);
      setState((current) => ({
        ...current,
        isJobLoading: false,
        job: notFound ? null : current.job,
        data: notFound ? null : current.data,
        error: notFound ? "任务不存在或已到期。" : errorText(error, "读取单体 MD 任务失败。"),
        jobLoadErrorKind: notFound ? "not_found" : "request"
      }));
    }
  }, []);

  const clearSelectedJob = useCallback((notify = true) => {
    selectedJobIdRef.current = null;
    pollRevisionRef.current += 1;
    pollAbortRef.current?.abort();
    pollAbortRef.current = null;
    setState((current) => ({
      ...current,
      job: null,
      data: null,
      isJobLoading: false,
      error: null,
      jobLoadErrorKind: null,
      artifactDeleteError: null
    }));
    if (notify) onJobIdChangeRef.current?.(null);
  }, []);

  const selectJob = useCallback(async (jobId: string) => {
    onJobIdChangeRef.current?.(jobId);
    await loadJob(jobId);
  }, [loadJob]);

  const submit = useCallback(async (
    request: MonomerMdJobCreateRequest
  ): Promise<string | null> => {
    const revision = submitRevisionRef.current + 1;
    submitRevisionRef.current = revision;
    submitAbortRef.current?.abort();
    const controller = new AbortController();
    submitAbortRef.current = controller;
    setState((current) => ({
      ...current,
      isSubmitting: true,
      error: null,
      jobLoadErrorKind: null,
      artifactDeleteError: null
    }));
    try {
      const created = await createMonomerMdJob(request, controller.signal);
      if (!mountedRef.current || controller.signal.aborted || submitRevisionRef.current !== revision) return null;

      const placeholder: MonomerMdJobResponse = {
        job_id: created.job_id,
        status: created.status,
        protocol: request.protocol,
        run_mode: request.run_mode,
        smiles: request.run_mode === "demo" ? request.smiles : undefined,
        config_json: request.run_mode === "formal" ? request.config_json : undefined
      };
      selectedJobIdRef.current = created.job_id;
      onJobIdChangeRef.current?.(created.job_id);
      setState((current) => ({
        ...current,
        isSubmitting: false,
        isJobLoading: true,
        job: placeholder,
        data: null
      }));
      void Promise.allSettled([refreshActiveJobs(), refreshStatus()]);
      if (request.run_mode === "formal") void refreshHistory();
      void loadJob(created.job_id);
      return created.job_id;
    } catch (error) {
      if (!mountedRef.current || controller.signal.aborted || isAbortError(error) || submitRevisionRef.current !== revision) return null;
      setState((current) => ({
        ...current,
        isSubmitting: false,
        error: errorText(error, "提交单体 MD 模拟失败。")
      }));
      return null;
    } finally {
      if (submitAbortRef.current === controller) { submitAbortRef.current = null; flushDeferredEvent(); }
    }
  }, [loadJob, refreshActiveJobs, refreshHistory, refreshStatus]);

  const cancelJob = useCallback(async (target: MonomerMdJobResponse) => {
    if (TERMINAL_STATUSES.has(target.status) || target.status === "cancel_requested") return;
    const key = `cancel:${target.job_id}`;
    actionControllersRef.current.get(key)?.abort();
    const controller = new AbortController();
    actionControllersRef.current.set(key, controller);
    setState((current) => ({
      ...current,
      cancellingJobIds: current.cancellingJobIds.includes(target.job_id)
        ? current.cancellingJobIds
        : [...current.cancellingJobIds, target.job_id]
    }));
    try {
      const updated = await cancelMonomerMdJob(target.job_id, controller.signal);
      if (!mountedRef.current || controller.signal.aborted) return;
      setState((current) => ({
        ...current,
        job: current.job?.job_id === target.job_id ? updated : current.job,
        activeJobs: current.activeJobs.map((job) =>
          job.job_id === target.job_id ? updated : job
        )
      }));
      await Promise.allSettled([refreshActiveJobs(), refreshHistory(), refreshStatus()]);
      if (selectedJobIdRef.current === target.job_id) void loadJob(target.job_id);
    } catch (error) {
      if (!mountedRef.current || controller.signal.aborted || isAbortError(error)) return;
      const message = errorText(error, "取消单体 MD 任务失败。 ");
      setState((current) => ({
        ...current,
        error: current.job?.job_id === target.job_id ? message : current.error,
        activeJobsError:
          current.job?.job_id === target.job_id ? current.activeJobsError : message
      }));
    } finally {
      if (actionControllersRef.current.get(key) === controller) {
        actionControllersRef.current.delete(key);
        flushDeferredEvent();
        if (mountedRef.current) {
          setState((current) => ({
            ...current,
            cancellingJobIds: current.cancellingJobIds.filter((id) => id !== target.job_id)
          }));
        }
      }
    }
  }, [loadJob, refreshActiveJobs, refreshHistory, refreshStatus]);

  const deleteArtifacts = useCallback(async () => {
    const jobId = selectedJobIdRef.current;
    if (!jobId) return;
    artifactRevisionRef.current += 1;
    const revision = artifactRevisionRef.current;
    artifactAbortRef.current?.abort();
    const controller = new AbortController();
    artifactAbortRef.current = controller;
    setState((current) => ({ ...current, artifactDeleteError: null }));
    try {
      const job = await deleteMonomerMdArtifacts(jobId, controller.signal);
      if (
        !mountedRef.current ||
        controller.signal.aborted ||
        artifactRevisionRef.current !== revision ||
        selectedJobIdRef.current !== jobId
      ) return;
      setState((current) => ({
        ...current,
        job,
        data: getMonomerMdJobResult(job) ?? current.data,
        artifactDeleteError: null
      }));
    } catch (error) {
      if (!mountedRef.current || controller.signal.aborted || isAbortError(error)) return;
      setState((current) => ({
        ...current,
        artifactDeleteError: errorText(error, "删除输出文件失败。")
      }));
    } finally {
      if (artifactAbortRef.current === controller) { artifactAbortRef.current = null; flushDeferredEvent(); }
    }
  }, []);

  const deleteJobRecord = useCallback(async (target: MonomerMdJobResponse) => {
    if (!TERMINAL_STATUSES.has(target.status)) return;
    const jobId = target.job_id;
    const key = `delete:${jobId}`;
    const revision = (actionRevisionsRef.current.get(key) ?? 0) + 1;
    actionRevisionsRef.current.set(key, revision);
    actionControllersRef.current.get(key)?.abort();
    const controller = new AbortController();
    actionControllersRef.current.set(key, controller);
    setState((current) => ({
      ...current,
      deletingJobIds: current.deletingJobIds.includes(jobId)
        ? current.deletingJobIds
        : [...current.deletingJobIds, jobId],
      deleteJobErrors: Object.fromEntries(
        Object.entries(current.deleteJobErrors).filter(([id]) => id !== jobId)
      )
    }));
    try {
      await deleteMonomerMdJob(jobId, controller.signal);
      if (!mountedRef.current || controller.signal.aborted || actionRevisionsRef.current.get(key) !== revision) return;
      const wasSelected = selectedJobIdRef.current === jobId;
      if (wasSelected) {
        selectedJobIdRef.current = null;
        pollRevisionRef.current += 1;
        pollAbortRef.current?.abort();
        pollAbortRef.current = null;
        onJobIdChangeRef.current?.(null);
      }
      setState((current) => ({
        ...current,
        job: current.job?.job_id === jobId ? null : current.job,
        data: current.job?.job_id === jobId ? null : current.data,
        isJobLoading: current.job?.job_id === jobId ? false : current.isJobLoading,
        error: current.job?.job_id === jobId ? null : current.error,
        activeJobs: current.activeJobs.filter((job) => job.job_id !== jobId),
        history: current.history
          ? {
              ...current.history,
              total: Math.max(0, current.history.total - 1),
              items: current.history.items.filter((job) => job.job_id !== jobId)
            }
          : null
      }));
      await Promise.allSettled([refreshActiveJobs(), refreshHistory(), refreshStatus()]);
    } catch (error) {
      if (!mountedRef.current || controller.signal.aborted || isAbortError(error) || actionRevisionsRef.current.get(key) !== revision) return;
      const message = errorText(error, "删除单体 MD 任务失败。 ");
      setState((current) => ({
        ...current,
        error: current.job?.job_id === jobId ? message : current.error,
        deleteJobErrors: { ...current.deleteJobErrors, [jobId]: message }
      }));
    } finally {
      if (actionRevisionsRef.current.get(key) === revision) {
        actionControllersRef.current.delete(key);
        flushDeferredEvent();
        if (mountedRef.current) {
          setState((current) => ({
            ...current,
            deletingJobIds: current.deletingJobIds.filter((id) => id !== jobId)
          }));
        }
      }
    }
  }, [refreshActiveJobs, refreshHistory, refreshStatus]);

  const changeHistoryQuery = useCallback((patch: Partial<MonomerMdJobListQuery>) => {
    setHistoryQuery((current) => ({
      ...current,
      ...patch,
      run_mode: "formal",
      page_size: HISTORY_PAGE_SIZE
    }));
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    if (enabled) void refreshStatus();
    return () => {
      mountedRef.current = false;
      selectedJobIdRef.current = null;
      pollRevisionRef.current += 1;
      submitRevisionRef.current += 1;
      activeRevisionRef.current += 1;
      historyRevisionRef.current += 1;
      pollAbortRef.current?.abort();
      pollAbortRef.current = null;
      submitAbortRef.current?.abort();
      activeAbortRef.current?.abort();
      historyAbortRef.current?.abort();
      artifactAbortRef.current?.abort();
      statusLoaderRef.current?.cancel();
      for (const controller of actionControllersRef.current.values()) controller.abort();
    };
  }, [enabled, refreshStatus]);

  useEffect(() => {
    if (!enabled) return;
    if (initialJobId) {
      if (selectedJobIdRef.current !== initialJobId) void loadJob(initialJobId);
      return;
    }
    if (selectedJobIdRef.current) clearSelectedJob(false);
  }, [clearSelectedJob, enabled, initialJobId, loadJob]);

  useEffect(() => {
    if (!enabled || !taskCenterActive) return;
    void refreshHistory(historyQuery);
  }, [enabled, historyQuery, refreshHistory, taskCenterActive]);

  useEffect(() => {
    if (enabled && taskCenterActive) void refreshActiveJobs();
  }, [enabled, taskCenterActive, refreshActiveJobs]);

  const refreshAll = useCallback(async () => {
    const selected = selectedJobIdRef.current;
    await Promise.allSettled([
      refreshStatus(), refreshActiveJobs(), refreshHistory(),
      ...(selected ? [loadJob(selected)] : [])
    ]);
  }, [refreshStatus, refreshActiveJobs, refreshHistory, loadJob]);

  function flushDeferredEvent() {
    if (deferredEventRefresh.current && actionControllersRef.current.size === 0 && !submitAbortRef.current && !artifactAbortRef.current) {
      deferredEventRefresh.current = false;
      void eventHandler.current({ ids: [], resync: true });
    }
  }
  eventHandler.current = async ({ ids, resync }) => {
    if (actionControllersRef.current.size > 0 || submitAbortRef.current || artifactAbortRef.current) {
      deferredEventRefresh.current = true;
      return;
    }
    const selected = selectedJobIdRef.current;
    await Promise.allSettled([
      refreshActiveJobs(), refreshHistory(), refreshStatus(),
      ...(selected && (resync || ids.includes(selected)) ? [loadJob(selected)] : [])
    ]);
  };

  return {
    historyQuery,
    ...state,
    isLoading: state.isSubmitting || state.isJobLoading,
    submit,
    eventConnectionState,
    refreshAll: async () => { if (eventConnectionState === "unavailable") reconnect(); await refreshAll(); },
    refreshStatus,
    refreshActiveJobs,
    refreshHistory,
    loadJob,
    selectJob,
    clearSelectedJob,
    cancelJob,
    changeHistoryQuery,
    deleteArtifacts,
    deleteJobRecord
  };
}
