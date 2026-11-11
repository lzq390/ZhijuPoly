import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useAuth } from "../auth/AuthProvider";
import { getSession, getSessionEpoch, onSessionRetired, sessionIsReady, type AuthSession } from "../auth/session";
import { fetchOverviewData } from "../services/overviewData";
import type { OverviewData, OverviewDataState } from "../types/overview";

type ReadState = { key: string; data: OverviewData | null; loading: boolean; refreshing: boolean; error: string | null };
const sessionKey = (session: AuthSession, epoch: number) => `${session.user?.id ?? "guest"}:${session.session_id ?? ""}:${epoch}`;

export function useOverviewData(): OverviewDataState {
  const auth = useAuth();
  const generation = useRef(0);
  const pending = useRef<AbortController | null>(null);
  const subscribe = useCallback((notify: () => void) => onSessionRetired(() => {
    ++generation.current;
    pending.current?.abort();
    pending.current = null;
    notify();
  }), []);
  const epoch = useSyncExternalStore(subscribe, getSessionEpoch, getSessionEpoch);
  const session = auth?.session ?? getSession();
  const key = sessionKey(session, epoch);
  const currentKey = sessionKey(getSession(), epoch);
  const access: OverviewDataState["access"] = !sessionIsReady() || auth?.status === "loading" || auth?.status === "error" || key !== currentKey ? "checking"
    : !session.authenticated || !session.session_id || auth?.status === "guest" ? "guest"
    : session.account_type === "free" ? "restricted" : "member";
  const [state, setState] = useState<ReadState>({ key, data: null, loading: access === "member", refreshing: false, error: null });

  const load = useCallback(async () => {
    if (access !== "member" || pending.current || !sessionIsReady() || sessionKey(getSession(), getSessionEpoch()) !== key) return;
    const requestGeneration = ++generation.current;
    const controller = new AbortController();
    pending.current = controller;
    const current = () => !controller.signal.aborted && requestGeneration === generation.current && epoch === getSessionEpoch()
      && sessionIsReady() && sessionKey(getSession(), getSessionEpoch()) === key;
    setState(previous => ({ key, data: previous.key === key ? previous.data : null, loading: previous.key !== key || !previous.data, refreshing: previous.key === key && Boolean(previous.data), error: null }));
    try {
      const data = await fetchOverviewData(controller.signal);
      if (current()) setState({ key, data, loading: false, refreshing: false, error: null });
    } catch (error) {
      if (current()) setState(previous => ({ ...previous, loading: false, refreshing: false, error: error instanceof Error && error.name !== "AbortError" ? error.message : "总览暂时无法刷新，请重试。" }));
    } finally {
      if (pending.current === controller) pending.current = null;
    }
  }, [access, epoch, key]);

  useEffect(() => {
    setState({ key, data: null, loading: access === "member", refreshing: false, error: null });
    if (access !== "member") return;
    void load();
    const visible = () => { if (document.visibilityState === "visible") void load(); };
    const timer = window.setInterval(visible, 60_000);
    window.addEventListener("focus", visible);
    document.addEventListener("visibilitychange", visible);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", visible);
      document.removeEventListener("visibilitychange", visible);
      ++generation.current;
      pending.current?.abort();
      pending.current = null;
    };
  }, [access, key, load]);

  const refresh = useCallback(() => { void load(); }, [load]);
  const matching = access === "member" && state.key === key;
  return {
    data: matching ? state.data : null,
    access,
    loading: access === "checking" || (access === "member" && (!matching || state.loading)),
    refreshing: matching && state.refreshing,
    error: matching ? state.error : null,
    refresh,
  };
}
