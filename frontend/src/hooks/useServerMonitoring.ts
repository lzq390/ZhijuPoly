import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useAuth } from "../auth/AuthProvider";
import { getSession, getSessionEpoch, onSessionRetired, sessionIsReady } from "../auth/session";
import { fetchMonitoringAccess, fetchMonitoringHistory, fetchMonitoringSnapshot, isMonitoringAccessError, MonitoringRequestError } from "../services/serverMonitoring";
import type { MonitoringHistory, MonitoringSnapshot } from "../types/serverMonitoring";

export type MonitoringAccess = "disabled" | "guest" | "checking" | "allowed" | "denied" | "unavailable";
type State = {
  key: string;
  access: MonitoringAccess;
  snapshot: MonitoringSnapshot | null;
  history: MonitoringHistory | null;
  snapshotError: string | null;
  historyError: string | null;
  receivedAt: number | null;
  historyReceivedAt: number | null;
  refreshing: boolean;
};
const empty = (key: string, access: MonitoringAccess): State => ({ key, access, snapshot: null, history: null,
  snapshotError: null, historyError: null, receivedAt: null, historyReceivedAt: null, refreshing: false });
export const isServerMonitoringEnabled = () => import.meta.env.VITE_SERVER_MONITORING_ENABLED === "true";

