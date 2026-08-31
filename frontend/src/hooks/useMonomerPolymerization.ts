import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchMonomerPolymerizationStatus,
  runMonomerPolymerization
} from "../services/api";
import type {
  MonomerPolymerizationRequest,
  MonomerPolymerizationResponse,
  MonomerPolymerizationStatusResponse
} from "../types";

type MonomerPolymerizationState = {
  status: MonomerPolymerizationStatusResponse | null;
  statusLoading: boolean;
  statusError: string | null;
  data: MonomerPolymerizationResponse | null;
  runLoading: boolean;
  runError: string | null;
};

function isAbortError(error: unknown) {
  return error instanceof DOMException
    ? error.name === "AbortError"
    : error instanceof Error && error.name === "AbortError";
}
function messageFromError(error: unknown, fallback: string, networkFallback: string) {
  if (!(error instanceof Error) || !error.message.trim()) return fallback;
  if (
    error instanceof TypeError ||
    /Failed to fetch|NetworkError|Load failed/i.test(error.message) ||
    /^Request (?:validation )?failed with status \d+$/.test(error.message)
  ) {
    return networkFallback;
  }
  if (/not installed|cannot load its rule files/i.test(error.message)) {
    return "SMiPoly 运行环境未就绪或规则文件无法加载。";
  }
  if (/dummy atom|wildcard|attachment point/i.test(error.message)) {
    return "请输入不含 * 连接点的普通单体 SMILES。";
  }
  if (/requires monomer_b_smiles/i.test(error.message)) {
    return "当前目标聚合物类型需要填写单体 B。";
  }
  return error.message;
}

export function useMonomerPolymerization() {
  const [state, setState] = useState<MonomerPolymerizationState>({
    status: null,
    statusLoading: true,
    statusError: null,
    data: null,
    runLoading: false,
    runError: null
  });
  const statusAbortRef = useRef<AbortController | null>(null);
  const runAbortRef = useRef<AbortController | null>(null);
  const statusRevisionRef = useRef(0);
  const runRevisionRef = useRef(0);

  const refreshStatus = useCallback(async () => {
    statusAbortRef.current?.abort();
    const controller = new AbortController();
    const revision = statusRevisionRef.current + 1;
    statusRevisionRef.current = revision;
    statusAbortRef.current = controller;
    setState((current) => ({
      ...current,
      statusLoading: true,
      statusError: null
    }));

    try {
      const status = await fetchMonomerPolymerizationStatus(controller.signal);
      if (controller.signal.aborted || statusRevisionRef.current !== revision) return null;
      setState((current) => ({
        ...current,
        status,
        statusLoading: false,
        statusError: null
      }));
      return status;
    } catch (error) {
      if (controller.signal.aborted || statusRevisionRef.current !== revision || isAbortError(error)) {
        return null;
      }
      setState((current) => ({
        ...current,
        status: null,
        statusLoading: false,
        statusError: messageFromError(
          error,
          "无法读取 SMiPoly 服务状态。",
          "无法连接 SMiPoly 状态服务，请检查网络后重试。"
        )
      }));
      return null;
    } finally {
      if (statusAbortRef.current === controller) statusAbortRef.current = null;
    }
  }, []);

  const run = useCallback(async (request: MonomerPolymerizationRequest) => {
    runAbortRef.current?.abort();
    const controller = new AbortController();
    const revision = runRevisionRef.current + 1;
    runRevisionRef.current = revision;
    runAbortRef.current = controller;
    setState((current) => ({
      ...current,
      data: null,
      runLoading: true,
      runError: null
    }));

    try {
      const data = await runMonomerPolymerization(request, controller.signal);
      if (controller.signal.aborted || runRevisionRef.current !== revision) return null;
      setState((current) => ({
        ...current,
        data,
        runLoading: false,
        runError: null
      }));
      return data;
    } catch (error) {
      if (controller.signal.aborted || runRevisionRef.current !== revision || isAbortError(error)) {
        return null;
      }
      setState((current) => ({
        ...current,
        data: null,
        runLoading: false,
        runError: messageFromError(
          error,
          "单体正向聚合失败，请稍后重试。",
          "单体正向聚合请求失败，请检查网络或稍后重试。"
        )
      }));
      return null;
    } finally {
      if (runAbortRef.current === controller) runAbortRef.current = null;
    }
  }, []);

  const clearResults = useCallback(() => {
    runRevisionRef.current += 1;
    runAbortRef.current?.abort();
    runAbortRef.current = null;
    setState((current) => ({
      ...current,
      data: null,
      runLoading: false,
      runError: null
    }));
  }, []);

  useEffect(() => {
    void refreshStatus();
    return () => {
      statusRevisionRef.current += 1;
      runRevisionRef.current += 1;
      statusAbortRef.current?.abort();
      runAbortRef.current?.abort();
      statusAbortRef.current = null;
      runAbortRef.current = null;
    };
  }, [refreshStatus]);

  return {
    ...state,
    refreshStatus,
    run,
    clearResults
  };
}
