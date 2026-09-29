import { useTaskEvents } from "./useTaskEvents";
import { useCallback, useEffect, useRef, useState } from "react";
import { userFacingMonomerDftMessage } from "../lib/monomerDftPresentation";
import {
  cancelMonomerDftJob,
  createMonomerDftJob,
  deleteMonomerDftArtifactsAndReloadJob,
  deleteMonomerDftJob,
  fetchMonomerDftCapabilities,
  fetchMonomerDftJob,
  fetchMonomerDftJobs,
  fetchMonomerDftStatus,
  MonomerDftApiError
} from "../services/api";
import type {
  MonomerDftCalculationType,
  MonomerDftCapabilitiesResponse,
  MonomerDftJobCreateRequest,
  MonomerDftJobListQuery,
  MonomerDftJobListResponse,
  MonomerDftJobResponse,
  MonomerDftJobStatus,
  MonomerDftModelCapability,
  MonomerDftProperty,
  MonomerDftServiceStatusResponse
} from "../types";

export const MONOMER_DFT_HISTORY_PAGE_SIZE = 10;
export const MONOMER_DFT_JOB_BACKOFF_MS = [1_500, 3_000, 6_000, 10_000] as const;

export type MonomerDftPollState = "idle" | "polling" | "watching" | "degraded" | "terminal" | "stopped";

const TERMINAL_STATUSES = new Set<MonomerDftJobStatus>(["completed", "failed", "cancelled"]);

export type MonomerDftValidationIssue = {
  field: "smiles" | "charge" | "multiplicity" | "model" | "calculation" | "properties" | "optimization" | "conformer";
  message: string;
};

export type MonomerDftValidationInput = {
  smiles: string;
  netCharge: number | null;
  multiplicity: number;
  psmilesMode: "close" | "cap" | null;
  calculationType: MonomerDftCalculationType;
  modelId: string;
  properties: MonomerDftProperty[];
  fmax?: number;
  maxSteps?: number;
  seed?: number;
  maxIterations?: number;
};

const AROMATIC_ELEMENT: Record<string, string> = {
  as: "As",
  b: "B",
  c: "C",
  n: "N",
  o: "O",
  p: "P",
  s: "S",
  se: "Se"
};

const SMILES_ATOM_TOKEN = /^(Cl|Br|Si|Se|Na|Li|Mg|Ca|Al|Zn|Fe|Cu|Pd|Pt|As|se|as|[A-Z][a-z]?|[bcnops])/;

const PROPERTY_LABELS: Record<MonomerDftProperty, string> = {
  energy: "能量",
  forces: "原子力",
  charges: "原子电荷",
  hessian: "二阶力常数",
  frequencies: "振动频率"
};

export function extractElementsFromSmiles(smiles: string): string[] {
  const elements: string[] = [];
  for (let index = 0; index < smiles.length;) {
    if (smiles[index] === "[") {
      const closingIndex = smiles.indexOf("]", index + 1);
      const bracketContent = smiles.slice(index + 1, closingIndex < 0 ? smiles.length : closingIndex);
      const token = bracketContent.replace(/^\d+/, "").match(SMILES_ATOM_TOKEN)?.[0];
      if (token) elements.push(AROMATIC_ELEMENT[token] ?? token);
      index = closingIndex < 0 ? smiles.length : closingIndex + 1;
      continue;
    }
    const token = smiles.slice(index).match(SMILES_ATOM_TOKEN)?.[0];
    if (token) {
      elements.push(AROMATIC_ELEMENT[token] ?? token);
      index += token.length;
    } else {
      index += 1;
    }
  }
  return [...new Set(elements)];
}

export function isMonomerDftTerminal(status: MonomerDftJobStatus): boolean {
  return TERMINAL_STATUSES.has(status);
}

