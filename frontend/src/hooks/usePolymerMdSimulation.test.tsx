// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PolymerMdCapabilities, PolymerMdJob, PolymerMdJobPage } from "../types/polymerMd";
import { usePolymerMdSimulation } from "./usePolymerMdSimulation";

const mocks = vi.hoisted(() => ({ capabilities: vi.fn(), status: vi.fn(), jobs: vi.fn(), job: vi.fn() }));
vi.mock("../services/polymerMdApi", async importOriginal => ({
  ...await importOriginal<typeof import("../services/polymerMdApi")>(),
  fetchPolymerMdCapabilities: mocks.capabilities,
  fetchPolymerMdStatus: mocks.status,
  fetchPolymerMdJobs: mocks.jobs,
  fetchPolymerMdJob: mocks.job
}));

const capabilities = { enabled: true } as PolymerMdCapabilities;
const history: PolymerMdJobPage = { items: [], total: 0, page: 1, page_size: 10 };
const job = { job_id: "slow-job", status: "running" } as PolymerMdJob;

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  sessionStorage.clear();
  mocks.capabilities.mockResolvedValue(capabilities);
  mocks.status.mockResolvedValue(capabilities);
  mocks.jobs.mockResolvedValue(history);
  mocks.job.mockResolvedValue(job);
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
    expect(mocks.status).toHaveBeenCalledTimes(2);
    await act(() => vi.advanceTimersByTimeAsync(10000));
    expect(mocks.jobs).toHaveBeenCalledTimes(2);
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
