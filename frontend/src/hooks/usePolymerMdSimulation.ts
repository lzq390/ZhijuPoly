import { useCallback, useEffect, useRef, useState } from "react";
import { getSessionEpoch, onSessionRetired } from "../auth/session";
import * as api from "../services/polymerMdApi";
import { clearPolymerMdPending, createPolymerMdPending, readPolymerMdPending, writePolymerMdPending, type PolymerMdPendingSubmission } from "../services/polymerMdPending";
import type { PolymerMdCapabilities, PolymerMdJob, PolymerMdJobPage, PolymerMdRequest, PolymerMdServiceStatus } from "../types/polymerMd";
import type { PolymerMdServiceCheck } from "../components/polymer-md/serviceState";
import { clearPolymerMdTrajectoryView, forgetPolymerMdJobView } from "../services/polymerMdViewState";
function errorText(error: unknown) {
  return error instanceof Error ? error.message : "读取计算任务失败，请稍后重试。";
}
const aborted = (error: unknown) => error instanceof Error && error.name === "AbortError";
function pollAfterCompletion(refresh: () => Promise<void>, intervalMs: number) {
  let active = true;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const schedule = () => {
    if (active) timer = setTimeout(run, intervalMs);
  };
  const run = () => { void refresh().then(schedule, schedule); };
  run();
  return () => { active = false; clearTimeout(timer); };
}
export function usePolymerMdSimulation(enabled: boolean, selectedId: string | null, onSelect: (id: string | null) => void) {
  const [capabilities, setCapabilities] = useState<PolymerMdCapabilities | null>(null);
  const [status, setStatus] = useState<PolymerMdServiceStatus | null>(null);
  const [history, setHistory] = useState<PolymerMdJobPage | null>(null);
  const [job, setJob] = useState<PolymerMdJob | null>(null);
  const [page, setPage] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const [serviceError, setServiceError] = useState<string | null>(null);
  const [serviceCheck, setServiceCheck] = useState<PolymerMdServiceCheck>("checking");
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [pendingSubmission, setPendingSubmission] = useState<PolymerMdPendingSubmission | null>(null);
  const [pendingIssue, setPendingIssue] = useState<string | null>(null);
  const identity = useRef(getSessionEpoch());
  const mounted = useRef(true);
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;
  const activeId = useRef(selectedId);
  activeId.current = selectedId;
  const requestControllers = useRef(new Set<AbortController>());
  const serviceController = useRef<AbortController | null>(null);
  const serviceConfirmed = useRef(false), serviceFailures = useRef(0), hasServiceSnapshot = useRef(false);
  const serviceRevision = useRef(0),
    historyRevision = useRef(0),
    jobRevision = useRef(0);
  const submitLock = useRef(false);
  const actionRevision = useRef(0), submissionRevision = useRef(0);
  const valid = useCallback(() => mounted.current && enabledRef.current && identity.current === getSessionEpoch(), []);
  const controller = useCallback(() => {
    const c = new AbortController();
    requestControllers.current.add(c);
    return c;
  }, []);
  const refreshService = useCallback(async () => {
    if (!enabled || !valid() || serviceController.current && !serviceController.current.signal.aborted) return;
    const revision = ++serviceRevision.current,
      c = controller();
    serviceController.current = c;
    let timedOut = false;
    let onAbort!: () => void;
    const interrupted = new Promise<never>((_, reject) => {
      onAbort = () => reject(new DOMException("Service check aborted", "AbortError"));
      c.signal.addEventListener("abort", onAbort, { once: true });
    });
    const timeout = setTimeout(() => { timedOut = true; c.abort(); }, 10000);
    try {
      // /status and /capabilities share one backend handler and complete wire
      // shape. One response gives admission and configuration one observation,
      // without probing the Worker twice during every refresh.
      const value = await Promise.race([api.fetchPolymerMdCapabilities(c.signal), interrupted]);
      if (!valid() || c.signal.aborted || revision !== serviceRevision.current) return;
      setCapabilities(value);
      setStatus(value);
      hasServiceSnapshot.current = true;
      serviceConfirmed.current = true;
      serviceFailures.current = 0;
      setServiceCheck("confirmed");
      setServiceError(null);
    } catch (e) {
      if (!valid() || revision !== serviceRevision.current || c.signal.aborted && !timedOut) return;
      serviceConfirmed.current = false;
      serviceFailures.current += 1;
      // Keep the last configuration available for reading, but never use it
      // as current submission authority. Repeated failures remain explicit.
      setServiceCheck(hasServiceSnapshot.current && serviceFailures.current === 1 ? "unconfirmed" : "unavailable");
      setServiceError("计算服务状态读取失败或超时，暂不能提交新计算；已有任务仍可查看，请稍后刷新。");
    } finally {
      clearTimeout(timeout);
      c.signal.removeEventListener("abort", onAbort);
      requestControllers.current.delete(c);
      if (serviceController.current === c) serviceController.current = null;
    }
  }, [controller, enabled, valid]);
  const refreshHistory = useCallback(async () => {
    if (!enabled || !valid()) return;
    const revision = ++historyRevision.current,
      c = controller();
    try {
      const result = await api.fetchPolymerMdJobs(page, c.signal);
      if (!valid() || c.signal.aborted || revision !== historyRevision.current) return;
      if (page > 1 && result.items.length === 0 && result.total <= (page - 1) * 10) {
        setPage(Math.max(1, Math.ceil(result.total / 10)));
        return;
      }
      setHistory(result);
      setHistoryError(null);
    } catch (e) {
      if (valid() && !c.signal.aborted && !aborted(e) && revision === historyRevision.current) setHistoryError(errorText(e));
    } finally {
      requestControllers.current.delete(c);
    }
  }, [controller, enabled, page, valid]);
  const refreshJob = useCallback(async () => {
    if (!enabled || !selectedId || !valid()) return;
    const id = selectedId,
      revision = ++jobRevision.current,
      c = controller();
    try {
      const result = await api.fetchPolymerMdJob(id, c.signal);
      if (!valid() || c.signal.aborted || revision !== jobRevision.current || activeId.current !== id) return;
      setJob(result);
      setError(null);
    } catch (e) {
      if (valid() && !c.signal.aborted && !aborted(e) && revision === jobRevision.current && activeId.current === id) {
        setError(errorText(e));
        if (e instanceof api.PolymerMdApiError && [403, 404].includes(e.status)) {
          forgetPolymerMdJobView(id);
          setJob(null);
        }
      }
    } finally {
      requestControllers.current.delete(c);
      if (valid() && revision === jobRevision.current) setLoading(false);
    }
  }, [controller, enabled, selectedId, valid]);
  useEffect(() => {
    mounted.current = true;
    const retire = onSessionRetired(() => {
      ++serviceRevision.current;
      ++historyRevision.current;
      ++jobRevision.current;
      ++actionRevision.current;
      ++submissionRevision.current;
      submitLock.current = false;
      for (const c of requestControllers.current) c.abort();
      requestControllers.current.clear();
      serviceController.current = null;
      serviceConfirmed.current = false;
      serviceFailures.current = 0;
      hasServiceSnapshot.current = false;
      setPendingSubmission(null);
      setPendingIssue(null);
      setJob(null);
      setHistory(null);
      setCapabilities(null);
      setStatus(null);
      setError(null);
      setServiceError(null);
      setServiceCheck("checking");
      setHistoryError(null);
      setLoading(false);
      setPage(1);
      setBusy(null);
    });
    return () => {
      mounted.current = false;
      retire();
      for (const c of requestControllers.current) c.abort();
      requestControllers.current.clear();
      serviceController.current = null;
      serviceConfirmed.current = false;
    };
  }, []);
  useEffect(() => {
    if (enabled) return;
    ++serviceRevision.current;
    ++historyRevision.current;
    ++jobRevision.current;
    ++actionRevision.current;
    ++submissionRevision.current;
    submitLock.current = false;
    for (const c of requestControllers.current) c.abort();
    requestControllers.current.clear();
    serviceController.current = null;
    serviceConfirmed.current = false;
    serviceFailures.current = 0;
    setServiceCheck("checking");
    setLoading(false);
    setBusy(null);
  }, [enabled]);
  const restorePending = useCallback(() => {
    if (!enabled || !valid()) return;
    try {
      setPendingSubmission(readPolymerMdPending());
      setPendingIssue(null);
    } catch (e) { setPendingIssue(errorText(e)); }
  }, [enabled, valid]);
  useEffect(restorePending, [restorePending]);
  useEffect(() => {
    if (!enabled) return;
    // Keep the newer completion-based polling: slow responses must not be
    // superseded merely because an interval elapsed.
    const stopService = pollAfterCompletion(refreshService, 10000);
    return stopService;
  }, [enabled, refreshService]);
  useEffect(() => {
    if (!enabled) return;
    const stopHistory = pollAfterCompletion(refreshHistory, 10000);
    return stopHistory;
  }, [enabled, refreshHistory]);
  useEffect(() => {
    ++jobRevision.current;
    setJob(null);
    setError(null);
    setLoading(false);
  }, [selectedId]);
  useEffect(() => {
    if (!enabled || !selectedId) return;
    setLoading(true);
    return pollAfterCompletion(refreshJob, 4000);
  }, [enabled, refreshJob, selectedId]);
  const refresh = useCallback(async () => {
    restorePending();
    await Promise.allSettled([refreshService(), refreshHistory(), refreshJob()]);
  }, [refreshHistory, refreshJob, refreshService, restorePending]);
  const sendPending = useCallback(async (attempt: PolymerMdPendingSubmission) => {
    const revision = ++actionRevision.current;
    const c = controller();
    setBusy("submit");
    setError(null);
    try {
      // Verify durable storage before every POST, including explicit recovery.
      writePolymerMdPending(attempt);
      const created = await api.createPolymerMdJob(attempt.request, attempt.key, c.signal);
      if (!valid() || c.signal.aborted || revision !== actionRevision.current) return;
      if (typeof created.job_id !== "string" || !created.job_id) throw new Error("未收到任务编号，请恢复原提交确认结果。");
      try {
        clearPolymerMdPending(attempt.key);
        setPendingSubmission(null);
        setPendingIssue(null);
      } catch (e) { setPendingIssue(errorText(e)); }
      onSelect(created.job_id);
      await Promise.allSettled([refreshService(), refreshHistory()]);
    } catch (e) {
      if (valid() && !c.signal.aborted && !aborted(e) && revision === actionRevision.current) {
        if (e instanceof api.PolymerMdApiError && (e.status === 409 || e.code === "idempotency_conflict")) {
          const conflict = { ...attempt, conflict: true };
          setPendingSubmission(conflict);
          try { writePolymerMdPending(conflict); } catch (storageError) { setPendingIssue(errorText(storageError)); }
        }
        setError(errorText(e));
      }
    } finally {
      requestControllers.current.delete(c);
      if (valid() && revision === actionRevision.current) setBusy(null);
    }
  }, [controller, onSelect, refreshHistory, refreshService, valid]);
  const submit = useCallback(async (request: PolymerMdRequest) => {
    if (!enabled || !valid() || busy || submitLock.current) return;
    if (!serviceConfirmed.current) { setError("计算服务状态尚未确认，请等待检查完成后提交。"); return; }
    const revision = ++submissionRevision.current;
    submitLock.current = true;
    try {
      const existing = readPolymerMdPending();
      if (existing) {
        setPendingSubmission(existing);
        setError("仍有结果未确认的提交。请恢复原提交，或先检查我的任务再放弃恢复。");
        return;
      }
      const attempt = createPolymerMdPending(request);
      setPendingSubmission(attempt);
      setPendingIssue(null);
      await sendPending(attempt);
    } catch (e) { if (valid()) setPendingIssue(errorText(e)); }
    finally { if (revision === submissionRevision.current) submitLock.current = false; }
  }, [busy, enabled, sendPending, valid]);
  const recoverSubmission = useCallback(async () => {
    if (!enabled || !valid() || busy || submitLock.current) return;
    if (!serviceConfirmed.current) { setError("计算服务状态尚未确认，请等待检查完成后恢复提交。"); return; }
    const revision = ++submissionRevision.current;
    submitLock.current = true;
    try {
      const attempt = readPolymerMdPending();
      setPendingSubmission(attempt);
      if (!attempt) { setError("未找到属于当前登录会话的待确认提交。"); return; }
      if (attempt.conflict) { setError("原提交标识发生冲突，请检查我的任务后放弃恢复；不会自动改用新标识。"); return; }
      setPendingIssue(null);
      await sendPending(attempt);
    } catch (e) { if (valid()) setPendingIssue(errorText(e)); }
    finally { if (revision === submissionRevision.current) submitLock.current = false; }
  }, [busy, enabled, sendPending, valid]);
  const discardSubmission = useCallback(() => {
    if (!enabled || !valid() || busy || submitLock.current) return;
    try {
      clearPolymerMdPending();
      setPendingSubmission(null);
      setPendingIssue(null);
      setError(null);
    } catch (e) { setPendingIssue(errorText(e)); }
  }, [busy, enabled, valid]);
  const mutate = useCallback(async (kind: "cancel" | "clear" | "delete", id: string) => {
    if (!enabled || !valid() || busy) return;
    const revision = ++actionRevision.current;
    const c = controller();
    setBusy(`${kind}:${id}`);
    setError(null);
    try {
      if (kind === "cancel") await api.cancelPolymerMdJob(id, c.signal);else if (kind === "clear") await api.clearPolymerMdArtifacts(id, c.signal);else await api.deletePolymerMdJob(id, c.signal);
      if (!valid() || c.signal.aborted || revision !== actionRevision.current) return;
      if (kind === "delete") forgetPolymerMdJobView(id);
      else if (kind === "clear") clearPolymerMdTrajectoryView(id, getSessionEpoch());
      if (kind === "delete" && activeId.current === id) {
        setJob(null);
        onSelect(null);
      } else if (activeId.current === id) await refreshJob();
      await Promise.allSettled([refreshHistory(), refreshService()]);
    } catch (e) {
      if (valid() && !c.signal.aborted && !aborted(e) && revision === actionRevision.current) setError(errorText(e));
    } finally {
      requestControllers.current.delete(c);
      if (valid() && revision === actionRevision.current) setBusy(null);
    }
  }, [busy, controller, enabled, onSelect, refreshHistory, refreshJob, refreshService, valid]);
  return {
    capabilities,
    status,
    history,
    job,
    page,
    setPage,
    error,
    serviceError,
    serviceCheck,
    historyError,
    busy,
    loading,
    pendingSubmission,
    pendingIssue,
    recoverSubmission,
    discardSubmission,
    submit,
    mutate,
    refresh
  };
}
