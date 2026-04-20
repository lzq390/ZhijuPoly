import { useState } from "react";
import { predictSmiles } from "../services/api";
import type { PredictRequest, PredictResponse } from "../types";

type PredictState = {
  isLoading: boolean;
  error: string | null;
  data: PredictResponse | null;
};

export function usePredict() {
  const [state, setState] = useState<PredictState>({
    isLoading: false,
    error: null,
    data: null
  });

  async function submit(request: PredictRequest) {
    setState((current) => ({
      ...current,
      isLoading: true,
      error: null
    }));

    try {
      const data = await predictSmiles(request);
      setState({
        isLoading: false,
        error: null,
        data
      });
      return data;
    } catch (error) {
      setState({
        isLoading: false,
        error: error instanceof Error ? error.message : "Unknown error",
        data: null
      });
      throw error;
    }
  }

  return {
    ...state,
    submit
  };
}
