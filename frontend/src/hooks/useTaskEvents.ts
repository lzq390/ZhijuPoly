import { useCallback, useEffect, useRef, useState } from "react";
import { onSessionRetired, privateFetch } from "../auth/session";
import { parseSseBlock } from "../services/sse";

export type TaskEventConnection = "connecting" | "live" | "reconnecting" | "unavailable";
type Task = {
  job_id: string; status: string; queue_position?: number | null;
  attempt?: number; artifacts_state?: string; artifacts_deleted?: boolean;
  artifact_deleted_at?: string | null;
};
type Change = { ids: string[]; resync: boolean };
const BASE = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

export function taskSignature(module: "md" | "dft", job: Task): string {
  return JSON.stringify(module === "md"
    ? [job.status, job.queue_position ?? null, job.artifacts_deleted ?? Boolean(job.artifact_deleted_at)]
    : [job.status, job.queue_position ?? null, job.attempt ?? 1, job.artifacts_state ?? "none"]);
}

/** One subscription per mounted module. Timers reconnect the stream or coalesce
 * actual changes; they never periodically fetch task lists or details. */
export function useTaskEvents(module: "md" | "dft", enabled: boolean, onChange: (change: Change) => void | Promise<unknown>) {
  const [connectionState, setConnectionState] = useState<TaskEventConnection>("connecting");
  const [generation, setGeneration] = useState(0);
  const callback = useRef(onChange);
  callback.current = onChange;
  const reconnect = useCallback(() => setGeneration(value => value + 1), []);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let changeTimer: ReturnType<typeof setTimeout> | undefined;
    // Only notifications advance this baseline. A REST read updates just one
    // view and cannot acknowledge that all dependent views have synchronized.
    let known = new Map<string, string>();
    let needsResync = true;
    let failures = 0;
    let pendingIds = new Set<string>();
    let pendingResync = false;
    const stop = () => {
      controller.abort();
      clearTimeout(retryTimer);
      clearTimeout(changeTimer);
    };
    const retire = onSessionRetired(() => { stop(); known.clear(); });
    const notify = (ids: string[], resync: boolean) => {
      ids.forEach(id => pendingIds.add(id));
      pendingResync ||= resync;
      if (changeTimer != null) return;
      changeTimer = setTimeout(() => {
        changeTimer = undefined;
        const change = { ids: [...pendingIds], resync: pendingResync };
        pendingIds = new Set(); pendingResync = false;
        if (!controller.signal.aborted) void Promise.resolve(callback.current(change)).catch(() => {});
      }, 100);
    };
    const consume = (block: string) => {
      if (controller.signal.aborted) return;
      const event = parseSseBlock(block);
      if (!event) return; // SSE comments are connection heartbeats only.
      if (event.event === "unavailable") {
        // The stream can survive a database outage. Treat its next valid
        // snapshot like a reconnect, even if the task signatures are unchanged.
        needsResync = true;
        setConnectionState("unavailable");
        return;
      }
      if (event.event !== "snapshot" || event.data.module !== module || !Array.isArray(event.data.jobs)) return;
      const changed: string[] = [];
      const next = new Map<string, string>();
      for (const row of event.data.jobs) {
        if (!row || typeof row.job_id !== "string" || typeof row.status !== "string") throw new Error("Invalid task notification");
        const signature = taskSignature(module, row);
        next.set(row.job_id, signature);
        if (known.get(row.job_id) !== signature) changed.push(row.job_id);
      }
      for (const id of known.keys()) {
        if (!next.has(id)) changed.push(id);
      }
      known = next;
      setConnectionState("live");
      // Initial synchronization closes the fetch/subscription race. Reconnects
      // and availability recovery also reconcile failed or missed reads once.
      if (needsResync || changed.length) notify(changed, needsResync);
      needsResync = false;
    };
    const connect = async () => {
      let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
      try {
        setConnectionState(failures ? "reconnecting" : "connecting");
        const response = await privateFetch(`${BASE}/task-events?module=${module}`, {
          headers: { Accept: "text/event-stream" }, signal: controller.signal
        });
        if (!response.ok) {
          await response.text();
          if (response.status >= 400 && response.status < 500) {
            setConnectionState("unavailable"); return;
          }
          throw new Error("Task notification unavailable");
        }
        if (!response.headers.get("content-type")?.includes("text/event-stream") || !response.body) {
          await response.text(); setConnectionState("unavailable"); return;
        }
        reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        needsResync = true;
        while (!controller.signal.aborted) {
          const { done, value } = await reader.read();
          if (done) throw new Error("Task notification disconnected");
          buffer += decoder.decode(value, { stream: true });
          buffer = buffer.replace(/\r\n/g, "\n");
          if (buffer.length > 4 * 1024 * 1024) throw new Error("Task notification exceeds size limit");
          let end: number;
          while ((end = buffer.indexOf("\n\n")) >= 0) {
            consume(buffer.slice(0, end)); buffer = buffer.slice(end + 2);
            failures = 0;
          }
        }
      } catch {
        if (!controller.signal.aborted) {
          setConnectionState("reconnecting");
          const delay = Math.min(30000, 1000 * 2 ** Math.min(failures++, 5));
          retryTimer = setTimeout(() => { void connect(); }, delay);
        }
      } finally {
        if (reader) {
          await reader.cancel().catch(() => {});
          reader.releaseLock();
        }
      }
    };
    void connect();
    return () => { stop(); retire(); };
  }, [enabled, generation, module]);
  return { connectionState, reconnect };
}
