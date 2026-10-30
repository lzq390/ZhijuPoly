// @vitest-environment jsdom

import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  OnlineKnowledgeHistoryItem,
  OnlineKnowledgeJobCreateResponse,
  OnlineKnowledgeJobResponse,
  OnlineKnowledgeSearchRequest,
  OnlineKnowledgeSearchResponse
} from "../types";
import { useOnlineKnowledgeSearch } from "./useOnlineKnowledgeSearch";

const api = vi.hoisted(() => ({
  create: vi.fn(), fetch: vi.fn(), history: vi.fn(), deleteHistory: vi.fn(), clearHistory: vi.fn()
}));

vi.mock("../services/api", async () => ({
  ...await vi.importActual<typeof import("../services/api")>("../services/api"),
  createOnlineKnowledgeJob: api.create,
  fetchOnlineKnowledgeJob: api.fetch,
  fetchOnlineKnowledgeHistory: api.history,
  deleteOnlineKnowledgeHistory: api.deleteHistory,
  clearOnlineKnowledgeHistory: api.clearHistory
}));

const payload: OnlineKnowledgeSearchRequest = {
  material: "New material", mode: "property", max_papers: 20, extraction_delay_seconds: 0
};
const savedResult: OnlineKnowledgeSearchResponse = {
  material: "Saved material", mode: "property", max_papers: 5, query_time_ms: 100,
  totalPapers: 2, exampleUsed: false, stats: {}, syntheses: [], propertyPoints: [],
  temperatureDistribution: [], solventDistribution: [], catalystTable: [], tempLabels: [],
  conditionSummary: [], reactionTypeTable: [], propertyNameDistribution: [],
  conditionDistribution: [], polymerTypeDistribution: [], relationshipDistribution: [], dataframe: []
};
const saved: OnlineKnowledgeHistoryItem = {
  history_id: 17, material: savedResult.material, mode: savedResult.mode,
  timestamp: "2026-10-28T00:00:00Z", papers_found: 2, reactions_extracted: 0,
  max_papers: 5, result_data: savedResult
};

function job(id = "old-job", status: OnlineKnowledgeJobResponse["status"] = "completed"): OnlineKnowledgeJobResponse {
  return {
    job_id: id, status, material: id, mode: "property", max_papers: 20,
    progress_stage: status, progress_message: status, processed_papers: 1, total_papers: 1,
    created_at: "2026-10-28T00:00:00Z", updated_at: "2026-10-28T00:00:01Z",
    error_message: status === "failed" ? "Old task failed" : null,
    result: status === "completed" ? { ...savedResult, material: id } : null
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}

async function flush() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

function expectHistory(state: ReturnType<typeof useOnlineKnowledgeSearch>) {
  expect(state).toMatchObject({
    isLoading: false, data: savedResult, error: null, jobId: null, jobStatus: null, job: null
  });
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  Object.values(api).forEach((mock) => mock.mockReset());
  api.create.mockResolvedValue({ job_id: "old-job", status: "pending" });
  api.history.mockResolvedValue({ history: [saved] });
  api.deleteHistory.mockResolvedValue({ success: true });
  api.clearHistory.mockResolvedValue({ success: true });
});

