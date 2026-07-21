import { useCallback, useEffect, useRef, useState } from "react";
import {
  createMonomerMdJob,
  deleteMonomerMdArtifacts,
  fetchMonomerMdJob,
  fetchMonomerMdProtocols,
  fetchMonomerMdStatus
} from "../services/api";
import type {
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
  error: string | null;
  data: MonomerMdSimulationResult | null;
  job: MonomerMdJobResponse | null;
  serviceStatus: MonomerMdServiceStatusResponse | null;
  protocolCatalog: MonomerMdProtocolCatalogResponse | null;
  isStatusLoading: boolean;
  statusError: string | null;
  protocolsError: string | null;
  artifactDeleteError: string | null;
};

const POLL_INTERVAL_MS = 1400;
const SERVICE_STATUS_POLL_INTERVAL_MS = 5000;
const TERMINAL_STATUSES = new Set<MonomerMdJobStatus>(["completed", "failed", "cancelled"]);

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

export function useMonomerMdSimulation() {
  const [smiles, setSmiles] = useState("");
  const [runMode, setRunMode] = useState<MonomerMdRunMode>("demo");
  const [selectedProtocol, setSelectedProtocol] = useState<MonomerMdProtocol>("Density");
  const [configText, setConfigText] = useState("");
  const [state, setState] = useState<MonomerMdSimulationState>({
    isLoading: false,
    error: null,
    data: null,
    job: null,
    serviceStatus: null,
    protocolCatalog: null,
    isStatusLoading: false,
    statusError: null,
    protocolsError: null,
    artifactDeleteError: null
  });
  const pollTokenRef = useRef(0);
  const pollAbortRef = useRef<AbortController | null>(null);
  const statusLoaderRef = useRef<MonomerMdStatusLoader | null>(null);
  if (statusLoaderRef.current === null) {
    statusLoaderRef.current = new MonomerMdStatusLoader(fetchMonomerMdStatus, fetchMonomerMdProtocols);
  }

  const refreshStatus = useCallback(async () => {
    setState((current) => ({ ...current, isStatusLoading: true, statusError: null, protocolsError: null }));
    const result = await statusLoaderRef.current?.load();
    if (!result) {
      return;
    }
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

  useEffect(() => {
    if (configText.trim()) {
      return;
    }
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
      pollAbortRef.current = null;
      statusLoaderRef.current?.cancel();
    };
  }, [refreshStatus]);

  useEffect(() => {
    const shouldPollStatus = state.serviceStatus?.busy === true || state.serviceStatus?.draining === true;
    if (!shouldPollStatus) {
      return;
    }

    let cancelled = false;
    let timer: number | null = null;
    const scheduleRefresh = () => {
      timer = window.setTimeout(() => {
        void refreshStatus().finally(() => {
          if (!cancelled) {
            scheduleRefresh();
          }
        });
      }, SERVICE_STATUS_POLL_INTERVAL_MS);
    };

    scheduleRefresh();
    return () => {
      cancelled = true;
      if (timer != null) {
        window.clearTimeout(timer);
      }
    };
  }, [refreshStatus, state.serviceStatus?.busy, state.serviceStatus?.draining]);

  async function pollJob(jobId: string, token: number, controller: AbortController) {
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
            error: "单体 MD 任务不存在或已过期，请重新提交。"
          }));
        }
      },
      onJob: (job) => {
        if (pollTokenRef.current !== token || controller.signal.aborted) {
          return;
        }
        const result = getMonomerMdJobResult(job);
        const isTerminal = TERMINAL_STATUSES.has(job.status);
        setState((current) => ({
          ...current,
          isLoading: !isTerminal,
          error: job.status === "failed" || job.status === "cancelled" ? jobErrorMessage(job) : null,
          data: result ?? current.data,
          job
        }));
      }
    });
  }

  async function submit(nextSmiles = smiles) {
    const normalizedSmiles = nextSmiles.trim();
    const validationError = runMode === "demo" ? getMonomerMdSmilesValidationError(normalizedSmiles) : null;
    if (validationError) {
      setState((current) => ({ ...current, isLoading: false, error: validationError }));
      return;
    }
    let payload;
    if (runMode === "formal") {
      const config = validateFormalConfig(configText, selectedProtocol);
      if (typeof config === "string") {
        setState((current) => ({ ...current, isLoading: false, error: config }));
        return;
      }
      payload = { protocol: selectedProtocol, run_mode: "formal" as const, config_json: config };
    } else {
      payload = { smiles: normalizedSmiles };
    }
    pollAbortRef.current?.abort();
    const token = pollTokenRef.current + 1;
    pollTokenRef.current = token;
    const controller = new AbortController();
    pollAbortRef.current = controller;
    setSmiles(normalizedSmiles);
    setState((current) => ({ ...current, isLoading: true, error: null, artifactDeleteError: null, data: null, job: null }));
    try {
      const createdJob = await createMonomerMdJob(payload, controller.signal);
      if (pollTokenRef.current !== token || controller.signal.aborted) {
        return;
      }
      setState((current) => ({
        ...current,
        job: { job_id: createdJob.job_id, status: createdJob.status, smiles: normalizedSmiles, protocol: runMode === "formal" ? selectedProtocol : "DensityDemo", run_mode: runMode }
      }));
      await pollJob(createdJob.job_id, token, controller);
    } catch (error) {
      if (pollTokenRef.current !== token || controller.signal.aborted || isAbortError(error)) {
        return;
      }
      setState((current) => ({
        ...current,
        isLoading: false,
        error: error instanceof Error ? error.message : "提交单体 MD 模拟失败。"
      }));
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
    setState((current) => ({ ...current, isLoading: false, error: null, data: null, job: null }));
  }

  async function deleteArtifacts() {
    if (!state.job?.job_id) {
      return;
    }
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
        artifactDeleteError: error instanceof Error ? error.message : "删除输出文件失败。"
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
    ...state,
    submit,
    reset,
    refreshStatus,
    loadProtocolTemplate,
    deleteArtifacts
  };
}