/** One access probe per identity; cached metrics are fetched only while this page is visible. */
export function useServerMonitoring(active: boolean) {
  const auth = useAuth();
  const epoch = useSyncExternalStore(onSessionRetired, getSessionEpoch, getSessionEpoch);
  const session = auth?.session ?? getSession();
  const enabled = isServerMonitoringEnabled();
  const ready = sessionIsReady() && (!auth || auth.status === "authenticated" || auth.status === "guest");
  const signedIn = ready && session.authenticated && Boolean(session.session_id);
  const key = `${epoch}:${session.user?.id ?? ""}:${session.session_id ?? ""}`;
  const baseAccess: MonitoringAccess = !enabled ? "disabled" : !ready ? "checking" : !signedIn ? "guest" : "checking";
  const [state, setState] = useState<State>(() => empty(key, baseAccess));
  const [accessAttempt, setAccessAttempt] = useState(0);
  const refreshMetrics = useRef<(() => void) | null>(null);
  const access = state.key === key && enabled && signedIn ? state.access : baseAccess;

  useEffect(() => {
    setState(empty(key, baseAccess));
    if (!enabled || !signedIn) return;
    const controller = new AbortController();
    const current = () => !controller.signal.aborted && getSessionEpoch() === epoch && sessionIsReady();
    void fetchMonitoringAccess(controller.signal).then(allowed => {
      if (current()) setState(empty(key, allowed ? "allowed" : "denied"));
    }).catch(error => {
      if (current()) setState(empty(key, error instanceof MonitoringRequestError && error.status === 403 ? "denied" : "unavailable"));
    });
    return () => controller.abort();
  }, [enabled, signedIn, ready, key, epoch, accessAttempt]);

  useEffect(() => {
    if (!enabled || !active || access !== "allowed" || !signedIn) return;
    let disposed = false;
    let blocked = false;
    const running = new Map<"snapshot" | "history", AbortController>();
    const timers = new Map<"snapshot" | "history", ReturnType<typeof setTimeout>>();
    let latestSnapshot = state.snapshot;
    let pendingHistory = state.history ? { value: state.history, receivedAt: state.historyReceivedAt } : null;
    let refreshHistoryAfterFlight = false;
    let historyGeneration = 0;
    const historyMatches = (history: MonitoringHistory | null | undefined, snapshot: MonitoringSnapshot | null): boolean => Boolean(history && snapshot &&
      history.collector_id === snapshot.collector_id && history.node_id === snapshot.node_id && history.started_at === snapshot.started_at &&
      Date.parse(history.captured_at) <= Date.parse(snapshot.captured_at));
    const current = () => !disposed && !blocked && epoch === getSessionEpoch() && sessionIsReady();
    const visible = () => document.visibilityState !== "hidden";
    const stop = () => {
      timers.forEach(clearTimeout); timers.clear();
      running.forEach(controller => controller.abort()); running.clear();
    };
    const load = async (kind: "snapshot" | "history") => {
      if (!current() || !visible() || running.has(kind)) return;
      clearTimeout(timers.get(kind));
      if (kind === "history") refreshHistoryAfterFlight = false;
      const requestHistoryGeneration = historyGeneration;
      const controller = new AbortController(); running.set(kind, controller);
      setState(previous => previous.key === key ? { ...previous, refreshing: true } : previous);
      try {
        const result = kind === "snapshot" ? await fetchMonitoringSnapshot(controller.signal) : await fetchMonitoringHistory(controller.signal);
        if (!current() || controller.signal.aborted) return;
        if (kind === "history" && requestHistoryGeneration !== historyGeneration) return;
        const receivedAt = Date.now();
        let resetHistory = false;
        let clockReset = false;
        if (kind === "snapshot") {
          const snapshot = result as MonitoringSnapshot;
          if (latestSnapshot?.collector_id === snapshot.collector_id && snapshot.sequence < latestSnapshot.sequence) return;
          clockReset = Boolean(latestSnapshot && latestSnapshot.collector_id === snapshot.collector_id && Date.parse(snapshot.captured_at) < Date.parse(latestSnapshot.captured_at));
          resetHistory = Boolean(latestSnapshot && (latestSnapshot.collector_id !== snapshot.collector_id || latestSnapshot.node_id !== snapshot.node_id ||
            latestSnapshot.started_at !== snapshot.started_at || Date.parse(snapshot.captured_at) < Date.parse(latestSnapshot.captured_at)));
          // A small clock rollback can leave old history timestamps below the new
          // snapshot. Retire that entire history generation, including in-flight reads.
          if (clockReset) { historyGeneration++; pendingHistory = null; }
          // Snapshot is authoritative. A restarted collector's wall clock can be earlier.
          latestSnapshot = snapshot;
        } else {
          pendingHistory = { value: result as MonitoringHistory, receivedAt };
        }
        const acceptedHistory = historyMatches(pendingHistory?.value, latestSnapshot) ? pendingHistory : null;
        if (resetHistory && !acceptedHistory) pendingHistory = null;
        setState(previous => {
          if (previous.key !== key || previous.access !== "allowed") return previous;
          const keepHistory = !resetHistory && historyMatches(previous.history, latestSnapshot);
          const next = { ...previous,
            history: acceptedHistory?.value ?? (keepHistory ? previous.history : null),
            historyReceivedAt: acceptedHistory?.receivedAt ?? (keepHistory ? previous.historyReceivedAt : null),
            historyError: acceptedHistory && (kind === "history" || acceptedHistory.value !== previous.history) ? null : previous.historyError };
          return kind === "snapshot"
            ? { ...next, snapshot: result as MonitoringSnapshot, snapshotError: null, receivedAt }
            : next;
        });
        if (resetHistory && !acceptedHistory) {
          // An old request may already be reading the previous atomic history file.
          if (running.has("history")) refreshHistoryAfterFlight = true;
          else void load("history");
        }
      } catch (error) {
        if (!current() || controller.signal.aborted) return;
        if (isMonitoringAccessError(error)) {
          blocked = true;
          stop();
          setState(empty(key, error instanceof MonitoringRequestError && error.status === 403 ? "denied" : "unavailable"));
        } else {
          setState(previous => previous.key !== key ? previous : { ...previous,
            [kind === "snapshot" ? "snapshotError" : "historyError"]: kind === "snapshot" ? "最新采样暂时不可用，正在重试。" : "历史数据暂时不可用，正在重试。" });
        }
      } finally {
        if (running.get(kind) === controller) running.delete(kind);
        if (current() && !controller.signal.aborted) {
          setState(previous => previous.key === key ? { ...previous, refreshing: running.size > 0 } : previous);
          if (visible()) timers.set(kind, setTimeout(() => { void load(kind); },
            kind === "snapshot" ? 5_000 : refreshHistoryAfterFlight ? 0 : 30_000));
        }
      }
    };
    const refresh = () => { void load("snapshot"); void load("history"); };
    const visibility = () => {
      if (visible()) refresh();
      else { stop(); setState(previous => previous.key === key ? { ...previous, refreshing: false } : previous); }
    };
    refreshMetrics.current = refresh;
    document.addEventListener("visibilitychange", visibility);
    const retire = onSessionRetired(() => { blocked = true; stop(); setState(empty(key, "checking")); });
    refresh();
    return () => {
      disposed = true; stop(); retire();
      if (refreshMetrics.current === refresh) refreshMetrics.current = null;
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [enabled, active, access, signedIn, key, epoch]);

  const refresh = useCallback(() => {
    if (refreshMetrics.current) refreshMetrics.current();
    else setAccessAttempt(value => value + 1);
  }, []);
  const matching = state.key === key && enabled && signedIn;
  return { ...(matching ? state : empty(key, baseAccess)), access, refresh, login: auth?.login };
}
export type ServerMonitoringControl = ReturnType<typeof useServerMonitoring>;
