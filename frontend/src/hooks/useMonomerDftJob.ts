import { useCallback, useEffect, useRef, useState } from "react";
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

export const MONOMER_DFT_JOB_POLL_MS = 1_500;
export const MONOMER_DFT_STATUS_POLL_MS = 10_000;
export const MONOMER_DFT_HISTORY_PAGE_SIZE = 20;
export const MONOMER_DFT_JOB_BACKOFF_MS = [1_500, 3_000, 6_000, 10_000] as const;

export type MonomerDftPollState = "idle" | "polling" | "degraded" | "terminal" | "stopped";

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
  b: "B",
  c: "C",
  n: "N",
  o: "O",
  p: "P",
  s: "S"
};

export function extractElementsFromSmiles(smiles: string): string[] {
  const tokens = smiles.match(/Cl|Br|Si|Se|Na|Li|Mg|Ca|Al|Zn|Fe|Cu|Pd|Pt|[A-Z][a-z]?|[bcnops]/g) ?? [];
  return [...new Set(tokens.map((token) => AROMATIC_ELEMENT[token] ?? token))];
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
      issues.push({ field: "smiles", message: "检测到 PSMILES 连接位点，请选择闭环或封端预处理方式。" });
    }
    if (!smiles.includes("*") && input.psmilesMode != null) {
      issues.push({ field: "smiles", message: "普通单体不需要 PSMILES 预处理方式。" });
    }
    if (input.psmilesMode === "close" && [...smiles].filter((character) => character === "*").length !== 2) {
      issues.push({ field: "smiles", message: "PSMILES 闭环模式要求恰好两个 * 连接位点。" });
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
    if (input.fmax == null || !Number.isFinite(input.fmax) || input.fmax < 0.001 || input.fmax > 1) {
      issues.push({ field: "optimization", message: "Fmax 阈值必须在 0.001–1.0 eV/Å 范围内。" });
    }
    if (input.maxSteps == null || !Number.isInteger(input.maxSteps) || input.maxSteps < 10 || input.maxSteps > 50) {
      issues.push({ field: "optimization", message: "最大优化步数必须是 10–50 的整数。" });
    }
  }
  if (input.seed != null && (!Number.isInteger(input.seed) || input.seed < 0 || input.seed > 2_147_483_647)) {
    issues.push({ field: "conformer", message: "构象 seed 必须是 0–2147483647 的整数。" });
  }
  if (input.maxIterations != null && (!Number.isInteger(input.maxIterations) || input.maxIterations < 1 || input.maxIterations > 5000)) {
    issues.push({ field: "conformer", message: "构象最大迭代必须是 1–5000 的整数。" });
  }
  if (!capabilities) {
    issues.push({ field: "model", message: "正在等待后端返回模型能力目录。" });
    return issues;
  }
  const model = capabilities.models.find((item) => item.id === input.modelId);
  if (!model) {
    issues.push({ field: "model", message: "请选择能力目录中的模型。" });
    return issues;
  }
  if (!model.available) {
    issues.push({ field: "model", message: `${model.label} 当前不可用。` });
  }
  if (!model.supported_calculation_types.includes(input.calculationType)) {
    issues.push({ field: "calculation", message: `${model.label} 不支持当前计算类型。` });
  }
  if (input.multiplicity !== 1 && !model.supports_spin) {
    issues.push({ field: "multiplicity", message: `${model.label} 不支持开放壳层；请使用单重态或选择支持多重度的模型。` });
  }
  const chargeWithinGlobalContract = input.netCharge == null || (
    Number.isInteger(input.netCharge) && input.netCharge >= -5 && input.netCharge <= 5
  );
  if (chargeWithinGlobalContract && model.charge_min != null && input.netCharge != null && input.netCharge < model.charge_min) {
    issues.push({ field: "charge", message: `${model.label} 支持的最小总电荷为 ${model.charge_min}。` });
  }
  if (chargeWithinGlobalContract && model.charge_max != null && input.netCharge != null && input.netCharge > model.charge_max) {
    issues.push({ field: "charge", message: `${model.label} 支持的最大总电荷为 ${model.charge_max}。` });
  }
  const unsupportedElements = extractElementsFromSmiles(smiles).filter(
    (element) => model.supported_elements.length > 0 && !model.supported_elements.includes(element)
  );
  if (unsupportedElements.length > 0) {
    issues.push({
      field: "smiles",
      message: `${model.label} 不支持当前结构中的元素：${unsupportedElements.join("、")}。`
    });
  }
  if (input.calculationType === "single_point") {
    if (!input.properties.includes("energy")) {
      issues.push({ field: "properties", message: "单点计算必须包含能量。" });
    }
    const unsupportedProperties = input.properties.filter(
      (property) => !model.supported_properties.includes(property)
    );
    if (unsupportedProperties.length > 0) {
      issues.push({
        field: "properties",
        message: `${model.label} 不支持：${unsupportedProperties.join("、")}。`
      });
    }
  }
  return issues;
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim() ? error.message : fallback;
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
  initialJobId?: string | null;
  onJobIdChange?: (jobId: string | null) => void;
};

