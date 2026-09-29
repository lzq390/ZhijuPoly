// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BATCH_HISTORY_KEY, usePolymerizationBatchJob } from "./usePolymerizationBatchJob";
import type { BatchJob } from "../types/polymerizationBatch";

const { fetchJob, fetchJobs } = vi.hoisted(() => ({ fetchJob: vi.fn(), fetchJobs: vi.fn() }));
vi.mock("../services/polymerizationBatchApi", () => ({ fetchBatchJob: fetchJob, fetchBatchJobs: fetchJobs }));
const first = "a".repeat(32), second = "b".repeat(32);
function job(id: string): BatchJob {
  return { job_id: id, status: "completed", stage: "finished", target_class: "polyimide",
    summary: { raw_pairs: 0, valid_pairs: 0, unique_pairs: 0, processed_pairs: 0, computed_unique_pairs: 0, candidate_count: 0,
      tables: { a: { row_count: 0, valid_rows: 0, unique_count: 0, duplicate_rows: 0, invalid_rows: 0, blank_rows: 0 }, b: { row_count: 0, valid_rows: 0, unique_count: 0, duplicate_rows: 0, invalid_rows: 0, blank_rows: 0 } } },
    artifacts: {}, created_at: "2026-09-10T00:00:00Z", updated_at: "2026-09-10T00:00:00Z", finished_at: null, expires_at: null, error_code: null, message: null };
}
beforeEach(() => {
  fetchJob.mockReset();
  fetchJobs.mockReset().mockResolvedValue({ items: [], total: 0, next_offset: null });
  localStorage.clear();
  window.history.replaceState(null, "", `/monomer-polymerization?mode=batch&job_id=${first}`);
});
afterEach(() => cleanup());

describe("batch task identity", () => {
  it("recovers owned tasks from server pages and ignores the legacy local history", async () => {
    window.history.replaceState(null, "", "/monomer-polymerization?mode=batch");
    localStorage.setItem(BATCH_HISTORY_KEY, JSON.stringify([second]));
    fetchJobs.mockResolvedValueOnce({ items: [job(first)], total: 21, next_offset: 20 })
      .mockResolvedValueOnce({ items: [job(second)], total: 21, next_offset: null });
    fetchJob.mockResolvedValue(job(first));
    const { result } = renderHook(usePolymerizationBatchJob);
    await waitFor(() => expect(result.current.jobId).toBe(first));
    expect(result.current.history).toEqual([first]); expect(result.current.historyTotal).toBe(21);
    act(() => result.current.setHistoryOffset(20));
    await waitFor(() => expect(result.current.history).toEqual([second]));
    expect(fetchJobs).toHaveBeenLastCalledWith(20, expect.any(AbortSignal)); expect(result.current.historyNext).toBeNull();
  });
  it("can retry a failed personal list before selecting any task", async () => {
    window.history.replaceState(null, "", "/monomer-polymerization?mode=batch");
    fetchJobs.mockRejectedValueOnce(new Error("temporary list failure")).mockResolvedValueOnce({ items: [], total: 0, next_offset: null });
    const { result } = renderHook(usePolymerizationBatchJob);
    await waitFor(() => expect(result.current.historyError).toBe("temporary list failure"));
    act(() => result.current.refreshHistory());
    await waitFor(() => expect(result.current.historyLoading).toBe(false));
    expect(result.current.historyError).toBeNull(); expect(fetchJobs).toHaveBeenCalledTimes(2);
  });
  it("ignores a late response from an aborted history request", async () => {
    let resolve!: (value: BatchJob) => void;
    fetchJob.mockReturnValueOnce(new Promise((done) => { resolve = done; })).mockResolvedValue(job(second));
    const { result } = renderHook(usePolymerizationBatchJob);
    const firstSignal = fetchJob.mock.calls[0][1] as AbortSignal;
    act(() => {
      window.history.pushState(null, "", `/monomer-polymerization?mode=batch&job_id=${second}`);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(firstSignal.aborted).toBe(true);
    await waitFor(() => expect(result.current.job?.job_id).toBe(second));
    await act(async () => { resolve(job(first)); });
    expect(result.current.job?.job_id).toBe(second);
    expect(result.current.isCurrentJob(first)).toBe(false);
    expect(localStorage.getItem(BATCH_HISTORY_KEY)).toBeNull();
  });

  it("does not remember an unreadable link or accept data for a different task", async () => {
    fetchJob.mockRejectedValueOnce(new Error("任务不存在")).mockResolvedValueOnce(job(second));
    const { result } = renderHook(usePolymerizationBatchJob);
    await waitFor(() => expect(result.current.error).toBe("任务不存在"));
    expect(localStorage.getItem(BATCH_HISTORY_KEY)).toBeNull();
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.error).toBe("返回的任务标识不一致，请重新刷新。"));
    expect(result.current.job).toBeNull();
    expect(localStorage.getItem(BATCH_HISTORY_KEY)).toBeNull();
  });

  it("does not restore expired downloads when an older status request finishes later", async () => {
    fetchJob.mockResolvedValueOnce(job(first));
    const { result } = renderHook(usePolymerizationBatchJob);
    await waitFor(() => expect(result.current.job?.job_id).toBe(first));
    let resolve!: (value: BatchJob) => void;
    fetchJob.mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    act(() => result.current.refresh());
    act(() => result.current.markFilesExpired(first));
    await act(async () => { resolve(job(first)); });
    expect(result.current.job?.status).toBe("expired");
    expect(result.current.job?.artifacts).toEqual({});
  });
});
