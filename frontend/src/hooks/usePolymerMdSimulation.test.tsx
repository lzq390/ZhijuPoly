// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PolymerMdCapabilities, PolymerMdJob, PolymerMdJobPage } from "../types/polymerMd";
import { usePolymerMdSimulation } from "./usePolymerMdSimulation";
import { retireSession } from "../auth/session";
import { polymerMdServiceState } from "../components/polymer-md/serviceState";
import { POLYMER_MD_DEFAULTS } from "../components/polymer-md/config";

const mocks = vi.hoisted(() => ({ capabilities: vi.fn(), status: vi.fn(), jobs: vi.fn(), job: vi.fn(), create: vi.fn(), cancel: vi.fn() }));
vi.mock("../services/polymerMdApi", async importOriginal => ({
  ...await importOriginal<typeof import("../services/polymerMdApi")>(),
  fetchPolymerMdCapabilities: mocks.capabilities,
  fetchPolymerMdStatus: mocks.status,
  fetchPolymerMdJobs: mocks.jobs,
  fetchPolymerMdJob: mocks.job,
  createPolymerMdJob: mocks.create,
  cancelPolymerMdJob: mocks.cancel
}));

const capabilities = { enabled: true, schema_ready: true, worker_ready: true, submit_enabled: true } as PolymerMdCapabilities;
const history: PolymerMdJobPage = { items: [], total: 0, page: 1, page_size: 10 };
const job = { job_id: "slow-job", status: "running" } as PolymerMdJob;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  vi.clearAllMocks();
  sessionStorage.clear();
  mocks.capabilities.mockResolvedValue(capabilities);
  mocks.status.mockResolvedValue(capabilities);
  mocks.jobs.mockResolvedValue(history);
  mocks.job.mockResolvedValue(job);
  mocks.cancel.mockResolvedValue(job);
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("polymer MD polling on a slow connection", () => {
  it("publishes a job response slower than the polling interval and stops polling after unmount", async () => {
    mocks.job.mockImplementation(() => new Promise(resolve => setTimeout(() => resolve(job), 5000)));
    const { result, unmount } = renderHook(() => usePolymerMdSimulation(true, job.job_id, vi.fn()));
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(result.current.job).toEqual(job);
    expect(result.current.loading).toBe(false);
    expect(mocks.job).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(4000));
    expect(mocks.job).toHaveBeenCalledTimes(2);
    unmount();
    await act(() => vi.advanceTimersByTimeAsync(20000));
    expect(mocks.job).toHaveBeenCalledTimes(2);
  });

  it("accepts slow history without overlapping requests while service checks continue", async () => {
    mocks.jobs.mockImplementation(() => new Promise(resolve => setTimeout(() => resolve(history), 15000)));
    const { result } = renderHook(() => usePolymerMdSimulation(true, null, vi.fn()));
    await act(() => vi.advanceTimersByTimeAsync(15000));
    expect(result.current.history).toEqual(history);
    expect(mocks.jobs).toHaveBeenCalledTimes(1);
    expect(mocks.capabilities).toHaveBeenCalledTimes(2);
    expect(mocks.status).not.toHaveBeenCalled();
    await act(() => vi.advanceTimersByTimeAsync(10000));
    expect(mocks.jobs).toHaveBeenCalledTimes(2);
  });
});