describe("online history refresh ordering", () => {
  const other = { ...saved, history_id: 18, material: "Other material" };

  it.each([
    ["delete", "success"], ["delete", "failure"],
    ["clear", "success"], ["clear", "failure"]
  ] as const)("keeps successful %s intact after a stale refresh returns %s", async (mutation, outcome) => {
    const pending = deferred<{ history: OnlineKnowledgeHistoryItem[] }>();
    api.history.mockResolvedValueOnce({ history: [saved, other] }).mockReturnValueOnce(pending.promise);
    const { result } = renderHook(() => useOnlineKnowledgeSearch());
    await act(async () => { await result.current.loadHistory(); });
    let refresh!: Promise<void>;
    act(() => { refresh = result.current.loadHistory(); });
    expect(result.current.isHistoryLoading).toBe(true);

    await act(async () => {
      if (mutation === "delete") await result.current.deleteHistoryItem(saved.history_id);
      else await result.current.clearHistory();
    });
    const expected = mutation === "delete" ? [other] : [];
    expect(result.current.history).toEqual(expected);
    await act(async () => {
      if (outcome === "success") pending.resolve({ history: [saved, other] });
      else pending.reject(new Error("Obsolete refresh failed"));
      await refresh;
    });
    expect(result.current).toMatchObject({ history: expected, isHistoryLoading: false, historyError: null });
    expect(api[mutation === "delete" ? "deleteHistory" : "clearHistory"]).toHaveBeenCalledOnce();
  });

  it.each(["success", "failure"] as const)(
    "keeps the newest refresh loading when an earlier request returns %s",
    async (outcome) => {
      const older = deferred<{ history: OnlineKnowledgeHistoryItem[] }>();
      const latest = deferred<{ history: OnlineKnowledgeHistoryItem[] }>();
      api.history.mockResolvedValueOnce({ history: [saved] })
        .mockReturnValueOnce(older.promise).mockReturnValueOnce(latest.promise);
      const { result } = renderHook(() => useOnlineKnowledgeSearch());
      await act(async () => { await result.current.loadHistory(); });
      let first!: Promise<void>;
      let second!: Promise<void>;
      act(() => { first = result.current.loadHistory(); });
      act(() => { second = result.current.loadHistory(); });

      await act(async () => {
        if (outcome === "success") older.resolve({ history: [other] });
        else older.reject(new Error("Obsolete refresh failed"));
        await first;
      });
      expect(result.current).toMatchObject({ history: [saved], isHistoryLoading: true, historyError: null });
      await act(async () => { latest.resolve({ history: [saved, other] }); await second; });
      expect(result.current).toMatchObject({ history: [saved, other], isHistoryLoading: false, historyError: null });
    }
  );

  it.each(["delete", "clear"] as const)("preserves the pending refresh when %s fails", async (mutation) => {
    const pending = deferred<{ history: OnlineKnowledgeHistoryItem[] }>();
    api.history.mockResolvedValueOnce({ history: [saved] }).mockReturnValueOnce(pending.promise);
    api[mutation === "delete" ? "deleteHistory" : "clearHistory"].mockRejectedValueOnce(new Error("Mutation failed"));
    const { result } = renderHook(() => useOnlineKnowledgeSearch());
    await act(async () => { await result.current.loadHistory(); });
    let refresh!: Promise<void>;
    act(() => { refresh = result.current.loadHistory(); });
    await act(async () => {
      const change = mutation === "delete" ? result.current.deleteHistoryItem(saved.history_id) : result.current.clearHistory();
      await expect(change).rejects.toThrow("Mutation failed");
    });
    expect(result.current).toMatchObject({ history: [saved], isHistoryLoading: true, historyError: "Mutation failed" });
    await act(async () => { pending.resolve({ history: [saved, other] }); await refresh; });
    expect(result.current.history).toEqual([saved, other]);
    expect(result.current.isHistoryLoading).toBe(false);
  });

  it("reports the current refresh failure and allows a later retry", async () => {
    api.history.mockRejectedValueOnce(new Error("Current refresh failed"));
    const { result } = renderHook(() => useOnlineKnowledgeSearch());
    await act(async () => { await result.current.loadHistory(); });
    expect(result.current).toMatchObject({ history: [], isHistoryLoading: false, historyError: "Current refresh failed" });
    await act(async () => { await result.current.loadHistory(); });
    expect(result.current).toMatchObject({ history: [saved], isHistoryLoading: false, historyError: null });
  });
});

afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("restoring online history during a task", () => {
  it("aborts a pending create request and ignores its late success without starting polling", async () => {
    const pending = deferred<OnlineKnowledgeJobCreateResponse>();
    api.create.mockReturnValue(pending.promise);
    const { result } = renderHook(() => useOnlineKnowledgeSearch());
    await act(async () => { await result.current.loadHistory(); });
    let submission!: Promise<void>;
    act(() => { submission = result.current.submit(payload); });
    const signal = api.create.mock.calls[0][1] as AbortSignal;

    act(() => result.current.restoreFromHistory(saved));
    expect(signal.aborted).toBe(true);
    expectHistory(result.current);
    expect(result.current.history).toEqual([saved]);
    await act(async () => {
      pending.resolve({ job_id: "old-job", status: "pending" });
      await submission;
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expectHistory(result.current);
    expect(api.create).toHaveBeenCalledOnce();
    expect(api.fetch).not.toHaveBeenCalled();
  });

  it.each(["completed", "failed", "rejected"] as const)(
    "ignores a late %s response from an in-flight GET even when the transport ignores abort",
    async (outcome) => {
      const pending = deferred<OnlineKnowledgeJobResponse>();
      api.fetch.mockReturnValue(pending.promise);
      const { result } = renderHook(() => useOnlineKnowledgeSearch());
      let submission!: Promise<void>;
      act(() => { submission = result.current.submit(payload); });
      await flush();
      await act(async () => { await vi.advanceTimersByTimeAsync(1_200); });
      const signal = api.fetch.mock.calls[0][1] as AbortSignal;

      act(() => result.current.restoreFromHistory(saved));
      expect(signal.aborted).toBe(true);
      expectHistory(result.current);
      await act(async () => {
        if (outcome === "rejected") pending.reject(new Error("Old network error"));
        else pending.resolve(job("old-job", outcome));
        await submission;
        await vi.advanceTimersByTimeAsync(60_000);
      });
      expectHistory(result.current);
      expect(api.create).toHaveBeenCalledOnce();
      expect(api.fetch).toHaveBeenCalledOnce();
      expect(api.history).not.toHaveBeenCalled();
    }
  );

  it.each(["initial delay", "poll interval", "network backoff"] as const)(
    "stops scheduled observation during %s without another POST or GET",
    async (phase) => {
      if (phase === "network backoff") api.fetch.mockRejectedValue(new TypeError("offline"));
      else api.fetch.mockResolvedValue(job("old-job", "running"));
      const { result } = renderHook(() => useOnlineKnowledgeSearch());
      let submission!: Promise<void>;
      act(() => { submission = result.current.submit(payload); });
      await flush();
      if (phase !== "initial delay") {
        await act(async () => { await vi.advanceTimersByTimeAsync(1_200); });
      }
      const fetchCount = api.fetch.mock.calls.length;
      act(() => result.current.restoreFromHistory(saved));
      await act(async () => { await submission; await vi.advanceTimersByTimeAsync(60_000); });
      expectHistory(result.current);
      expect(api.create).toHaveBeenCalledOnce();
      expect(api.fetch).toHaveBeenCalledTimes(fetchCount);
    }
  );

  it("keeps a replacement task isolated when the old task finishes late", async () => {
    const old = deferred<OnlineKnowledgeJobResponse>();
    const replacement = deferred<OnlineKnowledgeJobResponse>();
    api.create.mockResolvedValueOnce({ job_id: "old-job", status: "pending" })
      .mockResolvedValueOnce({ job_id: "new-job", status: "pending" });
    api.fetch.mockReturnValueOnce(old.promise).mockReturnValueOnce(replacement.promise);
    const { result } = renderHook(() => useOnlineKnowledgeSearch());
    let first!: Promise<void>;
    let second!: Promise<void>;
    act(() => { first = result.current.submit(payload); });
    await flush();
    await act(async () => { await vi.advanceTimersByTimeAsync(1_200); });
    act(() => result.current.restoreFromHistory(saved));
    act(() => { second = result.current.submit({ ...payload, material: "new-job" }); });
    await flush();
    await act(async () => { await vi.advanceTimersByTimeAsync(1_200); });
    const newSignal = api.fetch.mock.calls[1][1] as AbortSignal;

    await act(async () => { old.resolve(job()); await first; });
    expect(result.current).toMatchObject({ isLoading: true, jobId: "new-job", error: null, data: null });
    expect(newSignal.aborted).toBe(false);
    await act(async () => { replacement.resolve(job("new-job")); await second; });
    expect(result.current).toMatchObject({ isLoading: false, data: { material: "new-job" }, jobId: "new-job" });
    expect(api.history).toHaveBeenCalledOnce();
    expect(api.create).toHaveBeenCalledTimes(2);
  });

  it("retains the new controller after a stale submission finally runs", async () => {
    const old = deferred<OnlineKnowledgeJobCreateResponse>();
    api.create.mockReturnValueOnce(old.promise).mockResolvedValueOnce({ job_id: "new-job", status: "pending" });
    const { result } = renderHook(() => useOnlineKnowledgeSearch());
    let first!: Promise<void>;
    let second!: Promise<void>;
    act(() => { first = result.current.submit(payload); });
    act(() => result.current.restoreFromHistory(saved));
    act(() => { second = result.current.submit(payload); });
    await flush();
    const newSignal = api.create.mock.calls[1][1] as AbortSignal;
    await act(async () => { old.resolve({ job_id: "old-job", status: "pending" }); await first; });

    act(() => result.current.restoreFromHistory(saved));
    expect(newSignal.aborted).toBe(true);
    await act(async () => { await second; });
    expectHistory(result.current);
    expect(api.fetch).not.toHaveBeenCalled();
  });
});
