import { useCallback, useEffect, useRef, useState } from "react";
import { requestServiceAccess } from "../auth/guestAccess";
import { getSessionEpoch, onSessionRetired } from "../auth/session";
import { predictMonomerPrecursors } from "../services/api";
import type { MonomerRetrosynthesisRequest, MonomerRetrosynthesisResponse } from "../types";

type State = {
  loading: boolean;
  error: string | null;
  data: MonomerRetrosynthesisResponse | null;
  snapshot: MonomerRetrosynthesisRequest | null;
};
const EMPTY: State = { loading: false, error: null, data: null, snapshot: null };

export function useMonomerRetrosynthesis() {
  const identityEpoch = useRef(getSessionEpoch()).current;
  const controllerRef = useRef<AbortController | null>(null);
  const revisionRef = useRef(0);
  const [state, setState] = useState<State>(EMPTY);

  const cancel = useCallback(() => {
    revisionRef.current += 1;
    controllerRef.current?.abort();
    controllerRef.current = null;
  }, []);

  useEffect(() => {
    const unsubscribe = onSessionRetired(() => { cancel(); setState(EMPTY); });
    return () => { unsubscribe(); cancel(); };
  }, [cancel]);

  // Returning true means a request started, so the page can open its results.
  const run = useCallback((request: MonomerRetrosynthesisRequest) => {
    if (identityEpoch !== getSessionEpoch() || !requestServiceAccess()) return false;
    cancel();
    const controller = new AbortController();
    controllerRef.current = controller;
    const revision = revisionRef.current;
    const current = () => !controller.signal.aborted && revisionRef.current === revision && identityEpoch === getSessionEpoch();
    setState({ loading: true, error: null, data: null, snapshot: { ...request } });
    void predictMonomerPrecursors(request, controller.signal).then(data => {
      if (current()) setState(previous => ({ ...previous, data }));
    }).catch(error => {
      if (!current() || (error instanceof Error && error.name === "AbortError")) return;
      setState(previous => ({ ...previous, error: error instanceof Error && error.message.trim()
        ? error.message : "单体逆合成反推失败，请稍后重试。" }));
    }).finally(() => {
      if (!current()) return;
      controllerRef.current = null;
      setState(previous => ({ ...previous, loading: false }));
    });
    return true;
  }, [cancel, identityEpoch]);

  return { ...state, run };
}
