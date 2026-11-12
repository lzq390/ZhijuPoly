// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { GUEST_SESSION, installSession, retireSession } from "../auth/session";
import { monitoringHistory, monitoringSnapshot } from "../test/serverMonitoringFixture";
import { useServerMonitoring } from "./useServerMonitoring";

let fetchMock: ReturnType<typeof vi.fn>;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
function success(input: string | URL | Request) {
  const url = String(input);
  return Promise.resolve(json(url.endsWith("access") ? { can_read: true } : url.endsWith("history") ? monitoringHistory() : monitoringSnapshot()));
}
const count = (kind: string) => fetchMock.mock.calls.filter(call => String(call[0]).endsWith(kind)).length;
beforeEach(() => {
  vi.stubEnv("VITE_SERVER_MONITORING_ENABLED", "true");
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] }); vi.setSystemTime(new Date("2026-11-11T01:00:00Z"));
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  fetchMock = vi.fn(success); vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); });
async function flush() { await act(async () => { await Promise.resolve(); await Promise.resolve(); }); }
async function advance(ms: number) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }

it("makes no requests when the feature is disabled or the user is a guest", async () => {
  vi.stubEnv("VITE_SERVER_MONITORING_ENABLED", "false");
  const first = renderHook(() => useServerMonitoring(true)); await flush();
  expect(first.result.current.access).toBe("disabled"); expect(fetchMock).not.toHaveBeenCalled(); first.unmount();
  vi.stubEnv("VITE_SERVER_MONITORING_ENABLED", "true"); retireSession(); installSession(GUEST_SESSION);
  const second = renderHook(() => useServerMonitoring(true)); await flush();
  expect(second.result.current.access).toBe("guest"); expect(fetchMock).not.toHaveBeenCalled();
});

it("checks access before data and never fetches metrics for a denied account", async () => {
  fetchMock.mockResolvedValue(json({ code: "monitoring_forbidden" }, 403));
  const { result } = renderHook(() => useServerMonitoring(true)); await flush(); await advance(60000);
  expect(result.current.access).toBe("denied"); expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(result.current.snapshot).toBeNull(); expect(result.current.history).toBeNull();
});

it("probes sidebar access once and polls only the open page at independent intervals", async () => {
  const { result, rerender } = renderHook(({ active }) => useServerMonitoring(active), { initialProps: { active: false } }); await flush();
  expect(result.current.access).toBe("allowed"); expect(fetchMock).toHaveBeenCalledTimes(1);
  rerender({ active: true }); await flush();
  expect(count("snapshot")).toBe(1); expect(count("history")).toBe(1);
  await advance(15000); expect(count("snapshot")).toBe(4); expect(count("history")).toBe(1);
  await advance(15000); expect(count("history")).toBe(2); expect(count("access")).toBe(1);
  rerender({ active: false }); await advance(60000);
  expect(count("snapshot")).toBe(7); expect(count("history")).toBe(2);
});

it("waits for a slow response to finish before starting the next poll", async () => {
  let resolve: (value: Response) => void = () => {};
  fetchMock.mockImplementation(input => String(input).endsWith("snapshot") ? new Promise<Response>(done => { resolve = done; }) : success(input));
  const { result } = renderHook(() => useServerMonitoring(true)); await flush(); await advance(6000);
  expect(count("snapshot")).toBe(1);
  await act(async () => { resolve(json(monitoringSnapshot())); });
  expect(result.current.snapshot?.hostname).toBe("compute-test");
  await advance(4999); expect(count("snapshot")).toBe(1);
  await advance(1); expect(count("snapshot")).toBe(2);
});

it("retains the last valid metrics after a collector failure, marked with an error", async () => {
  const { result } = renderHook(() => useServerMonitoring(true)); await flush();
  fetchMock.mockImplementation(input => String(input).endsWith("snapshot") ? Promise.resolve(json({ code: "monitoring_unavailable" }, 503)) : success(input));
  await advance(5000);
  expect(result.current.access).toBe("allowed"); expect(result.current.snapshot?.hostname).toBe("compute-test");
  expect(result.current.snapshotError).toContain("正在重试"); expect(result.current.history).not.toBeNull();
});

