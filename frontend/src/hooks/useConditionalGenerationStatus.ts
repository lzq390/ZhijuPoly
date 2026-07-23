import { useCallback, useEffect, useRef, useState } from "react";
import { fetchConditionalGenerationTgStatus } from "../services/api";
import type { ConditionalGenerationTgStatusResponse } from "../types";
import { abortableDelay, isAbortError, JOB_POLL_BACKOFF_MS } from "./jobPolling";

type ConditionalGenerationStatusState = {
  serviceStatus: ConditionalGenerationTgStatusResponse | null;
  serviceStatusError: string | null;
  isStatusLoading: boolean;
};

export const CONDITIONAL_STATUS_RETRY_DELAYS_MS = JOB_POLL_BACKOFF_MS;

export function useConditionalGenerationStatus() {
  const [state, setState] = useState<ConditionalGenerationStatusState>({
    serviceStatus: null,
    serviceStatusError: null,
    isStatusLoading: true
  });
  const requestTokenRef = useRef(0);
  const requestAbortRef = useRef<AbortController | null>(null);

  const refreshStatus = useCallback(async () => {
    requestAbortRef.current?.abort();
    const token = requestTokenRef.current + 1;
    requestTokenRef.current = token;
    const controller = new AbortController();
    requestAbortRef.current = controller;
    setState({ serviceStatus: null, serviceStatusError: null, isStatusLoading: true });

    let lastError: unknown = null;
    try {
      for (let attempt = 0; attempt <= CONDITIONAL_STATUS_RETRY_DELAYS_MS.length; attempt += 1) {
        try {
          const serviceStatus = await fetchConditionalGenerationTgStatus(controller.signal);
          if (requestTokenRef.current !== token || controller.signal.aborted) {
            return;
          }
          setState({ serviceStatus, serviceStatusError: null, isStatusLoading: false });
          return;
        } catch (error) {
          if (requestTokenRef.current !== token || controller.signal.aborted || isAbortError(error)) {
            return;
          }
          lastError = error;
        }

        if (attempt === CONDITIONAL_STATUS_RETRY_DELAYS_MS.length) {
          break;
        }
        if (!(await abortableDelay(CONDITIONAL_STATUS_RETRY_DELAYS_MS[attempt], controller.signal))) {
          return;
        }
      }

      if (requestTokenRef.current === token && !controller.signal.aborted) {
        setState({
          serviceStatus: null,
          serviceStatusError: lastError instanceof Error ? lastError.message : "Failed to check generation service.",
          isStatusLoading: false
        });
      }
    } finally {
      if (requestAbortRef.current === controller) {
        requestAbortRef.current = null;
      }
    }
  }, []);

  useEffect(() => {
    void refreshStatus();
    return () => {
      requestTokenRef.current += 1;
      requestAbortRef.current?.abort();
      requestAbortRef.current = null;
    };
  }, [refreshStatus]);

  return {
    ...state,
    refreshStatus
  };
}
