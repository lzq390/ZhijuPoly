import { useCallback, useEffect, useRef, useState } from "react";
import { fetchBatchJob, fetchBatchJobs } from "../services/polymerizationBatchApi";
import type { BatchJob } from "../types/polymerizationBatch";

export const BATCH_HISTORY_KEY = "nexpoly:polymerization-batch:history";
export const isBatchActive = (job: BatchJob) => ["queued", "running", "cancelling"].includes(job.status);
function withFileExpiry(job: BatchJob): BatchJob {
  return job.status === "expired" || (!isBatchActive(job) && job.expires_at && Date.parse(job.expires_at) <= Date.now())
    ? { ...job, status: "expired", artifacts: {} }
    : job;
}
function currentJobId(): string | null {
  const id = new URLSearchParams(window.location.search).get("job_id");
  return id && /^[0-9a-f]{32}$/.test(id) ? id : null;
}
export function usePolymerizationBatchJob(enabled = true) {
  const [jobId, setJobId] = useState(() => enabled ? currentJobId() : null);
  const [job, setJob] = useState<BatchJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [historyJobs, setHistoryJobs] = useState<BatchJob[]>([]);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [historyNext, setHistoryNext] = useState<number | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [historyRevision, setHistoryRevision] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setHistoryLoading(true); setHistoryError(null);
    void fetchBatchJobs(historyOffset, controller.signal).then(page => {
      if (controller.signal.aborted) return;
      setHistoryJobs(page.items.map(withFileExpiry)); setHistoryTotal(page.total); setHistoryNext(page.next_offset);
      if (!currentId.current && page.items[0]) changeJob(page.items[0].job_id);
    }).catch(reason => {
      if (!controller.signal.aborted) setHistoryError(reason instanceof Error ? reason.message : "无法读取我的任务。");
    }).finally(() => { if (!controller.signal.aborted) setHistoryLoading(false); });
    return () => controller.abort();
  }, [enabled, historyOffset, historyRevision]);
  const [revision, setRevision] = useState(0);
  const [settledRevision, setSettledRevision] = useState(-1);
  const currentId = useRef(jobId);
  const request = useRef<AbortController | null>(null);
  const expiredJobs = useRef(new Set<string>());
  const changeJob = useCallback((id: string | null) => {
    currentId.current = id;
    request.current?.abort();
    setJobId(id);
    setJob(null);
    setError(null);
    setRevision((value) => value + 1);
  }, []);
  const selectJob = useCallback((id: string) => {
    if (!/^[0-9a-f]{32}$/.test(id)) return;
    changeJob(id);
    setHistoryRevision(value => value + 1);
    const url = new URL(window.location.href);
    // A submission may finish after the user has switched to the single tab.
    if (url.searchParams.get("mode") !== "single") url.searchParams.set("mode", "batch");
    url.searchParams.set("job_id", id);
    window.history.replaceState(window.history.state, "", url);
  }, [changeJob]);
  useEffect(() => {
    if (!enabled) return;
    const changed = () => {
      const id = currentJobId();
      if (id !== currentId.current) changeJob(id);
    };
    window.addEventListener("popstate", changed);
    return () => window.removeEventListener("popstate", changed);
  }, [changeJob, enabled]);
  useEffect(() => {
    if (!enabled || !jobId) return;
    const controller = new AbortController();
    request.current = controller;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const next = await fetchBatchJob(jobId, controller.signal);
        if (controller.signal.aborted || currentId.current !== jobId) return;
        if (next.job_id !== jobId) throw new Error("返回的任务标识不一致，请重新刷新。");
        setJob(withFileExpiry(expiredJobs.current.has(jobId) ? { ...next, status: "expired" } : next));
        setError(null);
        if (isBatchActive(next)) timer = setTimeout(() => void load(), 2000);
      } catch (reason) {
        if (controller.signal.aborted || currentId.current !== jobId) return;
        setError(reason instanceof Error ? reason.message : "任务状态暂时无法读取。");
        timer = setTimeout(() => void load(), 5000);
      } finally {
        if (!controller.signal.aborted && currentId.current === jobId) setSettledRevision(revision);
      }
    };
    void load();
    return () => {
      controller.abort();
      clearTimeout(timer);
      if (request.current === controller) request.current = null;
    };
  }, [enabled, jobId, revision]);
  // Completed tasks stop polling, but their download deadline still applies.
  useEffect(() => {
    if (!job || job.job_id !== jobId || isBatchActive(job) || job.status === "expired" || !job.expires_at) return;
    const deadline = Date.parse(job.expires_at);
    if (!Number.isFinite(deadline)) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const expire = () => {
      const remaining = deadline - Date.now();
      if (remaining > 0) timer = setTimeout(expire, Math.min(remaining, 2 ** 31 - 1));
      else setJob((current) => current?.job_id === jobId ? withFileExpiry(current) : current);
    };
    expire();
    return () => clearTimeout(timer);
  }, [jobId, job?.job_id, job?.status, job?.expires_at]);
  const isCurrentJob = useCallback((id: string) => currentId.current === id, []);
  const markFilesExpired = useCallback((id: string) => {
    if (currentId.current !== id) return;
    expiredJobs.current.add(id);
    setJob((current) => current?.job_id === id ? { ...current, status: "expired", artifacts: {} } : current);
  }, []);
  const refresh = useCallback(() => {
    request.current?.abort();
    setRevision((value) => value + 1);
    setHistoryRevision(value => value + 1);
  }, []);
  return {
    job: job?.job_id === jobId ? job : null, jobId, error, history: historyJobs.map(item => item.job_id), historyJobs, historyTotal, historyOffset, historyNext, historyLoading, historyError, setHistoryOffset, refreshHistory: () => setHistoryRevision(value => value + 1), selectJob, isCurrentJob, markFilesExpired,
    refreshing: Boolean(jobId) && revision !== settledRevision, refresh
  };
}
