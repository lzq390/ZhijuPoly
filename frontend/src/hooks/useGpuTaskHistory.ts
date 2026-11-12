import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useAuth } from "../auth/AuthProvider";
import { getSession, getSessionEpoch, onSessionRetired, sessionIsReady } from "../auth/session";
import { fetchGpuTaskHistory } from "../services/gpuTaskHistory";
import { isMonitoringAccessError, MonitoringRequestError } from "../services/serverMonitoring";
import type { GpuTaskHistoryResponse } from "../types/gpuTaskHistory";
import type { MonitoringFreshness } from "../types/serverMonitoring";
import { isServerMonitoringEnabled } from "./useServerMonitoring";

type State = { key: string; data: GpuTaskHistoryResponse | null; receivedAt: number | null; error: string | null; loading: boolean;
  accessError: boolean; activityAvailable: boolean; historyAvailable: boolean };
const empty = (key: string, error: string | null = null, accessError = false): State => ({ key, data: null, receivedAt: null, error, loading: false,
  accessError, activityAvailable: false, historyAvailable: false });
const completeHistory = (data: GpuTaskHistoryResponse) => data.status === "ready"
  && new Set(data.sources.map(source => source.source_id)).size === 6 && data.sources.every(source => source.status === "ready");

/** One all-GPU cache per mounted view. Selecting a card never changes the request lifecycle. */
export function useGpuTaskHistory() {
  const auth = useAuth();
  const epoch = useSyncExternalStore(onSessionRetired, getSessionEpoch, getSessionEpoch);
  const session = auth?.session ?? getSession();
  const ready = sessionIsReady() && (!auth || auth.status === "authenticated" || auth.status === "guest");
  const active = isServerMonitoringEnabled() && ready && session.authenticated && Boolean(session.user?.id && session.session_id);
  const key = `${epoch}:${session.user?.id ?? ""}:${session.session_id ?? ""}`;
  const [state, setState] = useState<State>(() => empty(key));
  const [now, setNow] = useState(Date.now);
  const refreshRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    setState(empty(key));
    if (!active) return;
    let disposed = false, blocked = false;
    let running: AbortController | null = null;
    let poll: ReturnType<typeof setTimeout> | undefined;
    let clock: ReturnType<typeof setInterval> | undefined;
    let inventoryKey: string | null = null;
    let lastActivity: GpuTaskHistoryResponse["activity"] | null = null;
    let lastHistory: Pick<GpuTaskHistoryResponse, "tasks" | "history_captured_at"> | null = null;
    const clearFallback = () => { inventoryKey = null; lastActivity = null; lastHistory = null; };
    const identityCurrent = () => {
      const installed = getSession();
      return !disposed && isServerMonitoringEnabled() && getSessionEpoch() === epoch && sessionIsReady() && installed.authenticated
        && installed.user?.id === session.user?.id && installed.session_id === session.session_id;
    };
    const current = () => !blocked && identityCurrent();
    const visible = () => document.visibilityState !== "hidden";
    const stop = () => { clearTimeout(poll); clearInterval(clock); running?.abort(); running = null; };
    const load = async () => {
      if (!current() || !visible() || running) return;
      clearTimeout(poll);
      const controller = new AbortController(); running = controller;
      setState(previous => previous.key === key ? { ...previous, loading: true } : previous);
      try {
        const incoming = await fetchGpuTaskHistory(controller.signal);
        if (!current() || controller.signal.aborted) return;
        const nextInventory = JSON.stringify([incoming.node_id, [...incoming.gpu_uuids].sort()]);
        if (inventoryKey !== nextInventory) clearFallback();
        inventoryKey = nextInventory;
        // Keep independent last complete results, including confirmed empty
        // lists. Device changes and clock rollback cannot reuse unrelated or
        // future observations; a collector restart on the same host can.
        const captured = Date.parse(incoming.captured_at);
        if (lastActivity && Date.parse(lastActivity.captured_at) > captured) lastActivity = null;
        if (lastHistory && Date.parse(lastHistory.history_captured_at) > captured) lastHistory = null;
        const activityComplete = incoming.activity.status === "ready";
        const historyComplete = completeHistory(incoming);
        if (activityComplete) lastActivity = incoming.activity;
        if (historyComplete) lastHistory = { tasks: incoming.tasks, history_captured_at: incoming.history_captured_at };
        const data = { ...incoming,
          ...(!historyComplete && lastHistory ? lastHistory : {}),
          activity: !activityComplete && lastActivity
            ? { ...incoming.activity, items: lastActivity.items, captured_at: lastActivity.captured_at }
            : incoming.activity,
        };
        const receivedAt = Date.now(); setNow(receivedAt);
        setState({ key, data, receivedAt, error: null, loading: false, accessError: false,
          activityAvailable: Boolean(lastActivity) || incoming.activity.items.length > 0,
          historyAvailable: Boolean(lastHistory) || incoming.tasks.length > 0 });
      } catch (error) {
        if (!current() || controller.signal.aborted) return;
        if (isMonitoringAccessError(error)) {
          blocked = true; stop(); clearFallback();
          setState(empty(key, error instanceof MonitoringRequestError ? error.message : "暂时无法确认访问权限。", true));
        } else {
          setState(previous => previous.key === key ? { ...previous, loading: false, error: "GPU 任务暂时读取失败，正在重试。" } : previous);
        }
      } finally {
        if (running === controller) {
          running = null;
          if (current() && !controller.signal.aborted && visible()) poll = setTimeout(() => { void load(); }, 5_000);
        }
      }
    };
    const resume = () => { clearInterval(clock); setNow(Date.now()); clock = setInterval(() => setNow(Date.now()), 1000); void load(); };
    const visibility = () => {
      if (visible()) { if (current()) resume(); }
      else { stop(); setState(previous => previous.key === key ? { ...previous, loading: false } : previous); }
    };
    refreshRef.current = () => {
      // An explicit retry revalidates the current identity at the API. Retired
      // identities and hidden views cannot revive this request lifecycle.
      if (!identityCurrent() || !visible()) return;
      blocked = false;
      resume();
    };
    const retire = onSessionRetired(() => { blocked = true; stop(); clearFallback(); setState(empty(key)); });
    document.addEventListener("visibilitychange", visibility);
    if (visible()) resume();
    return () => { disposed = true; stop(); retire(); refreshRef.current = null; document.removeEventListener("visibilitychange", visibility); };
  }, [active, key, epoch]);

  const matching = active && state.key === key;
  const result = matching ? state : empty(key);
  const age = result.data ? result.data.age_seconds + Math.max(0, now - (result.receivedAt ?? now)) / 1000 : null;
  const freshness: MonitoringFreshness = !result.data || result.data.freshness === "unavailable" || (age ?? 0) > 60 ? "unavailable"
    : result.data.freshness === "stale" || Boolean(result.error) || (age ?? 0) > 15 ? "stale" : "fresh";
  // A fresh process sample must not rejuvenate an older history scan.
  const sourceAge = (capturedAt: string | undefined) => result.data && capturedAt
    ? (age ?? 0) + Math.max(0, Date.parse(result.data.captured_at) - Date.parse(capturedAt)) / 1000 : null;
  const activityAge = sourceAge(result.data?.activity.captured_at);
  const historyAge = sourceAge(result.data?.history_captured_at);
  const activityFreshness: MonitoringFreshness = activityAge === null || activityAge > 60 || freshness === "unavailable" ? "unavailable"
    : activityAge > 15 || freshness === "stale" ? "stale" : "fresh";
  const historyFreshness: MonitoringFreshness = historyAge === null || historyAge > 180 ? "unavailable"
    : historyAge > 90 || Boolean(result.error) ? "stale" : "fresh";
  const refresh = useCallback(() => {
    const installed = getSession();
    if (!active || !isServerMonitoringEnabled() || !sessionIsReady() || getSessionEpoch() !== epoch || !installed.authenticated
      || installed.user?.id !== session.user?.id || installed.session_id !== session.session_id) return;
    refreshRef.current?.();
  }, [active, key, epoch]);
  const activityCached = !result.data || result.data.activity.status !== "ready" || activityFreshness !== "fresh" || Boolean(result.error);
  const historyCached = !result.data || !completeHistory(result.data) || historyFreshness !== "fresh" || Boolean(result.error);
  return { ...result, active, age, freshness, activityAge, activityFreshness, historyAge, historyFreshness, activityCached, historyCached, refresh };
}
export type GpuTaskHistoryControl = ReturnType<typeof useGpuTaskHistory>;