export function validateMonomerDftRequest(
  input: MonomerDftValidationInput,
  capabilities: MonomerDftCapabilitiesResponse | null
): MonomerDftValidationIssue[] {
  const issues: MonomerDftValidationIssue[] = [];
  const smiles = input.smiles.trim();
  if (!smiles) {
    issues.push({ field: "smiles", message: "请先在共享结构工作台设置单体结构。" });
  } else {
    if (/\s/.test(smiles)) {
      issues.push({ field: "smiles", message: "SMILES / PSMILES 不能包含空白字符。" });
    }
    if (smiles.includes("*") && input.psmilesMode == null) {
      issues.push({ field: "smiles", message: "检测到 PSMILES 连接位点，请选择连接两端或补全两端。" });
    }
    if (!smiles.includes("*") && input.psmilesMode != null) {
      issues.push({ field: "smiles", message: "普通分子不需要处理连接位点。" });
    }
    if (input.psmilesMode === "close" && [...smiles].filter((character) => character === "*").length !== 2) {
      issues.push({ field: "smiles", message: "连接两端需要结构中恰好有两个 * 连接位点。" });
    }
    if (smiles.includes(".")) {
      issues.push({ field: "smiles", message: "一次任务只接受一个连通分子，不能包含以 . 分隔的多组分。" });
    }
  }
  if (input.netCharge != null && !Number.isInteger(input.netCharge)) {
    issues.push({ field: "charge", message: "总电荷必须是整数。" });
  } else if (input.netCharge != null && (input.netCharge < -5 || input.netCharge > 5)) {
    issues.push({ field: "charge", message: "总电荷必须在 -5–5 范围内。" });
  }
  if (!Number.isInteger(input.multiplicity) || input.multiplicity < 1 || input.multiplicity > 7) {
    issues.push({ field: "multiplicity", message: "自旋多重度必须是 1–7 的整数（2S+1）。" });
  }
  if (input.calculationType === "optimization") {
    const minOptimizationSteps = capabilities?.limits.min_optimization_steps ?? 10;
    const maxOptimizationSteps = capabilities?.limits.max_optimization_steps ?? 50;
    if (input.fmax == null || !Number.isFinite(input.fmax) || input.fmax < 0.001 || input.fmax > 1) {
      issues.push({ field: "optimization", message: "收敛力阈值必须在 0.001–1.0 eV/Å 范围内。" });
    }
    if (
      input.maxSteps == null ||
      !Number.isInteger(input.maxSteps) ||
      input.maxSteps < minOptimizationSteps ||
      input.maxSteps > maxOptimizationSteps
    ) {
      issues.push({
        field: "optimization",
        message: `最大优化步数必须是 ${minOptimizationSteps}–${maxOptimizationSteps} 的整数。`
      });
    }
  }
  if (input.seed != null && (!Number.isInteger(input.seed) || input.seed < 0 || input.seed > 2_147_483_647)) {
    issues.push({ field: "conformer", message: "随机种子必须是 0–2147483647 的整数。" });
  }
  if (input.maxIterations != null && (!Number.isInteger(input.maxIterations) || input.maxIterations < 1 || input.maxIterations > 5000)) {
    issues.push({ field: "conformer", message: "初始构型最大优化步数必须是 1–5000 的整数。" });
  }
  if (!capabilities) {
    issues.push({ field: "model", message: "正在载入可用的计算方案。" });
    return issues;
  }
  const model = capabilities.models.find((item) => item.id === input.modelId);
  if (!model) {
    issues.push({ field: "model", message: "请选择适合当前分子的计算用途。" });
    return issues;
  }
  if (!model.available) {
    issues.push({ field: "model", message: "当前计算方案暂不可用，请选择其他用途。" });
  }
  if (!model.supported_calculation_types.includes(input.calculationType)) {
    issues.push({ field: "calculation", message: "当前计算方案不支持所选计算类型。" });
  }
  if (!capabilities.calculation_types.includes(input.calculationType)) {
    issues.push({ field: "calculation", message: "当前服务未开放该计算类型。" });
  }
  if (input.multiplicity !== 1 && !model.supports_spin) {
    issues.push({ field: "multiplicity", message: "当前计算方案不支持多重态，请使用单重态或选择其他用途。" });
  }
  const chargeWithinGlobalContract = input.netCharge == null || (
    Number.isInteger(input.netCharge) && input.netCharge >= -5 && input.netCharge <= 5
  );
  if (chargeWithinGlobalContract && model.charge_min != null && input.netCharge != null && input.netCharge < model.charge_min) {
    issues.push({ field: "charge", message: `当前计算方案支持的最小总电荷为 ${model.charge_min}。` });
  }
  if (chargeWithinGlobalContract && model.charge_max != null && input.netCharge != null && input.netCharge > model.charge_max) {
    issues.push({ field: "charge", message: `当前计算方案支持的最大总电荷为 ${model.charge_max}。` });
  }
  const unsupportedElements = extractElementsFromSmiles(smiles).filter(
    (element) => model.supported_elements.length > 0 && !model.supported_elements.includes(element)
  );
  if (unsupportedElements.length > 0) {
    issues.push({
      field: "smiles",
      message: `当前计算方案不支持这些元素：${unsupportedElements.join("、")}。`
    });
  }
  if (input.calculationType === "single_point") {
    if (!input.properties.includes("energy")) {
      issues.push({ field: "properties", message: "单点计算必须包含能量。" });
    }
  }
  const unsupportedProperties = input.properties.filter(
    (property) => (
      !capabilities.properties.includes(property) ||
      !model.supported_properties.includes(property)
    )
  );
  if (unsupportedProperties.length > 0) {
    issues.push({
      field: "properties",
      message: `当前计算方案不支持：${unsupportedProperties.map((property) => PROPERTY_LABELS[property]).join("、")}。`
    });
  }
  return issues;
}