describe("polymer MD service observation freshness", () => {
  it("uses one complete observation and does not restart service checks when history pagination changes", async () => {
    const { result } = renderHook(() => usePolymerMdSimulation(true, null, vi.fn()));
    expect(result.current.serviceCheck).toBe("checking");
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(result.current.capabilities).toEqual(capabilities);
    expect(result.current.status).toEqual(capabilities);
    expect(result.current.serviceCheck).toBe("confirmed");
    act(() => result.current.setPage(2));
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(mocks.capabilities).toHaveBeenCalledTimes(1);
    expect(mocks.status).not.toHaveBeenCalled();
  });

  it("retains configuration after a missed observation, blocks submission, reports repeated failures and recovers", async () => {
    const { result } = renderHook(() => usePolymerMdSimulation(true, null, vi.fn()));
    await act(() => vi.advanceTimersByTimeAsync(0));
    mocks.capabilities.mockRejectedValue(new Error("temporary connection failure"));
    await act(() => vi.advanceTimersByTimeAsync(10000));
    expect(result.current.serviceCheck).toBe("unconfirmed");
    expect(result.current.capabilities).toEqual(capabilities);
    expect(result.current.status).toEqual(capabilities);
    expect(polymerMdServiceState(false, result.current.serviceError, result.current.capabilities, result.current.status, result.current.serviceCheck).ready).toBe(false);
    await act(() => result.current.submit(POLYMER_MD_DEFAULTS));
    expect(mocks.create).not.toHaveBeenCalled();
    expect(result.current.pendingSubmission).toBeNull();
    expect(result.current.error).toContain("状态尚未确认");
    await act(() => vi.advanceTimersByTimeAsync(10000));
    expect(result.current.serviceCheck).toBe("unavailable");
    mocks.capabilities.mockResolvedValue(capabilities);
    await act(() => vi.advanceTimersByTimeAsync(10000));
    expect(result.current.serviceCheck).toBe("confirmed");
    expect(result.current.serviceError).toBeNull();
  });

  it("bounds a hung observation, aborts it and rejects its late success after a newer authoritative block", async () => {
    const { result } = renderHook(() => usePolymerMdSimulation(true, null, vi.fn()));
    await act(() => vi.advanceTimersByTimeAsync(0));
    let resolveOld!: (value: PolymerMdCapabilities) => void;
    const old = new Promise<PolymerMdCapabilities>(resolve => { resolveOld = resolve; });
    mocks.capabilities.mockImplementationOnce(() => old);
    await act(() => vi.advanceTimersByTimeAsync(10000));
    const signal = mocks.capabilities.mock.calls[1][0] as AbortSignal;
    expect(result.current.serviceCheck).toBe("confirmed");
    await act(() => vi.advanceTimersByTimeAsync(10000));
    expect(signal.aborted).toBe(true);
    expect(result.current.serviceCheck).toBe("unconfirmed");
    const blocked = { ...capabilities, worker_ready: false, submit_enabled: false, admission_reason: "maintenance" };
    mocks.capabilities.mockResolvedValue(blocked);
    await act(() => vi.advanceTimersByTimeAsync(10000));
    expect(result.current.status).toEqual(blocked);
    expect(result.current.serviceCheck).toBe("confirmed");
    await act(async () => { resolveOld(capabilities); await old; });
    expect(result.current.status).toEqual(blocked);
    expect(polymerMdServiceState(false, result.current.serviceError, result.current.capabilities, result.current.status, result.current.serviceCheck).ready).toBe(false);
    expect(mocks.capabilities).toHaveBeenCalledTimes(3);
  });

  it("does not overlap slow service checks with manual refresh or periodic polling", async () => {
    mocks.capabilities.mockImplementation(() => new Promise(resolve => setTimeout(() => resolve(capabilities), 9000)));
    const { result, unmount } = renderHook(() => usePolymerMdSimulation(true, null, vi.fn()));
    await act(() => vi.advanceTimersByTimeAsync(8000));
    await act(() => result.current.refresh());
    expect(mocks.capabilities).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(1000));
    expect(result.current.serviceCheck).toBe("confirmed");
    await act(() => vi.advanceTimersByTimeAsync(9999));
    expect(mocks.capabilities).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(mocks.capabilities).toHaveBeenCalledTimes(2);
    const signal = mocks.capabilities.mock.calls[1][0] as AbortSignal;
    unmount();
    expect(signal.aborted).toBe(true);
    await act(() => vi.advanceTimersByTimeAsync(30000));
    expect(mocks.capabilities).toHaveBeenCalledTimes(2);
  });

  it("reports initial failure immediately while cancellation remains independent of service readiness", async () => {
    mocks.capabilities.mockRejectedValue(new Error("offline"));
    const { result } = renderHook(() => usePolymerMdSimulation(true, job.job_id, vi.fn()));
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(result.current.serviceCheck).toBe("unavailable");
    expect(result.current.capabilities).toBeNull();
    await act(() => result.current.mutate("cancel", job.job_id));
    expect(mocks.cancel).toHaveBeenCalledTimes(1);
    expect(result.current.job).toEqual(job);
  });

  it("clears confirmation on pause and requires a new observation after resuming", async () => {
    const { result, rerender } = renderHook(({ enabled }) => usePolymerMdSimulation(enabled, null, vi.fn()), { initialProps: { enabled: true } });
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(result.current.serviceCheck).toBe("confirmed");
    rerender({ enabled: false });
    expect(result.current.serviceCheck).toBe("checking");
    mocks.capabilities.mockImplementationOnce(() => new Promise(() => {}));
    rerender({ enabled: true });
    expect(result.current.capabilities).toEqual(capabilities);
    expect(result.current.serviceCheck).toBe("checking");
    await act(() => result.current.submit(POLYMER_MD_DEFAULTS));
    expect(mocks.create).not.toHaveBeenCalled();
    await act(() => vi.advanceTimersByTimeAsync(10000));
    expect(result.current.serviceCheck).toBe("unconfirmed");
  });

  it("discards service authority and a late response when the login session is retired", async () => {
    let resolveOld!: (value: PolymerMdCapabilities) => void;
    const old = new Promise<PolymerMdCapabilities>(resolve => { resolveOld = resolve; });
    mocks.capabilities.mockImplementationOnce(() => old);
    const { result } = renderHook(() => usePolymerMdSimulation(true, null, vi.fn()));
    const signal = mocks.capabilities.mock.calls[0][0] as AbortSignal;
    act(() => retireSession());
    expect(signal.aborted).toBe(true);
    await act(async () => { resolveOld(capabilities); await old; });
    expect(result.current.capabilities).toBeNull();
    expect(result.current.status).toBeNull();
    expect(result.current.serviceCheck).toBe("checking");
  });
});


