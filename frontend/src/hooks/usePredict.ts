import { useCallback, useEffect, useRef, useState } from "react";
import { predictSmiles } from "../services/api";
import type { PredictRequest, PredictResponse } from "../types";

type PredictState = {
  isLoading: boolean;
  error: string | null;
  data: PredictResponse | null;
};

function localPredictionError(error: unknown) {
  if (!(error instanceof Error) || !error.message.trim()) {
    return "性质预测失败，请稍后重试。";
  }
  if (
    error instanceof TypeError ||
    /^Request (?:validation )?failed with status \d+$/.test(error.message)
  ) {
    return "性质预测请求失败，请检查网络或稍后重试。";
  }
  return error.message;
}

export function usePredict() {
  const [state, setState] = useState<PredictState>({
    isLoading: false,
    error: null,
    data: null
  });
  const abortRef = useRef<AbortController | null>(null);
  const requestRevisionRef = useRef(0);

  useEffect(() => {
    return () => {
      requestRevisionRef.current += 1;
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, []);

  const submit = useCallback(async (request: PredictRequest) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    const revision = requestRevisionRef.current + 1;
    requestRevisionRef.current = revision;
    abortRef.current = controller;

    setState({
      isLoading: true,
      error: null,
      data: null
    });

    try {
      const data = await predictSmiles(request, controller.signal);
      if (controller.signal.aborted || requestRevisionRef.current !== revision) {
        return data;
      }
      setState({
        isLoading: false,
        error: null,
        data
      });
      return data;
    } catch (error) {
      if (controller.signal.aborted || requestRevisionRef.current !== revision) {
        throw error;
      }
      setState({
        isLoading: false,
        error: localPredictionError(error),
        data: null
      });
      throw error;
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
      }
    }
  }, []);

  return {
    ...state,
    submit
  };
}