type JobPollSession = {
  jobId: string;
  selectionEpoch: number;
  controller: AbortController;
  timer: ReturnType<typeof globalThis.setTimeout> | null;
};

export function useMonomerDftJob({ initialJobId = null, onJobIdChange }: UseMonomerDftJobOptions = {}) {
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
  const [isServiceLoading, setIsServiceLoading] = useState(true);
  const [isHistoryLoading, setIsHistoryLoading] = useState(true);
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
  const jobPollSessionRef = useRef<JobPollSession | null>(null);
  const cancelAbortRef = useRef<AbortController | null>(null);
  const deleteAbortRef = useRef<AbortController | null>(null);
  const submitAbortRef = useRef<AbortController | null>(null);
  const cancellingJobIdRef = useRef<string | null>(null);
  const deletingArtifactsJobIdRef = useRef<string | null>(null);
  const pendingSubmissionRef = useRef<{ payload: string; idempotencyKey: string } | null>(null);
  const schemaReadyRef = useRef(false);
  const knownJobIdsRef = useRef(new Set<string>());
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
    const pollSession = jobPollSessionRef.current;
    jobPollSessionRef.current = null;
    if (pollSession?.timer != null) globalThis.clearTimeout(pollSession.timer);
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
    knownJobIdsRef.current.clear();
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
      for (const item of nextHistory.items) knownJobIdsRef.current.add(item.job_id);
      setHistory(nextHistory);
      setHistoryError(null);
    } catch (error) {
      if (historyTokenRef.current !== token || controller.signal.aborted || isAbortError(error)) return;
      setHistoryError(errorMessage(error, "读取单体 DFT 全局任务历史失败。"));
    } finally {
      if (historyTokenRef.current === token && !controller.signal.aborted) {
        historyAbortRef.current = null;
        setIsHistoryLoading(false);
      }
    }
  }, []);

  const stopJobPoll = useCallback((nextState?: MonomerDftPollState): void => {
    const session = jobPollSessionRef.current;
    jobPollSessionRef.current = null;
    if (session?.timer != null) globalThis.clearTimeout(session.timer);
    session?.controller.abort();
    if (nextState) setPollState(nextState);
  }, []);

  const selectionMatches = useCallback((jobId: string, epoch: number): boolean => (
    activeJobIdRef.current === jobId && selectionEpochRef.current === epoch
  ), []);

  const startJobPoll = useCallback((jobId: string, selectionEpoch: number): void => {
    stopJobPoll();
    const session: JobPollSession = {
      jobId,
      selectionEpoch,
      controller: new AbortController(),
      timer: null
    };
    jobPollSessionRef.current = session;
    let transientFailures = 0;

    const isCurrent = () => (
      jobPollSessionRef.current === session &&
      !session.controller.signal.aborted &&
      selectionMatches(jobId, selectionEpoch)
    );
    const schedule = (delayMs: number) => {
      if (!isCurrent()) return;
      session.timer = globalThis.setTimeout(() => {
        session.timer = null;
        void pollOnce();
      }, delayMs);
    };
    const pollOnce = async (): Promise<void> => {
      if (!isCurrent()) return;
      const requestOperationRevision = operationRevisionRef.current;
      try {
        const nextJob = await fetchMonomerDftJob(jobId, session.controller.signal);
        if (!isCurrent()) return;
        knownJobIdsRef.current.add(jobId);
        if (
          requestOperationRevision !== operationRevisionRef.current ||
          cancellingJobIdRef.current === jobId ||
          deletingArtifactsJobIdRef.current === jobId
        ) {
          schedule(MONOMER_DFT_JOB_POLL_MS);
          return;
        }
        transientFailures = 0;
        setJob(nextJob);
        setJobError(nextJob.error?.message ?? null);
        if (pollingIsComplete(nextJob)) {
          jobPollSessionRef.current = null;
          setPollState("terminal");
          void refreshHistory();
          void refreshStatus();
          return;
        }
        setPollState("polling");
        schedule(MONOMER_DFT_JOB_POLL_MS);
      } catch (error) {
        if (!isCurrent() || isAbortError(error)) return;
        if (
          error instanceof MonomerDftApiError &&
          error.status === 404 &&
          knownJobIdsRef.current.has(jobId)
        ) {
          jobPollSessionRef.current = null;
          activeJobIdRef.current = null;
          selectionEpochRef.current += 1;
          setJob(null);
          setPollState("stopped");
          setJobError("该任务已被删除或已按保留策略到期清理。");
          onJobIdChangeRef.current?.(null);
          void refreshHistory();
          return;
        }
        if (!isRetryableMonomerDftPollError(error)) {
          jobPollSessionRef.current = null;
          setPollState("stopped");
          setJobError(errorMessage(error, "读取单体 DFT 任务失败。"));
          return;
        }
        transientFailures += 1;
        setPollState("degraded");
        setJobError(errorMessage(error, "读取单体 DFT 任务失败。"));
        schedule(monomerDftPollRetryDelayMs(error, transientFailures));
      }
    };

    setPollState("polling");
    void pollOnce();
  }, [refreshHistory, refreshStatus, selectionMatches, stopJobPoll]);

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
    stopJobPoll();
    invalidateOperations();
    selectionEpochRef.current += 1;
    activeJobIdRef.current = jobId;
    return selectionEpochRef.current;
  }, [invalidateOperations, stopJobPoll]);

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
    startJobPoll(normalizedJobId, selectionEpoch);
  }, [beginSelection, startJobPoll]);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof globalThis.setTimeout> | null = null;
    const refresh = async () => {
      await refreshStatus(true);
      if (schemaReadyRef.current) {
        await refreshHistory(historyQueryRef.current);
      }
      if (!stopped) {
        timer = globalThis.setTimeout(() => void refresh(), MONOMER_DFT_STATUS_POLL_MS);
      }
    };
    void refresh();
    return () => {
      stopped = true;
      if (timer != null) globalThis.clearTimeout(timer);
      statusTokenRef.current += 1;
      historyTokenRef.current += 1;
      statusAbortRef.current?.abort();
      historyAbortRef.current?.abort();
      statusAbortRef.current = null;
      historyAbortRef.current = null;
    };
  }, [refreshHistory, refreshStatus]);

  useEffect(() => {
    if (serviceStatus?.schema_ready !== true) {
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
  }, [beginSelection, initialJobId, loadJob, serviceStatus?.schema_ready]);

  useEffect(() => () => {
    selectionEpochRef.current += 1;
    activeJobIdRef.current = null;
    operationRevisionRef.current += 1;
    stopJobPoll();
    cancelAbortRef.current?.abort();
    deleteAbortRef.current?.abort();
    submitAbortRef.current?.abort();
    for (const controller of purgeControllersRef.current.values()) controller.abort();
  }, [stopJobPoll]);

  async function submit(request: MonomerDftJobCreateRequest): Promise<string | null> {
    if (!schemaReadyRef.current) return null;
    const serializedRequest = JSON.stringify(request);
    const pendingSubmission = pendingSubmissionRef.current;
    const idempotencyKey = pendingSubmission?.payload === serializedRequest
      ? pendingSubmission.idempotencyKey
      : makeRequestId();
    pendingSubmissionRef.current = { payload: serializedRequest, idempotencyKey };
    stopJobPoll();
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
      knownJobIdsRef.current.add(created.job_id);
      const selectionEpoch = beginSelection(created.job_id);
      setJob(created);
      onJobIdChangeRef.current?.(created.job_id);
      startJobPoll(created.job_id, selectionEpoch);
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
        stopJobPoll("terminal");
      } else if (jobPollSessionRef.current == null) {
        startJobPoll(targetJobId, selectionEpoch);
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
        if (jobPollSessionRef.current == null) startJobPoll(targetJobId, selectionEpoch);
      } else {
        stopJobPoll("terminal");
      }
      void refreshHistory();
    } catch (error) {
      if (controller.signal.aborted || isAbortError(error) || operationRevisionRef.current !== operationRevision) return;
      if (!selectionMatches(targetJobId, selectionEpoch)) return;
      setJobError(errorMessage(error, "删除单体 DFT 任务产物失败。"));
    } finally {
      if (operationRevisionRef.current === operationRevision) {
        deleteAbortRef.current = null;
        deletingArtifactsJobIdRef.current = null;
        setDeletingArtifactsJobId(null);
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
    if (activeJobIdRef.current === targetJobId) operationRevisionRef.current += 1;
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
      knownJobIdsRef.current.delete(targetJobId);
      setHistory((current) => current ? {
        ...current,
        total: Math.max(0, current.total - 1),
        items: current.items.filter((item) => item.job_id !== targetJobId)
      } : current);
      if (activeJobIdRef.current === targetJobId) {
        stopJobPoll("idle");
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

  const isJobLoading = pollState === "polling" || pollState === "degraded";
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