function errorMessage(error: unknown, fallback: string): string {
  if (!(error instanceof Error) || !error.message.trim()) return fallback;
  return userFacingMonomerDftMessage(error.message, {
    code: error instanceof MonomerDftApiError ? error.code : null,
    fallback
  });
}

function makeRequestId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `dft-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

export function isRetryableMonomerDftPollError(error: unknown): boolean {
  if (error instanceof MonomerDftApiError) {
    return error.status === 429 || (error.status >= 400 && error.retryable);
  }
  return error instanceof TypeError;
}

export function monomerDftPollRetryDelayMs(error: unknown, failureCount: number): number {
  if (error instanceof MonomerDftApiError && error.retryAfterSeconds != null) {
    return Math.min(60_000, Math.max(1_000, error.retryAfterSeconds * 1_000));
  }
  const index = Math.min(
    MONOMER_DFT_JOB_BACKOFF_MS.length - 1,
    Math.max(0, Math.trunc(failureCount) - 1)
  );
  return MONOMER_DFT_JOB_BACKOFF_MS[index];
}

function artifactDeletionIsPending(job: MonomerDftJobResponse): boolean {
  return job.artifacts_state === "delete_requested";
}

function pollingIsComplete(job: MonomerDftJobResponse): boolean {
  return isMonomerDftTerminal(job.status) && !artifactDeletionIsPending(job);
}

type UseMonomerDftJobOptions = {
  enabled?: boolean;
  initialJobId?: string | null;
  onJobIdChange?: (jobId: string | null) => void;
};

type JobReadSession = {
  jobId: string;
  selectionEpoch: number;
  controller: AbortController;
};

export function useMonomerDftJob({ initialJobId = null, onJobIdChange, enabled = true }: UseMonomerDftJobOptions = {}) {
  const [serviceStatus, setServiceStatus] = useState<MonomerDftServiceStatusResponse | null>(null);
  const [capabilities, setCapabilities] = useState<MonomerDftCapabilitiesResponse | null>(null);
  const [job, setJob] = useState<MonomerDftJobResponse | null>(null);
  const [history, setHistory] = useState<MonomerDftJobListResponse | null>(null);
  const [historyQuery, setHistoryQuery] = useState<MonomerDftJobListQuery>({
    page: 1,
    page_size: MONOMER_DFT_HISTORY_PAGE_SIZE,
    status: "",
    calculation_type: ""
  });
  const [isServiceLoading, setIsServiceLoading] = useState(enabled);
  const [isHistoryLoading, setIsHistoryLoading] = useState(enabled);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [pollState, setPollState] = useState<MonomerDftPollState>("idle");
  const [cancellingJobId, setCancellingJobId] = useState<string | null>(null);
  const [deletingArtifactsJobId, setDeletingArtifactsJobId] = useState<string | null>(null);
  const [deletingJobIds, setDeletingJobIds] = useState<string[]>([]);
  const [deleteJobErrors, setDeleteJobErrors] = useState<Record<string, string>>({});
  const [serviceError, setServiceError] = useState<string | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [jobError, setJobError] = useState<string | null>(null);
  const activeJobIdRef = useRef<string | null>(null);
  const selectionEpochRef = useRef(0);
  const operationRevisionRef = useRef(0);
  const historyQueryRef = useRef(historyQuery);
  const onJobIdChangeRef = useRef(onJobIdChange);
  const historyTokenRef = useRef(0);
  const statusTokenRef = useRef(0);
  const statusAbortRef = useRef<AbortController | null>(null);
  const historyAbortRef = useRef<AbortController | null>(null);
  const jobReadSessionRef = useRef<JobReadSession | null>(null);
  const cancelAbortRef = useRef<AbortController | null>(null);
  const deleteAbortRef = useRef<AbortController | null>(null);
  const submitAbortRef = useRef<AbortController | null>(null);
  const cancellingJobIdRef = useRef<string | null>(null);
  const deletingArtifactsJobIdRef = useRef<string | null>(null);
  const pendingSubmissionRef = useRef<{ payload: string; idempotencyKey: string } | null>(null);
  const schemaReadyRef = useRef(false);
  const deferredEventRefresh = useRef(false);
  const eventHandler = useRef<(change: { ids: string[]; resync: boolean }) => Promise<unknown>>(async () => {});
  const { connectionState: eventConnectionState, reconnect } =
    useTaskEvents("dft", enabled, change => eventHandler.current(change));

  const purgeControllersRef = useRef(new Map<string, AbortController>());
  const purgeRevisionsRef = useRef(new Map<string, number>());

  useEffect(() => {
    onJobIdChangeRef.current = onJobIdChange;
  }, [onJobIdChange]);

  useEffect(() => {
    historyQueryRef.current = historyQuery;
  }, [historyQuery]);

  const enforceSchemaGate = useCallback((): void => {
    schemaReadyRef.current = false;
    historyTokenRef.current += 1;
    historyAbortRef.current?.abort();
    historyAbortRef.current = null;
    const pollSession = jobReadSessionRef.current;
    jobReadSessionRef.current = null;
    pollSession?.controller.abort();
    selectionEpochRef.current += 1;
    activeJobIdRef.current = null;
    operationRevisionRef.current += 1;
    cancelAbortRef.current?.abort();
    deleteAbortRef.current?.abort();
    submitAbortRef.current?.abort();
    cancelAbortRef.current = null;
    deleteAbortRef.current = null;
    submitAbortRef.current = null;
    cancellingJobIdRef.current = null;
    deletingArtifactsJobIdRef.current = null;
    pendingSubmissionRef.current = null;
    for (const controller of purgeControllersRef.current.values()) controller.abort();
    purgeControllersRef.current.clear();
    purgeRevisionsRef.current.clear();
    setCapabilities(null);
    setHistory(null);
    setJob(null);
    setHistoryError(null);
    setJobError(null);
    setIsHistoryLoading(false);
    setIsSubmitting(false);
    setCancellingJobId(null);
    setDeletingArtifactsJobId(null);
    setDeletingJobIds([]);
    setDeleteJobErrors({});
    setPollState("idle");
    onJobIdChangeRef.current?.(null);
  }, []);

  const refreshStatus = useCallback(async (includeCapabilities = false): Promise<void> => {
    const token = statusTokenRef.current + 1;
    statusTokenRef.current = token;
    statusAbortRef.current?.abort();
    const controller = new AbortController();
    statusAbortRef.current = controller;
    setIsServiceLoading(true);
    try {
      if (includeCapabilities) {
        const [statusResult, capabilitiesResult] = await Promise.allSettled([
          fetchMonomerDftStatus(controller.signal),
          fetchMonomerDftCapabilities(controller.signal)
        ]);
        if (statusTokenRef.current !== token || controller.signal.aborted) return;
        if (statusResult.status === "fulfilled") {
          setServiceStatus(statusResult.value);
        }
        if (capabilitiesResult.status === "fulfilled") {
          setCapabilities(capabilitiesResult.value);
        }
        const observedNotReady = (
          statusResult.status === "fulfilled" &&
          statusResult.value.schema_ready !== true
        ) || (
          capabilitiesResult.status === "fulfilled" &&
          capabilitiesResult.value.schema_ready !== true
        );
        if (observedNotReady) {
          enforceSchemaGate();
        }
        if (statusResult.status === "rejected") throw statusResult.reason;
        if (capabilitiesResult.status === "rejected") throw capabilitiesResult.reason;
        schemaReadyRef.current = (
          statusResult.value.schema_ready === true &&
          capabilitiesResult.value.schema_ready === true
        );
      } else {
        const nextStatus = await fetchMonomerDftStatus(controller.signal);
        if (statusTokenRef.current !== token || controller.signal.aborted) return;
        schemaReadyRef.current = nextStatus.schema_ready === true;
        setServiceStatus(nextStatus);
        if (!schemaReadyRef.current) enforceSchemaGate();
      }
      setServiceError(null);
    } catch (error) {
      if (statusTokenRef.current !== token || controller.signal.aborted || isAbortError(error)) return;
      setServiceError(errorMessage(error, "读取单体 DFT 服务状态失败。"));
    } finally {
      if (statusTokenRef.current === token && !controller.signal.aborted) {
        statusAbortRef.current = null;
        setIsServiceLoading(false);
      }
    }
  }, [enforceSchemaGate]);

  const refreshHistory = useCallback(async (query = historyQueryRef.current): Promise<void> => {
    if (!schemaReadyRef.current) {
      setHistory(null);
      setHistoryError(null);
      setIsHistoryLoading(false);
      return;
    }
    const token = historyTokenRef.current + 1;
    historyTokenRef.current = token;
    historyAbortRef.current?.abort();
    const controller = new AbortController();
    historyAbortRef.current = controller;
    setIsHistoryLoading(true);
    try {
      let nextHistory = await fetchMonomerDftJobs(query, controller.signal);
      if (historyTokenRef.current !== token || controller.signal.aborted) return;
      const lastPage = Math.max(1, Math.ceil(nextHistory.total / query.page_size));
      if (query.page > lastPage) {
        const correctedQuery = { ...query, page: lastPage };
        historyQueryRef.current = correctedQuery;
        setHistoryQuery(correctedQuery);
        nextHistory = await fetchMonomerDftJobs(correctedQuery, controller.signal);
        if (historyTokenRef.current !== token || controller.signal.aborted) return;
      }
      setHistory(nextHistory);
      setHistoryError(null);
    } catch (error) {
      if (historyTokenRef.current !== token || controller.signal.aborted || isAbortError(error)) return;
      setHistoryError(errorMessage(error, "读取单体 DFT 任务历史失败。"));
    } finally {
      if (historyTokenRef.current === token && !controller.signal.aborted) {
        historyAbortRef.current = null;
        setIsHistoryLoading(false);
      }
    }
  }, []);

  const stopJobRead = useCallback((nextState?: MonomerDftPollState): void => {
    const session = jobReadSessionRef.current;
    jobReadSessionRef.current = null;
    session?.controller.abort();
    if (nextState) setPollState(nextState);
  }, []);

  const selectionMatches = useCallback((jobId: string, epoch: number): boolean => (
    activeJobIdRef.current === jobId && selectionEpochRef.current === epoch
  ), []);

  const refreshSelectedJob = useCallback(async (jobId: string, selectionEpoch: number): Promise<void> => {
    stopJobRead();
    const session = { jobId, selectionEpoch, controller: new AbortController() };
    jobReadSessionRef.current = session;
    const isCurrent = () => jobReadSessionRef.current === session && !session.controller.signal.aborted && selectionMatches(jobId, selectionEpoch);
    const requestOperationRevision = operationRevisionRef.current;
    setPollState("polling");
    try {
      const nextJob = await fetchMonomerDftJob(jobId, session.controller.signal);
      if (!isCurrent()) return;
      if (requestOperationRevision !== operationRevisionRef.current || cancellingJobIdRef.current === jobId || deletingArtifactsJobIdRef.current === jobId) {
        deferredEventRefresh.current = true;
        setPollState("watching");
        return;
      }
      setJob(nextJob);
      setJobError(nextJob.error ? userFacingMonomerDftMessage(nextJob.error.message, {
        code: nextJob.error.code, fallback: "计算任务未能完成，请检查输入后重试。"
      }) : null);
      setPollState(pollingIsComplete(nextJob) ? "terminal" : "watching");
    } catch (error) {
      if (!isCurrent() || isAbortError(error)) return;
      if (error instanceof MonomerDftApiError && error.status === 404) {
        activeJobIdRef.current = null;
        selectionEpochRef.current += 1;
        setJob(null);
        onJobIdChangeRef.current?.(null);
        setJobError("该任务已被删除或已按保留策略到期清理。");
      } else {
        setJobError(errorMessage(error, "读取单体 DFT 任务失败，请手动刷新。"));
      }
      setPollState("stopped");
    } finally {
      if (jobReadSessionRef.current === session) jobReadSessionRef.current = null;
    }
  }, [selectionMatches, stopJobRead]);

  const invalidateOperations = useCallback((): void => {
    operationRevisionRef.current += 1;
    cancelAbortRef.current?.abort();
    deleteAbortRef.current?.abort();
    submitAbortRef.current?.abort();
    cancelAbortRef.current = null;
    deleteAbortRef.current = null;
    submitAbortRef.current = null;
    cancellingJobIdRef.current = null;
    deletingArtifactsJobIdRef.current = null;
    setCancellingJobId(null);
    setDeletingArtifactsJobId(null);
    setIsSubmitting(false);
  }, []);

  const beginSelection = useCallback((jobId: string | null): number => {
    stopJobRead();
    invalidateOperations();
    selectionEpochRef.current += 1;
    activeJobIdRef.current = jobId;
    return selectionEpochRef.current;
  }, [invalidateOperations, stopJobRead]);

  const loadJob = useCallback((jobId: string, updateLocation = true) => {
    if (!schemaReadyRef.current) return;
    const normalizedJobId = jobId.trim();
    if (!normalizedJobId) {
      return;
    }
    const selectionEpoch = beginSelection(normalizedJobId);
    setJob(null);
    setJobError(null);
    if (updateLocation) {
      onJobIdChangeRef.current?.(normalizedJobId);
    }
    refreshSelectedJob(normalizedJobId, selectionEpoch);
  }, [beginSelection, refreshSelectedJob]);

  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    void refreshStatus(true).then(() => {
      if (!stopped && schemaReadyRef.current) void refreshHistory(historyQueryRef.current);
      else if (!stopped) setIsHistoryLoading(false);
    });
    return () => {
      stopped = true;
      statusTokenRef.current += 1;
      historyTokenRef.current += 1;
      statusAbortRef.current?.abort();
      historyAbortRef.current?.abort();
    };
  }, [enabled, refreshHistory, refreshStatus]);

  useEffect(() => {
    if (!enabled || serviceStatus?.schema_ready !== true || capabilities?.schema_ready !== true) {
      return;
    }
    if (!initialJobId) {
      if (activeJobIdRef.current) {
        beginSelection(null);
        setJob(null);
        setJobError(null);
        setPollState("idle");
      }
      return;
    }
    if (initialJobId !== activeJobIdRef.current) {
      loadJob(initialJobId, false);
    }
  }, [beginSelection, capabilities?.schema_ready, enabled, initialJobId, loadJob, serviceStatus?.schema_ready]);

  useEffect(() => () => {
    selectionEpochRef.current += 1;
    activeJobIdRef.current = null;
    operationRevisionRef.current += 1;
    stopJobRead();
    cancelAbortRef.current?.abort();
    deleteAbortRef.current?.abort();
    submitAbortRef.current?.abort();
    for (const controller of purgeControllersRef.current.values()) controller.abort();
  }, [stopJobRead]);

  async function submit(request: MonomerDftJobCreateRequest): Promise<string | null> {
    if (!schemaReadyRef.current) return null;
    const serializedRequest = JSON.stringify(request);
    const pendingSubmission = pendingSubmissionRef.current;
    const idempotencyKey = pendingSubmission?.payload === serializedRequest
      ? pendingSubmission.idempotencyKey
      : makeRequestId();
    pendingSubmissionRef.current = { payload: serializedRequest, idempotencyKey };
    stopJobRead();
    const operationRevision = operationRevisionRef.current + 1;
    operationRevisionRef.current = operationRevision;
    submitAbortRef.current?.abort();
    const controller = new AbortController();
    submitAbortRef.current = controller;
    setIsSubmitting(true);
    setJobError(null);
    try {
      const created = await createMonomerDftJob(request, idempotencyKey, controller.signal);
      if (controller.signal.aborted || operationRevisionRef.current !== operationRevision) return null;
      pendingSubmissionRef.current = null;
      const selectionEpoch = beginSelection(created.job_id);
      setJob(created);
      onJobIdChangeRef.current?.(created.job_id);
      refreshSelectedJob(created.job_id, selectionEpoch);
      void refreshHistory();
      void refreshStatus();
      return created.job_id;
    } catch (error) {
      if (controller.signal.aborted || isAbortError(error) || operationRevisionRef.current !== operationRevision) return null;
      setJobError(errorMessage(error, "提交单体 DFT 任务失败。"));
      return null;
    } finally {
      if (operationRevisionRef.current === operationRevision) {
        submitAbortRef.current = null;
        setIsSubmitting(false);
        flushDeferredEvent();
      }
    }
  }

  async function cancel(): Promise<void> {
    if (!schemaReadyRef.current || !job || isMonomerDftTerminal(job.status)) {
      return;
    }
    const targetJobId = job.job_id;
    const selectionEpoch = selectionEpochRef.current;
    const operationRevision = operationRevisionRef.current + 1;
    operationRevisionRef.current = operationRevision;
    cancelAbortRef.current?.abort();
    const controller = new AbortController();
    cancelAbortRef.current = controller;
    cancellingJobIdRef.current = targetJobId;
    setCancellingJobId(targetJobId);
    setJobError(null);
    try {
      const nextJob = await cancelMonomerDftJob(targetJobId, controller.signal);
      if (
        controller.signal.aborted ||
        operationRevisionRef.current !== operationRevision ||
        !selectionMatches(targetJobId, selectionEpoch)
      ) return;
      setJob(nextJob);
      if (pollingIsComplete(nextJob)) {
        stopJobRead("terminal");
      } else {
        stopJobRead("watching");
      }
      void refreshHistory();
    } catch (error) {
      if (controller.signal.aborted || isAbortError(error) || operationRevisionRef.current !== operationRevision) return;
      if (!selectionMatches(targetJobId, selectionEpoch)) return;
      setJobError(errorMessage(error, "取消单体 DFT 任务失败。"));
    } finally {
      if (operationRevisionRef.current === operationRevision) {
        cancelAbortRef.current = null;
        cancellingJobIdRef.current = null;
        setCancellingJobId(null);
        flushDeferredEvent();
      }
    }
  }

  async function rerun(): Promise<string | null> {
    if (!job) {
      return null;
    }
    return submit(job.request);
  }

  async function deleteArtifacts(): Promise<void> {
    if (!schemaReadyRef.current || !job || !isMonomerDftTerminal(job.status)) {
      return;
    }
    const targetJobId = job.job_id;
    const selectionEpoch = selectionEpochRef.current;
    const operationRevision = operationRevisionRef.current + 1;
    operationRevisionRef.current = operationRevision;
    deleteAbortRef.current?.abort();
    const controller = new AbortController();
    deleteAbortRef.current = controller;
    deletingArtifactsJobIdRef.current = targetJobId;
    setDeletingArtifactsJobId(targetJobId);
    setJobError(null);
    try {
      const nextJob = await deleteMonomerDftArtifactsAndReloadJob(targetJobId, controller.signal);
      if (
        controller.signal.aborted ||
        operationRevisionRef.current !== operationRevision ||
        !selectionMatches(targetJobId, selectionEpoch)
      ) return;
      setJob(nextJob);
      if (artifactDeletionIsPending(nextJob)) {
        stopJobRead("watching");
      } else {
        stopJobRead("terminal");
      }
      void refreshHistory();
    } catch (error) {
      if (controller.signal.aborted || isAbortError(error) || operationRevisionRef.current !== operationRevision) return;
      if (!selectionMatches(targetJobId, selectionEpoch)) return;
      setJobError(errorMessage(error, "删除单体 DFT 输出文件失败。"));
    } finally {
      if (operationRevisionRef.current === operationRevision) {
        deleteAbortRef.current = null;
        deletingArtifactsJobIdRef.current = null;
        setDeletingArtifactsJobId(null);
        flushDeferredEvent();
      }
    }
  }

  async function deleteJobRecord(target: MonomerDftJobResponse): Promise<void> {
    if (!schemaReadyRef.current || !isMonomerDftTerminal(target.status)) return;
    const targetJobId = target.job_id;
    const revision = (purgeRevisionsRef.current.get(targetJobId) ?? 0) + 1;
    purgeRevisionsRef.current.set(targetJobId, revision);
    purgeControllersRef.current.get(targetJobId)?.abort();
    const controller = new AbortController();
    purgeControllersRef.current.set(targetJobId, controller);
    if (activeJobIdRef.current === targetJobId) invalidateOperations();
    setDeletingJobIds((current) => current.includes(targetJobId) ? current : [...current, targetJobId]);
    setDeleteJobErrors((current) => {
      const next = { ...current };
      delete next[targetJobId];
      return next;
    });
    try {
      await deleteMonomerDftJob(targetJobId, controller.signal);
      if (
        controller.signal.aborted ||
        purgeRevisionsRef.current.get(targetJobId) !== revision
      ) return;
      setHistory((current) => current ? {
        ...current,
        total: Math.max(0, current.total - 1),
        items: current.items.filter((item) => item.job_id !== targetJobId)
      } : current);
      if (activeJobIdRef.current === targetJobId) {
        stopJobRead("idle");
        selectionEpochRef.current += 1;
        activeJobIdRef.current = null;
        setJob(null);
        setJobError(null);
        onJobIdChangeRef.current?.(null);
      }
      await Promise.allSettled([refreshHistory(), refreshStatus()]);
    } catch (error) {
      if (
        controller.signal.aborted ||
        isAbortError(error) ||
        purgeRevisionsRef.current.get(targetJobId) !== revision
      ) return;
      const message = errorMessage(error, "删除单体 DFT 任务失败。");
      setDeleteJobErrors((current) => ({ ...current, [targetJobId]: message }));
      if (activeJobIdRef.current === targetJobId) setJobError(message);
    } finally {
      if (purgeRevisionsRef.current.get(targetJobId) === revision) {
        purgeControllersRef.current.delete(targetJobId);
        setDeletingJobIds((current) => current.filter((id) => id !== targetJobId));
        flushDeferredEvent();
      }
    }
  }

  function changeHistoryQuery(update: Partial<MonomerDftJobListQuery>): void {
    const next = { ...historyQueryRef.current, ...update };
    historyQueryRef.current = next;
    setHistoryQuery(next);
    if (schemaReadyRef.current) void refreshHistory(next);
  }

  function clearJob(): void {
    beginSelection(null);
    setJob(null);
    setJobError(null);
    setPollState("idle");
    onJobIdChangeRef.current?.(null);
  }

  const selectedModel: MonomerDftModelCapability | null =
    capabilities?.models.find((item) => item.id === job?.request.model) ?? null;

  const isJobLoading = pollState === "polling";
  const refreshAll = async () => {
    await refreshStatus(true);
    if (!schemaReadyRef.current) return;
    const selected = activeJobIdRef.current;
    await Promise.allSettled([refreshHistory(), ...(selected ? [refreshSelectedJob(selected, selectionEpochRef.current)] : [])]);
  };
  function flushDeferredEvent() {
    if (deferredEventRefresh.current && !cancelAbortRef.current && !deleteAbortRef.current && !submitAbortRef.current && purgeControllersRef.current.size === 0) {
      deferredEventRefresh.current = false;
      void eventHandler.current({ ids: [], resync: true });
    }
  }
  eventHandler.current = async ({ ids, resync }) => {
    if (cancelAbortRef.current || deleteAbortRef.current || submitAbortRef.current || purgeControllersRef.current.size > 0) {
      deferredEventRefresh.current = true;
      return;
    }
    // A notification can precede the initial capabilities response. Preserve
    // that schema/capability check when replacing an in-flight status read.
    await refreshStatus(true);
    if (!schemaReadyRef.current) return;
    const selected = activeJobIdRef.current;
    await Promise.allSettled([refreshHistory(), ...(selected && (resync || ids.includes(selected))
      ? [refreshSelectedJob(selected, selectionEpochRef.current)] : [])]);
  };
  const isCancelling = cancellingJobId != null && cancellingJobId === job?.job_id;
  const isDeletingArtifacts = deletingArtifactsJobId != null && deletingArtifactsJobId === job?.job_id;

  return {
    serviceStatus,
    capabilities,
    job,
    history,
    historyQuery,
    selectedModel,
    pollState,
    isServiceLoading,
    isHistoryLoading,
    isJobLoading,
    isSubmitting,
    isCancelling,
    cancellingJobId,
    isDeletingArtifacts,
    deletingJobIds,
    deleteJobErrors,
    serviceError,
    historyError,
    jobError,
    eventConnectionState,
    refreshAll: async () => { if (eventConnectionState === "unavailable") reconnect(); await refreshAll(); },
    refreshStatus: () => refreshStatus(true),
    refreshHistory,
    changeHistoryQuery,
    loadJob,
    submit,
    cancel,
    rerun,
    deleteArtifacts,
    deleteJobRecord,
    clearJob
  };
}
