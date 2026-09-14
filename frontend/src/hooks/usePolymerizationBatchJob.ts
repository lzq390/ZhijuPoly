import { useCallback, useEffect, useRef, useState } from "react";
import { fetchBatchJob } from "../services/polymerizationBatchApi";
import type { BatchJob } from "../types/polymerizationBatch";

export const BATCH_HISTORY_KEY = "nexpoly:polymerization-batch:history";
export const isBatchActive = (job: BatchJob) => ["queued", "running", "cancelling"].includes(job.status);
function rememberJob(id: string): string[] {
  const ids = [id, ...recentJobs().filter((item) => item !== id)].slice(0, 20);
  try { localStorage.setItem(BATCH_HISTORY_KEY, JSON.stringify(ids)); } catch { /* History is optional. */ }
  return ids;
}
function withFileExpiry(job: BatchJob): BatchJob {
  return job.status === "expired" || (!isBatchActive(job) && job.expires_at && Date.parse(job.expires_at) <= Date.now())
    ? { ...job, status: "expired", artifacts: {} }
    : job;
}
function recentJobs(): string[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(BATCH_HISTORY_KEY) ?? "[]");
    return Array.isArray(value) ? value.filter((id): id is string => typeof id === "string" && /^[0-9a-f]{32}$/.test(id)).slice(0, 20) : [];
  } catch { return []; }
}
function currentJobId(): string | null {
  const id = new URLSearchParams(window.location.search).get("job_id");
  return id && /^[0-9a-f]{32}$/.test(id) ? id : recentJobs()[0] ?? null;
}
export function usePolymerizationBatchJob() {
  const [jobId, setJobId] = useState(currentJobId);
  const [job, setJob] = useState<BatchJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState(recentJobs);
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
    setHistory(rememberJob(id));
    const url = new URL(window.location.href);
    // A submission may finish after the user has switched to the single tab.
    if (url.searchParams.get("mode") !== "single") url.searchParams.set("mode", "batch");
    url.searchParams.set("job_id", id);
    window.history.replaceState(window.history.state, "", url);
  }, [changeJob]);
  useEffect(() => {
    const changed = () => {
      const id = currentJobId();
      if (id !== currentId.current) changeJob(id);
    };
    window.addEventListener("popstate", changed);
    return () => window.removeEventListener("popstate", changed);
  }, [changeJob]);
  useEffect(() => {
    if (!jobId) return;
    const controller = new AbortController();
    request.current = controller;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let remembered = false;
    const load = async () => {
      try {
        const next = await fetchBatchJob(jobId, controller.signal);
        if (controller.signal.aborted || currentId.current !== jobId) return;
        if (next.job_id !== jobId) throw new Error("返回的任务标识不一致，请重新刷新。");
        setJob(withFileExpiry(expiredJobs.current.has(jobId) ? { ...next, status: "expired" } : next));
        setError(null);
        if (!remembered) {
          setHistory(rememberJob(jobId));
          remembered = true;
        }
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
  }, [jobId, revision]);
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
  }, []);
  return {
    job: job?.job_id === jobId ? job : null, jobId, error, history, selectJob, isCurrentJob, markFilesExpired,
    refreshing: Boolean(jobId) && revision !== settledRevision, refresh
  };
}