describe("polymer MD pause lifecycle", () => {
  it("preserves the selected job while paused, stops hidden polling and resumes", async () => {
    const { result, rerender } = renderHook(({ enabled }) => usePolymerMdSimulation(enabled, job.job_id, vi.fn()), { initialProps: { enabled: true } });
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(result.current.job).toEqual(job);
    rerender({ enabled: false });
    const calls = [mocks.job.mock.calls.length, mocks.jobs.mock.calls.length, mocks.status.mock.calls.length];
    await act(() => vi.advanceTimersByTimeAsync(30000));
    expect(result.current.job).toEqual(job);
    expect(result.current.loading).toBe(false);
    expect([mocks.job.mock.calls.length, mocks.jobs.mock.calls.length, mocks.status.mock.calls.length]).toEqual(calls);
    rerender({ enabled: true });
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(mocks.job.mock.calls.length).toBe(calls[0] + 1);
    expect(result.current.job).toEqual(job);
  });

  it("aborts a paused request and rejects its late response after resuming", async () => {
    let resolveOld!: (value: PolymerMdJob) => void;
    const pending = new Promise<PolymerMdJob>(resolve => { resolveOld = resolve; });
    mocks.job.mockImplementationOnce(() => pending);
    const current = { ...job, status: "completed" } as PolymerMdJob;
    const { result, rerender } = renderHook(({ enabled }) => usePolymerMdSimulation(enabled, job.job_id, vi.fn()), { initialProps: { enabled: true } });
    const signal = mocks.job.mock.calls[0][1] as AbortSignal;
    rerender({ enabled: false });
    expect(signal.aborted).toBe(true);
    mocks.job.mockResolvedValue(current);
    rerender({ enabled: true });
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(result.current.job).toEqual(current);
    await act(async () => { resolveOld(job); await pending; });
    expect(result.current.job).toEqual(current);
  });
});