it.each([[403, "monitoring_forbidden"], [403, "membership_required"], [409, "session_context_mismatch"], [401, "authentication_required"], [503, "authentication_unavailable"]])("clears all cached data after authentication failure %s / %s", async (status, code) => {
  const { result } = renderHook(() => useServerMonitoring(true)); await flush();
  expect(result.current.snapshot).not.toBeNull();
  fetchMock.mockImplementation(input => String(input).endsWith("snapshot") ? Promise.resolve(json({ code }, status)) : success(input));
  await advance(5000);
  expect(result.current.snapshot).toBeNull(); expect(result.current.history).toBeNull();
  const requests = fetchMock.mock.calls.length; await advance(60000); expect(fetchMock).toHaveBeenCalledTimes(requests);
});

it("pauses in the background and immediately refreshes both cached resources when visible", async () => {
  renderHook(() => useServerMonitoring(true)); await flush();
  await act(async () => { Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" }); document.dispatchEvent(new Event("visibilitychange")); });
  await advance(120000); expect(count("snapshot")).toBe(1); expect(count("history")).toBe(1);
  await act(async () => { Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" }); document.dispatchEvent(new Event("visibilitychange")); });
  expect(count("snapshot")).toBe(2); expect(count("history")).toBe(2);
});

it("clears the previous collector's history and rejects its late response", async () => {
  const { result } = renderHook(() => useServerMonitoring(true)); await flush();
  fetchMock.mockImplementation(input => String(input).endsWith("snapshot") ? Promise.resolve(json(monitoringSnapshot({ collector_id: "collector-2", started_at: "2026-11-11T01:00:01Z" }))) : success(input));
  await advance(5000); expect(result.current.snapshot?.collector_id).toBe("collector-2"); expect(result.current.history).toBeNull();
  await act(async () => result.current.refresh());
  expect(result.current.snapshot?.collector_id).toBe("collector-2"); expect(result.current.history).toBeNull();
});

it("clears identity-bound samples and ignores a late delivery after logout", async () => {
  const { result } = renderHook(() => useServerMonitoring(true)); await flush();
  let resolve: (value: Response) => void = () => {};
  fetchMock.mockImplementation(input => String(input).endsWith("snapshot") ? new Promise<Response>(done => { resolve = done; }) : success(input));
  await advance(5000);
  await act(async () => { retireSession(); installSession(GUEST_SESSION); });
  await act(async () => { resolve(json(monitoringSnapshot({ hostname: "old-private-host" }))); });
  expect(result.current.snapshot).toBeNull(); expect(result.current.history).toBeNull(); expect(result.current.access).toBe("guest");
});

it("does not let a request aborted while hidden overwrite the immediate resumed sample or create another polling timer", async () => {
  const { result } = renderHook(() => useServerMonitoring(true)); await flush();
  let finishHidden: (value: Response) => void = () => {};
  fetchMock.mockImplementationOnce(() => new Promise<Response>(resolve => { finishHidden = resolve; }));
  await advance(5000);
  await act(async () => { Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" }); document.dispatchEvent(new Event("visibilitychange")); });
  fetchMock.mockImplementation(input => String(input).endsWith("snapshot") ? Promise.resolve(json(monitoringSnapshot({ hostname: "resumed-host" }))) : success(input));
  await act(async () => { Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" }); document.dispatchEvent(new Event("visibilitychange")); });
  expect(result.current.snapshot?.hostname).toBe("resumed-host");
  await act(async () => { finishHidden(json(monitoringSnapshot({ hostname: "aborted-host" }))); });
  expect(result.current.snapshot?.hostname).toBe("resumed-host");
  await advance(5000);
  expect(count("snapshot")).toBe(4); expect(count("history")).toBe(2);
});

it("rechecks a new account with its new session context and rejects a late old-account sample", async () => {
  const { result } = renderHook(() => useServerMonitoring(true)); await flush();
  let finishOld: (value: Response) => void = () => {};
  fetchMock.mockImplementationOnce(() => new Promise<Response>(resolve => { finishOld = resolve; }));
  await advance(5000);
  fetchMock.mockImplementation(input => String(input).endsWith("snapshot") ? Promise.resolve(json(monitoringSnapshot({ hostname: "account-b-host" }))) : success(input));
  const beforeSwitch = fetchMock.mock.calls.length;
  await act(async () => {
    retireSession();
    installSession({ authenticated: true, user: { id: "account-b", username: "account-b", must_change_password: false }, session_id: "session-b", csrf_token: "csrf-b", capabilities: {} });
  });
  await flush();
  expect(count("access")).toBe(2); expect(result.current.snapshot?.hostname).toBe("account-b-host");
  expect(fetchMock.mock.calls.slice(beforeSwitch).every(call => new Headers(call[1].headers).get("X-Session-Context") === "session-b")).toBe(true);
  await act(async () => { finishOld(json(monitoringSnapshot({ hostname: "private-account-a-host" }))); });
  expect(result.current.snapshot?.hostname).toBe("account-b-host");
});

it("treats an authentication failure on history as authoritative over an in-flight snapshot success", async () => {
  const { result } = renderHook(() => useServerMonitoring(true)); await flush();
  let finishSnapshot: (value: Response) => void = () => {};
  fetchMock.mockImplementation(input => String(input).endsWith("snapshot")
    ? new Promise<Response>(resolve => { finishSnapshot = resolve; })
    : Promise.resolve(json({ code: "authentication_unavailable" }, 503)));
  await act(async () => result.current.refresh());
  expect(result.current.access).toBe("unavailable"); expect(result.current.snapshot).toBeNull(); expect(result.current.history).toBeNull();
  await act(async () => { finishSnapshot(json(monitoringSnapshot({ hostname: "late-private-host" }))); });
  expect(result.current.snapshot).toBeNull(); expect(result.current.history).toBeNull();
  const requests = fetchMock.mock.calls.length; await advance(60000); expect(fetchMock).toHaveBeenCalledTimes(requests);
});

it("stages history received before the first snapshot, then displays the matching pair immediately", async () => {
  let finishSnapshot: (value: Response) => void = () => {};
  fetchMock.mockImplementation(input => String(input).endsWith("snapshot")
    ? new Promise<Response>(resolve => { finishSnapshot = resolve; }) : success(input));
  const { result } = renderHook(() => useServerMonitoring(true)); await flush();
  expect(result.current.history).toBeNull();
  await act(async () => { finishSnapshot(json(monitoringSnapshot())); });
  expect(result.current.history?.collector_id).toBe("collector-1"); expect(count("history")).toBe(1);
});

it("accepts a restarted collector with an earlier wall clock and immediately refetches its history", async () => {
  const { result } = renderHook(() => useServerMonitoring(true)); await flush();
  const restarted = { collector_id: "collector-2", started_at: "2026-11-10T23:59:00Z", captured_at: "2026-11-10T23:59:10Z" };
  fetchMock.mockImplementation(input => Promise.resolve(json(String(input).endsWith("snapshot") ? monitoringSnapshot(restarted) : monitoringHistory({ ...restarted, points: [] }))));
  await advance(5000);
  expect(result.current.snapshot?.collector_id).toBe("collector-2"); expect(result.current.history?.collector_id).toBe("collector-2");
  expect(count("history")).toBe(2);
});

it("uses already-arrived new-collector history when the new snapshot completes later", async () => {
  const { result } = renderHook(() => useServerMonitoring(true)); await flush();
  let finishSnapshot: (value: Response) => void = () => {};
  const restarted = { collector_id: "collector-2", started_at: "2026-11-11T01:00:00Z", captured_at: "2026-11-11T01:00:05Z" };
  fetchMock.mockImplementation(input => String(input).endsWith("snapshot")
    ? new Promise<Response>(resolve => { finishSnapshot = resolve; }) : Promise.resolve(json(monitoringHistory({ ...restarted, points: [] }))));
  await act(async () => result.current.refresh());
  expect(result.current.snapshot?.collector_id).toBe("collector-1"); expect(result.current.history?.collector_id).toBe("collector-1");
  await act(async () => { finishSnapshot(json(monitoringSnapshot(restarted))); });
  expect(result.current.snapshot?.collector_id).toBe("collector-2"); expect(result.current.history?.collector_id).toBe("collector-2");
  expect(count("history")).toBe(2);
});

it("clears history after an in-process clock rollback and immediately replaces a late pre-rollback history response", async () => {
  const { result } = renderHook(() => useServerMonitoring(true)); await flush();
  let finishOldHistory: (value: Response) => void = () => {};
  const nextSnapshot = monitoringSnapshot({ sequence: 2, captured_at: "2026-11-11T00:50:00Z" });
  let delayed = false;
  fetchMock.mockImplementation(input => {
    if (String(input).endsWith("snapshot")) return Promise.resolve(json(nextSnapshot));
    if (!delayed) { delayed = true; return new Promise<Response>(resolve => { finishOldHistory = resolve; }); }
    return Promise.resolve(json(monitoringHistory({ captured_at: nextSnapshot.captured_at, points: [] })));
  });
  await act(async () => result.current.refresh());
  expect(result.current.snapshot?.sequence).toBe(2); expect(result.current.history).toBeNull();
  await act(async () => { finishOldHistory(json(monitoringHistory())); });
  expect(result.current.history).toBeNull();
  await advance(0);
  expect(result.current.history?.captured_at).toBe("2026-11-11T00:50:00Z"); expect(count("history")).toBe(3);
});

it("turns a hanging fetch into a bounded collection failure without discarding last valid data", async () => {
  const { result } = renderHook(() => useServerMonitoring(true)); await flush();
  fetchMock.mockImplementation((_input, init) => new Promise((_resolve, reject) => {
    init.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
  }));
  await advance(15000);
  expect(result.current.snapshotError).toContain("正在重试"); expect(result.current.snapshot?.hostname).toBe("compute-test");
  expect(result.current.refreshing).toBe(false);
});

it("also retires pre-reset history whose timestamps still precede a small wall-clock rollback", async () => {
  fetchMock.mockImplementation(input => String(input).endsWith("history")
    ? Promise.resolve(json(monitoringHistory({ captured_at: "2026-11-11T00:59:30Z", points: [] }))) : success(input));
  const { result } = renderHook(() => useServerMonitoring(true)); await flush();
  expect(result.current.history).not.toBeNull();
  let finishHistory: (value: Response) => void = () => {};
  fetchMock.mockImplementation(input => String(input).endsWith("snapshot")
    ? Promise.resolve(json(monitoringSnapshot({ sequence: 2, captured_at: "2026-11-11T00:59:50Z" })))
    : new Promise<Response>(resolve => { finishHistory = resolve; }));
  await advance(5000);
  expect(result.current.history).toBeNull(); expect(count("history")).toBe(2);
  await act(async () => { finishHistory(json(monitoringHistory({ captured_at: "2026-11-11T00:59:50Z", points: [] }))); });
  expect(result.current.history?.captured_at).toBe("2026-11-11T00:59:50Z");
});

it("does not clear a history-fetch error merely because another snapshot succeeds", async () => {
  const { result } = renderHook(() => useServerMonitoring(true)); await flush();
  fetchMock.mockImplementation(input => String(input).endsWith("history") ? Promise.resolve(json({ code: "monitoring_unavailable" }, 503)) : success(input));
  await act(async () => result.current.refresh());
  expect(result.current.historyError).not.toBeNull();
  await advance(5000); expect(result.current.historyError).not.toBeNull();
});
