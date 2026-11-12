// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { GUEST_SESSION, installSession, retireSession } from "../auth/session";
import { gpuActivityItem, gpuHistoricalTask, gpuTaskHistory } from "../test/gpuTaskHistoryFixture";
import { useGpuTaskHistory } from "./useGpuTaskHistory";

let fetcher: ReturnType<typeof vi.fn>;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const flush = async () => { await act(async () => { await Promise.resolve(); await Promise.resolve(); }); };
const advance = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
beforeEach(() => {
  vi.stubEnv("VITE_SERVER_MONITORING_ENABLED", "true"); vi.useFakeTimers(); vi.setSystemTime(new Date("2026-11-11T01:00:00Z"));
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  fetcher = vi.fn(async () => json(gpuTaskHistory())); vi.stubGlobal("fetch", fetcher);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

it("does not fetch without the feature or an authenticated identity", async () => {
  vi.stubEnv("VITE_SERVER_MONITORING_ENABLED", "false");
  const first = renderHook(() => useGpuTaskHistory()); await flush(); expect(fetcher).not.toHaveBeenCalled(); first.unmount();
  vi.stubEnv("VITE_SERVER_MONITORING_ENABLED", "true"); retireSession(); installSession(GUEST_SESSION);
  const second = renderHook(() => useGpuTaskHistory()); await flush();
  expect(second.result.current.data).toBeNull(); expect(fetcher).not.toHaveBeenCalled();
});

it("waits for completion before its next 5-second all-GPU poll", async () => {
  let finish: (response: Response) => void = () => {};
  fetcher.mockImplementationOnce(() => new Promise<Response>(resolve => { finish = resolve; }));
  const { result } = renderHook(() => useGpuTaskHistory()); await flush(); await advance(2000);
  expect(fetcher).toHaveBeenCalledTimes(1);
  await act(async () => { finish(json(gpuTaskHistory())); });
  expect(result.current.data?.gpu_uuids).toEqual(["gpu-first", "gpu-second"]);
  await advance(4_999); expect(fetcher).toHaveBeenCalledTimes(1);
  await advance(1); expect(fetcher).toHaveBeenCalledTimes(2);
  expect(fetcher.mock.calls.every(call => String(call[0]).endsWith("/server-monitoring/gpu-tasks"))).toBe(true);
});

it("aborts hidden reads, resumes immediately, and ignores the abandoned response", async () => {
  let finishOld: (response: Response) => void = () => {};
  fetcher.mockImplementationOnce(() => new Promise<Response>(resolve => { finishOld = resolve; }));
  const { result } = renderHook(() => useGpuTaskHistory()); await flush();
  const signal = fetcher.mock.calls[0][1].signal as AbortSignal;
  await act(async () => { Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" }); document.dispatchEvent(new Event("visibilitychange")); });
  expect(signal.aborted).toBe(true); await advance(120_000); expect(fetcher).toHaveBeenCalledTimes(1);
  await act(async () => { Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" }); document.dispatchEvent(new Event("visibilitychange")); });
  expect(fetcher).toHaveBeenCalledTimes(2); expect(result.current.data?.collector_id).toBe("task-collector-1");
  await act(async () => { finishOld(json(gpuTaskHistory({ collector_id: "old-hidden" }))); });
  expect(result.current.data?.collector_id).toBe("task-collector-1");
  await advance(5_000); expect(fetcher).toHaveBeenCalledTimes(3);
});

it("retains cached records after timeout and ages activity and history on their own clocks", async () => {
  fetcher.mockResolvedValueOnce(json(gpuTaskHistory({ age_seconds: 10, history_captured_at: "2026-11-11T00:58:45Z" })));
  const { result } = renderHook(() => useGpuTaskHistory()); await flush();
  fetcher.mockImplementation((_input, init) => new Promise((_resolve, reject) => {
    init.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
  }));
  expect(result.current.activityFreshness).toBe("fresh"); expect(result.current.historyFreshness).toBe("fresh");
  await advance(6000); expect(result.current.activityFreshness).toBe("stale"); expect(result.current.historyFreshness).toBe("stale");
  await advance(10_000); expect(result.current.error).toContain("读取失败"); expect(result.current.data).not.toBeNull(); expect(result.current.loading).toBe(false);
  await advance(36_000); expect(result.current.activityFreshness).toBe("unavailable"); expect(result.current.historyFreshness).toBe("stale");
  await advance(44_000); expect(result.current.historyFreshness).toBe("unavailable");
});

it("does not rejuvenate an old history scan with a fresh activity response or an old activity sample with a fresh history scan", async () => {
  fetcher.mockResolvedValueOnce(json(gpuTaskHistory({ history_captured_at: "2026-11-11T00:56:00Z" })));
  const { result } = renderHook(() => useGpuTaskHistory()); await flush();
  expect(result.current.activityFreshness).toBe("fresh"); expect(result.current.historyFreshness).toBe("unavailable");
  fetcher.mockResolvedValueOnce(json(gpuTaskHistory({ captured_at: "2026-11-11T01:00:05Z", history_captured_at: "2026-11-11T00:56:00Z", activity: { captured_at: "2026-11-11T01:00:05Z", status: "ready", error_code: null, items: [] } })));
  await advance(5_000); expect(result.current.historyAge).toBe(245); expect(result.current.activityFreshness).toBe("fresh");
  fetcher.mockResolvedValueOnce(json(gpuTaskHistory({ captured_at: "2026-11-11T01:00:10Z", history_captured_at: "2026-11-11T01:00:10Z", activity: { captured_at: "2026-11-11T00:59:00Z", status: "ready", error_code: null, items: [] } })));
  await advance(5_000); expect(result.current.historyFreshness).toBe("fresh"); expect(result.current.activityFreshness).toBe("unavailable");
});

it.each([[401, "authentication_required"], [403, "monitoring_forbidden"], [409, "session_context_mismatch"], [503, "authentication_unavailable"]])("clears cached task records and stops after auth failure %s/%s", async (status, code) => {
  const { result } = renderHook(() => useGpuTaskHistory()); await flush(); expect(result.current.data).not.toBeNull();
  fetcher.mockResolvedValue(json({ code }, Number(status)));
  await advance(5_000); expect(result.current.data).toBeNull();
  const count = fetcher.mock.calls.length; await advance(180_000); expect(fetcher).toHaveBeenCalledTimes(count);
});

it("clears a retired account immediately and rejects late delivery into the new account", async () => {
  const { result } = renderHook(() => useGpuTaskHistory()); await flush();
  let finishOld: (response: Response) => void = () => {};
  fetcher.mockImplementationOnce(() => new Promise<Response>(resolve => { finishOld = resolve; }));
  await advance(5_000);
  fetcher.mockResolvedValue(json(gpuTaskHistory({ collector_id: "new-account" })));
  await act(async () => { retireSession(); installSession({ authenticated: true, user: { id: "user-b", username: "user-b", must_change_password: false }, session_id: "session-b", csrf_token: "csrf-b", capabilities: {} }); });
  await flush(); expect(result.current.data?.collector_id).toBe("new-account");
  expect(new Headers(fetcher.mock.calls.at(-1)?.[1].headers).get("X-Session-Context")).toBe("session-b");
  await act(async () => { finishOld(json(gpuTaskHistory({ collector_id: "private-account-a" }))); });
  expect(result.current.data?.collector_id).toBe("new-account");
  await act(async () => { retireSession(); installSession(GUEST_SESSION); });
  expect(result.current.data).toBeNull(); expect(result.current.active).toBe(false);
});

it("does not leak a cached response when identity changes without a retired epoch", async () => {
  const { result, rerender } = renderHook(() => useGpuTaskHistory()); await flush();
  let finishNew: (response: Response) => void = () => {};
  fetcher.mockImplementationOnce(() => new Promise<Response>(resolve => { finishNew = resolve; }));
  installSession({ authenticated: true, user: { id: "new-user", username: "new-user", must_change_password: false }, session_id: "new-session", csrf_token: "csrf-new", capabilities: {} });
  rerender(); expect(result.current.data).toBeNull();
  await act(async () => { finishNew(json(gpuTaskHistory({ collector_id: "new-session-data" }))); });
  expect(result.current.data?.collector_id).toBe("new-session-data");
});

it("retries a transient authentication outage explicitly and waits for a new authorized response before restoring data", async () => {
  const { result } = renderHook(() => useGpuTaskHistory()); await flush();
  expect(result.current.data).not.toBeNull();
  fetcher.mockResolvedValueOnce(json({ code: "authentication_unavailable" }, 503));
  await advance(5_000); expect(result.current.data).toBeNull();
  const stopped = fetcher.mock.calls.length; await advance(5_000); expect(fetcher).toHaveBeenCalledTimes(stopped);
  let finish: (response: Response) => void = () => {};
  fetcher.mockImplementationOnce(() => new Promise<Response>(resolve => { finish = resolve; }));
  await act(async () => result.current.refresh());
  expect(fetcher).toHaveBeenCalledTimes(stopped + 1); expect(result.current.loading).toBe(true); expect(result.current.data).toBeNull();
  expect(new Headers(fetcher.mock.calls.at(-1)?.[1].headers).get("X-Session-Context")).toBe("test-session");
  await act(async () => { finish(json(gpuTaskHistory({ collector_id: "recovered-after-retry" }))); });
  expect(result.current.data?.collector_id).toBe("recovered-after-retry"); expect(result.current.error).toBeNull();
  await advance(5_000); expect(fetcher).toHaveBeenCalledTimes(stopped + 2);
});

it("does not let an old retry callback issue requests after logout", async () => {
  fetcher.mockResolvedValueOnce(json({ code: "authentication_unavailable" }, 503));
  const { result } = renderHook(() => useGpuTaskHistory()); await flush();
  const oldRefresh = result.current.refresh;
  await act(async () => { retireSession(); installSession(GUEST_SESSION); });
  const requests = fetcher.mock.calls.length;
  await act(async () => oldRefresh()); await advance(5_000);
  expect(fetcher).toHaveBeenCalledTimes(requests); expect(result.current.data).toBeNull();
});

it.each([true, false])("binds retry callbacks to their identity even when an account switch retires the epoch: %s", async retire => {
  const { result, rerender } = renderHook(() => useGpuTaskHistory()); await flush();
  const oldRefresh = result.current.refresh;
  await act(async () => {
    if (retire) retireSession();
    installSession({ authenticated: true, user: { id: "other", username: "other", must_change_password: false }, session_id: "other-session", csrf_token: "other-csrf", capabilities: {} });
  });
  rerender(); await flush(); const requests = fetcher.mock.calls.length;
  await act(async () => oldRefresh());
  expect(fetcher).toHaveBeenCalledTimes(requests);
  await act(async () => result.current.refresh());
  expect(fetcher).toHaveBeenCalledTimes(requests + 1);
  expect(new Headers(fetcher.mock.calls.at(-1)?.[1].headers).get("X-Session-Context")).toBe("other-session");
});

it("keeps independently complete groups and original timestamps when later verification is partial", async () => {
  const first = gpuTaskHistory();
  first.activity.items = [gpuActivityItem({ kind: "task", status: "running", label: "DFT任务" })];
  fetcher.mockResolvedValueOnce(json(first));
  const { result } = renderHook(() => useGpuTaskHistory()); await flush();
  const partial = gpuTaskHistory({ captured_at: "2026-11-11T01:00:05Z", history_captured_at: "2026-11-11T01:00:05Z", status: "partial", tasks: [] });
  partial.sources[0] = { ...partial.sources[0], status: "unavailable", matched_tasks: 0, error_code: "source_unavailable" };
  partial.activity = { captured_at: partial.captured_at, status: "partial", error_code: "activity_partial", items: [] };
  fetcher.mockResolvedValueOnce(json(partial)); await advance(5000);
  expect(result.current.data?.tasks).toEqual(first.tasks);
  expect(result.current.data?.activity.items).toEqual(first.activity.items);
  expect(result.current.data?.history_captured_at).toBe(first.history_captured_at);
  expect(result.current.data?.activity.captured_at).toBe(first.activity.captured_at);
  expect(result.current.historyAge).toBe(5); expect(result.current.activityAge).toBe(5);
  expect(result.current.activityCached).toBe(true); expect(result.current.historyCached).toBe(true);
  expect(result.current.accessError).toBe(false);
});

it("updates healthy history while retaining activity, then accepts confirmed empty activity as a new result", async () => {
  const first = gpuTaskHistory();
  first.activity.items = [gpuActivityItem({ kind: "task", status: "running", label: "DFT任务" })];
  fetcher.mockResolvedValueOnce(json(first));
  const { result } = renderHook(() => useGpuTaskHistory()); await flush();
  const next = gpuTaskHistory({ captured_at: "2026-11-11T01:00:05Z", history_captured_at: "2026-11-11T01:00:05Z", tasks: [] });
  next.activity = { captured_at: next.captured_at, status: "unavailable", error_code: "activity_unavailable", items: [] };
  fetcher.mockResolvedValueOnce(json(next)); await advance(5000);
  expect(result.current.data?.tasks).toEqual([]); expect(result.current.historyCached).toBe(false);
  expect(result.current.data?.activity.items).toEqual(first.activity.items); expect(result.current.activityCached).toBe(true);
  const recovered = gpuTaskHistory({ captured_at: "2026-11-11T01:00:10Z", history_captured_at: "2026-11-11T01:00:10Z", tasks: [] });
  recovered.activity.captured_at = recovered.captured_at;
  fetcher.mockResolvedValueOnce(json(recovered)); await advance(5000);
  expect(result.current.data?.activity.items).toEqual([]); expect(result.current.activityCached).toBe(false);
  expect(result.current.activityAvailable).toBe(true); expect(result.current.historyAvailable).toBe(true);
  fetcher.mockResolvedValueOnce(json({ ...next, captured_at: "2026-11-11T01:00:15Z" })); await advance(5000);
  expect(result.current.data?.activity.items).toEqual([]); expect(result.current.activityCached).toBe(true);
});

it("preserves successful activity while history fails and does not turn partial emptiness into success", async () => {
  const { result } = renderHook(() => useGpuTaskHistory()); await flush();
  const next = gpuTaskHistory({ captured_at: "2026-11-11T01:00:05Z", status: "unavailable", tasks: [] });
  next.sources = next.sources.map(source => ({ ...source, status: "unavailable", matched_tasks: 0, error_code: "source_unavailable" }));
  next.activity = { captured_at: next.captured_at, status: "ready", error_code: null, items: [gpuActivityItem({ kind: "task", status: "running", label: "DFT任务" })] };
  fetcher.mockResolvedValueOnce(json(next)); await advance(5000);
  expect(result.current.data?.tasks).toEqual([gpuHistoricalTask()]); expect(result.current.historyCached).toBe(true);
  expect(result.current.data?.activity.items).toHaveLength(1); expect(result.current.activityCached).toBe(false);
});

it("does not reuse old devices' complete results for a changed physical GPU inventory", async () => {
  const { result } = renderHook(() => useGpuTaskHistory()); await flush();
  const next = gpuTaskHistory({ captured_at: "2026-11-11T01:00:05Z", gpu_uuids: ["gpu-replacement"], status: "partial", tasks: [] });
  next.activity = { captured_at: next.captured_at, status: "unavailable", error_code: "activity_unavailable", items: [] };
  fetcher.mockResolvedValueOnce(json(next)); await advance(5000);
  expect(result.current.data?.tasks).toEqual([]); expect(result.current.historyAvailable).toBe(false);
  expect(result.current.data?.activity.items).toEqual([]); expect(result.current.activityAvailable).toBe(false);
});

it("keeps same-GPU results through collector restarts but clears future cache on clock rollback", async () => {
  const first = gpuTaskHistory();
  first.activity.items = [gpuActivityItem({ kind: "task", status: "running", label: "DFT任务" })];
  fetcher.mockResolvedValueOnce(json(first));
  const { result } = renderHook(() => useGpuTaskHistory()); await flush();
  const next = gpuTaskHistory({ collector_id: "restarted", captured_at: "2026-11-11T01:00:05Z", gpu_uuids: ["gpu-second", "gpu-first"], status: "partial", tasks: [] });
  next.activity = { captured_at: next.captured_at, status: "unavailable", error_code: "activity_unavailable", items: [] };
  fetcher.mockResolvedValueOnce(json(next)); await advance(5000);
  expect(result.current.data?.tasks).toEqual(first.tasks); expect(result.current.data?.activity.items).toEqual(first.activity.items);
  const rollback = { ...next, captured_at: "2026-11-11T00:59:00Z", history_captured_at: "2026-11-11T00:59:00Z", activity: { ...next.activity, captured_at: "2026-11-11T00:59:00Z" } };
  fetcher.mockResolvedValueOnce(json(rollback)); await advance(5000);
  expect(result.current.data?.tasks).toEqual([]); expect(result.current.data?.activity.items).toEqual([]);
  expect(result.current.historyAvailable).toBe(false); expect(result.current.activityAvailable).toBe(false);
});

it("discards both fallback groups after authentication failure before an authorized partial retry", async () => {
  const { result } = renderHook(() => useGpuTaskHistory()); await flush();
  fetcher.mockResolvedValueOnce(json({ code: "authentication_unavailable" }, 503)); await advance(5000);
  expect(result.current.accessError).toBe(true); expect(result.current.data).toBeNull();
  const partial = gpuTaskHistory({ status: "partial", tasks: [] });
  partial.activity = { ...partial.activity, status: "partial", error_code: "activity_partial", items: [] };
  fetcher.mockResolvedValueOnce(json(partial)); await act(async () => result.current.refresh());
  expect(result.current.data?.tasks).toEqual([]); expect(result.current.historyAvailable).toBe(false);
  expect(result.current.activityAvailable).toBe(false); expect(result.current.accessError).toBe(false);
});
